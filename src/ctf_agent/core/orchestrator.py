from __future__ import annotations

import sys
import os
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ctf_agent.agents import (
    AgentContext,
    AgentMessageBus,
    CategoryClassification,
    CategoryClassifier,
    CriticAgent,
    ExecutionBatch,
    ExecutorAgent,
    Plan,
    PlannerAgent,
    VerificationResult,
    VerifierAgent,
    specialist_for_category,
)
from ctf_agent.core.config import get_nested
from ctf_agent.core.models import Challenge, FlagCandidate
from ctf_agent.core.state import ChallengeRunState, ChallengeState
from ctf_agent.core.redaction import redact_value
from ctf_agent.core.trace import TraceEvent, TraceStore, summarize_text
from ctf_agent.core.workspace import WorkspaceLayout, WorkspaceManager
from ctf_agent.graph.builder import build_workflow
from ctf_agent.graph.checkpoint import graph_thread_id, open_run_checkpointer
from ctf_agent.graph.nodes import NodeRuntime, bind_runtime, clear_runtime
from ctf_agent.graph.reasoner_adapter import GraphReasonerAdapter
from ctf_agent.graph.state import WorkflowState, initial_workflow_state
from ctf_agent.llm import LLMProvider, PromptStore, build_provider
from ctf_agent.llm.loop import LLMActionLoop
from ctf_agent.memory import MemoryStore
from ctf_agent.knowledge import SkillIndex
from ctf_agent.platforms.base import PlatformAdapter
from ctf_agent.pydantic_agent.agent import PydanticAISolverReasoner, ReasoningError
from ctf_agent.pydantic_agent.tools import ToolDependencies
from ctf_agent.sandbox import DockerExecutor, Executor, LocalExecutor, docker_available, image_for_category
from ctf_agent.sandbox.network_policy import docker_network_policy, local_executor_network_note
from ctf_agent.tools import default_registry


@dataclass
class SolveResult:
    challenge_id: str
    state: ChallengeState
    flags: list[str] = field(default_factory=list)
    run_dir: Path | None = None
    steps_executed: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def solved(self) -> bool:
        return self.state is ChallengeState.SOLVED


class Orchestrator:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        executor_name: str | None = None,
        max_steps: int = 10,
        timeout: int | None = None,
        brain: str | None = None,
        llm_provider: LLMProvider | None = None,
        prompt_store: PromptStore | None = None,
        mode: str | None = None,
        critic_after_failures: int | None = None,
        graph_reasoner: PydanticAISolverReasoner | None = None,
    ) -> None:
        self.config = config
        self.executor_name = executor_name
        self.max_steps = max_steps
        self.timeout = int(timeout or get_nested(config, ("sandbox", "timeout_seconds")) or 60)
        self.graph_reasoner = graph_reasoner
        self.mode = mode or str(get_nested(config, ("orchestration", "mode")) or "single")
        if self.mode not in {"single", "specialist", "critic-after-failures"}:
            raise ValueError(f"Unknown orchestration mode: {self.mode}")
        self.critic_after_failures = int(
            critic_after_failures or get_nested(config, ("orchestration", "critic_after_failures")) or 2
        )
        self.brain = str(brain or get_nested(config, ("orchestration", "brain")) or "graph").lower()
        if self.brain not in {"llm", "fallback", "hybrid", "graph"}:
            raise ValueError(f"Unknown brain mode: {self.brain}")
        self.workspace = WorkspaceManager(get_nested(config, ("workspace_dir",)) or "~/ctf-workspace")
        self.llm_error: str | None = None
        if llm_provider is not None:
            self.llm_provider = llm_provider
        else:
            try:
                self.llm_provider = build_provider(config)
            except ValueError as exc:
                self.llm_error = str(exc)
                self.llm_provider = None
        self.prompt_store = prompt_store or PromptStore(get_nested(config, ("llm", "prompt_dir")))
        self.planner = PlannerAgent()
        self.executor_agent = ExecutorAgent()
        self.verifier = VerifierAgent()
        self.classifier = CategoryClassifier()
        self.critic = CriticAgent()
        self.message_bus = AgentMessageBus()
        self.tool_registry = default_registry()

    def solve(self, challenge: Challenge, *, adapter: PlatformAdapter | None = None) -> SolveResult:
        state = self.workspace.init_state(challenge)
        layout = self.workspace.layout_for(challenge.id)
        if adapter is not None:
            adapter.download_files(challenge, layout.work_dir)
        return self._run_loop(state, layout, resume=False)

    def resume_from_run_dir(self, run_dir: str | Path) -> SolveResult:
        run_path = Path(run_dir).expanduser().resolve()
        if not run_path.is_dir():
            raise FileNotFoundError(f"Run directory not found: {run_path}")
        workspace_root = run_path.parent.parent if run_path.parent.name == "runs" else self.workspace.workspace_root
        self.workspace = WorkspaceManager(workspace_root)
        resume = self.workspace.resume(run_path.name)
        return self._run_loop(resume.state, resume.layout, resume=True)

    def _run_loop(self, state: ChallengeRunState, layout: WorkspaceLayout, *, resume: bool) -> SolveResult:
        trace_store = TraceStore(layout.trace_path)
        executor = self._build_executor(state.challenge, trace_store)
        if self.llm_error:
            trace_store.append(
                TraceEvent(
                    challenge_id=state.challenge.id,
                    agent="orchestrator",
                    action="llm-disabled",
                    stderr=self.llm_error,
                    metadata={"reason": ("LLM provider configuration failed; using deterministic fallback" if self.brain != "graph" else "LLM provider configuration failed; graph mode requires PydanticAI and deterministic fallback is disabled")},
                )
            )

        if self.brain == "graph" and state.state is ChallengeState.PAUSED and not resume:
            return self._result_from_state(
                state,
                layout,
                steps_executed=0,
                metadata=self._graph_resume_metadata(state, resume),
            )
        try:
            brain_mode = self._effective_brain_mode()
        except RuntimeError as exc:
            if self.brain == "graph":
                metadata = self._record_graph_error(state, layout, trace_store, exc, resume=resume)
                return self._result_from_state(state, layout, steps_executed=0, metadata=metadata)
            raise
        if state.state is ChallengeState.SOLVED:
            return self._result_from_state(
                state,
                layout,
                steps_executed=0,
                metadata={"resumed": resume, "brain_mode": brain_mode, "brain": self._brain_metadata(brain_mode)},
            )

        context = AgentContext(
            state=state,
            layout=layout,
            trace_store=trace_store,
            executor=executor,
            tool_registry=self.tool_registry,
            config=self.config,
            max_steps=self.max_steps,
            timeout=self.timeout,
            llm_provider=self.llm_provider,
            prompt_store=self.prompt_store,
            message_bus=self.message_bus,
        )
        context.metadata["resume_requested"] = resume
        context.metadata["graph_resume_requested"] = brain_mode == "graph" and resume
        classification = self._classify(context)
        trace_store.append(
            TraceEvent(
                challenge_id=state.challenge.id,
                agent="orchestrator",
                action="brain-mode",
                stdout=brain_mode,
                metadata={
                    "brain_mode": brain_mode,
                    "brain": self._brain_metadata(brain_mode),
                    "requested_brain": self.brain,
                    "provider_available": bool(self.llm_provider and self.prompt_store and not self.llm_error),
                    "classification": classification.category,
                },
            )
        )
        steps_executed = 0
        if brain_mode == "graph":
            brain_context = self._build_brain_context(context, classification)
            context.metadata["brain_mode"] = brain_mode
            context.metadata["brain_context"] = brain_context
            trace_store.append(
                TraceEvent(
                    challenge_id=state.challenge.id,
                    agent="orchestrator",
                    action="graph-start",
                    metadata={"brain_mode": brain_mode, "classification": classification.category},
                )
            )
            try:
                workflow_state = self._invoke_graph_workflow(context)
                graph_metadata = self._apply_graph_result(state, workflow_state, layout)
                for key in ("graph_resume_requested", "graph_checkpoint_found", "graph_thread_id", "graph_resume_mode"):
                    if key in context.metadata:
                        graph_metadata[key] = context.metadata[key]
                if isinstance(state.metadata.get("graph"), dict):
                    state.metadata["graph"].update({key: graph_metadata[key] for key in ("graph_resume_requested", "graph_checkpoint_found", "graph_thread_id", "graph_resume_mode") if key in graph_metadata})
                    self.workspace.save_state(state)
            except Exception as exc:
                metadata = self._record_graph_error(state, layout, trace_store, exc, resume=resume, classification=classification)
                return self._result_from_state(state, layout, steps_executed=0, metadata=metadata)
            trace_store.append(
                TraceEvent(
                    challenge_id=state.challenge.id,
                    agent="orchestrator",
                    action="graph-finish",
                    stdout=state.state.value,
                    metadata=graph_metadata,
                )
            )
            return self._result_from_state(
                state,
                layout,
                steps_executed=graph_metadata["tool_call_count"],
                metadata=self._graph_run_metadata(resume, classification, graph_metadata, brain_context),
            )
        if brain_mode == "llm" and self.llm_provider is not None and self.prompt_store is not None:
            brain_context = self._build_brain_context(context, classification)
            context.metadata["brain_mode"] = brain_mode
            context.metadata["brain_context"] = brain_context
            loop_result = LLMActionLoop(verifier=self.verifier, save_state=self.workspace.save_state).run(context)
            steps_executed = loop_result.steps_executed
            if not loop_result.solved and not loop_result.paused:
                self._record_failure(state, "interactive LLM action loop ended without a verified flag")
            learned = self._learn_from_run(layout, state, trace_store)
            return self._result_from_state(
                state,
                layout,
                steps_executed=steps_executed,
                metadata={
                    **self._run_metadata(resume, classification, loop_result.verification, loop_result.batch, brain_mode=brain_mode, brain_context=brain_context),
                    "loop": "interactive-llm",
                    "paused": loop_result.paused,
                    "observations": [observation.to_dict() for observation in loop_result.observations],
                    "learned": learned,
                },
            )

        plan = self._initial_plan(context, classification)
        batch, verification = self._execute_plan(context, plan)
        steps_executed += len(batch.results)

        if verification.solved:
            state.transition_to(ChallengeState.SOLVED)
            self.workspace.save_state(state)
            learned = self._learn_from_run(layout, state, trace_store)
            return self._result_from_state(
                state,
                layout,
                steps_executed=steps_executed,
                metadata={**self._run_metadata(resume, classification, verification, batch, brain_mode=brain_mode), "learned": learned},
            )

        self._record_failure(state, "no verified flag candidate after initial plan")
        if self.mode == "critic-after-failures" and self._failure_count(state) >= self.critic_after_failures and steps_executed < self.max_steps:
            state.transition_to(ChallengeState.ANALYZING)
            state.start_attempt()
            context.max_steps = max(1, self.max_steps - steps_executed)
            critic_plan = self.critic.run(context)
            critic_batch, critic_verification = self._execute_plan(context, critic_plan)
            steps_executed += len(critic_batch.results)
            batch.results.extend(critic_batch.results)
            batch.skipped.extend(critic_batch.skipped)
            verification = critic_verification

        if state.attempts and state.attempts[-1].ended_at is None:
            state.attempts[-1].finish()
        state.transition_to(ChallengeState.SOLVED if verification.solved else ChallengeState.FAILED)
        self.workspace.save_state(state)
        learned = self._learn_from_run(layout, state, trace_store)
        return self._result_from_state(
            state,
            layout,
            steps_executed=steps_executed,
            metadata={**self._run_metadata(resume, classification, verification, batch, brain_mode=brain_mode), "learned": learned},
        )

    def _classify(self, context: AgentContext) -> CategoryClassification:
        classification = self.classifier.classify(context.state.challenge, context.layout.work_dir)
        context.message_bus.add_hypothesis(
            "classifier",
            f"classified as {classification.category}",
            scores=classification.scores,
            evidence=classification.evidence,
        )
        context.trace_store.append(
            TraceEvent(
                challenge_id=context.state.challenge.id,
                agent="classifier",
                action="classify",
                stdout=classification.category,
                metadata={"scores": classification.scores, "evidence": classification.evidence},
            )
        )
        context.state.metadata["classification"] = {
            "category": classification.category,
            "scores": classification.scores,
            "evidence": classification.evidence,
        }
        self.workspace.save_state(context.state)
        return classification

    def _initial_plan(self, context: AgentContext, classification: CategoryClassification) -> Plan:
        if self.mode in {"specialist", "critic-after-failures"}:
            return specialist_for_category(classification.category).run(context)
        return self.planner.run(context)

    def _effective_brain_mode(self) -> str:
        if self.brain == "fallback":
            return "fallback"
        if self.brain == "graph":
            self._ensure_graph_reasoner()
            return "graph"
        if self.llm_error or self.llm_provider is None or self.prompt_store is None:
            return "fallback"
        return "llm"

    def _ensure_graph_reasoner(self) -> None:
        if self.graph_reasoner is not None:
            return
        provider = str(os.environ.get("CTF_AGENT_LLM_PROVIDER") or get_nested(self.config, ("llm", "provider")) or "openai")
        disabled = {"none", "disabled", "off", "dry-run", "fallback", ""}
        if provider.lower().strip() in disabled:
            raise RuntimeError(self._graph_provider_setup_error(provider))
        try:
            self.graph_reasoner = PydanticAISolverReasoner(provider=provider)
        except (ReasoningError, ValueError) as exc:
            raise RuntimeError(self._graph_provider_setup_error(provider)) from exc

    def _graph_provider_setup_error(self, provider: str) -> str:
        return (
            f"graph mode requires a configured PydanticAI provider; current provider is {provider!r}. "
            "Set CTF_AGENT_LLM_PROVIDER plus provider environment variables such as OPENAI_API_KEY, OPENAI_MODEL, and OPENAI_BASE_URL; "
            "run ctf-agent doctor llm; use --brain fallback for offline deterministic mode."
        )

    def _brain_metadata(self, brain_mode: str) -> dict[str, Any]:
        return {
            "requested": self.brain,
            "effective": brain_mode,
            "provider": self.llm_provider.name if self.llm_provider is not None else None,
        }

    def _build_brain_context(self, context: AgentContext, classification: CategoryClassification) -> dict[str, Any]:
        if "memory_matches" not in context.metadata:
            context.metadata["memory_matches"] = self.planner._memory_matches(context)
        planner_plan = self.planner._deterministic_plan(context)
        specialist = specialist_for_category(classification.category)
        selected_tools = specialist.select_tools(context)
        support_tools = specialist.support_tools(context)
        pipeline = specialist.build_pipeline(context, selected_tools, support_tools)
        challenge = context.state.challenge
        query_parts = [challenge.title, challenge.category, challenge.description, *challenge.hints, *challenge.files]
        query_parts.extend(str(item) for item in (context.metadata.get("php_analysis") or []))
        skill_notes = SkillIndex.from_config(context.config).search(query_parts, category=classification.category, limit=8)
        context.metadata["relevant_skill_notes"] = skill_notes
        return {
            "memory_matches": context.metadata.get("memory_matches") or [],
            "planner": planner_plan.to_dict(),
            "relevant_skill_notes": skill_notes,
            "specialist": {
                "name": specialist.name,
                "category": specialist.category,
                "selected_tools": [tool.to_dict() for tool in selected_tools],
                "support_tools": [tool.to_dict() for tool in support_tools],
                "pipeline": pipeline.to_dict(),
            },
        }

    def _build_graph_runtime(self, context: AgentContext) -> NodeRuntime:
        tool_dependencies = ToolDependencies(context=context)
        self._ensure_graph_reasoner()
        reasoner = self.graph_reasoner
        if reasoner is None:
            raise RuntimeError("graph mode requires a configured PydanticAI provider")
        network_authorization_scope = local_executor_network_note(context.config, context.state.challenge).to_dict()
        trace_summary = [event.to_dict() for event in context.trace_store.read_events()[-8:]]
        adapter = GraphReasonerAdapter(
            reasoner,
            challenge=context.state.challenge.to_dict(),
            memory=context.metadata.get("memory_matches", []),
            skills=context.metadata.get("relevant_skill_notes", []),
            tool_capabilities=[tool.to_dict() for tool in context.tool_registry.list()],
            network_authorization_scope=network_authorization_scope,
            run_id=context.state.challenge.id,
            provider_name=self._graph_reasoner_provider_name(reasoner),
            model_name=self._graph_reasoner_model_name(reasoner),
            iteration_limits={"max_steps": context.max_steps, "timeout": context.timeout},
            trace_summary=trace_summary,
        )
        return NodeRuntime(tools=tool_dependencies, reasoner=adapter)

    def _invoke_graph_workflow(self, context: AgentContext) -> WorkflowState:
        run_dir = context.layout.challenge_dir
        thread_id = graph_thread_id(str(run_dir))
        config = {"configurable": {"thread_id": thread_id}}
        resume_requested = context.metadata.get("graph_resume_requested") is True
        try:
            runtime = self._build_graph_runtime(context)
            bind_runtime(run_dir, runtime)
            with open_run_checkpointer(run_dir) as checkpointer:
                checkpointer.setup()
                workflow = build_workflow(
                    checkpointer=checkpointer,
                    max_tool_calls=context.max_steps,
                    max_total_seconds=context.timeout,
                )
                checkpoint_found = checkpointer.get_tuple(config) is not None
                context.metadata["graph_resume_requested"] = resume_requested
                context.metadata["graph_checkpoint_found"] = checkpoint_found
                context.metadata["graph_thread_id"] = thread_id
                context.metadata["graph_resume_mode"] = "resumed" if resume_requested else "fresh"
                context.trace_store.append(
                    TraceEvent(
                        challenge_id=context.state.challenge.id,
                        agent="orchestrator",
                        action="graph-checkpoint",
                        metadata={
                            "graph_resume_requested": resume_requested,
                            "graph_checkpoint_found": checkpoint_found,
                            "graph_thread_id": thread_id,
                            "graph_resume_mode": context.metadata["graph_resume_mode"],
                        },
                    )
                )
                if resume_requested:
                    if not checkpoint_found:
                        raise RuntimeError("Cannot resume graph run: no checkpoint found for this run.")
                    final_state = workflow.invoke(None, config=config)
                else:
                    graph_state = initial_workflow_state(
                        context.state.challenge,
                        run_dir=run_dir,
                        max_iterations=max(1, context.max_steps),
                        memory_matches=context.metadata.get("memory_matches", []),
                        skill_notes=context.metadata.get("relevant_skill_notes", []),
                    )
                    final_state = workflow.invoke(
                        graph_state,
                        config=config,
                        interrupt_before=["verify_candidates"],
                    )
        except RuntimeError as exc:
            message = str(exc)
            safe_messages = (
                "Cannot resume graph run: no checkpoint found for this run.",
                "graph mode requires a configured PydanticAI provider",
                "failed to open run graph checkpointer",
                "graph workflow invocation failed without fallback",
            )
            if message in safe_messages or message.startswith(("failed to open run graph checkpointer", "graph mode requires a configured PydanticAI provider")):
                raise
            raise RuntimeError("graph workflow invocation failed without fallback") from exc
        except Exception as exc:
            raise RuntimeError("graph workflow invocation failed without fallback") from exc
        finally:
            clear_runtime(run_dir)
        if not isinstance(final_state, dict) or final_state.get("phase") == "error" or final_state.get("failure_reason"):
            raise RuntimeError("graph workflow invocation failed without fallback")
        return final_state

    def _graph_reasoner_provider_name(self, reasoner: PydanticAISolverReasoner) -> str:
        model = getattr(getattr(reasoner, "agent", None), "model", None)
        provider = getattr(model, "provider", None)
        name = getattr(provider, "name", None) or getattr(model, "provider_name", None)
        return str(name or os.environ.get("CTF_AGENT_LLM_PROVIDER") or get_nested(self.config, ("llm", "provider")) or "openai")

    def _graph_reasoner_model_name(self, reasoner: PydanticAISolverReasoner) -> str:
        model = getattr(getattr(reasoner, "agent", None), "model", None)
        name = getattr(model, "model_name", None) or getattr(model, "name", None)
        return str(name or "")

    def _apply_graph_result(self, state: ChallengeRunState, workflow_state: WorkflowState, layout: WorkspaceLayout) -> dict[str, Any]:
        graph_metadata = self._graph_result_metadata(workflow_state, layout)
        verified_candidates = self._verified_graph_candidates(workflow_state)
        graph_metadata["reported_solved"] = graph_metadata["solved"]
        graph_metadata["solved"] = bool(verified_candidates)
        state.metadata["graph"] = graph_metadata
        if verified_candidates:
            for candidate in verified_candidates:
                value = candidate.get("value")
                if not value:
                    continue
                state.add_flag_candidate(
                    FlagCandidate(
                        value=str(value),
                        source=str(candidate.get("source") or "graph.verify_candidates"),
                        confidence=float(candidate.get("confidence") or 0.0),
                        verified=True,
                        submitted=bool(candidate.get("submitted", False)),
                        metadata=dict(candidate.get("metadata") or {}),
                    )
                )
            self._transition_graph_state(state, ChallengeState.SOLVED)
        elif bool(workflow_state.get("paused")):
            self._transition_graph_state(state, ChallengeState.PAUSED)
        elif workflow_state.get("failure_reason"):
            self._transition_graph_state(state, ChallengeState.FAILED)
        elif state.state is ChallengeState.NEW:
            self._transition_graph_state(state, ChallengeState.ANALYZING)
        self.workspace.save_state(state)
        return {
            "brain": "graph",
            "graph_version": "8D",
            "iteration_count": graph_metadata["iteration"],
            "tool_call_count": len(graph_metadata["tool_calls"]),
            "hypothesis_count": len(graph_metadata["hypotheses"]),
            "graph_terminal_phase": graph_metadata["terminal_phase"],
            "graph_solved": bool(verified_candidates),
            "graph_paused": graph_metadata["paused"],
            "graph_failure_reason": graph_metadata["failure_reason"],
            "pause_reason": graph_metadata["pause_reason"],
            "pending_human_question": graph_metadata["pending_human_question"],
            "next_goal": graph_metadata["next_goal"],
        }

    def _graph_result_metadata(self, workflow_state: WorkflowState, layout: WorkspaceLayout) -> dict[str, Any]:
        metadata = {
            "version": "8D",
            "run_dir": str(layout.challenge_dir),
            "terminal_phase": workflow_state.get("phase"),
            "iteration": int(workflow_state.get("iteration") or 0),
            "max_iterations": int(workflow_state.get("max_iterations") or 0),
            "observations": workflow_state.get("observations", []),
            "hypotheses": workflow_state.get("hypotheses", []),
            "current_hypothesis": workflow_state.get("current_hypothesis"),
            "candidate_chains": workflow_state.get("candidate_chains", []),
            "tool_calls": workflow_state.get("tool_calls", []),
            "failed_actions": workflow_state.get("failed_actions", []),
            "paused": bool(workflow_state.get("paused")),
            "pause_reason": workflow_state.get("pause_reason"),
            "pending_human_question": workflow_state.get("pending_human_question"),
            "next_goal": workflow_state.get("next_goal"),
            "failure_reason": workflow_state.get("failure_reason"),
            "solved": bool(workflow_state.get("solved")),
            "verified_candidates": workflow_state.get("verified_candidates", []),
            "events": workflow_state.get("events", []),
        }
        return self._json_safe_graph_metadata(metadata)

    def _json_safe_graph_metadata(self, value: Any) -> Any:
        def shrink(item: Any) -> Any:
            if isinstance(item, str):
                return summarize_text(item, limit=4000)
            if isinstance(item, dict):
                return {str(key): shrink(val) for key, val in item.items()}
            if isinstance(item, list):
                return [shrink(val) for val in item]
            if isinstance(item, tuple):
                return [shrink(val) for val in item]
            return item
        redacted = redact_value(shrink(value))
        return json.loads(json.dumps(redacted, ensure_ascii=False, default=str))

    def _verified_graph_candidates(self, workflow_state: WorkflowState) -> list[dict[str, Any]]:
        if not workflow_state.get("solved"):
            return []
        if not any(event.get("node") == "verify_candidates" and event.get("status") == "ok" for event in workflow_state.get("events", []) if isinstance(event, dict)):
            return []
        candidates = []
        for candidate in workflow_state.get("verified_candidates", []):
            if isinstance(candidate, dict) and candidate.get("verified") and candidate.get("value"):
                candidates.append(self._json_safe_graph_metadata(candidate))
        return candidates

    def _transition_graph_state(self, state: ChallengeRunState, target: ChallengeState) -> None:
        if state.state is target:
            return
        if target is ChallengeState.SOLVED:
            if state.state is ChallengeState.NEW:
                state.transition_to(ChallengeState.ANALYZING)
            if state.state is ChallengeState.ANALYZING:
                state.transition_to(ChallengeState.VERIFYING)
            elif state.state is ChallengeState.RUNNING:
                state.transition_to(ChallengeState.VERIFYING)
            elif state.state is ChallengeState.PAUSED:
                state.transition_to(ChallengeState.VERIFYING)
            state.transition_to(ChallengeState.SOLVED)
            return
        if target is ChallengeState.PAUSED:
            if state.state is ChallengeState.SOLVED:
                return
            state.transition_to(ChallengeState.PAUSED)
            return
        if target is ChallengeState.FAILED:
            if state.state is ChallengeState.SOLVED:
                return
            if state.state is ChallengeState.FAILED:
                return
            state.transition_to(ChallengeState.FAILED)
            return
        if target is ChallengeState.ANALYZING and state.state is ChallengeState.NEW:
            state.transition_to(ChallengeState.ANALYZING)

    def _graph_run_metadata(
        self,
        resume: bool,
        classification: CategoryClassification,
        graph_metadata: dict[str, Any],
        brain_context: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "resumed": resume,
            "mode": self.mode,
            "brain_mode": "graph",
            "brain": "graph",
            "brain_details": self._brain_metadata("graph"),
            "classification": self._classification_metadata(classification),
            "brain_context": brain_context,
            **graph_metadata,
            "graph": self._json_safe_graph_metadata(graph_metadata),
        }

    def _graph_resume_metadata(self, state: ChallengeRunState, resume: bool) -> dict[str, Any]:
        graph_metadata = self._json_safe_graph_metadata(state.metadata.get("graph", {}))
        return {
            "resumed": resume,
            "mode": self.mode,
            "brain_mode": "graph",
            "brain": "graph",
            "brain_details": self._brain_metadata("graph"),
            "graph": graph_metadata,
            "pause_reason": graph_metadata.get("pause_reason"),
            "pending_human_question": graph_metadata.get("pending_human_question"),
            "next_goal": graph_metadata.get("next_goal"),
        }

    def _classification_metadata(self, classification: CategoryClassification | None) -> dict[str, Any] | None:
        if classification is None:
            return None
        return {
            "category": classification.category,
            "scores": classification.scores,
            "evidence": classification.evidence,
        }

    def _record_graph_error(
        self,
        state: ChallengeRunState,
        layout: WorkspaceLayout,
        trace_store: TraceStore,
        error: Exception,
        *,
        resume: bool,
        classification: CategoryClassification | None = None,
    ) -> dict[str, Any]:
        reason = summarize_text(str(redact_value(str(error))), limit=1000) or "graph workflow failed"
        trace_store.append(
            TraceEvent(
                challenge_id=state.challenge.id,
                agent="orchestrator",
                action="graph-error",
                stderr=reason,
                metadata={"brain_mode": "graph", "classification": self._classification_metadata(classification)},
            )
        )
        state.metadata["graph"] = self._json_safe_graph_metadata(
            {
                "version": "8D",
                "run_dir": str(layout.challenge_dir),
                "terminal_phase": "error",
                "iteration": 0,
                "max_iterations": self.max_steps,
                "tool_calls": [],
                "hypotheses": [],
                "paused": False,
                "pause_reason": None,
                "pending_human_question": None,
                "next_goal": None,
                "failure_reason": reason,
                "solved": False,
            }
        )
        self._transition_graph_state(state, ChallengeState.FAILED)
        self.workspace.save_state(state)
        return {
            "resumed": resume,
            "mode": self.mode,
            "brain_mode": "graph",
            "brain": "graph",
            "brain_details": self._brain_metadata("graph"),
            "classification": self._classification_metadata(classification),
            "graph_version": "8D",
            "iteration_count": 0,
            "tool_call_count": 0,
            "hypothesis_count": 0,
            "graph_terminal_phase": "error",
            "graph_solved": False,
            "graph_paused": False,
            "graph_failure_reason": reason,
            "pause_reason": None,
            "pending_human_question": None,
            "next_goal": None,
            "graph": state.metadata["graph"],
        }

    def _execute_plan(self, context: AgentContext, plan: Plan) -> tuple[ExecutionBatch, VerificationResult]:
        state = context.state
        if not state.attempts or state.attempts[-1].ended_at is not None:
            state.start_attempt()
        state.transition_to(ChallengeState.ANALYZING)
        self.workspace.save_state(state)

        if not plan.commands:
            self._record_failure(state, "empty plan")
            return ExecutionBatch(), VerificationResult()

        state.transition_to(ChallengeState.RUNNING)
        self.workspace.save_state(state)
        context.metadata["plan"] = plan
        batch = self.executor_agent.run(context)

        state.transition_to(ChallengeState.VERIFYING)
        self.workspace.save_state(state)
        context.metadata["execution_batch"] = batch
        verification = self.verifier.run(context)
        for candidate in verification.candidates:
            context.message_bus.add_flag_candidate("verifier", candidate)
        if state.attempts:
            state.attempts[-1].finish()
        self.workspace.save_state(state)
        return batch, verification

    def _record_failure(self, state: ChallengeRunState, reason: str) -> None:
        state.metadata["failure_count"] = self._failure_count(state) + 1
        reviews = state.metadata.setdefault("failure_reviews", [])
        if isinstance(reviews, list):
            reviews.append(
                {
                    "wrong_hypotheses": ["Initial route did not produce a verified flag candidate."],
                    "invalid_commands": [],
                    "next_suggestions": [
                        "Review trace summaries and artifact outputs before adding more commands.",
                        "Try a broader non-destructive scan or category-specific specialist route.",
                    ],
                    "reason": reason,
                }
            )
        self.message_bus.add_failure("orchestrator", reason, failure_count=state.metadata["failure_count"])
        self.workspace.save_state(state)

    def _failure_count(self, state: ChallengeRunState) -> int:
        return int(state.metadata.get("failure_count", 0))

    def _run_metadata(
        self,
        resume: bool,
        classification: CategoryClassification,
        verification: VerificationResult,
        batch: ExecutionBatch,
        *,
        brain_mode: str,
        brain_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        data = {
            "resumed": resume,
            "mode": self.mode,
            "brain_mode": brain_mode,
            "brain": self._brain_metadata(brain_mode),
            "classification": {
                "category": classification.category,
                "scores": classification.scores,
                "evidence": classification.evidence,
            },
            "verification": _verification_metadata(verification),
            "execution": _execution_metadata(batch),
            "message_bus": self.message_bus.to_dict(),
        }
        if brain_context is not None:
            data["brain_context"] = brain_context
        return data

    def _build_executor(self, challenge: Challenge, trace_store: TraceStore) -> Executor:
        explicit_executor = self.executor_name is not None
        executor_name = self.executor_name or str(get_nested(self.config, ("sandbox", "engine")) or "docker")
        if executor_name == "local":
            self._trace_network_policy(trace_store, challenge, local_executor_network_note(self.config, challenge), "local")
            return LocalExecutor(self.workspace.workspace_root, trace_store=trace_store, challenge_id=challenge.id)
        if executor_name == "docker":
            if not docker_available():
                if explicit_executor:
                    raise RuntimeError("Docker is not available; cannot use explicit docker executor.")
                print("Docker is not available; falling back to local executor.", file=sys.stderr)
                self._trace_network_policy(trace_store, challenge, local_executor_network_note(self.config, challenge), "local")
                return LocalExecutor(self.workspace.workspace_root, trace_store=trace_store, challenge_id=challenge.id)
            network_policy = docker_network_policy(self.config, challenge)
            self._trace_network_policy(trace_store, challenge, network_policy, "docker")
            return DockerExecutor(
                self.workspace.workspace_root,
                image=image_for_category(self.config, challenge.category),
                network=network_policy.effective_network,
                memory=get_nested(self.config, ("sandbox", "memory")),
                cpu=get_nested(self.config, ("sandbox", "cpu")),
                trace_store=trace_store,
                challenge_id=challenge.id,
            )
        raise ValueError(f"Unknown executor: {executor_name}")

    def _trace_network_policy(self, trace_store: TraceStore, challenge: Challenge, policy, executor_name: str) -> None:
        trace_store.append(
            TraceEvent(
                challenge_id=challenge.id,
                agent="orchestrator",
                action="network-authorization",
                stdout=policy.reason,
                metadata={
                    "executor": executor_name,
                    "policy": policy.to_dict(),
                    "connection_authorization": {
                        "source": challenge.metadata.get("source") or "challenge",
                        "profile": challenge.metadata.get("profile"),
                        "source_dir": challenge.metadata.get("source_dir"),
                        "connection_present": bool(challenge.connection),
                    },
                },
            )
        )

    def _learn_from_run(self, layout: WorkspaceLayout, state: ChallengeRunState, trace_store: TraceStore) -> list[dict[str, Any]]:
        if not isinstance(self.config.get("memory"), dict):
            return []
        if get_nested(self.config, ("memory", "enabled")) is False or get_nested(self.config, ("memory", "auto_learn")) is False:
            return []
        try:
            items = MemoryStore.from_config(self.config).learn_from_run(layout.challenge_dir)
        except Exception as exc:
            trace_store.append(
                TraceEvent(
                    challenge_id=state.challenge.id,
                    agent="memory",
                    action="learn-failed",
                    stderr=str(exc),
                    metadata={"source_run": str(layout.challenge_dir)},
                )
            )
            return []
        trace_store.append(
            TraceEvent(
                challenge_id=state.challenge.id,
                agent="memory",
                action="learn",
                stdout=f"learned {len(items)} knowledge item(s)",
                metadata={"items": [item.to_dict() for item in items], "source_run": str(layout.challenge_dir)},
            )
        )
        return [item.to_dict() for item in items]

    def _result_from_state(
        self,
        state: ChallengeRunState,
        layout: WorkspaceLayout,
        *,
        steps_executed: int,
        metadata: dict[str, Any] | None = None,
    ) -> SolveResult:
        flags = [candidate.value for candidate in state.flag_candidates if candidate.verified]
        return SolveResult(
            challenge_id=state.challenge.id,
            state=state.state,
            flags=flags,
            run_dir=layout.challenge_dir,
            steps_executed=steps_executed,
            metadata=metadata or {},
        )


def _verification_metadata(verification: VerificationResult) -> dict[str, Any]:
    return {
        "solved": verification.solved,
        "candidates": [candidate.to_dict() for candidate in verification.candidates],
    }


def _execution_metadata(batch: ExecutionBatch) -> dict[str, Any]:
    return {
        "results": [
            {
                "command": result.command,
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "artifacts": [artifact.to_dict() for artifact in result.artifacts],
            }
            for result in batch.results
        ],
        "skipped": [command.to_dict() for command in batch.skipped],
    }
