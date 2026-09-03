#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if ! grep -qiE "microsoft|wsl" /proc/version 2>/dev/null; then
  echo "warning: this does not look like WSL; continuing because the bootstrap is non-destructive."
fi

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 is required. Suggested install:"
  echo "  sudo apt update && sudo apt install python3 python3-venv python3-pip"
  exit 1
fi

if [ ! -d .venv ]; then
  python3 -m venv .venv
fi

. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"

cat <<'EOF'

Python bootstrap complete.

Suggested WSL system tools, install only if you want them:
  sudo apt update
  sudo apt install git make docker.io docker-compose-plugin ripgrep gh python-is-python3

Optional:
  uv  - faster Python package manager
  rg  - fast source/artifact search
  gh  - GitHub CLI for issue/PR workflows

Next checks:
  make doctor
  make test
  make eval-local
EOF
