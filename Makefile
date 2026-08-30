.PHONY: help install install-deps lint lint-python lint-tui lint-bridge test test-python test-tui build build-tui build-bridge build-ui build-core check-commits check-pr-title check-large-files check-vendored-invariants beta ci clean coverage coverage-summary coverage-diff coverage-ratchet coverage-baseline-check coverage-baseline-candidate

PYTHON ?= python3
PYTHON_VERSION ?= 3.12
PYTHON_LINT_TARGETS ?= raven evolver agents tests scripts
COMMIT_RANGE ?= origin/main..HEAD
COVERAGE_BASE_REF ?= origin/main
# Required coverage percentage for executable lines changed by a PR.
COVERAGE_DIFF_THRESHOLD ?= 90
# Allowed line or branch regression in percentage points to absorb rounding noise.
COVERAGE_RATCHET_TOLERANCE ?= 0.05
COVERAGE_REPORT_ARGS = --cov=raven --cov-branch --cov-report=term-missing:skip-covered --cov-report=xml --cov-report=json --cov-report=html

help:
	@echo "Targets:"
	@echo "  install        Install Python deps, Node deps, and git hooks"
	@echo "  install-deps   Install Python deps only (CI uses this)"
	@echo "  lint           Run Python, TUI, and bridge lint gates"
	@echo "  lint-python    Ruff check + format gate over raven/, evolver/, tests/, scripts/"
	@echo "  lint-tui       TypeScript lint + RPC drift check"
	@echo "  lint-bridge    Bridge package build check"
	@echo "  test           Run focused Python checks and TUI tests"
	@echo "  coverage       Run the default Python suite with line and branch coverage"
	@echo "  coverage-diff  Check changed executable lines against COVERAGE_BASE_REF"
	@echo "  coverage-ratchet Check total line and branch coverage against the baseline"
	@echo "  coverage-baseline-check Ensure a proposed baseline never lowers the target branch"
	@echo "  check-commits  Validate Conventional Commit subjects"
	@echo "  check-pr-title Validate the PR title in PR_TITLE"
	@echo "  check-large-files Validate PR files avoid blocked assets and size bloat"
	@echo "  check-vendored-invariants Validate vendored subagents carry this trunk's fixes"
	@echo "  beta           Build this checkout and publish it to the beta channel"
	@echo "  ci             Run the local CI gate"
	@echo "  clean          Remove generated caches and build output"

install-deps:
	uv sync --frozen --python $(PYTHON_VERSION) --extra dev --dev

install: install-deps
	uv run --frozen --python $(PYTHON_VERSION) pre-commit install
	uv run --frozen --python $(PYTHON_VERSION) pre-commit install --hook-type commit-msg
	npm ci
	npm ci --prefix ui-web
	npm ci --prefix ui-tui
	npm ci --prefix bridge

lint: lint-python lint-ui lint-tui lint-bridge check-vendored-subagents check-vendored-invariants

lint-python:
	uv run --frozen --python $(PYTHON_VERSION) --extra dev ruff check $(PYTHON_LINT_TARGETS)
	uv run --frozen --python $(PYTHON_VERSION) --extra dev ruff format --check $(PYTHON_LINT_TARGETS)

lint-imports:
	uv run --frozen --python $(PYTHON_VERSION) lint-imports

lint-ui:
	npm run gen:check --prefix ui-web
	npm run type-check --prefix ui-web

lint-tui:
	npm run lint --prefix ui-tui
	npm run lint:rpc --prefix ui-tui
	npm run lint:i18n --prefix ui-tui
	npm run type-check --prefix ui-tui

lint-bridge:
	npm run build --prefix bridge

test: test-python test-ui test-tui

test-ui:
	npm test --prefix ui-web

test-python:
	uv run --frozen --python $(PYTHON_VERSION) --all-extras pytest -q

coverage:
	TERM=dumb uv run --frozen --python $(PYTHON_VERSION) --all-extras pytest -q $(COVERAGE_REPORT_ARGS)

coverage-summary:
	uv run --frozen --python $(PYTHON_VERSION) python scripts/coverage_gate.py summary

coverage-diff:
	uv run --frozen --python $(PYTHON_VERSION) python scripts/coverage_gate.py diff --base-ref $(COVERAGE_BASE_REF) --threshold $(COVERAGE_DIFF_THRESHOLD)

coverage-ratchet:
	uv run --frozen --python $(PYTHON_VERSION) python scripts/coverage_gate.py ratchet --tolerance $(COVERAGE_RATCHET_TOLERANCE)

coverage-baseline-check:
	uv run --frozen --python $(PYTHON_VERSION) python scripts/coverage_gate.py baseline-check --base-ref $(COVERAGE_BASE_REF)

coverage-baseline-candidate:
	uv run --frozen --python $(PYTHON_VERSION) python scripts/coverage_gate.py baseline

test-tui:
	npm test --prefix ui-tui

build: build-tui build-bridge build-ui

build-tui:
	npm run build --prefix ui-tui

build-ui:
	npm ci --prefix ui-web
	npm run --prefix ui-web build
	python3 ui-web/build.py

build-bridge:
	npm run build --prefix bridge

build-core:
	uv run --frozen --python $(PYTHON_VERSION) python scripts/build_core_wheel.py

check-commits:
	npx commitlint --from origin/main --to HEAD --config commitlint.config.cjs
	PYTHONPATH=. uv run --frozen --python $(PYTHON_VERSION) --extra dev python scripts/check_commit_messages.py $(COMMIT_RANGE)

check-pr-title:
	PYTHONPATH=. uv run --frozen --python $(PYTHON_VERSION) --extra dev python scripts/check_pr_title.py

check-large-files:
	PYTHONPATH=. uv run --frozen --python $(PYTHON_VERSION) --extra dev python scripts/check_large_files.py $(COMMIT_RANGE)

beta:
	PYTHONPATH=. uv run python scripts/publish_beta.py $(BETA_ARGS)

ci: lint test build

clean:
	rm -rf .pytest_cache .ruff_cache .uv-cache .mypy_cache htmlcov coverage.xml coverage.json coverage-baseline-candidate.json dist build
	rm -rf ui-tui/dist ui-tui/coverage ui-tui/.vitest-cache ui-tui/packages/hermes-ink/dist
	rm -rf bridge/dist
	rm -rf ui-web/dist
	find . -type d -name __pycache__ -prune -exec rm -rf {} +

check-vendored-subagents:
	@python3 scripts/check_vendored_subagents.py

check-vendored-invariants:
	@python3 scripts/check_vendored_invariants.py
