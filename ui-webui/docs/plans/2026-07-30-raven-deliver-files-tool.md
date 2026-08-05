# Raven `deliver_files` Tool Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Raven main agent a `deliver_files` tool that hands output files to the user in the web UI, with one-click download, available on the `web` channel only.

**Architecture:** The tool validates paths with the same resolver `read_file`/`write_file` use, registers each file in a persisted token store, and returns a text summary to the model. The file manifest travels out of band of the model's text on a new optional `ToolEvent.metadata` field, through the existing `tool.complete` wire event, into `ToolResultEndEvent.metadata`, and so onto the persisted `ToolResultBlock` the web UI already renders. Bytes never enter the manifest: they stream over two new HTTP routes on the gateway's existing aiohttp app, keyed by an opaque token, relayed to the browser by a stream-proxy route on the web service.

**Tech Stack:** Python 3.12+ (`uv` / `pytest`, `asyncio_mode = "auto"`), aiohttp (gateway HTTP + service HTTP client), FastAPI (service), React 19 + TypeScript + Vite + i18next (frontend, `pnpm`).

**Spec:** `ui-webui/docs/specs/2026-07-30-raven-deliver-files-tool-design.md`

## Global Constraints

- **Read `AGENTS.md` (repo root) before the first edit.** Its rules override anything here that conflicts.
- **Do not commit until the user says so** (`AGENTS.md` §3.4). The commit step at the end of each task is the *intended* commit boundary, not authorization. Ask once, then honor the answer for the run.
- **Confirm the branch base with the user before cutting a branch** (`AGENTS.md` §2.2); default `main` only if they decline to choose. Branch name: `feat/deliver_files_tool`.
- **Commit messages: Conventional Commits, all English, ASCII-only** (`AGENTS.md` §3.1, §3.1.1). No em-dash, no curly quotes, no `§`. Trailer `Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>`.
- **Code comments: English, and only where the logic is non-obvious** (`AGENTS.md` §1). Do not annotate edits (`# new`, `# changed`).
- **Python packages: `uv` only** (`AGENTS.md` §4). No new dependency is needed by this plan; `aiohttp>=3.13.4` is already a runtime dep.
- **Run tests as `uv run pytest ...`, never bare `pytest`.**
- **Test file naming per `AGENTS.md` §5.** Do not create a new file when an existing one covers the module: add to `tests/test_cli_tui_commands.py` and `tests/test_cli_gateway_commands.py` rather than making phase-suffixed files.
- **Frontend gate (no JS unit-test runner):** `pnpm -C frontend lint` must report **0 errors** and `pnpm -C frontend build` must pass, run from `ui-webui/`.
- **Frontend formatting is Prettier with tabs, width 4, single quotes, semicolons, print width 100.** Match it; a stray reformat is diff noise.
- **i18n JSON: targeted text edits only.** Never rewrite `en.json` / `zh.json` wholesale (it reformats compact objects).
- **Manifest invariant: no file bytes, ever.** The manifest carries paths, names, sizes, media types and tokens only.
- **Token invariant: `secrets.token_urlsafe(32)`, never derived from the path.** A path-derived token would be guessable from a guessable input and would dissolve the download trust boundary.

---

## File Structure

**New — Raven core**

| Path | Responsibility |
|---|---|
| `raven/agent/tools/_deliverables.py` | `DeliverableRecord` + `DeliverableStore`: the persisted token registry. No knowledge of tools, HTTP, or the agent loop. |
| `raven/agent/tools/deliver.py` | `DeliverFilesTool`: channel gate, path validation, manifest construction. Depends on the store and on `filesystem._resolve_path`. |
| `raven/web_rpc/files.py` | `resolve_download()` (pure decision logic) + `add_files_routes()` (aiohttp adapter). Depends on the store only. |

**New — tests**

`tests/test_deliverable_store.py`, `tests/test_deliver_files_tool.py`, `tests/test_web_rpc_files_download.py`, `tests/integration/test_file_delivery_smoke.py`

**Modified**

| Path | Change |
|---|---|
| `raven/agent/tools/base.py` | `take_metadata()` hook, default `None` |
| `raven/spine/events.py` | `ToolEvent.metadata` field |
| `raven/agent/loop/main.py` | `deliverables` ctor param, registration guard, `_set_tool_context` whitelist entry, metadata pickup, `ToolEvent(metadata=...)` |
| `raven/tui_rpc/spine.py` | serialize `metadata` on `tool.complete` |
| `raven/web_rpc/server.py` | accept the store, register the two routes |
| `raven/cli/gateway_commands.py` | build the store when the web channel is on; pass it to `AgentLoop` and to `WebSocketRpcServer` |
| `raven/config/paths.py` | `get_deliverables_path()` |
| `CONTEXT.md` | define **Deliverable** and **Delivery Manifest** (`AGENTS.md` §6) |
| `ui-webui/service/raven_gateway_agent.py` | merge wire `metadata` into `ToolResultEndEvent.metadata`; export the gateway HTTP base |
| `ui-webui/service/raven_config_routes.py` | the two stream-proxy routes |
| `ui-webui/frontend/src/components/delivery/deriveDeliverables.ts` | tool name, manifest key, `token` field, drop `parseDeliveryContext` |
| `ui-webui/frontend/src/api/files.ts` | anchor download + HEAD pre-check, token-based archive |
| `ui-webui/frontend/src/components/chat/tool-renderers/index.tsx` | computed registry key |
| `ui-webui/frontend/src/components/delivery/DeliveredFileRow.tsx` | new download helper |
| `ui-webui/frontend/src/i18n/locales/{en,zh}.json` | one new failure string |

---

## Task 1: The persisted token store

**Files:**
- Create: `raven/agent/tools/_deliverables.py`
- Create: `tests/test_deliverable_store.py`
- Modify: `raven/config/paths.py` (append after `get_sentinel_dir`)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `DeliverableRecord(token: str, path: str, name: str, media_type: str, size: int, conversation: str, created_at: str)` — frozen dataclass.
  - `DeliverableStore(path: Path)`; `register(*, path: str, name: str, media_type: str, size: int, conversation: str) -> DeliverableRecord`; `get(token: str) -> DeliverableRecord | None`; `drop(token: str) -> None`; `prune_missing() -> int`.
  - `raven.config.paths.get_deliverables_path() -> Path`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_deliverable_store.py`:

```python
"""Tests for the persisted deliverable token store."""

from __future__ import annotations

import json

from raven.agent.tools._deliverables import DeliverableStore


def _make_file(tmp_path, name="report.pdf", body=b"hello"):
    fp = tmp_path / name
    fp.write_bytes(body)
    return fp


def test_register_returns_random_token_not_derived_from_path(tmp_path) -> None:
    """The token is the download capability, so it must not be derivable from
    the path (paths are guessable; a derived token would be too)."""
    store = DeliverableStore(tmp_path / "deliverables.json")
    a = _make_file(tmp_path, "a.txt")
    b = _make_file(tmp_path, "b.txt")

    rec_a = store.register(path=str(a), name="a.txt", media_type="text/plain", size=5, conversation="web:s1")
    rec_b = store.register(path=str(b), name="b.txt", media_type="text/plain", size=5, conversation="web:s1")

    assert rec_a.token != rec_b.token
    assert len(rec_a.token) >= 32
    assert "a.txt" not in rec_a.token


def test_register_same_path_same_conversation_reuses_token_and_refreshes_size(tmp_path) -> None:
    """Re-delivering a file must not mint a second token (the panel de-dups by
    path), but the size shown must track the file as it is now."""
    store = DeliverableStore(tmp_path / "deliverables.json")
    fp = _make_file(tmp_path, "a.txt", b"12345")

    first = store.register(path=str(fp), name="a.txt", media_type="text/plain", size=5, conversation="web:s1")
    second = store.register(path=str(fp), name="a.txt", media_type="text/plain", size=99, conversation="web:s1")

    assert second.token == first.token
    assert second.size == 99
    assert store.get(first.token).size == 99


def test_register_same_path_different_conversation_mints_new_token(tmp_path) -> None:
    store = DeliverableStore(tmp_path / "deliverables.json")
    fp = _make_file(tmp_path, "a.txt")

    one = store.register(path=str(fp), name="a.txt", media_type="text/plain", size=5, conversation="web:s1")
    two = store.register(path=str(fp), name="a.txt", media_type="text/plain", size=5, conversation="web:s2")

    assert one.token != two.token


def test_token_resolves_after_reload(tmp_path) -> None:
    """The core promise of persisting the registry: a gateway restart must not
    break download buttons the (persisted) manifest still shows."""
    path = tmp_path / "deliverables.json"
    fp = _make_file(tmp_path, "a.txt")
    token = DeliverableStore(path).register(
        path=str(fp), name="a.txt", media_type="text/plain", size=5, conversation="web:s1"
    ).token

    reloaded = DeliverableStore(path)

    rec = reloaded.get(token)
    assert rec is not None
    assert rec.path == str(fp)
    assert rec.name == "a.txt"


def test_startup_prunes_entries_whose_file_is_gone(tmp_path) -> None:
    path = tmp_path / "deliverables.json"
    fp = _make_file(tmp_path, "a.txt")
    store = DeliverableStore(path)
    token = store.register(
        path=str(fp), name="a.txt", media_type="text/plain", size=5, conversation="web:s1"
    ).token
    fp.unlink()

    reloaded = DeliverableStore(path)

    assert reloaded.get(token) is None
    assert json.loads(path.read_text(encoding="utf-8")) == {}


def test_unreadable_store_starts_empty_instead_of_raising(tmp_path) -> None:
    path = tmp_path / "deliverables.json"
    path.write_text("{ not json", encoding="utf-8")

    store = DeliverableStore(path)

    assert store.get("anything") is None


def test_drop_removes_and_persists(tmp_path) -> None:
    path = tmp_path / "deliverables.json"
    fp = _make_file(tmp_path, "a.txt")
    store = DeliverableStore(path)
    token = store.register(
        path=str(fp), name="a.txt", media_type="text/plain", size=5, conversation="web:s1"
    ).token

    store.drop(token)

    assert store.get(token) is None
    assert DeliverableStore(path).get(token) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_deliverable_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'raven.agent.tools._deliverables'`

- [ ] **Step 3: Implement the store**

Create `raven/agent/tools/_deliverables.py`:

```python
"""Persisted registry of files delivered to the user.

The token is the download capability: the gateway HTTP routes serve a file only
when a token resolves here, so a path never travels in a URL and there is
nothing for a caller to traverse.
"""

from __future__ import annotations

import json
import os
import secrets
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger


@dataclass(frozen=True)
class DeliverableRecord:
    """One delivered file, addressable by its opaque token."""

    token: str
    path: str
    name: str
    media_type: str
    size: int
    conversation: str
    created_at: str


class DeliverableStore:
    """Token -> delivered file, persisted as one JSON object."""

    def __init__(self, path: Path) -> None:
        self._path = Path(path)
        self._by_token: dict[str, DeliverableRecord] = {}
        self._load()
        self.prune_missing()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("deliverables: unreadable store at {}; starting empty", self._path)
            return
        if not isinstance(raw, dict):
            return
        for token, fields in raw.items():
            if not isinstance(fields, dict):
                continue
            try:
                self._by_token[token] = DeliverableRecord(token=token, **fields)
            except TypeError:
                logger.warning("deliverables: dropping malformed entry {}", token)

    def _save(self) -> None:
        payload = {
            token: {k: v for k, v in asdict(rec).items() if k != "token"}
            for token, rec in self._by_token.items()
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    def prune_missing(self) -> int:
        """Drop entries whose file is gone. Returns how many were dropped."""
        gone = [token for token, rec in self._by_token.items() if not os.path.isfile(rec.path)]
        for token in gone:
            del self._by_token[token]
        if gone:
            self._save()
        return len(gone)

    def register(
        self, *, path: str, name: str, media_type: str, size: int, conversation: str
    ) -> DeliverableRecord:
        """Register a delivered file, reusing the token if this conversation
        already delivered this path (the size is refreshed, so the UI never
        shows a stale figure for a file that changed)."""
        for token, rec in self._by_token.items():
            if rec.conversation == conversation and rec.path == path:
                refreshed = replace(rec, name=name, media_type=media_type, size=size)
                self._by_token[token] = refreshed
                self._save()
                return refreshed
        rec = DeliverableRecord(
            token=secrets.token_urlsafe(32),
            path=path,
            name=name,
            media_type=media_type,
            size=size,
            conversation=conversation,
            created_at=datetime.now(UTC).isoformat(),
        )
        self._by_token[rec.token] = rec
        self._save()
        return rec

    def get(self, token: str) -> DeliverableRecord | None:
        return self._by_token.get(token)

    def drop(self, token: str) -> None:
        if self._by_token.pop(token, None) is not None:
            self._save()
```

- [ ] **Step 4: Add the path helper**

In `raven/config/paths.py`, after `get_sentinel_dir()`:

```python
def get_deliverables_path() -> Path:
    """Return the delivered-files token registry path."""
    return get_runtime_subdir("deliverables") / "deliverables.json"
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_deliverable_store.py -q`
Expected: PASS (7 tests)

- [ ] **Step 6: Commit**

```bash
git add raven/agent/tools/_deliverables.py raven/config/paths.py tests/test_deliverable_store.py
git commit -m "feat(agent): add persisted deliverable token store"
```

---

## Task 2: The manifest channel (hook, event field, wire)

**Files:**
- Modify: `raven/agent/tools/base.py` (after `blocking_for`, around line 35)
- Modify: `raven/spine/events.py:64-76` (the `ToolEvent` dataclass)
- Modify: `raven/agent/loop/main.py:1727-1735` (tool-event payload) and `:2455-2462` (`ToolEvent` construction)
- Modify: `raven/tui_rpc/spine.py:151-161` (the `tool.complete` payload)
- Create: `tests/test_tool_metadata_channel.py`

**Interfaces:**
- Consumes: nothing from Task 1.
- Produces:
  - `Tool.take_metadata() -> dict[str, Any] | None` — default `None`; called by the loop once per tool call, after `execute()`.
  - `ToolEvent.metadata: dict[str, Any] | None = None`.
  - `tool.complete` wire payload gains a `"metadata"` key (`None` for every tool that does not override the hook).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tool_metadata_channel.py`:

```python
"""Tests for the optional structured-metadata channel from a tool to the turn
stream: Tool.take_metadata -> ToolEvent.metadata -> the tool.complete wire event."""

from __future__ import annotations

from typing import Any

from raven.agent.tools.base import Tool
from raven.spine.events import ToolEvent, ToolPhase


class _Bare(Tool):
    @property
    def name(self) -> str:
        return "bare"

    @property
    def description(self) -> str:
        return "no metadata"

    @property
    def parameters(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


def test_take_metadata_defaults_to_none() -> None:
    """Existing tools must keep working untouched, so the hook is opt-in."""
    assert _Bare().take_metadata() is None


def test_tool_event_metadata_defaults_to_none() -> None:
    """Additive field: every existing ToolEvent construction site keeps working."""
    event = ToolEvent(phase=ToolPhase.COMPLETE, tool_call_id="t1")
    assert event.metadata is None


def test_tool_event_carries_metadata() -> None:
    event = ToolEvent(phase=ToolPhase.COMPLETE, tool_call_id="t1", metadata={"raven_delivery": {"files": []}})
    assert event.metadata == {"raven_delivery": {"files": []}}
```

Append to `tests/test_tui_rpc_tool_events.py`:

```python
async def test_tool_complete_wire_payload_carries_metadata() -> None:
    """The manifest must survive the one tool.complete serialization site, which
    the TUI and the web channel share."""
    from raven.spine.events import ToolEvent, ToolPhase

    emitted: list[dict] = []

    class _Emitter:
        async def emit(self, cid: str, frame: dict) -> None:
            emitted.append(frame)

    from raven.tui_rpc.spine import TuiOutlet

    outlet = TuiOutlet(emitter=_Emitter())
    await outlet.deliver(
        ToolEvent(
            phase=ToolPhase.COMPLETE,
            tool_call_id="t1",
            result_preview="done",
            metadata={"raven_delivery": {"files": [], "invalid": []}},
        )
    )

    complete = [f for f in emitted if f["type"] == "tool.complete"]
    assert complete, f"expected a tool.complete frame, got {emitted}"
    assert complete[0]["payload"]["metadata"] == {"raven_delivery": {"files": [], "invalid": []}}
```

> **Note for the implementer:** read `tests/test_tui_rpc_tool_events.py` and `raven/tui_rpc/spine.py` first and adapt the outlet construction above to the real `TuiOutlet` signature and delivery entry point (the constructor takes the emitter plus a channel; the method that consumes a spine event may be named differently). Keep the assertion exactly as written — that is the contract under test.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_tool_metadata_channel.py tests/test_tui_rpc_tool_events.py -q`
Expected: FAIL — `AttributeError: 'Tool' object has no attribute 'take_metadata'` and `TypeError: ToolEvent.__init__() got an unexpected keyword argument 'metadata'`

- [ ] **Step 3: Add the hook to the tool base class**

In `raven/agent/tools/base.py`, after `blocking_for`:

```python
    def take_metadata(self) -> dict[str, Any] | None:
        """Structured payload for this call's turn-stream event, consumed once.

        Opt-in: ``execute`` returns only a string, so a tool whose result also
        has to reach a UI (rather than the model) hands it back here and the loop
        attaches it to the emitted ToolEvent.
        """
        return None
```

- [ ] **Step 4: Add the event field**

In `raven/spine/events.py`, inside `ToolEvent`, after `blocking: bool = False`:

```python
    # COMPLETE only: opt-in structured payload from Tool.take_metadata (e.g. a
    # deliver_files manifest). Outlets that do not understand a key ignore it.
    metadata: dict[str, Any] | None = None
```

- [ ] **Step 5: Pick the metadata up in the loop**

In `raven/agent/loop/main.py`, replace the `emit_tool_event` block at `:1727-1735`:

```python
                    if emit_tool_event:
                        tool_metadata = None
                        if (executed := self.tools.get(tool_call.name)) is not None:
                            tool_metadata = executed.take_metadata()
                        await on_tool_event(
                            "complete",
                            {
                                "tool_call_id": tool_call.id,
                                "result_preview": preview,
                                "truncated": len(result_str) > 200,
                                "metadata": tool_metadata,
                            },
                        )
```

And in the `on_tool` handler inside `run_turn` (`:2454-2462`), pass it through:

```python
            else:
                await emit(
                    ToolEvent(
                        phase=ToolPhase.COMPLETE,
                        tool_call_id=info["tool_call_id"],
                        result_preview=info["result_preview"],
                        truncated=info["truncated"],
                        metadata=info.get("metadata"),
                    )
                )
```

- [ ] **Step 6: Serialize it on the wire**

In `raven/tui_rpc/spine.py`, in the `tool.complete` payload at `:151-161`:

```python
                        "payload": {
                            "tool_call_id": out.tool_call_id,
                            "result_preview": out.result_preview,
                            "truncated": out.truncated,
                            "metadata": out.metadata,
                        },
```

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_tool_metadata_channel.py tests/test_tui_rpc_tool_events.py tests/test_spine_events.py -q`
Expected: PASS

- [ ] **Step 8: Guard against regressions elsewhere on the turn stream**

Run: `uv run pytest tests/test_tui_rpc_turn_send.py tests/test_tui_rpc_spine.py tests/test_cli_repl_spine.py tests/test_spine_scheduler_lane.py -q`
Expected: PASS (the field is additive with a default; any failure here means a construction site was broken)

- [ ] **Step 9: Commit**

```bash
git add raven/agent/tools/base.py raven/spine/events.py raven/agent/loop/main.py raven/tui_rpc/spine.py tests/test_tool_metadata_channel.py tests/test_tui_rpc_tool_events.py
git commit -m "feat(spine): carry opt-in tool metadata on the turn stream"
```

---

## Task 3: The `deliver_files` tool

**Files:**
- Create: `raven/agent/tools/deliver.py`
- Create: `tests/test_deliver_files_tool.py`

**Interfaces:**
- Consumes: `DeliverableStore` and `DeliverableRecord` (Task 1); `Tool.take_metadata` (Task 2); `raven.agent.tools.filesystem._resolve_path(path: str, workspace: Path | None, allowed_dir: Path | None) -> Path`.
- Produces: `DeliverFilesTool(store: DeliverableStore, *, workspace: Path | None = None, allowed_dir: Path | None = None)`, with `name == "deliver_files"` and `set_context(channel: str, chat_id: str, session_key: str) -> None`.

**Why the manifest handoff is a dict keyed by session key, not a ContextVar:**
`registry.execute` runs the tool inside `asyncio.wait_for` (`raven/agent/tools/registry.py:80`). On CPython 3.13 that happens to preserve context writes back to the caller, but that is an implementation detail of the 3.12+ `wait_for` rewrite — if it ever reverts to wrapping in `ensure_future`, a ContextVar written inside `execute()` would silently stop reaching the loop and the delivery box would silently stop rendering. Reads *down* into a child context are guaranteed, so `set_context` uses a `ContextVar` (written by the loop's task, read inside `execute`), and the manifest comes *back* through `self._pending[session_key]`, which `take_metadata()` pops using the same session key it reads from its own context. Concurrent turns have different session keys, so they cannot collide.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_deliver_files_tool.py`:

```python
"""Tests for the deliver_files tool: the web-channel gate, path validation, and
the manifest it hands back to the turn stream."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent.tools._deliverables import DeliverableStore
from raven.agent.tools.deliver import DeliverFilesTool


@pytest.fixture
def tool(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    store = DeliverableStore(tmp_path / "deliverables.json")
    t = DeliverFilesTool(store, workspace=workspace, allowed_dir=None)
    t.set_context("web", "default", "web:s1")
    return t


def _write(tool_workspace: Path, name: str, body: bytes = b"hello") -> Path:
    fp = tool_workspace / name
    fp.write_bytes(body)
    return fp


def test_name_is_snake_case(tool) -> None:
    assert tool.name == "deliver_files"


async def test_happy_path_manifest_shape(tool, tmp_path) -> None:
    _write(tmp_path / "ws", "report.pdf", b"12345")

    summary = await tool.execute(
        files=[{"path": "report.pdf", "title": "Q3 report", "description": "final"}],
        message="here you go",
    )

    assert "report.pdf" in summary
    manifest = tool.take_metadata()["raven_delivery"]
    assert manifest["message"] == "here you go"
    assert manifest["invalid"] == []
    assert len(manifest["files"]) == 1
    entry = manifest["files"][0]
    assert entry["name"] == "report.pdf"
    assert entry["title"] == "Q3 report"
    assert entry["description"] == "final"
    assert entry["size"] == 5
    assert entry["media_type"] == "application/pdf"
    assert entry["token"]
    assert entry["download_path"] == f"/files/download?token={entry['token']}"
    assert "bytes" not in entry and "content" not in entry


async def test_take_metadata_is_consumed_once(tool, tmp_path) -> None:
    _write(tmp_path / "ws", "a.txt")

    await tool.execute(files=[{"path": "a.txt"}])

    assert tool.take_metadata() is not None
    assert tool.take_metadata() is None


async def test_non_web_channel_refuses_without_touching_the_filesystem(tool, tmp_path) -> None:
    """The refusal must not depend on the path being bad: an existing, valid
    file is still refused, and nothing is registered."""
    _write(tmp_path / "ws", "real.txt")
    tool.set_context("whatsapp", "123", "whatsapp:123")

    result = await tool.execute(files=[{"path": "real.txt"}])

    assert result.startswith("Error")
    assert "web" in result
    assert "whatsapp" in result
    assert tool.take_metadata() is None


async def test_missing_file_yields_an_error_and_no_manifest(tool) -> None:
    """Nothing was delivered, so there is no manifest to render; the renderer
    falls back to the text summary, which names the failure."""
    result = await tool.execute(files=[{"path": "nope.txt"}])

    assert result.startswith("Error")
    assert "nope.txt" in result
    assert tool.take_metadata() is None


async def test_mixed_valid_and_invalid(tool, tmp_path) -> None:
    _write(tmp_path / "ws", "good.txt")

    summary = await tool.execute(files=[{"path": "good.txt"}, {"path": "bad.txt"}])

    assert "good.txt" in summary
    assert "bad.txt" in summary
    manifest = tool.take_metadata()["raven_delivery"]
    assert [f["name"] for f in manifest["files"]] == ["good.txt"]
    assert manifest["invalid"] == [{"path": "bad.txt", "reason": "not found or not a regular file"}]


async def test_directory_is_invalid(tool, tmp_path) -> None:
    (tmp_path / "ws" / "sub").mkdir()

    result = await tool.execute(files=[{"path": "sub"}])

    assert result.startswith("Error")


async def test_duplicate_paths_are_deduplicated(tool, tmp_path) -> None:
    _write(tmp_path / "ws", "a.txt")

    await tool.execute(files=[{"path": "a.txt"}, {"path": "./a.txt"}])

    manifest = tool.take_metadata()["raven_delivery"]
    assert len(manifest["files"]) == 1


async def test_redelivery_in_same_conversation_reuses_token(tool, tmp_path) -> None:
    _write(tmp_path / "ws", "a.txt")

    await tool.execute(files=[{"path": "a.txt"}])
    first = tool.take_metadata()["raven_delivery"]["files"][0]["token"]
    await tool.execute(files=[{"path": "a.txt"}])
    second = tool.take_metadata()["raven_delivery"]["files"][0]["token"]

    assert first == second


async def test_path_outside_allowed_dir_is_invalid_when_restricted(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_bytes(b"nope")
    store = DeliverableStore(tmp_path / "deliverables.json")
    restricted = DeliverFilesTool(store, workspace=workspace, allowed_dir=workspace)
    restricted.set_context("web", "default", "web:s1")

    result = await restricted.execute(files=[{"path": str(outside)}])

    assert result.startswith("Error")
    assert restricted.take_metadata() is None


async def test_two_different_files_get_different_tokens(tool, tmp_path) -> None:
    _write(tmp_path / "ws", "a.txt")
    _write(tmp_path / "ws", "b.txt")

    await tool.execute(files=[{"path": "a.txt"}, {"path": "b.txt"}])

    tokens = [f["token"] for f in tool.take_metadata()["raven_delivery"]["files"]]
    assert tokens[0] != tokens[1]
    assert "a.txt" not in tokens[0]
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_deliver_files_tool.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'raven.agent.tools.deliver'`

- [ ] **Step 3: Implement the tool**

Create `raven/agent/tools/deliver.py`:

```python
"""Hand output files to the user as downloadable deliverables (web UI only)."""

from __future__ import annotations

import mimetypes
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from raven.agent.tools._deliverables import DeliverableStore
from raven.agent.tools.base import Tool
from raven.agent.tools.filesystem import _resolve_path

_WEB_CHANNEL = "web"


@dataclass(frozen=True)
class _DeliverCtx:
    channel: str
    chat_id: str
    session_key: str


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class DeliverFilesTool(Tool):
    """Register output files as user-downloadable deliverables.

    The manifest travels back to the turn stream via ``take_metadata`` rather
    than the return value, which the model reads: bytes and UI detail must not
    enter the model's context.
    """

    def __init__(
        self,
        store: DeliverableStore,
        *,
        workspace: Path | None = None,
        allowed_dir: Path | None = None,
    ) -> None:
        self._store = store
        self._workspace = workspace
        self._allowed_dir = allowed_dir
        self._ctx: ContextVar[_DeliverCtx | None] = ContextVar("deliver_files_ctx", default=None)
        self._default = _DeliverCtx(channel="cli", chat_id="direct", session_key="cli:direct")
        # Written by execute (which may run in a child context) and popped by
        # take_metadata in the loop's own task, so the handoff cannot rely on a
        # ContextVar write propagating upward. Keyed by session so concurrent
        # turns never read each other's manifest.
        self._pending: dict[str, dict[str, Any]] = {}

    def _cur(self) -> _DeliverCtx:
        return self._ctx.get() or self._default

    def set_context(self, channel: str, chat_id: str, session_key: str) -> None:
        """Set this turn's routing context (turn-local)."""
        self._ctx.set(_DeliverCtx(channel=channel, chat_id=chat_id, session_key=session_key))

    def take_metadata(self) -> dict[str, Any] | None:
        return self._pending.pop(self._cur().session_key, None)

    @property
    def name(self) -> str:
        return "deliver_files"

    @property
    def description(self) -> str:
        return (
            "Deliver finished output files to the user. Call this with the files the user "
            "should receive (a report, a dataset, a chart) once they are written to disk. "
            "The web UI renders them as a download card and collects them in a Deliverables "
            "panel. Deliver only final artifacts, not scratch files."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "minItems": 1,
                    "description": "The files to deliver.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Path of the file to deliver, relative to the workspace or absolute.",
                            },
                            "title": {
                                "type": "string",
                                "description": "Optional human-friendly title shown in the UI.",
                            },
                            "description": {
                                "type": "string",
                                "description": "Optional one-line note about the file.",
                            },
                        },
                        "required": ["path"],
                    },
                },
                "message": {
                    "type": "string",
                    "description": "Optional note accompanying the whole delivery.",
                },
            },
            "required": ["files"],
        }

    async def execute(
        self, files: list[dict[str, Any]] | None = None, message: str | None = None, **kwargs: Any
    ) -> str:
        ctx = self._cur()
        if ctx.channel != _WEB_CHANNEL:
            return (
                f"Error: deliver_files is only available on the web UI channel "
                f"(current channel: {ctx.channel}). Give the user the file path instead."
            )
        if not files:
            return "Error: deliver_files needs at least one file."

        delivered: list[dict[str, Any]] = []
        invalid: list[dict[str, str]] = []
        seen: set[str] = set()

        for entry in files:
            entry = entry if isinstance(entry, dict) else {}
            raw = str(entry.get("path") or "")
            if not raw:
                invalid.append({"path": raw, "reason": "empty path"})
                continue
            try:
                resolved = _resolve_path(raw, self._workspace, self._allowed_dir)
            except PermissionError as exc:
                invalid.append({"path": raw, "reason": str(exc)})
                continue
            except (OSError, ValueError, RuntimeError) as exc:
                invalid.append({"path": raw, "reason": f"cannot resolve path: {exc}"})
                continue

            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)

            if not resolved.is_file():
                invalid.append({"path": raw, "reason": "not found or not a regular file"})
                continue

            record = self._store.register(
                path=key,
                name=resolved.name,
                media_type=mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
                size=resolved.stat().st_size,
                conversation=ctx.session_key,
            )
            delivered.append(
                {
                    "path": key,
                    "name": record.name,
                    "title": entry.get("title"),
                    "description": entry.get("description"),
                    "size": record.size,
                    "media_type": record.media_type,
                    "token": record.token,
                    "download_path": f"/files/download?token={quote(record.token)}",
                }
            )

        if delivered:
            self._pending[ctx.session_key] = {
                "raven_delivery": {"message": message, "files": delivered, "invalid": invalid}
            }

        return self._summary(delivered, invalid)

    @staticmethod
    def _summary(delivered: list[dict[str, Any]], invalid: list[dict[str, str]]) -> str:
        failures = ", ".join(f"{item['path']} ({item['reason']})" for item in invalid)
        if not delivered:
            return f"Error: delivered no files. Could not deliver: {failures}."
        listing = ", ".join(f"{item['name']} ({_human_size(item['size'])})" for item in delivered)
        text = f"Delivered {len(delivered)} file(s): {listing}."
        if invalid:
            text += f" Could not deliver: {failures}."
        return text
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_deliver_files_tool.py -q`
Expected: PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
git add raven/agent/tools/deliver.py tests/test_deliver_files_tool.py
git commit -m "feat(agent): add web-only deliver_files tool"
```

---

## Task 4: Wire the tool into the loop and the gateway

**Files:**
- Modify: `raven/agent/loop/main.py` (ctor signature near `:257`, field init near `:323`, `_register_default_tools` at `:584-588`, `_set_tool_context` at `:1220-1233`)
- Modify: `raven/cli/gateway_commands.py` (before the `AgentLoop(...)` at `:224`, and the `WebSocketRpcServer(...)` at `:412-416`)
- Modify: `tests/test_cli_tui_commands.py`, `tests/test_cli_gateway_commands.py`
- Modify: `CONTEXT.md`

**Interfaces:**
- Consumes: `DeliverableStore` (Task 1), `DeliverFilesTool` (Task 3), `get_deliverables_path()` (Task 1).
- Produces: `AgentLoop(..., deliverables: DeliverableStore | None = None)`; `WebSocketRpcServer(..., deliverables: DeliverableStore | None = None)`.

**Ordering note:** `AgentLoop` is built at `gateway_commands.py:224`, well before the web channel block at `:403`. The store needs only a path, so build it *before* the `AgentLoop` call, gated on `config.gateway.web.enabled`, and hand the same instance to both `AgentLoop` and `WebSocketRpcServer`. Do not add a late-bind setter: registration happens in the constructor, so the store has to be there by then.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_cli_tui_commands.py`:

```python
# ---------------------------------------------------------------------------
# deliver_files is web-only: the TUI must not get a deliverable store
# ---------------------------------------------------------------------------


def test_tui_agent_loop_receives_no_deliverables_store(patched_tui_loop_deps) -> None:
    """deliver_files is a web-UI-only tool (its download box exists only there),
    and registration is gated on the store's presence, so the TUI must pass
    nothing. Passing a store here would put the tool in the TUI model's schema."""
    from raven.cli.tui_commands import _build_tui_agent_loop

    _build_tui_agent_loop()

    kwargs = patched_tui_loop_deps["agent_loop_kwargs"]
    assert kwargs.get("deliverables") is None
```

Add to `tests/test_cli_gateway_commands.py` (read the file first and reuse its existing fixture for capturing `AgentLoop` kwargs; if it has none, mirror `patched_tui_loop_deps` from `tests/test_cli_tui_commands.py`):

```python
def test_gateway_builds_deliverables_store_when_web_enabled(<gateway fixture>) -> None:
    """The web channel is the only surface that can render a delivery box, so it
    is the only one that constructs the store that enables the tool."""
    # config.gateway.web.enabled = True
    # run the gateway build path
    # assert the AgentLoop kwargs carry a DeliverableStore instance


def test_gateway_passes_no_store_when_web_disabled(<gateway fixture>) -> None:
    # config.gateway.web.enabled = False
    # assert kwargs["deliverables"] is None
```

> **Note for the implementer:** `tests/test_cli_gateway_commands.py` exists; read it and follow whatever isolation pattern it already uses for the gateway build path rather than inventing one. The two assertions above are the contract; the harness is whatever that file already does. If the gateway build path is not unit-testable there, assert instead on a small extracted helper `_build_deliverable_store(config) -> DeliverableStore | None` that you add to `gateway_commands.py` and call from the build path.

And add a behavioural test for the whitelist, in `tests/test_deliver_files_tool.py`:

```python
def test_set_tool_context_hands_deliver_files_the_channel_and_session_key() -> None:
    """The tool needs the turn's channel (for the web gate) and the session key
    (for token reuse); only _set_tool_context supplies them, and it only reaches
    tools named in its whitelist."""
    from raven.agent.loop.main import AgentLoop

    seen: dict[str, tuple[str, str, str]] = {}

    class _FakeDeliver:
        def set_context(self, channel: str, chat_id: str, session_key: str) -> None:
            seen["args"] = (channel, chat_id, session_key)

    class _Tools:
        def get(self, name: str):
            return _FakeDeliver() if name == "deliver_files" else None

    class _Stub:
        tools = _Tools()

    AgentLoop._set_tool_context(_Stub(), "web", "default", None, session_key="web:s1")

    assert seen["args"] == ("web", "default", "web:s1")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli_tui_commands.py tests/test_cli_gateway_commands.py tests/test_deliver_files_tool.py -q`

Expected, per test:
- `test_set_tool_context_hands_deliver_files_the_channel_and_session_key` → **FAIL** with `KeyError: 'args'` (the tool is not in the whitelist yet, so `set_context` is never called).
- `test_gateway_builds_deliverables_store_when_web_enabled` → **FAIL** (no store is built).
- `test_tui_agent_loop_receives_no_deliverables_store` → **PASSES today**, because nothing is passed yet. That is intended: it is a guard that must keep passing after Task 4 adds the parameter, not a red-first test. Do not "fix" it to fail.

- [ ] **Step 3: Add the ctor parameter and the registration guard**

In `raven/agent/loop/main.py`, add to the `__init__` signature next to `channels_config` (`:257`):

```python
        deliverables: "DeliverableStore | None" = None,
```

Store it next to the other collaborators (near `:323`):

```python
        self._deliverables = deliverables
```

In `_register_default_tools` (`:584-588`), after the filesystem tools are registered:

```python
        if self._deliverables is not None:
            from raven.agent.tools.deliver import DeliverFilesTool

            self.tools.register(
                DeliverFilesTool(
                    self._deliverables, workspace=self.workspace, allowed_dir=allowed_dir
                )
            )
```

- [ ] **Step 4: Add the tool to the per-turn context whitelist**

In `_set_tool_context` (`:1224` and `:1230`):

```python
        for name in ("message", "spawn", "cron", "deep_research", "run_subagent_dag", "deliver_files"):
            if tool := self.tools.get(name):
                if not hasattr(tool, "set_context"):
                    continue
                if name == "message":
                    tool.set_context(channel, chat_id, message_id)
                elif name in ("spawn", "deep_research", "run_subagent_dag", "deliver_files"):
                    tool.set_context(channel, chat_id, session_key or f"{channel}:{chat_id}")
                else:
                    tool.set_context(channel, chat_id)
```

- [ ] **Step 5: Build the store in the gateway**

In `raven/cli/gateway_commands.py`, before the `AgentLoop(` call at `:224`:

```python
        deliverables = None
        if config.gateway.web.enabled:
            from raven.agent.tools._deliverables import DeliverableStore
            from raven.config.paths import get_deliverables_path

            deliverables = DeliverableStore(get_deliverables_path())
```

Pass it in the `AgentLoop(...)` kwargs (next to `disabled_tools=`):

```python
            deliverables=deliverables,
```

And hand the same instance to the HTTP server at `:412-416`:

```python
                    web_server = WebSocketRpcServer(
                        host=web_cfg.host,
                        port=web_cfg.port,
                        auth_token=web_cfg.auth_token or None,
                        deliverables=deliverables,
                    )
```

- [ ] **Step 6: Accept the store on the server (routes come in Task 5)**

In `raven/web_rpc/server.py`, add the keyword to `__init__` and stash it:

```python
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        *,
        auth_token: str | None = None,
        deliverables: "DeliverableStore | None" = None,
    ) -> None:
        ...
        self._deliverables = deliverables
```

- [ ] **Step 7: Define the domain terms**

`AGENTS.md` §6 requires a new domain term to be defined in the matching `CONTEXT.md` in the same change. Add to `CONTEXT.md`, at the end of the `### Channels & Front-ends` section:

```markdown
**Deliverable**:
An output file the agent hands to the user through the `deliver_files` tool, addressed by an
opaque token in the persisted registry (`deliverables/deliverables.json`) rather than by path.
Web-channel only: the download UI exists only there, and the tool is not registered on any
other surface.
_Avoid_: calling any file the agent wrote a Deliverable - only a `deliver_files` call makes one.

**Delivery Manifest**:
The structured list of Deliverables a single `deliver_files` call produced (name, size, media
type, token), carried on `ToolEvent.metadata` so it reaches the web UI and persists in message
history. Never carries file bytes.
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli_tui_commands.py tests/test_cli_gateway_commands.py tests/test_deliver_files_tool.py tests/test_cli_tui_bootstrap.py tests/test_cli_agent_commands.py -q`
Expected: PASS

- [ ] **Step 9: Verify the TUI really does not register the tool**

Run:
```bash
uv run python - <<'EOF'
import asyncio, inspect
from raven.agent.loop import AgentLoop
sig = inspect.signature(AgentLoop.__init__)
assert "deliverables" in sig.parameters, "ctor param missing"
assert sig.parameters["deliverables"].default is None, "must default to None"
print("ok: deliverables defaults to None, so no store means no tool")
EOF
```
Expected: `ok: ...`

- [ ] **Step 10: Commit**

```bash
git add raven/agent/loop/main.py raven/cli/gateway_commands.py raven/web_rpc/server.py CONTEXT.md tests/test_cli_tui_commands.py tests/test_cli_gateway_commands.py tests/test_deliver_files_tool.py
git commit -m "feat(agent): register deliver_files only on the web channel"
```

---

## Task 5: The gateway download routes

**Files:**
- Create: `raven/web_rpc/files.py`
- Create: `tests/test_web_rpc_files_download.py`
- Modify: `raven/web_rpc/server.py` (`serve_forever`, `:139-146`)

**Interfaces:**
- Consumes: `DeliverableStore`, `DeliverableRecord` (Task 1); `self._deliverables` on the server (Task 4).
- Produces:
  - `resolve_download(store: DeliverableStore, token: str | None) -> DeliverableRecord | None` — returns `None` for an unknown token *and* for a known token whose file is gone, dropping the stale entry as it goes.
  - `add_files_routes(app: web.Application, store: DeliverableStore | None) -> None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_rpc_files_download.py`:

```python
"""Tests for the gateway's deliverable download routes. The opaque token is the
trust boundary: no path ever appears in a request."""

from __future__ import annotations

import zipfile
from io import BytesIO

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from raven.agent.tools._deliverables import DeliverableStore
from raven.web_rpc.files import add_files_routes, resolve_download


@pytest.fixture
async def client(tmp_path):
    store = DeliverableStore(tmp_path / "deliverables.json")
    app = web.Application()
    add_files_routes(app, store)
    async with TestClient(TestServer(app)) as c:
        c.store = store
        c.tmp_path = tmp_path
        yield c


def _register(store, tmp_path, name="report.pdf", body=b"PDF-BYTES"):
    fp = tmp_path / name
    fp.write_bytes(body)
    return store.register(
        path=str(fp),
        name=name,
        media_type="application/pdf",
        size=len(body),
        conversation="web:s1",
    )


async def test_download_returns_bytes_and_attachment_header(client) -> None:
    record = _register(client.store, client.tmp_path)

    res = await client.get("/files/download", params={"token": record.token})

    assert res.status == 200
    assert await res.read() == b"PDF-BYTES"
    assert "attachment" in res.headers["Content-Disposition"]
    assert "report.pdf" in res.headers["Content-Disposition"]


async def test_unknown_token_is_404(client) -> None:
    res = await client.get("/files/download", params={"token": "nope"})
    assert res.status == 404


async def test_missing_token_is_400(client) -> None:
    res = await client.get("/files/download")
    assert res.status == 400


async def test_deleted_file_is_404_and_entry_is_pruned(client) -> None:
    record = _register(client.store, client.tmp_path)
    (client.tmp_path / "report.pdf").unlink()

    res = await client.get("/files/download", params={"token": record.token})

    assert res.status == 404
    assert client.store.get(record.token) is None


async def test_head_agrees_with_get(client) -> None:
    """The frontend pre-checks with HEAD so a failed download becomes a toast
    instead of a blank tab."""
    record = _register(client.store, client.tmp_path)

    ok = await client.head("/files/download", params={"token": record.token})
    missing = await client.head("/files/download", params={"token": "nope"})

    assert ok.status == 200
    assert missing.status == 404


async def test_archive_zips_every_requested_token(client) -> None:
    one = _register(client.store, client.tmp_path, "a.txt", b"AAA")
    two = _register(client.store, client.tmp_path, "b.txt", b"BBB")

    res = await client.get(
        "/files/download-archive", params=[("token", one.token), ("token", two.token)]
    )

    assert res.status == 200
    archive = zipfile.ZipFile(BytesIO(await res.read()))
    assert sorted(archive.namelist()) == ["a.txt", "b.txt"]
    assert archive.read("a.txt") == b"AAA"


async def test_archive_skips_missing_members(client) -> None:
    one = _register(client.store, client.tmp_path, "a.txt", b"AAA")

    res = await client.get(
        "/files/download-archive", params=[("token", one.token), ("token", "nope")]
    )

    assert res.status == 200
    assert zipfile.ZipFile(BytesIO(await res.read())).namelist() == ["a.txt"]


async def test_archive_with_no_valid_member_is_404(client) -> None:
    res = await client.get("/files/download-archive", params={"token": "nope"})
    assert res.status == 404


async def test_resolve_download_drops_stale_entry(tmp_path) -> None:
    store = DeliverableStore(tmp_path / "deliverables.json")
    record = _register(store, tmp_path)
    (tmp_path / "report.pdf").unlink()

    assert resolve_download(store, record.token) is None
    assert store.get(record.token) is None
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_rpc_files_download.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'raven.web_rpc.files'`

- [ ] **Step 3: Implement the routes**

Create `raven/web_rpc/files.py`:

```python
"""HTTP download routes for delivered files, served by the gateway.

The request carries an opaque token, never a path, so these routes are not a
general file-read primitive: they serve exactly what ``deliver_files`` recorded.
"""

from __future__ import annotations

import os
import tempfile
import zipfile

from aiohttp import web
from loguru import logger

from raven.agent.tools._deliverables import DeliverableRecord, DeliverableStore
from raven.config.paths import get_cache_dir

_CHUNK = 64 * 1024


def resolve_download(store: DeliverableStore, token: str | None) -> DeliverableRecord | None:
    """Resolve a token to a still-present file, dropping a stale entry."""
    if not token:
        return None
    record = store.get(token)
    if record is None:
        return None
    if not os.path.isfile(record.path):
        store.drop(token)
        return None
    return record


def _attachment(name: str) -> str:
    safe = name.replace('"', "")
    return f'attachment; filename="{safe}"'


def add_files_routes(app: web.Application, store: DeliverableStore | None) -> None:
    """Register the deliverable download routes. A None store registers nothing,
    so a gateway without the web channel exposes no download surface at all."""
    if store is None:
        return

    async def download(request: web.Request) -> web.StreamResponse:
        token = request.query.get("token")
        if not token:
            raise web.HTTPBadRequest(text="token is required")
        record = resolve_download(store, token)
        if record is None:
            raise web.HTTPNotFound(text="not found")
        return web.FileResponse(
            record.path,
            headers={
                "Content-Type": record.media_type,
                "Content-Disposition": _attachment(record.name),
            },
        )

    async def download_archive(request: web.Request) -> web.StreamResponse:
        tokens = request.query.getall("token", [])
        records = [r for r in (resolve_download(store, t) for t in tokens) if r is not None]
        if not records:
            raise web.HTTPNotFound(text="not found")

        response = web.StreamResponse(
            headers={
                "Content-Type": "application/zip",
                "Content-Disposition": _attachment("deliverables.zip"),
            }
        )
        if request.method == "HEAD":
            await response.prepare(request)
            await response.write_eof()
            return response

        tmp_dir = get_cache_dir() / "deliverables"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        handle, tmp_name = tempfile.mkstemp(suffix=".zip", dir=str(tmp_dir))
        os.close(handle)
        try:
            with zipfile.ZipFile(tmp_name, "w", zipfile.ZIP_DEFLATED) as archive:
                for record in records:
                    archive.write(record.path, arcname=record.name)
            await response.prepare(request)
            with open(tmp_name, "rb") as fh:
                while chunk := fh.read(_CHUNK):
                    await response.write(chunk)
            await response.write_eof()
            return response
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                logger.warning("deliverables: could not remove temp archive {}", tmp_name)

    app.router.add_get("/files/download", download, allow_head=True)
    app.router.add_get("/files/download-archive", download_archive, allow_head=True)
```

> `add_get(..., allow_head=True)` is the aiohttp default and is spelled out here because the frontend's HEAD pre-check depends on it.

- [ ] **Step 4: Mount them on the gateway app**

In `raven/web_rpc/server.py`, in `serve_forever`:

```python
        app = web.Application()
        app.router.add_get("/ws", self._handle_ws)
        add_files_routes(app, self._deliverables)
```

with `from raven.web_rpc.files import add_files_routes` at the top of the module.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_rpc_files_download.py -q`
Expected: PASS (10 tests)

- [ ] **Step 6: Verify no regression on the WS server**

Run: `uv run pytest tests/test_tui_rpc_server_socket.py tests/test_cli_gateway_commands.py -q`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add raven/web_rpc/files.py raven/web_rpc/server.py tests/test_web_rpc_files_download.py
git commit -m "feat(web-rpc): serve delivered files over token-addressed http routes"
```

---

## Task 6: Service metadata merge and stream-proxy

**Files:**
- Modify: `ui-webui/service/raven_gateway_agent.py` (`:367-371`, and near the `GATEWAY_WS_URL` constant at `:58`)
- Modify: `ui-webui/service/raven_config_routes.py` (add routes to the `/raven` router)

**Interfaces:**
- Consumes: the `tool.complete` wire payload key `metadata` (Task 2); the gateway routes `/files/download` and `/files/download-archive` (Task 5).
- Produces: `gateway_http_base() -> str` in `raven_gateway_agent.py`; service routes `GET|HEAD /raven/files/download` and `GET|HEAD /raven/files/download-archive`.

**No test runner covers `ui-webui/service/`.** Gate this task with the import check in Step 5 and the manual acceptance in Task 8. Do not claim it is tested.

- [ ] **Step 1: Merge the wire metadata into the tool result event**

In `ui-webui/service/raven_gateway_agent.py`, at the `ToolResultEndEvent` construction (`:367-371`):

```python
                yield ToolResultEndEvent(
                    reply_id=reply_id,
                    tool_call_id=tcid,
                    state=ToolResultState.SUCCESS,
                    metadata={
                        "truncated": bool(payload.get("truncated")),
                        **(payload.get("metadata") or {}),
                    },
                )
```

This is what puts the manifest on the persisted `ToolResultBlock`, which is what keeps the Deliverables panel populated across a reload.

- [ ] **Step 2: Expose the gateway HTTP base**

In `ui-webui/service/raven_gateway_agent.py`, next to `GATEWAY_WS_URL` (`:58`):

```python
def gateway_http_base() -> str:
    """HTTP origin of the gateway, derived from its WS URL.

    The manifest deliberately carries no host (it is persisted into message
    history, where a host captured at delivery time can later be wrong), so the
    origin is composed at request time from the one URL the service already has.
    """
    base = GATEWAY_WS_URL
    if base.endswith("/ws"):
        base = base[: -len("/ws")]
    if base.startswith("wss://"):
        return "https://" + base[len("wss://") :]
    if base.startswith("ws://"):
        return "http://" + base[len("ws://") :]
    return base
```

- [ ] **Step 3: Add the stream-proxy routes**

In `ui-webui/service/raven_config_routes.py`, add the imports and the routes inside `build_raven_config_router()`:

```python
from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import StreamingResponse
from raven_gateway_agent import GatewayClient, gateway_http_base
```

```python
    @router.api_route("/files/download", methods=["GET", "HEAD"])
    async def download_deliverable(request: Request):
        return await _proxy_files(request, "/files/download")

    @router.api_route("/files/download-archive", methods=["GET", "HEAD"])
    async def download_deliverable_archive(request: Request):
        return await _proxy_files(request, "/files/download-archive")
```

and the helper, above `build_raven_config_router`:

```python
_FORWARDED_HEADERS = ("content-type", "content-disposition", "content-length")


async def _proxy_files(request: Request, path: str) -> StreamingResponse:
    """Relay a token-addressed download from the gateway.

    The gateway is loopback-bound by design, so the browser cannot reach it
    directly whenever it is not on the gateway host. The service forwards the
    token and relays the stream; it resolves no paths and reads no files, so file
    access and authorization stay entirely on the gateway side. Both hops stream,
    so memory stays bounded no matter how large the deliverable is.
    """
    import aiohttp

    url = f"{gateway_http_base()}{path}"
    session = aiohttp.ClientSession()
    try:
        upstream = await session.request(
            request.method, url, params=request.query_params.multi_items()
        )
    except aiohttp.ClientError as exc:
        await session.close()
        raise HTTPException(status_code=502, detail=f"gateway unreachable: {exc}") from exc

    if upstream.status >= 400:
        status = upstream.status
        upstream.release()
        await session.close()
        raise HTTPException(status_code=status, detail="deliverable not available")

    headers = {k: v for k, v in upstream.headers.items() if k.lower() in _FORWARDED_HEADERS}

    async def body():
        try:
            async for chunk in upstream.content.iter_chunked(64 * 1024):
                yield chunk
        finally:
            upstream.release()
            await session.close()

    return StreamingResponse(body(), status_code=upstream.status, headers=headers)
```

- [ ] **Step 4: Check the URL derivation**

Run:
```bash
cd ui-webui/service && python - <<'EOF'
import os
os.environ["RAVEN_GATEWAY_WS_URL"] = "ws://127.0.0.1:8765/ws"
import importlib, raven_gateway_agent as m
importlib.reload(m)
assert m.gateway_http_base() == "http://127.0.0.1:8765", m.gateway_http_base()
os.environ["RAVEN_GATEWAY_WS_URL"] = "wss://box.example:9000/ws"
importlib.reload(m)
assert m.gateway_http_base() == "https://box.example:9000", m.gateway_http_base()
print("ok: ws->http and wss->https derivation")
EOF
```
Expected: `ok: ws->http and wss->https derivation`

(Run this in the `ravenx` conda env, the one `start_webapp.sh` uses for the service.)

- [ ] **Step 5: Check both modules still import**

Run:
```bash
cd ui-webui/service && python -c "import raven_config_routes, raven_gateway_agent; print('ok: service modules import')"
```
Expected: `ok: service modules import`

- [ ] **Step 6: Commit**

```bash
git add ui-webui/service/raven_gateway_agent.py ui-webui/service/raven_config_routes.py
git commit -m "feat(ui-webui): relay tool metadata and proxy deliverable downloads"
```

---

## Task 7: Frontend contract changes

**Files:**
- Modify: `ui-webui/frontend/src/components/delivery/deriveDeliverables.ts`
- Modify: `ui-webui/frontend/src/api/files.ts`
- Modify: `ui-webui/frontend/src/components/chat/tool-renderers/index.tsx:31`
- Modify: `ui-webui/frontend/src/components/delivery/DeliveredFileRow.tsx`
- Modify: `ui-webui/frontend/src/i18n/locales/en.json`, `ui-webui/frontend/src/i18n/locales/zh.json`

**Interfaces:**
- Consumes: the manifest shape from Task 3 (`raven_delivery` key, tool name `deliver_files`, per-file `token` and `download_path`) and the service routes from Task 6.
- Produces: `filesApi.downloadDeliverable(token: string, filename: string): Promise<void>`, `filesApi.downloadArchive(tokens: string[]): Promise<void>`. Both take primitives, not a `DeliveredFile`, so `files.ts` needs no import from `deriveDeliverables.ts` (which imports `filesApi` — a type-only import would be erased, but avoiding the edge entirely keeps `import/no-cycle` quiet).

- [ ] **Step 1: Update the manifest contract**

In `deriveDeliverables.ts`:

```ts
export interface DeliveredFile {
	path: string;
	name: string;
	title?: string | null;
	description?: string | null;
	size: number;
	media_type: string;
	token: string;
	download_path: string;
}
```

```ts
/** The tool name whose results carry a delivery manifest. */
export const DELIVER_FILES_TOOL = 'deliver_files';
```

```ts
	const raw = metadata?.raven_delivery;
```

Update the two doc comments that say `ravenx_delivery` to `raven_delivery`.

**Delete** `parseDeliveryContext` entirely, and replace `downloadAllDeliverables`:

```ts
/** Download every given file as a single zip. */
export async function downloadAllDeliverables(files: DeliveredFile[]): Promise<void> {
	if (files.length === 0) return;
	await filesApi.downloadArchive(files.map((f) => f.token));
}
```

- [ ] **Step 2: Rewrite the download helpers**

Replace the body of `ui-webui/frontend/src/api/files.ts`:

```ts
import { client, getBaseUrl } from './client';

/** Trigger a browser "save as" for a URL the server marks as an attachment. */
function saveFromUrl(url: string, filename: string): void {
	const anchor = document.createElement('a');
	anchor.href = url;
	anchor.download = filename;
	document.body.appendChild(anchor);
	anchor.click();
	anchor.remove();
}

/**
 * A deliverable is streamed, not buffered: there is no size cap on delivery, so
 * fetching into a Blob would have to hold the whole file in the tab. A plain
 * anchor hands the stream to the browser instead. The cost is losing fetch's
 * error path, so a HEAD runs first and a failure becomes a caller-side error
 * rather than a blank tab.
 */
async function ensureAvailable(path: string): Promise<void> {
	await client.stream(path, { method: 'HEAD', silent: true });
}

export const filesApi = {
	/** Download one delivered file, addressed by its opaque token. */
	downloadDeliverable: async (token: string, filename: string): Promise<void> => {
		const path = `/raven/files/download?token=${encodeURIComponent(token)}`;
		await ensureAvailable(path);
		saveFromUrl(`${getBaseUrl()}${path}`, filename);
	},

	/** Download several delivered files as one zip ("Download all"). */
	downloadArchive: async (tokens: string[]): Promise<void> => {
		if (tokens.length === 0) return;
		const params = new URLSearchParams();
		for (const token of tokens) params.append('token', token);
		const path = `/raven/files/download-archive?${params.toString()}`;
		await ensureAvailable(path);
		saveFromUrl(`${getBaseUrl()}${path}`, 'deliverables.zip');
	},
};
```

- [ ] **Step 3: Make the renderer registry key follow the constant**

In `ui-webui/frontend/src/components/chat/tool-renderers/index.tsx`, import the constant and replace the literal key at `:31`:

```ts
import { DELIVER_FILES_TOOL } from '@/components/delivery/deriveDeliverables';
```

```ts
	[DELIVER_FILES_TOOL]: DeliverFilesRenderer,
```

- [ ] **Step 4: Point the row at the new helper and surface failures**

In `DeliveredFileRow.tsx`, replace the `downloadFromPath(file.download_path, file.name)` call with `filesApi.downloadDeliverable(file)`, wrapped so a rejection shows the new string:

```tsx
	const onDownload = async () => {
		try {
			await filesApi.downloadDeliverable(file.token, file.name);
		} catch {
			toast.error(t('tool.deliverFilesUnavailable'));
		}
	};
```

> Use whichever toast helper the file's neighbours already use; do not introduce a new notification library. If the component has no toast in scope, surface the failure the same way `DeliverablesPanel` surfaces other errors.

- [ ] **Step 5: Add the i18n string (targeted edits only)**

In `en.json`, inside the `tool` object next to `deliverFilesTitle_other`:

```json
		"deliverFilesUnavailable": "This file is no longer available for download.",
```

In `zh.json`, the same key:

```json
		"deliverFilesUnavailable": "该文件已无法下载。",
```

- [ ] **Step 6: Run the frontend gate**

Run:
```bash
cd ui-webui && pnpm -C frontend lint && pnpm -C frontend build
```
Expected: lint reports **0 errors** (pre-existing warnings are acceptable); build succeeds. A `parseDeliveryContext`-not-found error means a consumer was missed — grep for it and remove the call.

- [ ] **Step 7: Commit**

```bash
git add ui-webui/frontend/src
git commit -m "feat(ui-webui): point the delivery UI at the raven manifest contract"
```

---

## Task 8: Integration smoke and manual acceptance

**Files:**
- Create: `tests/integration/test_file_delivery_smoke.py`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing.

- [ ] **Step 1: Write the smoke test**

Create `tests/integration/test_file_delivery_smoke.py`:

```python
"""Multi-module smoke: the tool registers a deliverable and the gateway's real
aiohttp routes serve exactly those bytes back."""

from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from raven.agent.tools._deliverables import DeliverableStore
from raven.agent.tools.deliver import DeliverFilesTool
from raven.web_rpc.files import add_files_routes


async def test_deliver_then_download_round_trip(tmp_path) -> None:
    workspace = tmp_path / "ws"
    workspace.mkdir()
    payload = b"report bytes, exactly these"
    (workspace / "report.txt").write_bytes(payload)

    store = DeliverableStore(tmp_path / "deliverables.json")
    tool = DeliverFilesTool(store, workspace=workspace, allowed_dir=workspace)
    tool.set_context("web", "default", "web:s1")

    await tool.execute(files=[{"path": "report.txt", "title": "The report"}])
    manifest = tool.take_metadata()["raven_delivery"]
    entry = manifest["files"][0]

    app = web.Application()
    add_files_routes(app, store)
    async with TestClient(TestServer(app)) as client:
        res = await client.get("/files/download", params={"token": entry["token"]})

        assert res.status == 200
        assert await res.read() == payload
        assert "report.txt" in res.headers["Content-Disposition"]


async def test_delivery_survives_a_store_restart(tmp_path) -> None:
    """The gateway can restart between delivery and download; the persisted
    registry is what keeps the button working."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"AAA")
    path = tmp_path / "deliverables.json"

    tool = DeliverFilesTool(DeliverableStore(path), workspace=workspace, allowed_dir=workspace)
    tool.set_context("web", "default", "web:s1")
    await tool.execute(files=[{"path": "a.txt"}])
    token = tool.take_metadata()["raven_delivery"]["files"][0]["token"]

    app = web.Application()
    add_files_routes(app, DeliverableStore(path))
    async with TestClient(TestServer(app)) as client:
        res = await client.get("/files/download", params={"token": token})

        assert res.status == 200
        assert await res.read() == b"AAA"
```

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/integration/test_file_delivery_smoke.py -q`
Expected: PASS (2 tests)

- [ ] **Step 3: Run the whole affected suite**

Run:
```bash
uv run pytest tests/test_deliverable_store.py tests/test_deliver_files_tool.py \
  tests/test_web_rpc_files_download.py tests/test_tool_metadata_channel.py \
  tests/test_tui_rpc_tool_events.py tests/test_spine_events.py \
  tests/test_cli_tui_commands.py tests/test_cli_gateway_commands.py \
  tests/test_cli_agent_commands.py tests/integration/test_file_delivery_smoke.py -q
```
Expected: all PASS

- [ ] **Step 4: Manual acceptance in the real app**

Enable the web channel in `~/.raven/config.json` (`gateway.web.enabled: true`), then:

```bash
RAVEN_GATEWAY=1 ./start_webapp.sh
```

Walk this list and record the actual result of each item — do not mark the task done on any item you did not observe:

1. Ask the agent to write a file and deliver it. The inline card appears with the file name and a humanized size.
2. Click **Download**. The saved bytes match the file on disk.
3. **Reload the page.** The Deliverables panel still lists the file and the button still works. (Joint acceptance for the `ToolEvent.metadata` persistence and the persisted registry.)
4. **Restart the gateway** (`./start_webapp.sh restart`). The old button still works.
5. Deliver a second file, then **Download all**. The zip opens and contains both.
6. Delete a delivered file on disk, then click its button. A toast says it is unavailable; no blank tab.
7. Send a turn from a non-web channel (or temporarily target one) and ask for a delivery. The tool refuses with the guidance string.
8. Run `raven tui` and check the tool list. `deliver_files` is **absent**.

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_file_delivery_smoke.py
git commit -m "test(agent): smoke the deliver_files download round trip"
```

---

## Self-Review

**Spec coverage.** Spec section 5 (tool) -> Task 3; 5.1 (registration gate) -> Task 4 steps 3, 5, 6; 5.2 (channel gate) -> Task 3 plus Task 4 step 4; 5.3-5.5 -> Task 3; section 6 (manifest channel) -> Task 2; section 7 (routes and store) -> Tasks 1 and 5; section 8 (service proxy) -> Task 6; section 9 (frontend) -> Task 7; section 10 (error handling) -> covered by the tests in Tasks 3, 5 and the manual list in Task 8; section 11 (testing) -> Tasks 1-8; decision 6 (no size cap, size shown) -> `_human_size` in Task 3 plus the streaming anchor in Task 7; `AGENTS.md` §6 (domain terms) -> Task 4 step 7.

**Known soft spots, called out rather than hidden.**
- Task 2 step 1 and Task 4 step 1 contain a harness note instead of final test code, because the exact `TuiOutlet` entry point and the gateway test fixture must be read from the existing files first. The assertions are fixed; only the scaffolding is to be adapted.
- `ui-webui/service/` has no test runner, so Task 6 is gated by import and derivation checks plus the manual list, not by unit tests. Task 6 says so explicitly.
- `DeliverableStore.register` scans linearly for the reuse lookup. Fine at the expected scale (tens of entries); revisit only if a registry ever grows large.
