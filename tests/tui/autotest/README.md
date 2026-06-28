# tui-autotest — Phase 1 MVP

> TUI 自动测试 harness. Lets Claude Code (or any `Bash()`-driven caller) black-box drive `raven tui` and other TUI subprocesses reproducibly. **Tier 1 backend** = `tui-use` npm CLI thin wrap (selected via 2026-05-20 Day 0 spike per `docs/openspec/changes/tui-auto-test/design.md` §D7).

## Quick start

```bash
# Install backend (one-time, system-level)
npm install -g tui-use

# Install Python deps (already in dev group)
uv sync

# Run all tui-autotest tests
uv run pytest tests/tui/autotest/tests/

# Unit tests only (no real subprocesses)
uv run pytest tests/tui/autotest/tests/ -m "not e2e"

# Ad-hoc smoke (no pytest)
uv run python -m tests.tui.autotest smoke "uv run raven tui --check"
```

## Harness API (see also: `specs/tui-autotest.md` §S3)

```python
import re
from tests.tui.autotest.runner import Harness

h = Harness(cols=120, rows=40)
try:
    h.env_set({"FORCE_COLOR": "1"})
    h.spawn("uv run raven tui")
    assert h.wait(r"Raven", timeout=25.0)
    h.type("/status")
    h.press("enter")
    assert h.wait(re.compile(r"OpenRouter|Model:"), timeout=10.0)
    h.press("escape")  # dismiss overlay
    h.press("ctrl+c")  # exit
    assert h.expect_exit(0, timeout=10.0)
finally:
    h.kill()  # idempotent
```

Or via the `harness` pytest fixture (auto-kills on teardown):

```python
def test_status(harness):
    harness.spawn("uv run raven tui")
    assert harness.wait(r"Raven", timeout=25.0)
    harness.type("/status")
    harness.press("enter")
    assert harness.wait(r"OpenRouter|Model:", timeout=10.0)
    harness.press("escape")
    harness.press("ctrl+c")
    assert harness.expect_exit(0, timeout=10.0)
```

## Best practices

### Readiness patterns

`tui-use snapshot` returns alt-screen rendered text only — content in scrollback (incl. pre-Ink ANSI sequences and the 🦞 emoji) is NOT visible. Use alt-screen-visible patterns for `wait()` readiness:

- ✅ `Raven` (brand text in main panel)
- ✅ `claude-sonnet-4-6` (provider+model header)
- ✅ `Session: tui:default`
- ❌ `🦞` (in byte stream but lost on alt-screen entry)

### Exit sequences

Raven TUI exit UX is **context-dependent**:

- **Inline-output commands** (e.g., command that prints + returns): one Ctrl+C exits.
- **Overlay-output commands** (status panel, picker, decision list): Esc dismisses overlay; Ctrl+C exits.
- **Mid-typing**: first Ctrl+C cancels input, second Ctrl+C exits.

Robust template:

```python
from tests.tui.autotest.runner import BackendError

for key in ("escape", "ctrl+c"):
    try:
        harness.press(key)
    except BackendError:
        break  # session already exited inline — fine
    time.sleep(0.5)
```

A handful of overlays still resist (`channels status`, `sentinel routines`, etc.) — those are marked `@pytest.mark.xfail(strict=False)` in `test_dogfood_whitelist.py` pending tui-chat UX wiring (which is touching Cancel anyway).

### Shell-quoted args don't survive tui-use

`tui-use start` invokes the user's shell, so multi-word `-m "args here"` args lose quotes during shell re-tokenization. Workarounds:

- Use slash commands typed into the TUI (`harness.type("/cmd")` + `press("enter")`), which exercise the full cli.dispatch RPC round-trip without going through shell argv.
- Avoid shell-special chars (`?` glob, `*` glob, `[ ]` brackets) in CLI args.

### LLM-dependent tests

The `agent`/`chat` round-trip costs ~$0.0001 per test (Qwen 3.6 Plus via OpenRouter). Mark with `@pytest.mark.e2e` and run only when chat-flow regressions are suspected.

`test_e2e_raven_tui_chat.py` (TUI chat) and `test_e2e_raven_chat_cli.py` (chat CLI) are `xfail-strict` until L2-A `tui-chat` wires the streaming RPC path. They will start passing the day tui-chat merges; strict=True will then fail the marker, prompting its removal.

### Destructive pairing (Phase 2 reserved, documentation only)

```python
def test_with_isolated_home(harness, tmp_path):
    harness.env_set({"HOME": str(tmp_path), "FORCE_COLOR": "1"})
    harness.spawn("uv run raven tui")
    # ... destructive test body ...
    # cleanup via tmp_path teardown (pytest auto)
```

EC does NOT honor `RAVEN_HOME` / XDG (verified 2026-05-20 spike) — `HOME` env override is the only working isolation path. Destructive commands are NOT yet wired into TUI whitelist; pattern is documented for future L2s adding them.

## Exit codes (CLI / pytest both)

| Code | Meaning |
|---|---|
| 0 | spawn ok + readiness matched + subprocess exit 0 |
| 1 | spawn ok but readiness timeout OR subprocess exit != 0 |
| 2 | harness self-error (tui-use missing / spawn pipeline broken) |

## Out of scope (Phase 1)

- DSL `.tape` file format — dropped 2026-05-20 (Path B pivot). See `docs/RepoMem/temp/tui-auto-test/dsl-decision-rationale.md` for rationale.
- MatchSnapshot golden-file diff — Phase 2 Python method.
- MCP server / interactive REPL — Phase 3.
- pytest plugin / GH Actions runner / asciinema record-replay — Phase 4.
- Windows support.
- Mock LLM provider — Phase 2 spike (currently EC has no `mock_response` integration).

## References

- proposal: `docs/openspec/changes/tui-auto-test/proposal.md`
- design: `docs/openspec/changes/tui-auto-test/design.md`
- tasks: `docs/openspec/changes/tui-auto-test/tasks.md`
- spec: `docs/openspec/changes/tui-auto-test/specs/tui-autotest.md`
- spike data: `docs/RepoMem/temp/tui-auto-test/spike-findings.md`
- DSL decision rationale: `docs/RepoMem/temp/tui-auto-test/dsl-decision-rationale.md`
- backend: <https://github.com/onesuper/tui-use> (MIT, npm `tui-use`)
