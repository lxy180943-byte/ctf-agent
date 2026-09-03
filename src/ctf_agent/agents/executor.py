from __future__ import annotations

from dataclasses import dataclass, field

from ctf_agent.agents.base import Agent, AgentContext
from ctf_agent.agents.planner import Plan, PlanCommand
from ctf_agent.core.models import Step
from ctf_agent.sandbox import ExecutionResult


@dataclass
class ExecutionBatch:
    results: list[ExecutionResult] = field(default_factory=list)
    skipped: list[PlanCommand] = field(default_factory=list)


class ExecutorAgent(Agent):
    def __init__(self) -> None:
        super().__init__(name="executor", role="Execute planned commands in the configured workspace sandbox.")

    def run(self, context: AgentContext) -> ExecutionBatch:
        plan = context.metadata.get("plan")
        if not isinstance(plan, Plan):
            raise ValueError("ExecutorAgent requires context.metadata['plan']")

        attempt = context.state.attempts[-1] if context.state.attempts else context.state.start_attempt()
        batch = ExecutionBatch()
        for index, plan_command in enumerate(plan.commands):
            if index >= context.max_steps:
                batch.skipped.append(plan_command)
                continue
            result = context.executor.run(
                plan_command.command,
                cwd=context.layout.work_dir,
                timeout=plan_command.timeout or context.timeout,
                env={},
            )
            step = Step(
                agent=self.name,
                action=plan_command.reason,
                command=["bash", "-lc", plan_command.command],
                artifacts=result.artifacts,
                exit_code=result.exit_code,
                started_at=result.started_at,
                ended_at=result.ended_at,
                metadata={
                    "timeout": result.timeout,
                    "timed_out": result.timed_out,
                    "cwd": result.cwd,
                    "executor": result.metadata.get("executor"),
                },
            )
            attempt.add_step(step)
            batch.results.append(result)
        return batch
