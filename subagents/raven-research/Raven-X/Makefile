.PHONY: help install install-deps lint lint-python lint-bridge test test-python build-bridge check-commits check-large-files ci clean

PYTHON ?= python3
PYTHON_LINT_TARGETS ?= scripts/check_commit_file.py scripts/check_commit_messages.py scripts/check_large_files.py scripts/commit_lint.py tests/test_commit_lint.py tests/test_large_file_check.py
# This repo has a single squashed commit and no shared origin/main, so HEAD~1
# does not resolve. Default to "working tree vs HEAD" and pass COMMIT_RANGE
# explicitly once there is history to compare.
COMMIT_RANGE ?= HEAD

help:
	@echo "Targets:"
	@echo "  install        Install Python deps and git hooks"
	@echo "  install-deps   Install Python deps only"
	@echo "  lint           Run the Python and bridge lint gates"
	@echo "  lint-python    Ruff-check the current lint target set"
	@echo "  lint-bridge    Bridge package build check"
	@echo "  test           Run the focused Python checks"
	@echo "  test-flow      Run the DR flow / anchor contract tests"
	@echo "  check-commits  Validate Conventional Commit subjects in COMMIT_RANGE"
	@echo "  check-large-files Validate files avoid blocked assets and size bloat"
	@echo "  ci             Run the local gate"
	@echo "  clean          Remove generated caches"

install-deps:
	uv sync --frozen --extra dev --dev

install: install-deps
	uv run pre-commit install
	uv run pre-commit install --hook-type commit-msg
	npm ci --prefix bridge

lint: lint-python lint-bridge

lint-python:
	uv run --extra dev ruff check $(PYTHON_LINT_TARGETS)
	uv run --extra dev ruff format --check $(PYTHON_LINT_TARGETS)

lint-bridge:
	npm run build --prefix bridge

test: test-python

test-python:
	uv run --extra dev pytest tests/test_commit_lint.py tests/test_large_file_check.py -q

# The flow / anchor contract. Must stay green through every refactor (AGENTS.md 5.3).
.PHONY: test-flow
test-flow:
	uv run pytest tests/test_agent_flow_dr.py tests/test_agent_flow_finalize.py \
	    tests/test_agent_flow_fetch_floor.py tests/test_agent_flow_spin_breaker.py \
	    tests/test_agent_loop_context_overflow.py tests/test_config_raven_loader.py \
	    tests/test_agent_flow_verify_elision.py tests/test_agent_loop_invariants.py \
	    tests/test_prompt_detector_disjointness.py tests/test_agent_hook_observers.py -q

build-bridge:
	npm run build --prefix bridge

# commitlint (npx) is gone with the root JS workspace; the Python checker is the gate.
# COMMIT_RANGE defaults to the last commit because this repo has no shared origin/main.
check-commits:
	PYTHONPATH=. uv run --extra dev python scripts/check_commit_messages.py $(COMMIT_RANGE)

check-large-files:
	PYTHONPATH=. uv run --extra dev python scripts/check_large_files.py $(COMMIT_RANGE)

ci: lint test test-flow

clean:
	rm -rf .pytest_cache .ruff_cache .uv-cache .mypy_cache htmlcov dist build
	rm -rf bridge/dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
