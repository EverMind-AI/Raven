# Contributing to Raven

Raven is early-stage. Keep contributions small, reviewable, and tied to a
clear user or maintainer need.

## Development Setup

```bash
make install
```

Run the local verification gate before opening a PR:

```bash
make ci
```

For focused checks:

```bash
make lint-python
make lint-tui
make lint-bridge
make test-python
make test-tui
```

The repository uses:

- `uv` for Python dependency management.
- `ruff` and `pre-commit` for Python and repository hygiene.
- `commitlint` plus a Python checker for Conventional Commit subjects and
  ASCII-only public history.
- `eslint`, `tsc`, `vitest`, and RPC drift checks for the TUI.
- `npm ci`, `tsc`, and `npm audit --audit-level=critical` for the bridge.

For source-based setup details, see [docs/dev.md](docs/dev.md).
AI-collaboration rules live in [AGENTS.md](AGENTS.md); `CLAUDE.md` is kept as a
compatibility entry point.

## Pull Requests

- Use a Conventional Commit PR title, for example `fix: handle empty session`.
- Keep the change scoped to one concern.
- Add or update tests for behavior changes.
- Update docs for user-facing changes.
- Include exact verification commands in the PR description.

## Security

Do not report vulnerabilities in public issues. Follow [SECURITY.md](SECURITY.md).
