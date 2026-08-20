# Per-Session Working Directory + Sub-Agent Call Labeling Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give each chat session its own working directory (reusing the existing `workspaces/` mechanism, isolated per session, with a user-settable absolute-path override below the chat box), default the CLI sub-agents to it, and label sub-agent tool calls as "Call Sub-Agent" in the web UI.

**Architecture:** The session's `workspace.workdir` is the single source of truth. Backend: make the `LocalWorkspaceManager` folder layout follow `workspace_id` (so `IsolationPolicy.PER_SESSION` actually isolates), add an optional `workdir` override param to `get_workspace`, persist a per-session `work_dir` on `SessionConfig`, and thread that override into every session→workspace caller (chat turn, workspace router, sub-agent factory). Sub-Agent `cwd` precedence becomes: per-sub-agent `cwd` > session `work_dir` > default session folder. Frontend: a working-directory control below `<TextInput/>` that PATCHes `work_dir`, and a "Call Sub-Agent" label branch driven by the global sub-agent name set.

**Tech Stack:** Python 3.11 / Pydantic v2 / FastAPI (backend); React + TypeScript + Vite + i18next + Tailwind (frontend). Tests: `unittest` run under `pytest`, `fakeredis` / FastAPI `TestClient`.

## Global Constraints

- **Base folder unchanged:** the workspace base directory stays named `workspaces/`. `work_dir` is the user's *term* for the per-session working directory (== `workspace.workdir`); do NOT rename anything on disk or in code.
- **Per-session isolation:** the on-disk folder is keyed by `workspace_id` (not `agent_id`); the reference service opts into `IsolationPolicy.PER_SESSION`. The library default isolation stays `PER_AGENT`.
- **cwd precedence (sub-agents):** per-sub-agent `config.cwd` (if set) > session `work_dir` (== resolved `workspace.workdir`) > `None` (process cwd, defensive fallback only).
- **`work_dir` value rule:** when set, `SessionConfig.work_dir` MUST be an absolute path (`os.path.isabs`); an invalid value on PATCH returns HTTP 422, never 500.
- **Additive:** the only edit to shared workspace internals is the `workspace_id`-keyed layout + the additive `workdir` kwarg. Non-local managers accept-and-ignore the kwarg.
- **UI label:** sub-agent tool calls read "Call Sub-Agent" / "调用子智能体"; the collapsed group summary counts them as "delegated to N sub-agents" / "调用 N 个子智能体".
- **Python style:** black line length 79; internal files/classes/functions are `_`-prefixed and exposed only via `__init__.py`; third-party imports lazy (not relevant here — all imports are stdlib/pydantic/fastapi already in use); English docstrings with the `Args:`/`Returns:` template.
- **Test conventions:** files are `tests/*_test.py`; assert whole data structures; use `AnyString`/`AnyValue` from `tests/utils.py` (import as `from utils import AnyString`) for nondeterministic fields; run with `python -m pytest` in the `ravenx` conda env from the repo root.
- **CI pins:** black 23.3.0, flake8 6.1.0 (via pre-commit). Do not reformat with a newer black.
- **Frontend:** source uses **tabs**; the typecheck gate is `pnpm -C examples/web_ui/frontend exec tsc -b`; keep `en.json` and `zh.json` key sets in parity.
- **Branch:** all work lands on `feat/session-working-directory`.

---

### Task 1: `SessionConfig.work_dir` field + absolute-path validator

**Files:**
- Modify: `src/agentscope/app/storage/_model/_session.py` (imports; `SessionConfig`, after line 146)
- Test: `tests/session_config_test.py` (create)

**Interfaces:**
- Produces: `SessionConfig.work_dir: str | None` (default `None`; when set, must be absolute). Consumed by Tasks 3, 4, 5.

- [ ] **Step 1: Write the failing test**

Create `tests/session_config_test.py`:

```python
# -*- coding: utf-8 -*-
"""Unit tests for SessionConfig.work_dir validation."""
import unittest

import pydantic

from agentscope.app.storage import SessionConfig


class SessionConfigWorkDirTest(unittest.TestCase):
    """Validate the work_dir field default + absolute-path rule."""

    def test_defaults_to_none(self) -> None:
        """work_dir defaults to None when omitted."""
        config = SessionConfig(workspace_id="ws1")
        self.assertIsNone(config.work_dir)

    def test_accepts_absolute_path(self) -> None:
        """An absolute path is stored verbatim."""
        config = SessionConfig(workspace_id="ws1", work_dir="/srv/work")
        self.assertEqual(config.work_dir, "/srv/work")

    def test_rejects_relative_path(self) -> None:
        """A relative path raises a validation error."""
        with self.assertRaises(pydantic.ValidationError):
            SessionConfig(workspace_id="ws1", work_dir="relative/dir")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/session_config_test.py -v`
Expected: FAIL — `test_rejects_relative_path` fails (no validator yet) or `test_accepts_absolute_path`/`test_defaults_to_none` fail with `TypeError`/unknown-field, because `work_dir` does not exist yet.

- [ ] **Step 3: Add the field + validator**

In `src/agentscope/app/storage/_model/_session.py`, update the top imports:

```python
# -*- coding: utf-8 -*-
"""The session data class for storage."""
import os
from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field, field_validator

from ._base import _RecordBase
from ....state import AgentState
```

Then, inside `class SessionConfig`, add the field + validator immediately after the `knowledge_config` field (after line 146):

```python
    work_dir: str | None = Field(
        default=None,
        description=(
            "Optional working-directory override for the session. When "
            "set it must be an absolute path and becomes the working "
            "directory used by the session's agent tools and CLI "
            "sub-agents (unless a sub-agent pins its own ``cwd``). "
            "``None`` means the session uses its default workspace "
            "directory (``basedir/<workspace_id>``)."
        ),
    )
    """The session working-directory override (absolute path), or None."""

    @field_validator("work_dir")
    @classmethod
    def _require_absolute_work_dir(cls, value: str | None) -> str | None:
        """Ensure an explicit ``work_dir`` is an absolute path.

        Args:
            value (`str | None`):
                The working-directory override to validate.

        Returns:
            `str | None`:
                The validated value (``None`` passes through unchanged).
        """
        if value is not None and not os.path.isabs(value):
            raise ValueError("work_dir must be an absolute path")
        return value
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/session_config_test.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/app/storage/_model/_session.py tests/session_config_test.py
git commit -m "feat(session): add work_dir override field to SessionConfig"
```

---

### Task 2: `workdir` override in `get_workspace` + per-session folder layout

**Files:**
- Modify: `src/agentscope/app/workspace_manager/_local_workspace_manager.py` (class docstring; `get_workspace` lines 70-140; `create_workspace` lines 147-172; add `_generate_id` import)
- Modify: `src/agentscope/app/workspace_manager/_base.py` (`get_workspace` abstract signature/docstring lines 96-117)
- Modify: `src/agentscope/app/workspace_manager/_docker_workspace_manager.py` (`get_workspace` signature)
- Modify: `src/agentscope/app/workspace_manager/_e2b_workspace_manager.py` (`get_workspace` signature)
- Modify: `src/agentscope/app/workspace_manager/_daytona_workspace_manager.py` (`get_workspace` signature)
- Modify: `src/agentscope/app/workspace_manager/_k8s_workspace_manager.py` (`get_workspace` signature)
- Modify: `src/agentscope/app/workspace_manager/_opensandbox_workspace_manager.py` (`get_workspace` signature)
- Modify: `tests/utils.py` (`FakeWorkspaceManager.get_workspace` signature, line 164)
- Test: `tests/workspace_manager_local_test.py` (create)

**Interfaces:**
- Produces: `WorkspaceManagerBase.get_workspace(user_id, agent_id, session_id, workspace_id=None, *, workdir=None)`. When `workdir` is a non-empty string, the local manager uses it as the workspace's `workdir` (overriding `basedir/<workspace_id>`) and rebuilds a cached workspace whose `workdir` no longer matches. Consumed by Tasks 3, 4.

- [ ] **Step 1: Write the failing test**

Create `tests/workspace_manager_local_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for LocalWorkspaceManager per-session layout + workdir override."""
import os
import tempfile
import unittest

from agentscope.app.workspace_manager import LocalWorkspaceManager


class LocalWorkspaceManagerWorkdirTest(unittest.IsolatedAsyncioTestCase):
    """Per-session folder keying, workdir override, and rebuild-on-change."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.basedir = self._tmp.name

    def tearDown(self) -> None:
        self._tmp.cleanup()

    async def test_folder_keyed_by_workspace_id(self) -> None:
        """Distinct workspace_ids resolve to distinct folders under basedir."""
        async with LocalWorkspaceManager(basedir=self.basedir) as mgr:
            ws_a = await mgr.get_workspace("u", "a", "s1", "wid-a")
            ws_b = await mgr.get_workspace("u", "a", "s2", "wid-b")
            self.assertEqual(ws_a.workdir, os.path.join(self.basedir, "wid-a"))
            self.assertEqual(ws_b.workdir, os.path.join(self.basedir, "wid-b"))

    async def test_workdir_override_wins(self) -> None:
        """An explicit workdir override is used verbatim (abspath'd)."""
        override = os.path.join(self.basedir, "custom", "repo")
        async with LocalWorkspaceManager(basedir=self.basedir) as mgr:
            ws = await mgr.get_workspace(
                "u",
                "a",
                "s1",
                "wid-a",
                workdir=override,
            )
            self.assertEqual(ws.workdir, os.path.abspath(override))

    async def test_override_change_rebuilds(self) -> None:
        """Changing the override for the same id rebuilds the workspace."""
        first = os.path.join(self.basedir, "first")
        second = os.path.join(self.basedir, "second")
        async with LocalWorkspaceManager(basedir=self.basedir) as mgr:
            ws1 = await mgr.get_workspace("u", "a", "s", "wid", workdir=first)
            ws2 = await mgr.get_workspace("u", "a", "s", "wid", workdir=second)
            self.assertEqual(ws1.workdir, os.path.abspath(first))
            self.assertEqual(ws2.workdir, os.path.abspath(second))
            self.assertIsNot(ws1, ws2)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/workspace_manager_local_test.py -v`
Expected: FAIL — `test_folder_keyed_by_workspace_id` fails (folder is currently `basedir/agent_id`, i.e. `basedir/a`), and the `workdir=` tests fail with `TypeError: get_workspace() got an unexpected keyword argument 'workdir'`.

- [ ] **Step 3: Rewrite `LocalWorkspaceManager.get_workspace` + `create_workspace`**

In `src/agentscope/app/workspace_manager/_local_workspace_manager.py`:

Update the import line (add `_generate_id`):

```python
from ..._logging import logger
from ..._utils._common import _generate_id
from ...workspace import LocalWorkspace
from ._base import WorkspaceManagerBase, IsolationPolicy
```

Update the class docstring's "On cache miss" sentence to read:

```python
    """Manages LocalWorkspace instances with TTL-based lazy lifecycle.

    Workspaces are keyed by ``workspace_id`` in the cache. On cache miss
    the manager reconstructs the workspace at ``basedir/<workspace_id>``
    (so the on-disk layout follows the configured
    :class:`IsolationPolicy`), unless an explicit ``workdir`` override is
    supplied — the workdir is deterministic for local workspaces so no
    storage lookup is needed.
    """
```

Replace the whole `get_workspace` method (lines 70-140) with:

```python
    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str | None = None,
        *,
        workdir: str | None = None,
    ) -> LocalWorkspace:
        """Return an initialized workspace, reconstructing from disk on
        cache miss.

        The on-disk workdir defaults to ``basedir/<workspace_id>`` so the
        layout follows the configured isolation policy. An explicit
        ``workdir`` override (the session's ``work_dir``) wins; a cached
        workspace whose ``workdir`` no longer matches the requested one is
        evicted and rebuilt so a mid-session change takes effect.

        Mirrors the Docker / E2B managers' double-check pattern: a first
        lock acquisition handles the cache-hit fast path and collects
        expired entries; expired / stale entries are then closed in
        parallel *outside* the lock; on a miss a second acquisition runs
        ``initialize()`` while holding the lock so two concurrent cache
        misses for the same ``workspace_id`` cannot create two workspaces.

        Args:
            user_id (`str`):
                Accepted for interface parity; not used here.
            agent_id (`str`):
                The agent id (used only to derive a fallback
                ``workspace_id`` when none is supplied).
            session_id (`str`):
                Accepted for interface parity; not used here.
            workspace_id (`str | None`, optional):
                Explicit workspace binding and cache key. ``None``
                triggers the :meth:`assign_workspace_id` fallback.
            workdir (`str | None`, optional):
                Explicit working-directory override. When set it wins
                over ``basedir/<workspace_id>``.

        Returns:
            `LocalWorkspace`:
                The initialized workspace.
        """
        del user_id, session_id  # accepted for interface parity

        if workspace_id is None:
            workspace_id = self.assign_workspace_id(
                user_id="",
                agent_id=agent_id,
                session_id="",
            )

        target_workdir = os.path.abspath(
            workdir
            if workdir
            else os.path.join(self._basedir, workspace_id),
        )

        # Phase 1: cache hit (matching workdir) + collect expired / stale.
        async with self._lock:
            now = time.monotonic()
            expired = self._pop_expired(now)
            stale: LocalWorkspace | None = None
            hit: LocalWorkspace | None = None
            cached = self._cache.get(workspace_id)
            if cached is not None:
                ws, _ = cached
                if ws.workdir == target_workdir:
                    self._cache[workspace_id] = (ws, now)
                    hit = ws
                else:
                    # The override changed — evict and rebuild below.
                    stale = self._cache.pop(workspace_id)[0]

        # Phase 2: close expired + stale entries outside the lock, in
        # parallel, so a slow stdio MCP shutdown does not block callers.
        to_close = list(expired)
        if stale is not None:
            to_close.append(stale)
        if to_close:
            await asyncio.gather(
                *(self._safe_close(ws) for ws in to_close),
                return_exceptions=True,
            )

        if hit is not None:
            return hit

        # Phase 3: build under the lock to prevent two concurrent
        # get_workspace(workspace_id=X) calls from creating two workspaces
        # for the same id.
        async with self._lock:
            cached = self._cache.get(workspace_id)
            if cached is not None and cached[0].workdir == target_workdir:
                ws, _ = cached
                self._cache[workspace_id] = (ws, time.monotonic())
                return ws

            ws = LocalWorkspace(
                workspace_id=workspace_id,
                workdir=target_workdir,
                default_mcps=self._default_mcps,
                skill_paths=self._skill_paths,
            )
            await ws.initialize()
            self._cache[workspace_id] = (ws, time.monotonic())
            return ws
```

Update the deprecated `create_workspace` (lines 147-172) so its workdir is keyed by a freshly minted id (consistent per-call folder rather than shared `agent_id`):

```python
    @deprecated(
        "LocalWorkspaceManager.create_workspace is deprecated; "
        "use get_workspace(workspace_id=None) instead.",
        category=None,
    )
    async def create_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> LocalWorkspace:
        """Create a new workspace for the given agent and return it.

        .. deprecated::
            Use :meth:`get_workspace` with ``workspace_id=None`` — it
            falls back to :meth:`assign_workspace_id` under the manager's
            isolation policy and reuses the cache path.
        """
        del user_id, agent_id, session_id  # accepted for interface parity

        workspace_id = _generate_id()
        workdir = os.path.join(self._basedir, workspace_id)
        os.makedirs(workdir, exist_ok=True)
        ws = LocalWorkspace(
            workspace_id=workspace_id,
            workdir=workdir,
            default_mcps=self._default_mcps,
            skill_paths=self._skill_paths,
        )
        await ws.initialize()
        async with self._lock:
            self._cache[ws.workspace_id] = (ws, time.monotonic())
        return ws
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/workspace_manager_local_test.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Propagate the `workdir` kwarg to the abstract base + other managers + test fake**

This keeps every `get_workspace` override signature-compatible with the base (mypy/LSP). None of these act on `workdir` — they accept-and-ignore it.

In `src/agentscope/app/workspace_manager/_base.py`, replace the abstract `get_workspace` (lines 96-117) with:

```python
    @abstractmethod
    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str | None = None,
        *,
        workdir: str | None = None,
    ) -> WorkspaceBase:
        """Return an initialized workspace.

        Args:
            user_id (`str`):
                The user id.
            agent_id (`str`):
                The agent id.
            session_id (`str`):
                The session id.
            workspace_id (`str | None`, optional):
                Explicit workspace binding. ``None`` triggers
                :meth:`assign_workspace_id` fallback — expected only for
                callers without a persisted binding.
            workdir (`str | None`, optional):
                Optional working-directory override. Only the local
                manager honors it; other backends accept and ignore it.
        """
```

In each of `_docker_workspace_manager.py`, `_e2b_workspace_manager.py`, `_daytona_workspace_manager.py`, `_k8s_workspace_manager.py`, `_opensandbox_workspace_manager.py`: locate the `async def get_workspace(` signature and add the `workdir` keyword-only parameter plus a `del workdir` line at the top of the body. Concretely, change the signature's closing lines from:

```python
        workspace_id: str | None = None,
    ) -> <BackendWorkspaceType>:
```

to:

```python
        workspace_id: str | None = None,
        *,
        workdir: str | None = None,
    ) -> <BackendWorkspaceType>:
```

and add, as the first statement of the method body (after the existing docstring), a line that discards the unused override so lint does not flag it:

```python
        del workdir  # honored only by the local manager
```

(Keep each manager's existing `del user_id`/etc. lines; just append `workdir` handling. If a method already starts with a `del ...` statement, add `workdir` to that same `del`.)

In `tests/utils.py`, update `FakeWorkspaceManager.get_workspace` (line 164) to match:

```python
    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str | None = None,
        *,
        workdir: str | None = None,
    ) -> WorkspaceBase:
        """Not implemented — team-tool tests never touch this."""
        raise NotImplementedError
```

- [ ] **Step 6: Run the workspace-manager test + the fake's consumers + type check**

Run: `python -m pytest tests/workspace_manager_local_test.py -v`
Expected: PASS (3 passed).

Run (guard against a broken fake signature used by team-tool tests):
`python -m pytest tests/ -q -k "team or workspace or session"`
Expected: PASS (no errors/failures introduced).

- [ ] **Step 7: Commit**

```bash
git add src/agentscope/app/workspace_manager/ tests/utils.py tests/workspace_manager_local_test.py
git commit -m "feat(workspace): per-session folder layout + workdir override in get_workspace"
```

---

### Task 3: Sub-Agent factory defaults `cwd` to the session working directory

**Files:**
- Modify: `src/agentscope/subagent/_agent_tools.py` (`_resolve_backend` → `_resolve_workspace`, lines 14-49; `_factory` tool construction, lines 88-112)
- Test: `tests/subagent_agent_tools_test.py` (update fakes + assertions; add precedence test)

**Interfaces:**
- Consumes: `SessionConfig.work_dir` (Task 1), `get_workspace(..., workdir=...)` (Task 2).
- Produces: each built `CliSubAgentTool` has `cwd = config.cwd or <session workspace.workdir>`.

- [ ] **Step 1: Update the test (fakes + assertions) — this is the failing test**

In `tests/subagent_agent_tools_test.py`, replace `_FakeWorkspace` and `_record` and adjust the assertions so the tests express the new `cwd` behavior.

Replace the `_FakeWorkspace` class (lines 14-22) with one that also exposes a `workdir`:

```python
class _FakeWorkspace:
    """Workspace exposing a fixed backend + workdir."""

    def __init__(self, backend: object, workdir: str) -> None:
        self._backend = backend
        self.workdir = workdir

    def get_backend(self) -> object:
        """Return the fixed backend."""
        return self._backend
```

Replace the `_record` helper (lines 53-68) so it accepts an optional `cwd`:

```python
def _record(
    subagent_id: str,
    name: str,
    command: str,
    cwd: str | None = None,
) -> SubAgentRecord:
    """Build a sub-agent record."""
    return SubAgentRecord(
        id=subagent_id,
        user_id="u1",
        data={
            "id": subagent_id,
            "type": "cli_subagent",
            "name": name,
            "description": "d",
            "command": command,
            "cwd": cwd,
            "env": None,
            "timeout": 600,
        },
    )
```

In `test_builds_one_tool_per_config`, change the workspace construction and add a `cwd` assertion. Replace its body with:

```python
    async def test_builds_one_tool_per_config(self) -> None:
        """Each stored config yields one CliSubAgentTool rooted at workdir."""
        backend = _Sentinel()
        storage = _FakeStorage(
            [
                _record("sa-1", "claude_code", "claude -p {prompt}"),
                _record("sa-2", "codex", "codex exec {prompt}"),
            ],
        )
        wm = _FakeWorkspaceManager(_FakeWorkspace(backend, "/ws/dir"))
        factory = make_subagent_tool_factory(storage, wm)
        tools = await factory("u1", "a1", "s1")
        self.assertEqual(len(tools), 2)
        self.assertTrue(all(isinstance(t, CliSubAgentTool) for t in tools))
        self.assertEqual({t.name for t in tools}, {"claude_code", "codex"})
        # Backend from the workspace is bound to each tool.
        self.assertIs(tools[0]._backend, backend)
        # With no per-sub-agent cwd, the tool defaults to the session
        # workspace workdir.
        self.assertEqual(tools[0]._cwd, "/ws/dir")
        self.assertEqual(tools[1]._cwd, "/ws/dir")
```

Add a new precedence test at the end of the class:

```python
    async def test_config_cwd_overrides_session_workdir(self) -> None:
        """A per-sub-agent cwd wins over the session workspace workdir."""
        backend = _Sentinel()
        storage = _FakeStorage(
            [_record("sa-1", "claude_code", "claude -p {prompt}", cwd="/pin")],
        )
        wm = _FakeWorkspaceManager(_FakeWorkspace(backend, "/ws/dir"))
        factory = make_subagent_tool_factory(storage, wm)
        tools = await factory("u1", "a1", "s1")
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0]._cwd, "/pin")
```

(The existing `test_skips_malformed_config` and `test_falls_back_to_local_backend` use `_FakeWorkspaceManager(None)`, which raises inside `get_workspace` → the factory falls back to `(None, None)`; those tools get `cwd=None` and a `LocalBackend`. Leave them as-is — they still pass.)

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/subagent_agent_tools_test.py -v`
Expected: FAIL — `test_builds_one_tool_per_config` fails on the `_cwd == "/ws/dir"` assertion (current factory passes `cwd=config.cwd`, i.e. `None`), and `test_config_cwd_overrides_session_workdir` fails for the same reason / because the factory doesn't read `workspace.workdir` yet.

- [ ] **Step 3: Rewrite the resolver + tool construction**

In `src/agentscope/subagent/_agent_tools.py`, replace `_resolve_backend` (lines 14-49) with `_resolve_workspace`:

```python
async def _resolve_workspace(
    storage: Any,
    workspace_manager: Any,
    user_id: str,
    agent_id: str,
    session_id: str,
) -> tuple[BackendBase | None, str | None]:
    """Best-effort resolution of the session's workspace backend + workdir.

    Returns ``(None, None)`` on any failure so the tool falls back to a
    :class:`LocalBackend` and the sub-agent's own ``cwd`` (or the process
    cwd). Honors the session's ``work_dir`` override when present, so
    sub-agents run in the same directory as the session's ``Bash`` tool.

    Args:
        storage (`Any`): The app storage backend.
        workspace_manager (`Any`): The workspace manager.
        user_id (`str`): The owner user id.
        agent_id (`str`): The agent id.
        session_id (`str`): The session id.

    Returns:
        `tuple[BackendBase | None, str | None]`:
            The resolved backend and workspace workdir, or ``(None, None)``.
    """
    try:
        session = await storage.get_session(user_id, agent_id, session_id)
        workspace_id = session.config.workspace_id if session else None
        work_dir = session.config.work_dir if session else None
        workspace = await workspace_manager.get_workspace(
            user_id,
            agent_id,
            session_id,
            workspace_id,
            workdir=work_dir,
        )
        return workspace.get_backend(), workspace.workdir
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive
        return None, None
```

In `_factory` (lines 79-112), replace the backend resolution + tool construction. Change:

```python
        records = await storage.list_subagents(user_id)
        backend = await _resolve_backend(
            storage,
            workspace_manager,
            user_id,
            agent_id,
            session_id,
        )
```

to:

```python
        records = await storage.list_subagents(user_id)
        backend, session_workdir = await _resolve_workspace(
            storage,
            workspace_manager,
            user_id,
            agent_id,
            session_id,
        )
```

and change the `CliSubAgentTool(...)` construction's `cwd=config.cwd,` line to:

```python
                    cwd=config.cwd or session_workdir,
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/subagent_agent_tools_test.py -v`
Expected: PASS (4 passed).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_agent_tools.py tests/subagent_agent_tools_test.py
git commit -m "feat(subagent): default sub-agent cwd to session working directory"
```

---

### Task 4: Thread the `work_dir` override into the chat turn + workspace router

**Files:**
- Modify: `src/agentscope/app/_service/_chat.py` (`get_workspace` call, lines 328-333)
- Modify: `src/agentscope/app/_router/_workspace.py` (`_resolve_workspace` helper, lines 55-60)
- Test: `tests/workspace_router_workdir_test.py` (create)

**Interfaces:**
- Consumes: `SessionConfig.work_dir` (Task 1), `get_workspace(..., workdir=...)` (Task 2).

- [ ] **Step 1: Write the failing test**

Create `tests/workspace_router_workdir_test.py`:

```python
# -*- coding: utf-8 -*-
"""The workspace router forwards the session's work_dir to get_workspace."""
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentscope.app._router._workspace import workspace_router
from agentscope.app.storage import SessionConfig, SessionRecord

HEADERS = {"X-User-ID": "u1"}


class _FakeWorkspace:
    """Workspace with an empty skill list."""

    async def list_skills(self) -> list:
        """Return no skills."""
        return []


class _RecordingManager:
    """Workspace manager that records the workdir it was called with."""

    def __init__(self) -> None:
        self.workdirs: list[str | None] = []

    async def get_workspace(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
        workspace_id: str | None = None,
        *,
        workdir: str | None = None,
    ) -> _FakeWorkspace:
        """Record the workdir override and return a fake workspace."""
        self.workdirs.append(workdir)
        return _FakeWorkspace()


class _FakeStorage:
    """Storage returning one fixed session."""

    def __init__(self, session: SessionRecord) -> None:
        self._session = session

    async def get_session(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> SessionRecord:
        """Return the fixed session."""
        return self._session


class WorkspaceRouterWorkdirTest(unittest.TestCase):
    """The router passes SessionConfig.work_dir through to get_workspace."""

    def test_list_skills_forwards_work_dir(self) -> None:
        """GET /workspace/skill forwards the session's work_dir override."""
        session = SessionRecord(
            user_id="u1",
            agent_id="a1",
            config=SessionConfig(workspace_id="ws1", work_dir="/custom/wd"),
        )
        manager = _RecordingManager()
        app = FastAPI()
        app.state.storage = _FakeStorage(session)
        app.state.workspace_manager = manager
        app.include_router(workspace_router)
        client = TestClient(app)

        resp = client.get(
            "/workspace/skill",
            params={"agent_id": "a1", "session_id": "s1"},
            headers=HEADERS,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(manager.workdirs, ["/custom/wd"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/workspace_router_workdir_test.py -v`
Expected: FAIL — `manager.workdirs` is `[None]` (the router does not forward `work_dir` yet), so the assertion `== ["/custom/wd"]` fails.

- [ ] **Step 3: Forward `work_dir` in the workspace router helper**

In `src/agentscope/app/_router/_workspace.py`, update the `_resolve_workspace` helper's `get_workspace` call (lines 55-60) to pass the override:

```python
    return await workspace_manager.get_workspace(
        user_id,
        agent_id,
        session_id,
        session_record.config.workspace_id,
        workdir=session_record.config.work_dir,
    )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/workspace_router_workdir_test.py -v`
Expected: PASS (1 passed).

- [ ] **Step 5: Forward `work_dir` in the chat turn**

In `src/agentscope/app/_service/_chat.py`, update the `get_workspace` call (lines 328-333) to pass the override:

```python
        workspace = await self._workspace_manager.get_workspace(
            user_id,
            agent_id,
            session_id,
            session_record.config.workspace_id,
            workdir=session_record.config.work_dir,
        )
```

This one-line change is exercised end-to-end by the smoke test in Task 6 (the chat service is not unit-tested in isolation; the override mechanism itself is covered by Tasks 2-4).

- [ ] **Step 6: Commit**

```bash
git add src/agentscope/app/_router/_workspace.py src/agentscope/app/_service/_chat.py tests/workspace_router_workdir_test.py
git commit -m "feat(session): thread work_dir override into chat turn + workspace router"
```

---

### Task 5: `UpdateSessionRequest.work_dir` + PATCH 422 on invalid path

**Files:**
- Modify: `src/agentscope/app/_router/_schema/_session.py` (`UpdateSessionRequest`, after line 137)
- Modify: `src/agentscope/app/_router/_session.py` (add `ValidationError` import; wrap the `SessionConfig.model_validate` in `update_session`, lines 492-500)
- Test: `tests/session_router_workdir_test.py` (create)

**Interfaces:**
- Consumes: `SessionConfig.work_dir` + its validator (Task 1).
- Produces: `PATCH /sessions/{id}` accepts `work_dir` (absolute → 200, relative → 422, `null` → cleared).

- [ ] **Step 1: Write the failing test**

Create `tests/session_router_workdir_test.py`:

```python
# -*- coding: utf-8 -*-
"""PATCH /sessions/{id} work_dir behavior (set / clear / reject relative)."""
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentscope.app._router import session_router
from agentscope.app.storage import SessionConfig, SessionRecord

HEADERS = {"X-User-ID": "u1"}


class _FakeSessionStorage:
    """Minimal in-memory session storage for the PATCH path."""

    def __init__(self) -> None:
        self._sessions: dict[str, SessionRecord] = {}

    async def get_session(
        self,
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> SessionRecord | None:
        """Return one session or None."""
        return self._sessions.get(session_id)

    async def upsert_session(
        self,
        *,
        user_id: str,
        agent_id: str,
        config: SessionConfig,
        state: object = None,
        session_id: str | None = None,
    ) -> SessionRecord:
        """Insert (session_id=None) or update the config/state in place."""
        if session_id is None:
            rec = SessionRecord(
                user_id=user_id,
                agent_id=agent_id,
                config=config,
            )
        else:
            update: dict = {"config": config}
            if state is not None:
                update["state"] = state
            rec = self._sessions[session_id].model_copy(update=update)
        self._sessions[rec.id] = rec
        return rec


def _make_client() -> tuple[TestClient, str]:
    """Build an app exposing only the session router + a seeded session."""
    storage = _FakeSessionStorage()
    seeded = SessionRecord(
        user_id="u1",
        agent_id="a1",
        config=SessionConfig(workspace_id="ws1"),
    )
    storage._sessions[seeded.id] = seeded
    app = FastAPI()
    app.state.storage = storage
    # update_session only touches `access` for non-None credential / KB
    # configs; a work_dir-only PATCH never calls it.
    app.state.resource_access_service = object()
    app.include_router(session_router)
    return TestClient(app), seeded.id


class SessionRouterWorkDirTest(unittest.TestCase):
    """Set, reject, and clear work_dir via PATCH."""

    def _patch(self, client: TestClient, sid: str, body: dict):
        return client.patch(
            f"/sessions/{sid}",
            params={"agent_id": "a1"},
            json=body,
            headers=HEADERS,
        )

    def test_set_absolute_path(self) -> None:
        """An absolute work_dir is accepted and persisted."""
        client, sid = _make_client()
        resp = self._patch(client, sid, {"work_dir": "/abs/x"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["config"]["work_dir"], "/abs/x")

    def test_reject_relative_path(self) -> None:
        """A relative work_dir is a 422, not a 500."""
        client, sid = _make_client()
        resp = self._patch(client, sid, {"work_dir": "relative/x"})
        self.assertEqual(resp.status_code, 422)

    def test_clear_with_null(self) -> None:
        """Sending null clears the override back to None."""
        client, sid = _make_client()
        self._patch(client, sid, {"work_dir": "/abs/x"})
        resp = self._patch(client, sid, {"work_dir": None})
        self.assertEqual(resp.status_code, 200)
        self.assertIsNone(resp.json()["config"]["work_dir"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/session_router_workdir_test.py -v`
Expected: FAIL — `test_set_absolute_path` fails because `UpdateSessionRequest` drops the unknown `work_dir` field (so the response `config.work_dir` stays `None`); `test_reject_relative_path` returns 500 (uncaught `ValidationError`) instead of 422.

- [ ] **Step 3: Add `work_dir` to `UpdateSessionRequest`**

In `src/agentscope/app/_router/_schema/_session.py`, inside `UpdateSessionRequest`, add after the `knowledge_config` field (after line 137, before `permission_mode`):

```python
    work_dir: str | None = Field(
        default=None,
        description=(
            "New session working-directory override (absolute path). "
            "PATCH semantics: omit → leave unchanged; null → reset to "
            "the default session workspace directory; a value → use it. "
            "A relative path is rejected with HTTP 422."
        ),
    )
```

- [ ] **Step 4: Convert `ValidationError` to 422 in `update_session`**

In `src/agentscope/app/_router/_session.py`, add the import near the other imports at the top of the file:

```python
from pydantic import ValidationError
```

Then wrap the `SessionConfig.model_validate` call in `update_session` (lines 492-500). Replace:

```python
    return await storage.upsert_session(
        user_id=user_id,
        agent_id=agent_id,
        config=SessionConfig.model_validate(
            {**existing.config.model_dump(mode="json"), **config_updates},
        ),
        state=updated_state,
        session_id=session_id,
    )
```

with:

```python
    try:
        new_config = SessionConfig.model_validate(
            {**existing.config.model_dump(mode="json"), **config_updates},
        )
    except ValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    return await storage.upsert_session(
        user_id=user_id,
        agent_id=agent_id,
        config=new_config,
        state=updated_state,
        session_id=session_id,
    )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/session_router_workdir_test.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add src/agentscope/app/_router/_schema/_session.py src/agentscope/app/_router/_session.py tests/session_router_workdir_test.py
git commit -m "feat(session): accept work_dir on PATCH /sessions with 422 on relative path"
```

---

### Task 6: Opt the reference service into per-session isolation + backend regression run

**Files:**
- Modify: `examples/agent_service/main.py` (import `IsolationPolicy`; `LocalWorkspaceManager(...)` construction, lines 50-57)

**Interfaces:**
- Consumes: Tasks 1-5. No new interface.

- [ ] **Step 1: Switch the reference workspace manager to `PER_SESSION`**

In `examples/agent_service/main.py`, ensure `IsolationPolicy` is imported from the workspace-manager package (add it to the existing `from agentscope.app.workspace_manager import ...` line, or add a new import):

```python
from agentscope.app.workspace_manager import (
    IsolationPolicy,
    LocalWorkspaceManager,
)
```

Then add `isolation=IsolationPolicy.PER_SESSION` to the `LocalWorkspaceManager(...)` call (keep the existing `basedir=` and `default_mcps=` arguments):

```python
    workspace_manager = LocalWorkspaceManager(
        basedir=os.path.join(os.path.dirname(__file__), "workspaces"),
        isolation=IsolationPolicy.PER_SESSION,
        default_mcps=default_mcps,
    )
```

(Match the exact existing argument names/values already present in `main.py`; only add the `isolation=` line and the import.)

- [ ] **Step 2: Byte-check the example imports**

Run: `python -c "import ast; ast.parse(open('examples/agent_service/main.py').read()); print('ok')"`
Expected: `ok`

- [ ] **Step 3: Run the full backend regression for touched areas**

Run: `python -m pytest tests/ -q`
Expected: PASS — all tests green (existing suite + the four new test files). No failures or errors.

If any pre-existing test asserts a `basedir/<agent_id>` workspace path (none found at plan time), update it to the `basedir/<workspace_id>` layout and note it in the task report.

- [ ] **Step 4: Lint the changed Python files**

Run: `pre-commit run --files $(git diff --name-only main -- '*.py' | tr '\n' ' ')`
Expected: PASS (black/flake8/pylint/mypy/docstring checks clean). This lints every Python file changed on the branch vs `main`. Fix any reported issue in code; do not disable checks. (pre-commit uses the repo-pinned black 23.3.0 / flake8 6.1.0 — do not substitute a newer black.)

- [ ] **Step 5: Commit**

```bash
git add examples/agent_service/main.py
git commit -m "feat(example): run reference agent service with per-session workspaces"
```

---

### Task 7: Working-directory control below the chat box (frontend)

**Files:**
- Modify: `examples/web_ui/frontend/src/api/types.ts` (`SessionConfig`, `UpdateSessionRequest`)
- Create: `examples/web_ui/frontend/src/components/chat/WorkingDirectoryControl.tsx`
- Modify: `examples/web_ui/frontend/src/components/chat/ChatContent.tsx` (new `belowInputSlot` prop + render)
- Modify: `examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx` (state, sync, handler, wire the slot)

**Interfaces:**
- Consumes: `sessionApi.update` (existing), `SessionConfig.work_dir` (Task 1, JSON).
- Produces: `WorkingDirectoryControl` component; `ChatContentProps.belowInputSlot`.

- [ ] **Step 1: Add `work_dir` to the frontend session types**

In `examples/web_ui/frontend/src/api/types.ts`, add to `interface SessionConfig` (after `workspace_id: string;`, line 125):

```ts
	/** Session working-directory override (absolute path), or null. */
	work_dir?: string | null;
```

And add to `interface UpdateSessionRequest` (after `knowledge_config?: ...`, before `permission_mode?`, line 190):

```ts
	/**
	 * New working-directory override. PATCH semantics:
	 *   - omit the field → leave unchanged
	 *   - set to `null`  → reset to the default session workspace dir
	 *   - set to a value → use this absolute path (relative → 422)
	 */
	work_dir?: string | null;
```

- [ ] **Step 2: Create the control component**

Create `examples/web_ui/frontend/src/components/chat/WorkingDirectoryControl.tsx`:

```tsx
import { FolderOpen } from 'lucide-react';
import { useEffect, useState } from 'react';

import { useTranslation } from '@/i18n/useI18n';
import { cn } from '@/lib/utils';

interface WorkingDirectoryControlProps {
	/** Current override, or null when the session uses its default dir. */
	value: string | null;
	/** Disable editing (e.g. no session selected yet). */
	disabled?: boolean;
	/**
	 * Persist a new override. Called with the trimmed absolute path, or
	 * `null` to clear the override. Not called when the value is unchanged.
	 */
	onChange: (path: string | null) => void | Promise<void>;
}

/**
 * A compact row below the chat input for viewing / setting the session's
 * working directory. An empty field means "use the default session
 * workspace"; a non-empty value must be an absolute path. Commits on blur
 * or Enter; shows an inline hint when the path is not absolute.
 */
export function WorkingDirectoryControl({
	value,
	disabled,
	onChange,
}: WorkingDirectoryControlProps) {
	const { t } = useTranslation();
	const [draft, setDraft] = useState(value ?? '');
	const [invalid, setInvalid] = useState(false);

	// Re-sync the draft when the persisted value changes (session switch).
	useEffect(() => {
		setDraft(value ?? '');
		setInvalid(false);
	}, [value]);

	const commit = () => {
		const trimmed = draft.trim();
		if (trimmed === '') {
			setInvalid(false);
			if (value !== null) void onChange(null);
			return;
		}
		if (!trimmed.startsWith('/')) {
			setInvalid(true);
			return;
		}
		setInvalid(false);
		if (trimmed !== value) void onChange(trimmed);
	};

	return (
		<div className="flex w-full items-center gap-2 px-1 text-xs text-muted-foreground">
			<FolderOpen className="size-3.5 shrink-0" />
			<input
				type="text"
				value={draft}
				disabled={disabled}
				aria-label={t('workingDirectory.label')}
				placeholder={t('workingDirectory.placeholder')}
				onChange={(e) => setDraft(e.target.value)}
				onBlur={commit}
				onKeyDown={(e) => {
					if (e.key === 'Enter') {
						e.preventDefault();
						commit();
					}
				}}
				className={cn(
					'min-w-0 flex-1 border-b border-transparent bg-transparent py-0.5 outline-none focus:border-border',
					invalid && 'border-red-500 focus:border-red-500',
				)}
			/>
			{invalid && (
				<span className="shrink-0 text-red-500">
					{t('workingDirectory.mustBeAbsolute')}
				</span>
			)}
		</div>
	);
}
```

- [ ] **Step 3: Add a slot below the input in `ChatContent`**

In `examples/web_ui/frontend/src/components/chat/ChatContent.tsx`, add to `interface ChatContentProps` (after `footerSlot?`, line 38):

```tsx
	/**
	 * Optional content pinned directly below the text input (e.g. the
	 * working-directory control). Rendered as the last child of the chat
	 * column, beneath `<TextInput/>`.
	 */
	belowInputSlot?: React.ReactNode;
```

Add `belowInputSlot` to the destructured props (in the component signature, after `footerSlot,`):

```tsx
	footerSlot,
	belowInputSlot,
```

Render it after `<TextInput .../>` (after line 132, before the closing `</div>`):

```tsx
			<TextInput
				className="min-w-full max-w-full w-full"
				onSend={onSend}
				disabled={disabled}
				autoComplete={autoComplete}
				allowedInputTypes={allowedInputTypes}
				fileProcessor={fileProcessor}
				phase={phase}
				onInterrupt={onInterrupt}
			/>
			{belowInputSlot ? (
				<div className="w-full max-w-full shrink-0">{belowInputSlot}</div>
			) : null}
		</div>
```

- [ ] **Step 4: Wire the control in `ChatViewport`**

In `examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx`:

Add the import (with the other `@/components/chat/...` imports):

```tsx
import { WorkingDirectoryControl } from '@/components/chat/WorkingDirectoryControl';
```

Add local state next to the other `selected*` state (after `selectedPermissionMode`, line 143):

```tsx
	const [selectedWorkDir, setSelectedWorkDir] = useState<string | null>(null);
```

In the "reset local UI state when the target session changes" effect (lines 363-368), add a reset:

```tsx
	useEffect(() => {
		setSelectedModel(null);
		setSelectedFallbackModel(null);
		setSelectedTTSModel(null);
		setSelectedKnowledgeConfig(null);
		setSelectedWorkDir(null);
	}, [sessionId]);
```

In the "Sync selectedModel + ..." effect (lines 449-473), add a sync for work_dir alongside the other `setSelected*` at the end of the effect body (after `setSelectedKnowledgeConfig(...)`, line 472):

```tsx
		setSelectedWorkDir(view.session.config.work_dir ?? null);
```

Add a handler next to `handlePermissionModeChange` (after line 546):

```tsx
	/**
	 * Persist a working-directory override. `null` resets to the default
	 * session workspace directory.
	 *
	 * @param path - New absolute working directory, or `null` to reset.
	 */
	const handleWorkDirChange = async (path: string | null) => {
		if (!sessionId || !agentId) return;
		setSelectedWorkDir(path);
		await sessionApi.update(sessionId, agentId, { work_dir: path });
		await refetchSessions();
	};
```

Pass the control into `ChatContent` as `belowInputSlot` (add the prop to the `<ChatContent ... />` element, e.g. right after `footerSlot={...}` at line 674):

```tsx
									belowInputSlot={
										sessionId ? (
											<WorkingDirectoryControl
												value={selectedWorkDir}
												disabled={!sessionId}
												onChange={handleWorkDirChange}
											/>
										) : null
									}
```

- [ ] **Step 5: Add i18n keys**

In `examples/web_ui/frontend/src/i18n/locales/en.json`, add a new top-level section (place it near the other UI sections, e.g. after the `textInput` block) — match the file's existing indentation (tabs):

```json
	"workingDirectory": {
		"label": "Working directory",
		"placeholder": "Default session workspace",
		"mustBeAbsolute": "Must be an absolute path"
	},
```

In `examples/web_ui/frontend/src/i18n/locales/zh.json`, add the parallel section:

```json
	"workingDirectory": {
		"label": "工作目录",
		"placeholder": "默认会话工作目录",
		"mustBeAbsolute": "必须是绝对路径"
	},
```

- [ ] **Step 6: Typecheck**

Run: `pnpm -C examples/web_ui/frontend exec tsc -b`
Expected: exit 0, no type errors.

- [ ] **Step 7: Commit**

```bash
git add examples/web_ui/frontend/src/api/types.ts examples/web_ui/frontend/src/components/chat/WorkingDirectoryControl.tsx examples/web_ui/frontend/src/components/chat/ChatContent.tsx examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx examples/web_ui/frontend/src/i18n/locales/en.json examples/web_ui/frontend/src/i18n/locales/zh.json
git commit -m "feat(webui): working-directory control below the chat input"
```

---

### Task 8: "Call Sub-Agent" tool-call labeling (frontend)

**Files:**
- Create: `examples/web_ui/frontend/src/components/chat/SubagentNamesContext.tsx`
- Modify: `examples/web_ui/frontend/src/components/chat/tool-renderers/DefaultRenderer.tsx` (`defaultRenderHeader`)
- Modify: `examples/web_ui/frontend/src/components/chat/tool-renderers/index.tsx` (`renderToolCall`)
- Modify: `examples/web_ui/frontend/src/components/chat/MessageBubble.tsx` (`summarizeToolGroup`, `renderBlock`, `MessageBubble`)
- Modify: `examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx` (provider)
- Modify: `examples/web_ui/frontend/src/i18n/locales/en.json`, `zh.json` (`tool.callSubagent`, `tool.summary.subagent_*`)

**Interfaces:**
- Consumes: `useSubagents()` → `SubAgentView[]` with `data.name` (existing).
- Produces: `SubagentNamesContext` (a `Set<string>` of registered sub-agent names) + `useSubagentNames()`.

- [ ] **Step 1: Create the context**

Create `examples/web_ui/frontend/src/components/chat/SubagentNamesContext.tsx`:

```tsx
import { createContext, useContext } from 'react';

/**
 * The set of registered sub-agent tool names for the current user. Used by
 * the tool-call renderers to label a call as "Call Sub-Agent" rather than
 * the generic "Call tool". Empty by default (no sub-agents / no provider).
 */
export const SubagentNamesContext = createContext<Set<string>>(new Set());

/** Read the current sub-agent name set. */
export function useSubagentNames(): Set<string> {
	return useContext(SubagentNamesContext);
}
```

- [ ] **Step 2: Branch the default header on sub-agent identity**

In `examples/web_ui/frontend/src/components/chat/tool-renderers/DefaultRenderer.tsx`, replace `defaultRenderHeader` (lines 25-32):

```tsx
/**
 * Default trigger line for tools without a custom `renderHeader` (e.g. MCP
 * tools and CLI sub-agents): a "Call tool" / "Call Sub-Agent" label followed
 * by the tool name in the shared argument style.
 */
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
		</>
	);
}
```

- [ ] **Step 3: Pass sub-agent identity through `renderToolCall`**

In `examples/web_ui/frontend/src/components/chat/tool-renderers/index.tsx`, replace `renderToolCall` (lines 50-55):

```tsx
export function renderToolCall(
	pair: ToolCallWithResult,
	t: TFunction,
	subagentNames?: Set<string>,
): ReactNode {
	const r = getRenderer(pair.call.name);
	const isSubagent = subagentNames?.has(pair.call.name) ?? false;
	const header = r.renderHeader?.(pair, t) ?? defaultRenderHeader(pair, t, isSubagent);
	const body = r.renderBody?.(pair, t) ?? defaultRenderBody(pair, t);
	return <ToolCallRow key={pair.call.id} pair={pair} header={header} body={body} />;
}
```

- [ ] **Step 4: Thread the set through `MessageBubble` + summarize sub-agents**

In `examples/web_ui/frontend/src/components/chat/MessageBubble.tsx`:

Add the import (with the other local imports):

```tsx
import { useSubagentNames } from './SubagentNamesContext';
```

Replace the `summarizeToolGroup` signature + counting loop + parts assembly (lines 310-350) so it counts sub-agents first and adds a summary part. Change the signature and add the counter:

```tsx
function summarizeToolGroup(
	calls: ToolCallWithResult[],
	t: TFunction,
	subagentNames: Set<string>,
) {
	let nBash = 0;
	let nRead = 0;
	let nEdit = 0;
	let nSearch = 0;
	let nTodo = 0;
	let nMCP = 0;
	let nSubagent = 0;
	let insertions = 0;
	let deletions = 0;

	for (const { call, result } of calls) {
		const name = call.name;
		if (subagentNames.has(name)) {
			nSubagent += 1;
		} else if (name === 'Bash') {
			nBash += 1;
		} else if (name === 'Read') {
			nRead += 1;
		} else if (name === 'Edit' || name === 'Write') {
			nEdit += 1;
			// Sum the real +/- line changes from the backend-provided diff.
			const diff = result ? getResultDiff(result) : undefined;
			if (diff) {
				const stats = countDiffStats(diff);
				insertions += stats.insertions;
				deletions += stats.deletions;
			}
		} else if (name === 'Grep' || name === 'Glob') {
			nSearch += 1;
		} else if (TODO_TOOLS.has(name)) {
			nTodo += 1;
		} else if (name.startsWith(MCP_TOOL_PREFIX)) {
			nMCP += 1;
		}
	}

	const parts: string[] = [];
	if (nBash > 0) parts.push(t('tool.summary.bash', { count: nBash }));
	if (nRead > 0) parts.push(t('tool.summary.read', { count: nRead }));
	if (nEdit > 0) parts.push(t('tool.summary.edit', { count: nEdit }));
	if (nSearch > 0) parts.push(t('tool.summary.search', { count: nSearch }));
	if (nTodo > 0) parts.push(t('tool.summary.todo', { count: nTodo }));
	if (nMCP > 0) parts.push(t('tool.summary.mcp', { count: nMCP }));
	if (nSubagent > 0) parts.push(t('tool.summary.subagent', { count: nSubagent }));
```

(Leave the `joined`/`title` computation below unchanged.)

Change the `renderBlock` signature to accept `subagentNames` (line 370), and pass it into `summarizeToolGroup` and `renderToolCall`:

```tsx
function renderBlock(
	block: ExtendedContentBlock,
	index: number,
	t: TFunction,
	subagentNames: Set<string>,
	onUserConfirm?: (
		toolCallBlock: ToolCallBlock,
		confirm: boolean,
		rules?: ToolCallBlock['suggested_rules'],
	) => void,
) {
```

Inside `renderBlock`, update the `summarizeToolGroup` call (line 382):

```tsx
				const { title, insertions, deletions } = summarizeToolGroup(
					block.calls,
					t,
					subagentNames,
				);
```

update the `renderToolCall` call (line 414):

```tsx
								{visible.map((pair) => renderToolCall(pair, t, subagentNames))}
```

and update the recursive `renderBlock` call in the `hint` case (line 563):

```tsx
									{items.map((inner, i) =>
										renderBlock(inner, i, t, subagentNames),
									)}
```

In the `MessageBubble` component body, read the context near the top (after `const { t } = useTranslation();`, line 605):

```tsx
	const subagentNames = useSubagentNames();
```

and pass it into the `renderBlock` call in the render (line 647):

```tsx
						{blocks.map((block, i) =>
							renderBlock(
								block,
								i,
								t,
								subagentNames,
								(
									toolCall: ToolCallBlock,
									confirm: boolean,
									rules?: ToolCallBlock['suggested_rules'],
								) => {
									onUserConfirm(toolCall, confirm, message.id, rules);
									toolCall.state = confirm ? 'allowed' : 'finished';
								},
							),
						)}
```

- [ ] **Step 5: Provide the set from `ChatViewport`**

In `examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx`:

Add the import:

```tsx
import { SubagentNamesContext } from '@/components/chat/SubagentNamesContext';
```

Compute the set from the already-present `subagents` (add near the other `useMemo`s, e.g. after the `panels` memo, before `const view = ...` at line 342):

```tsx
	const subagentNameSet = useMemo(
		() => new Set(subagents.map((s) => s.data.name)),
		[subagents],
	);
```

Wrap the returned tree in the provider. Change the outer `return ( <> ... </> );` so the fragment becomes the provider:

```tsx
	return (
		<SubagentNamesContext.Provider value={subagentNameSet}>
			<main className="flex size-full">
				{/* ...unchanged... */}
			</main>
			<CreateCredentialDialog
				open={credentialOpen}
				onOpenChange={setCredentialOpen}
				onCreated={() => setCredentialRefetchTrigger((n) => n + 1)}
			/>
		</SubagentNamesContext.Provider>
	);
```

(Replace only the wrapping `<>` / `</>` with the `<SubagentNamesContext.Provider value={subagentNameSet}>` / `</SubagentNamesContext.Provider>` tags; keep `<main>...</main>` and `<CreateCredentialDialog .../>` exactly as they are.)

- [ ] **Step 6: Add i18n keys**

In `examples/web_ui/frontend/src/i18n/locales/en.json`, inside the `"tool"` object add (after `"callGeneric": "Call tool",`):

```json
		"callSubagent": "Call Sub-Agent",
```

and inside `"tool"."summary"` add (after the `mcp_other` entry):

```json
			"subagent_one": "delegated to {{count}} sub-agent",
			"subagent_other": "delegated to {{count}} sub-agents",
```

In `examples/web_ui/frontend/src/i18n/locales/zh.json`, inside `"tool"` add (after `"callGeneric": "调用工具",`):

```json
		"callSubagent": "调用子智能体",
```

and inside `"tool"."summary"` add (after the `mcp_other` entry):

```json
			"subagent_one": "调用 {{count}} 个子智能体",
			"subagent_other": "调用 {{count}} 个子智能体",
```

- [ ] **Step 7: Typecheck**

Run: `pnpm -C examples/web_ui/frontend exec tsc -b`
Expected: exit 0, no type errors.

- [ ] **Step 8: Commit**

```bash
git add examples/web_ui/frontend/src/components/chat/SubagentNamesContext.tsx examples/web_ui/frontend/src/components/chat/tool-renderers/DefaultRenderer.tsx examples/web_ui/frontend/src/components/chat/tool-renderers/index.tsx examples/web_ui/frontend/src/components/chat/MessageBubble.tsx examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx examples/web_ui/frontend/src/i18n/locales/en.json examples/web_ui/frontend/src/i18n/locales/zh.json
git commit -m "feat(webui): label CLI sub-agent tool calls as Call Sub-Agent"
```

---

## Final verification (after all tasks)

- [ ] Backend: `python -m pytest tests/ -q` → all green.
- [ ] Backend lint: `pre-commit run --files <changed *.py>` → clean.
- [ ] Frontend: `pnpm -C examples/web_ui/frontend exec tsc -b` → exit 0.
- [ ] Frontend build: `pnpm -C examples/web_ui/frontend build` → succeeds.
- [ ] Manual smoke (optional, needs redis + the service on :8001 + UI on :5173): create two sessions under one agent, confirm each gets its own `workspaces/<workspace_id>/` folder; set a custom absolute path below the chat box and confirm a sub-agent (and `Bash`) runs there; confirm a sub-agent call renders "Call Sub-Agent".
