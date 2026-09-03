from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from statistics import mean
from typing import Any

from ctf_agent.core.config import get_nested
from ctf_agent.core.models import utc_now
from ctf_agent.core.orchestrator import Orchestrator, SolveResult
from ctf_agent.core.state import ChallengeRunState
from ctf_agent.core.trace import TraceEvent, TraceStore, summarize_text
from ctf_agent.evals.base import BenchmarkAdapter, BenchmarkChallenge, sanitize_benchmark_for_solver
from ctf_agent.evals.local import LocalBenchmark
from ctf_agent.llm import LLMProvider
from ctf_agent.memory import KnowledgeItem, MemoryStore


@dataclass
class Scorecard:
    solved: bool
    false_positive: bool
    tools_used: list[str] = field(default_factory=list)
    stuck_stage: str = "solved"
    next_suggestions: list[str] = field(default_factory=list)
    max_time_exceeded: bool = False
    required_tools: list[str] = field(default_factory=list)
    missing_required_tools: list[str] = field(default_factory=list)
    trace_summary: list[str] = field(default_factory=list)
    evidence_steps: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "solved": self.solved,
            "false_positive": self.false_positive,
            "tools_used": list(self.tools_used),
            "stuck_stage": self.stuck_stage,
            "next_suggestions": list(self.next_suggestions),
            "max_time_exceeded": self.max_time_exceeded,
            "required_tools": list(self.required_tools),
            "missing_required_tools": list(self.missing_required_tools),
            "trace_summary": list(self.trace_summary),
            "evidence_steps": list(self.evidence_steps),
        }


@dataclass
class EvalChallengeResult:
    challenge_id: str
    title: str
    category: str
    solved: bool
    expected_flags: list[str]
    found_flags: list[str]
    steps_used: int
    time_used: float
    command_count: int
    verifier_false_positive: bool
    resume_success: bool
    run_dir: str | None
    expected_flag_matched: bool = False
    solve_success: bool = False
    repeat_index: int = 1
    max_time: float | None = None
    difficulty: str = "unknown"
    tags: list[str] = field(default_factory=list)
    required_tools: list[str] = field(default_factory=list)
    scorecard: Scorecard = field(default_factory=lambda: Scorecard(solved=False, false_positive=False))
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        isolated = _expected_flag_isolated(self.metadata)
        data = {
            "challenge_id": self.challenge_id,
            "title": self.title,
            "category": self.category,
            "solved": self.solved,
            "expected_flag_matched": self.expected_flag_matched,
            "expected_flag_count": len(self.expected_flags),
            "found_flag_count": len(self.found_flags),
            "solve_success": self.solve_success,
            "steps_used": self.steps_used,
            "time_used": self.time_used,
            "command_count": self.command_count,
            "verifier_false_positive": self.verifier_false_positive,
            "resume_success": self.resume_success,
            "run_dir": self.run_dir,
            "repeat_index": self.repeat_index,
            "max_time": self.max_time,
            "difficulty": self.difficulty,
            "tags": list(self.tags),
            "required_tools": list(self.required_tools),
            "scorecard": self.scorecard.to_dict(),
            "error": self.error,
            "metadata": dict(self.metadata),
        }
        if not isolated:
            data["expected_flags"] = list(self.expected_flags)
            data["found_flags"] = list(self.found_flags)
        return data


@dataclass
class RegressionComparison:
    repeat_index: int
    solved_count: int
    steps_used: int
    time_used: float
    solved_delta: int
    steps_delta: int
    time_delta: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "repeat_index": self.repeat_index,
            "solved_count": self.solved_count,
            "steps_used": self.steps_used,
            "time_used": self.time_used,
            "solved_delta": self.solved_delta,
            "steps_delta": self.steps_delta,
            "time_delta": self.time_delta,
        }


@dataclass
class EvalSummary:
    dataset: str
    output_dir: Path
    results: list[EvalChallengeResult]
    started_at: str
    ended_at: str
    repeat: int = 1
    filters: dict[str, Any] = field(default_factory=dict)
    regression: list[RegressionComparison] = field(default_factory=list)

    @property
    def solved_count(self) -> int:
        return sum(1 for result in self.results if result.solved)

    @property
    def steps_used(self) -> int:
        return sum(result.steps_used for result in self.results)

    @property
    def time_used(self) -> float:
        return round(sum(result.time_used for result in self.results), 6)

    @property
    def command_count(self) -> int:
        return sum(result.command_count for result in self.results)

    @property
    def verifier_false_positive(self) -> int:
        return sum(1 for result in self.results if result.verifier_false_positive)

    @property
    def resume_success(self) -> int:
        return sum(1 for result in self.results if result.resume_success)

    def metrics(self) -> dict[str, Any]:
        return {
            "challenge_count": len(self.results),
            "unique_challenge_count": len({result.challenge_id for result in self.results}),
            "repeat": self.repeat,
            "solved_count": self.solved_count,
            "steps_used": self.steps_used,
            "time_used": self.time_used,
            "command_count": self.command_count,
            "verifier_false_positive": self.verifier_false_positive,
            "resume_success": self.resume_success,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset": self.dataset,
            "output_dir": str(self.output_dir),
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "filters": dict(self.filters),
            "metrics": self.metrics(),
            "capability_gaps": capability_gap_summary(self),
            "regression": [item.to_dict() for item in self.regression],
            "results": [result.to_dict() for result in self.results],
        }


class BenchmarkRunner:
    def __init__(
        self,
        config: dict[str, Any],
        *,
        max_steps: int = 20,
        executor_name: str | None = None,
        timeout: int | None = None,
        brain: str | None = None,
        mode: str | None = None,
        output_dir: str | Path | None = None,
        only_category: str | None = None,
        only_tag: str | None = None,
        fail_fast: bool = False,
        repeat: int = 1,
        regression: bool = False,
        write_memory: bool = True,
        llm_provider: LLMProvider | None = None,
    ) -> None:
        self.config = config
        self.max_steps = max_steps
        self.executor_name = executor_name
        self.timeout = timeout
        self.brain = brain
        self.mode = mode
        self.output_dir = Path(output_dir).expanduser() if output_dir else None
        self.only_category = only_category
        self.only_tag = only_tag
        self.fail_fast = fail_fast
        self.repeat = max(1, int(repeat))
        self.regression = regression or self.repeat > 1
        self.write_memory = write_memory
        self.llm_provider = llm_provider

    def run(self, adapter: BenchmarkAdapter) -> EvalSummary:
        started_at = utc_now()
        output_dir = self._output_dir(adapter)
        output_dir.mkdir(parents=True, exist_ok=True)
        results_path = output_dir / "eval_results.jsonl"
        report_path = output_dir / "eval_report.md"
        results: list[EvalChallengeResult] = []
        challenges = self._filtered_challenges(adapter)

        with results_path.open("w", encoding="utf-8") as handle:
            stop = False
            for repeat_index in range(1, self.repeat + 1):
                for item in challenges:
                    result = self._run_one(item, repeat_index=repeat_index)
                    results.append(result)
                    handle.write(json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True) + "\n")
                    if self.write_memory:
                        self._write_eval_memory(adapter, item, result)
                    if self.fail_fast and not result.solved:
                        stop = True
                        break
                if stop:
                    break

        summary = EvalSummary(
            dataset=adapter.name,
            output_dir=output_dir,
            results=results,
            started_at=started_at,
            ended_at=utc_now(),
            repeat=self.repeat,
            filters={"only_category": self.only_category, "only_tag": self.only_tag, "fail_fast": self.fail_fast},
            regression=self._regression(results) if self.regression else [],
        )
        report_path.write_text(render_eval_report(summary), encoding="utf-8")
        (output_dir / "capability_gaps.md").write_text(render_capability_gap_report(summary), encoding="utf-8")
        (output_dir / "eval_summary.json").write_text(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        return summary

    def _filtered_challenges(self, adapter: BenchmarkAdapter) -> list[BenchmarkChallenge]:
        challenges = adapter.list_challenges()
        if self.only_category:
            challenges = [item for item in challenges if item.challenge.category == self.only_category]
        if self.only_tag:
            challenges = [item for item in challenges if self.only_tag in item.tags]
        return challenges

    def _run_one(self, item: BenchmarkChallenge, *, repeat_index: int) -> EvalChallengeResult:
        challenge = sanitize_benchmark_for_solver(item)
        start = time.monotonic()
        try:
            orchestrator = Orchestrator(
                self.config,
                executor_name=self.executor_name,
                max_steps=self.max_steps,
                timeout=self.timeout,
                brain=self.brain,
                llm_provider=self.llm_provider,
                mode=self.mode,
            )
            solve_result = orchestrator.solve(challenge, adapter=item.adapter)
            elapsed = round(time.monotonic() - start, 6)
            expected_flag_matched = _expected_flag_matched(solve_result, item.expected_flags)
            false_positive = _false_positive(solve_result, item.expected_flags)
            solve_success = _solve_success(solve_result, item.expected_flags, expected_flag_matched)
            resume_success = _resume_success(self.config, solve_result, self.executor_name, self.timeout, self.brain, self.mode)
            command_count = _command_count(solve_result)
            events = _events_for_run(solve_result)
            scorecard = _scorecard(item, solve_result, false_positive, elapsed, events, error=None)
            return EvalChallengeResult(
                challenge_id=challenge.id,
                title=challenge.title,
                category=challenge.category,
                solved=solve_success and not false_positive and not scorecard.max_time_exceeded,
                expected_flags=item.expected_flags,
                found_flags=solve_result.flags,
                steps_used=solve_result.steps_executed,
                time_used=elapsed,
                command_count=command_count,
                verifier_false_positive=false_positive,
                resume_success=resume_success,
                expected_flag_matched=expected_flag_matched,
                solve_success=solve_success,
                run_dir=str(solve_result.run_dir) if solve_result.run_dir else None,
                repeat_index=repeat_index,
                max_time=item.max_time,
                difficulty=item.difficulty,
                tags=item.tags,
                required_tools=item.required_tools,
                scorecard=scorecard,
                metadata={"solve": solve_result.metadata, **item.metadata},
            )
        except Exception as exc:
            elapsed = round(time.monotonic() - start, 6)
            scorecard = Scorecard(
                solved=False,
                false_positive=False,
                tools_used=[],
                stuck_stage="exception",
                next_suggestions=["Inspect exception details and verify benchmark metadata/files before rerunning."],
                max_time_exceeded=bool(item.max_time and elapsed > item.max_time),
                required_tools=item.required_tools,
                missing_required_tools=list(item.required_tools),
                trace_summary=[],
            )
            return EvalChallengeResult(
                challenge_id=challenge.id,
                title=challenge.title,
                category=challenge.category,
                solved=False,
                expected_flags=item.expected_flags,
                found_flags=[],
                steps_used=0,
                time_used=elapsed,
                command_count=0,
                verifier_false_positive=False,
                resume_success=False,
                expected_flag_matched=False,
                solve_success=False,
                run_dir=None,
                repeat_index=repeat_index,
                max_time=item.max_time,
                difficulty=item.difficulty,
                tags=item.tags,
                required_tools=item.required_tools,
                scorecard=scorecard,
                error=str(exc),
                metadata=item.metadata,
            )

    def _output_dir(self, adapter: BenchmarkAdapter) -> Path:
        if self.output_dir:
            return self.output_dir
        workspace = Path(get_nested(self.config, ("workspace_dir",)) or "~/ctf-workspace").expanduser()
        stamp = utc_now().replace(":", "").replace("-", "").replace(".", "")
        return workspace / "evals" / f"{adapter.name}-{stamp}"

    def _regression(self, results: list[EvalChallengeResult]) -> list[RegressionComparison]:
        by_repeat: dict[int, list[EvalChallengeResult]] = {}
        for result in results:
            by_repeat.setdefault(result.repeat_index, []).append(result)
        if not by_repeat:
            return []
        baseline = _repeat_metrics(by_repeat[min(by_repeat)])
        comparisons: list[RegressionComparison] = []
        for repeat_index in sorted(by_repeat):
            current = _repeat_metrics(by_repeat[repeat_index])
            comparisons.append(
                RegressionComparison(
                    repeat_index=repeat_index,
                    solved_count=current["solved_count"],
                    steps_used=current["steps_used"],
                    time_used=current["time_used"],
                    solved_delta=current["solved_count"] - baseline["solved_count"],
                    steps_delta=current["steps_used"] - baseline["steps_used"],
                    time_delta=round(current["time_used"] - baseline["time_used"], 6),
                )
            )
        return comparisons

    def _write_eval_memory(self, adapter: BenchmarkAdapter, item: BenchmarkChallenge, result: EvalChallengeResult) -> None:
        memory_cfg = self.config.get("memory")
        if not isinstance(memory_cfg, dict) or get_nested(self.config, ("memory", "enabled")) is False:
            return
        try:
            store = MemoryStore.from_config(self.config)
            commands = _commands_for_run(result.run_dir) if result.run_dir else []
            store.add(
                KnowledgeItem(
                    category=item.challenge.category,
                    pattern=f"benchmark:{adapter.name}:{item.challenge.id}:{'solved' if result.solved else 'failed'}",
                    symptom=f"difficulty={item.difficulty}; tags={','.join(item.tags)}; stuck_stage={result.scorecard.stuck_stage}",
                    solution=("Solved benchmark route." if result.solved else "Benchmark failure; review scorecard suggestions.") + " " + " ".join(result.scorecard.next_suggestions[:2]),
                    commands=commands[:8],
                    source_run=result.run_dir or f"benchmark:{adapter.name}:{item.challenge.id}",
                    confidence=0.65 if result.solved else 0.3,
                    success_count=1 if result.solved else 0,
                    failure_count=0 if result.solved else 1,
                    source_type="benchmark",
                    metadata={
                        "kind": "eval-benchmark-result",
                        "experience_scope": "benchmark",
                        "source_type": "benchmark",
                        "benchmark": adapter.name,
                        "challenge_id": item.challenge.id,
                        "repeat_index": result.repeat_index,
                        "scorecard": result.scorecard.to_dict(),
                    },
                )
            )
        except Exception:
            return


def adapter_for_path(path: str | Path) -> BenchmarkAdapter:
    return LocalBenchmark(path)


def render_eval_report(summary: EvalSummary) -> str:
    metrics = summary.metrics()
    lines = [
        f"# Eval Report: {summary.dataset}",
        "",
        f"- started_at: `{summary.started_at}`",
        f"- ended_at: `{summary.ended_at}`",
        f"- output_dir: `{summary.output_dir}`",
        f"- filters: `{json.dumps(summary.filters, ensure_ascii=False, sort_keys=True)}`",
        "",
        "## Metrics",
        "",
    ]
    for key in ("challenge_count", "unique_challenge_count", "repeat", "solved_count", "steps_used", "time_used", "command_count", "verifier_false_positive", "resume_success"):
        lines.append(f"- {key}: `{metrics[key]}`")

    gap = capability_gap_summary(summary)
    lines.extend(["", "## Capability Gaps", ""])
    lines.append(f"- weak_categories: `{', '.join(gap['weak_categories']) or '-'}`")
    lines.append(f"- missing_tools: `{', '.join(gap['missing_tools']) or '-'}`")
    lines.append(f"- verifier_false_positive_categories: `{', '.join(gap['verifier_false_positive_categories']) or '-'}`")
    lines.append(f"- common_stuck_stages: `{json.dumps(gap['stuck_stages'], ensure_ascii=False, sort_keys=True)}`")
    lines.append(f"- detail_report: `{summary.output_dir / 'capability_gaps.md'}`")

    if summary.regression:
        lines.extend(["", "## Regression", ""])
        for item in summary.regression:
            lines.append(
                f"- repeat {item.repeat_index}: solved={item.solved_count} ({item.solved_delta:+}), "
                f"steps={item.steps_used} ({item.steps_delta:+}), time={item.time_used:.6f}s ({item.time_delta:+.6f}s)"
            )

    lines.extend(["", "## Results", ""])
    for result in summary.results:
        isolated = _expected_flag_isolated(result.metadata)
        expected = None if isolated else (", ".join(result.expected_flags) if result.expected_flags else "<not provided>")
        found = "<redacted>" if isolated and result.found_flags else (", ".join(result.found_flags) if result.found_flags else "<none>")
        lines.extend(
            [
                f"### {result.challenge_id}: {result.title}",
                "",
                f"- repeat_index: `{result.repeat_index}`",
                f"- category: `{result.category}`",
                f"- difficulty: `{result.difficulty}`",
                f"- tags: `{', '.join(result.tags) or '-'}`",
                f"- required_tools: `{', '.join(result.required_tools) or '-'}`",
                f"- solved: `{result.solved}`",
                f"- false_positive: `{result.verifier_false_positive}`",
                f"- expected_flag_matched: `{result.expected_flag_matched}`",
                f"- stuck_stage: `{result.scorecard.stuck_stage}`",
                f"- tools_used: `{', '.join(result.scorecard.tools_used) or '-'}`",
                f"- max_time: `{result.max_time if result.max_time is not None else ''}`",
                f"- max_time_exceeded: `{result.scorecard.max_time_exceeded}`",
                f"- steps_used: `{result.steps_used}`",
                f"- time_used: `{result.time_used}`",
                f"- command_count: `{result.command_count}`",
                f"- resume_success: `{result.resume_success}`",
                ("- isolated_expected_flag_count: `" + str(len(result.expected_flags)) + "`") if expected is None else ("- expected_flags: `" + expected + "`"),
                f"- found_flags: `{found}`",
                f"- run_dir: `{result.run_dir or ''}`",
            ]
        )
        if result.error:
            lines.append(f"- error: `{result.error}`")
        if result.scorecard.evidence_steps:
            lines.extend(["", "Evidence steps:"])
            lines.extend(f"- {item}" for item in result.scorecard.evidence_steps)
        if not result.solved:
            lines.extend(["", "Trace summary:"])
            if result.scorecard.trace_summary:
                lines.extend(f"- {item}" for item in result.scorecard.trace_summary)
            else:
                lines.append("- No trace summary available.")
            lines.extend(["", "Next suggestions:"])
            lines.extend(f"- {item}" for item in result.scorecard.next_suggestions)
        lines.append("")
    return "\n".join(lines)


def capability_gap_summary(summary: EvalSummary) -> dict[str, Any]:
    by_category: dict[str, dict[str, Any]] = {}
    missing_tools: dict[str, int] = {}
    false_positive_categories: dict[str, int] = {}
    stuck_stages: dict[str, int] = {}
    for result in summary.results:
        row = by_category.setdefault(result.category, {"total": 0, "solved": 0, "false_positive": 0, "steps": []})
        row["total"] += 1
        row["solved"] += 1 if result.solved else 0
        row["false_positive"] += 1 if result.verifier_false_positive else 0
        row["steps"].append(result.steps_used)
        if result.verifier_false_positive:
            false_positive_categories[result.category] = false_positive_categories.get(result.category, 0) + 1
        stuck_stages[result.scorecard.stuck_stage] = stuck_stages.get(result.scorecard.stuck_stage, 0) + 1
        for tool in result.scorecard.missing_required_tools:
            missing_tools[tool] = missing_tools.get(tool, 0) + 1
    weak = []
    for category, row in by_category.items():
        if row["solved"] < row["total"] or row["false_positive"]:
            weak.append(category)
    return {
        "weak_categories": sorted(weak),
        "missing_tools": [tool for tool, _ in sorted(missing_tools.items(), key=lambda item: (-item[1], item[0]))],
        "verifier_false_positive_categories": [category for category, _ in sorted(false_positive_categories.items(), key=lambda item: (-item[1], item[0]))],
        "stuck_stages": dict(sorted(stuck_stages.items())),
        "by_category": by_category,
    }


def render_capability_gap_report(summary: EvalSummary) -> str:
    gap = capability_gap_summary(summary)
    lines = [
        f"# Capability Gap Report: {summary.dataset}",
        "",
        "## Category Strength",
        "",
    ]
    for category, row in sorted(gap["by_category"].items()):
        total = int(row["total"])
        solved = int(row["solved"])
        solve_rate = solved / total if total else 0.0
        avg_steps = mean(row["steps"]) if row["steps"] else 0.0
        lines.append(f"- {category}: solved={solved}/{total} solve_rate={solve_rate:.2f} false_positive={row['false_positive']} avg_steps={avg_steps:.2f}")
    lines.extend(["", "## Missing Tools", ""])
    if gap["missing_tools"]:
        for tool in gap["missing_tools"]:
            count = sum(1 for result in summary.results if tool in result.scorecard.missing_required_tools)
            lines.append(f"- {tool}: missing in {count} result(s)")
    else:
        lines.append("- No missing required tools detected.")
    lines.extend(["", "## Verifier Risks", ""])
    if gap["verifier_false_positive_categories"]:
        for category in gap["verifier_false_positive_categories"]:
            count = sum(1 for result in summary.results if result.category == category and result.verifier_false_positive)
            lines.append(f"- {category}: {count} false positive result(s); tighten flag_regex or expected flag checks.")
    else:
        lines.append("- No verifier false positives detected.")
    lines.extend(["", "## Stuck Stages", ""])
    for stage, count in gap["stuck_stages"].items():
        lines.append(f"- {stage}: {count}")
    lines.extend(["", "## Next Actions", ""])
    if gap["weak_categories"]:
        lines.append("- Prioritize specialist pipeline and verifier improvements for: " + ", ".join(gap["weak_categories"]))
    if gap["missing_tools"]:
        lines.append("- Install or remap required tools: " + ", ".join(gap["missing_tools"]))
    if not gap["weak_categories"] and not gap["missing_tools"] and not gap["verifier_false_positive_categories"]:
        lines.append("- No capability gap detected in this dataset run; keep this report as the regression baseline.")
    return "\n".join(lines) + "\n"


def _expected_flag_matched(result: SolveResult, expected_flags: list[str]) -> bool:
    if not expected_flags:
        return False
    return any(flag in expected_flags for flag in result.flags)


def _solve_success(result: SolveResult, expected_flags: list[str], expected_flag_matched: bool) -> bool:
    if expected_flags:
        return result.solved and expected_flag_matched
    return result.solved


def _expected_flag_isolated(metadata: dict[str, Any]) -> bool:
    return bool(metadata.get("expected_flag_isolated") or metadata.get("benchmark_type") == "reasoning")


def _false_positive(result: SolveResult, expected_flags: list[str]) -> bool:
    if not result.solved or not expected_flags:
        return False
    return not any(flag in expected_flags for flag in result.flags)


def _resume_success(config: dict[str, Any], result: SolveResult, executor_name: str | None, timeout: int | None, brain: str | None, mode: str | None) -> bool:
    if not result.run_dir:
        return False
    try:
        resumed = Orchestrator(config, executor_name=executor_name, timeout=timeout, brain=brain, mode=mode).resume_from_run_dir(result.run_dir)
    except Exception:
        return False
    return resumed.solved == result.solved and set(resumed.flags) >= set(result.flags)


def _command_count(result: SolveResult) -> int:
    return sum(1 for event in _events_for_run(result) if event.agent == "executor" and event.action == "run-command")


def _events_for_run(result: SolveResult) -> list[TraceEvent]:
    if not result.run_dir:
        return []
    try:
        run_dir = Path(result.run_dir)
        state = ChallengeRunState.from_dict(json.loads((run_dir / "state.json").read_text(encoding="utf-8")))
        return [event for event in TraceStore(run_dir / "trace.jsonl").read_events() if event.timestamp >= state.created_at]
    except Exception:
        return []


def _scorecard(item: BenchmarkChallenge, result: SolveResult, false_positive: bool, elapsed: float, events: list[TraceEvent], *, error: str | None) -> Scorecard:
    solved = result.solved and not false_positive
    max_time_exceeded = bool(item.max_time is not None and elapsed > item.max_time)
    tools = _tools_used(result, events)
    missing_required = [tool for tool in item.required_tools if tool not in tools]
    stuck_stage = "solved" if solved and not max_time_exceeded else _stuck_stage(result, events, error, false_positive, max_time_exceeded)
    return Scorecard(
        solved=solved and not max_time_exceeded,
        false_positive=false_positive,
        tools_used=tools,
        stuck_stage=stuck_stage,
        next_suggestions=_next_suggestions(item, stuck_stage, false_positive, missing_required),
        max_time_exceeded=max_time_exceeded,
        required_tools=item.required_tools,
        missing_required_tools=missing_required if not solved else [],
        trace_summary=_trace_summary(events),
        evidence_steps=_evidence_steps(result),
    )


def _tools_used(result: SolveResult, events: list[TraceEvent]) -> list[str]:
    tools: list[str] = []
    solve_meta = result.metadata.get("execution", {}) if isinstance(result.metadata, dict) else {}
    if isinstance(solve_meta, dict):
        for item in solve_meta.get("results", []):
            if isinstance(item, dict):
                command = str(item.get("command") or "")
                _add_tool_from_command(tools, command)
    for event in events:
        if event.agent == "executor" and event.action == "run-command":
            _add_tool_from_command(tools, _format_command(event.command))
        plan = event.metadata.get("plan") if isinstance(event.metadata, dict) else None
        if isinstance(plan, dict):
            for command in plan.get("commands", []):
                if isinstance(command, dict):
                    tool = command.get("metadata", {}).get("tool") if isinstance(command.get("metadata"), dict) else None
                    if tool and str(tool) not in tools:
                        tools.append(str(tool))
    return sorted(tools)


def _add_tool_from_command(tools: list[str], command: str) -> None:
    first = command.strip().split(" ", 1)[0] if command.strip() else ""
    if first:
        first = Path(first).name
        if first not in tools:
            tools.append(first)


def _stuck_stage(result: SolveResult, events: list[TraceEvent], error: str | None, false_positive: bool, max_time_exceeded: bool) -> str:
    if error:
        return "exception"
    if max_time_exceeded:
        return "budget"
    if false_positive:
        return "verifying"
    actions = [event.action for event in events]
    if any("plan" in action or "triage" in action for action in actions) and not any(action == "run-command" for action in actions):
        return "planning"
    if any(action == "run-command" for action in actions) and not result.flags:
        return "verifying"
    if result.state.value == "failed":
        return "failed"
    return result.state.value


def _next_suggestions(item: BenchmarkChallenge, stuck_stage: str, false_positive: bool, missing_required: list[str]) -> list[str]:
    suggestions: list[str] = []
    if false_positive:
        suggestions.append("Tighten flag_regex or verifier checks against expected_flags before counting this result.")
    if missing_required:
        suggestions.append("Install or map required tools for this benchmark: " + ", ".join(missing_required))
    if stuck_stage == "planning":
        suggestions.append("Review benchmark metadata and category routing; planner did not reach execution.")
    elif stuck_stage == "verifying":
        suggestions.append("Inspect stdout/stderr artifacts and add category-specific extraction logic.")
    elif stuck_stage == "budget":
        suggestions.append("Increase max_time/max_steps or shorten expensive pipeline commands.")
    elif stuck_stage == "exception":
        suggestions.append("Open the run error and validate challenge.yaml fields/files.")
    else:
        suggestions.append("Review trace tail and specialist hypothesis before rerunning with a narrower route.")
    if item.tags:
        suggestions.append("Search memory for benchmark tags: " + ", ".join(item.tags[:5]))
    return suggestions


def _evidence_steps(result: SolveResult, limit: int = 12) -> list[str]:
    observations = result.metadata.get("observations", []) if isinstance(result.metadata, dict) else []
    if not isinstance(observations, list):
        return []
    lines: list[str] = []
    tail = observations[-limit:]
    start = max(1, len(observations) - len(tail) + 1)
    for index, item in enumerate(tail, start=start):
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "unknown")
        summary = summarize_text(str(item.get("summary") or ""), 240) or ""
        metadata = item.get("metadata", {})
        bits = [f"step={index}", f"source={source}"]
        if summary:
            bits.append(summary.replace("\n", " ")[:260])
        if isinstance(metadata, dict) and metadata:
            bits.append(f"metadata={json.dumps(metadata, ensure_ascii=False, sort_keys=True)[:260]}")
        lines.append(" | ".join(bits))
    return lines


def _trace_summary(events: list[TraceEvent], limit: int = 8) -> list[str]:
    tail = events[-limit:]
    lines = []
    for event in tail:
        command = _format_command(event.command)
        message = event.stdout or event.stderr or ""
        detail = summarize_text(message, 240) or ""
        bits = [event.agent, event.action]
        if command:
            bits.append(command)
        if event.exit_code is not None:
            bits.append(f"exit={event.exit_code}")
        if detail:
            bits.append(detail.replace("\n", " ")[:260])
        lines.append(" | ".join(bits))
    return lines


def _commands_for_run(run_dir: str | None) -> list[str]:
    if not run_dir:
        return []
    try:
        events = TraceStore(Path(run_dir) / "trace.jsonl").read_events()
    except Exception:
        return []
    commands: list[str] = []
    for event in events:
        if event.agent == "executor" and event.action == "run-command":
            command = _format_command(event.command)
            if command and command not in commands:
                commands.append(command)
    return commands


def _format_command(command: list[str] | None) -> str:
    if not command:
        return ""
    if len(command) >= 3 and command[0] in {"bash", "docker"} and command[-2] == "-lc":
        return command[-1]
    return " ".join(command)


def _repeat_metrics(results: list[EvalChallengeResult]) -> dict[str, Any]:
    return {
        "solved_count": sum(1 for result in results if result.solved),
        "steps_used": sum(result.steps_used for result in results),
        "time_used": round(sum(result.time_used for result in results), 6),
        "avg_time": round(mean([result.time_used for result in results] or [0.0]), 6),
    }
