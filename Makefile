PYTHON ?= $(shell if [ -x .venv/bin/python ]; then echo .venv/bin/python; else echo python3; fi)

.PHONY: doctor test install install-dev eval-local lint-basic clean-generated docker-build docker-doctor docker-build-generic docker-build-pwn docker-build-web docker-build-crypto docker-build-rev docker-build-forensics

install:
	$(PYTHON) -m pip install -e .

install-dev:
	$(PYTHON) -m pip install -e ".[dev]"

doctor:
	PYTHONPATH=src $(PYTHON) -m ctf_agent.cli doctor

test:
	$(PYTHON) -m pytest

eval-local:
	CTF_AGENT_MEMORY_ENABLED=false PYTHONPATH=src $(PYTHON) -m ctf_agent.cli eval ./evals/datasets/local --executor local --mode specialist --max-steps 30 --output-dir "$(HOME)/ctf-workspace/evals/local-latest"

lint-basic:
	PYTHONDONTWRITEBYTECODE=1 $(PYTHON) scripts/lint_basic.py

clean-generated:
	find . -type d \( -name "__pycache__" -o -name ".pytest_cache" -o -name ".mypy_cache" -o -name ".ruff_cache" -o -name "*.egg-info" -o -name "build" -o -name "dist" \) -prune -exec rm -rf {} +
	find . -type f \( -name "*.pyc" -o -name "*.pyo" -o -name ".coverage" \) -delete

docker-build:
	PYTHONPATH=src $(PYTHON) -m ctf_agent.cli docker build --profile all

docker-doctor:
	PYTHONPATH=src $(PYTHON) -m ctf_agent.cli docker doctor --run-tools

docker-build-generic:
	PYTHONPATH=src $(PYTHON) -m ctf_agent.cli docker build --profile generic

docker-build-pwn:
	PYTHONPATH=src $(PYTHON) -m ctf_agent.cli docker build --profile pwn

docker-build-web:
	PYTHONPATH=src $(PYTHON) -m ctf_agent.cli docker build --profile web

docker-build-crypto:
	PYTHONPATH=src $(PYTHON) -m ctf_agent.cli docker build --profile crypto

docker-build-rev:
	PYTHONPATH=src $(PYTHON) -m ctf_agent.cli docker build --profile rev

docker-build-forensics:
	PYTHONPATH=src $(PYTHON) -m ctf_agent.cli docker build --profile forensics
