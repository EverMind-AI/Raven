# Raven Test Suite

The test suite covers the CLI, provider routing, context assembly, memory,
channels, sandbox behavior, TUI RPC contracts, and proactive engine flows.

## Running Tests

Run the default suite from the repository root:

```bash
uv run pytest
```

For focused work, target the relevant file or marker:

```bash
uv run pytest tests/test_cli_doctor_commands.py
uv run pytest -m "not real_llm"
```

Tests should avoid live network calls by default. When a test needs external
services or a real model, guard it behind an explicit marker or environment
variable so CI and local contributors get deterministic results.

## Coverage Governance

Raven uses three related checks:

- **Coverage** measures line and branch execution across the default Python
  suite. It is the broad health signal for the production `raven/` package.
- **Diff coverage** measures executable lines added or modified relative to the
  PR target branch. Comments, deleted lines, and non-executable lines are not
  part of its denominator. The initial threshold is 90%.
- **The ratchet** compares total line and branch coverage with the audited
  baseline in `.github/coverage-baseline.json`. Neither metric may decline by
  more than 0.05 percentage points. This protects the established level without
  pretending the current project is already near 100%.

Run the same coverage command used by CI:

```bash
make coverage
```

`PYTHON_VERSION` defaults to the CI version (`3.12`) and is shared by all
Python Make targets. It can be overridden for exploratory compatibility runs,
for example `make coverage PYTHON_VERSION=3.13`; only the configured CI matrix
version may produce an authoritative baseline.

The command prints missing lines and creates `coverage.xml`, `coverage.json`,
and `htmlcov/`. Open `htmlcov/index.html` in a browser for line-by-line detail.
Generated reports are ignored by git.

After running coverage, use the local gates:

```bash
make coverage-summary
make coverage-diff COVERAGE_BASE_REF=origin/main
make coverage-ratchet
make coverage-baseline-check COVERAGE_BASE_REF=origin/main
```

The summary prints separate total line and branch percentages plus the ten
lowest-covered production files. A diff failure lists every uncovered changed
line as `path:line`. A ratchet failure prints the current value, baseline,
change in percentage points, and the files with the largest declines.

The CI unit job runs the default suite once and reuses its data for all gates.
It uploads XML, JSON, and HTML reports even when tests or a gate fail. Diff
coverage runs only for pull requests because a push has no PR target branch;
push builds still enforce the overall ratchet.

### Baseline updates

`make coverage-baseline-candidate` creates
`coverage-baseline-candidate.json` from the current report. The tracked
bootstrap baseline is accepted only after the introducing PR reproduces it in
Python 3.12 Ubuntu CI within the ratchet tolerance. After bootstrap, the
baseline must only be replaced with a candidate downloaded from a successful
`main` CI run. Review the reference commit and totals in the candidate before
replacement. Never edit the percentages by hand or lower the baseline to make
a change pass. PR CI compares a proposed baseline with the target branch and
rejects any decrease in either line or branch coverage.

### Scope and exclusions

The default coverage suite excludes tests marked `integration` or `e2e`, plus
`tests/integration/`, because they require real services, credentials,
subprocess stacks, VMs, or network access and are not deterministic unit-CI
inputs. Provider cases that require live credentials remain skipped.

All production Python under `raven/` is measured except:

- `raven/__main__.py` and `raven/evolver/__main__.py`, which only forward their
  module entry points;
- `raven/utils/win_fcntl_shim.py`, which is executable only on Windows while the
  canonical coverage job runs on Ubuntu.

Type-checking-only blocks and `if __name__ == "__main__"` launcher blocks are
excluded as non-runtime paths. No low-coverage feature module is omitted.
