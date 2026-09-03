# Sub-agent everos memory record - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** After a sub-agent call finishes, write a small `memory.json` beside that call's prompt and output listing what the sub-agent wrote into everos, on all three delegation paths.

**Architecture:** A new module `raven/agent/subagent_memory.py` reads everos over HTTP, filtered by the join key the host already owns (`cli:<instance agent_id>`), and writes a two-field-per-item JSON file. It is scheduled as a background task after each path's existing `finish` call and never awaited by the dispatch path. An agent opts in by declaring an everos identity in its config; without one, nothing runs.

**Tech Stack:** Python 3.12+, pydantic v2 (config schema), httpx (async HTTP, `MockTransport` in tests), loguru, pytest + pytest-asyncio, uv.

**Spec:** `docs/specs/2026-08-17-subagent-everos-memory-record-design.md`

## Deviation from the spec

The spec's section 3 describes `MemoryTrace.open(...)` before dispatch plus
`settle()` after. That shape existed to capture the call's start time for the
record's `window` field. Section 4 of the spec dropped `window` (the record is
read by another sub-agent, not a human auditor), so `open` would capture
nothing and the pair collapses to a **single post-finish call**. This plan
implements the collapsed shape: one call site per path instead of two, and a
module of plain functions instead of a class.

One consequence worth stating: the join key is resolved at settle time, not
before dispatch. It has to be. `InstanceRegistry` only holds the id after the
backend commits it (`cli_agent.py:610`), so a first call to a fresh handle has
no registry row until the run is over.

## Global Constraints

- Package manager is `uv`. Never `pip`, never hand-edit `pyproject.toml` or `uv.lock` (AGENTS.md section 4).
- Run tests as `uv run pytest ...`, never bare `pytest` (AGENTS.md section 5.4).
- Comments only where logic is non-obvious or a constraint is hidden, and always in English (AGENTS.md section 1). Match the surrounding density: these modules comment the *why*, not the *what*.
- Do not commit unless the user explicitly asks (AGENTS.md section 3.4). The commit steps below are written out so they are ready to run, but each needs the user's word first.
- Commit messages: Conventional Commits, all-ASCII, header <= 100 chars, with the `Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>` trailer (AGENTS.md sections 3.1, 3.3).
- New domain terms go in `CONTEXT.md` in the same change (AGENTS.md section 6).
- Extend existing test files; do not add per-feature ones (AGENTS.md section 5.1).
- everos wire contract: `POST <base_url>/api/v2/memory/get`, body takes `user_id` XOR `agent_id`, `memory_type`, `filters`, `page_size` (max 100); response is `{"request_id": ..., "data": {"episodes": [...], "agent_cases": [...], ...}}`.

## File structure

| File | Responsibility |
|---|---|
| `raven/agent/subagent_memory.py` (create) | The whole feature: identity type, everos read, record write. No imports from `subagent/` so it stays testable standalone. |
| `raven/config/schema.py` (modify) | `SubagentEverosConfig` + the `everos` field on `ThirdPartyCliSubagentConfig`. |
| `raven/agent/subagent/manager.py` (modify) | Identity map built from config; scheduling helper; two call sites (spawn, direct chat). |
| `raven/agent/subagent_dag/runner.py` (modify) | Third call site, plus the `everos_for` callable parameter. |
| `raven/agent/subagent_dag/tool.py` (modify) | Passes the manager's `everos_for` into `run_dag`. |
| `CONTEXT.md` (modify) | Defines **Memory record**. |

---

### Task 1: Declared everos identity on a CLI sub-agent config

**Files:**
- Modify: `raven/config/schema.py` (add `SubagentEverosConfig` above `ThirdPartyCliSubagentConfig:912`; add the `everos` field inside it)
- Test: `tests/test_subagent_third_party.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SubagentEverosConfig` with fields `user_id: str | None`, `agent_id: str | None`, `base_url: str | None`, `session_prefix: str`; and `ThirdPartyCliSubagentConfig.everos: SubagentEverosConfig | None`. Task 2 reads these off the config object.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_third_party.py`:

```python
def test_cli_agent_accepts_a_declared_everos_identity() -> None:
    cfg = ThirdPartyCliSubagentConfig(
        name="Raven-Code",
        command="run.py --session {agent_id}",
        resume_command="run.py --session {agent_id}",
        everos={"userId": "raven-code", "agentId": "raven-code"},
    )
    assert cfg.everos is not None
    assert cfg.everos.user_id == "raven-code"
    assert cfg.everos.agent_id == "raven-code"
    assert cfg.everos.base_url is None
    assert cfg.everos.session_prefix == "cli:"


def test_cli_agent_without_an_everos_block_declares_none() -> None:
    cfg = ThirdPartyCliSubagentConfig(name="Coder", command="claude-acp")
    assert cfg.everos is None


def test_an_everos_block_naming_no_owner_is_rejected() -> None:
    with pytest.raises(ValidationError, match="userId or agentId"):
        ThirdPartyCliSubagentConfig(
            name="Raven-Code",
            command="run.py",
            everos={"baseUrl": "http://localhost:18791"},
        )
```

Add `from pydantic import ValidationError` and `SubagentEverosConfig` to that file's imports if absent.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_subagent_third_party.py -k everos -v
```

Expected: FAIL, `ThirdPartyCliSubagentConfig` has no field `everos` (pydantic rejects the extra key or the attribute is missing).

- [ ] **Step 3: Implement**

In `raven/config/schema.py`, immediately above `class ThirdPartyCliSubagentConfig`:

```python
class SubagentEverosConfig(Base):
    """A sub-agent's everos identity, as the host must address it to read back
    what that sub-agent wrote.

    Declared rather than discovered. The alternative -- reading the fork's own
    config.json next to its ``run.py`` -- would couple the host to a directory
    convention that lives entirely inside each fork.
    """

    user_id: str | None = None
    """Owner of this sub-agent's ``episode`` memories."""
    agent_id: str | None = None
    """Owner of this sub-agent's ``agent_case`` memories."""
    base_url: str | None = None
    """everos service for this sub-agent. ``None`` takes the host's own."""
    session_prefix: str = "cli:"
    """What the fork's launcher prepends to the host-minted id before handing it
    to its Raven as ``--session``. Configurable because that is a convention
    living in each fork's run.py, not something the host controls."""

    @model_validator(mode="after")
    def _check_owner_declared(self) -> "SubagentEverosConfig":
        if not self.user_id and not self.agent_id:
            raise ValueError("everos needs userId or agentId (it could query nothing otherwise)")
        return self
```

Inside `ThirdPartyCliSubagentConfig`, after `max_output_chars: int = 30000`:

```python
    everos: SubagentEverosConfig | None = None
    """This agent's everos identity, or ``None`` for an agent that writes no
    everos memory. Declaring it is what turns the Memory record on."""
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_subagent_third_party.py -k everos -v
```

Expected: 3 passed.

- [ ] **Step 5: Check nothing else broke**

```bash
uv run pytest tests/test_subagent_third_party.py tests/test_config_raven_sections.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit (only on the user's word)**

```bash
git add raven/config/schema.py tests/test_subagent_third_party.py
git commit -m "$(cat <<'EOF'
feat(config): let a cli sub-agent declare its everos identity

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Read a sub-agent's memories out of everos

**Files:**
- Create: `raven/agent/subagent_memory.py`
- Test: `tests/test_subagent_memory.py` (create)

**Interfaces:**
- Consumes: `SubagentEverosConfig` from Task 1.
- Produces:
  - `EverosIdentity` frozen dataclass: `user_id: str | None`, `agent_id: str | None`, `base_url: str`, `session_prefix: str`.
  - `identity_from_config(cfg: Any, default_base_url: str) -> EverosIdentity | None`
  - `MemoryItem` frozen dataclass: `type: str`, `text: str`
  - `async collect_memories(client: httpx.AsyncClient, identity: EverosIdentity, session_id: str) -> list[MemoryItem]`

  Task 3 calls `collect_memories`; Task 4 calls `identity_from_config`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_subagent_memory.py`:

```python
"""The Memory record: what a sub-agent wrote into everos for one call."""

from __future__ import annotations

import json

import httpx
import pytest

from raven.agent.subagent_memory import (
    EverosIdentity,
    MemoryItem,
    collect_memories,
    identity_from_config,
)
from raven.config.schema import SubagentEverosConfig


class _MockEverOS:
    """Canned /memory/get responses, keyed by the memory_type asked for."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.rows: dict[str, list[dict]] = {"episode": [], "agent_case": []}
        self.status = 200

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8"))
        self.requests.append(body)
        if self.status != 200:
            return httpx.Response(self.status, json={"detail": "boom"})
        kind = body["memory_type"]
        key = {"episode": "episodes", "agent_case": "agent_cases"}[kind]
        return httpx.Response(200, json={"request_id": "t", "data": {key: self.rows[kind]}})

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def _identity(**kw) -> EverosIdentity:
    return EverosIdentity(
        user_id=kw.get("user_id", "raven-code"),
        agent_id=kw.get("agent_id", "raven-code"),
        base_url="http://everos.test",
        session_prefix="cli:",
    )


def test_identity_defaults_the_base_url_to_the_hosts() -> None:
    cfg = SubagentEverosConfig(user_id="raven-code")
    identity = identity_from_config(cfg, "http://localhost:18791")
    assert identity is not None
    assert identity.base_url == "http://localhost:18791"
    assert identity.agent_id is None


def test_identity_keeps_its_own_base_url_when_declared() -> None:
    cfg = SubagentEverosConfig(agent_id="raven-code", base_url="http://elsewhere:9/")
    identity = identity_from_config(cfg, "http://localhost:18791")
    assert identity is not None
    assert identity.base_url == "http://elsewhere:9"


def test_no_config_means_no_identity() -> None:
    assert identity_from_config(None, "http://localhost:18791") is None


@pytest.mark.asyncio
async def test_an_episode_becomes_one_item_of_its_summary() -> None:
    mock = _MockEverOS()
    mock.rows["episode"] = [{"id": "e1", "summary": "Audited the checkout read-only."}]
    async with mock.client() as client:
        items = await collect_memories(client, _identity(), "cli:abc")
    assert items == [MemoryItem(type="episode", text="Audited the checkout read-only.")]


@pytest.mark.asyncio
async def test_a_case_joins_its_intent_and_insight() -> None:
    mock = _MockEverOS()
    mock.rows["agent_case"] = [
        {"id": "c1", "task_intent": "Fix the flaky test", "key_insight": "It raced on the index lock."}
    ]
    async with mock.client() as client:
        items = await collect_memories(client, _identity(user_id=None), "cli:abc")
    assert items == [
        MemoryItem(type="agent_case", text="Fix the flaky test - It raced on the index lock.")
    ]


@pytest.mark.asyncio
async def test_the_query_carries_the_session_id_and_one_owner_per_call() -> None:
    mock = _MockEverOS()
    async with mock.client() as client:
        await collect_memories(client, _identity(), "cli:abc")
    assert len(mock.requests) == 2
    for body in mock.requests:
        assert body["filters"] == {"session_id": "cli:abc"}
        assert ("user_id" in body) != ("agent_id" in body)
    assert {b["memory_type"] for b in mock.requests} == {"episode", "agent_case"}


@pytest.mark.asyncio
async def test_only_the_declared_owner_is_queried() -> None:
    mock = _MockEverOS()
    async with mock.client() as client:
        await collect_memories(client, _identity(agent_id=None), "cli:abc")
    assert [b["memory_type"] for b in mock.requests] == ["episode"]


@pytest.mark.asyncio
async def test_profile_and_skill_are_never_queried() -> None:
    mock = _MockEverOS()
    async with mock.client() as client:
        await collect_memories(client, _identity(), "cli:abc")
    assert not {b["memory_type"] for b in mock.requests} & {"profile", "agent_skill"}


@pytest.mark.asyncio
async def test_long_text_is_capped() -> None:
    mock = _MockEverOS()
    mock.rows["episode"] = [{"id": "e1", "summary": "x" * 900}]
    async with mock.client() as client:
        items = await collect_memories(client, _identity(user_id="u", agent_id=None), "cli:abc")
    assert len(items[0].text) == 500
    assert items[0].text.endswith("...")


@pytest.mark.asyncio
async def test_an_item_with_no_usable_text_is_dropped() -> None:
    mock = _MockEverOS()
    mock.rows["episode"] = [{"id": "e1", "summary": "   "}]
    async with mock.client() as client:
        items = await collect_memories(client, _identity(agent_id=None), "cli:abc")
    assert items == []


@pytest.mark.asyncio
async def test_an_http_error_propagates_to_the_caller() -> None:
    mock = _MockEverOS()
    mock.status = 500
    async with mock.client() as client:
        with pytest.raises(httpx.HTTPStatusError):
            await collect_memories(client, _identity(), "cli:abc")
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_subagent_memory.py -v
```

Expected: collection error, `No module named 'raven.agent.subagent_memory'`.

- [ ] **Step 3: Implement**

Create `raven/agent/subagent_memory.py`:

```python
"""What a sub-agent wrote into everos during one call.

A sub-agent running on the everos memory backend writes into a store the host
never sees. The host holds the call's prompt and output; everos holds what the
sub-agent concluded from it. This module joins the two.

The join needs no cooperation from the sub-agent: every Raven fork passes the
host-minted ``{agent_id}`` through to its own Raven as ``--session cli:<id>``,
and that session id lands on every memory everos extracts from the call. The
host mints that id and keeps it in ``InstanceRegistry``, so one filtered read
of ``/api/v2/memory/get`` answers "what did this sub-agent write here".

The file this produces is read by *another sub-agent*, not by a human auditor,
so it carries text and nothing else. Identity, session id, timings and item ids
are diagnostics: they go to the log, where they cost a reader nothing.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from loguru import logger

_TEXT_CAP = 500
_PAGE_SIZE = 100
_HTTP_TIMEOUT_S = 30.0

# episode is user-owned, agent_case is agent-owned; the endpoint takes exactly
# one owner per call, so each is asked for separately.
#
# profile and agent_skill are deliberately absent. They accumulate across calls
# -- they describe what a sub-agent *is*, not what it just did -- so including
# them would dilute the one signal the reader came for.
_OWNED: tuple[tuple[str, str, str], ...] = (
    ("user_id", "episode", "episodes"),
    ("agent_id", "agent_case", "agent_cases"),
)


@dataclass(frozen=True)
class EverosIdentity:
    """How the host addresses one sub-agent's memories."""

    user_id: str | None
    agent_id: str | None
    base_url: str
    session_prefix: str


@dataclass(frozen=True)
class MemoryItem:
    """One memory, as the record carries it."""

    type: str
    text: str


def identity_from_config(cfg: Any, default_base_url: str) -> EverosIdentity | None:
    """Read a sub-agent's declared everos identity, or ``None`` if it has none.

    Args:
        cfg (`SubagentEverosConfig | None`):
            The agent's declared block.
        default_base_url (`str`):
            The host's own everos base url, used when the block names none.

    Returns:
        `EverosIdentity | None`:
            The identity, or ``None`` when nothing was declared.
    """
    if cfg is None:
        return None
    base = (getattr(cfg, "base_url", None) or default_base_url).rstrip("/")
    return EverosIdentity(
        user_id=getattr(cfg, "user_id", None),
        agent_id=getattr(cfg, "agent_id", None),
        base_url=base,
        session_prefix=getattr(cfg, "session_prefix", "cli:"),
    )


def _cap(text: str) -> str:
    collapsed = " ".join(str(text or "").split())
    if len(collapsed) <= _TEXT_CAP:
        return collapsed
    return collapsed[: _TEXT_CAP - 3] + "..."


def _text_of(memory_type: str, row: dict) -> str:
    if memory_type == "episode":
        return _cap(row.get("summary") or row.get("subject") or "")
    intent = str(row.get("task_intent") or "").strip()
    insight = str(row.get("key_insight") or "").strip()
    return _cap(f"{intent} - {insight}" if intent and insight else intent or insight)


async def collect_memories(
    client: httpx.AsyncClient,
    identity: EverosIdentity,
    session_id: str,
) -> list[MemoryItem]:
    """Every memory everos holds for this identity under ``session_id``.

    Raises whatever httpx raises: the caller decides what an unreachable everos
    means for the record.

    Args:
        client (`httpx.AsyncClient`):
            Client to talk to everos with.
        identity (`EverosIdentity`):
            Whose memories to read.
        session_id (`str`):
            The join key, already prefixed.

    Returns:
        `list[MemoryItem]`:
            Items with usable text, episodes first.
    """
    items: list[MemoryItem] = []
    for owner_key, memory_type, data_key in _OWNED:
        owner_id = getattr(identity, owner_key)
        if not owner_id:
            continue
        response = await client.post(
            f"{identity.base_url}/api/v2/memory/get",
            json={
                owner_key: owner_id,
                "memory_type": memory_type,
                "filters": {"session_id": session_id},
                "page_size": _PAGE_SIZE,
            },
            timeout=_HTTP_TIMEOUT_S,
        )
        response.raise_for_status()
        data = (response.json() or {}).get("data") or {}
        for row in data.get(data_key) or []:
            if not isinstance(row, dict):
                continue
            text = _text_of(memory_type, row)
            if text:
                items.append(MemoryItem(type=memory_type, text=text))
    return items
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_subagent_memory.py -v
```

Expected: 10 passed.

- [ ] **Step 5: Commit (only on the user's word)**

```bash
git add raven/agent/subagent_memory.py tests/test_subagent_memory.py
git commit -m "$(cat <<'EOF'
feat(agent): read a sub-agent's everos memories by its call session id

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Write the record, with polling and honest statuses

**Files:**
- Modify: `raven/agent/subagent_memory.py`
- Modify: `CONTEXT.md` (after the **Subagent history** entry ending at line 612)
- Test: `tests/test_subagent_memory.py`

**Interfaces:**
- Consumes: `collect_memories`, `EverosIdentity`, `MemoryItem` from Task 2.
- Produces:
  ```python
  async def record_memories(
      *,
      agent: str,
      identity: EverosIdentity,
      resolve_session_id: Callable[[], Awaitable[str | None]],
      write: Callable[[str], Awaitable[None]],
      budget_s: float = 60.0,
      client: httpx.AsyncClient | None = None,
  ) -> None
  ```
  Tasks 4 and 5 call exactly this. `write` receives the finished JSON text; the
  caller closes over where it goes, which is what lets the DAG path write
  through its workspace backend while the other two write a local file.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_memory.py`:

```python
from raven.agent.subagent_memory import record_memories


def _sink() -> tuple[list[str], "Callable"]:
    written: list[str] = []

    async def write(text: str) -> None:
        written.append(text)

    return written, write


async def _key(value: str | None = "cli:abc"):
    return value


@pytest.mark.asyncio
async def test_a_found_memory_is_recorded_as_settled() -> None:
    mock = _MockEverOS()
    mock.rows["episode"] = [{"id": "e1", "summary": "Ran the audit."}]
    written, write = _sink()
    async with mock.client() as client:
        await record_memories(
            agent="Raven-Code",
            identity=_identity(agent_id=None),
            resolve_session_id=lambda: _key(),
            write=write,
            budget_s=0.0,
            client=client,
        )
    payload = json.loads(written[0])
    assert payload == {
        "agent": "Raven-Code",
        "status": "settled",
        "memories": [{"type": "episode", "text": "Ran the audit."}],
    }


@pytest.mark.asyncio
async def test_nothing_found_within_the_budget_is_pending() -> None:
    mock = _MockEverOS()
    written, write = _sink()
    async with mock.client() as client:
        await record_memories(
            agent="Raven-Code",
            identity=_identity(),
            resolve_session_id=lambda: _key(),
            write=write,
            budget_s=0.0,
            client=client,
        )
    payload = json.loads(written[0])
    assert payload["status"] == "pending"
    assert payload["memories"] == []


@pytest.mark.asyncio
async def test_an_unreachable_everos_is_unavailable_not_a_raise() -> None:
    mock = _MockEverOS()
    mock.status = 500
    written, write = _sink()
    async with mock.client() as client:
        await record_memories(
            agent="Raven-Code",
            identity=_identity(),
            resolve_session_id=lambda: _key(),
            write=write,
            budget_s=0.0,
            client=client,
        )
    assert json.loads(written[0])["status"] == "unavailable"


@pytest.mark.asyncio
async def test_no_join_key_writes_no_file_at_all() -> None:
    mock = _MockEverOS()
    written, write = _sink()
    async with mock.client() as client:
        await record_memories(
            agent="Raven-Code",
            identity=_identity(),
            resolve_session_id=lambda: _key(None),
            write=write,
            budget_s=0.0,
            client=client,
        )
    assert written == []
    assert mock.requests == []


@pytest.mark.asyncio
async def test_polling_stops_once_the_result_stops_growing() -> None:
    mock = _MockEverOS()
    mock.rows["episode"] = [{"id": "e1", "summary": "First."}]
    written, write = _sink()
    async with mock.client() as client:
        await record_memories(
            agent="Raven-Code",
            identity=_identity(agent_id=None),
            resolve_session_id=lambda: _key(),
            write=write,
            budget_s=5.0,
            client=client,
        )
    # One poll finds it, a second confirms it stopped growing. No third.
    assert len(mock.requests) == 2
    assert json.loads(written[0])["status"] == "settled"


@pytest.mark.asyncio
async def test_a_failing_write_never_raises_at_the_caller() -> None:
    mock = _MockEverOS()

    async def write(_: str) -> None:
        raise OSError("read-only file system")

    async with mock.client() as client:
        await record_memories(
            agent="Raven-Code",
            identity=_identity(),
            resolve_session_id=lambda: _key(),
            write=write,
            budget_s=0.0,
            client=client,
        )
```

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_subagent_memory.py -k record -v
```

Expected: FAIL, `cannot import name 'record_memories'`.

- [ ] **Step 3: Implement**

Add `import asyncio` to the module's imports (Task 2 left it out because
nothing there used it; `_poll` below is its first use).

Append to `raven/agent/subagent_memory.py`:

```python
_DEFAULT_BUDGET_S = 60.0
_BACKOFF_S = (2.0, 4.0, 8.0, 16.0, 30.0)

SETTLED = "settled"
PENDING = "pending"
UNAVAILABLE = "unavailable"


def _delays(budget_s: float) -> list[float]:
    """Backoff steps that fit the budget, always at least one immediate look."""
    delays: list[float] = [0.0]
    spent = 0.0
    for step in _BACKOFF_S:
        if spent + step > budget_s:
            break
        delays.append(step)
        spent += step
    while spent + _BACKOFF_S[-1] <= budget_s:
        delays.append(_BACKOFF_S[-1])
        spent += _BACKOFF_S[-1]
    return delays


async def _poll(
    client: httpx.AsyncClient,
    identity: EverosIdentity,
    session_id: str,
    budget_s: float,
) -> tuple[list[MemoryItem], str]:
    """Look until the result stops growing, or the budget is spent.

    everos extracts asynchronously (it runs an LLM), so the first look after a
    call usually finds nothing. Growth stopping is the signal that extraction
    finished, which is why this cannot just take one snapshot.
    """
    found: list[MemoryItem] = []
    for delay in _delays(budget_s):
        if delay:
            await asyncio.sleep(delay)
        current = await collect_memories(client, identity, session_id)
        if current and len(current) == len(found):
            return current, SETTLED
        found = current
    return (found, SETTLED) if found else ([], PENDING)


async def record_memories(
    *,
    agent: str,
    identity: EverosIdentity,
    resolve_session_id: Callable[[], Awaitable[str | None]],
    write: Callable[[str], Awaitable[None]],
    budget_s: float = _DEFAULT_BUDGET_S,
    client: httpx.AsyncClient | None = None,
) -> None:
    """Write one call's Memory record.

    Never raises. This is an audit trail written after the call it describes
    has already answered, and losing it must not disturb anything.

    ``resolve_session_id`` is called here rather than before dispatch because
    the registry only holds the instance's id once the backend has committed
    it, which happens when the run ends.

    Args:
        agent (`str`):
            The sub-agent's configured name, recorded verbatim.
        identity (`EverosIdentity`):
            Whose memories to read.
        resolve_session_id (`Callable[[], Awaitable[str | None]]`):
            Yields the join key, or ``None`` when this call has none (a
            stateless agent, or an id that was never read back). ``None``
            writes no file: there is nothing truthful to say.
        write (`Callable[[str], Awaitable[None]]`):
            Receives the record's JSON text.
        budget_s (`float`):
            How long to keep looking for a memory everos has not extracted yet.
        client (`httpx.AsyncClient | None`):
            Injected in tests; otherwise one is built and closed here.
    """
    owned = client is None
    http = client or httpx.AsyncClient(timeout=httpx.Timeout(_HTTP_TIMEOUT_S))
    try:
        try:
            session_id = await resolve_session_id()
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.warning("Memory record for {} could not resolve its join key: {}", agent, exc)
            return
        if not session_id:
            logger.debug("Memory record for {} skipped: no instance id to join on", agent)
            return
        try:
            items, status = await _poll(http, identity, session_id, budget_s)
        except Exception as exc:  # noqa: BLE001 - an unreachable everos is a status, not a failure
            logger.warning("Memory record for {} could not read everos at {}: {}", agent, identity.base_url, exc)
            items, status = [], UNAVAILABLE
        payload = {
            "agent": agent,
            "status": status,
            "memories": [{"type": item.type, "text": item.text} for item in items],
        }
        logger.debug(
            "Memory record for {} ({}): {} item(s) under {} at {}",
            agent,
            status,
            len(items),
            session_id,
            identity.base_url,
        )
        try:
            await write(json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception as exc:  # noqa: BLE001 - see the docstring
            logger.warning("Memory record for {} could not be written: {}", agent, exc)
    finally:
        if owned:
            await http.aclose()
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_subagent_memory.py -v
```

Expected: 16 passed.

- [ ] **Step 5: Define the domain term**

In `CONTEXT.md`, insert after the **Subagent history** entry (which ends with the
`_Avoid_:` line at 612) and before **Handoff Block**:

```markdown
**Memory record** (`raven/agent/subagent_memory.py`):
What one Subagent wrote into everos during one call, written beside that call's own
`prompt.md` / `out.md` inside Subagent history: `memory.json` for a `spawn` call and a
Direct Chat turn, `<node>.memory.json` for a DAG node. Holds the sub-agent's name, a
`status`, and a list of `{type, text}` items -- `episode` (its `summary`) and `agent_case`
(its `task_intent` and `key_insight`), capped at 500 characters each. Its reader is another
sub-agent asking what this one did, so it carries text and nothing else; identity, session
id and item ids are logged, not recorded. `status` distinguishes the three outcomes an
empty list would otherwise conflate: `settled` (the sub-agent wrote nothing), `pending`
(everos had not finished extracting within the poll budget), `unavailable` (everos could
not be read). Produced only for an agent whose config declares an everos identity; the
join key is `<sessionPrefix><instance agent id>`, the id the host mints and the fork
passes on to its own Raven. Attribution is per *instance*, not per call: a memory everos
extracts late can appear in two consecutive records.
_Avoid_: "memory trace" -- an earlier name for the recorder, from a draft where it also
captured the call's time window.
```

- [ ] **Step 6: Commit (only on the user's word)**

```bash
git add raven/agent/subagent_memory.py tests/test_subagent_memory.py CONTEXT.md
git commit -m "$(cat <<'EOF'
feat(agent): write a sub-agent's memory record after a call settles

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Wire the spawn and direct-chat paths

**Files:**
- Modify: `raven/agent/subagent/manager.py`
- Test: `tests/test_subagent_history.py`

**Interfaces:**
- Consumes: `identity_from_config`, `record_memories`, `EverosIdentity` from Tasks 2-3.
- Produces: `SubagentManager.everos_identity(agent: str | None) -> EverosIdentity | None` (Task 5 passes this into the DAG runner) and the private `SubagentManager._schedule_memory_record(...)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_history.py`:

```python
@pytest.mark.asyncio
async def test_a_declared_identity_becomes_a_memory_record(tmp_path, monkeypatch) -> None:
    from raven.agent.subagent import manager as manager_mod

    calls: list[dict] = []

    async def _fake_record(**kwargs) -> None:
        calls.append(kwargs)
        await kwargs["write"]('{"agent": "Raven-Code", "status": "settled", "memories": []}')

    monkeypatch.setattr(manager_mod, "record_memories", _fake_record)

    d = _session_dir(tmp_path, "web:abc")
    record = SpawnRecord.open(d, task_id="t1", task="ask", meta={"agent": "Raven-Code"})
    record.finish(status="completed", output="done")

    await manager_mod.write_memory_record_for(
        directory=record.dir,
        filename="memory.json",
        agent="Raven-Code",
        identity=manager_mod.EverosIdentity(
            user_id="raven-code", agent_id=None, base_url="http://everos.test", session_prefix="cli:"
        ),
        resolve_session_id=_noop_key,
        budget_s=0.0,
    )

    assert json.loads((record.dir / "memory.json").read_text(encoding="utf-8"))["agent"] == "Raven-Code"
    assert calls and calls[0]["agent"] == "Raven-Code"


async def _noop_key() -> str:
    return "cli:abc"


def test_no_declared_identity_means_no_memory_record() -> None:
    from raven.agent.subagent.manager import SubagentManager

    assert SubagentManager.everos_identity_from(None, "http://localhost:18791") is None
```

Add `import json` and `import pytest` to that file's imports if absent.

- [ ] **Step 2: Run the tests to verify they fail**

```bash
uv run pytest tests/test_subagent_history.py -k memory -v
```

Expected: FAIL, `write_memory_record_for` / `everos_identity_from` do not exist.

- [ ] **Step 3: Implement**

In `raven/agent/subagent/manager.py`, add to the imports:

```python
from raven.agent.subagent_memory import EverosIdentity, identity_from_config, record_memories
```

Add a module-level helper (near `_write_spawn_status`, which it mirrors):

```python
async def write_memory_record_for(
    *,
    directory: Path,
    filename: str,
    agent: str,
    identity: EverosIdentity,
    resolve_session_id,
    budget_s: float | None = None,
) -> None:
    """Write one call's Memory record into a local record directory."""

    async def _write(text: str) -> None:
        (directory / filename).write_text(text, encoding="utf-8")

    kwargs = {"budget_s": budget_s} if budget_s is not None else {}
    await record_memories(
        agent=agent,
        identity=identity,
        resolve_session_id=resolve_session_id,
        write=_write,
        **kwargs,
    )
```

In `SubagentManager.__init__`, beside the other per-config state:

```python
        self._everos_identities: dict[str, EverosIdentity] = {}
```

Add the static resolver and the accessor:

```python
    @staticmethod
    def everos_identity_from(cfg: Any, default_base_url: str) -> EverosIdentity | None:
        """This agent's declared everos identity, or ``None``."""
        return identity_from_config(cfg, default_base_url)

    def everos_identity(self, agent: str | None) -> EverosIdentity | None:
        """The declared identity for ``agent``, or ``None`` when it declared none."""
        return self._everos_identities.get(agent or "")
```

In `add_third_party_subagent`, alongside the existing `backends` / `meta` build
(the identity map is rebuilt with them so a hot config change applies to it too):

```python
        identities: dict[str, EverosIdentity] = {}
```

inside the `for cfg in enabled_third_party(configs):` loop, after
`meta.append(...)`:

```python
                if (identity := identity_from_config(getattr(cfg, "everos", None), _host_everos_base_url())) :
                    identities[name] = identity
```

and after `self._third_party_meta = meta`:

```python
        self._everos_identities = identities
```

Add the host base-url reader near the module's other helpers:

```python
def _host_everos_base_url() -> str:
    """The host's own everos service, used by any sub-agent that names none."""
    from raven.config.raven import load_raven_config

    try:
        cfg = load_raven_config().plugins.config.get("everos-memory") or {}
        return str(cfg.get("base_url") or "http://localhost:18791")
    except Exception:  # noqa: BLE001 - a missing plugin config must not sink the manager
        return "http://localhost:18791"
```

Add the scheduling helper on the manager:

```python
    def _schedule_memory_record(
        self,
        *,
        task_id: str,
        agent: str | None,
        handle: str,
        session_key: str | None,
        directory: Path,
        filename: str,
    ) -> None:
        """Record what this call wrote into everos, in the background.

        Never awaited by the dispatch path: everos extraction runs an LLM, and a
        sub-agent's reply must not wait on the host's bookkeeping.
        """
        identity = self.everos_identity(agent)
        if identity is None:
            return

        async def _resolve() -> str | None:
            agent_id = await get_registry().lookup(session_key or "", agent or "", handle)
            return f"{identity.session_prefix}{agent_id}" if agent_id else None

        task = asyncio.create_task(
            write_memory_record_for(
                directory=directory,
                filename=filename,
                agent=agent or "",
                identity=identity,
                resolve_session_id=_resolve,
            )
        )
        self._track(f"{task_id}:memory", task, session_key)
```

Wire the spawn path. The `try` whose branches call `record.finish(...)` ends at
line 684; give it a `finally` so all four outcomes are covered by one site:

```python
        finally:
            self._schedule_memory_record(
                task_id=task_id,
                agent=agent,
                handle=handle,
                session_key=session_key,
                directory=record.dir,
                filename="memory.json",
            )
```

Wire the direct-chat path the same way. The `try` at line 435 has three
`record.finish(...)` branches; add, inside the `async with` block:

```python
            finally:
                self._schedule_memory_record(
                    task_id=task_id,
                    agent=agent,
                    handle=handle,
                    session_key=session_key,
                    directory=record.dir,
                    filename="memory.json",
                )
```

- [ ] **Step 4: Run the tests to verify they pass**

```bash
uv run pytest tests/test_subagent_history.py -v
```

Expected: all pass, including the two new cases.

- [ ] **Step 5: Check the paths still behave**

```bash
uv run pytest tests/test_subagent_history.py tests/test_subagent_direct_chat.py tests/test_subagent_third_party.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit (only on the user's word)**

```bash
git add raven/agent/subagent/manager.py tests/test_subagent_history.py
git commit -m "$(cat <<'EOF'
feat(agent): record everos memories for spawn and direct chat calls

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Wire the DAG path

**Files:**
- Modify: `raven/agent/subagent_dag/runner.py` (`_run_node:459-545`, and the `run_dag`/`_run_group` parameter chain)
- Modify: `raven/agent/subagent_dag/tool.py:609` (where `run_dag` is called)
- Test: `tests/test_subagent_dag_runner.py`

**Interfaces:**
- Consumes: `record_memories` (Task 3), `SubagentManager.everos_identity` (Task 4).
- Produces: `run_dag(..., everos_for: Callable[[str], EverosIdentity | None] | None = None)`, threaded to `_run_node` exactly as the existing `state_for` callable is.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_subagent_dag_runner.py`:

```python
@pytest.mark.asyncio
async def test_a_node_leaves_a_memory_record(tmp_path, monkeypatch) -> None:
    from raven.agent.subagent_dag import runner as runner_mod
    from raven.agent.subagent_memory import EverosIdentity

    async def _fake_record(**kwargs) -> None:
        await kwargs["write"]('{"agent": "Raven-Code", "status": "settled", "memories": []}')

    monkeypatch.setattr(runner_mod, "record_memories", _fake_record)

    identity = EverosIdentity(
        user_id="raven-code", agent_id=None, base_url="http://everos.test", session_prefix="cli:"
    )
    store = await _run_one_node_dag(
        tmp_path,
        everos_for=lambda _name: identity,
    )
    written = Path(store.run_dir) / "n1.memory.json"
    assert json.loads(written.read_text(encoding="utf-8"))["status"] == "settled"
```

Build `_run_one_node_dag` on the file's existing single-node DAG fixture, passing
`everos_for` straight through to `run_dag` and returning the store. Reuse
whatever backend stub the neighbouring tests already use rather than adding one.

- [ ] **Step 2: Run the test to verify it fails**

```bash
uv run pytest tests/test_subagent_dag_runner.py -k memory_record -v
```

Expected: FAIL, `run_dag() got an unexpected keyword argument 'everos_for'`.

- [ ] **Step 3: Implement**

In `raven/agent/subagent_dag/runner.py`, import both:

```python
from raven.agent.subagent.instances import get_registry
from raven.agent.subagent_memory import EverosIdentity, record_memories
```

`get_registry` is what `_record_node_memory` resolves the join key through, and
`runner.py` does not import it today.

Add the parameter to `run_dag`, to `_run_group`, and to `_run_node`, each
beside the existing `state_for` (same position, same optional shape):

```python
    everos_for: "Callable[[str], EverosIdentity | None] | None" = None,
```

Pass it down at both call sites the way `state_for` is passed.

In `_run_node`, after `await _write_node_status(...)` at line 545 (so it runs for
a completed *and* a failed node, matching the other three paths) — and OUTSIDE
the `async with semaphore:` block, so a finished node stops holding a
concurrency slot while the recorder polls:

```python
    identity = everos_for(node.subagent) if everos_for is not None else None
    if identity is not None:
        task = asyncio.create_task(
            _record_node_memory(node, store=store, identity=identity, session_key=session_key)
        )
        _RECORD_TASKS.add(task)
        task.add_done_callback(_RECORD_TASKS.discard)
```

with, at module level:

```python
# asyncio holds only a weak reference to a running task, so a fire-and-forget
# record has to be kept alive by its scheduler until it finishes.
_RECORD_TASKS: set[asyncio.Task] = set()
```

`run_dag` must NOT await, gather, or time out on `_RECORD_TASKS`. The spec is
explicit that the record is never awaited by the dispatch path: the recorder
polls for up to 60s, and both the node's concurrency slot and the run's summary
would otherwise wait on it.

Add the helper beside `_write_node_status`, which it mirrors:

```python
async def _record_node_memory(
    node: DagNodeSpec,
    *,
    store: DagRunStore,
    identity: EverosIdentity,
    session_key: str | None,
) -> None:
    """Write this node's Memory record, swallowing any failure.

    Written through the store rather than to a local path: a DAG run's files go
    to the session's workspace backend, which may not be this filesystem.
    """

    async def _resolve() -> str | None:
        # `instance or id` mirrors CliAgentBackend.run's own derivation
        # (cli_agent.py:368), which is the handle the registry row was
        # committed under. A node naming no instance still has a row, keyed by
        # its node id, so keying on `instance` alone would drop the record for
        # the common case.
        handle = node.instance or node.id
        agent_id = await get_registry().lookup(session_key or "", node.subagent, handle)
        return f"{identity.session_prefix}{agent_id}" if agent_id else None

    async def _write(text: str) -> None:
        await store.write_text(store._backend.join_path(store.run_dir, f"{node.id}.memory.json"), text)

    try:
        await record_memories(
            agent=node.subagent,
            identity=identity,
            resolve_session_id=_resolve,
            write=_write,
        )
    except Exception:  # noqa: BLE001 - a record must never fail a node
        logger.opt(exception=True).warning("Memory record for DAG node {} failed", node.id)
```

Replace `store._backend.join_path(...)` with a public accessor if `DagRunStore`
grows one; today the store exposes `run_dir` but no join helper, so add one
rather than reaching into `_backend`:

```python
    def memory_path(self, node_id: str) -> str:
        """Path of a node's Memory record: ``<run_dir>/<node_id>.memory.json``."""
        return self._backend.join_path(self.run_dir, f"{node_id}.memory.json")
```

in `raven/agent/subagent_dag/_store.py` beside `output_path`, and call
`store.memory_path(node.id)` from the helper.

In `raven/agent/subagent_dag/tool.py`, at the `run_dag(...)` call around line
609, pass the manager's accessor:

```python
            everos_for=self._manager.everos_identity,
```

Match the attribute name the tool already uses for the manager.

- [ ] **Step 4: Run the test to verify it passes**

```bash
uv run pytest tests/test_subagent_dag_runner.py -k memory_record -v
```

Expected: PASS.

- [ ] **Step 5: Run the full affected suite**

```bash
uv run pytest tests/test_subagent_dag_runner.py tests/test_subagent_dag_core.py tests/test_rpc_dag.py \
  tests/test_subagent_history.py tests/test_subagent_memory.py tests/test_subagent_direct_chat.py \
  tests/test_subagent_third_party.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit (only on the user's word)**

```bash
git add raven/agent/subagent_dag/runner.py raven/agent/subagent_dag/_store.py \
        raven/agent/subagent_dag/tool.py tests/test_subagent_dag_runner.py
git commit -m "$(cat <<'EOF'
feat(agent): record everos memories for each dag node

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: End-to-end check against the live everos

**Files:** none changed. This is a verification task.

- [ ] **Step 1: Declare an identity on a real fork**

Add to the `Raven-Code` entry in `~/.raven/config.json` via the supported API
(never by hand -- see the project's install notes):

```python
uv run python -c "
from raven.config.update_subagents import get_agents, set_agents
entries = get_agents()
for e in entries:
    if e['name'] == 'Raven-Code':
        e['everos'] = {'userId': 'raven-code', 'agentId': 'raven-code'}
set_agents(entries)
print('ok')
"
```

- [ ] **Step 2: Run one real call**

Spawn `Raven-Code` once from a session (any short task), then find the record:

```bash
find /root/.raven/workspace/sessions -name 'memory.json' -newermt '-10 minutes' | head
```

- [ ] **Step 3: Confirm the record**

Expected: a `memory.json` beside that call's `prompt.md` / `out.md`, with
`status` of `settled` (with items) or `pending`. `unavailable` means everos was
unreachable; check `curl -s localhost:18791/health`.

- [ ] **Step 4: Confirm the opt-out**

Spawn `Coder` (an ACP agent with no everos block) and confirm its record
directory has no `memory.json`.

- [ ] **Step 5: Report the result**

Report which statuses were observed and how long extraction actually took. If
`pending` dominates, the 60s budget is the thing to raise -- it is config, not
code.

---

## Self-review

**Spec coverage.** Section 1 config interface -> Task 1. Section 2 join key ->
Tasks 3 (resolver contract) and 4/5 (registry lookup). Section 3 recorder ->
Tasks 2-3, with the `open`/`settle` collapse recorded under "Deviation from the
spec". Section 4 record file -> Task 3. Section 5 wiring, all three paths ->
Tasks 4-5. Section 6 failure behaviour -> Task 3's four status tests plus the
swallowed-write test. Testing section -> Tasks 2-5. Domain terms -> Task 3 Step
5. The spec's open assumption (60s budget) is carried into Task 6 Step 5 as
something to measure rather than guess.

**Naming consistency.** `EverosIdentity`, `MemoryItem`, `identity_from_config`,
`collect_memories`, `record_memories`, `write_memory_record_for`,
`everos_identity`, `_schedule_memory_record`, `memory_path`, and the status
constants `SETTLED` / `PENDING` / `UNAVAILABLE` are each defined once and used
under that exact name everywhere later.

**Known soft spots for the executor.** Task 5 Step 1 leans on the existing
single-node DAG fixture in `tests/test_subagent_dag_runner.py` rather than
inventing one; read that file before writing the test. Task 5 Step 3's
`tool.py` edit needs the manager attribute name that file actually uses. Task 4
Step 3 places two `finally` blocks -- confirm the indentation lands on the
`try` that owns the `record.finish` calls, not an inner one.
