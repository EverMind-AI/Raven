# tui-autotest — real-terminal smoke for `raven tui`

A black-box harness that drives `raven tui` (or any TUI subprocess) through a real
PTY, wrapping the [`tui-use`](https://github.com/onesuper/tui-use) npm CLI.

## What this tier is for, and what it cannot do

It proves **the front door opens**: the Python parent spawns the Node child, the
unix socket RPC connects, Ink renders into an alt-screen, and keystrokes arrive.
Nothing else in the repo can prove that -- vitest mocks the gateway, and pytest
never renders Ink.

It is a smoke tier, not a safety net:

- **It reads a 120x40 character screen.** It can see that a table appeared, not
  that the numbers in it are right. Wrong model resolved, wrong session id,
  wrong permission decision -- invisible unless the visible text changes.
- **A real run is not a real check.** Every assertion in this tier was once
  satisfied by the composer echoing the typed command, so it stayed green for
  months while three commands in the whitelist did not even exist (#228).
- **Tight assertions are brittle, loose ones are vacuous.** Screen text depends
  on terminal size, local state (how many skills / cron jobs exist), and timing.
  That tension does not go away; it is only ever managed.
- **It runs on one machine.** No CI job runs this tier, and in practice it runs
  on macOS only, so platform-specific behaviour (see #205, Windows terminal
  restore) is out of reach by construction.

Behaviour coverage belongs one layer down: `ui-tui/src/__tests__/` for TUI logic
(mock the gateway, render with `ink-testing-library`), `tests/test_rpc_*.py`
for the RPC and dispatch layers.

## Prerequisites

Three, and missing any of them fails in a way that looks like a product bug:

```bash
npm install -g tui-use            # the PTY backend, must be on PATH
npm ci && (cd ui-tui && npm ci)   # repo root AND ui-tui
(cd ui-tui && npm run build)      # dist/entry.js is what `raven tui` loads
```

## Running

```bash
uv run pytest tests/tui/autotest -m e2e          # real subprocesses, spends LLM turns
uv run pytest tests/tui/autotest -m "not e2e"    # harness unit tests only
uv run python -m tests.tui.autotest smoke "uv run raven tui --check"
```

A bare `uv run pytest` skips this tier: `pyproject.toml` sets
`addopts = -m "not integration and not e2e"`.

## Writing a test

Use the `harness` fixture; it kills the session on teardown either way.

```python
import re

import pytest

from tests.tui.autotest.raven_ux import READY_RE, exit_tui

_EXPECTED = r"Raven Status"


@pytest.mark.e2e
def test_status_round_trip(harness):
    harness.spawn("uv run raven tui")

    # 1. Readiness is the status bar, NOT the banner. The banner paints while
    #    the app still drops keystrokes.
    assert harness.wait(READY_RE, timeout=60.0), f"never ready; screen=\n{harness.screen()}"

    # 2. The pattern must not already be on screen, or the assertion below
    #    proves nothing. The welcome frame carries "OpenRouter", the tool list
    #    carries "cron", and the composer echoes whatever you type.
    assert not re.search(_EXPECTED, harness.screen())

    # 3. Confirm the composer took the text before submitting.
    harness.type("/status")
    assert harness.wait(r"/status", timeout=10.0)
    harness.press("enter")

    assert harness.wait(_EXPECTED, timeout=30.0)

    # 4. Ctrl+C is a ladder, not an exit key: busy cancels the turn, a
    #    non-empty composer clears the input, and only then does it quit. Press
    #    until the process is gone.
    exit_tui(harness)
    assert harness.expect_exit(0, timeout=10.0)
```

Those four points are the whole lesson of #228. Each one was a defect in every
file in this tier at the same time, and points 2 and 3 are why nobody noticed.

## Harness API

```python
h = Harness(cols=120, rows=40)
h.env_set({"FORCE_COLOR": "1"})   # before spawn only
h.spawn("uv run raven tui")       # one subprocess per Harness
h.type("text"); h.press("enter")  # tui-use verbs
h.wait(pattern, timeout=...)      # poll snapshots for a regex -> bool
h.screen(); h.dump()              # rendered text (frame rows stripped)
h.expect_exit(0, timeout=...)     # -> bool
h.kill()                          # idempotent
```

Constraints worth knowing:

- **Snapshots are alt-screen only.** Anything in scrollback, including pre-Ink
  ANSI output, is invisible to `wait()`.
- **The tui-use daemon addresses one "current" session.** `spawn` makes its
  session current and the verbs carry no session id, so two pytest processes
  driving the daemon at once would fight over it. Do not run this tier in
  parallel.
- **Shell-quoted args do not survive `tui-use start`.** It goes through a shell,
  so `-m "two words"` loses its quotes. Type slash commands into the TUI
  instead, which exercises the fuller path anyway.
- **`HOME` is the only isolation knob** for a destructive test (`env_set({"HOME":
  str(tmp_path)})`); everos honours neither `RAVEN_HOME` nor XDG.

## Cost

`test_e2e_raven_tui_chat.py` runs live model turns (four per full run) against
whatever provider `~/.raven/config.json` names. Everything else is free.

## Exit codes (`python -m tests.tui.autotest`)

| Code | Meaning |
|---|---|
| 0 | spawn ok, readiness matched, subprocess exited 0 |
| 1 | spawn ok but readiness timed out, or subprocess exit != 0 |
| 2 | harness self-error (`tui-use` missing, spawn pipeline broken) |
