.PHONY: help install install-deps lint lint-python lint-tui lint-bridge test test-python test-tui build build-tui build-bridge check-commits check-pr-title ci clean

PYTHON ?= python3
PYTHON_LINT_TARGETS ?= scripts/check_commit_file.py scripts/check_commit_messages.py scripts/check_pr_title.py scripts/commit_lint.py tests/test_commit_lint.py
COMMIT_RANGE ?=

help:
	@echo "Targets:"
	@echo "  install        Install Python deps, Node deps, and git hooks"
	@echo "  install-deps   Install Python deps only (CI uses this)"
	@echo "  lint           Run Python, TUI, and bridge lint gates"
	@echo "  lint-python    Ruff-check the current lint target set"
	@echo "  lint-tui       TypeScript lint + RPC drift check"
	@echo "  lint-bridge    Bridge package build check"
	@echo "  test           Run focused Python checks and TUI tests"
	@echo "  check-commits  Validate Conventional Commit subjects"
	@echo "  check-pr-title Validate the PR title in PR_TITLE"
	@echo "  ci             Run the local CI gate"
	@echo "  clean          Remove generated caches and build output"

install-deps:
	uv sync --frozen --extra dev --dev

install: install-deps
	uv run pre-commit install
	uv run pre-commit install --hook-type commit-msg
	npm ci --prefix ui-tui
	npm ci --prefix bridge

lint: lint-python lint-tui lint-bridge

lint-python:
	uv run --extra dev ruff check $(PYTHON_LINT_TARGETS)
	uv run --extra dev ruff format --check $(PYTHON_LINT_TARGETS)

lint-tui:
	npm run lint --prefix ui-tui
	npm run lint:rpc --prefix ui-tui
	npm run type-check --prefix ui-tui

lint-bridge:
	npm run build --prefix bridge

test: test-python test-tui

test-python:
	uv run --extra dev pytest tests/test_commit_lint.py -q

test-tui:
	npm test --prefix ui-tui

build: build-tui build-bridge

build-tui:
	npm run build --prefix ui-tui

build-bridge:
	npm run build --prefix bridge

check-commits:
	uv run --extra dev python scripts/check_commit_messages.py $(COMMIT_RANGE)

check-pr-title:
	uv run --extra dev python scripts/check_pr_title.py

ci: lint test build

clean:
	rm -rf .pytest_cache .ruff_cache .uv-cache .mypy_cache htmlcov dist build
	rm -rf ui-tui/dist ui-tui/coverage ui-tui/.vitest-cache ui-tui/packages/hermes-ink/dist
	rm -rf bridge/dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
