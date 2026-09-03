#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ctf_agent.core.doctor import build_report, print_text


def main() -> int:
    parser = argparse.ArgumentParser(description="Check the WSL CTF agent environment.")
    parser.add_argument("--create-dirs", action="store_true", help="Create required home directories if missing.")
    parser.add_argument("--docker-run", action="store_true", help="Run docker hello-world smoke test.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON.")
    args = parser.parse_args()

    report = build_report(create_dirs=args.create_dirs, docker_run=args.docker_run)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print_text(report)
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
