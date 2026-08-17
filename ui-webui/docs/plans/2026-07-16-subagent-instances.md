# Stateful Sub-Agent Instances + Prototype Sidebar + Instance Monitor — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a single CLI sub-agent *prototype* be instantiated into multiple stateful *instances* whose context survives across calls (Claude Code: `--session-id` then `--resume`), edited from a new left sidebar and monitored in the right dock.

**Architecture:** Additive change. `CliSubAgentConfig` gains an optional `resume_command`; when set the prototype is *stateful*. The per-prototype `CliSubAgentTool` then exposes an `instance` handle and, per call, resolves `handle → agent_id(uuid)` against a per-session Redis registry to decide CREATE (`command`) vs RESUME (`resume_command`), attaching `{instance, agent_id, action}` to its result metadata. Frontend: a new left `SubagentSidebar` owns prototype CRUD; the right dock becomes a transcript-derived `SubagentInstanceMonitor`; stateful tool calls get a `[handle · new/resumed]` badge.

**Tech Stack:** Python 3.11 (pydantic v2, FastAPI, redis.asyncio, pytest/unittest), React + TypeScript + Vite + shadcn UI (`tsc -b` typecheck, eslint; **no JS test runner**).

## Global Constraints

- **Encapsulation:** internal files/classes/functions are `_`-prefixed; expose only through `__init__.py`. (CLAUDE.md)
- **Lazy imports:** third-party libs imported at point of use, not file top. (CLAUDE.md)
- **Docstrings:** English only, strict `Args:`/`Returns:` template with backtick-typed params. (CLAUDE.md)
- **black line length 79**, flake8, pylint, mypy, docstring checks via `pre-commit run --all-files`. Do not skip hooks. (CLAUDE.md)
- **Tests:** assertions compare the **whole** data structure; use `AnyString`/`AnyValue` from `tests/utils.py` for nondeterministic fields (uuids). (CLAUDE.md)
- **Command-template placeholders:** `{prompt}` (existing) and `{agent_id}` (new, underscore).
- **Error noun:** sub-agent execution error strings use the noun **"Sub-Agent"** (existing convention — keep exactly).
- **Backend test command:** `pytest tests/<file>_test.py -v`. **Frontend verify:** `pnpm -C examples/web_ui/frontend build` (runs `tsc -b && vite build`) and `pnpm -C examples/web_ui/frontend lint`.
- **Backward compatibility:** a prototype with no `resume_command` must behave exactly as today (schema `{prompt}` only, stateless run). No data migration.

**Before you start (once, not per task):** this is unrelated to the current `feat/editable-credential-models` WIP. Create a dedicated branch off `main`:
```bash
git switch -c feat/subagent-instances main
```
Reference spec: `docs/superpowers/specs/2026-07-16-subagent-instances-design.md`.

---

## Task 1: Add `resume_command` field + validators to the prototype

**Files:**
- Modify: `src/agentscope/subagent/_base.py`
- Test: `tests/subagent_factory_test.py` (extend existing)

**Interfaces:**
- Produces: `CliSubAgentConfig.resume_command: str | None` (default `None`). When non-empty, both `command` and `resume_command` must contain `{prompt}` and `{agent_id}` and have a literal first token.

- [ ] **Step 1: Update the existing round-trip test for the new field, and add stateful-validator tests**

In `tests/subagent_factory_test.py`, the existing `test_from_dict_round_trip` asserts the whole `model_dump()`. Add `"resume_command": None` to its expected dict (insert right after the `"command"` line):

```python
        self.assertEqual(
            config.model_dump(),
            {
                "id": AnyString(),
                "type": "cli_subagent",
                "name": "claude_code",
                "description": "Delegate coding.",
                "command": "claude -p {prompt}",
                "resume_command": None,
                "cwd": None,
                "env": None,
                "timeout": 600,
            },
        )
```

Then append these tests to the `SubAgentFactoryTest` class:

```python
    def test_stateful_round_trip(self) -> None:
        """A stateful config keeps both command templates."""
        config = SubAgentFactory.from_dict(
            {
                "type": "cli_subagent",
                "name": "claude_code",
                "description": "Delegate coding.",
                "command": "claude -p {prompt} --session-id {agent_id}",
                "resume_command": "claude -p {prompt} --resume {agent_id}",
            },
        )
        self.assertEqual(
            config.model_dump(),
            {
                "id": AnyString(),
                "type": "cli_subagent",
                "name": "claude_code",
                "description": "Delegate coding.",
                "command": "claude -p {prompt} --session-id {agent_id}",
                "resume_command": "claude -p {prompt} --resume {agent_id}",
                "cwd": None,
                "env": None,
                "timeout": 600,
            },
        )

    def test_stateful_requires_agent_id_in_command(self) -> None:
        """resume_command set but command missing {agent_id} → error."""
        with self.assertRaises(ValueError):
            SubAgentFactory.from_dict(
                {
                    "type": "cli_subagent",
                    "name": "x",
                    "description": "x",
                    "command": "claude -p {prompt}",
                    "resume_command": "claude -p {prompt} --resume {agent_id}",
                },
            )

    def test_stateful_requires_placeholders_in_resume(self) -> None:
        """resume_command missing {prompt}/{agent_id} → error."""
        with self.assertRaises(ValueError):
            SubAgentFactory.from_dict(
                {
                    "type": "cli_subagent",
                    "name": "x",
                    "description": "x",
                    "command": "claude -p {prompt} --session-id {agent_id}",
                    "resume_command": "claude --resume",
                },
            )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/subagent_factory_test.py -v`
Expected: FAIL — `test_from_dict_round_trip` fails on the dict mismatch (no `resume_command` key), and the three new tests fail (`resume_command` unknown / no validation yet).

- [ ] **Step 3: Add the field and validators**

In `src/agentscope/subagent/_base.py`, add the field right after the `command` field's docstring (after line 62, before `cwd`):

```python
    resume_command: str | None = Field(
        default=None,
        description=(
            "Optional command template used to RESUME an existing "
            "instance. When set, the prototype is stateful: the first "
            "call to an instance handle runs `command` (create) and "
            "later calls run this template (resume). Both `command` and "
            "`resume_command` must then contain '{prompt}' and "
            "'{agent_id}'. When empty, the prototype is stateless."
        ),
    )
    """The optional resume command template containing ``{prompt}`` and
    ``{agent_id}``; presence makes the prototype stateful."""
```

Then add a model validator at the end of the class (after `_require_literal_executable`). It needs `model_validator`, so update the pydantic import at the top of the file:

```python
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)
```

Append to the class:

```python
    @model_validator(mode="after")
    def _validate_stateful(self) -> "CliSubAgentConfig":
        """Validate the create/resume template pair when stateful.

        When ``resume_command`` is set the prototype is stateful, so both
        ``command`` and ``resume_command`` must carry ``{prompt}`` and
        ``{agent_id}`` and start with a literal executable.

        Returns:
            `CliSubAgentConfig`:
                The validated config instance.
        """
        if not self.resume_command:
            return self
        for label, template in (
            ("command", self.command),
            ("resume_command", self.resume_command),
        ):
            for placeholder in ("{prompt}", "{agent_id}"):
                if placeholder not in template:
                    raise ValueError(
                        f"stateful {label} must contain the "
                        f"'{placeholder}' placeholder",
                    )
            try:
                tokens = shlex.split(template)
            except ValueError as error:
                raise ValueError(
                    f"{label} is not valid shell syntax: {error}",
                ) from error
            if not tokens or "{prompt}" in tokens[0]:
                raise ValueError(
                    f"{label}'s first token must be a literal executable",
                )
        return self
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/subagent_factory_test.py -v`
Expected: PASS (all tests, including the router-adjacent factory ones).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_base.py tests/subagent_factory_test.py
git commit -m "feat(subagent): add optional resume_command to CliSubAgentConfig"
```

---

## Task 2: Add the `SubAgentInstanceRecord` storage model

**Files:**
- Create: `src/agentscope/app/storage/_model/_subagent_instance.py`
- Modify: `src/agentscope/app/storage/_model/__init__.py`
- Modify: `src/agentscope/app/storage/__init__.py`
- Test: `tests/subagent_instance_storage_test.py` (create)

**Interfaces:**
- Produces: `SubAgentInstanceRecord(_RecordBase)` with fields `session_id: str`, `handle: str`, `agent_id: str`, `prototype_name: str`. Importable as `from agentscope.app.storage import SubAgentInstanceRecord`.

- [ ] **Step 1: Write the failing test**

Create `tests/subagent_instance_storage_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for the sub-agent instance record model."""

import unittest

from agentscope.app.storage import SubAgentInstanceRecord
from utils import AnyString, AnyValue


class SubAgentInstanceRecordTest(unittest.TestCase):
    """Validate the instance record shape."""

    def test_record_fields(self) -> None:
        """The record carries session/handle/agent_id/prototype_name."""
        record = SubAgentInstanceRecord(
            session_id="s1",
            handle="writer",
            agent_id="abc123",
            prototype_name="claude_code",
        )
        self.assertEqual(
            record.model_dump(),
            {
                "id": AnyString(),
                "updated_at": AnyValue(),
                "created_at": AnyValue(),
                "session_id": "s1",
                "handle": "writer",
                "agent_id": "abc123",
                "prototype_name": "claude_code",
            },
        )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/subagent_instance_storage_test.py -v`
Expected: FAIL with `ImportError: cannot import name 'SubAgentInstanceRecord'`.

- [ ] **Step 3: Create the model and export it**

Create `src/agentscope/app/storage/_model/_subagent_instance.py`:

```python
# -*- coding: utf-8 -*-
"""The sub-agent instance record (per-session runtime instance)."""

from pydantic import Field

from ._base import _RecordBase


class SubAgentInstanceRecord(_RecordBase):
    """A stateful sub-agent instance materialized within a session.

    Maps an agent-chosen ``handle`` to the CLI session id (``agent_id``)
    that RavenX generated when the instance was created, so later calls
    can RESUME the same CLI conversation.
    """

    session_id: str
    """The RavenX chat session this instance belongs to."""

    handle: str
    """The agent-chosen instance handle, unique within the session."""

    agent_id: str
    """The uuid used as the CLI session id (``{agent_id}``)."""

    prototype_name: str
    """The ``CliSubAgentConfig.name`` this instance is an instance of."""
```

In `src/agentscope/app/storage/_model/__init__.py`, add the import (next to the `_subagent` import) and the `__all__` entry:

```python
from ._subagent import SubAgentRecord
from ._subagent_instance import SubAgentInstanceRecord
```
Add `"SubAgentInstanceRecord",` to `__all__` (next to `"SubAgentRecord",`).

In `src/agentscope/app/storage/__init__.py`, add `SubAgentInstanceRecord` to the import block from `._model` (next to `SubAgentRecord`) and to `__all__`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/subagent_instance_storage_test.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/app/storage/_model/_subagent_instance.py src/agentscope/app/storage/_model/__init__.py src/agentscope/app/storage/__init__.py tests/subagent_instance_storage_test.py
git commit -m "feat(storage): add SubAgentInstanceRecord model"
```

---

## Task 3: Add per-session instance storage methods (StorageBase + RedisStorage)

**Files:**
- Modify: `src/agentscope/app/storage/_base.py` (add 3 abstract methods)
- Modify: `src/agentscope/app/storage/_redis_storage.py` (KeyConfig + 3 impls)
- Test: `tests/subagent_instance_storage_test.py` (extend)

**Interfaces:**
- Consumes: `SubAgentInstanceRecord` (Task 2).
- Produces, on `StorageBase`/`RedisStorage`:
  - `async def upsert_subagent_instance(session_id, handle, agent_id, prototype_name) -> str` (returns record id)
  - `async def get_subagent_instance(session_id, handle) -> SubAgentInstanceRecord | None`
  - `async def list_subagent_instances(session_id) -> list[SubAgentInstanceRecord]`

`RedisStorage` is the only `StorageBase` subclass (verified), so adding abstract methods is safe.

- [ ] **Step 1: Write the failing test (Redis via fakeredis)**

The suite uses fakeredis-style async clients elsewhere; if unavailable, this test is skipped. Extend `tests/subagent_instance_storage_test.py`:

```python
import asyncio


class RedisInstanceStorageTest(unittest.TestCase):
    """Round-trip instance records through RedisStorage."""

    def _make_storage(self):
        """Build a RedisStorage backed by fakeredis, or skip."""
        try:
            import fakeredis.aioredis as fakeredis
        except ImportError:  # pragma: no cover
            self.skipTest("fakeredis not installed")
        from agentscope.app.storage import RedisStorage

        storage = RedisStorage()
        storage._client = fakeredis.FakeRedis(decode_responses=True)
        return storage

    def test_upsert_get_list(self) -> None:
        """Upsert then get then list, scoped by session."""
        storage = self._make_storage()

        async def _run():
            await storage.upsert_subagent_instance(
                "s1", "writer", "abc123", "claude_code",
            )
            got = await storage.get_subagent_instance("s1", "writer")
            listed = await storage.list_subagent_instances("s1")
            missing = await storage.get_subagent_instance("s1", "nope")
            other_session = await storage.list_subagent_instances("s2")
            return got, listed, missing, other_session

        got, listed, missing, other_session = asyncio.run(_run())
        self.assertEqual(got.agent_id, "abc123")
        self.assertEqual(got.handle, "writer")
        self.assertEqual(len(listed), 1)
        self.assertIsNone(missing)
        self.assertEqual(other_session, [])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/subagent_instance_storage_test.py::RedisInstanceStorageTest -v`
Expected: FAIL with `AttributeError: 'RedisStorage' object has no attribute 'upsert_subagent_instance'` (or SKIP if fakeredis is absent — in that case rely on Step 4's manual reasoning + Task 4/5 tests exercising the registry against a fake).

- [ ] **Step 3a: Add abstract methods to `StorageBase`**

In `src/agentscope/app/storage/_base.py`, first extend the model import (it already imports `SubAgentRecord` around line 20) to also import `SubAgentInstanceRecord`. Then add, immediately after `delete_subagent` (after line 183):

```python
    @abstractmethod
    async def upsert_subagent_instance(
        self,
        session_id: str,
        handle: str,
        agent_id: str,
        prototype_name: str,
    ) -> str:
        """Create or update a per-session sub-agent instance.

        Args:
            session_id (`str`):
                The chat session the instance belongs to.
            handle (`str`):
                The agent-chosen instance handle.
            agent_id (`str`):
                The CLI session id generated at create time.
            prototype_name (`str`):
                The prototype ``name`` this instance is of.

        Returns:
            `str`:
                The id of the created or updated record.
        """

    @abstractmethod
    async def get_subagent_instance(
        self,
        session_id: str,
        handle: str,
    ) -> SubAgentInstanceRecord | None:
        """Fetch one instance by session and handle.

        Args:
            session_id (`str`):
                The chat session id.
            handle (`str`):
                The instance handle.

        Returns:
            `SubAgentInstanceRecord | None`:
                The record, or ``None`` if not found.
        """

    @abstractmethod
    async def list_subagent_instances(
        self,
        session_id: str,
    ) -> list[SubAgentInstanceRecord]:
        """List all instances for a session.

        Args:
            session_id (`str`):
                The chat session id.

        Returns:
            `list[SubAgentInstanceRecord]`:
                All instance records for the session.
        """
```

- [ ] **Step 3b: Add KeyConfig entries + implementations to `RedisStorage`**

In `src/agentscope/app/storage/_redis_storage.py`, extend the model import to also import `SubAgentInstanceRecord` (alongside `SubAgentRecord`). Add to `KeyConfig` (after the `subagent`/`subagent_index` lines, ~line 63/69):

```python
        subagent_instance: str = (
            "agentscope:session:{session_id}:subagent_instance:{handle}"
        )
        subagent_instance_index: str = (
            "agentscope:session:{session_id}:subagent_instances"
        )
```

Add the three methods immediately after `delete_subagent` (after line 519):

```python
    async def upsert_subagent_instance(
        self,
        session_id: str,
        handle: str,
        agent_id: str,
        prototype_name: str,
    ) -> str:
        """Create or update a per-session sub-agent instance record.

        Args:
            session_id (`str`):
                The chat session id.
            handle (`str`):
                The instance handle (unique within the session).
            agent_id (`str`):
                The CLI session id generated at create time.
            prototype_name (`str`):
                The prototype ``name`` this instance is of.

        Returns:
            `str`:
                The id of the created or updated record.
        """
        key = self._key(
            self.key_config.subagent_instance,
            session_id=session_id,
            handle=handle,
        )
        raw = await self._client.get(key)
        if raw:
            record = SubAgentInstanceRecord.model_validate_json(raw)
            record.agent_id = agent_id
            record.prototype_name = prototype_name
            record.updated_at = datetime.now()
        else:
            record = SubAgentInstanceRecord(
                session_id=session_id,
                handle=handle,
                agent_id=agent_id,
                prototype_name=prototype_name,
            )
        index_key = self._key(
            self.key_config.subagent_instance_index,
            session_id=session_id,
        )
        await self._set_with_ttl(key, record.model_dump_json())
        await self._client.sadd(index_key, handle)
        return record.id

    async def get_subagent_instance(
        self,
        session_id: str,
        handle: str,
    ) -> SubAgentInstanceRecord | None:
        """Fetch one instance record by session and handle."""
        key = self._key(
            self.key_config.subagent_instance,
            session_id=session_id,
            handle=handle,
        )
        raw = await self._client.get(key)
        return (
            SubAgentInstanceRecord.model_validate_json(raw) if raw else None
        )

    async def list_subagent_instances(
        self,
        session_id: str,
    ) -> list[SubAgentInstanceRecord]:
        """Return all instance records for a session."""
        index_key = self._key(
            self.key_config.subagent_instance_index,
            session_id=session_id,
        )
        handles = await self._client.smembers(index_key)
        records = []
        for handle in handles:
            raw = await self._client.get(
                self._key(
                    self.key_config.subagent_instance,
                    session_id=session_id,
                    handle=handle,
                ),
            )
            if raw:
                records.append(
                    SubAgentInstanceRecord.model_validate_json(raw),
                )
        return records
```

(`datetime` is already imported in this module — it is used by `upsert_subagent`.)

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/subagent_instance_storage_test.py -v`
Expected: PASS (or the Redis test SKIPs if fakeredis is absent; the model test still passes).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/app/storage/_base.py src/agentscope/app/storage/_redis_storage.py tests/subagent_instance_storage_test.py
git commit -m "feat(storage): per-session sub-agent instance CRUD"
```

---

## Task 4: Add the `SessionInstanceRegistry`

**Files:**
- Create: `src/agentscope/subagent/_instance_registry.py`
- Modify: `src/agentscope/subagent/__init__.py`
- Test: `tests/subagent_instance_registry_test.py` (create)

**Interfaces:**
- Consumes: any object with `get_subagent_instance(session_id, handle)` and `upsert_subagent_instance(session_id, handle, agent_id, prototype_name)` (duck-typed — no `app.storage` import, to avoid inverting the module dependency).
- Produces: `SessionInstanceRegistry(storage, session_id)` with `async def resolve(handle, prototype_name) -> tuple[str, bool]` → `(agent_id, created)`.

- [ ] **Step 1: Write the failing test**

Create `tests/subagent_instance_registry_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for the per-session instance registry."""

import asyncio
import unittest

from agentscope.subagent import SessionInstanceRegistry
from utils import AnyString


class _FakeStorage:
    """In-memory stand-in for the instance storage surface."""

    def __init__(self) -> None:
        self._by_key: dict[tuple[str, str], dict] = {}

    async def get_subagent_instance(self, session_id, handle):
        """Return a stored record-like object or None."""
        data = self._by_key.get((session_id, handle))
        if data is None:
            return None
        return type("Rec", (), data)

    async def upsert_subagent_instance(
        self, session_id, handle, agent_id, prototype_name,
    ):
        """Store the instance fields."""
        self._by_key[(session_id, handle)] = {
            "session_id": session_id,
            "handle": handle,
            "agent_id": agent_id,
            "prototype_name": prototype_name,
        }
        return "rec-id"


class SessionInstanceRegistryTest(unittest.TestCase):
    """Validate create-then-resume resolution."""

    def test_first_call_creates_then_reuses(self) -> None:
        """First resolve mints+persists; second returns same id."""
        storage = _FakeStorage()
        registry = SessionInstanceRegistry(storage, "s1")

        async def _run():
            first = await registry.resolve("writer", "claude_code")
            second = await registry.resolve("writer", "claude_code")
            return first, second

        (id1, created1), (id2, created2) = asyncio.run(_run())
        self.assertEqual(id1, AnyString())
        self.assertTrue(created1)
        self.assertEqual(id2, id1)
        self.assertFalse(created2)

    def test_distinct_handles_get_distinct_ids(self) -> None:
        """Different handles mint different agent ids."""
        storage = _FakeStorage()
        registry = SessionInstanceRegistry(storage, "s1")

        async def _run():
            a, _ = await registry.resolve("writer", "claude_code")
            b, _ = await registry.resolve("writer2", "claude_code")
            return a, b

        a, b = asyncio.run(_run())
        self.assertNotEqual(a, b)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/subagent_instance_registry_test.py -v`
Expected: FAIL with `ImportError: cannot import name 'SessionInstanceRegistry'`.

- [ ] **Step 3: Create the registry and export it**

Create `src/agentscope/subagent/_instance_registry.py`:

```python
# -*- coding: utf-8 -*-
"""Per-session registry mapping instance handles to CLI session ids."""

import uuid
from typing import Any


class SessionInstanceRegistry:
    """Resolves a sub-agent instance handle to a stable CLI session id.

    Scoped to one chat session. Backed by a storage object exposing
    ``get_subagent_instance`` / ``upsert_subagent_instance`` (duck-typed
    so this module does not import ``app.storage``). The first time a
    handle is seen it mints a uuid and persists it; later lookups return
    the stored id so callers can RESUME the same CLI conversation.
    """

    def __init__(self, storage: Any, session_id: str) -> None:
        """Initialize the registry.

        Args:
            storage (`Any`):
                Storage backend with ``get_subagent_instance`` and
                ``upsert_subagent_instance`` coroutines.
            session_id (`str`):
                The chat session this registry is scoped to.
        """
        self._storage = storage
        self._session_id = session_id

    async def resolve(
        self,
        handle: str,
        prototype_name: str,
    ) -> tuple[str, bool]:
        """Resolve ``handle`` to a CLI session id, creating if new.

        Args:
            handle (`str`):
                The agent-chosen instance handle.
            prototype_name (`str`):
                The prototype ``name`` this instance is of.

        Returns:
            `tuple[str, bool]`:
                ``(agent_id, created)`` — ``created`` is ``True`` when a
                fresh id was minted (caller should run the create
                command), ``False`` when an existing id was reused
                (caller should run the resume command).
        """
        record = await self._storage.get_subagent_instance(
            self._session_id,
            handle,
        )
        if record is not None:
            return record.agent_id, False
        agent_id = uuid.uuid4().hex
        await self._storage.upsert_subagent_instance(
            self._session_id,
            handle,
            agent_id,
            prototype_name,
        )
        return agent_id, True
```

In `src/agentscope/subagent/__init__.py`, add the import and `__all__` entry:

```python
from ._instance_registry import SessionInstanceRegistry
```
Add `"SessionInstanceRegistry",` to `__all__`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/subagent_instance_registry_test.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_instance_registry.py src/agentscope/subagent/__init__.py tests/subagent_instance_registry_test.py
git commit -m "feat(subagent): add SessionInstanceRegistry"
```

---

## Task 5: Make `CliSubAgentTool` instance-aware (create-then-resume)

**Files:**
- Modify: `src/agentscope/subagent/_tool.py`
- Test: `tests/subagent_tool_test.py` (create)

**Interfaces:**
- Consumes: `SessionInstanceRegistry` (Task 4), `CliSubAgentConfig.resume_command` (Task 1).
- Produces: `CliSubAgentTool(name, description, command, resume_command=None, cwd=None, env=None, timeout=600, backend=None, registry=None, middlewares=None)`. Stateless when `resume_command`/`registry` unset → `input_schema` is `{prompt}` only. Stateful → `input_schema` adds required `instance`; result metadata `{instance, agent_id, action}`.

- [ ] **Step 1: Write the failing test**

Create `tests/subagent_tool_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for the instance-aware CLI sub-agent tool."""

import asyncio
import unittest

from agentscope.subagent import CliSubAgentTool
from agentscope.tool import ExecResult


class _RecordingBackend:
    """Fake backend that records argv and returns canned output."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def exec_shell(self, argv, cwd=None, timeout=None):
        """Record argv and return a successful ExecResult."""
        self.calls.append(argv)
        return ExecResult(exit_code=0, stdout=b"ok", stderr=b"")


class _FixedRegistry:
    """Registry stub: create on first handle, resume after."""

    def __init__(self) -> None:
        self._seen: dict[str, str] = {}

    async def resolve(self, handle, prototype_name):
        """Return (agent_id, created)."""
        if handle in self._seen:
            return self._seen[handle], False
        self._seen[handle] = "AID"
        return "AID", True


async def _collect(gen):
    """Drain an async generator to a list."""
    return [chunk async for chunk in gen]


class CliSubAgentToolStatelessTest(unittest.TestCase):
    """Stateless tool keeps the {prompt}-only schema and behavior."""

    def test_schema_and_run(self) -> None:
        """Schema is prompt-only; argv substitutes {prompt}."""
        backend = _RecordingBackend()
        tool = CliSubAgentTool(
            name="cc",
            description="d",
            command="claude -p {prompt}",
            backend=backend,
        )
        self.assertEqual(tool.input_schema["required"], ["prompt"])
        self.assertNotIn("instance", tool.input_schema["properties"])
        chunks = asyncio.run(_collect(tool.call(prompt="hi")))
        self.assertEqual(backend.calls, [["claude", "-p", "hi"]])
        self.assertEqual(chunks[-1].content[0].text, "ok")


class CliSubAgentToolStatefulTest(unittest.TestCase):
    """Stateful tool creates then resumes with a stable agent_id."""

    def _make_tool(self):
        """Build a stateful tool with recording backend + registry."""
        backend = _RecordingBackend()
        tool = CliSubAgentTool(
            name="cc",
            description="d",
            command="claude -p {prompt} --session-id {agent_id}",
            resume_command="claude -p {prompt} --resume {agent_id}",
            backend=backend,
            registry=_FixedRegistry(),
        )
        return tool, backend

    def test_schema_requires_instance(self) -> None:
        """Stateful schema exposes a required instance handle."""
        tool, _ = self._make_tool()
        self.assertIn("instance", tool.input_schema["properties"])
        self.assertEqual(
            sorted(tool.input_schema["required"]),
            ["instance", "prompt"],
        )

    def test_create_then_resume(self) -> None:
        """First call uses command, second uses resume_command."""
        tool, backend = self._make_tool()

        first = asyncio.run(
            _collect(tool.call(prompt="draft", instance="writer")),
        )
        second = asyncio.run(
            _collect(tool.call(prompt="revise", instance="writer")),
        )

        self.assertEqual(
            backend.calls,
            [
                ["claude", "-p", "draft", "--session-id", "AID"],
                ["claude", "-p", "revise", "--resume", "AID"],
            ],
        )
        self.assertEqual(
            first[-1].metadata,
            {"instance": "writer", "agent_id": "AID", "action": "create"},
        )
        self.assertEqual(
            second[-1].metadata,
            {"instance": "writer", "agent_id": "AID", "action": "resume"},
        )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/subagent_tool_test.py -v`
Expected: FAIL — `CliSubAgentTool.__init__() got an unexpected keyword argument 'resume_command'` (and stateful assertions fail).

- [ ] **Step 3: Rewrite the tool for instances**

In `src/agentscope/subagent/_tool.py`:

Add `import uuid` at the top (after `import shlex`).

Replace the `__init__` signature + body (lines 38–86) with:

```python
    def __init__(
        self,
        name: str,
        description: str,
        command: str,
        resume_command: str | None = None,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 600,
        backend: BackendBase | None = None,
        registry: Any = None,
        middlewares: List[ToolMiddlewareBase] | None = None,
    ) -> None:
        """Initialize the CLI sub-agent tool.

        Args:
            name (`str`):
                The tool name presented to the agent.
            description (`str`):
                The tool description presented to the agent.
            command (`str`):
                The create/first-call command template containing
                ``{prompt}`` (and ``{agent_id}`` when stateful).
            resume_command (`str | None`, optional):
                The resume command template. When set (with a
                ``registry``), the tool is stateful.
            cwd (`str | None`, optional):
                Working directory for the sub-agent process.
            env (`dict[str, str] | None`, optional):
                Extra environment variables applied via an ``env`` prefix.
            timeout (`int`, defaults to `600`):
                Maximum seconds to wait for the sub-agent.
            backend (`BackendBase | None`, optional):
                The execution backend. Defaults to :class:`LocalBackend`.
            registry (`Any`, optional):
                A :class:`SessionInstanceRegistry`-like object resolving
                instance handles to CLI session ids. Required for the
                tool to be stateful.
            middlewares (`List[ToolMiddlewareBase] | None`, optional):
                Tool middlewares wrapping execution.
        """
        super().__init__(middlewares=middlewares)
        self.name = name
        self.description = description
        self._command = command
        self._resume_command = resume_command
        self._cwd = cwd
        self._env = env
        self._timeout = timeout
        self._backend = backend or LocalBackend()
        self._registry = registry
        self.input_schema = self._build_input_schema()

    @property
    def is_stateful(self) -> bool:
        """Whether this tool manages resumable instances."""
        return bool(self._resume_command) and self._registry is not None

    def _build_input_schema(self) -> dict:
        """Build the tool input schema.

        Returns:
            `dict`:
                A ``{prompt}``-only schema when stateless; a
                ``{prompt, instance}`` schema when stateful.
        """
        properties: dict = {
            "prompt": {
                "type": "string",
                "description": "The task to delegate to the sub-agent.",
            },
        }
        required = ["prompt"]
        if self.is_stateful:
            properties["instance"] = {
                "type": "string",
                "description": (
                    "A stable handle for this sub-agent instance. Reuse "
                    "the same handle to continue the same conversation "
                    "(context is preserved); use a new handle to start a "
                    "fresh instance."
                ),
            }
            required.append("instance")
        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }
```

Replace `_build_argv` (lines 88–113) with a version that takes the template + optional agent_id:

```python
    def _build_argv(
        self,
        prompt: str,
        template: str,
        agent_id: str | None = None,
    ) -> list[str]:
        """Build the argv for a delegated ``prompt``.

        Substitutes ``{prompt}`` (and ``{agent_id}`` when provided)
        inside each token, then prepends an ``env KEY=VAL`` prefix when
        env vars are configured.

        Args:
            prompt (`str`):
                The task to delegate.
            template (`str`):
                The command template to expand.
            agent_id (`str | None`, optional):
                The CLI session id substituted for ``{agent_id}``.

        Returns:
            `list[str]`:
                The argv to run without a shell.
        """
        argv = []
        for tok in shlex.split(template):
            tok = tok.replace("{prompt}", prompt)
            if agent_id is not None:
                tok = tok.replace("{agent_id}", agent_id)
            argv.append(tok)
        if self._env:
            argv = [
                "env",
                *(f"{key}={value}" for key, value in self._env.items()),
                *argv,
            ]
        return argv
```

Replace the `call` signature + the first lines that build argv (lines 115–135) so it resolves the instance and chooses the template. Change the signature to accept `instance`, and compute `argv` + `metadata` before the `try`:

```python
    async def call(  # type: ignore[override]
        self,
        prompt: str,
        instance: str | None = None,
    ) -> AsyncGenerator[ToolChunk, None]:
        """Run the sub-agent and yield its result.

        Args:
            prompt (`str`):
                The task to delegate to the sub-agent.
            instance (`str | None`, optional):
                The instance handle (stateful prototypes only). A new
                handle creates a fresh instance; a known handle resumes.

        Yields:
            `ToolChunk`:
                A single terminal chunk with the sub-agent's output.
        """
        metadata: dict = {}
        if self.is_stateful:
            handle = instance or uuid.uuid4().hex
            try:
                agent_id, created = await self._registry.resolve(
                    handle,
                    self.name,
                )
            except Exception:  # noqa: BLE001 - degrade to a create run
                agent_id, created = uuid.uuid4().hex, True
            template = self._command if created else self._resume_command
            argv = self._build_argv(prompt, template, agent_id)
            metadata = {
                "instance": handle,
                "agent_id": agent_id,
                "action": "create" if created else "resume",
            }
        else:
            argv = self._build_argv(
                prompt,
                self._command,
                uuid.uuid4().hex,
            )
        try:
            result = await self._backend.exec_shell(
                argv,
                cwd=self._cwd,
                timeout=float(self._timeout),
            )
        except Exception as exc:  # noqa: BLE001
            yield ToolChunk(
                content=[TextBlock(text=f"Sub-Agent failed: {exc}")],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return
```

Finally, attach `metadata` to the terminal success chunk. Change the last `yield` (lines 188–192) to:

```python
        yield ToolChunk(
            content=[TextBlock(text=output)],
            state=ToolResultState.RUNNING,
            is_last=True,
            metadata=metadata,
        )
```

Also add `Any` to the `typing` import at the top if not already present (it is: `from typing import Any, AsyncGenerator, List`).

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/subagent_tool_test.py -v`
Expected: PASS. Also run `pytest tests/subagent_factory_test.py tests/subagent_router_test.py -v` — still PASS (stateless unchanged).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_tool.py tests/subagent_tool_test.py
git commit -m "feat(subagent): stateful create-then-resume in CliSubAgentTool"
```

---

## Task 6: Wire `resume_command` + registry through the tool factory

**Files:**
- Modify: `src/agentscope/subagent/_agent_tools.py`
- Test: `tests/subagent_agent_tools_test.py` (create)

**Interfaces:**
- Consumes: `CliSubAgentTool(..., resume_command=..., registry=...)` (Task 5), `SessionInstanceRegistry` (Task 4).
- Produces: for each stored config the factory builds a `CliSubAgentTool` passing `resume_command=config.resume_command` and `registry=SessionInstanceRegistry(storage, session_id)`.

- [ ] **Step 1: Write the failing test**

Create `tests/subagent_agent_tools_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for make_subagent_tool_factory instance wiring."""

import asyncio
import unittest

from agentscope.subagent import (
    CliSubAgentConfig,
    make_subagent_tool_factory,
)
from agentscope.app.storage import SubAgentRecord


class _FakeStorage:
    """Storage stub exposing list_subagents + instance methods."""

    def __init__(self, configs) -> None:
        self._records = [
            SubAgentRecord(
                id=c.id,
                user_id="u1",
                data=c.model_dump(mode="json"),
            )
            for c in configs
        ]

    async def list_subagents(self, user_id):
        """Return the seeded records."""
        return self._records

    async def get_session(self, *a, **k):
        """No session → factory falls back to LocalBackend."""
        return None

    async def get_subagent_instance(self, session_id, handle):
        """No instances stored."""
        return None

    async def upsert_subagent_instance(self, *a, **k):
        """No-op."""
        return "rec"


class _FakeWorkspaceManager:
    """Workspace manager stub that always fails resolution."""

    async def get_workspace(self, *a, **k):
        """Raise so the factory falls back to (None, None)."""
        raise RuntimeError("no workspace")


class MakeFactoryTest(unittest.TestCase):
    """The factory builds stateful tools for stateful configs."""

    def test_stateful_tool_is_built(self) -> None:
        """A config with resume_command yields a stateful tool."""
        config = CliSubAgentConfig(
            name="cc",
            description="d",
            command="claude -p {prompt} --session-id {agent_id}",
            resume_command="claude -p {prompt} --resume {agent_id}",
        )
        factory = make_subagent_tool_factory(
            _FakeStorage([config]),
            _FakeWorkspaceManager(),
        )
        tools = asyncio.run(factory("u1", "a1", "s1"))
        self.assertEqual(len(tools), 1)
        self.assertTrue(tools[0].is_stateful)
        self.assertIn("instance", tools[0].input_schema["properties"])

    def test_stateless_tool_is_built(self) -> None:
        """A config without resume_command yields a stateless tool."""
        config = CliSubAgentConfig(
            name="cc",
            description="d",
            command="claude -p {prompt}",
        )
        factory = make_subagent_tool_factory(
            _FakeStorage([config]),
            _FakeWorkspaceManager(),
        )
        tools = asyncio.run(factory("u1", "a1", "s1"))
        self.assertFalse(tools[0].is_stateful)
        self.assertEqual(tools[0].input_schema["required"], ["prompt"])
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `pytest tests/subagent_agent_tools_test.py -v`
Expected: FAIL — `test_stateful_tool_is_built` fails because the factory does not yet pass `resume_command`/`registry`, so `is_stateful` is `False`.

- [ ] **Step 3: Pass the new args in the factory**

In `src/agentscope/subagent/_agent_tools.py`, add the import at the top:

```python
from ._instance_registry import SessionInstanceRegistry
```

Replace the `CliSubAgentTool(...)` construction (lines 104–113) with:

```python
            tools.append(
                CliSubAgentTool(
                    name=config.name,
                    description=config.description,
                    command=config.command,
                    resume_command=config.resume_command,
                    cwd=config.cwd or session_workdir,
                    env=config.env,
                    timeout=config.timeout,
                    backend=backend,
                    registry=SessionInstanceRegistry(storage, session_id),
                ),
            )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `pytest tests/subagent_agent_tools_test.py -v`
Expected: PASS.

- [ ] **Step 5: Run the whole backend suite + pre-commit**

Run: `pytest tests -q` then `pre-commit run --all-files`
Expected: PASS / all hooks green. Fix any black/flake8/mypy issues inline (do not disable checks).

- [ ] **Step 6: Commit**

```bash
git add src/agentscope/subagent/_agent_tools.py tests/subagent_agent_tools_test.py
git commit -m "feat(subagent): wire resume_command + per-session registry into tool factory"
```

---

## Task 7: Frontend types + `useSubagents.update`

**Files:**
- Modify: `examples/web_ui/frontend/src/api/types.ts`
- Modify: `examples/web_ui/frontend/src/hooks/useSubagents.ts`

**Interfaces:**
- Produces: `SubAgentData.resume_command?: string | null`; `useSubagents()` returns an additional `update(subagentId: string, data: Record<string, unknown>) => Promise<void>`.

- [ ] **Step 1: Add `resume_command` to `SubAgentData`**

In `examples/web_ui/frontend/src/api/types.ts`, in the `SubAgentData` interface add the field after `command`:

```ts
export interface SubAgentData {
	type: 'cli_subagent';
	name: string;
	description: string;
	command: string;
	resume_command?: string | null;
	cwd?: string | null;
	env?: Record<string, string> | null;
	timeout?: number;
}
```

- [ ] **Step 2: Add `update` to the hook**

In `examples/web_ui/frontend/src/hooks/useSubagents.ts`, add an `update` callback after `create` and include it in the returned object:

```ts
	const update = useCallback(
		async (subagentId: string, data: Record<string, unknown>) => {
			await subagentApi.update(subagentId, { data });
			await refetch();
		},
		[refetch],
	);
```
Change the return to:
```ts
	return { subagents, loading, error, refetch, create, update, remove };
```

- [ ] **Step 3: Verify typecheck**

Run: `pnpm -C examples/web_ui/frontend build`
Expected: PASS (no TS errors). `subagentApi.update` already exists in `api/subagent.ts`.

- [ ] **Step 4: Commit**

```bash
git add examples/web_ui/frontend/src/api/types.ts examples/web_ui/frontend/src/hooks/useSubagents.ts
git commit -m "feat(webui): add resume_command type + useSubagents.update"
```

---

## Task 8: New left `SubagentSidebar` (prototype CRUD) + mount + toggle

**Files:**
- Create: `examples/web_ui/frontend/src/components/subagent/SubagentSidebar.tsx`
- Modify: `examples/web_ui/frontend/src/pages/chat/index.tsx` (lift `useSubagents`, add toggle + mount)
- Modify: `examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx` (accept `subagents` as a prop instead of calling the hook)
- Modify: `examples/web_ui/frontend/src/i18n/locales/en.json`, `zh.json` (sidebar keys — see Task 11 for full block; add the minimal keys used here)

**Interfaces:**
- Consumes: `useSubagents()` (`subagents`, `loading`, `create`, `update`, `remove`) — Task 7.
- Produces: `SubagentSidebar({ subagents, loading, onCreate, onUpdate, onRemove })`; `ChatViewport` gains a `subagents: SubAgentView[]` prop and no longer calls `useSubagents()`.

- [ ] **Step 1: Create the sidebar component**

Create `examples/web_ui/frontend/src/components/subagent/SubagentSidebar.tsx`:

```tsx
import { Bot, Pencil, Plus, Trash2, X } from 'lucide-react';
import { useState } from 'react';

import type { SubAgentView } from '@/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import {
	Sidebar,
	SidebarContent,
	SidebarGroup,
	SidebarGroupContent,
	SidebarHeader,
} from '@/components/ui/sidebar';
import { Textarea } from '@/components/ui/textarea';
import { useTranslation } from '@/i18n/useI18n';

interface SubagentSidebarProps {
	subagents: SubAgentView[];
	loading: boolean;
	onCreate: (data: Record<string, unknown>) => Promise<void>;
	onUpdate: (id: string, data: Record<string, unknown>) => Promise<void>;
	onRemove: (id: string) => Promise<void>;
}

interface FormState {
	name: string;
	description: string;
	command: string;
	resume_command: string;
	cwd: string;
	timeout: string;
}

const EMPTY_FORM: FormState = {
	name: '',
	description: '',
	command: '',
	resume_command: '',
	cwd: '',
	timeout: '600',
};

function toForm(view: SubAgentView): FormState {
	return {
		name: view.data.name,
		description: view.data.description,
		command: view.data.command,
		resume_command: view.data.resume_command ?? '',
		cwd: view.data.cwd ?? '',
		timeout: String(view.data.timeout ?? 600),
	};
}

function toPayload(form: FormState): Record<string, unknown> {
	return {
		type: 'cli_subagent',
		name: form.name.trim(),
		description: form.description.trim(),
		command: form.command.trim(),
		resume_command: form.resume_command.trim() || null,
		cwd: form.cwd.trim() || null,
		timeout: Number(form.timeout) || 600,
	};
}

/**
 * Left sidebar that owns CLI sub-agent *prototype* CRUD: list existing
 * prototypes, create a new one, edit inline, or delete. A prototype with
 * a `resume_command` is stateful (instantiable). Mirrors the shadcn
 * `Sidebar` structure used by `TeamSidebar`.
 */
export function SubagentSidebar({
	subagents,
	loading,
	onCreate,
	onUpdate,
	onRemove,
}: SubagentSidebarProps) {
	const { t } = useTranslation();
	// `null` = form closed; '' = creating new; otherwise editing that id.
	const [editingId, setEditingId] = useState<string | null>(null);
	const [form, setForm] = useState<FormState>(EMPTY_FORM);

	const openCreate = () => {
		setForm(EMPTY_FORM);
		setEditingId('');
	};
	const openEdit = (view: SubAgentView) => {
		setForm(toForm(view));
		setEditingId(view.id);
	};
	const close = () => setEditingId(null);

	const canSubmit =
		form.name.trim() && form.description.trim() && form.command.includes('{prompt}');

	const submit = async () => {
		const payload = toPayload(form);
		if (editingId) await onUpdate(editingId, payload);
		else await onCreate(payload);
		close();
	};

	const set = (key: keyof FormState) => (e: { target: { value: string } }) =>
		setForm((f) => ({ ...f, [key]: e.target.value }));

	return (
		<Sidebar collapsible="none" className="w-72 border-r">
			<SidebarHeader>
				<div className="flex items-center justify-between px-2 py-1">
					<span className="text-muted-foreground text-xs uppercase tracking-wide">
						{t('subagent-sidebar.title')}
					</span>
					<Button size="icon" variant="ghost" className="size-6" onClick={openCreate}>
						<Plus className="size-4" />
					</Button>
				</div>
			</SidebarHeader>
			<SidebarContent>
				<SidebarGroup>
					<SidebarGroupContent className="flex flex-col gap-2 px-2">
						{loading && (
							<p className="text-xs text-muted-foreground">{t('common.loading')}</p>
						)}
						{!loading && subagents.length === 0 && editingId === null && (
							<p className="text-xs text-muted-foreground">
								{t('subagent-sidebar.empty')}
							</p>
						)}
						{subagents.map((sa) => (
							<div
								key={sa.id}
								className="flex items-center gap-2 rounded-sm border px-2 py-1"
							>
								<Bot className="size-4 shrink-0" />
								<span className="min-w-0 flex-1 truncate text-sm">{sa.data.name}</span>
								<Button
									size="icon"
									variant="ghost"
									className="size-6"
									onClick={() => openEdit(sa)}
								>
									<Pencil className="size-3.5" />
								</Button>
								<Button
									size="icon"
									variant="ghost"
									className="size-6"
									onClick={() => onRemove(sa.id)}
								>
									<Trash2 className="size-3.5" />
								</Button>
							</div>
						))}

						{editingId !== null && (
							<div className="flex flex-col gap-2 rounded-sm border p-2">
								<div className="flex items-center justify-between">
									<span className="text-xs font-medium">
										{editingId
											? t('subagent-sidebar.editTitle')
											: t('subagent-sidebar.newTitle')}
									</span>
									<Button size="icon" variant="ghost" className="size-6" onClick={close}>
										<X className="size-4" />
									</Button>
								</div>
								<Label className="text-xs">{t('subagent-sidebar.nameLabel')}</Label>
								<Input value={form.name} onChange={set('name')} placeholder="claude_code" />
								<Label className="text-xs">{t('subagent-sidebar.descLabel')}</Label>
								<Textarea value={form.description} onChange={set('description')} rows={2} />
								<Label className="text-xs">{t('subagent-sidebar.commandLabel')}</Label>
								<Textarea
									value={form.command}
									onChange={set('command')}
									rows={2}
									placeholder="claude -p {prompt} --permission-mode auto --session-id {agent_id}"
								/>
								<Label className="text-xs">
									{t('subagent-sidebar.resumeCommandLabel')}
								</Label>
								<Textarea
									value={form.resume_command}
									onChange={set('resume_command')}
									rows={2}
									placeholder="claude -p {prompt} --permission-mode auto --resume {agent_id}"
								/>
								<p className="text-[11px] text-muted-foreground">
									{t('subagent-sidebar.resumeHint')}
								</p>
								<Label className="text-xs">{t('subagent-sidebar.cwdLabel')}</Label>
								<Input value={form.cwd} onChange={set('cwd')} />
								<Label className="text-xs">{t('subagent-sidebar.timeoutLabel')}</Label>
								<Input value={form.timeout} onChange={set('timeout')} inputMode="numeric" />
								<Button disabled={!canSubmit} onClick={submit} className="mt-1">
									{t('common.save')}
								</Button>
							</div>
						)}
					</SidebarGroupContent>
				</SidebarGroup>
			</SidebarContent>
		</Sidebar>
	);
}
```

> Note: confirm the imported shadcn primitives exist at these paths (`@/components/ui/button`, `input`, `label`, `textarea`, `sidebar`). They are used elsewhere in the app; if `Textarea`/`Label` live under different names, match the existing import used by `AddSubagentDialog.tsx` before deleting it in Task 9.

- [ ] **Step 2: Lift `useSubagents` into `ChatPageInner` + add toggle + mount**

In `examples/web_ui/frontend/src/pages/chat/index.tsx`:

Add imports:
```tsx
import { useState } from 'react'; // if not already imported
import { Bot } from 'lucide-react'; // if not already imported
import { SubagentSidebar } from '@/components/subagent/SubagentSidebar';
import { useSubagents } from '@/hooks/useSubagents';
import { SidebarMenuButton } from '@/components/ui/sidebar'; // if not already imported
```

Inside `ChatPageInner`, add state + the hook near the top of the component body:
```tsx
	const [subagentSidebarOpen, setSubagentSidebarOpen] = useState(false);
	const {
		subagents,
		loading: subagentsLoading,
		create: createSubagent,
		update: updateSubagent,
		remove: removeSubagent,
	} = useSubagents();
```

Add a toggle button in the session sidebar footer — replace the empty `<SidebarFooter />` (line 351) with:
```tsx
				<SidebarFooter>
					<SidebarMenuButton onClick={() => setSubagentSidebarOpen((v) => !v)}>
						<Bot />
						<span>{t('subagent-sidebar.title')}</span>
					</SidebarMenuButton>
				</SidebarFooter>
```

Mount the sidebar right after the session `</Sidebar>` (line 352) and before the TeamSidebar comment/block:
```tsx
				{subagentSidebarOpen && (
					<SubagentSidebar
						subagents={subagents}
						loading={subagentsLoading}
						onCreate={createSubagent}
						onUpdate={updateSubagent}
						onRemove={removeSubagent}
					/>
				)}
```

Pass `subagents` into `ChatViewport` (line 365–369):
```tsx
					<ChatViewport
						agentId={effectiveAgentId}
						sessionId={effectiveSessionId}
						subagents={subagents}
						onTeamUpdated={refetchSessions}
					/>
```

- [ ] **Step 3: Make `ChatViewport` consume `subagents` from props**

In `examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx`:
- Add `subagents: SubAgentView[]` to the component's props interface (and import `SubAgentView` from `@/api` if needed).
- Delete the `useSubagents()` destructuring block (lines 179–184). `subagentsLoading`, `addSubagent`, `removeSubagent` are no longer needed (the panel that used them is replaced in Task 9); `subagents` now comes from props.
- `subagentNameSet` (line 345) already reads `subagents` — unchanged, now sourced from the prop.

- [ ] **Step 4: Add the minimal i18n keys used above**

Add a `subagent-sidebar` block to both `en.json` and `zh.json` (full block is defined in Task 11; add it now so the build passes). Also ensure `common.save`, `common.loading` exist (they are used across the app; if `common.save` is missing, add `"save": "Save"` / `"保存"`).

- [ ] **Step 5: Verify typecheck**

Run: `pnpm -C examples/web_ui/frontend build`
Expected: PASS. If `ChatViewport` still references removed vars (`subagentsLoading`, `addSubagent`, `removeSubagent`), the TS build will flag them — those references are removed in Task 9 when the panel content is swapped; if the build fails only inside the `subagent` PanelDescriptor, proceed to Task 9 and re-run the build there. (To keep this task independently green, temporarily leave the old `SubagentPanel` descriptor referencing `subagents`/`subagentsLoading` via props — but since `subagentsLoading` is removed, set the panel's `loading={false}` placeholder until Task 9. Prefer doing Task 9 immediately after.)

- [ ] **Step 6: Commit**

```bash
git add examples/web_ui/frontend/src/components/subagent/SubagentSidebar.tsx examples/web_ui/frontend/src/pages/chat/index.tsx examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx examples/web_ui/frontend/src/i18n/locales/en.json examples/web_ui/frontend/src/i18n/locales/zh.json
git commit -m "feat(webui): left sidebar for sub-agent prototype CRUD"
```

---

## Task 9: Right-dock Instance Monitor (transcript-derived) + retire old panel/modal

**Files:**
- Create: `examples/web_ui/frontend/src/components/subagent/deriveInstances.ts`
- Create: `examples/web_ui/frontend/src/components/subagent/SubagentInstanceMonitor.tsx`
- Modify: `examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx` (swap the `subagent` panel content)
- Delete: `examples/web_ui/frontend/src/components/panel/SubagentPanel.tsx`
- Delete: `examples/web_ui/frontend/src/components/dialog/AddSubagentDialog.tsx`

**Interfaces:**
- Consumes: `msgs: Msg[]` (already in `ChatViewport` from `useMessages`), `subagents: SubAgentView[]` (prop from Task 8), `result.metadata` (`{instance, agent_id, action}` from Task 5).
- Produces: `deriveInstances(msgs, statefulNames)` → `Instance[]`; `SubagentInstanceMonitor({ msgs, subagents })`.

- [ ] **Step 1: Create the derivation helper**

Create `examples/web_ui/frontend/src/components/subagent/deriveInstances.ts`:

```ts
import type { ContentBlock, Msg } from '@agentscope-ai/agentscope/message';

import { getResultText, parseInput } from '@/components/chat/tool-renderers/_shared';
import type { ToolCallWithResult } from '@/components/chat/tool-renderers/types';

export interface Exchange {
	prompt: string;
	output: string;
	action: 'create' | 'resume' | 'unknown';
}

export interface Instance {
	handle: string;
	prototype: string;
	agentId?: string;
	exchanges: Exchange[];
}

/**
 * Derive the session's stateful sub-agent instances from the transcript.
 *
 * Pairs each `tool_use` block with its `tool_result` (by id) across all
 * messages, keeps only calls to a stateful prototype (`statefulNames`)
 * that carry an `instance` handle, and groups them by handle in call
 * order. `action`/`agentId` come from the result metadata when present,
 * else `action` falls back to first-occurrence ("create") vs later
 * ("resume").
 */
export function deriveInstances(msgs: Msg[], statefulNames: Set<string>): Instance[] {
	// 1) Pair tool_use with tool_result by id across the whole session.
	const results = new Map<string, ToolCallWithResult['result']>();
	const calls: { name: string; input: string; id: string }[] = [];
	for (const msg of msgs) {
		const blocks: ContentBlock[] = Array.isArray(msg.content) ? msg.content : [];
		for (const block of blocks) {
			if (block.type === 'tool_use') {
				calls.push({ name: block.name, input: block.input, id: block.id });
			} else if (block.type === 'tool_result') {
				results.set(block.id, block);
			}
		}
	}

	// 2) Group stateful, instance-bearing calls by handle in order.
	const byHandle = new Map<string, Instance>();
	for (const call of calls) {
		if (!statefulNames.has(call.name)) continue;
		const input = parseInput(call.input);
		const handle = typeof input.instance === 'string' ? input.instance : undefined;
		if (!handle) continue;
		const result = results.get(call.id);
		const meta = (result?.metadata ?? {}) as Record<string, unknown>;
		const metaAction = meta.action === 'create' || meta.action === 'resume' ? meta.action : undefined;
		const agentId = typeof meta.agent_id === 'string' ? meta.agent_id : undefined;

		let inst = byHandle.get(handle);
		if (!inst) {
			inst = { handle, prototype: call.name, exchanges: [] };
			byHandle.set(handle, inst);
		}
		if (agentId && !inst.agentId) inst.agentId = agentId;
		inst.exchanges.push({
			prompt: typeof input.prompt === 'string' ? input.prompt : '',
			output: getResultText(result),
			action: metaAction ?? (inst.exchanges.length === 0 ? 'create' : 'resume'),
		});
	}
	return [...byHandle.values()];
}
```

> Note: `getResultText` is already exported from `_shared.tsx`. `parseInput` too. If the `Msg` block union names differ (`tool_use`/`tool_result` are the SDK's block `type`s used in `MessageBubble.tsx`), match `MessageBubble`'s pairing code exactly.

- [ ] **Step 2: Create the monitor component**

Create `examples/web_ui/frontend/src/components/subagent/SubagentInstanceMonitor.tsx`:

```tsx
import type { Msg } from '@agentscope-ai/agentscope/message';
import { Bot } from 'lucide-react';
import { useMemo, useState } from 'react';

import type { SubAgentView } from '@/api';
import { useTranslation } from '@/i18n/useI18n';

import { deriveInstances } from './deriveInstances';

interface SubagentInstanceMonitorProps {
	msgs: Msg[];
	subagents: SubAgentView[];
}

/**
 * Read-only right-dock monitor of this session's stateful sub-agent
 * instances. Derived entirely from the transcript: lists each instance
 * and, when selected, its ordered interaction history (prompt → output,
 * with a create/resume marker per exchange).
 */
export function SubagentInstanceMonitor({ msgs, subagents }: SubagentInstanceMonitorProps) {
	const { t } = useTranslation();
	const statefulNames = useMemo(
		() => new Set(subagents.filter((s) => s.data.resume_command).map((s) => s.data.name)),
		[subagents],
	);
	const instances = useMemo(() => deriveInstances(msgs, statefulNames), [msgs, statefulNames]);
	const [selected, setSelected] = useState<string | null>(null);
	const active = instances.find((i) => i.handle === selected) ?? null;

	if (instances.length === 0) {
		return <p className="p-3 text-xs text-muted-foreground">{t('subagent-monitor.empty')}</p>;
	}

	return (
		<div className="flex flex-col gap-2 p-2">
			{instances.map((inst) => (
				<button
					key={inst.handle}
					type="button"
					onClick={() => setSelected(inst.handle === selected ? null : inst.handle)}
					className="flex items-center gap-2 rounded-sm border px-2 py-1 text-left hover:bg-accent"
				>
					<Bot className="size-4 shrink-0" />
					<span className="min-w-0 flex-1 truncate text-sm">{inst.handle}</span>
					<span className="text-xs text-muted-foreground">{inst.prototype}</span>
					<span className="text-xs text-muted-foreground">×{inst.exchanges.length}</span>
				</button>
			))}
			{active && (
				<div className="flex flex-col gap-2">
					{active.agentId && (
						<p className="px-1 text-[11px] text-muted-foreground">
							{t('subagent-monitor.sessionId')}: {active.agentId}
						</p>
					)}
					{active.exchanges.map((ex, i) => (
						<div key={i} className="flex flex-col gap-1 rounded-sm border bg-background">
							<div className="flex items-center gap-2 border-b px-2 py-1 text-xs text-muted-foreground">
								<span>{t(`subagent-monitor.action.${ex.action}`)}</span>
							</div>
							<div className="px-2 py-1 text-xs whitespace-pre-wrap break-words">{ex.prompt}</div>
							<div className="border-t px-2 py-1 text-xs whitespace-pre-wrap break-words overflow-auto max-h-[200px]">
								{ex.output}
							</div>
						</div>
					))}
				</div>
			)}
		</div>
	);
}
```

- [ ] **Step 3: Swap the `subagent` panel content in `ChatViewport`**

In `examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx`:
- Add import: `import { SubagentInstanceMonitor } from '@/components/subagent/SubagentInstanceMonitor';`
- Remove the `SubagentPanel` import.
- Replace the `subagent` PanelDescriptor content (lines 266–273) with:
```tsx
					content: <SubagentInstanceMonitor msgs={msgs} subagents={subagents} />,
```
  and update its `title` to `t('subagent-monitor.title')` and keep `icon: <Bot className="size-4" />`.
- Remove any now-dead references to `subagentsLoading`, `addSubagent`, `removeSubagent` (the panel no longer uses them). Ensure the `subagent` descriptor is included in the `useMemo` dependency array with `msgs` and `subagents`.

- [ ] **Step 4: Delete the retired files**

```bash
git rm examples/web_ui/frontend/src/components/panel/SubagentPanel.tsx examples/web_ui/frontend/src/components/dialog/AddSubagentDialog.tsx
```
Then grep for and remove any remaining imports of these two modules:
```bash
grep -rn "SubagentPanel\|AddSubagentDialog" examples/web_ui/frontend/src
```
Expected after cleanup: no matches.

- [ ] **Step 5: Verify typecheck + lint**

Run: `pnpm -C examples/web_ui/frontend build && pnpm -C examples/web_ui/frontend lint`
Expected: PASS, no unused-var or missing-import errors.

- [ ] **Step 6: Commit**

```bash
git add -A examples/web_ui/frontend/src/components/subagent examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx
git commit -m "feat(webui): transcript-derived sub-agent instance monitor; retire old panel"
```

---

## Task 10: Transcript `[handle · new/resumed]` badge

**Files:**
- Modify: `examples/web_ui/frontend/src/components/chat/tool-renderers/DefaultRenderer.tsx`
- Modify: `examples/web_ui/frontend/src/components/chat/tool-renderers/index.tsx` (pass the badge through)

**Interfaces:**
- Consumes: `pair.call.input` (has `instance`), `pair.result.metadata.action` (Task 5), `subagentRenderBody` (existing).
- Produces: a `[handle · new/resumed]` marker rendered in the sub-agent tool-call header.

- [ ] **Step 1: Add a badge helper + render it in the sub-agent header**

In `DefaultRenderer.tsx`, add a helper and extend `defaultRenderHeader` to append the badge when the call is a sub-agent. Import `parseInput` from `_shared` (already imported) and `useTranslation` is not available here (renderers take `t` as a param). Add:

```tsx
/**
 * A compact `handle · new|resumed` badge for a stateful sub-agent call.
 * Handle comes from the call input; the create/resume action from the
 * result metadata. Returns null when the call carries no instance handle
 * (stateless sub-agent) so stateless calls look unchanged.
 */
export function subagentBadge(pair: ToolCallWithResult, t: TFunction): ReactNode {
	const input = parseInput(pair.call.input);
	const handle = typeof input.instance === 'string' ? input.instance : undefined;
	if (!handle) return null;
	const action = pair.result?.metadata?.action;
	const label =
		action === 'resume'
			? t('tool.subagentResumed')
			: action === 'create'
				? t('tool.subagentNew')
				: null;
	return (
		<span className="text-xs text-muted-foreground">
			[{handle}
			{label ? ` · ${label}` : ''}]
		</span>
	);
}
```

Change `defaultRenderHeader` to append the badge for sub-agents:

```tsx
export function defaultRenderHeader(
	pair: ToolCallWithResult,
	t: TFunction,
	isSubagent = false,
): ReactNode {
	return (
		<>
			<span className={toolLabelClass}>
				{isSubagent ? t('tool.callSubagent') : t('tool.callGeneric')}
			</span>
			<span className={toolArgClass}>{defaultGetDisplayName(pair.call)}</span>
			{isSubagent && subagentBadge(pair, t)}
		</>
	);
}
```

- [ ] **Step 2: Verify the header path receives `isSubagent`**

In `index.tsx`, `renderToolCall` already calls `defaultRenderHeader(pair, t, isSubagent)` (line 58) — no change needed. Confirm by reading it.

- [ ] **Step 3: Verify typecheck**

Run: `pnpm -C examples/web_ui/frontend build`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add examples/web_ui/frontend/src/components/chat/tool-renderers/DefaultRenderer.tsx
git commit -m "feat(webui): [handle · new/resumed] badge on stateful sub-agent calls"
```

---

## Task 11: i18n keys (en + zh) + retire dead keys

**Files:**
- Modify: `examples/web_ui/frontend/src/i18n/locales/en.json`
- Modify: `examples/web_ui/frontend/src/i18n/locales/zh.json`

**Interfaces:**
- Produces: all `subagent-sidebar.*`, `subagent-monitor.*`, and `tool.subagentNew` / `tool.subagentResumed` keys referenced in Tasks 8–10. Removes `dialog-subagent-add.*` and `panel.subagent.*` keys no longer referenced.

- [ ] **Step 1: Add the new keys (en.json)**

Add a top-level `subagent-sidebar` and `subagent-monitor` block, and two `tool.*` keys. Under `tool` (next to `callSubagent`/`subagentPrompt` at ~line 54):
```json
		"subagentNew": "new",
		"subagentResumed": "resumed",
```
Add top-level blocks (place near the other `dialog-*` / `panel` blocks):
```json
	"subagent-sidebar": {
		"title": "Sub-Agents",
		"empty": "No sub-agent prototypes yet.",
		"newTitle": "New prototype",
		"editTitle": "Edit prototype",
		"nameLabel": "Name",
		"descLabel": "Description",
		"commandLabel": "Command (create)",
		"resumeCommandLabel": "Resume command (optional)",
		"resumeHint": "Set both {prompt} and {agent_id} in each template to make this prototype stateful (create then resume).",
		"cwdLabel": "Working directory (optional)",
		"timeoutLabel": "Timeout (s)"
	},
	"subagent-monitor": {
		"title": "Instances",
		"empty": "No sub-agent instances in this session yet.",
		"sessionId": "Session id",
		"action": {
			"create": "new",
			"resume": "resumed",
			"unknown": "call"
		}
	},
```
Ensure `common.save` exists; if not, add `"save": "Save"` under `common`.

- [ ] **Step 2: Add the same keys (zh.json)**

Under `tool`:
```json
		"subagentNew": "新建",
		"subagentResumed": "续接",
```
Top-level blocks:
```json
	"subagent-sidebar": {
		"title": "子智能体",
		"empty": "暂无子智能体原型。",
		"newTitle": "新建原型",
		"editTitle": "编辑原型",
		"nameLabel": "名称",
		"descLabel": "描述",
		"commandLabel": "命令（创建）",
		"resumeCommandLabel": "续接命令（可选）",
		"resumeHint": "在两个模板中都填入 {prompt} 和 {agent_id}，即可让该原型变为有状态（先创建后续接）。",
		"cwdLabel": "工作目录（可选）",
		"timeoutLabel": "超时（秒）"
	},
	"subagent-monitor": {
		"title": "实例",
		"empty": "本会话暂无子智能体实例。",
		"sessionId": "会话 id",
		"action": {
			"create": "新建",
			"resume": "续接",
			"unknown": "调用"
		}
	},
```
Ensure `common.save` exists (`"save": "保存"`).

- [ ] **Step 3: Remove dead keys**

Grep for the retired keys and remove them from both locales only after confirming no references remain:
```bash
grep -rn "dialog-subagent-add\|panel.subagent" examples/web_ui/frontend/src
```
Remove the `dialog-subagent-add` block and the `panel.subagent` block from both `en.json` and `zh.json` only if the grep shows no `.tsx`/`.ts` references (the `panel.subagent.title` reference was replaced by `subagent-monitor.title` in Task 9; if any reference remains, fix it first).

- [ ] **Step 4: Verify typecheck + lint + JSON validity**

Run: `pnpm -C examples/web_ui/frontend build && pnpm -C examples/web_ui/frontend lint`
Expected: PASS. (A trailing-comma / JSON syntax error will fail the build — validate both JSON files.)

- [ ] **Step 5: Commit**

```bash
git add examples/web_ui/frontend/src/i18n/locales/en.json examples/web_ui/frontend/src/i18n/locales/zh.json
git commit -m "chore(webui): i18n for sub-agent sidebar + instance monitor"
```

---

## Task 12: End-to-end verification

**Files:** none (verification only)

- [ ] **Step 1: Backend suite + hooks**

Run: `pytest tests -q && pre-commit run --all-files`
Expected: all tests pass; all hooks green.

- [ ] **Step 2: Frontend build + lint**

Run: `pnpm -C examples/web_ui/frontend build && pnpm -C examples/web_ui/frontend lint`
Expected: PASS.

- [ ] **Step 3: Manual smoke (requires Redis + the web UI running — see project run setup)**

1. In the left Sub-Agents sidebar, create a stateful prototype:
   - name `claude_code`, command `claude -p {prompt} --permission-mode auto --session-id {agent_id}`, resume command `claude -p {prompt} --permission-mode auto --resume {agent_id}`.
2. In chat, ask the agent to delegate to `claude_code` with an instance handle `writer`, then delegate again to the same `writer`.
3. Verify: transcript shows `[writer · new]` then `[writer · resumed]`; the right-dock **Instances** panel lists `writer` with 2 exchanges; expanding shows both prompt→output pairs.
4. Create a stateless prototype (no resume command) and confirm it still runs one-shot with a `{prompt}`-only call and no badge.

- [ ] **Step 4: Open a PR (optional, when ready)**

```bash
git push -u origin feat/subagent-instances
gh pr create --title "feat(subagent): stateful sub-agent instances + prototype sidebar + instance monitor" --body "Implements docs/superpowers/specs/2026-07-16-subagent-instances-design.md"
```

---

## Notes on decomposition

- **Backend before frontend:** Tasks 1–6 are self-contained and independently testable (pytest). Tasks 7–11 are frontend; Task 8 and Task 9 are tightly coupled (Task 8 removes `ChatViewport`'s `useSubagents()` call; Task 9 swaps the panel that used its now-removed vars) — do them back-to-back and treat the `build` at the end of Task 9 as their joint gate.
- **YAGNI:** no forget/kill endpoint, no Redis-persisted instance history (monitor derives from the transcript), per the spec's non-goals.
- **Type consistency check:** `resume_command` (snake_case) in Python + TS; registry `resolve() -> (agent_id, created)`; tool metadata keys `instance`/`agent_id`/`action` with `action ∈ {"create","resume"}`; monitor/badge read those exact keys.
