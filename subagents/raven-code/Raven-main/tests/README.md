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
