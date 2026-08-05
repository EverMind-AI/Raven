# Unified `/subagents` Page + Stateful Third-Party CLI Sub-Agents — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the two sub-agent pages into one `/subagents` page backed by the Raven config data plane, and teach Raven's third-party CLI sub-agents to create-and-resume sessions.

**Architecture:** Raven's `ThirdPartyCliSubagentConfig` grows a `resume_command` plus id-and-transcript metadata; `CliAgentBackend` uses a JSON-file registry at `~/.raven/subagent_instances.json` to map a caller-supplied instance handle to a CLI session id, choosing the create or resume template accordingly. The web page and the chat instance monitor both switch from the AgentScope REST surface to the gateway-proxied Raven config RPCs, after which the AgentScope sub-agent client code is deleted.

**Tech Stack:** Python 3.13 / pydantic v2 / pytest (raven core, `uv run`); React 19 + Vite + Tailwind v4 + shadcn (ui-webui frontend, `pnpm`); FastAPI (ui-webui service).

**Spec:** `ui-webui/docs/specs/2026-07-27-unified-subagents-page-design.md`

## Global Constraints

- Raven core deps and test runs go through `uv` only — `uv run pytest ...`, never bare `pytest` (AGENTS.md 4, 5.4).
- `ui-webui/` is a pnpm workspace; uv/pytest rules do not apply there (`ui-webui/CLAUDE.md`).
- Do not create new test files. Extend `tests/test_subagent_third_party.py` and `tests/test_web_rpc_config.py` (AGENTS.md 5.4).
- Python lint gate for every task touching `.py`: `uv run ruff format --check <files>` and `uv run ruff check <files>` must both be clean. CI runs `make lint-python`, which includes `ruff format --check` — a hand-wrapped string literal that ruff would rejoin fails the build.
- Source comments in English only, and only where the logic is non-obvious (AGENTS.md 1).
- Commit messages: Conventional Commits, all-ASCII, `<type>(<scope>): <subject>` with header <= 100 chars, plus a `Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>` trailer (AGENTS.md 3).
- **Do not run `git commit` until the user explicitly asks** (AGENTS.md 3.4). The commit steps below are written out so the message is ready, but they stay unchecked until the user says so.
- Prettier config for the frontend is tabs, width 4, single quotes, semicolons, print width 100. Do not reformat untouched lines.
- Frontend gate for every frontend task: `pnpm -C ui-webui/frontend lint` (0 errors) and `pnpm -C ui-webui/frontend build`.
- i18n edits are targeted text edits to `src/i18n/locales/{en,zh}.json`. Never rewrite the file with a full `json.dump` — it reformats compact objects.
- Wire format for Raven config is camelCase (`Base` uses `to_camel`); both spellings are accepted on write, camelCase is what reads return.

---

## File Structure

**Raven core (created):**
- `raven/agent/subagent/backends/transcript.py` — pure transcript parsers, no I/O.
- `raven/agent/subagent/instances.py` — the handle-to-session-id registry and its file format.

**Raven core (modified):**
- `raven/config/schema.py` — `ThirdPartyCliSubagentConfig` fields + validator.
- `raven/agent/subagent/backends/cli_agent.py` — create/resume execution.
- `raven/agent/subagent/backends/base.py`, `raven_loop.py`, `openai_api.py` — protocol signature.
- `raven/agent/subagent/backends/__init__.py` — pass the new config fields through.
- `raven/agent/subagent/manager.py` — `instance` plumbing, 3-tuple agent listing.
- `raven/agent/tools/spawn.py` — conditional `instance` parameter.
- `raven/agent/subagent/presets.py` — complete preset commands.
- `raven/web_rpc/methods_config.py` — `raven.subagents.instances`.

**ui-webui service (modified):**
- `service/raven_config_routes.py` — `GET /raven/subagents/instances`.

**Frontend (modified):**
- `src/api/ravenConfig.ts` — extended types + the instances call.
- `src/hooks/useRavenSubagents.ts` (created) — shared config fetch.
- `src/pages/subagent/index.tsx` — rewritten against the Raven data plane.
- `src/components/subagent/SubagentInstanceMonitor.tsx`, `deriveInstances.ts` — registry-backed.
- `src/pages/chat/index.tsx`, `ChatViewport.tsx` — swap the data source.
- `src/App.tsx`, `src/components/layout/AppSidebar.tsx` — drop the second route.
- `src/i18n/locales/{en,zh}.json` — new keys.

**Frontend (deleted):**
- `src/pages/raven-subagents/`, `src/api/subagent.ts`, `src/hooks/useSubagents.ts`, and the `SubAgent*` types in `src/api/types.ts`.

---

## Task 1: Transcript parsers

**Files:**
- Create: `raven/agent/subagent/backends/transcript.py`
- Test: `tests/test_subagent_third_party.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `parse_codex_jsonl(stdout: str) -> tuple[str | None, str | None]` returning `(thread_id, reply)`.
  - `parse_claude_stream_json(stdout: str) -> tuple[str | None, str | None, bool]` returning `(session_id, reply, is_error)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_third_party.py`, after the CLI backend block:

```python
# --- transcript parsing --------------------------------------------------

from raven.agent.subagent.backends.transcript import parse_claude_stream_json, parse_codex_jsonl


def test_parse_codex_jsonl_extracts_thread_and_last_reply() -> None:
    stdout = "\n".join(
        [
            '{"type":"thread.started","thread_id":"th_1"}',
            "not json at all",
            '{"type":"item.completed","item":{"type":"reasoning","text":"ignored"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"final"}}',
        ]
    )
    assert parse_codex_jsonl(stdout) == ("th_1", "final")


def test_parse_codex_jsonl_empty_is_all_none() -> None:
    assert parse_codex_jsonl("") == (None, None)


def test_parse_claude_stream_json_extracts_result() -> None:
    stdout = "\n".join(
        [
            '{"type":"system","subtype":"hook_started","session_id":"sess-1"}',
            '{"type":"assistant","message":{"content":[]},"session_id":"sess-1"}',
            '{"type":"result","subtype":"success","is_error":false,'
            '"result":"OK","session_id":"sess-1"}',
        ]
    )
    assert parse_claude_stream_json(stdout) == ("sess-1", "OK", False)


def test_parse_claude_stream_json_flags_is_error() -> None:
    stdout = '{"type":"result","is_error":true,"result":"boom","session_id":"s"}'
    assert parse_claude_stream_json(stdout) == ("s", "boom", True)


def test_parse_claude_stream_json_without_result_event() -> None:
    # Session id still recoverable; no reply means the caller falls back to raw stdout.
    stdout = '{"type":"system","subtype":"init","session_id":"sess-2"}'
    assert parse_claude_stream_json(stdout) == ("sess-2", None, False)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_third_party.py -k "parse_codex or parse_claude" -v`
Expected: collection error — `ModuleNotFoundError: No module named 'raven.agent.subagent.backends.transcript'`.

- [ ] **Step 3: Write the implementation**

Create `raven/agent/subagent/backends/transcript.py`:

```python
"""Structured parsing of third-party CLI agent transcripts.

Both formats are newline-delimited JSON emitted by an agent CLI in headless
mode. Parsing is not cosmetic: a Claude Code run wraps its answer in tens of
kilobytes of hook and init events, so without extraction the reply is lost to
``max_output_chars`` truncation.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any


def _iter_json_objects(stdout: str) -> Iterator[dict[str, Any]]:
    """Yield each line that parses as a JSON object, skipping everything else."""
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(obj, dict):
            yield obj


def parse_codex_jsonl(stdout: str) -> tuple[str | None, str | None]:
    """Parse a Codex ``exec --json`` transcript.

    The session id is the ``thread_id`` of the first ``thread.started`` event;
    the reply is the ``item.text`` of the last completed ``agent_message``.
    """
    thread_id: str | None = None
    reply: str | None = None
    for obj in _iter_json_objects(stdout):
        event_type = obj.get("type")
        if event_type == "thread.started" and thread_id is None:
            candidate = obj.get("thread_id")
            if isinstance(candidate, str):
                thread_id = candidate
        elif event_type == "item.completed":
            item = obj.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message" and isinstance(item.get("text"), str):
                reply = item["text"]
    return thread_id, reply


def parse_claude_stream_json(stdout: str) -> tuple[str | None, str | None, bool]:
    """Parse a Claude Code ``--output-format stream-json --verbose`` transcript.

    Every event carries ``session_id``; the answer is ``result`` on the terminal
    ``type == "result"`` event, which also carries ``is_error``. Claude does not
    reliably exit non-zero on failure under ``-p``, so ``is_error`` is the
    authoritative failure signal.
    """
    session_id: str | None = None
    reply: str | None = None
    is_error = False
    for obj in _iter_json_objects(stdout):
        candidate = obj.get("session_id")
        if session_id is None and isinstance(candidate, str):
            session_id = candidate
        if obj.get("type") == "result":
            is_error = bool(obj.get("is_error"))
            result = obj.get("result")
            if isinstance(result, str):
                reply = result
    return session_id, reply, is_error


__all__ = ["parse_codex_jsonl", "parse_claude_stream_json"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_third_party.py -k "parse_codex or parse_claude" -v`
Expected: 5 passed.

- [ ] **Step 5: Commit** (only once the user asks)

```bash
git add raven/agent/subagent/backends/transcript.py tests/test_subagent_third_party.py
git commit -m "feat(agent): parse codex jsonl and claude stream-json transcripts

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Task 2: Schema fields and validator

**Files:**
- Modify: `raven/config/schema.py:6` (import), `raven/config/schema.py:620-637` (`ThirdPartyCliSubagentConfig`)
- Test: `tests/test_subagent_third_party.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ThirdPartyCliSubagentConfig` with `resume_command: str | None`, `id_source: Literal["provisioned", "derived"]`, `session_id_pattern: str | None`, `output_pattern: str | None`, `transcript_format: Literal["text", "codex_jsonl", "claude_stream_json"]`. Wire aliases are `resumeCommand`, `idSource`, `sessionIdPattern`, `outputPattern`, `transcriptFormat`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_third_party.py`:

```python
# --- stateful CLI schema validation --------------------------------------

from pydantic import ValidationError


def test_cli_config_stateless_rejects_agent_id() -> None:
    with pytest.raises(ValidationError):
        ThirdPartyCliSubagentConfig(name="x", command="claude -p {prompt} --session-id {agent_id}")


def test_cli_config_provisioned_requires_agent_id_in_command() -> None:
    with pytest.raises(ValidationError):
        ThirdPartyCliSubagentConfig(
            name="x",
            command="claude -p {prompt}",
            resume_command="claude -p {prompt} --resume {agent_id}",
            id_source="provisioned",
        )


def test_cli_config_derived_rejects_agent_id_in_command() -> None:
    with pytest.raises(ValidationError):
        ThirdPartyCliSubagentConfig(
            name="x",
            command="codex exec {prompt} --session {agent_id}",
            resume_command="codex exec resume {agent_id} {prompt}",
            id_source="derived",
        )


def test_cli_config_resume_requires_agent_id() -> None:
    with pytest.raises(ValidationError):
        ThirdPartyCliSubagentConfig(
            name="x",
            command="claude -p {prompt} --session-id {agent_id}",
            resume_command="claude -p {prompt}",
        )


def test_cli_config_valid_stateful_provisioned_round_trips_camel() -> None:
    cfg = ThirdPartyCliSubagentConfig(
        name="claude_code",
        command="claude -p {prompt} --session-id {agent_id}",
        resume_command="claude -p {prompt} --resume {agent_id}",
        transcript_format="claude_stream_json",
    )
    dumped = cfg.model_dump(by_alias=True)
    assert dumped["resumeCommand"] == "claude -p {prompt} --resume {agent_id}"
    assert dumped["idSource"] == "provisioned"
    assert dumped["transcriptFormat"] == "claude_stream_json"
    # camelCase is accepted on the way back in.
    assert ThirdPartyCliSubagentConfig(**dumped).resume_command == cfg.resume_command
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_third_party.py -k "cli_config" -v`
Expected: FAIL — `ThirdPartyCliSubagentConfig` rejects the unknown `resume_command` keyword, and no validation error is raised for the placeholder cases.

- [ ] **Step 3: Write the implementation**

In `raven/config/schema.py`, extend the pydantic import:

```python
from pydantic import BaseModel, ConfigDict, Field, model_validator
```

Replace the body of `ThirdPartyCliSubagentConfig`:

```python
class ThirdPartyCliSubagentConfig(Base):
    """A third-party CLI agent (claude code, codex, ...) callable as a native subagent.

    ``command`` is an argv template: a ``{prompt}`` token is replaced by the task
    text as a single argv token (injection-safe), ``{prompt_file}`` by a path to
    a file holding the prompt. With neither placeholder, the prompt is delivered
    on the child's stdin.

    Setting ``resume_command`` makes the agent stateful: a caller-supplied
    instance handle is bound to the CLI's own session id, substituted as
    ``{agent_id}``. With ``id_source="provisioned"`` raven mints the id and
    passes it on the create call; with ``"derived"`` the CLI mints it and raven
    reads it back out of the transcript.
    """

    name: str
    kind: Literal["cli"] = "cli"
    description: str = ""
    command: str
    resume_command: str | None = None
    id_source: Literal["provisioned", "derived"] = "provisioned"
    session_id_pattern: str | None = None
    output_pattern: str | None = None
    transcript_format: Literal["text", "codex_jsonl", "claude_stream_json"] = "text"
    cwd: str | None = None
    env: dict[str, str] = Field(default_factory=dict)
    timeout: int = 600
    max_output_chars: int = 30000

    @model_validator(mode="after")
    def _check_agent_id_placeholders(self) -> "ThirdPartyCliSubagentConfig":
        # An {agent_id} nothing substitutes reaches the CLI as a literal and the
        # run fails obscurely, so reject the bad combinations at write time.
        in_command = "{agent_id}" in self.command
        if not self.resume_command:
            if in_command:
                raise ValueError("command uses {agent_id} but resumeCommand is unset (agent is stateless)")
            return self
        if "{agent_id}" not in self.resume_command:
            raise ValueError("resumeCommand must contain {agent_id}")
        if self.id_source == "provisioned" and not in_command:
            raise ValueError("command must contain {agent_id} when idSource is 'provisioned'")
        if self.id_source == "derived" and in_command:
            raise ValueError("command must not contain {agent_id} when idSource is 'derived'")
        return self
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_third_party.py -v`
Expected: all pass, including the pre-existing cases (they use stateless commands with no `{agent_id}`).

- [ ] **Step 5: Commit** (only once the user asks)

```bash
git add raven/config/schema.py tests/test_subagent_third_party.py
git commit -m "feat(config): add stateful fields to third-party cli subagent schema

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Task 3: Instance registry

**Files:**
- Create: `raven/agent/subagent/instances.py`
- Test: `tests/test_subagent_third_party.py`

**Interfaces:**
- Consumes: `raven.config.loader.get_config_path`.
- Produces:
  - `InstanceRegistry(path: Path | None = None)` with `async lookup(session_key, agent, handle) -> str | None`, `async commit(session_key, agent, handle, agent_id) -> None`, `list_instances(session_key: str | None = None) -> list[dict]`, `async delete_session(session_key) -> int`.
  - `get_registry() -> InstanceRegistry` — process-wide singleton shared by the backend and the RPC handler.
  - Record shape: `{"sessionKey", "agent", "handle", "agentId", "createdAtMs", "updatedAtMs"}`.

**Lifetime:** there is no time-based expiry. A record lives as long as its chat
session does and is removed by `delete_session`, which is what bounds the file.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_third_party.py`:

```python
# --- instance registry ---------------------------------------------------

from raven.agent.subagent.instances import InstanceRegistry


async def test_registry_commit_then_lookup(tmp_path: Path) -> None:
    reg = InstanceRegistry(path=tmp_path / "inst.json")
    assert await reg.lookup("web:s1", "claude_code", "refactor") is None
    await reg.commit("web:s1", "claude_code", "refactor", "sess-abc")
    assert await reg.lookup("web:s1", "claude_code", "refactor") == "sess-abc"
    # Scoped by session and by agent, not by handle alone.
    assert await reg.lookup("web:s2", "claude_code", "refactor") is None
    assert await reg.lookup("web:s1", "codex", "refactor") is None


async def test_registry_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "inst.json"
    await InstanceRegistry(path=path).commit("web:s1", "codex", "h", "th_9")
    assert await InstanceRegistry(path=path).lookup("web:s1", "codex", "h") == "th_9"


async def test_registry_list_filters_by_session(tmp_path: Path) -> None:
    reg = InstanceRegistry(path=tmp_path / "inst.json")
    await reg.commit("web:s1", "claude_code", "a", "id-a")
    await reg.commit("web:s2", "claude_code", "b", "id-b")
    handles = {r["handle"] for r in reg.list_instances("web:s1")}
    assert handles == {"a"}
    assert len(reg.list_instances()) == 2


async def test_registry_keeps_old_records(tmp_path: Path) -> None:
    # No time-based expiry: an instance stays resumable for as long as its
    # session exists, however long ago it was last used.
    path = tmp_path / "inst.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "instances": [
                    {
                        "sessionKey": "web:s1",
                        "agent": "claude_code",
                        "handle": "old",
                        "agentId": "x",
                        "createdAtMs": 1,
                        "updatedAtMs": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    reg = InstanceRegistry(path=path)
    assert [r["handle"] for r in reg.list_instances()] == ["old"]
    assert await reg.lookup("web:s1", "claude_code", "old") == "x"


async def test_registry_delete_session_removes_only_that_session(tmp_path: Path) -> None:
    path = tmp_path / "inst.json"
    reg = InstanceRegistry(path=path)
    await reg.commit("web:s1", "claude_code", "a", "id-a")
    await reg.commit("web:s1", "codex", "b", "id-b")
    await reg.commit("web:s2", "claude_code", "c", "id-c")

    assert await reg.delete_session("web:s1") == 2
    assert [r["handle"] for r in reg.list_instances()] == ["c"]
    # Deleting an unknown session is a no-op, not an error.
    assert await reg.delete_session("web:nope") == 0
    # The removal is persisted, not just cached.
    assert [r["handle"] for r in InstanceRegistry(path=path).list_instances()] == ["c"]


async def test_registry_tolerates_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "inst.json"
    path.write_text("{ not json", encoding="utf-8")
    reg = InstanceRegistry(path=path)
    assert reg.list_instances() == []
    await reg.commit("web:s1", "codex", "h", "th_1")
    assert await reg.lookup("web:s1", "codex", "h") == "th_1"
```

Add `import json` to the test module's imports if it is not already there.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_third_party.py -k registry -v`
Expected: collection error — `ModuleNotFoundError: No module named 'raven.agent.subagent.instances'`.

- [ ] **Step 3: Write the implementation**

Create `raven/agent/subagent/instances.py`:

```python
"""Persistent instance registry for stateful third-party CLI subagents.

Maps ``(session_key, agent, handle)`` to the CLI's own session id so a later
spawn with the same handle resumes instead of starting fresh. Stored as a list
rather than a keyed object so an arbitrary handle needs no key escaping.

A record's lifetime is its chat session's: there is no time-based expiry, and
``delete_session`` (called when the session is deleted) is what bounds the file.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path
from typing import Any

from loguru import logger

_FILENAME = "subagent_instances.json"

_Key = tuple[str, str, str]


def default_registry_path() -> Path:
    from raven.config.loader import get_config_path

    return get_config_path().parent / _FILENAME


class InstanceRegistry:
    """Handle-to-session-id store backed by one JSON file."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or default_registry_path()
        self._lock = asyncio.Lock()
        self._records: dict[_Key, dict[str, Any]] | None = None

    def _load(self) -> dict[_Key, dict[str, Any]]:
        if self._records is not None:
            return self._records
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raw = {}
        records: dict[_Key, dict[str, Any]] = {}
        for rec in (raw.get("instances") if isinstance(raw, dict) else None) or []:
            if not isinstance(rec, dict):
                continue
            key = (rec.get("sessionKey"), rec.get("agent"), rec.get("handle"))
            if not all(isinstance(part, str) and part for part in key):
                continue
            records[key] = rec  # type: ignore[index]
        self._records = records
        return records

    def _flush(self) -> None:
        assert self._records is not None
        data = {"version": 1, "instances": list(self._records.values())}
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self._path)

    async def lookup(self, session_key: str, agent: str, handle: str) -> str | None:
        async with self._lock:
            rec = self._load().get((session_key, agent, handle))
        return rec.get("agentId") if rec else None

    async def commit(self, session_key: str, agent: str, handle: str, agent_id: str) -> None:
        async with self._lock:
            records = self._load()
            key = (session_key, agent, handle)
            now = int(time.time() * 1000)
            records[key] = {
                "sessionKey": session_key,
                "agent": agent,
                "handle": handle,
                "agentId": agent_id,
                "createdAtMs": (records.get(key) or {}).get("createdAtMs", now),
                "updatedAtMs": now,
            }
            try:
                self._flush()
            except OSError as e:  # noqa: BLE001 - persistence is best-effort
                logger.warning("subagent instance registry write failed: {}", e)

    async def delete_session(self, session_key: str) -> int:
        """Drop every record for a deleted chat session. Returns the count removed."""
        async with self._lock:
            records = self._load()
            doomed = [k for k in records if k[0] == session_key]
            for key in doomed:
                del records[key]
            if doomed:
                try:
                    self._flush()
                except OSError as e:  # noqa: BLE001 - persistence is best-effort
                    logger.warning("subagent instance registry write failed: {}", e)
            return len(doomed)

    def list_instances(self, session_key: str | None = None) -> list[dict[str, Any]]:
        """Most-recently-used first, optionally scoped to one session."""
        records = list(self._load().values())
        if session_key is not None:
            records = [r for r in records if r.get("sessionKey") == session_key]
        return sorted(records, key=lambda r: r.get("updatedAtMs") or 0, reverse=True)


_registry: InstanceRegistry | None = None


def get_registry() -> InstanceRegistry:
    """Process-wide registry, so the gateway's RPC reads what the backend wrote."""
    global _registry
    if _registry is None:
        _registry = InstanceRegistry()
    return _registry


__all__ = ["InstanceRegistry", "get_registry", "default_registry_path"]
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_third_party.py -k registry -v`
Expected: 6 passed.

- [ ] **Step 5: Commit** (only once the user asks)

```bash
git add raven/agent/subagent/instances.py tests/test_subagent_third_party.py
git commit -m "feat(agent): add persistent subagent instance registry

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Task 4: Stateful `CliAgentBackend`

**Files:**
- Modify: `raven/agent/subagent/backends/cli_agent.py` (whole file)
- Modify: `raven/agent/subagent/backends/__init__.py:25-33`
- Test: `tests/test_subagent_third_party.py`

**Interfaces:**
- Consumes: `parse_codex_jsonl`, `parse_claude_stream_json` (Task 1); `InstanceRegistry`, `get_registry` (Task 3); the schema fields (Task 2).
- Produces: `CliAgentBackend(name, command, resume_command=None, id_source="provisioned", session_id_pattern=None, output_pattern=None, transcript_format="text", cwd=None, env=None, timeout=600, max_output_chars=30000, registry=None)` with `is_stateful: bool` and `async run(task, *, task_id, workspace, executor, session_key=None, instance=None) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_third_party.py`:

```python
# --- stateful CLI backend ------------------------------------------------


async def test_cli_backend_substitutes_provisioned_agent_id(tmp_path: Path) -> None:
    be = CliAgentBackend(
        name="idecho",
        command="printf %s {agent_id}",
        resume_command="printf resumed-%s {agent_id}",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    first = await be.run("task", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    # Provisioned: raven minted a uuid and passed it through.
    assert first and first != "{agent_id}"
    second = await be.run("task", task_id="t2", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert second == f"resumed-{first}"


async def test_cli_backend_provisioned_id_is_a_uuid_whatever_the_handle(tmp_path: Path) -> None:
    # The handle is a registry key, never the CLI's session id. It falls back to
    # task_id (8 hex chars) when the caller omits `instance`, and claude rejects
    # a non-UUID --session-id outright, so the minted id must not derive from it.
    be = CliAgentBackend(
        name="idecho",
        command="printf %s {agent_id}",
        resume_command="printf resumed-%s {agent_id}",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    minted = await be.run("t", task_id="deadbeef", workspace=tmp_path, executor=None, session_key="s")
    assert uuid.UUID(minted)  # raises ValueError if the handle leaked through
    assert minted != "deadbeef"


async def test_cli_backend_stateless_ignores_instance(tmp_path: Path) -> None:
    be = CliAgentBackend(name="pf", command="printf %s {prompt}")
    out = await be.run("hi", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert out == "hi"


def _fixture_cmd(tmp_path: Path, name: str, lines: list[str]) -> str:
    """A `cat` command that replays a canned transcript.

    The transcript must reach the backend through a file, not inline in the
    command: `command` is parsed with shlex.split, which strips JSON's double
    quotes ('{"type":"x"}' -> '{type:x}') and splits on the newlines, so an
    inline JSONL fixture never survives to the parser.
    """
    path = tmp_path / name
    path.write_text("\n".join(lines), encoding="utf-8")
    return f"cat {path}"


async def test_cli_backend_derived_id_from_codex_jsonl(tmp_path: Path) -> None:
    cmd = _fixture_cmd(
        tmp_path,
        "codex.jsonl",
        [
            '{"type":"thread.started","thread_id":"th_7"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
        ],
    )
    be = CliAgentBackend(
        name="codexfake",
        command=cmd,
        resume_command="printf resumed-%s {agent_id}",
        id_source="derived",
        transcript_format="codex_jsonl",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    first = await be.run("task", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert first == "done"  # reply extracted, not the raw JSONL
    second = await be.run("task", task_id="t2", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert second == "resumed-th_7"  # id was derived from the transcript and reused


async def test_cli_backend_claude_stream_json_extracts_result(tmp_path: Path) -> None:
    cmd = _fixture_cmd(
        tmp_path,
        "claude.jsonl",
        [
            '{"type":"system","subtype":"init","session_id":"s1"}',
            '{"type":"result","is_error":false,"result":"the answer","session_id":"s1"}',
        ],
    )
    be = CliAgentBackend(name="claudefake", command=cmd, transcript_format="claude_stream_json")
    assert await be.run("q", task_id="t1", workspace=tmp_path, executor=None) == "the answer"


async def test_cli_backend_claude_is_error_raises_on_zero_exit(tmp_path: Path) -> None:
    # `cat` exits 0; only is_error marks the failure.
    cmd = _fixture_cmd(
        tmp_path,
        "claude-err.jsonl",
        ['{"type":"result","is_error":true,"result":"blew up","session_id":"s1"}'],
    )
    be = CliAgentBackend(name="claudefake", command=cmd, transcript_format="claude_stream_json")
    with pytest.raises(RuntimeError, match="blew up"):
        await be.run("q", task_id="t1", workspace=tmp_path, executor=None)


async def test_cli_backend_failed_create_does_not_bind_handle(tmp_path: Path) -> None:
    reg = InstanceRegistry(path=tmp_path / "inst.json")
    be = CliAgentBackend(
        name="boom",
        command="false {agent_id}",
        resume_command="printf resumed-%s {agent_id}",
        registry=reg,
    )
    with pytest.raises(RuntimeError):
        await be.run("t", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    # Deferred commit: a poisoned handle would resume a session that never existed.
    assert await reg.lookup("s", "boom", "h") is None


async def test_cli_backend_output_pattern_extracts_reply(tmp_path: Path) -> None:
    be = CliAgentBackend(name="pat", command="printf 'noise BEGIN kept END noise'", output_pattern=r"BEGIN (.*?) END")
    assert await be.run("x", task_id="t1", workspace=tmp_path, executor=None) == "kept"


async def test_cli_backend_derived_without_id_warns(tmp_path: Path) -> None:
    be = CliAgentBackend(
        name="noid",
        command="printf %s plain-text-output",
        resume_command="printf resumed-%s {agent_id}",
        id_source="derived",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    out = await be.run("t", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert out.startswith("plain-text-output")
    assert "not resumable" in out
```

- [ ] **Step 2: Run the tests to verify they fail**

Add `import uuid` to the test module's imports if it is not already there.

Run: `uv run pytest tests/test_subagent_third_party.py -k "cli_backend" -v`
Expected: FAIL — `CliAgentBackend.__init__` got an unexpected keyword argument `resume_command`.

- [ ] **Step 3: Write the implementation**

Replace `raven/agent/subagent/backends/cli_agent.py` with:

```python
"""Third-party CLI agent backend: shell out to an external agent (claude code,
codex, ...) as a spawned sub-agent (req5).

Runs on the host rather than through the sandbox executor - these CLIs need the
host's auth, config, and PATH. Prompt delivery: a ``{prompt}`` token in the
command is substituted as a single argv token (injection-safe), ``{prompt_file}``
as a path to a file holding the prompt; with neither, the prompt goes on the
child's stdin.

With ``resume_command`` set the agent is stateful: an instance handle is bound
to the CLI's own session id in :mod:`raven.agent.subagent.instances`, so a later
spawn with the same handle resumes that session.
"""

from __future__ import annotations

import asyncio
import os
import re
import shlex
import tempfile
import uuid
from pathlib import Path
from typing import Any

from loguru import logger

from raven.agent.subagent.backends.transcript import parse_claude_stream_json, parse_codex_jsonl
from raven.agent.subagent.instances import InstanceRegistry, get_registry


class CliAgentBackend:
    def __init__(
        self,
        *,
        name: str,
        command: str,
        resume_command: str | None = None,
        id_source: str = "provisioned",
        session_id_pattern: str | None = None,
        output_pattern: str | None = None,
        transcript_format: str = "text",
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 600,
        max_output_chars: int = 30000,
        registry: InstanceRegistry | None = None,
    ) -> None:
        self.name = name
        self.command = command
        self.resume_command = resume_command
        self.id_source = id_source
        self.transcript_format = transcript_format
        self.cwd = cwd
        self.env = env or {}
        self.timeout = timeout
        self.max_output_chars = max_output_chars
        self._session_id_re = re.compile(session_id_pattern) if session_id_pattern else None
        self._output_re = re.compile(output_pattern) if output_pattern else None
        self._registry = registry or get_registry()

    @property
    def is_stateful(self) -> bool:
        return bool(self.resume_command)

    def _build_argv(self, template: str, prompt: str, prompt_file: str, agent_id: str | None) -> tuple[list[str], bool]:
        argv: list[str] = []
        used_placeholder = False
        for tok in shlex.split(template):
            if agent_id is not None:
                tok = tok.replace("{agent_id}", agent_id)
            if "{prompt}" in tok:
                argv.append(tok.replace("{prompt}", prompt))
                used_placeholder = True
            elif "{prompt_file}" in tok:
                argv.append(tok.replace("{prompt_file}", prompt_file))
                used_placeholder = True
            else:
                argv.append(tok)
        return argv, used_placeholder

    async def _exec(self, template: str, task: str, task_id: str, cwd: str, agent_id: str | None) -> tuple[str, str]:
        fd, prompt_path = tempfile.mkstemp(prefix=f"raven_subagent_{task_id}_", suffix=".prompt.txt")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(task)
            argv, used_placeholder = self._build_argv(template, task, prompt_path, agent_id)
            env = {**os.environ, **self.env}
            logger.info("Subagent [{}] CLI agent {!r}: {}", task_id, self.name, argv[:1])
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=None if used_placeholder else asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            stdin_bytes = None if used_placeholder else task.encode("utf-8")
            try:
                out, err = await asyncio.wait_for(proc.communicate(input=stdin_bytes), timeout=self.timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                raise RuntimeError(f"CLI agent {self.name!r} timed out after {self.timeout}s") from None
            stdout = out.decode("utf-8", "replace")
            stderr = err.decode("utf-8", "replace")
            if proc.returncode != 0:
                tail = (stdout + "\n" + stderr).strip()[-2000:]
                raise RuntimeError(f"CLI agent {self.name!r} exited {proc.returncode}: {tail}")
            return stdout, stderr
        finally:
            try:
                os.unlink(prompt_path)
            except OSError:
                pass

    async def run(
        self,
        task: str,
        *,
        task_id: str,
        workspace: Path,
        executor: Any,
        session_key: str | None = None,
        instance: str | None = None,
    ) -> str:
        skey = session_key or "default"
        handle = instance or task_id
        template = self.command
        agent_id: str | None = None
        created = False

        if self.is_stateful:
            existing = await self._registry.lookup(skey, self.name, handle)
            if existing is not None:
                agent_id = existing
                template = self.resume_command or self.command
            else:
                created = True
                # Minted independently of `handle`: a CLI constrains its session
                # id (claude rejects a non-UUID) while a handle is free-form.
                agent_id = None if self.id_source == "derived" else str(uuid.uuid4())

        stdout, stderr = await self._exec(template, task, task_id, self.cwd or str(workspace), agent_id)

        jsonl_id: str | None = None
        jsonl_reply: str | None = None
        if self.transcript_format == "codex_jsonl":
            jsonl_id, jsonl_reply = parse_codex_jsonl(stdout)
        elif self.transcript_format == "claude_stream_json":
            jsonl_id, jsonl_reply, is_error = parse_claude_stream_json(stdout)
            # Claude does not reliably exit non-zero under -p, so the exit code
            # check above is not enough on its own.
            if is_error:
                raise RuntimeError(f"CLI agent {self.name!r} reported an error: {(jsonl_reply or stdout).strip()[-2000:]}")

        if created and self.id_source == "derived":
            if jsonl_id is not None:
                agent_id = jsonl_id
            elif self._session_id_re is not None and (m := self._session_id_re.search(stdout)) is not None:
                agent_id = m.group(1)

        # Deferred commit: binding a handle after a failed create would resume a
        # session that never existed.
        if created and agent_id is not None:
            await self._registry.commit(skey, self.name, handle, agent_id)

        combined = f"{stdout}\n{stderr}" if stderr else stdout
        if jsonl_reply is not None:
            output = jsonl_reply.strip()
        elif self._output_re is not None and (m := self._output_re.search(stdout)) is not None:
            output = m.group(1).strip()
        else:
            output = combined.strip()

        # Appended last so reply extraction cannot swallow it.
        if created and self.id_source == "derived" and agent_id is None:
            output += (
                "\n\n[raven] Warning: no session id could be extracted from the create "
                "output, so this instance is not resumable. Use a new handle to recreate."
            )
        return output[: self.max_output_chars]
```

Then update `build_third_party_backend` in `raven/agent/subagent/backends/__init__.py` to pass the new fields:

```python
    if kind == "cli":
        return CliAgentBackend(
            name=cfg.name,
            command=cfg.command,
            resume_command=cfg.resume_command,
            id_source=cfg.id_source,
            session_id_pattern=cfg.session_id_pattern,
            output_pattern=cfg.output_pattern,
            transcript_format=cfg.transcript_format,
            cwd=cfg.cwd,
            env=dict(cfg.env),
            timeout=cfg.timeout,
            max_output_chars=cfg.max_output_chars,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_third_party.py -v`
Expected: all pass, including the five pre-existing `cli_backend` cases.

- [ ] **Step 5: Commit** (only once the user asks)

```bash
git add raven/agent/subagent/backends/cli_agent.py raven/agent/subagent/backends/__init__.py tests/test_subagent_third_party.py
git commit -m "feat(agent): support create-and-resume for third-party cli subagents

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Task 5: Plumb the instance handle to the model

**Files:**
- Modify: `raven/agent/subagent/backends/base.py:19`
- Modify: `raven/agent/subagent/backends/raven_loop.py:80`, `openai_api.py:42`
- Modify: `raven/agent/subagent/manager.py:92-116`, `118-171`, `196-216`
- Modify: `raven/agent/tools/spawn.py:48-107`
- Test: `tests/test_subagent_third_party.py`

**Interfaces:**
- Consumes: `CliAgentBackend.is_stateful` (Task 4).
- Produces:
  - `SubagentBackend.run(task, *, task_id, workspace, executor, session_key=None, instance=None) -> str`.
  - `SubagentManager.spawn(task, label=None, origin_channel="cli", origin_chat_id="direct", session_key=None, agent=None, instance=None) -> str`.
  - `SubagentManager.list_third_party_agents() -> list[tuple[str, str, bool]]` — `(name, description, stateful)`.
  - `SpawnTool.execute(task, label=None, agent=None, instance=None, **kwargs) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_third_party.py`, and update the existing
`test_manager_resolves_backends` unpacking from `for n, _ in` to
`for n, _, _ in`:

```python
def test_manager_lists_stateful_flag(tmp_path: Path) -> None:
    stateless = ThirdPartyCliSubagentConfig(name="codex", command="codex exec {prompt}")
    stateful = ThirdPartyCliSubagentConfig(
        name="claude_code",
        command="claude -p {prompt} --session-id {agent_id}",
        resume_command="claude -p {prompt} --resume {agent_id}",
    )
    mgr = _mgr(tmp_path, [stateless, stateful])
    assert mgr.list_third_party_agents() == [("codex", "", False), ("claude_code", "", True)]


def test_spawn_tool_exposes_instance_param_only_when_stateful(tmp_path: Path) -> None:
    stateless = ThirdPartyCliSubagentConfig(name="codex", command="codex exec {prompt}")
    assert "instance" not in SpawnTool(manager=_mgr(tmp_path, [stateless])).parameters["properties"]

    stateful = ThirdPartyCliSubagentConfig(
        name="claude_code",
        command="claude -p {prompt} --session-id {agent_id}",
        resume_command="claude -p {prompt} --resume {agent_id}",
    )
    props = SpawnTool(manager=_mgr(tmp_path, [stateful])).parameters["properties"]
    assert "instance" in props
    assert props["instance"]["type"] == "string"


async def test_manager_forwards_instance_to_backend(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    class _Recorder:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None):
            seen["session_key"] = session_key
            seen["instance"] = instance
            return "ok"

    mgr = _mgr(tmp_path, [])
    mgr._backends["rec"] = _Recorder()
    mgr.set_submit(lambda req: None)
    await mgr.spawn("t", session_key="web:s1", agent="rec", instance="refactor-auth")
    for task in list(mgr._running_tasks.values()):
        await task
    assert seen == {"session_key": "web:s1", "instance": "refactor-auth"}
```

Add `from typing import Any` to the test module's imports if it is not already there.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_third_party.py -k "stateful_flag or instance_param or forwards_instance" -v`
Expected: FAIL — the 2-tuple comparison mismatches, `instance` is absent from the tool schema, and `spawn()` rejects the `instance` keyword.

- [ ] **Step 3: Write the implementation**

`backends/base.py` — widen the protocol:

```python
    async def run(
        self,
        task: str,
        *,
        task_id: str,
        workspace: Path,
        executor: Any,
        session_key: str | None = None,
        instance: str | None = None,
    ) -> str: ...
```

`backends/raven_loop.py:80` and `backends/openai_api.py:42` — add the same two
keyword-only parameters to each `run` signature and ignore them (a raven-loop
or HTTP sub-agent has no CLI session to resume).

`manager.py` — in `set_third_party_subagents`, record the stateful flag:

```python
                meta.append((name, getattr(cfg, "description", "") or "", bool(getattr(cfg, "resume_command", None))))
```

and widen the annotations `self._third_party_meta: list[tuple[str, str, bool]]`
and `def list_third_party_agents(self) -> list[tuple[str, str, bool]]:`.

In `spawn`, add the parameter and carry it in `origin`:

```python
    async def spawn(
        self,
        task: str,
        label: str | None = None,
        origin_channel: str = "cli",
        origin_chat_id: str = "direct",
        session_key: str | None = None,
        agent: str | None = None,
        instance: str | None = None,
    ) -> str:
```

```python
        origin = {
            "channel": origin_channel,
            "chat_id": origin_chat_id,
            "session_key": quota_key,
            "agent": agent,
            "instance": instance,
        }
```

In `_run_subagent_inner`, forward both:

```python
            final_result = await backend.run(
                task,
                task_id=task_id,
                workspace=self.workspace,
                executor=executor,
                session_key=origin.get("session_key"),
                instance=origin.get("instance"),
            )
```

`agent/tools/spawn.py` — the listing is now a 3-tuple, so update both readers
and add the conditional parameter:

```python
    def _third_party_agents(self) -> list[tuple[str, str, bool]]:
        lister = getattr(self._manager, "list_third_party_agents", None)
        return lister() if callable(lister) else []
```

```python
        agents = self._third_party_agents()
        if agents:
            listing = "; ".join(f"{name} ({desc})" if desc else name for name, desc, _ in agents)
```

```python
        agents = self._third_party_agents()
        if agents:
            props["agent"] = {
                "type": "string",
                "enum": [name for name, _, _ in agents],
                "description": (
                    "Optional: delegate to this specialized third-party agent instead of "
                    "a default Raven subagent."
                ),
            }
        if any(stateful for _, _, stateful in agents):
            props["instance"] = {
                "type": "string",
                "description": (
                    "Optional: a short semantic handle (e.g. 'refactor-auth') naming a "
                    "conversation with a stateful third-party agent. Reuse the same handle "
                    "to continue that session; omit it to start a fresh one."
                ),
            }
```

```python
    async def execute(
        self,
        task: str,
        label: str | None = None,
        agent: str | None = None,
        instance: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Spawn a subagent to execute the given task."""
        org = self._cur()
        return await self._manager.spawn(
            task=task,
            label=label,
            origin_channel=org.channel,
            origin_chat_id=org.chat_id,
            session_key=org.session_key,
            agent=agent,
            instance=instance,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_third_party.py -v`
Expected: all pass.

- [ ] **Step 5: Run the wider subagent suite for regressions**

Run: `uv run pytest tests/ -k "subagent or spawn or sentinel" -x -q`
Expected: pass. `sentinel/executor/spawn.py` and `action_executor.py` call
`spawn()` without `instance`, which the default covers.

- [ ] **Step 6: Commit** (only once the user asks)

```bash
git add raven/agent/subagent/ raven/agent/tools/spawn.py tests/test_subagent_third_party.py
git commit -m "feat(agent): expose instance handle on spawn for stateful subagents

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Task 6: Complete presets

**Files:**
- Modify: `raven/agent/subagent/presets.py` (whole file)
- Test: `tests/test_subagent_third_party.py`

**Interfaces:**
- Consumes: the schema from Task 2.
- Produces: `THIRD_PARTY_SUBAGENT_PRESETS` keyed `claude_code`, `codex`, `mirothinker`, all camelCase on the wire and all valid against `ThirdPartyCliSubagentConfig` / `ThirdPartyOpenAISubagentConfig`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_subagent_third_party.py`:

```python
def test_presets_are_valid_and_complete() -> None:
    presets = {p["name"]: p for p in third_party_subagent_presets()}
    assert set(presets) == {"claude_code", "codex", "mirothinker"}
    # Every preset validates against the schema, so "Add from preset" can never
    # produce a config the write path would reject.
    SubagentsConfig(third_party=list(presets.values()))

    claude = presets["claude_code"]
    assert "--output-format stream-json" in claude["command"]
    assert "--verbose" in claude["command"]
    assert "--session-id {agent_id}" in claude["command"]
    assert "--resume {agent_id}" in claude["resumeCommand"]
    assert claude["transcriptFormat"] == "claude_stream_json"
    assert claude["idSource"] == "provisioned"

    codex = presets["codex"]
    assert "-s workspace-write" in codex["command"]
    assert "--json" in codex["command"]
    assert codex["transcriptFormat"] == "codex_jsonl"
    assert codex["idSource"] == "derived"
    assert "{agent_id}" not in codex["command"]

    assert presets["mirothinker"]["baseUrl"] == "https://api.miromind.ai/v1"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_subagent_third_party.py -k presets_are_valid -v`
Expected: FAIL — `KeyError: 'resumeCommand'` (the current presets are the minimal `claude -p {prompt}` form).

- [ ] **Step 3: Write the implementation**

Replace the `THIRD_PARTY_SUBAGENT_PRESETS` dict and the module docstring in
`raven/agent/subagent/presets.py`:

```python
"""Built-in third-party subagent presets (req5).

Copy any of these into ``subagents.thirdParty`` in ``~/.raven/config.json`` (or
add them from the web UI) so the main agent can dispatch to that agent via
``spawn(agent=<name>)``. They are templates: adjust ``command`` / ``baseUrl`` /
``model`` to your install.

- ``claude_code`` - Claude Code CLI headless, stateful. ``--output-format
  stream-json --verbose`` is required: the plain text output buries the answer
  in tens of kilobytes of hook and init noise, which ``maxOutputChars`` would
  truncate away.
- ``codex`` - OpenAI Codex CLI headless, stateful. The session id and the reply
  are both read out of the ``exec --json`` JSONL stream.
- ``mirothinker`` - MiroMind deep-research over an OpenAI-compatible endpoint
  (set ``apiKey``).
"""

_CLAUDE_TAIL = "--output-format stream-json --verbose"
_CODEX_HEAD = "codex -a never exec -s workspace-write -c 'sandbox_workspace_write.network_access=true' --json"

THIRD_PARTY_SUBAGENT_PRESETS: dict[str, dict[str, Any]] = {
    "claude_code": {
        "name": "claude_code",
        "kind": "cli",
        "description": (
            "Claude Code CLI - strong general coding / agent tasks. Stateful: a new "
            "instance handle starts a fresh session, reusing a handle resumes it."
        ),
        "command": f"claude -p {{prompt}} --permission-mode auto --session-id {{agent_id}} {_CLAUDE_TAIL}",
        "resumeCommand": f"claude -p {{prompt}} --permission-mode auto --resume {{agent_id}} {_CLAUDE_TAIL}",
        "idSource": "provisioned",
        "transcriptFormat": "claude_stream_json",
        "timeout": 600,
    },
    "codex": {
        "name": "codex",
        "kind": "cli",
        "description": (
            "OpenAI Codex CLI - coding tasks. Stateful: the session id and the reply "
            "are parsed from the `exec --json` JSONL stream and the id is reused on resume."
        ),
        "command": f"{_CODEX_HEAD} {{prompt}}",
        "resumeCommand": f"{_CODEX_HEAD} resume {{agent_id}} {{prompt}}",
        "idSource": "derived",
        "transcriptFormat": "codex_jsonl",
        "timeout": 600,
    },
    "mirothinker": {
        "name": "mirothinker",
        "kind": "openai",
        "description": (
            "MiroMind deep-research (OpenAI-compatible HTTP). Returns a sourced report "
            "with citations. Requires an apiKey."
        ),
        "baseUrl": "https://api.miromind.ai/v1",
        "model": "mirothinker-1-7-deepresearch",
        "apiKey": "",
        "timeout": 1200,
    },
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_third_party.py tests/test_web_rpc_config.py -v`
Expected: all pass. `test_presets_list_set_and_hotapply` in
`test_web_rpc_config.py` only asserts the preset names, so it is unaffected.

- [ ] **Step 5: Smoke-test the claude preset end to end**

Run, from a scratch directory:

```bash
claude -p 'reply with exactly: OK' --permission-mode auto \
  --session-id 11111111-2222-3333-4444-555555555555 \
  --output-format stream-json --verbose | tail -1
```

Expected: a single JSON object with `"type":"result"`, `"is_error":false`, and
`"result":"OK"`. This is the shape `parse_claude_stream_json` depends on; if the
installed CLI has changed it, fix the parser before continuing.

- [ ] **Step 6: Commit** (only once the user asks)

```bash
git add raven/agent/subagent/presets.py tests/test_subagent_third_party.py
git commit -m "feat(agent): ship complete stateful presets for claude code and codex

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Task 7: Instances RPC and service route

**Files:**
- Modify: `raven/web_rpc/methods_config.py:42-63`
- Modify: `ui-webui/service/raven_config_routes.py:20-34`
- Test: `tests/test_web_rpc_config.py`

**Interfaces:**
- Consumes: `raven.agent.subagent.instances.get_registry` (Task 3).
- Produces:
  - RPC `raven.subagents.instances`, optional param `session_key`, result `{"instances": [...]}`.
  - RPC `raven.subagents.instances.delete`, required param `session_key`, result `{"removed": <count>}`.
  - HTTP `GET` and `DELETE` on `/raven/subagents/instances?session_key=<key>` on the ui-webui service.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_web_rpc_config.py`:

```python
async def test_subagent_instances_list_and_filter(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.agent.subagent import instances as instances_mod

    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    await reg.commit("web:s1", "claude_code", "refactor", "sess-a")
    await reg.commit("web:s2", "codex", "review", "th-b")
    monkeypatch.setattr(instances_mod, "_registry", reg)

    d = Dispatcher()
    register_config_methods(d)

    resp = await _dispatch(d, "raven.subagents.instances", {})
    assert {r["handle"] for r in resp["result"]["instances"]} == {"refactor", "review"}

    resp = await _dispatch(d, "raven.subagents.instances", {"session_key": "web:s1"})
    only = resp["result"]["instances"]
    assert len(only) == 1
    assert only[0]["agent"] == "claude_code"
    assert only[0]["agentId"] == "sess-a"


async def test_subagent_instances_delete_by_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.agent.subagent import instances as instances_mod

    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    await reg.commit("web:s1", "claude_code", "refactor", "sess-a")
    await reg.commit("web:s2", "codex", "review", "th-b")
    monkeypatch.setattr(instances_mod, "_registry", reg)

    d = Dispatcher()
    register_config_methods(d)

    resp = await _dispatch(d, "raven.subagents.instances.delete", {"session_key": "web:s1"})
    assert resp["result"]["removed"] == 1

    resp = await _dispatch(d, "raven.subagents.instances", {})
    assert [r["handle"] for r in resp["result"]["instances"]] == ["review"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_web_rpc_config.py -k instances -v`
Expected: FAIL — the dispatcher returns a method-not-found error for
`raven.subagents.instances`.

- [ ] **Step 3: Write the implementation**

In `raven/web_rpc/methods_config.py`, extend the import block inside
`register_config_methods` and register the method next to the other three:

```python
    from raven.agent.subagent.instances import get_registry
```

```python
    async def _instances(params: dict) -> dict:
        return {"instances": get_registry().list_instances(params.get("session_key"))}

    async def _instances_delete(params: dict) -> dict:
        return {"removed": await get_registry().delete_session(params.get("session_key", ""))}

    dispatcher.register("raven.subagents.instances", _instances)
    dispatcher.register("raven.subagents.instances.delete", _instances_delete)
```

Update the docstring bullet to
``` - ``raven.subagents.{list,set,presets,instances}`` — third-party sub-agents; ``set`` ```
and add a line noting that ``instances.delete`` drops a deleted chat session's
instance records (their lifetime is the session's, with no time-based expiry).

In `ui-webui/service/raven_config_routes.py`, add both routes alongside the
existing sub-agent routes:

```python
    @router.get("/subagents/instances")
    async def list_subagent_instances(session_key: str | None = None) -> dict:
        client = _client()
        return await client.call("raven.subagents.instances", {"session_key": session_key})

    @router.delete("/subagents/instances")
    async def delete_subagent_instances(session_key: str) -> dict:
        client = _client()
        return await client.call("raven.subagents.instances.delete", {"session_key": session_key})
```

Match the surrounding style for how `_client()` is obtained in that file.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_rpc_config.py -v`
Expected: all pass.

- [ ] **Step 5: Commit** (only once the user asks)

```bash
git add raven/web_rpc/methods_config.py ui-webui/service/raven_config_routes.py tests/test_web_rpc_config.py
git commit -m "feat(web_rpc): expose stateful subagent instances over the config rpc

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Task 8: Frontend API types and shared hook

**Files:**
- Modify: `ui-webui/frontend/src/api/ravenConfig.ts:1-70`
- Create: `ui-webui/frontend/src/hooks/useRavenSubagents.ts`

**Interfaces:**
- Consumes: the RPC from Task 7 and the schema from Task 2.
- Produces:
  - `RavenCliSubagent` with `resumeCommand?`, `idSource?`, `sessionIdPattern?`, `outputPattern?`, `transcriptFormat?`, `env?`.
  - `RavenOpenAISubagent` with `temperature?`, `maxTokens?`.
  - `RavenSubagentInstance { sessionKey; agent; handle; agentId; createdAtMs; updatedAtMs }`.
  - `ravenConfigApi.listSubagentInstances(sessionKey?)`.
  - `useRavenSubagents()` returning `{ agents, presets, loading, reload, save }` where `save(next: RavenThirdPartySubagent[]): Promise<void>`.

- [ ] **Step 1: Extend the types**

In `src/api/ravenConfig.ts`, replace the two sub-agent interfaces and add the
instance type:

```ts
export interface RavenCliSubagent {
	name: string;
	kind: 'cli';
	description?: string;
	command: string;
	resumeCommand?: string | null;
	idSource?: 'provisioned' | 'derived';
	sessionIdPattern?: string | null;
	outputPattern?: string | null;
	transcriptFormat?: 'text' | 'codex_jsonl' | 'claude_stream_json';
	cwd?: string | null;
	env?: Record<string, string>;
	timeout?: number;
}

export interface RavenOpenAISubagent {
	name: string;
	kind: 'openai';
	description?: string;
	baseUrl: string;
	model: string;
	apiKey?: string;
	systemPrompt?: string | null;
	temperature?: number | null;
	maxTokens?: number | null;
	timeout?: number;
}

export interface RavenSubagentInstance {
	sessionKey: string;
	agent: string;
	handle: string;
	agentId: string;
	createdAtMs: number;
	updatedAtMs: number;
}
```

Add the call to `ravenConfigApi`, next to `presets`:

```ts
	listSubagentInstances: (sessionKey?: string) =>
		client.get<{ instances: RavenSubagentInstance[] }>(
			sessionKey
				? `/raven/subagents/instances?session_key=${encodeURIComponent(sessionKey)}`
				: '/raven/subagents/instances',
		),

	deleteSubagentInstances: (sessionKey: string) =>
		client.delete<{ removed: number }>(
			`/raven/subagents/instances?session_key=${encodeURIComponent(sessionKey)}`,
		),
```

Check how `client.delete` passes a body versus a query string in
`src/api/client.ts` before settling on the URL form above — `ravenConfigApi.removeCron`
uses a path parameter, so a query string may need the same treatment.

- [ ] **Step 2: Write the shared hook**

Create `src/hooks/useRavenSubagents.ts`:

```ts
import { useCallback, useEffect, useState } from 'react';

import { ravenConfigApi } from '@/api';
import type { RavenThirdPartySubagent } from '@/api';

/**
 * Raven's third-party sub-agent config. Writes replace the whole list: the
 * gateway validates it, persists it to ~/.raven, and hot-applies it to the
 * running AgentLoop.
 */
export function useRavenSubagents() {
	const [agents, setAgents] = useState<RavenThirdPartySubagent[]>([]);
	const [presets, setPresets] = useState<RavenThirdPartySubagent[]>([]);
	const [loading, setLoading] = useState(true);

	const reload = useCallback(async () => {
		setLoading(true);
		try {
			const [a, p] = await Promise.all([
				ravenConfigApi.listSubagents(),
				ravenConfigApi.presets(),
			]);
			setAgents(a.agents ?? []);
			setPresets(p.presets ?? []);
		} finally {
			setLoading(false);
		}
	}, []);

	useEffect(() => {
		void reload();
	}, [reload]);

	const save = useCallback(async (next: RavenThirdPartySubagent[]) => {
		await ravenConfigApi.setSubagents(next);
		setAgents(next);
	}, []);

	return { agents, presets, loading, reload, save };
}
```

- [ ] **Step 3: Run the frontend gate**

Run: `pnpm -C ui-webui/frontend lint && pnpm -C ui-webui/frontend build`
Expected: 0 eslint errors, build succeeds. The hook is not yet imported anywhere, which is fine.

- [ ] **Step 4: Commit** (only once the user asks)

```bash
git add ui-webui/frontend/src/api/ravenConfig.ts ui-webui/frontend/src/hooks/useRavenSubagents.ts
git commit -m "feat(ui-webui): type raven subagent config and instances on the client

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Task 9: Rewrite the `/subagents` page

**Files:**
- Modify: `ui-webui/frontend/src/pages/subagent/index.tsx` (whole file)
- Modify: `ui-webui/frontend/src/i18n/locales/en.json:463-497`, `zh.json` (same block)

**Interfaces:**
- Consumes: `useRavenSubagents` (Task 8), `RavenCliSubagent` / `RavenOpenAISubagent` (Task 8).
- Produces: `SubAgentsPage` with no external props, still the default export of `@/pages/subagent`.

- [ ] **Step 1: Add the i18n keys**

Targeted edits only. In `en.json`, inside `subagent-sidebar`, remove
`credentialLabel`, `credentialEmpty`, `credentialAdd`, `credNameLabel`,
`credBaseUrlLabel`, `credApiKeyLabel`, `credCreateUse`, and `statefulLabel`;
add:

```json
		"baseUrlLabel": "Base URL",
		"apiKeyLabel": "API key",
		"apiKeyKeepHint": "Leave blank to keep the stored key.",
		"idSourceLabel": "Session id source",
		"idSourceProvisioned": "provisioned (raven mints the id)",
		"idSourceDerived": "derived (the CLI mints it, raven reads it back)",
		"transcriptFormatLabel": "Transcript format",
		"transcriptText": "text (plain stdout)",
		"transcriptCodex": "codex_jsonl (codex exec --json)",
		"transcriptClaude": "claude_stream_json (claude --output-format stream-json)",
		"sessionIdPatternLabel": "Session id regex (derived + text only)",
		"outputPatternLabel": "Output regex (optional)",
		"patternHint": "Group 1 of the first match is used.",
		"kindBadgeCli": "cli",
		"kindBadgeOpenai": "openai",
		"saved": "Sub-agents saved and applied to the running gateway."
```

Mirror the same keys in `zh.json` with Chinese copy. Also update
`subagent-sidebar.subtitle` to "Third-party agents Raven can dispatch to." /
the Chinese equivalent, and `resumeHint` to describe the new rule: "A resume
command makes this agent stateful. It must contain {agent_id}; with a
provisioned id source the create command must contain it too."

- [ ] **Step 2: Rewrite the page**

Replace `src/pages/subagent/index.tsx`. Keep the existing two-pane shell
verbatim (`Sidebar` + `SidebarHeader` with `em-kicker` / `em-accent`, the preset
`DropdownMenu`, the `Skeleton` loading state, the `Empty` states, the right-hand
`main` with the delete and close buttons, and `DeleteDialog`). Change only the
data layer and the form fields.

Form state and conversion:

```tsx
type Kind = 'cli' | 'openai';

interface FormState {
	kind: Kind;
	name: string;
	description: string;
	command: string;
	resumeCommand: string;
	idSource: 'provisioned' | 'derived';
	sessionIdPattern: string;
	outputPattern: string;
	transcriptFormat: 'text' | 'codex_jsonl' | 'claude_stream_json';
	cwd: string;
	env: string;
	baseUrl: string;
	model: string;
	apiKey: string;
	systemPrompt: string;
	temperature: string;
	maxTokens: string;
	timeout: string;
}

const EMPTY_FORM: FormState = {
	kind: 'cli',
	name: '',
	description: '',
	command: '',
	resumeCommand: '',
	idSource: 'provisioned',
	sessionIdPattern: '',
	outputPattern: '',
	transcriptFormat: 'text',
	cwd: '',
	env: '',
	baseUrl: '',
	model: '',
	apiKey: '',
	systemPrompt: '',
	temperature: '',
	maxTokens: '',
	timeout: '600',
};

function envToText(env: Record<string, string> | undefined): string {
	return Object.entries(env ?? {})
		.map(([k, v]) => `${k}=${v}`)
		.join('\n');
}

function textToEnv(text: string): Record<string, string> {
	const out: Record<string, string> = {};
	for (const line of text.split('\n')) {
		const trimmed = line.trim();
		const idx = trimmed.indexOf('=');
		if (!trimmed || idx <= 0) continue;
		out[trimmed.slice(0, idx).trim()] = trimmed.slice(idx + 1).trim();
	}
	return out;
}

function toForm(a: RavenThirdPartySubagent): FormState {
	if (a.kind === 'openai') {
		return {
			...EMPTY_FORM,
			kind: 'openai',
			name: a.name,
			description: a.description ?? '',
			baseUrl: a.baseUrl,
			model: a.model,
			// Never surface a stored key; blank means "keep it" on save.
			apiKey: '',
			systemPrompt: a.systemPrompt ?? '',
			temperature: a.temperature == null ? '' : String(a.temperature),
			maxTokens: a.maxTokens == null ? '' : String(a.maxTokens),
			timeout: String(a.timeout ?? 1200),
		};
	}
	return {
		...EMPTY_FORM,
		kind: 'cli',
		name: a.name,
		description: a.description ?? '',
		command: a.command,
		resumeCommand: a.resumeCommand ?? '',
		idSource: a.idSource ?? 'provisioned',
		sessionIdPattern: a.sessionIdPattern ?? '',
		outputPattern: a.outputPattern ?? '',
		transcriptFormat: a.transcriptFormat ?? 'text',
		cwd: a.cwd ?? '',
		env: envToText(a.env),
		timeout: String(a.timeout ?? 600),
	};
}

/** `previous` is the stored entry being edited, used to keep an unchanged api key. */
function toEntry(f: FormState, previous?: RavenThirdPartySubagent): RavenThirdPartySubagent {
	const description = f.description.trim() || undefined;
	const timeout = f.timeout.trim() ? Number(f.timeout) : undefined;
	if (f.kind === 'openai') {
		const stored = previous?.kind === 'openai' ? previous.apiKey : undefined;
		return {
			name: f.name.trim(),
			kind: 'openai',
			description,
			baseUrl: f.baseUrl.trim(),
			model: f.model.trim(),
			apiKey: f.apiKey.trim() || stored || '',
			systemPrompt: f.systemPrompt.trim() || null,
			temperature: f.temperature.trim() === '' ? null : Number(f.temperature),
			maxTokens: f.maxTokens.trim() === '' ? null : Number(f.maxTokens),
			timeout,
		};
	}
	return {
		name: f.name.trim(),
		kind: 'cli',
		description,
		command: f.command.trim(),
		resumeCommand: f.resumeCommand.trim() || null,
		idSource: f.idSource,
		sessionIdPattern: f.sessionIdPattern.trim() || null,
		outputPattern: f.outputPattern.trim() || null,
		transcriptFormat: f.transcriptFormat,
		cwd: f.cwd.trim() || null,
		env: textToEnv(f.env),
		timeout,
	};
}
```

Validation mirroring the Task 2 validator, used to gate the save button and to
colour the resume hint:

```tsx
	const stateful = form.resumeCommand.trim().length > 0;
	const derived = form.idSource === 'derived';
	const idInCommand = form.command.includes('{agent_id}');
	const cliOk = stateful
		? form.resumeCommand.includes('{agent_id}') && (derived ? !idInCommand : idInCommand)
		: !idInCommand;
	const openaiOk = !!form.baseUrl.trim() && !!form.model.trim();
	const canSubmit =
		!!form.name.trim() && (form.kind === 'openai' ? openaiOk : !!form.command.trim() && cliOk);
```

Whole-list writes keyed by name. `editingName` is `null` (nothing selected),
`''` (creating), or the stored name:

```tsx
	const { agents, presets, loading, save } = useRavenSubagents();
	const [editingName, setEditingName] = useState<string | null>(null);
	const [form, setForm] = useState<FormState>(EMPTY_FORM);
	const [submitting, setSubmitting] = useState(false);
	const [error, setError] = useState<string | null>(null);

	const submit = async () => {
		const previous = agents.find((a) => a.name === editingName);
		const entry = toEntry(form, previous);
		// Name is the primary key, so a rename drops the old row.
		const next = [...agents.filter((a) => a.name !== editingName && a.name !== entry.name), entry];
		setSubmitting(true);
		setError(null);
		try {
			await save(next);
			setEditingName(null);
			toast.success(t('subagent-sidebar.saved'));
		} catch (err) {
			setError(String((err as Error)?.message ?? err));
		} finally {
			setSubmitting(false);
		}
	};

	const remove = async (name: string) => {
		await save(agents.filter((a) => a.name !== name));
		if (editingName === name) setEditingName(null);
	};
```

Presets fill the form rather than saving directly, so the user can supply an
`apiKey` or adjust a command before it is written:

```tsx
	const addFromPreset = (preset: RavenThirdPartySubagent) => {
		setForm(toForm(preset));
		setError(null);
		setEditingName('');
	};
```

The dropdown item label is `preset.name` — Raven's presets RPC returns bare
config dicts with no separate label.

The sidebar row keeps the stateful badge and gains a kind badge:

```tsx
	{(sa.kind === 'cli' ? sa.resumeCommand : false) && (
		<Badge variant="outline" className="text-[10px] px-1 py-0">
			{t('subagent-sidebar.statefulBadge')}
		</Badge>
	)}
```

CLI form fields, in order: name, description, kind (only when creating),
command, resumeCommand + hint, idSource select, transcriptFormat select,
sessionIdPattern (rendered only when `derived && transcriptFormat === 'text'`),
outputPattern, cwd, env, timeout. OpenAI form fields: name, description, kind
(only when creating), baseUrl, model, apiKey (`type="password"`, with
`apiKeyKeepHint` shown when editing), systemPrompt, temperature, maxTokens,
timeout.

- [ ] **Step 3: Run the frontend gate**

Run: `pnpm -C ui-webui/frontend lint && pnpm -C ui-webui/frontend build`
Expected: 0 eslint errors, build succeeds.

- [ ] **Step 4: Verify against a running stack**

Run `./start_webapp.sh` from the repo root with `RAVEN_GATEWAY=1`, open
`http://localhost:5173/subagents`, and check:

1. "Add from preset" -> `claude_code` fills the form with the full command
   including `--output-format stream-json --verbose`; Save succeeds.
2. `cat ~/.raven/config.json` shows the entry under `subagents.thirdParty` with
   `resumeCommand` / `idSource` / `transcriptFormat`.
3. Editing the `mirothinker` entry without retyping the key and saving leaves
   `apiKey` unchanged in the config file.
4. Typing a command with `{agent_id}` but no resume command disables Save.

- [ ] **Step 5: Commit** (only once the user asks)

```bash
git add ui-webui/frontend/src/pages/subagent/index.tsx ui-webui/frontend/src/i18n/locales/
git commit -m "feat(ui-webui): back the subagents page with raven config

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Task 10: Registry-backed instance monitor

**Files:**
- Modify: `ui-webui/frontend/src/components/subagent/SubagentInstanceMonitor.tsx` (whole file)
- Modify: `ui-webui/frontend/src/components/subagent/deriveInstances.ts:173-178`
- Modify: `ui-webui/frontend/src/pages/chat/index.tsx:47,107`, `ChatViewport.tsx:79,143,360,424,437`
- Modify: `ui-webui/frontend/src/hooks/useSessions.ts:65-73`
- Modify: `ui-webui/frontend/src/i18n/locales/{en,zh}.json` (`subagent-monitor.transport`)

**Interfaces:**
- Consumes: `useRavenSubagents` and `ravenConfigApi.listSubagentInstances` (Task 8).
- Produces: `SubagentInstanceMonitor({ msgs, sessionId, subagents }: { msgs: Msg[]; sessionId: string; subagents: RavenThirdPartySubagent[] })`; `transportOf(prototype: string, subagents: RavenThirdPartySubagent[]): string`.

- [ ] **Step 1: Update the transports i18n**

In `en.json`, replace the `subagent-monitor.transport` object (the current
labels conflate transport with product name):

```json
		"transport": { "cli": "CLI", "claude": "Claude Code", "codex": "Codex", "openai": "OpenAI API" }
```

Mirror in `zh.json`.

- [ ] **Step 2: Repoint `transportOf` at the Raven config**

In `deriveInstances.ts`, swap the import `SubAgentView` for
`RavenThirdPartySubagent` and replace the function:

```ts
/**
 * Derive a prototype's transport from its Raven config, used as a fallback
 * when no live overlay entry is available yet.
 */
export function transportOf(prototype: string, subagents: RavenThirdPartySubagent[]): string {
	const cfg = subagents.find((s) => s.name === prototype);
	if (!cfg) return 'cli';
	if (cfg.kind === 'openai') return 'openai';
	if (cfg.transcriptFormat === 'codex_jsonl') return 'codex';
	if (cfg.transcriptFormat === 'claude_stream_json') return 'claude';
	return 'cli';
}
```

`deriveInstances()` itself is unchanged — it reads the transcript, not the
config.

- [ ] **Step 3: Rewrite the monitor's data source**

In `SubagentInstanceMonitor.tsx`, take `sessionId`, fetch the registry, and
merge it with the transcript-derived exchanges:

```tsx
export function SubagentInstanceMonitor({ msgs, sessionId, subagents }: SubagentInstanceMonitorProps) {
	const { t } = useTranslation();
	const { instances: liveInstances } = useSubagentInstances();
	const { dagRuns } = useDagRuns();
	const [registry, setRegistry] = useState<RavenSubagentInstance[]>([]);

	// The gateway agent scopes a spawn to `web:<session id>`; see
	// service/raven_gateway_agent.py.
	const sessionKey = sessionId ? `web:${sessionId}` : '';

	// A registry row only appears when a spawn completes, which always coincides
	// with a new message — so the message count is a sufficient refresh trigger.
	const msgCount = msgs.length;
	useEffect(() => {
		if (!sessionKey) return;
		let cancelled = false;
		ravenConfigApi
			.listSubagentInstances(sessionKey)
			.then((res) => {
				if (!cancelled) setRegistry(res.instances ?? []);
			})
			.catch(() => {
				if (!cancelled) setRegistry([]);
			});
		return () => {
			cancelled = true;
		};
	}, [sessionKey, msgCount]);

	const statefulNames = useMemo(
		() => new Set(subagents.filter((s) => s.kind === 'cli' && s.resumeCommand).map((s) => s.name)),
		[subagents],
	);
	const derived = useMemo(
		() => deriveInstances(msgs, statefulNames, dagRuns),
		[msgs, statefulNames, dagRuns],
	);

	// The registry is the authoritative instance list (it survives a reload);
	// the transcript supplies each instance's exchange history.
	const instances = useMemo(() => {
		const byHandle = new Map(derived.map((i) => [i.handle, i]));
		const merged = registry.map((r) => ({
			handle: r.handle,
			prototype: r.agent,
			agentId: r.agentId,
			exchanges: byHandle.get(r.handle)?.exchanges ?? [],
		}));
		const known = new Set(merged.map((i) => i.handle));
		return [...merged, ...derived.filter((i) => !known.has(i.handle))];
	}, [registry, derived]);
```

The rest of the component (the button list, the transport and status badges,
the selected instance's exchange panel) is unchanged.

Add the imports: `useEffect`, `useState` from react; `ravenConfigApi` and the
types `RavenSubagentInstance` / `RavenThirdPartySubagent` from `@/api`.

- [ ] **Step 4: Swap the chat page's data source**

In `src/pages/chat/index.tsx`, replace the `useSubagents` import and call with
`useRavenSubagents`, destructuring `agents`:

```tsx
	const { agents: subagents } = useRavenSubagents();
```

In `ChatViewport.tsx`: change the prop type on line 79 to
`subagents: RavenThirdPartySubagent[]`, pass `sessionId` into the monitor on
line 360, and change line 437 to `new Set(subagents.map((s) => s.name))`.

```tsx
				content: <SubagentInstanceMonitor msgs={msgs} sessionId={sessionId} subagents={subagents} />,
```

- [ ] **Step 5: Clean up the registry when a session is deleted**

A registry record's lifetime is its session's, so the single delete choke point
gets the cleanup. In `src/hooks/useSessions.ts`, extend `remove`:

```ts
	/** Deletes a session, its Raven sub-agent instances, and refreshes the list. */
	const remove = useCallback(
		async (sessionId: string) => {
			if (!agentId) throw new Error('No agent selected');
			await sessionApi.delete(sessionId, agentId);
			// Best-effort: a gateway that is down must not fail the delete the
			// user asked for. The orphan it leaves is session-scoped, so it can
			// never collide with a live session or surface in the UI.
			try {
				await ravenConfigApi.deleteSubagentInstances(`web:${sessionId}`);
			} catch {
				/* ignore */
			}
			await refetch();
		},
		[agentId, refetch],
	);
```

Add the `ravenConfigApi` import. Note the `web:` prefix must match
`raven_gateway_agent.py:231`; if that ever changes, both sides move together.

- [ ] **Step 6: Run the frontend gate**

Run: `pnpm -C ui-webui/frontend lint && pnpm -C ui-webui/frontend build`
Expected: 0 eslint errors, build succeeds.

- [ ] **Step 7: Verify against a running stack**

With the stack up and `claude_code` configured, ask the agent in the web chat
to delegate something to Claude Code with a named instance. Then check:

1. The Instances dock lists the handle with the `Claude Code` transport badge.
2. `cat ~/.raven/subagent_instances.json` contains a record whose `sessionKey`
   matches `web:<the chat session id>`, and whose `agentId` is a UUID rather
   than the handle.
3. Reloading the page keeps the instance listed (with zero exchanges until the
   transcript reloads), which is the behavior the registry buys.
4. Deleting that chat session removes its records from
   `~/.raven/subagent_instances.json`, and leaves another session's records
   untouched.

- [ ] **Step 8: Commit** (only once the user asks)

```bash
git add ui-webui/frontend/src/components/subagent/ ui-webui/frontend/src/pages/chat/ ui-webui/frontend/src/hooks/useSessions.ts ui-webui/frontend/src/i18n/locales/
git commit -m "feat(ui-webui): drive the instance monitor from the raven registry

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Task 11: Delete the legacy AgentScope sub-agent surface

**Files:**
- Delete: `ui-webui/frontend/src/pages/raven-subagents/index.tsx` (and the directory)
- Delete: `ui-webui/frontend/src/api/subagent.ts`, `ui-webui/frontend/src/hooks/useSubagents.ts`
- Modify: `ui-webui/frontend/src/App.tsx:17,56`
- Modify: `ui-webui/frontend/src/components/layout/AppSidebar.tsx:132-142`
- Modify: `ui-webui/frontend/src/api/index.ts:10`, `ui-webui/frontend/src/api/types.ts`

**Interfaces:**
- Consumes: Tasks 9 and 10, which removed the last readers.
- Produces: nothing new; `/raven-subagents` stops resolving and `subagentApi` no longer exists.

- [ ] **Step 1: Confirm there are no readers left**

Run:

```bash
cd ui-webui/frontend && grep -rn "subagentApi\|useSubagents\|SubAgentView\|SubAgentPreset\|raven-subagents" src
```

Expected: no matches outside the files being deleted in this task. Any other hit
must be migrated before deleting — do not force it through.

- [ ] **Step 2: Delete the files and the route**

```bash
cd ui-webui/frontend
rm -r src/pages/raven-subagents src/api/subagent.ts src/hooks/useSubagents.ts
```

In `src/App.tsx`, remove the `RavenSubAgentsPage` import (line 17) and its route
(line 56). In `src/components/layout/AppSidebar.tsx`, remove the whole
`SidebarMenuItem` whose tooltip is `'Raven sub-agents'` (lines 132-142). In
`src/api/index.ts`, remove the `subagentApi` re-export. In `src/api/types.ts`,
remove `SubAgentView`, `SubAgentPreset`, `SubAgentListResponse`,
`SubAgentPresetsResponse`, `CreateSubAgentRequest`, `CreateSubAgentResponse`,
`UpdateSubAgentRequest`, and any type used only by those.

- [ ] **Step 3: Run the frontend gate**

Run: `pnpm -C ui-webui/frontend lint && pnpm -C ui-webui/frontend build`
Expected: 0 eslint errors, build succeeds. A `tsc` error here means a reader was
missed in Step 1 — fix it rather than re-adding the deleted file.

- [ ] **Step 4: Run the full backend suite**

Run: `uv run pytest tests/ -q`
Expected: pass. This is the last checkpoint before the feature is complete.

- [ ] **Step 5: Update the ui-webui CLAUDE.md pointer**

`ui-webui/CLAUDE.md` currently says
`src/pages/raven-channels|raven-cron|raven-subagents` are the P4 config pages.
Change that list to `src/pages/raven-channels|raven-cron|subagent`, and note
that `/subagents` is now Raven-backed rather than an AgentScope page.

- [ ] **Step 6: Commit** (only once the user asks)

```bash
git add -A ui-webui/frontend/src ui-webui/CLAUDE.md
git commit -m "refactor(ui-webui): drop the agentscope subagent client and duplicate page

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Self-review notes

**Spec coverage:** 5.1 -> Task 2; 5.2 -> Task 1; 5.3 -> Task 3; 5.3.1 -> Tasks 3
and 4; 5.4 -> Task 4; 5.5 -> Task 5; 5.6 -> Task 6; 5.7 -> Task 7; 6.1 -> Task 9;
6.2 -> Task 10; 6.2.1 -> Task 10 step 5; 6.3 -> Tasks 9 and 11; section 7
testing -> the test steps in every task.

**Type consistency:** `resumeCommand` / `idSource` / `sessionIdPattern` /
`outputPattern` / `transcriptFormat` are spelled identically in the schema
aliases (Task 2), the presets (Task 6), the TS types (Task 8), the page
(Task 9), and `transportOf` (Task 10). `list_third_party_agents` returns the
3-tuple in Task 5 and is consumed as a 3-tuple in the same task.

**Open question for execution time:** the working tree is on
`feat/gateway-web-channel` with uncommitted reskin changes. Per AGENTS.md 2.2 the
base branch must be confirmed with the user before cutting a branch for this
work.
