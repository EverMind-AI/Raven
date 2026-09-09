# Subagents preset configuration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `/subagents` a credentials-shaped page where five built-in presets are configurable in a few fields, and give Raven the three backend capabilities those presets need.

**Architecture:** Backend first. Tasks 1-3 add the three mechanisms (an `openclaw_json` transcript parser, session-id recovery from stderr, a login-shell environment base for spawned CLIs); Task 4 adds the two presets that depend on them. Tasks 5-9 then rebuild the page: i18n naming, a pure `form.ts`, a display `catalog.ts` plus `SubagentIcon`, the form with an Advanced disclosure, and the regrouped left list.

**Tech Stack:** Python 3.13 + pydantic v2 + pytest (`uv run`); React 19 + Vite + Tailwind v4 + shadcn/Radix + i18next (`pnpm`, from `ui-webui/`).

**Spec:** `docs/specs/2026-08-05-subagents-preset-config-design.md`. Read section 3 before Task 4 - the preset commands are what they are because of measured CLI behaviour, not preference.

## Global Constraints

- Branch is `feat/subagents_preset_config`, already cut from local `main` (`ce7a0e1`). Do not commit on `main`.
- Committing on this feature branch is authorized for this plan's execution: commit each task as its own commit. The authorization is branch-scoped - never commit on `main`, never `git push`, never `git commit --amend` (a fix round is a new commit, per `AGENTS.md` section 3.4).
- Commit messages: Conventional Commits, all-English, ASCII-only, header <= 100 chars, trailer `Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>`.
- Python package manager is `uv` only. Never `pip`. Run tests as `uv run pytest`.
- JS package manager is `pnpm` only, run from `ui-webui/`.
- Code comments: English, and only where the logic is non-obvious or a constraint is hidden. Do not annotate edits.
- Prettier for the frontend is tabs, width 4, single quotes, semicolons, print width 100.
- i18n: edit `src/i18n/locales/{en,zh}.json` with targeted edits only. Never rewrite the file with `json.dump` - it reformats compact objects into diff noise.
- **There is no JS unit-test runner in this repo.** For frontend tasks the verification gate is `pnpm -C frontend lint` (0 errors; pre-existing warnings are acceptable) plus `pnpm -C frontend build`, plus the stated manual check. Do not invent a test framework.
- English UI copy uses `Subagent` / `Subagents`. Chinese copy uses `子智能体`. The word `prototype` / `原型` is retired in both.
- Preset `name` values are reserved: `claude_code`, `codex`, `openclaw`, `hermes`, `mirothinker`.

---

### Task 1: `openclaw_json` transcript format

OpenClaw's `--json` stdout is a single JSON document, not JSONL, so it needs its own parser rather than reuse of `_iter_json_objects`.

**Files:**
- Modify: `raven/agent/subagent/backends/transcript.py` (add parser, extend `__all__`, update module docstring)
- Modify: `raven/agent/subagent/backends/cli_agent.py:247-256` (dispatch), `:29` (import)
- Modify: `raven/config/schema.py:784` (the `transcript_format` literal)
- Modify: `ui-webui/frontend/src/api/ravenConfig.ts:20` (the mirrored TS union)
- Modify: `ui-webui/frontend/src/i18n/locales/en.json`, `zh.json` (the select-option label)
- Test: `tests/test_subagent_third_party.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `parse_openclaw_json(stdout: str) -> tuple[str | None, str | None]` returning `(session_id, reply)`; the literal string `"openclaw_json"` as a valid `transcript_format` / `transcriptFormat` value. Task 4 depends on both.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_subagent_third_party.py`. Import the parser next to the existing backend imports.

```python
def test_parse_openclaw_json_extracts_reply_and_session_id() -> None:
    stdout = json.dumps(
        {
            "payloads": [{"text": "the answer", "mediaUrl": None}],
            "meta": {
                "agentMeta": {"sessionId": "86287eee-186d-498f-82b5-27875a25ee42"},
                "finalAssistantVisibleText": "the answer",
            },
        }
    )
    assert parse_openclaw_json(stdout) == ("86287eee-186d-498f-82b5-27875a25ee42", "the answer")


def test_parse_openclaw_json_falls_back_to_final_visible_text() -> None:
    # A delivery-only run can come back with no payload; the reply is still in meta.
    stdout = json.dumps({"payloads": [], "meta": {"finalAssistantVisibleText": "delivered"}})
    assert parse_openclaw_json(stdout) == (None, "delivered")


def test_parse_openclaw_json_survives_non_json() -> None:
    # Never raise into the run path: a diagnostic-only stdout yields no reply,
    # and _attempt then falls back to the raw combined output.
    assert parse_openclaw_json("openclaw: something went wrong") == (None, None)


async def test_cli_backend_openclaw_json_provisioned_round_trip(tmp_path: Path) -> None:
    # openclaw takes the caller's id, so create and resume are the same command
    # and the reply must come out of the JSON rather than the raw document.
    payload = json.dumps(
        {"payloads": [{"text": "hello"}], "meta": {"agentMeta": {"sessionId": "ignored"}}}
    )
    path = tmp_path / "openclaw.json"
    path.write_text(payload, encoding="utf-8")
    # `sh -c <script> -- {agent_id}` keeps {agent_id} literally in the command,
    # which `provisioned` requires, while making it an inert positional the
    # script never reads. Appending it to `cat` instead would name a second file
    # that does not exist, and cat exits 1 on that.
    cmd = f"sh -c 'cat {path}' -- " + "{agent_id}"
    be = CliAgentBackend(
        name="openclawfake",
        command=cmd,
        resume_command=cmd,
        id_source="provisioned",
        transcript_format="openclaw_json",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    out = await be.run("task", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert out == "hello"
```

The `sh -c` wrapper is deliberate: `provisioned` requires `{agent_id}` to appear
in the command, and this is the cheapest way to satisfy that without the token
also being interpreted as an argument the fixture command would choke on.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_third_party.py -k openclaw -v`
Expected: FAIL - `ImportError: cannot import name 'parse_openclaw_json'`.

- [ ] **Step 3: Add the parser**

In `raven/agent/subagent/backends/transcript.py`:

```python
def parse_openclaw_json(stdout: str) -> tuple[str | None, str | None]:
    """Parse an ``openclaw agent --json`` result.

    Unlike the other two formats this is one JSON document, not JSONL: the reply
    is the first ``payloads[]`` entry carrying text, falling back to
    ``meta.finalAssistantVisibleText`` for a run that produced no payload, and the
    session id is ``meta.agentMeta.sessionId``.
    """
    try:
        obj = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return None, None
    if not isinstance(obj, dict):
        return None, None

    meta = obj.get("meta")
    meta = meta if isinstance(meta, dict) else {}
    agent_meta = meta.get("agentMeta")
    agent_meta = agent_meta if isinstance(agent_meta, dict) else {}
    session_id = agent_meta.get("sessionId")
    if not isinstance(session_id, str):
        session_id = None

    reply: str | None = None
    payloads = obj.get("payloads")
    if isinstance(payloads, list):
        for payload in payloads:
            if isinstance(payload, dict) and isinstance(payload.get("text"), str):
                reply = payload["text"]
                break
    if reply is None and isinstance(meta.get("finalAssistantVisibleText"), str):
        reply = meta["finalAssistantVisibleText"]
    return session_id, reply
```

Update `__all__` to `["parse_codex_jsonl", "parse_claude_stream_json", "parse_openclaw_json"]`, and change the docstring's opening line from "Both formats are newline-delimited JSON" to note that `openclaw_json` is a single document instead.

- [ ] **Step 4: Wire it into the backend and the schema**

`cli_agent.py:29` - extend the import to include `parse_openclaw_json`.

`cli_agent.py`, in `_attempt`, after the `claude_stream_json` branch:

```python
        elif self.transcript_format == "openclaw_json":
            jsonl_id, jsonl_reply = parse_openclaw_json(stdout)
```

`raven/config/schema.py:784`:

```python
    transcript_format: Literal["text", "codex_jsonl", "claude_stream_json", "openclaw_json"] = "text"
```

`ui-webui/frontend/src/api/ravenConfig.ts:20`:

```ts
	transcriptFormat?: 'text' | 'codex_jsonl' | 'claude_stream_json' | 'openclaw_json';
```

Add one i18n key to each locale, beside the existing `transcriptClaude`:

- `en.json` -> `subagent-sidebar.transcriptOpenclaw`: `"openclaw_json (openclaw agent --json)"`
- `zh.json` -> `subagent-sidebar.transcriptOpenclaw`: `"openclaw_json（openclaw agent --json）"`

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_third_party.py -k openclaw -v`
Expected: PASS (4 tests).

Run: `uv run pytest tests/test_subagent_third_party.py tests/test_web_rpc_config.py -q`
Expected: PASS, no regressions.

Run: `pnpm -C frontend build`
Expected: succeeds (the TS union widened, no consumer breaks).

- [ ] **Step 6: Prepare the commit**

```bash
git add raven/agent/subagent/backends/transcript.py raven/agent/subagent/backends/cli_agent.py \
        raven/config/schema.py tests/test_subagent_third_party.py \
        ui-webui/frontend/src/api/ravenConfig.ts \
        ui-webui/frontend/src/i18n/locales/en.json ui-webui/frontend/src/i18n/locales/zh.json
```

Message:

```
feat(agent): parse openclaw --json transcripts

openclaw agent's plain output interleaves ANSI-coloured plugin and transport
diagnostics on stdout, and --verbose off does not suppress them, so a text
transcript cannot yield the reply. Its --json stdout is a single clean JSON
document, so add a parser for it alongside codex_jsonl and claude_stream_json
rather than a regex over escaped JSON, which would return multi-line replies
carrying literal backslash-n.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
```

---

### Task 2: Recover the derived session id from stderr

Hermes prints its session id on stderr, and `_attempt` searches only stdout.

**Files:**
- Modify: `raven/agent/subagent/backends/cli_agent.py:258-262`
- Test: `tests/test_subagent_third_party.py`

**Interfaces:**
- Consumes: nothing.
- Produces: no new symbols. Behaviour change only: with `id_source="derived"` and a `session_id_pattern`, the id is matched against stdout first, then stderr. Task 4's `hermes` preset depends on this.

- [ ] **Step 1: Write the failing test**

Add a helper next to `_fixture_cmd`, then the test.

```python
def _split_stream_cmd(tmp_path: Path, stdout_text: str, stderr_text: str) -> str:
    """A script that writes the reply to stdout and the session id to stderr.

    A script file, not an inline command: `command` goes through shlex.split,
    which would eat the redirection and the quoting.
    """
    path = tmp_path / "split_stream.sh"
    path.write_text(f'printf %s {stdout_text!r}\nprintf %s {stderr_text!r} >&2\n', encoding="utf-8")
    return f"sh {path}"


async def test_cli_backend_derived_id_from_stderr(tmp_path: Path) -> None:
    # hermes prints the reply on stdout and `session_id: <id>` on stderr, so the
    # transcript for id recovery has to be both streams.
    be = CliAgentBackend(
        name="hermesfake",
        command=_split_stream_cmd(tmp_path, "the answer", "session_id: 20260805_093449_6486bf"),
        resume_command="printf resumed-%s {agent_id}",
        id_source="derived",
        transcript_format="text",
        session_id_pattern=r"session_id:\s*(\S+)",
        output_pattern=r"(?s)\A(.*?)\s*\Z",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    first = await be.run("task", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    # output_pattern selects stdout, so the stderr id line stays out of the reply.
    assert first == "the answer"
    second = await be.run("task", task_id="t2", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert second == "resumed-20260805_093449_6486bf"


async def test_cli_backend_prefers_stdout_id_over_stderr(tmp_path: Path) -> None:
    # stdout stays authoritative: a CLI that prints the id on both streams must
    # not have the stderr copy win.
    be = CliAgentBackend(
        name="bothstreams",
        command=_split_stream_cmd(tmp_path, "session_id: from-stdout", "session_id: from-stderr"),
        resume_command="printf resumed-%s {agent_id}",
        id_source="derived",
        transcript_format="text",
        session_id_pattern=r"session_id:\s*(\S+)",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    await be.run("task", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    second = await be.run("task", task_id="t2", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert second == "resumed-from-stdout"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_third_party.py -k "stderr or bothstreams" -v`
Expected: `test_cli_backend_derived_id_from_stderr` FAILS - the second run returns the warning about no extractable session id instead of `resumed-...`. `test_cli_backend_prefers_stdout_id_over_stderr` passes already; it is the regression guard for step 3.

- [ ] **Step 3: Search stderr as a fallback**

In `cli_agent.py`, replace the derived-id block:

```python
        if created and self.id_source == "derived":
            if jsonl_id is not None:
                agent_id = jsonl_id
            elif self._session_id_re is not None:
                # stdout first, then stderr: hermes prints the id only on stderr,
                # which `combined` below already counts as part of the transcript.
                for stream in (stdout, stderr):
                    if (m := self._session_id_re.search(stream)) is not None:
                        agent_id = m.group(1)
                        break
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_third_party.py -k "stderr or bothstreams" -v`
Expected: PASS (2 tests).

Run: `uv run pytest tests/test_subagent_third_party.py -q`
Expected: PASS. `codex` is unaffected because `jsonl_id` is consulted before the regex branch.

- [ ] **Step 5: Prepare the commit**

```bash
git add raven/agent/subagent/backends/cli_agent.py tests/test_subagent_third_party.py
```

Message:

```
fix(agent): read a derived subagent session id from stderr too

A CLI can print its session id on stderr rather than stdout (hermes does, under
chat -Q), and the derived-id branch searched only stdout, so such an agent bound
no handle and was silently not resumable. stdout stays authoritative; stderr is
only a fallback, and it is already part of the transcript the same method
returns as combined output.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
```

---

### Task 3: Spawn CLI subagents under the login shell's environment

**Files:**
- Create: `raven/agent/subagent/backends/env.py`
- Modify: `raven/agent/subagent/backends/cli_agent.py:131` (use it), imports
- Test: `tests/test_subagent_third_party.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `login_shell_env() -> dict[str, str]` in `raven.agent.subagent.backends.env`, plus module-level cache globals `_LOGIN_ENV: dict[str, str] | None` and `_LOGIN_ENV_FAILED: bool` that tests reset via monkeypatch.

- [ ] **Step 1: Write the failing tests**

```python
import raven.agent.subagent.backends.env as env_mod
from raven.agent.subagent.backends.env import login_shell_env


@pytest.fixture
def _clear_login_env_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """The capture is cached per process, so each test needs a cold cache."""
    monkeypatch.setattr(env_mod, "_LOGIN_ENV", None)
    monkeypatch.setattr(env_mod, "_LOGIN_ENV_FAILED", False)


def test_login_shell_env_parses_nul_separated_output(
    monkeypatch: pytest.MonkeyPatch, _clear_login_env_cache: None
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"PATH=/usr/local/bin:/usr/bin\0HOME=/root\0", b"")

    monkeypatch.setattr(env_mod.subprocess, "run", fake_run)
    assert login_shell_env() == {"PATH": "/usr/local/bin:/usr/bin", "HOME": "/root"}
    # Cached: a second call must not shell out again.
    login_shell_env()
    assert len(calls) == 1
    assert calls[0][:2] == ["bash", "-lc"]


def test_login_shell_env_falls_back_when_capture_fails(
    monkeypatch: pytest.MonkeyPatch, _clear_login_env_cache: None
) -> None:
    def boom(argv, **kwargs):
        raise OSError("no bash")

    monkeypatch.setattr(env_mod.subprocess, "run", boom)
    monkeypatch.setenv("RAVEN_ENV_PROBE", "inherited")
    assert login_shell_env()["RAVEN_ENV_PROBE"] == "inherited"


def test_login_shell_env_falls_back_when_capture_is_empty(
    monkeypatch: pytest.MonkeyPatch, _clear_login_env_cache: None
) -> None:
    monkeypatch.setattr(
        env_mod.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"", b""),
    )
    monkeypatch.setenv("RAVEN_ENV_PROBE", "inherited")
    assert login_shell_env()["RAVEN_ENV_PROBE"] == "inherited"


async def test_cli_backend_uses_login_env_and_per_agent_env_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clear_login_env_cache: None
) -> None:
    # The login shell's value is the base; the per-agent `env` still overrides it,
    # which is the escape hatch for a machine whose login shell resolves the
    # wrong interpreter.
    monkeypatch.setattr(
        env_mod.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, b"FROM_LOGIN=yes\0OVERRIDE_ME=login\0PATH=/usr/bin:/bin\0", b""
        ),
    )
    script = tmp_path / "show_env.sh"
    script.write_text('printf "%s/%s" "$FROM_LOGIN" "$OVERRIDE_ME"\n', encoding="utf-8")
    be = CliAgentBackend(
        name="envcheck",
        command=f"sh {script}",
        env={"OVERRIDE_ME": "agent"},
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    out = await be.run("task", task_id="t1", workspace=tmp_path, executor=None)
    assert out == "yes/agent"
```

Add `import subprocess` to the test module's imports.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_third_party.py -k "login_env or login_shell" -v`
Expected: FAIL - `ModuleNotFoundError: raven.agent.subagent.backends.env`.

- [ ] **Step 3: Write the module**

`raven/agent/subagent/backends/env.py`:

```python
"""The environment a spawned CLI subagent runs under.

A third-party CLI agent is a program the user also runs from their own terminal,
so it should see that terminal's environment rather than raven's. Raven's own
process environment carries its launcher's PATH ordering and whatever the editor
injected, which is enough to make a child resolve the wrong interpreter: raven's
PATH can list a conda bin ahead of /usr/local/bin, and openclaw hard-exits on an
unsupported node with no way to override the check.
"""

from __future__ import annotations

import os
import subprocess

from loguru import logger

_LOGIN_ENV: dict[str, str] | None = None
_LOGIN_ENV_FAILED = False


def login_shell_env() -> dict[str, str]:
    """Return the login shell's environment, captured once per process.

    Falls back to raven's own environment when the capture fails or comes back
    empty, so an unusual login shell degrades to the previous behaviour instead
    of making every spawn fail.
    """
    global _LOGIN_ENV, _LOGIN_ENV_FAILED
    if _LOGIN_ENV is not None:
        return _LOGIN_ENV
    if _LOGIN_ENV_FAILED:
        return dict(os.environ)
    try:
        proc = subprocess.run(["bash", "-lc", "env -0"], capture_output=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as exc:
        _LOGIN_ENV_FAILED = True
        logger.warning("Login shell environment capture failed ({}); subagents inherit raven's", exc)
        return dict(os.environ)
    captured = {
        key: value
        for key, _, value in (
            entry.partition("=") for entry in proc.stdout.decode("utf-8", "replace").split("\0")
        )
        if key and _
    }
    if not captured:
        _LOGIN_ENV_FAILED = True
        logger.warning("Login shell environment came back empty; subagents inherit raven's")
        return dict(os.environ)
    _LOGIN_ENV = captured
    return captured


__all__ = ["login_shell_env"]
```

The comprehension keeps only entries that actually contained `=` (`_` is the
separator returned by `partition`), which drops the trailing empty field `env -0`
leaves behind.

- [ ] **Step 4: Use it in the backend**

`cli_agent.py` - add `from raven.agent.subagent.backends.env import login_shell_env` to the imports, and change the env line in `_exec`:

```python
            env = {**login_shell_env(), **self.env}
```

Update the module docstring's "Runs on the host rather than through the sandbox
executor - these CLIs need the host's auth, config, and PATH." to say the
environment is the login shell's, not raven's.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_third_party.py -k "login_env or login_shell" -v`
Expected: PASS (4 tests).

Run: `uv run pytest tests/test_subagent_third_party.py -q`
Expected: PASS.

- [ ] **Step 6: Verify against the real CLIs**

Run:

```bash
uv run python -c "
from raven.agent.subagent.backends.env import login_shell_env
import shutil
env = login_shell_env()
print('vars:', len(env))
print('node:', shutil.which('node', path=env['PATH']))
"
```

Expected: a node in one of `>=22.22.3 <23`, `>=24.15.0 <25`, `>=25.9.0`. On this
host that is `/usr/local/bin/node` (v22.23.2), where raven's own environment
resolves v25.8.2 and openclaw refuses to start.

- [ ] **Step 7: Prepare the commit**

```bash
git add raven/agent/subagent/backends/env.py raven/agent/subagent/backends/cli_agent.py \
        tests/test_subagent_third_party.py
```

Message:

```
fix(agent): run CLI subagents under the login shell environment

A spawned CLI agent inherited raven's own process environment, which carries its
launcher's PATH ordering and editor-injected variables. On a host where a conda
bin precedes /usr/local/bin that resolves an unsupported node and openclaw
hard-exits, and CLAUDE_CODE_SSE_PORT would point a spawned claude at the editor
session's port. Capture the login shell's environment once and use it as the
base; the per-agent env still layers on top, and a failed capture falls back to
inheriting so no spawn breaks.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
```

---

### Task 4: The `openclaw` and `hermes` presets

**Files:**
- Modify: `raven/agent/subagent/presets.py`
- Test: `tests/test_subagent_third_party.py:963` (`test_presets_are_valid_and_complete`)

**Interfaces:**
- Consumes: `"openclaw_json"` as a `transcript_format` (Task 1); stderr session-id recovery (Task 2).
- Produces: two new keys in `THIRD_PARTY_SUBAGENT_PRESETS`, surfaced unchanged by `raven.subagents.presets` and `GET /raven/subagents/presets`. Tasks 7 and 9 key their display catalog off the names `openclaw` and `hermes`.

- [ ] **Step 1: Extend the preset test**

In `test_presets_are_valid_and_complete`, change the name set and append assertions:

```python
    assert set(presets) == {"claude_code", "codex", "mirothinker", "openclaw", "hermes"}
```

```python
    openclaw = presets["openclaw"]
    # Create and resume are intentionally identical: an openclaw session is
    # addressed by the id the caller supplies, so re-passing it continues that
    # session. Verified against openclaw 2026.7.1-2.
    assert openclaw["command"] == openclaw["resumeCommand"]
    assert "--session-id {agent_id}" in openclaw["command"]
    assert openclaw["idSource"] == "provisioned"
    # --json is required, not cosmetic: plain output interleaves ANSI-coloured
    # plugin and transport diagnostics on stdout and --verbose off keeps them.
    assert "--json" in openclaw["command"]
    assert openclaw["transcriptFormat"] == "openclaw_json"

    hermes = presets["hermes"]
    # The global -z flag does not join a session: `-z --resume <id>` answers with
    # no prior context and opens a new session, in either flag order. So a
    # stateful hermes has to go through the `chat` subcommand, whose -Q keeps
    # stdout to the final answer and puts the session id on stderr.
    assert hermes["command"].startswith("hermes --yolo chat ")
    assert "-z" not in hermes["command"]
    assert "-Q" in hermes["command"]
    assert hermes["idSource"] == "derived"
    assert hermes["transcriptFormat"] == "text"
    assert "--resume {agent_id}" in hermes["resumeCommand"]
    assert "{agent_id}" not in hermes["command"]
    assert hermes["sessionIdPattern"] == r"session_id:\s*(\S+)"
    # Keeps the stderr session-id line out of the reply.
    assert hermes["outputPattern"] == r"(?s)\A(.*?)\s*\Z"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_subagent_third_party.py::test_presets_are_valid_and_complete -v`
Expected: FAIL on the name-set assertion (`openclaw` and `hermes` missing).

- [ ] **Step 3: Add the presets**

In `presets.py`, beside `_CLAUDE_TAIL` and `_CODEX_HEAD`:

```python
# Create and resume are the same call: an openclaw session is addressed by the id
# the caller supplies. --json is required, not cosmetic - plain output interleaves
# ANSI-coloured plugin and transport diagnostics on stdout, and --verbose off does
# not suppress them, so a text transcript cannot yield the reply.
_OPENCLAW_CMD = "openclaw agent --local --json --session-id {agent_id} -m {prompt}"
# `chat -q` rather than the global `-z`: -z does not join a session, so
# `-z --resume <id>` silently starts a new one. -Q keeps stdout to the final
# answer alone, and prints the session id on stderr.
_HERMES_ONESHOT = "hermes --yolo chat -Q -q {prompt}"
```

Then two entries in `THIRD_PARTY_SUBAGENT_PRESETS`:

```python
    "openclaw": {
        "name": "openclaw",
        "kind": "cli",
        "description": (
            "OpenClaw agent CLI - general assistant with its own tool set. Needs a node "
            "the CLI supports on PATH, and an agent id whose workspace has no persona "
            "files, or the first turn of a fresh session answers the bootstrap instead "
            "of the task."
        ),
        "command": _OPENCLAW_CMD,
        "resumeCommand": _OPENCLAW_CMD,
        "idSource": "provisioned",
        "transcriptFormat": "openclaw_json",
    },
    "hermes": {
        "name": "hermes",
        "kind": "cli",
        "description": "Hermes Agent CLI - general assistant with tool calling.",
        "command": _HERMES_ONESHOT,
        "resumeCommand": f"{_HERMES_ONESHOT} --resume {{agent_id}}",
        "idSource": "derived",
        "transcriptFormat": "text",
        "sessionIdPattern": r"session_id:\s*(\S+)",
        "outputPattern": r"(?s)\A(.*?)\s*\Z",
    },
```

Extend the module docstring's bullet list with both, mirroring how `claude_code`
and `codex` explain their required flags.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_third_party.py::test_presets_are_valid_and_complete tests/test_subagent_third_party.py::test_presets_have_no_timeout -v`
Expected: PASS. `_normalized` validates every preset against `SubagentsConfig` at
import, so a placeholder mistake fails here rather than at a user's click.

Run: `uv run pytest tests/ -q -x`
Expected: PASS.

- [ ] **Step 5: Dispatch to both for real**

Both CLIs are installed on this host. With the gateway running:

```bash
uv run python -c "
import asyncio, uuid
from raven.agent.subagent.backends import build_third_party_backend
from raven.config.schema import SubagentsConfig
from raven.agent.subagent.presets import third_party_subagent_preset
from pathlib import Path
import tempfile

async def go(name):
    cfg = SubagentsConfig(third_party=[third_party_subagent_preset(name)]).third_party[0]
    be = build_third_party_backend(cfg)
    with tempfile.TemporaryDirectory() as d:
        out = await be.run('Reply with exactly the word: PONG', task_id='t1',
                           workspace=Path(d), executor=None,
                           session_key='smoke', instance=str(uuid.uuid4()))
    print(name, '->', repr(out[:200]))

asyncio.run(go('hermes'))
asyncio.run(go('openclaw'))
"
```

Expected: `hermes -> 'PONG'`. For `openclaw`, a fresh session may answer its
bootstrap persona rather than `PONG` (spec 5.3) - that is the documented
behaviour, not a failure of this task; record what came back. Report the actual
output either way rather than asserting success.

- [ ] **Step 6: Prepare the commit**

```bash
git add raven/agent/subagent/presets.py tests/test_subagent_third_party.py
```

Message:

```
feat(agent): add openclaw and hermes subagent presets

Both are stateful, by different mechanisms: openclaw takes the caller's session
id so create and resume are one command, while hermes mints its own and only the
chat subcommand joins a session, so its id is derived from the -Q stderr line.
Command flags are pinned to what was measured against openclaw 2026.7.1-2 and
hermes on 2026-08-05, not to what the docs claim; the disagreements are recorded
in the design doc.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
```

---

### Task 5: Unify the user-visible naming

Pure copy change, no structural edits, so it lands and can be reviewed on its own.

**Files:**
- Modify: `ui-webui/frontend/src/i18n/locales/en.json`, `zh.json`
- Modify: `ui-webui/frontend/src/hooks/useRavenSubagents.ts:27`

**Interfaces:**
- Consumes: nothing.
- Produces: the i18n keys later tasks render. No key is renamed; only values change, plus new keys added in Tasks 8 and 9.

- [ ] **Step 1: Edit the English values**

Targeted edits only. Left column is the current value, right is the new one.

| Key | New value |
|---|---|
| `common.subagents` | `Subagents` |
| `subagent-sidebar.kicker` | `Subagents · Presets` |
| `subagent-sidebar.title` | `Subagents` |
| `subagent-sidebar.selectHint` | `Select a subagent, or add one from a preset.` |
| `subagent-sidebar.empty` | `No subagents configured yet.` |
| `subagent-sidebar.newTitle` | `New subagent` |
| `subagent-sidebar.editTitle` | `Edit subagent` |
| `subagent-sidebar.saved` | `Subagents saved and applied to the running gateway.` |
| `subagent-sidebar.nameTaken` | `A subagent named "{{name}}" already exists. Pick another name, or edit that one.` |
| `panel.subagent.entity` | `subagent` |
| `tool.callSubagent` | `Call Subagent` |
| `tool.subagentDag` | `Execute Subagent DAG` |
| `tool.subagentDagTitle_one` | `Execute Subagent DAG: {{count}} node` |
| `tool.subagentDagTitle_other` | `Execute Subagent DAG: {{count}} nodes` |
| `tool.summary.dag_one` | `ran {{count}} subagent DAG` |
| `tool.summary.dag_other` | `ran {{count}} subagent DAGs` |
| `tool.summary.subagent_one` | `delegated to {{count}} subagent` |
| `tool.summary.subagent_other` | `delegated to {{count}} subagents` |
| `messageBubble.hintSource.subagent_response` | `Subagent Response` |
| `subagent-monitor.empty` | `No subagent instances in this session yet.` |

`subagent-sidebar.subtitle` already reads "Third-party agents Raven can dispatch to." - leave it.

- [ ] **Step 2: Edit the Chinese values**

Only the four carrying `原型`; `子智能体` is already the canonical term and stays.

| Key | New value |
|---|---|
| `subagent-sidebar.kicker` | `子智能体 · 预设` |
| `subagent-sidebar.selectHint` | `选择一个子智能体，或从预设添加。` |
| `subagent-sidebar.empty` | `暂无已配置的子智能体。` |
| `subagent-sidebar.newTitle` | `新建子智能体` |
| `subagent-sidebar.editTitle` | `编辑子智能体` |

- [ ] **Step 3: Fix the hardcoded string**

`hooks/useRavenSubagents.ts:27` - change `Failed to load sub-agents:` to
`Failed to load subagents:`.

- [ ] **Step 4: Verify no stale spelling remains**

Run:

```bash
cd ui-webui/frontend
grep -rniE "sub-agent|sub agent|prototype|原型" src/i18n/locales/en.json src/i18n/locales/zh.json src/hooks/useRavenSubagents.ts
```

Expected: no output.

Run: `pnpm -C frontend lint && pnpm -C frontend build`
Expected: 0 eslint errors, build succeeds.

Run:

```bash
node -e "for (const l of ['en','zh']) JSON.parse(require('fs').readFileSync('src/i18n/locales/'+l+'.json')); console.log('both locales parse')"
```

Expected: `both locales parse`.

- [ ] **Step 5: Manual check**

Start the app (`./start_webapp.sh` from the repo root), open `/subagents`, and
confirm the rail entry, kicker, and title all read "Subagents". Switch the UI
language and confirm the Chinese copy has no `原型`.

- [ ] **Step 6: Prepare the commit**

```bash
git add ui-webui/frontend/src/i18n/locales/en.json ui-webui/frontend/src/i18n/locales/zh.json \
        ui-webui/frontend/src/hooks/useRavenSubagents.ts
```

Message:

```
refactor(ui-webui): settle on one spelling for subagents

The UI mixed Sub-Agents, Sub-Agent, sub-agent and prototype across the rail, the
page, the tool cards and the instance panel. Use Subagent/Subagents throughout
and retire the prototype wording, which described an earlier model of the page.
Keys and the directory name are unchanged, so this is copy only.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
```

---

### Task 6: Extract `form.ts`

Move the form's pure logic out of the page with no behaviour change, so the later
UI tasks work on a small file and this move stays reviewable on its own.

**Files:**
- Create: `ui-webui/frontend/src/pages/subagent/form.ts`
- Modify: `ui-webui/frontend/src/pages/subagent/index.tsx` (import from it; delete the moved code)

**Interfaces:**
- Consumes: `RavenThirdPartySubagent` from `@/api` (with `'openclaw_json'` in the `transcriptFormat` union, Task 1).
- Produces, all from `pages/subagent/form.ts`:
  - `type Kind = 'cli' | 'openai'`
  - `interface FormState` - the 20 fields as they exist today at `index.tsx:41-61`
  - `const EMPTY_FORM: FormState`
  - `function toForm(a: RavenThirdPartySubagent): FormState`
  - `function toEntry(f: FormState, previous?: RavenThirdPartySubagent): RavenThirdPartySubagent`
  - `function envToText(env: Record<string, string> | undefined): string`
  - `function textToEnv(text: string): Record<string, string>`
  - `function validate(f: FormState, agents: RavenThirdPartySubagent[], editingName: string | null): { canSubmit: boolean; nameCollides: boolean; cliOk: boolean; stateful: boolean; derived: boolean }`

  Tasks 8 and 9 import from here. Do not rename any of these later.

- [ ] **Step 1: Move the code verbatim**

Cut `index.tsx:39-183` - `Kind`, `FormState`, `EMPTY_FORM`, `envToText`,
`textToEnv`, the `text` / `num` helpers with their comment, `toForm`, `toEntry` -
into `pages/subagent/form.ts` unchanged, exporting `Kind`, `FormState`,
`EMPTY_FORM`, `envToText`, `textToEnv`, `toForm`, `toEntry`. Keep `text` and
`num` module-private. Keep every existing comment; they record constraints
(`toForm`'s note on why each field falls back to a string, `toEntry`'s on the
stored api key).

- [ ] **Step 2: Move the validation into `validate`**

The page currently computes this inline at `index.tsx:226-237`. Move it, keeping
the comment that says it mirrors `raven/config/schema.py`:

```ts
/** Mirrors the backend's rules (raven/config/schema.py). A stateful
 *  *provisioned* command needs `{agent_id}`; a *derived* command must not
 *  (the CLI mints the id itself). */
export function validate(
	f: FormState,
	agents: RavenThirdPartySubagent[],
	editingName: string | null,
) {
	const stateful = f.resumeCommand.trim().length > 0;
	const derived = f.idSource === 'derived';
	const idInCommand = f.command.includes('{agent_id}');
	const cliOk = stateful
		? f.resumeCommand.includes('{agent_id}') && (derived ? !idInCommand : idInCommand)
		: !idInCommand;
	const openaiOk = !!f.baseUrl.trim() && !!f.model.trim();
	const nameCollides = agents.some(
		(a) => a.name === f.name.trim() && a.name !== editingName,
	);
	const canSubmit =
		!!f.name.trim() &&
		!nameCollides &&
		(f.kind === 'openai' ? openaiOk : !!f.command.trim() && cliOk);
	return { canSubmit, nameCollides, cliOk, stateful, derived };
}
```

- [ ] **Step 3: Import in the page**

Replace the deleted block in `index.tsx` with:

```ts
import { EMPTY_FORM, toEntry, toForm, validate } from '@/pages/subagent/form';
import type { FormState, Kind } from '@/pages/subagent/form';
```

and replace the inline validation with
`const { canSubmit, nameCollides, cliOk, stateful, derived } = validate(form, agents, editingName);`.

- [ ] **Step 4: Verify nothing changed**

Run: `pnpm -C frontend lint && pnpm -C frontend build`
Expected: 0 eslint errors, build succeeds.

Run: `git diff --stat`
Expected: `index.tsx` shrinks by roughly 145 lines, `form.ts` is new. No other file touched.

- [ ] **Step 5: Manual check**

On `/subagents`, create a CLI subagent, save it, reopen it, and delete it. Then
create an OpenAI-compatible one with a key, reopen it and confirm the key field
is blank with the "leave blank to keep" hint. Behaviour must be identical to
before this task.

- [ ] **Step 6: Prepare the commit**

```bash
git add ui-webui/frontend/src/pages/subagent/form.ts ui-webui/frontend/src/pages/subagent/index.tsx
```

Message:

```
refactor(ui-webui): split the subagent form state out of the page

The page was one 667-line file holding state shape, wire conversion, validation
and every field's markup. Move the pure parts into form.ts unchanged so the
upcoming preset UI works on a small file. No behaviour change.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
```

---

### Task 7: `catalog.ts` and `SubagentIcon`

**Files:**
- Create: `ui-webui/frontend/src/pages/subagent/catalog.ts`
- Create: `ui-webui/frontend/src/components/SubagentIcon.tsx`
- Modify: `ui-webui/frontend/src/i18n/locales/en.json`, `zh.json` (preset labels)

**Interfaces:**
- Consumes: preset names from Task 4 (`openclaw`, `hermes`).
- Produces:
  - `PRESET_DISPLAY: Record<string, PresetDisplay>` and
    `interface PresetDisplay { labelKey: string; basicFields: BasicField[] }` where
    `type BasicField = 'apiKey' | 'baseUrl' | 'model'`
  - `function presetLabel(name: string, t: TFunction): string` - the catalog label, falling back to `name`
  - `function isPresetName(name: string, presets: RavenThirdPartySubagent[]): boolean`
  - `<SubagentIcon type={string} size={number} />`

  Tasks 8 and 9 import all four.

- [ ] **Step 1: Write the catalog**

`pages/subagent/catalog.ts`. `basicFields` lists what stays outside the Advanced
disclosure for that preset; everything else in the form goes inside it.

```ts
import type { TFunction } from 'i18next';

import type { RavenThirdPartySubagent } from '@/api';

/** Which fields a preset needs outside the Advanced disclosure. */
export type BasicField = 'apiKey' | 'baseUrl' | 'model';

export interface PresetDisplay {
	labelKey: string;
	basicFields: BasicField[];
}

/** Display metadata for the built-in presets, keyed by the `name` the backend
 *  ships in raven/agent/subagent/presets.py. A preset with no entry here still
 *  renders, with its name as the label and a neutral icon, so adding one in
 *  Python cannot break this page - the same contract ProviderIcon has with the
 *  provider registry. */
export const PRESET_DISPLAY: Record<string, PresetDisplay> = {
	claude_code: { labelKey: 'subagent-sidebar.presetClaudeCode', basicFields: [] },
	codex: { labelKey: 'subagent-sidebar.presetCodex', basicFields: [] },
	openclaw: { labelKey: 'subagent-sidebar.presetOpenclaw', basicFields: [] },
	hermes: { labelKey: 'subagent-sidebar.presetHermes', basicFields: [] },
	mirothinker: {
		labelKey: 'subagent-sidebar.presetMirothinker',
		basicFields: ['apiKey', 'baseUrl', 'model'],
	},
};

export function presetLabel(name: string, t: TFunction): string {
	const entry = PRESET_DISPLAY[name];
	return entry ? t(entry.labelKey) : name;
}

export function isPresetName(name: string, presets: RavenThirdPartySubagent[]): boolean {
	return presets.some((p) => p.name === name);
}
```

- [ ] **Step 2: Write the icon**

`components/SubagentIcon.tsx`, mirroring `components/ProviderIcon.tsx`:

```tsx
import { Anthropic, Codex, OpenAI } from '@lobehub/icons';
import type { ReactNode } from 'react';

import Bot from '~icons/solar/cpu-bold-duotone';
import Terminal from '~icons/solar/command-bold-duotone';

interface SubagentIconProps {
	/** A preset `name`, or a custom subagent's kind (`cli` / `openai`). */
	type: string;
	size?: number;
}

/** Brand mark per built-in preset name. Anything unmapped - every custom
 *  subagent, and any preset added to presets.py without an entry here - falls
 *  back to a neutral glyph rather than rendering nothing. */
export function SubagentIcon({ type, size = 20 }: SubagentIconProps) {
	let inner: ReactNode;
	switch (type) {
		case 'claude_code':
			inner = <Anthropic.Avatar size={size} />;
			break;
		case 'codex':
			inner = <Codex.Avatar size={size} />;
			break;
		case 'mirothinker':
		case 'openai':
			inner = <OpenAI.Avatar size={size} />;
			break;
		case 'cli':
			inner = <Terminal width={size} height={size} />;
			break;
		default:
			inner = <Bot width={size} height={size} />;
	}
	return <span className="inline-flex shrink-0 items-center justify-center">{inner}</span>;
}
```

Both icon names were checked against `node_modules/@iconify-json/solar/icons.json`
and exist: `command-bold-duotone` and `cpu-bold-duotone` (the latter is already
imported by `pages/subagent/index.tsx:35`). Note that `terminal-bold-duotone`
does *not* exist in that set, so do not substitute it.

- [ ] **Step 3: Add the label keys**

`en.json`, inside `subagent-sidebar`:

| Key | Value |
|---|---|
| `presetClaudeCode` | `Claude Code` |
| `presetCodex` | `Codex` |
| `presetOpenclaw` | `OpenClaw` |
| `presetHermes` | `Hermes Agent` |
| `presetMirothinker` | `MiroThinker` |

`zh.json`: the same five keys with the same values - these are product names and
are not translated.

- [ ] **Step 4: Verify**

Run: `pnpm -C frontend lint && pnpm -C frontend build`
Expected: 0 eslint errors, build succeeds. Nothing imports these yet, so the only
signal is that they compile and the icon names resolve.

- [ ] **Step 5: Prepare the commit**

```bash
git add ui-webui/frontend/src/pages/subagent/catalog.ts \
        ui-webui/frontend/src/components/SubagentIcon.tsx \
        ui-webui/frontend/src/i18n/locales/en.json ui-webui/frontend/src/i18n/locales/zh.json
```

Message:

```
feat(ui-webui): add subagent preset display metadata

Labels, marks and per-preset field lists live on the client; presets.py stays the
single source of truth for the config template. A preset with no catalog entry
still renders under its own name, so adding one in Python cannot break the page.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
```

---

### Task 8: `SubagentForm.tsx` with the Advanced disclosure

**Files:**
- Create: `ui-webui/frontend/src/pages/subagent/SubagentForm.tsx`
- Modify: `ui-webui/frontend/src/pages/subagent/form.ts` (the api-key rule)
- Modify: `ui-webui/frontend/src/pages/subagent/index.tsx` (render it)
- Modify: `ui-webui/frontend/src/i18n/locales/en.json`, `zh.json`

**Interfaces:**
- Consumes: everything from Tasks 6 and 7.
- Changes: `validate` from Task 6 gains `keyOk: boolean` in its return object.
  Task 9 does not read it, but the widened shape is what `index.tsx` destructures
  from here on.
- Produces: `<SubagentForm />` taking
  `{ form, setForm, preset, editingName, submitting, error, canSubmit, nameCollides, cliOk, stateful, derived, onSubmit, onClose, onDelete }`,
  where `preset: RavenThirdPartySubagent | null` is the matching built-in when the
  selection is a preset. Task 9 renders it.

- [ ] **Step 1: Extend `validate` with the api-key rule**

A `mirothinker` row can currently be saved with the empty `apiKey` the preset
ships, then fails at dispatch. In `form.ts`, add to `validate`:

```ts
	// An openai-kind agent cannot work without a key. Blank is still allowed when
	// one is already stored for this name - that is the "leave blank to keep the
	// stored key" path, which never surfaces the stored value.
	const storedKey = agents.some((a) => a.name === editingName && a.kind === 'openai' && !!a.apiKey);
	const keyOk = f.kind !== 'openai' || !!f.apiKey.trim() || storedKey;
```

and include `&& keyOk` in `canSubmit`'s openai branch, returning `keyOk` from the
function so the form can mark the field.

- [ ] **Step 2: Move the markup**

Move `index.tsx:370-638` - the whole `<div className="flex flex-col gap-y-3 p-6 max-w-2xl">`
subtree - into `SubagentForm.tsx` unchanged, then restructure only the grouping:

- Header row (title, delete, close) stays at the top. When `preset` is non-null,
  render `<SubagentIcon type={preset.name} size={24} />`, `presetLabel(...)`, the
  kind badge, a `stateful` badge when `preset.kind === 'cli' && preset.resumeCommand`,
  and `preset.description` as muted text.
- Name and Type inputs render only when `preset` is null. A preset's name is its
  primary key and its kind is fixed.
- For a preset, render only the fields in `PRESET_DISPLAY[preset.name].basicFields`,
  plus the four that apply to every CLI agent: `cwd`, `env`, `timeout`,
  `readsLocalFiles`. For a custom agent, render the kind's full basic set as
  today: `command` for cli, `baseUrl` / `model` / `apiKey` for openai.
- Everything else - `description`, `command`, `resumeCommand` with its hint,
  `idSource`, `transcriptFormat`, `sessionIdPattern`, `outputPattern`,
  `systemPrompt`, `temperature`, `maxTokens` - moves inside:

```tsx
<details className="mt-2 rounded-lg border px-3 py-2">
	<summary className="cursor-pointer text-sm text-muted-foreground hover:text-foreground">
		{t('subagent-sidebar.advanced')}
	</summary>
	<div className="mt-3 flex flex-col gap-y-3">{/* the fields above */}</div>
</details>
```

Add `openclaw_json` to the `transcriptFormat` select, using the
`subagent-sidebar.transcriptOpenclaw` key added in Task 1.

Keep the existing conditional that shows `sessionIdPattern` only when
`derived && form.transcriptFormat === 'text'` (`index.tsx:503`).

- [ ] **Step 3: Add the new keys**

`en.json`, inside `subagent-sidebar`:

| Key | Value |
|---|---|
| `advanced` | `Advanced` |
| `apiKeyRequiredTag` | `Required` |
| `presetFixedNameHint` | `A preset's name is fixed - it is how Raven addresses this agent.` |

`zh.json`:

| Key | Value |
|---|---|
| `advanced` | `高级` |
| `apiKeyRequiredTag` | `必填` |
| `presetFixedNameHint` | `预设的名称固定 —— Raven 以它寻址该智能体。` |

- [ ] **Step 4: Render it from the page**

In `index.tsx`, replace the moved subtree with:

```tsx
<SubagentForm
	form={form}
	setForm={setForm}
	preset={presets.find((p) => p.name === (editingName || form.name)) ?? null}
	editingName={editingName}
	submitting={submitting}
	error={error}
	canSubmit={canSubmit}
	nameCollides={nameCollides}
	cliOk={cliOk}
	stateful={stateful}
	derived={derived}
	onSubmit={submit}
	onClose={close}
	onDelete={() => {
		const target = agents.find((a) => a.name === editingName);
		if (target) setDeleteTarget(target);
	}}
/>
```

- [ ] **Step 5: Verify**

Run: `pnpm -C frontend lint && pnpm -C frontend build`
Expected: 0 eslint errors, build succeeds.

- [ ] **Step 6: Manual check**

On `/subagents`:

1. Open the `mirothinker` preset. Only API key, base URL and model show outside
   Advanced; base URL and model are pre-filled; Save is disabled until a key is
   typed.
2. Open `claude_code`. No required field; the command sits inside Advanced,
   pre-filled, and is editable.
3. Save `claude_code` untouched, then confirm `~/.raven/config.json` gained a
   `subagents.thirdParty` entry whose `command` matches the preset.
4. Add a custom CLI agent and confirm the full field set still appears with Name
   and Type editable.

- [ ] **Step 7: Prepare the commit**

```bash
git add ui-webui/frontend/src/pages/subagent/SubagentForm.tsx \
        ui-webui/frontend/src/pages/subagent/form.ts \
        ui-webui/frontend/src/pages/subagent/index.tsx \
        ui-webui/frontend/src/i18n/locales/en.json ui-webui/frontend/src/i18n/locales/zh.json
```

Message:

```
feat(ui-webui): ask a preset only for the fields it needs

Configuring a preset meant reading a flat form of 20-plus fields, including two
raw regexes. Show only what the selected agent actually needs - nothing for the
CLI presets, an API key for mirothinker - and move the rest behind an Advanced
disclosure, pre-filled and still editable so a non-standard install stays
fixable from the UI. Saving an openai-kind agent now requires a key unless one
is already stored, instead of accepting the preset's empty placeholder and
failing at dispatch.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
```

---

### Task 9: Regroup the left list

**Files:**
- Modify: `ui-webui/frontend/src/pages/subagent/index.tsx`
- Modify: `ui-webui/frontend/src/i18n/locales/en.json`, `zh.json`

**Interfaces:**
- Consumes: everything from Tasks 6-8.
- Produces: the finished page. Nothing depends on it.

- [ ] **Step 1: Add the group keys**

`en.json`, inside `subagent-sidebar`:

| Key | Value |
|---|---|
| `configured` | `Configured` |
| `groupPresets` | `Presets` |
| `groupCustom` | `Custom` |
| `newCli` | `Custom CLI agent` |
| `newOpenai` | `Custom OpenAI-compatible agent` |
| `presetNameReserved` | `"{{name}}" is a preset name. Open it under Presets, or pick another name.` |

`zh.json`:

| Key | Value |
|---|---|
| `configured` | `已配置` |
| `groupPresets` | `预设` |
| `groupCustom` | `自定义` |
| `newCli` | `自定义命令行智能体` |
| `newOpenai` | `自定义 OpenAI 兼容智能体` |
| `presetNameReserved` | `"{{name}}" 是预设名称。请在"预设"分组中打开它，或换一个名称。` |

- [ ] **Step 2: Replace the sidebar body**

Delete the `addFromPreset` dropdown (`index.tsx:286-305`) and the lone `Plus`
button; presets are now permanently visible, so a dropdown that pre-fills a blank
form has nothing left to do. Remove the now-unused `DropdownMenu` imports and the
`subagent-sidebar.addFromPreset` key from both locales.

Render three groups, following `pages/credential/index.tsx:94-148`:

```tsx
const unconfiguredPresets = presets.filter((p) => !agents.some((a) => a.name === p.name));

// ... inside SidebarContent
{agents.length > 0 && (
	<SidebarGroup>
		<SidebarGroupLabel className="justify-between">
			<span>{t('subagent-sidebar.configured')}</span>
			<span className="text-gold font-semibold tabular-nums">{agents.length}</span>
		</SidebarGroupLabel>
		<SidebarGroupContent>
			<SidebarMenu>
				{agents.map((sa) => (
					<SidebarMenuItem key={sa.name}>
						<SidebarMenuButton isActive={editingName === sa.name} onClick={() => openEdit(sa)}>
							<SubagentIcon type={sa.name in PRESET_DISPLAY ? sa.name : sa.kind} size={18} />
							<span className="min-w-0 flex-1 truncate">{presetLabel(sa.name, t)}</span>
							<Badge variant="secondary" className="text-[10px] px-1 py-0">
								{sa.kind === 'cli'
									? t('subagent-sidebar.kindBadgeCli')
									: t('subagent-sidebar.kindBadgeOpenai')}
							</Badge>
							{sa.kind === 'cli' && sa.resumeCommand && (
								<Badge variant="outline" className="text-[10px] px-1 py-0">
									{t('subagent-sidebar.statefulBadge')}
								</Badge>
							)}
						</SidebarMenuButton>
					</SidebarMenuItem>
				))}
			</SidebarMenu>
		</SidebarGroupContent>
	</SidebarGroup>
)}
```

Then a `groupPresets` group over `unconfiguredPresets`, each row calling
`addFromPreset(preset)`, and a `groupCustom` group with exactly two rows:
`newCli` calling `openCreate('cli')` and `newOpenai` calling `openCreate('openai')`.

Widen `openCreate` to take the kind, and clear the preset flag:

```tsx
const openCreate = (kind: Kind) => {
	setForm({ ...EMPTY_FORM, kind });
	setError(null);
	setEditingName('');
	setPresetName(null);
};
```

`presetName` is the explicit preset-mode flag Task 8 introduced. Preset mode is
never inferred from the typed name: the Name placeholder is itself a preset name,
so matching on it flipped the pane mid-typing and hid the field the user was
editing. So `openCreate` and `close` clear it, `addFromPreset` sets it to the
preset's name, and `openEdit` sets it only when the row's name is a preset name.
The two `groupCustom` rows therefore reach a genuinely blank custom form.

Give the `Sidebar` the same `w-72` the credentials page uses.

- [ ] **Step 3: Reserve the preset names**

In `submit`, before the existing duplicate-name check:

```tsx
		// Scoped to creating a *custom* agent: presetName is null only then. The
		// add-from-preset flow also runs with editingName === '', and its entry
		// name is necessarily a preset name, so keying on the name alone would
		// reject every preset save. A preset owns its name, so a custom agent
		// taking it would make the preset row point at something the user did
		// not configure there.
		if (!editingName && presetName === null && isPresetName(entry.name, presets)) {
			setError(t('subagent-sidebar.presetNameReserved', { name: entry.name }));
			return;
		}
```

`!editingName` scopes this to creation, so editing a configured preset - whose
name legitimately equals a preset name - still saves.

- [ ] **Step 4: Verify**

Run: `pnpm -C frontend lint && pnpm -C frontend build`
Expected: 0 eslint errors, build succeeds.

Run: `grep -rn "addFromPreset" ui-webui/frontend/src`
Expected: no output.

- [ ] **Step 5: Manual check**

On `/subagents`:

1. All five presets appear under Presets on a config with no subagents.
2. Configure `codex`; it moves to Configured and the count reads 1.
3. Add a custom CLI agent named `hermes` and confirm the save is refused with the
   reserved-name message.
4. Delete `codex` and confirm it returns to Presets.
5. Configure all five, dispatch to each from a chat, and confirm the instance
   panel lists them.

- [ ] **Step 6: Prepare the commit**

```bash
git add ui-webui/frontend/src/pages/subagent/index.tsx \
        ui-webui/frontend/src/i18n/locales/en.json ui-webui/frontend/src/i18n/locales/zh.json
```

Message:

```
feat(ui-webui): list subagent presets on the page, not in a dropdown

The five built-ins were reachable only through an Add-from-preset dropdown that
pre-filled the same flat form. Group the list the way the credentials page does -
Configured, Presets, Custom - so every preset is one click from configured, and
reserve the preset names so a custom agent cannot take one and leave the preset
row pointing at something else.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
```

---

## Final gate

After Task 9, before asking about a merge request:

- [ ] `uv run pytest -q` - full suite green
- [ ] `make lint` - GitLab CI runs only the Python unit suite, so lint failures otherwise surface much later, at the monthly GitHub PR
- [ ] `make check-large-files`
- [ ] `pnpm -C frontend lint` and `pnpm -C frontend build`
- [ ] `git log --oneline main..HEAD` - one commit per task, every message ASCII
- [ ] `grep -rnP "[^\x00-\x7F]" $(git diff --name-only main..HEAD -- '*.py')` - no non-ASCII in Python sources
- [ ] Confirm `.hermes/`, `docs/plans/`, `docs/specs/2026-08-05-session-workspace-design.md` and `ui-webui/frontend/src/pages/setup/` are not staged. They are other work or local junk (spec section 9).

## Self-review notes

Spec coverage: 4.1 -> Task 2; 4.2 -> Task 1; 4.3 -> Task 3; 5.1/5.2 -> Task 4;
5.3 -> Task 4 step 3 (preset descriptions); 6.1 -> Task 5; 6.2 -> Task 9;
6.3 -> Task 8; 6.4 -> Tasks 6, 7, 8; the mirothinker api-key rule -> Task 8
step 1; section 7's test list -> Tasks 1-4 plus each task's verify step.

One deliberate refinement over the spec: `parse_openclaw_json` falls back to
`meta.finalAssistantVisibleText` when `payloads` is empty. The spec named only
`payloads[0].text`; both fields were present in the captured payload, and the
fallback costs three lines.

Type consistency was checked across tasks: `validate`'s return widens once, in
Task 8, and that is declared there; `isPresetName` from Task 7 is consumed in
Task 9 step 3; `Kind`, `EMPTY_FORM`, `PRESET_DISPLAY` and `presetLabel` keep the
names Task 6 and Task 7 give them. Both solar icon names used in Task 7 were
verified present in the installed icon set.
