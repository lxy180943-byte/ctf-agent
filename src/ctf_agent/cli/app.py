from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from collections.abc import Sequence

from ctf_agent import __version__
from ctf_agent.core.config import get_nested, load_config
from ctf_agent.core.doctor import build_llm_report, build_report, print_llm_text, print_text
from ctf_agent.core.maturity import build_maturity_report, render_maturity_report, write_maturity_report
from ctf_agent.core.logging import setup_logging
from ctf_agent.core.orchestrator import Orchestrator, SolveResult
from ctf_agent.core.models import FlagCandidate, utc_now
from ctf_agent.core.state import ChallengeRunState
from ctf_agent.core.trace import TraceEvent
from ctf_agent.core.reporter import Reporter
from ctf_agent.core.reviewer import RunReviewer
from ctf_agent.core.submitter import Submitter
from ctf_agent.core.workspace import WorkspaceManager
from ctf_agent.evals import BenchmarkRunner, adapter_for_path
from ctf_agent.memory import KnowledgeItem, MemoryStore
from ctf_agent.platforms.ctfd import CTFdConfigError, adapter_from_config
from ctf_agent.platforms.local import LocalPlatformAdapter
from ctf_agent.sandbox import (
    BUILDABLE_PROFILES,
    DockerExecutor,
    LocalExecutor,
    build_profile,
    docker_available,
    docker_profiles_doctor,
    image_for_category,
    profile_names,
)
from ctf_agent.sandbox.network_policy import docker_network_policy, local_executor_network_note
from ctf_agent.tools import build_tools_doctor, default_registry, print_tools_doctor
from ctf_agent.ui import serve as serve_ui


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ctf-agent",
        description="Local-first CTF solving agent for authorized challenges and benchmarks.",
    )
    parser.add_argument("--config", help="Path to a YAML config file. Defaults to configs/default.yaml.")

    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("version", help="Print the ctf-agent version.")

    doctor = subparsers.add_parser("doctor", help="Check WSL, tools, directories, Docker, LLM, and executors.")
    doctor.add_argument("section", nargs="?", choices=["environment", "executors", "llm"], default="environment")
    doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    doctor.add_argument("--skip-docker-run", action="store_true", help="Do not run docker hello-world.")
    doctor.add_argument("--no-create-dirs", action="store_true", help="Do not create required directories.")

    list_parser = subparsers.add_parser("list", help="List challenges from a local directory.")
    list_parser.add_argument("path", help="Directory containing challenge.yaml or challenge subdirectories.")
    list_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    inspect_parser = subparsers.add_parser("inspect", help="Inspect one local challenge.")
    inspect_parser.add_argument("path", help="Challenge directory or challenge.yaml path.")
    inspect_parser.add_argument("--json", action="store_true", default=True, help="Print machine-readable JSON.")

    exec_parser = subparsers.add_parser("exec", help="Run a command against a local challenge workspace.")
    exec_parser.add_argument("challenge_dir", help="Challenge directory or challenge.yaml path.")
    exec_parser.add_argument("--executor", choices=["local", "docker"], help="Executor backend. Defaults to sandbox.engine.")
    exec_parser.add_argument("--timeout", type=int, help="Command timeout in seconds.")
    exec_parser.add_argument("--env", action="append", default=[], help="Environment override as KEY=VALUE. May be repeated.")
    exec_parser.add_argument("exec_command", nargs=argparse.REMAINDER, help="Command after --, for example: -- 'file ./binary'")

    tools_parser = subparsers.add_parser("tools", help="List and check configured CTF tools.")
    tools_subparsers = tools_parser.add_subparsers(dest="tools_command")

    tools_list = tools_subparsers.add_parser("list", help="List built-in tool specs.")
    tools_list.add_argument("--category", help="Filter by category.")
    tools_list.add_argument("--query", help="Search tool name, category, or description.")
    tools_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    tools_doctor = tools_subparsers.add_parser("doctor", help="Check whether tool binaries are installed.")
    tools_doctor.add_argument("--category", help="Filter by category.")
    tools_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    solve_parser = subparsers.add_parser("solve", help="Solve a local challenge with graph brain by default.")
    solve_parser.add_argument("challenge_dir", help="Challenge directory or challenge.yaml path.")
    solve_parser.add_argument("--max-steps", type=int, default=10, help="Maximum planned command steps to execute.")
    solve_parser.add_argument("--timeout", type=int, help="Per-command timeout in seconds.")
    solve_parser.add_argument("--executor", choices=["local", "docker"], help="Executor backend. Defaults to sandbox.engine.")
    solve_parser.add_argument("--mode", choices=["single", "specialist", "critic-after-failures"], help="Orchestration mode.")
    solve_parser.add_argument("--brain", choices=["graph", "fallback", "llm", "hybrid"], default="graph", help="Brain mode. Defaults to graph production mode. Use fallback for offline deterministic compatibility; llm/hybrid are deprecated legacy modes.")
    solve_parser.add_argument("--critic-after-failures", type=int, help="Failure count before CriticAgent runs.")
    solve_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    resume_parser = subparsers.add_parser("resume", help="Resume a saved run directory.")
    resume_parser.add_argument("run_dir", help="Run directory, for example ~/ctf-workspace/runs/challenge1.")
    resume_parser.add_argument("--max-steps", type=int, default=10, help="Maximum planned command steps to execute.")
    resume_parser.add_argument("--timeout", type=int, help="Per-command timeout in seconds.")
    resume_parser.add_argument("--executor", choices=["local", "docker"], help="Executor backend. Defaults to sandbox.engine.")
    resume_parser.add_argument("--mode", choices=["single", "specialist", "critic-after-failures"], help="Orchestration mode.")
    resume_parser.add_argument("--brain", choices=["graph", "fallback", "llm", "hybrid"], default="graph", help="Brain mode. Defaults to graph production mode. Use fallback for offline deterministic compatibility; llm/hybrid are deprecated legacy modes.")
    resume_parser.add_argument("--critic-after-failures", type=int, help="Failure count before CriticAgent runs.")
    resume_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    report_parser = subparsers.add_parser("report", help="Generate writeup.md for a saved run directory.")
    report_parser.add_argument("run_dir", help="Run directory, for example ~/ctf-workspace/runs/challenge1.")
    report_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    review_parser = subparsers.add_parser("review-run", help="Generate run_review.md with success/failure retrospective notes.")
    review_parser.add_argument("run_dir", help="Run directory, for example ~/ctf-workspace/runs/challenge1.")
    review_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    submit_parser = subparsers.add_parser("submit", help="Submit or dry-run submit the best verified flag for a saved run.")
    submit_parser.add_argument("run_dir", help="Run directory, for example ~/ctf-workspace/runs/challenge1.")
    submit_parser.add_argument("--flag", help="Override the flag value. Defaults to the best verified candidate.")
    submit_parser.add_argument("--dry-run", action="store_true", help="Do not submit externally. This is the default.")
    submit_parser.add_argument("--submit", action="store_true", help="Really submit when the platform supports it.")
    submit_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    memory_parser = subparsers.add_parser("memory", help="Search, add, and learn local CTF knowledge.")
    memory_subparsers = memory_parser.add_subparsers(dest="memory_command")

    memory_search = memory_subparsers.add_parser("search", help="Search stored knowledge.")
    memory_search.add_argument("query", nargs="?", default="", help="Search text.")
    memory_search.add_argument("--category", help="Filter by category.")
    memory_search.add_argument("--limit", type=int, default=10, help="Maximum results.")
    memory_search.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    memory_add = memory_subparsers.add_parser("add", help="Add one traceable knowledge item.")
    memory_add.add_argument("--category", required=True)
    memory_add.add_argument("--pattern", required=True)
    memory_add.add_argument("--symptom", required=True)
    memory_add.add_argument("--solution", required=True)
    memory_add.add_argument("--command", dest="memory_commands", action="append", default=[], help="Useful command. May be repeated.")
    memory_add.add_argument("--source-run", required=True, help="Source run directory for traceability.")
    memory_add.add_argument("--confidence", type=float, default=0.5)
    memory_add.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    memory_learn = memory_subparsers.add_parser("learn", help="Extract traceable knowledge from a saved run.")
    memory_learn.add_argument("run_dir", help="Run directory, for example ~/ctf-workspace/runs/challenge1.")
    memory_learn.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    memory_promote = memory_subparsers.add_parser("promote", help="Increase confidence and success_count for a knowledge item.")
    memory_promote.add_argument("id")
    memory_promote.add_argument("--amount", type=float, default=0.08)
    memory_promote.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    memory_demote = memory_subparsers.add_parser("demote", help="Decrease confidence and increase failure_count for a knowledge item.")
    memory_demote.add_argument("id")
    memory_demote.add_argument("--amount", type=float, default=0.08)
    memory_demote.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    memory_prune = memory_subparsers.add_parser("prune", help="Delete low-confidence knowledge items.")
    memory_prune.add_argument("--min-confidence", type=float, default=0.2)
    memory_prune.add_argument("--source-type", help="Only prune this source_type.")
    memory_prune.add_argument("--include-successful", action="store_true", help="Also prune items with success_count > 0.")
    memory_prune.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    eval_parser = subparsers.add_parser("eval", help="Run a benchmark dataset and write eval_report.md/eval_results.jsonl.")
    eval_parser.add_argument("dataset", help="Dataset directory, for example ./evals/datasets/local.")
    eval_parser.add_argument("--max-steps", type=int, default=20, help="Maximum planned command steps per challenge.")
    eval_parser.add_argument("--timeout", type=int, help="Per-command timeout in seconds.")
    eval_parser.add_argument("--executor", choices=["local", "docker"], help="Executor backend. Defaults to sandbox.engine.")
    eval_parser.add_argument("--mode", choices=["single", "specialist", "critic-after-failures"], help="Orchestration mode.")
    eval_parser.add_argument("--brain", choices=["graph", "fallback", "llm", "hybrid"], default="graph", help="Brain mode. Defaults to graph production mode. Use fallback for offline deterministic compatibility; llm/hybrid are deprecated legacy modes.")
    eval_parser.add_argument("--output-dir", help="Directory for eval_report.md and eval_results.jsonl.")
    eval_parser.add_argument("--maturity-output", help="Path for docs/maturity_report.md. Defaults to the repo docs path.")
    eval_parser.add_argument("--only-category", help="Run only challenges in this category.")
    eval_parser.add_argument("--only-tag", help="Run only challenges with this tag.")
    eval_parser.add_argument("--fail-fast", action="store_true", help="Stop after the first unsolved or failed challenge.")
    eval_parser.add_argument("--repeat", type=int, default=1, help="Run the same filtered dataset N times.")
    eval_parser.add_argument("--regression", action="store_true", help="Compare each repeat against repeat 1. Enabled automatically when --repeat > 1.")
    eval_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON summary.")

    maturity_parser = subparsers.add_parser("maturity-report", help="Generate the current maturity report.")
    maturity_parser.add_argument("--eval-summary", help="Path to a specific eval_summary.json file.")
    maturity_parser.add_argument("--output", help="Output markdown path. Defaults to docs/maturity_report.md.")
    maturity_parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    ui_parser = subparsers.add_parser("ui", help="Start the local web workbench.")
    ui_parser.add_argument("--host", default="127.0.0.1", help="Bind host. Defaults to 127.0.0.1.")
    ui_parser.add_argument("--port", type=int, default=8008, help="Bind port. Defaults to 8008.")
    ui_parser.add_argument("--challenges", default=None, help="Challenge root shown in the workbench. Defaults to ui.challenge_root or examples.")

    ctfd_parser = subparsers.add_parser("ctfd", help="Work with an authorized CTFd competition profile.")
    ctfd_subparsers = ctfd_parser.add_subparsers(dest="ctfd_command")

    ctfd_list = ctfd_subparsers.add_parser("list", help="List CTFd challenges for a configured profile.")
    ctfd_list.add_argument("--profile", default=None, help="CTFd profile name from platform.ctfd.profiles.")
    ctfd_list.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    ctfd_pull = ctfd_subparsers.add_parser("pull", help="Pull one CTFd challenge and attachments into the local workspace.")
    ctfd_pull.add_argument("id", help="CTFd challenge id.")
    ctfd_pull.add_argument("--profile", default=None, help="CTFd profile name from platform.ctfd.profiles.")
    ctfd_pull.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    ctfd_solve = ctfd_subparsers.add_parser("solve", help="Pull and solve one CTFd challenge.")
    ctfd_solve.add_argument("id", help="CTFd challenge id.")
    ctfd_solve.add_argument("--profile", default=None, help="CTFd profile name from platform.ctfd.profiles.")
    ctfd_solve.add_argument("--max-steps", type=int, default=10, help="Maximum planned command steps to execute.")
    ctfd_solve.add_argument("--timeout", type=int, help="Per-command timeout in seconds.")
    ctfd_solve.add_argument("--executor", choices=["local", "docker"], help="Executor backend. Defaults to sandbox.engine.")
    ctfd_solve.add_argument("--mode", choices=["single", "specialist", "critic-after-failures"], help="Orchestration mode.")
    ctfd_solve.add_argument("--brain", choices=["graph", "fallback", "llm", "hybrid"], default="graph", help="Brain mode. Defaults to graph production mode. Use fallback for offline deterministic compatibility; llm/hybrid are deprecated legacy modes.")
    ctfd_solve.add_argument("--critic-after-failures", type=int, help="Failure count before CriticAgent runs.")
    ctfd_solve.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    ctfd_submit = ctfd_subparsers.add_parser("submit", help="Submit or dry-run submit a saved CTFd run.")
    ctfd_submit.add_argument("run_dir", help="Run directory, for example ~/ctf-workspace/runs/123.")
    ctfd_submit.add_argument("--profile", default=None, help="CTFd profile name from platform.ctfd.profiles.")
    ctfd_submit.add_argument("--flag", help="Override the flag value. Defaults to best verified candidate.")
    ctfd_submit.add_argument("--submit", action="store_true", help="Really submit when profile submit_enabled is true.")
    ctfd_submit.add_argument("--confirm", default="", help="Required exact confirmation string: SUBMIT <profile> <challenge_id>.")
    ctfd_submit.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    docker_parser = subparsers.add_parser("docker", help="Build and check ctf-agent Docker sandbox images.")
    docker_subparsers = docker_parser.add_subparsers(dest="docker_command")

    docker_build = docker_subparsers.add_parser("build", help="Build one or all local Docker profiles.")
    docker_build.add_argument("--profile", default="all", choices=["all", *BUILDABLE_PROFILES], help="Profile to build. Defaults to all.")
    docker_build.add_argument("--no-cache", action="store_true", help="Pass --no-cache to docker build.")
    docker_build.add_argument("--pull", action="store_true", help="Pass --pull to docker build.")
    docker_build.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    docker_doctor = docker_subparsers.add_parser("doctor", help="Check local Docker profile images.")
    docker_doctor.add_argument("--run-tools", action="store_true", help="Run core tool checks inside each available image.")
    docker_doctor.add_argument("--include-optional", action="store_true", help="Include optional profiles such as sage.")
    docker_doctor.add_argument("--json", action="store_true", help="Print machine-readable JSON.")

    return parser


def warn_deprecated_brain(args: argparse.Namespace) -> None:
    brain = getattr(args, "brain", None)
    if brain in {"llm", "hybrid"}:
        print(f"warning: --brain {brain} is deprecated legacy compatibility; graph is the default production mode, and --brain fallback is the offline deterministic mode.", file=sys.stderr)


def print_graph_failure_if_present(result: SolveResult) -> None:
    reason = result.metadata.get("graph_failure_reason") or result.metadata.get("failure_reason")
    if result.metadata.get("brain") == "graph" and reason:
        print(str(reason), file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "version":
        print(f"ctf-agent {__version__}")
        return 0

    config = load_config(args.config)
    setup_logging(config)

    if args.command == "doctor":
        if args.section == "executors":
            report = build_executor_doctor(config)
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print_executor_doctor(report)
            return 0 if report["ok"] else 1
        if args.section == "llm":
            report = build_llm_report(config)
            if args.json:
                print(json.dumps(report, indent=2, sort_keys=True))
            else:
                print_llm_text(report)
            return 0 if report["ok"] else 1
        report = build_report(create_dirs=not args.no_create_dirs, docker_run=not args.skip_docker_run, config=config)
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print_text(report)
        return 0 if report["ok"] else 1

    if args.command == "list":
        adapter = LocalPlatformAdapter(args.path)
        challenges = adapter.list_challenges()
        if args.json:
            print(json.dumps([challenge.to_dict() for challenge in challenges], indent=2, ensure_ascii=False, sort_keys=True))
        else:
            for challenge in challenges:
                print(f"{challenge.id}\t{challenge.category}\t{challenge.title}")
        return 0

    if args.command == "inspect":
        adapter = LocalPlatformAdapter(args.path)
        challenge = adapter.get_challenge(args.path)
        print(json.dumps(challenge.to_dict(), indent=2, ensure_ascii=False, sort_keys=True))
        return 0

    if args.command == "exec":
        return run_exec_command(args, config)

    if args.command == "tools":
        return run_tools_command(args)

    if args.command == "solve":
        warn_deprecated_brain(args)
        adapter = LocalPlatformAdapter(args.challenge_dir)
        challenge = adapter.get_challenge(args.challenge_dir)
        orchestrator = Orchestrator(
            config,
            executor_name=args.executor,
            max_steps=args.max_steps,
            timeout=args.timeout,
            brain=args.brain,
            mode=args.mode,
            critic_after_failures=args.critic_after_failures,
        )
        try:
            result = orchestrator.solve(challenge, adapter=adapter)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 69
        print_solve_result(result, as_json=args.json)
        print_graph_failure_if_present(result)
        return 0 if result.solved else 1

    if args.command == "resume":
        warn_deprecated_brain(args)
        orchestrator = Orchestrator(
            config,
            executor_name=args.executor,
            max_steps=args.max_steps,
            timeout=args.timeout,
            brain=args.brain,
            mode=args.mode,
            critic_after_failures=args.critic_after_failures,
        )
        try:
            result = orchestrator.resume_from_run_dir(args.run_dir)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 69
        print_solve_result(result, as_json=args.json)
        print_graph_failure_if_present(result)
        return 0 if result.solved else 1

    if args.command == "report":
        try:
            path = Reporter(get_nested(config, ("workspace_dir",))).generate(args.run_dir)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps({"writeup": str(path)}, indent=2, sort_keys=True))
        else:
            print(f"writeup: {path}")
        return 0

    if args.command == "review-run":
        try:
            review = RunReviewer(get_nested(config, ("workspace_dir",))).review(args.run_dir)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(review.to_dict(), indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print(f"run_review: {review.run_dir / 'run_review.md'}")
            print(f"state: {review.state}")
            print(f"effective_commands: {len(review.effective_commands)}")
            print(f"ineffective_commands: {len(review.ineffective_commands)}")
        return 0

    if args.command == "submit":
        actual_submit = bool(args.submit) and not bool(args.dry_run)
        try:
            result = Submitter(config).submit_run(args.run_dir, flag=args.flag, submit=actual_submit)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 1
        if args.json:
            print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print(f"challenge: {result.challenge_id}")
            print(f"platform: {result.platform}")
            print(f"dry_run: {result.dry_run}")
            print(f"submitted: {result.submitted}")
            print(f"accepted: {result.accepted}")
            print(f"flag: {result.flag}")
            print(f"message: {result.message}")
        return 0

    if args.command == "memory":
        return run_memory_command(args, config)

    if args.command == "eval":
        warn_deprecated_brain(args)
        return run_eval_command(args, config)

    if args.command == "maturity-report":
        return run_maturity_report_command(args, config)

    if args.command == "ui":
        serve_ui(config, host=args.host, port=args.port, challenge_root=args.challenges)
        return 0

    if args.command == "ctfd":
        return run_ctfd_command(args, config)

    if args.command == "docker":
        return run_docker_command(args)

    parser.error(f"unknown command: {args.command}")
    return 2



def run_ctfd_command(args: argparse.Namespace, config: dict) -> int:
    if args.ctfd_command is None:
        print("usage: ctf-agent ctfd {list,pull,solve,submit} ...")
        return 0
    try:
        adapter = adapter_from_config(config, args.profile)
    except CTFdConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if args.ctfd_command == "list":
        try:
            challenges = adapter.list_challenges()
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 69
        if args.json:
            print(json.dumps([challenge.to_dict() for challenge in challenges], indent=2, ensure_ascii=False, sort_keys=True))
        else:
            for challenge in challenges:
                meta = challenge.metadata
                score = meta.get("dynamic_scoring", {}).get("value") if isinstance(meta.get("dynamic_scoring"), dict) else ""
                locked = "locked" if meta.get("locked") else "open"
                hidden = "hidden" if meta.get("hidden") else "visible"
                solved = meta.get("dynamic_scoring", {}).get("solved_by_me") if isinstance(meta.get("dynamic_scoring"), dict) else ""
                print(f"{challenge.id}\t{challenge.category}\t{score}\t{locked}/{hidden}\tsolved={solved}\t{challenge.title}")
        return 0

    if args.ctfd_command == "pull":
        try:
            challenge = adapter.get_challenge(args.id)
            manager = WorkspaceManager(get_nested(config, ("workspace_dir",)) or "~/ctf-workspace")
            state = manager.init_state(challenge)
            layout = manager.layout_for(challenge.id)
            artifacts = adapter.download_files(challenge, layout.work_dir)
            state.metadata["downloaded_artifacts"] = [artifact.to_dict() for artifact in artifacts]
            manager.save_state(state)
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 69
        data = {"challenge": challenge.to_dict(), "run_dir": str(layout.challenge_dir), "artifacts": [artifact.to_dict() for artifact in artifacts]}
        if args.json:
            print(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print(f"challenge: {challenge.id}")
            print(f"profile: {adapter.profile.name}")
            print(f"run_dir: {layout.challenge_dir}")
            for artifact in artifacts:
                print(f"artifact: {artifact.path} sha256={artifact.metadata.get('sha256')} size={artifact.metadata.get('size')}")
        return 0

    if args.ctfd_command == "solve":
        warn_deprecated_brain(args)
        try:
            challenge = adapter.get_challenge(args.id)
            result = Orchestrator(
                config,
                executor_name=args.executor,
                max_steps=args.max_steps,
                timeout=args.timeout,
                brain=args.brain,
                mode=args.mode,
                critic_after_failures=args.critic_after_failures,
            ).solve(challenge, adapter=adapter)
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 69
        except Exception as exc:
            print(str(exc), file=sys.stderr)
            return 69
        print_solve_result(result, as_json=args.json)
        print_graph_failure_if_present(result)
        return 0 if result.solved else 1

    if args.ctfd_command == "submit":
        return run_ctfd_submit_command(args, config, adapter)
    raise SystemExit(f"Unknown ctfd command: {args.ctfd_command}")


def run_ctfd_submit_command(args: argparse.Namespace, config: dict, adapter) -> int:
    try:
        run_path = Path(args.run_dir).expanduser().resolve()
        workspace_root = run_path.parent.parent if run_path.parent.name == "runs" else Path(get_nested(config, ("workspace_dir",)) or "~/ctf-workspace").expanduser()
        manager = WorkspaceManager(workspace_root)
        state = manager.load_state(run_path.name)
        flag = args.flag or select_submit_flag(state)
        result = adapter.submit_flag(state.challenge, flag, submit=bool(args.submit), confirm=args.confirm)
        update_submit_state(state, flag, result)
        manager.save_state(state)
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1
    data = {
        "challenge_id": result.challenge_id,
        "profile": adapter.profile.name,
        "flag": result.flag,
        "dry_run": not bool(args.submit),
        "submitted": result.submitted,
        "accepted": result.accepted,
        "message": result.message,
        "metadata": result.metadata,
    }
    if args.json:
        print(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(f"challenge: {result.challenge_id}")
        print(f"profile: {adapter.profile.name}")
        print(f"dry_run: {not bool(args.submit)}")
        print(f"submitted: {result.submitted}")
        print(f"accepted: {result.accepted}")
        print(f"flag: {result.flag}")
        print(f"message: {result.message}")
        if not result.submitted:
            print(f"confirmation_required: {adapter.confirmation_string(state.challenge)}")
    return 0 if result.submitted or not args.submit else 1


def select_submit_flag(state: ChallengeRunState) -> str:
    verified = [candidate for candidate in state.flag_candidates if candidate.verified]
    if not verified:
        raise ValueError("No verified flag candidate found; pass --flag explicitly to submit a chosen value")
    return sorted(verified, key=lambda item: (-item.confidence, item.value))[0].value


def update_submit_state(state: ChallengeRunState, flag: str, result) -> None:
    matched = False
    for candidate in state.flag_candidates:
        if candidate.value == flag:
            candidate.submitted = result.submitted
            candidate.verified = candidate.verified or bool(result.accepted)
            matched = True
    if not matched:
        state.add_flag_candidate(FlagCandidate(value=flag, source="ctfd-submit", confidence=0.5, verified=bool(result.accepted), submitted=result.submitted))
    state.metadata["last_submit"] = {
        "platform": "ctfd",
        "flag": flag,
        "submitted": result.submitted,
        "accepted": result.accepted,
        "message": result.message,
        "updated_at": utc_now(),
    }


def run_docker_command(args: argparse.Namespace) -> int:
    if args.docker_command is None:
        print("usage: ctf-agent docker {build,doctor} ...")
        return 0
    if args.docker_command == "build":
        profiles = list(BUILDABLE_PROFILES) if args.profile == "all" else [args.profile]
        results = [build_profile(profile, no_cache=args.no_cache, pull=args.pull) for profile in profiles]
        if args.json:
            print(json.dumps(results, indent=2, ensure_ascii=False, sort_keys=True))
        else:
            for result in results:
                status = "ok" if result["ok"] else "failed"
                print(f"{result['profile']}: {status} image={result['image']} dockerfile={result['dockerfile']}")
                if not result["ok"]:
                    print(result["output"])
        return 0 if all(result["ok"] for result in results) else 1
    if args.docker_command == "doctor":
        report = docker_profiles_doctor(run_tools=args.run_tools, include_optional=args.include_optional)
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print_docker_profiles_doctor(report)
        return 0 if report["ok"] else 1
    raise SystemExit(f"Unknown docker command: {args.docker_command}")


def print_docker_profiles_doctor(report: dict[str, object]) -> None:
    print("CTF Agent Docker Profiles Doctor")
    print(f"OK: {report['ok']}")
    print(f"Docker available: {report['docker_available']}")
    print(f"Tool checks: {report['run_tools']}")
    profiles = report["profiles"]
    assert isinstance(profiles, list)
    for profile in profiles:
        assert isinstance(profile, dict)
        print(f"- {profile['profile']}: image={profile['image']} exists={profile['exists']} ok={profile['ok']}")
        dockerfile = profile.get("dockerfile")
        if dockerfile:
            print(f"  dockerfile: {dockerfile}")
        build_command = profile.get("build_command")
        if not profile.get("exists") and build_command:
            print(f"  build: {build_command}")
        for note in profile.get("notes", []):
            print(f"  note: {note}")
        checks = profile.get("checks", [])
        assert isinstance(checks, list)
        for check in checks:
            assert isinstance(check, dict)
            command = " ".join(check.get("command", []))
            print(f"  check: {command} ok={check['ok']} exit={check['exit_code']}")


def run_eval_command(args: argparse.Namespace, config: dict) -> int:
    adapter = adapter_for_path(args.dataset)
    summary = BenchmarkRunner(
        config,
        max_steps=args.max_steps,
        executor_name=args.executor,
        timeout=args.timeout,
        brain=args.brain,
        mode=args.mode,
        output_dir=args.output_dir,
        only_category=args.only_category,
        only_tag=args.only_tag,
        fail_fast=args.fail_fast,
        repeat=args.repeat,
        regression=args.regression,
    ).run(adapter)
    maturity_path = write_maturity_report(config, eval_summary=summary, output_path=args.maturity_output)
    metrics = summary.metrics()
    if args.brain == "graph" and metrics["solved_count"] != metrics["challenge_count"]:
        for result in summary.results:
            reason = result.error or result.metadata.get("graph_failure_reason") or result.metadata.get("solve", {}).get("graph_failure_reason")
            if reason:
                print(f"graph eval failure for {result.challenge_id}: {reason}", file=sys.stderr)
                break
    if args.json:
        print(json.dumps(summary.to_dict(), indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(f"dataset: {summary.dataset}")
        print(f"output_dir: {summary.output_dir}")
        print(f"solved_count: {metrics['solved_count']}/{metrics['challenge_count']}")
        print(f"steps_used: {metrics['steps_used']}")
        print(f"time_used: {metrics['time_used']}")
        print(f"command_count: {metrics['command_count']}")
        print(f"verifier_false_positive: {metrics['verifier_false_positive']}")
        print(f"resume_success: {metrics['resume_success']}")
        print(f"eval_report: {summary.output_dir / 'eval_report.md'}")
        print(f"eval_results: {summary.output_dir / 'eval_results.jsonl'}")
        print(f"capability_gaps: {summary.output_dir / 'capability_gaps.md'}")
        print(f"maturity_report: {maturity_path}")
    return 0 if metrics["solved_count"] == metrics["challenge_count"] and metrics["verifier_false_positive"] == 0 else 1




def run_maturity_report_command(args: argparse.Namespace, config: dict) -> int:
    report = build_maturity_report(config, eval_summary_path=args.eval_summary)
    output = write_maturity_report(config, eval_summary_path=args.eval_summary, output_path=args.output)
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
    else:
        print(render_maturity_report(report), end="")
        print(f"maturity_report: {output}")
    return 0


def run_memory_command(args: argparse.Namespace, config: dict) -> int:
    store = MemoryStore.from_config(config)
    if args.memory_command is None:
        print("usage: ctf-agent memory {search,add,learn} ...")
        return 0
    if args.memory_command == "search":
        items = store.search(args.query, category=args.category, limit=args.limit)
        if args.json:
            print(json.dumps([item.to_dict() for item in items], indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print_memory_items(items)
        return 0
    if args.memory_command == "add":
        item = store.add(
            KnowledgeItem(
                category=args.category,
                pattern=args.pattern,
                symptom=args.symptom,
                solution=args.solution,
                commands=args.memory_commands,
                source_run=args.source_run,
                confidence=args.confidence,
                metadata={"source": "cli-add"},
            )
        )
        if args.json:
            print(json.dumps(item.to_dict(), indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print(f"added: {item.id}")
            print(f"source_run: {item.source_run}")
        return 0
    if args.memory_command == "learn":
        items = store.learn_from_run(args.run_dir)
        if args.json:
            print(json.dumps([item.to_dict() for item in items], indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print(f"learned: {len(items)}")
            for item in items:
                print(f"- {item.id}\t{item.category}\t{item.pattern}\tsource_run={item.source_run}")
        return 0
    if args.memory_command == "promote":
        item = store.promote(args.id, amount=args.amount)
        if args.json:
            print(json.dumps(item.to_dict(), indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print(f"promoted: {item.id} confidence={item.confidence:.2f} success_count={item.success_count}")
        return 0
    if args.memory_command == "demote":
        item = store.demote(args.id, amount=args.amount)
        if args.json:
            print(json.dumps(item.to_dict(), indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print(f"demoted: {item.id} confidence={item.confidence:.2f} failure_count={item.failure_count}")
        return 0
    if args.memory_command == "prune":
        deleted = store.prune(min_confidence=args.min_confidence, source_type=args.source_type, include_successful=args.include_successful)
        if args.json:
            print(json.dumps({"deleted": deleted}, indent=2, sort_keys=True))
        else:
            print(f"pruned: {deleted}")
        return 0
    raise SystemExit(f"Unknown memory command: {args.memory_command}")


def print_memory_items(items: list[KnowledgeItem]) -> None:
    if not items:
        print("No knowledge items found.")
        return
    for item in items:
        commands = " ; ".join(item.commands[:2]) if item.commands else "-"
        print(f"{item.id}\t{item.category}\tconfidence={item.confidence:.2f}\tsuccess={item.success_count}\tfailure={item.failure_count}\tsource_type={item.source_type}\tsource_run={item.source_run}")
        print(f"  pattern: {item.pattern}")
        print(f"  symptom: {item.symptom}")
        print(f"  solution: {item.solution}")
        print(f"  commands: {commands}")


def print_solve_result(result: SolveResult, *, as_json: bool = False) -> None:
    data = {
        "challenge_id": result.challenge_id,
        "state": result.state.value,
        "solved": result.solved,
        "flags": result.flags,
        "run_dir": str(result.run_dir) if result.run_dir else None,
        "steps_executed": result.steps_executed,
        "metadata": result.metadata,
    }
    if as_json:
        print(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))
        return
    print(f"challenge: {result.challenge_id}")
    print(f"state: {result.state.value}")
    print(f"run_dir: {result.run_dir}")
    print(f"steps_executed: {result.steps_executed}")
    if result.flags:
        for flag in result.flags:
            print(f"flag: {flag}")
    else:
        print("flag: <none>")


def run_tools_command(args: argparse.Namespace) -> int:
    registry = default_registry()
    if args.tools_command is None:
        print("usage: ctf-agent tools {list,doctor} ...")
        return 0
    if args.tools_command == "list":
        tools = registry.query(args.query) if args.query else registry.list(category=args.category)
        if args.category and args.query:
            tools = [tool for tool in tools if tool.category == args.category]
        if args.json:
            print(json.dumps([tool.to_dict() for tool in tools], indent=2, ensure_ascii=False, sort_keys=True))
        else:
            for tool in tools:
                risk = tool.risk_level.value if hasattr(tool.risk_level, "value") else str(tool.risk_level)
                bins = ",".join(tool.required_bins) if tool.required_bins else "-"
                print(f"{tool.category}\t{tool.name}\t{risk}\t{bins}\t{tool.description}")
        return 0
    if args.tools_command == "doctor":
        report = build_tools_doctor(registry, category=args.category)
        if args.json:
            print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
        else:
            print_tools_doctor(report)
        return 0
    raise SystemExit(f"Unknown tools command: {args.tools_command}")


def parse_env(values: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise SystemExit(f"--env must be KEY=VALUE, got: {value}")
        key, env_value = value.split("=", 1)
        if not key:
            raise SystemExit("--env key cannot be empty")
        parsed[key] = env_value
    return parsed


def command_from_remainder(values: list[str]) -> str:
    command = list(values)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise SystemExit("exec requires a command after --")
    if len(command) == 1:
        return command[0]
    return shlex.join(command)


def run_exec_command(args: argparse.Namespace, config: dict) -> int:
    normalize_exec_options(args)
    adapter = LocalPlatformAdapter(args.challenge_dir)
    challenge = adapter.get_challenge(args.challenge_dir)
    manager = WorkspaceManager(get_nested(config, ("workspace_dir",)) or "~/ctf-workspace")
    state = manager.init_state(challenge)
    layout = manager.layout_for(challenge.id)
    adapter.download_files(challenge, layout.work_dir)
    trace_store = manager.trace_store_for(challenge.id)

    explicit_executor = args.executor is not None
    executor_name = args.executor or str(get_nested(config, ("sandbox", "engine")) or "docker")
    timeout = int(args.timeout or get_nested(config, ("sandbox", "timeout_seconds")) or 60)
    env = parse_env(args.env)
    command = command_from_remainder(args.exec_command)

    if executor_name == "local":
        trace_network_policy(trace_store, challenge, local_executor_network_note(config, challenge), "local")
        executor = LocalExecutor(manager.workspace_root, trace_store=trace_store, challenge_id=challenge.id)
    elif executor_name == "docker":
        if not docker_available():
            if explicit_executor:
                print("Docker is not available; cannot use explicit docker executor.", file=sys.stderr)
                return 69
            print("Docker is not available; falling back to local executor.", file=sys.stderr)
            executor_name = "local"
            trace_network_policy(trace_store, challenge, local_executor_network_note(config, challenge), "local")
            executor = LocalExecutor(manager.workspace_root, trace_store=trace_store, challenge_id=challenge.id)
        else:
            network_policy = docker_network_policy(config, challenge)
            trace_network_policy(trace_store, challenge, network_policy, "docker")
            executor = DockerExecutor(
                manager.workspace_root,
                image=image_for_category(config, challenge.category),
                network=network_policy.effective_network,
                memory=get_nested(config, ("sandbox", "memory")),
                cpu=get_nested(config, ("sandbox", "cpu")),
                trace_store=trace_store,
                challenge_id=challenge.id,
            )
    else:
        raise SystemExit(f"Unknown executor: {executor_name}")

    result = executor.run(command, cwd=layout.work_dir, timeout=timeout, env=env)
    state.metadata["last_exec"] = {
        "executor": executor_name,
        "command": command,
        "exit_code": result.exit_code,
        "timed_out": result.timed_out,
        "artifacts": [artifact.to_dict() for artifact in result.artifacts],
    }
    manager.save_state(state)
    if result.stdout:
        print(result.stdout, end="" if result.stdout.endswith("\n") else "\n")
    if result.stderr:
        print(result.stderr, file=sys.stderr, end="" if result.stderr.endswith("\n") else "\n")
    return result.exit_code


def trace_network_policy(trace_store, challenge, policy, executor_name: str) -> None:
    trace_store.append(
        TraceEvent(
            challenge_id=challenge.id,
            agent="cli",
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


def normalize_exec_options(args: argparse.Namespace) -> None:
    remaining = list(args.exec_command)
    parsed_env = list(args.env)
    while remaining and remaining[0] != "--":
        option = remaining[0]
        if option == "--executor" and len(remaining) >= 2:
            args.executor = remaining[1]
            remaining = remaining[2:]
        elif option == "--timeout" and len(remaining) >= 2:
            args.timeout = int(remaining[1])
            remaining = remaining[2:]
        elif option == "--env" and len(remaining) >= 2:
            parsed_env.append(remaining[1])
            remaining = remaining[2:]
        else:
            break
    args.env = parsed_env
    args.exec_command = remaining


def build_executor_doctor(config: dict) -> dict[str, object]:
    sandbox = config.get("sandbox", {})
    images = sandbox.get("images", {})
    docker_ok = docker_available()
    return {
        "ok": True,
        "local": {"ok": True, "workspace_required": True},
        "docker": {
            "ok": docker_ok,
            "available": docker_ok,
            "network": sandbox.get("network", "none"),
            "allow_network": bool(sandbox.get("allow_network", False) or sandbox.get("allow_challenge_network", False)),
            "memory": sandbox.get("memory"),
            "cpu": sandbox.get("cpu"),
            "images": images,
        },
    }


def print_executor_doctor(report: dict[str, object]) -> None:
    docker = report["docker"]
    assert isinstance(docker, dict)
    print("CTF Agent Executor Doctor")
    print(f"OK: {report['ok']}")
    print("- local: ok workspace-boundary=enforced")
    print(f"- docker: available={docker['available']} network={docker['network']} allow_network={docker.get('allow_network')} memory={docker['memory']} cpu={docker['cpu']}")
    print("Docker images:")
    images = docker.get("images", {})
    assert isinstance(images, dict)
    for name, image in sorted(images.items()):
        print(f"- {name}: {image}")
