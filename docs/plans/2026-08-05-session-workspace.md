# Session Workspace Implementation Plan

> **Superseded — kept as a task record.** This plan was written for, and
> executed against, the per-session design that was later withdrawn. Two things
> it specifies are no longer true: the gateway isolates per **channel**, not per
> session, and there is no `<agent home>/ws` — a channel's directory comes from
> `channels.<name>.workspace`, defaulting to `~/.raven/tmp/<channel>`. See
> [the design doc](../specs/2026-08-05-session-workspace-design.md) for what
> actually shipped.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give every session its own working directory that the leader and all its sub-agents share, defaulting to the launch directory on `raven tui` / `raven agent` and to `<agent home>/ws/<channel>/<chat_id>/` on `raven gateway`, with a manual override.

**Architecture:** One new module (`raven/agent/workdir.py`) owns the resolution rules and a per-turn `ContextVar`. `AgentLoop` binds that variable once per turn next to the existing per-turn tool-context wiring. Path-aware tools read it at call time and fall back to their constructor value; components that outlive the turn (sub-agents, DAG runs) capture it explicitly at call time. Agent home (`~/.raven/workspace`) stays global and keeps holding user memory, skills, transcripts and the memory store.

**Tech Stack:** Python 3.12+, `uv`, pytest, Typer (CLI), FastAPI (webui service), React + TypeScript (webui frontend).

**Spec:** [docs/specs/2026-08-05-session-workspace-design.md](../specs/2026-08-05-session-workspace-design.md)

## Global Constraints

- Package manager is `uv` only. Never `pip`, never hand-edit `pyproject.toml` dependency tables or `uv.lock`. (AGENTS.md section 4)
- Run tests as `uv run pytest ...`, never bare `pytest`. (AGENTS.md section 5.4)
- Code comments in English, and only where the logic is non-obvious. Do not add comments that restate what the code does or mark an edit. (AGENTS.md section 1)
- Commit messages are Conventional Commits, entirely ASCII English, header at most 100 characters, with a `Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>` trailer. (AGENTS.md section 3)
- CLI tests go in the existing `tests/test_cli_<module>_commands.py`. Do not create new per-feature CLI test files. (AGENTS.md section 5.1)
- **Before Task 1, confirm the branch base with the user** and cut a branch named `feat/session_workspace` from it. Do not start editing on `main`. (AGENTS.md section 2.2)
- The commit step inside each task is authorized by the user starting execution of this plan. Nothing here authorizes a push or a PR; ask separately. (AGENTS.md sections 3.4, 3.6)
- Agent home is `config.workspace_path` (`~/.raven/workspace` by default) and must keep holding user memory, skills, transcripts, the Skill Hub cache and the memory store. No task moves any of those.

---

## File Structure

**Created:**

| File | Responsibility |
|---|---|
| `raven/agent/workdir.py` | The only place that knows the resolution rules, the directory layout, override validation, and the per-turn `ContextVar` |
| `tests/test_agent_workdir.py` | Unit tests for the resolver in isolation |
| `tests/test_tools_workdir.py` | Path tools honour the bound working directory and the two-root fence |
| `ui-webui/frontend/src/api/ravenSessionWorkdir.ts` | Frontend client for the per-session workdir endpoint |

**Modified:**

| File | Change |
|---|---|
| `raven/agent/tools/filesystem.py` | `_resolve_path` takes multiple allowed roots; `_FsTool` reads the bound workdir |
| `raven/agent/tools/shell.py` | `ExecTool` resolves cwd from the binding; fence accepts agent home as a second root |
| `raven/agent/tools/deliver.py` | Same fallback rule as the filesystem tools |
| `raven/agent/tools/media_gen.py` | Output directory derives from the bound workdir |
| `raven/agent/loop/main.py` | Accepts a resolver, binds the `ContextVar` per turn, passes two fence roots, hands the workdir to sub-agent and DAG calls, caches one `CheckpointService` per working directory, builds the executor on the mount root |
| `raven/sandbox/__init__.py` | `build_executor` accepts extra volumes so agent home stays reachable inside the VM |
| `raven/agent/subagent/manager.py` | `run_subagent` accepts and forwards a per-call workspace |
| `raven/agent/subagent_dag/tool.py` | `run_dag` uses the per-call workdir |
| `raven/cli/_helpers.py` | `load_runtime_config` gains a `home` override; `workspace` stops writing agent home |
| `raven/cli/agent_commands.py`, `tui_commands.py`, `gateway_commands.py` | Flags, policy selection, resolver construction |
| `raven/cli/sentinel_commands.py` | `--help` text only |
| `raven/web_rpc/methods_config.py` | `raven.session.workdir.get` / `.set` |
| `ui-webui/service/raven_config_routes.py` | `GET` / `PUT /sessions/{key}/workdir` |
| `ui-webui/frontend/src/api/index.ts`, `pages/chat/ChatViewport.tsx` | Wire the control |
| `tests/test_cli_{agent,gateway,tui}_commands.py` | Flag parsing and defaults |
| `CONTEXT.md` | Term definitions |

---

## Task 1: The workdir resolver module

Self-contained: no other module imports it yet, so it can be written and tested alone.

**Files:**
- Create: `raven/agent/workdir.py`
- Test: `tests/test_agent_workdir.py`

**Interfaces:**
- Consumes: `raven.utils.helpers.safe_filename` (existing, `raven/utils/helpers.py:195`).
- Produces:
  - `WorkdirPolicy.LAUNCH_DIR` / `WorkdirPolicy.PER_SESSION`
  - `WorkdirResolver(policy, *, agent_home: Path, launch_dir: Path | None = None, session_root: Path | None = None, sessions: Any = None, explicit_workdir: Path | None = None)`
  - `WorkdirResolver.resolve(session_key: str) -> Path`
  - `WorkdirResolver.mount_root() -> Path` — the single directory that contains
    every working directory this process can produce. Task 6 mounts it into the
    sandbox VM.
  - `validate_override(value: str | Path, agent_home: Path) -> Path`
  - `current() -> Path | None`
  - `bind(path: Path)` — a context manager

- [ ] **Step 1: Write the failing tests**

Create `tests/test_agent_workdir.py`:

```python
"""Unit tests for the session working-directory resolver."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.workdir import (
    WorkdirPolicy,
    WorkdirResolver,
    bind,
    current,
    validate_override,
)


class _StubSessions:
    """Stands in for SessionManager: only ``get_or_create`` is used."""

    def __init__(self, metadata_by_key: dict[str, dict]) -> None:
        self._metadata_by_key = metadata_by_key

    def get_or_create(self, key: str):
        return SimpleNamespace(metadata=self._metadata_by_key.get(key, {}))


def test_per_session_mirrors_the_transcript_layout(tmp_path: Path) -> None:
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_SESSION,
        agent_home=tmp_path,
        session_root=tmp_path / "ws",
    )
    assert resolver.resolve("web:2fb833e6") == tmp_path / "ws" / "web" / "2fb833e6"


def test_per_session_creates_the_directory(tmp_path: Path) -> None:
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_SESSION,
        agent_home=tmp_path,
        session_root=tmp_path / "ws",
    )
    assert resolver.resolve("web:abc").is_dir()


def test_colonless_key_gets_a_placeholder_chat_id(tmp_path: Path) -> None:
    """``heartbeat`` is the one session key in the codebase with no colon."""
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_SESSION,
        agent_home=tmp_path,
        session_root=tmp_path / "ws",
    )
    assert resolver.resolve("heartbeat") == tmp_path / "ws" / "heartbeat" / "_"


def test_chat_id_with_separators_is_sanitised(tmp_path: Path) -> None:
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_SESSION,
        agent_home=tmp_path,
        session_root=tmp_path / "ws",
    )
    resolved = resolver.resolve("whatsapp:../../etc/passwd")
    assert (tmp_path / "ws") in resolved.parents


def test_launch_dir_ignores_the_session_key(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    resolver = WorkdirResolver(
        WorkdirPolicy.LAUNCH_DIR,
        agent_home=tmp_path / "home",
        launch_dir=project,
    )
    assert resolver.resolve("cli:one") == project
    assert resolver.resolve("cli:two") == project


def test_persisted_override_beats_the_policy_default(tmp_path: Path) -> None:
    pinned = tmp_path / "pinned"
    pinned.mkdir()
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_SESSION,
        agent_home=tmp_path,
        session_root=tmp_path / "ws",
        sessions=_StubSessions({"web:abc": {"workdir": str(pinned)}}),
    )
    assert resolver.resolve("web:abc") == pinned


def test_explicit_workdir_beats_the_persisted_override(tmp_path: Path) -> None:
    """A flag typed for this run wins over anything on disk."""
    pinned = tmp_path / "pinned"
    flagged = tmp_path / "flagged"
    pinned.mkdir()
    flagged.mkdir()
    resolver = WorkdirResolver(
        WorkdirPolicy.LAUNCH_DIR,
        agent_home=tmp_path,
        launch_dir=tmp_path,
        explicit_workdir=flagged,
        sessions=_StubSessions({"cli:abc": {"workdir": str(pinned)}}),
    )
    assert resolver.resolve("cli:abc") == flagged


def test_override_must_be_absolute(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="absolute"):
        validate_override("relative/path", tmp_path)


@pytest.mark.parametrize("subtree", ["user_memory", "skills", "sessions"])
def test_override_cannot_point_into_agent_home_internals(tmp_path: Path, subtree: str) -> None:
    with pytest.raises(ValueError, match=subtree):
        validate_override(tmp_path / subtree / "nested", tmp_path)


def test_override_may_point_at_agent_home_itself(tmp_path: Path) -> None:
    assert validate_override(tmp_path, tmp_path) == tmp_path


def test_binding_is_scoped(tmp_path: Path) -> None:
    assert current() is None
    with bind(tmp_path):
        assert current() == tmp_path
    assert current() is None


def test_mount_root_covers_every_per_session_directory(tmp_path: Path) -> None:
    """One sandbox mount has to contain every directory the process can produce."""
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_SESSION,
        agent_home=tmp_path,
        session_root=tmp_path / "ws",
    )
    root = resolver.mount_root()
    assert root == tmp_path / "ws"
    assert root in resolver.resolve("web:abc").parents


def test_mount_root_is_the_launch_dir_for_terminal_entrypoints(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    resolver = WorkdirResolver(
        WorkdirPolicy.LAUNCH_DIR,
        agent_home=tmp_path / "home",
        launch_dir=project,
    )
    assert resolver.mount_root() == project
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_agent_workdir.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'raven.agent.workdir'`

- [ ] **Step 3: Write the module**

Create `raven/agent/workdir.py`:

```python
"""Resolve the working directory a turn runs in.

Agent home (user memory, skills, transcripts) is global and separate; this
module only decides where a turn reads and writes files.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

from raven.utils.helpers import safe_filename

# Subtrees of agent home the agent must not be able to adopt as a working
# directory: it would then write artifacts over its own memory and skills.
_PROTECTED_SUBTREES = ("user_memory", "skills", "sessions")

_CURRENT: ContextVar[Path | None] = ContextVar("raven_session_workdir", default=None)


class WorkdirPolicy(str, Enum):
    """How an entrypoint picks a working directory when nothing overrides it."""

    LAUNCH_DIR = "launch_dir"
    PER_SESSION = "per_session"


def current() -> Path | None:
    """The working directory bound for the running turn, if any."""
    return _CURRENT.get()


@contextmanager
def bind(path: Path) -> Iterator[None]:
    """Bind ``path`` as the working directory for the enclosing block."""
    token = _CURRENT.set(path)
    try:
        yield
    finally:
        _CURRENT.reset(token)


def validate_override(value: str | Path, agent_home: Path) -> Path:
    """Check a user-supplied working directory, returning it resolved."""
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"working directory must be an absolute path, got {value!r}")
    resolved = path.resolve()
    home = Path(agent_home).expanduser().resolve()
    for subtree in _PROTECTED_SUBTREES:
        candidate = home / subtree
        if resolved == candidate or candidate in resolved.parents:
            raise ValueError(
                f"working directory must not be inside the agent's {subtree} tree ({candidate})"
            )
    return resolved


class WorkdirResolver:
    """Maps a session key to the directory that session's turns work in."""

    def __init__(
        self,
        policy: WorkdirPolicy,
        *,
        agent_home: Path,
        launch_dir: Path | None = None,
        session_root: Path | None = None,
        sessions: Any = None,
        explicit_workdir: Path | None = None,
    ) -> None:
        self._policy = policy
        self._agent_home = Path(agent_home).expanduser()
        self._launch_dir = Path(launch_dir) if launch_dir else Path.cwd()
        self._session_root = Path(session_root) if session_root else self._agent_home / "ws"
        self._sessions = sessions
        self._explicit_workdir = Path(explicit_workdir) if explicit_workdir else None

    def resolve(self, session_key: str) -> Path:
        """Resolve and create the working directory for ``session_key``."""
        path = self._explicit_workdir or self._persisted(session_key) or self._default(session_key)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _persisted(self, session_key: str) -> Path | None:
        if self._sessions is None or not session_key:
            return None
        stored = self._sessions.get_or_create(session_key).metadata.get("workdir")
        if not stored:
            return None
        return validate_override(stored, self._agent_home)

    def _default(self, session_key: str) -> Path:
        if self._policy is WorkdirPolicy.LAUNCH_DIR:
            return self._launch_dir
        channel, _, chat_id = session_key.partition(":")
        return self._session_root / safe_filename(channel) / (safe_filename(chat_id) or "_")

    def mount_root(self) -> Path:
        """The directory a sandbox VM must mount to cover every session.

        Per-session directories all live under one root, so a single mount
        serves them all; a launch-directory process only ever has the one.
        An explicit override outside this root is the caller's problem to
        report -- see the sandbox wiring in the loop.
        """
        if self._policy is WorkdirPolicy.LAUNCH_DIR:
            return self._explicit_workdir or self._launch_dir
        return self._session_root
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agent_workdir.py -v`
Expected: PASS, 14 tests.

- [ ] **Step 5: Commit**

```bash
git add raven/agent/workdir.py tests/test_agent_workdir.py
git commit -m "feat(agent): add the session working-directory resolver

Owns the layout, the precedence chain and the per-turn binding. Nothing
consumes it yet.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Task 2: Path tools read the bound working directory

After this task the tools honour a binding when one exists and behave exactly as before when none does, so nothing changes yet at runtime.

**Files:**
- Modify: `raven/agent/tools/filesystem.py:29-34`
- Modify: `raven/agent/tools/shell.py:168`
- Modify: `raven/agent/tools/deliver.py:43-53`
- Modify: `raven/agent/tools/media_gen.py:103`
- Test: `tests/test_tools_workdir.py`

**Interfaces:**
- Consumes: `raven.agent.workdir.current` from Task 1.
- Produces: no signature changes. Every tool keeps its constructor `workspace` argument as the fallback used when no binding is active.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_tools_workdir.py`:

```python
"""Path tools resolve against the working directory bound for the turn."""

from pathlib import Path

import pytest

from raven.agent.tools.filesystem import ListDirTool, ReadFileTool, WriteFileTool
from raven.agent.tools.shell import ExecTool
from raven.agent.workdir import bind


@pytest.mark.asyncio
async def test_write_file_lands_in_the_bound_workdir(tmp_path: Path) -> None:
    fallback = tmp_path / "fallback"
    session = tmp_path / "session"
    fallback.mkdir()
    session.mkdir()
    tool = WriteFileTool(workspace=fallback)

    with bind(session):
        await tool.execute(path="out.txt", content="hello")

    assert (session / "out.txt").read_text(encoding="utf-8") == "hello"
    assert not (fallback / "out.txt").exists()


@pytest.mark.asyncio
async def test_write_file_falls_back_without_a_binding(tmp_path: Path) -> None:
    fallback = tmp_path / "fallback"
    fallback.mkdir()
    tool = WriteFileTool(workspace=fallback)

    await tool.execute(path="out.txt", content="hello")

    assert (fallback / "out.txt").read_text(encoding="utf-8") == "hello"


@pytest.mark.asyncio
async def test_read_file_resolves_relative_paths_against_the_binding(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    (session / "note.txt").write_text("from session", encoding="utf-8")
    tool = ReadFileTool(workspace=tmp_path / "fallback")

    with bind(session):
        result = await tool.execute(path="note.txt")

    assert "from session" in str(result)


@pytest.mark.asyncio
async def test_list_dir_defaults_to_the_binding(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    (session / "marker.txt").write_text("x", encoding="utf-8")
    tool = ListDirTool(workspace=tmp_path / "fallback")

    with bind(session):
        result = await tool.execute(path=".")

    assert "marker.txt" in str(result)


@pytest.mark.asyncio
async def test_exec_runs_in_the_bound_workdir(tmp_path: Path) -> None:
    session = tmp_path / "session"
    session.mkdir()
    tool = ExecTool(working_dir=str(tmp_path / "fallback"))

    with bind(session):
        result = await tool.execute(command="pwd")

    assert str(session.resolve()) in str(result)


@pytest.mark.asyncio
async def test_exec_per_call_working_dir_still_wins(tmp_path: Path) -> None:
    """The explicit argument outranks the binding, as it did the constructor."""
    session = tmp_path / "session"
    explicit = tmp_path / "explicit"
    session.mkdir()
    explicit.mkdir()
    tool = ExecTool()

    with bind(session):
        result = await tool.execute(command="pwd", working_dir=str(explicit))

    assert str(explicit.resolve()) in str(result)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_tools_workdir.py -v`
Expected: FAIL — the writes land in `fallback`, so `test_write_file_lands_in_the_bound_workdir` fails on the `session / "out.txt"` read with `FileNotFoundError`.

- [ ] **Step 3: Make the tools read the binding**

In `raven/agent/tools/filesystem.py`, add the import and change `_FsTool._resolve`:

```python
from raven.agent import workdir
```

```python
    def _resolve(self, path: str) -> Path:
        return _resolve_path(path, workdir.current() or self._workspace, self._allowed_dir)
```

In `raven/agent/tools/shell.py`, add the same import and change the cwd line (currently line 168):

```python
        cwd = working_dir or str(workdir.current() or "") or self.working_dir or os.getcwd()
```

In `raven/agent/tools/deliver.py`, add the import and replace reads of `self._workspace` in path resolution with `workdir.current() or self._workspace`.

In `raven/agent/tools/media_gen.py`, add the import and change `_output_path`:

```python
    def _output_path(self, ext: str) -> Path:
        out_dir = (workdir.current() or self._workspace) / self._output_subdir
        out_dir.mkdir(parents=True, exist_ok=True)
        return out_dir / f"{self.name}-{uuid.uuid4().hex[:12]}.{ext}"
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_tools_workdir.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Run the neighbouring suites for regressions**

Run: `uv run pytest tests/test_read_file_image.py tests/test_shell_approval.py tests/test_sandbox_unit.py -q`
Expected: PASS, no new failures.

- [ ] **Step 6: Commit**

```bash
git add raven/agent/tools/filesystem.py raven/agent/tools/shell.py \
        raven/agent/tools/deliver.py raven/agent/tools/media_gen.py \
        tests/test_tools_workdir.py
git commit -m "feat(agent): resolve tool paths against the bound working directory

The constructor workspace stays as the fallback, so behaviour is unchanged
until a caller binds a directory.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Task 3: AgentLoop binds the working directory per turn

**Files:**
- Modify: `raven/agent/loop/main.py:292-300` (constructor signature), `:376` (assignment), `:2500` (per-turn wiring)
- Test: `tests/test_agent_loop_workdir.py`

**Interfaces:**
- Consumes: `WorkdirResolver`, `bind` from Task 1.
- Produces:
  - `AgentLoop(..., workdir_resolver: WorkdirResolver | None = None)`
  - `AgentLoop.session_workdir(session_key: str) -> Path` — resolver result, or `self.workspace` when no resolver was supplied. Tasks 4 and 5 call this.

- [ ] **Step 1: Write the failing test**

Create `tests/test_agent_loop_workdir.py`:

```python
"""AgentLoop binds a per-session working directory for the duration of a turn."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.workdir import WorkdirPolicy, WorkdirResolver
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


class _FakeChatProvider:
    """Minimal provider: the turns here never reach the model."""

    def get_default_model(self) -> str:
        return "stub-model"


def _stub_edges(loop: AgentLoop) -> None:
    """No-op the sandbox/MCP bring-up so a turn runs without a VM.

    Same shape as tests/test_agent_loop_run_emit.py:189.
    """

    async def _noop() -> None:
        return None

    loop._start_executor = _noop


def _req(text: str) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="web", chat_id="abc", sender_id="u", chat_type=ChatType.DM),
        text=text,
    )


def _resolver(tmp_path: Path) -> WorkdirResolver:
    return WorkdirResolver(
        WorkdirPolicy.PER_SESSION,
        agent_home=tmp_path,
        session_root=tmp_path / "ws",
    )


def test_session_workdir_uses_the_resolver(tmp_path: Path) -> None:
    loop = AgentLoop(
        provider=_FakeChatProvider(),
        workspace=tmp_path,
        workdir_resolver=_resolver(tmp_path),
    )

    assert loop.session_workdir("web:abc") == tmp_path / "ws" / "web" / "abc"


def test_session_workdir_falls_back_to_the_workspace(tmp_path: Path) -> None:
    """No resolver means the pre-split behaviour: one directory for everything."""
    loop = AgentLoop(provider=_FakeChatProvider(), workspace=tmp_path)

    assert loop.session_workdir("web:abc") == tmp_path
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_agent_loop_workdir.py -v`
Expected: FAIL — `AttributeError: 'AgentLoop' object has no attribute 'session_workdir'`

- [ ] **Step 3: Add the constructor argument and the accessor**

In `raven/agent/loop/main.py`, add to the `__init__` signature (keyword-only region, after `workspace`):

```python
        workdir_resolver: "WorkdirResolver | None" = None,
```

Store it next to `self.workspace = workspace` (line 376):

```python
        self._workdir_resolver = workdir_resolver
```

Add the accessor as a method on `AgentLoop`:

```python
    def session_workdir(self, session_key: str) -> Path:
        """The directory this session's turn works in.

        Without a resolver the loop keeps its pre-split behaviour: every
        session shares ``self.workspace``.
        """
        if self._workdir_resolver is None:
            return self.workspace
        return self._workdir_resolver.resolve(session_key)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_agent_loop_workdir.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: Write the failing test for the per-turn binding**

Append to `tests/test_agent_loop_workdir.py`:

```python
from raven.agent import workdir as workdir_mod


class _EmitCollector:
    def __init__(self) -> None:
        self.events: list = []

    async def __call__(self, ev) -> None:
        self.events.append(ev)


def _drain() -> list:
    return []


@pytest.mark.asyncio
async def test_turn_binds_the_session_workdir(tmp_path: Path) -> None:
    """A turn runs with the binding active and releases it afterwards."""
    seen: list[Path | None] = []
    loop = AgentLoop(
        provider=_FakeChatProvider(),
        workspace=tmp_path,
        workdir_resolver=_resolver(tmp_path),
    )
    _stub_edges(loop)

    original = loop._set_tool_context

    def _spy(*args, **kwargs):
        seen.append(workdir_mod.current())
        return original(*args, **kwargs)

    loop._set_tool_context = _spy
    await loop.run_turn(_req("/help"), _EmitCollector(), _drain)

    assert seen == [tmp_path / "ws" / "web" / "abc"]
    assert workdir_mod.current() is None
```

`/help` is a slash command, so the turn short-circuits before reaching the model
(see `tests/test_agent_loop_run_emit.py:202`) while still passing through the
per-turn wiring this test spies on. If `run_turn`'s signature has drifted, read
`tests/test_agent_loop_run_emit.py:250-255` for the current call shape.

- [ ] **Step 6: Run it to verify it fails**

Run: `uv run pytest tests/test_agent_loop_workdir.py -k binds -v`
Expected: FAIL — `seen == [None]`, because no binding is established.

- [ ] **Step 7: Bind the directory for the turn**

In `raven/agent/loop/main.py`, wrap the turn body so the binding covers it. At the
existing per-turn wiring site (line 2500, `self._set_tool_context(channel, chat_id,
metadata.get("message_id"), session_key=key)`), enter the binding before that call
and exit it when the turn ends, using `contextlib.ExitStack` if the surrounding
function already manages resources, or by wrapping the turn body:

```python
        with workdir.bind(self.session_workdir(key)):
            self._set_tool_context(channel, chat_id, metadata.get("message_id"), session_key=key)
            ...  # existing turn body, unchanged
```

Add `from raven.agent import workdir` to the module imports.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agent_loop_workdir.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 9: Run the agent-loop suite for regressions**

Run: `uv run pytest tests/ -k "agent_loop" -q`
Expected: PASS, no new failures.

- [ ] **Step 10: Commit**

```bash
git add raven/agent/loop/main.py tests/test_agent_loop_workdir.py
git commit -m "feat(agent): bind a per-session working directory for each turn

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Task 4: Sub-agents and DAG runs inherit the session workdir

The binding is a `ContextVar`, which an `asyncio.Task` inherits at creation. That
is not enough on its own: a sub-agent outlives the turn, so it must be handed the
directory explicitly rather than reading a variable whose binding has since been
released.

**Files:**
- Modify: `raven/agent/subagent/manager.py:244`, `:268` and the `run_subagent` entry point
- Modify: `raven/agent/subagent_dag/tool.py:340`
- Modify: `raven/agent/loop/main.py:707-708` (DAG tool construction)
- Test: `tests/test_subagent_workdir.py`

**Interfaces:**
- Consumes: `AgentLoop.session_workdir` from Task 3, `workdir.current` from Task 1.
- Produces: `SubagentManager.spawn(..., workspace: Path | None = None)` — `None` keeps `self.workspace`. The value is carried through `_run_subagent` into `_run_subagent_inner`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_subagent_workdir.py`:

```python
"""A sub-agent works in the directory its spawning turn was bound to."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from raven.agent.subagent import manager as manager_mod
from raven.agent.subagent.manager import SubagentManager
from raven.agent.workdir import bind


class _StubProvider:
    def get_default_model(self) -> str:
        return "stub-model"


class _DummyExecutor:
    async def __aenter__(self) -> "_DummyExecutor":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _RecordingBackend:
    """Records the workspace it is handed, and can block until released."""

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.workspace: Path | None = None
        self._gate = gate

    async def run(self, task: str, *, task_id, workspace, executor, session_key=None, instance=None) -> str:
        if self._gate is not None:
            await self._gate.wait()
        self.workspace = workspace
        return "done"


def _manager(tmp_path: Path, monkeypatch, backend: _RecordingBackend) -> SubagentManager:
    manager = SubagentManager(provider=_StubProvider(), workspace=tmp_path / "fallback")
    monkeypatch.setattr(manager_mod, "build_executor", lambda *a, **k: _DummyExecutor())
    monkeypatch.setattr(manager, "_resolve_backend", lambda agent: backend)
    monkeypatch.setattr(manager, "_announce_result", _noop_announce)
    return manager


async def _noop_announce(*args, **kwargs) -> None:
    return None


@pytest.mark.asyncio
async def test_spawn_captures_the_bound_workdir(tmp_path: Path, monkeypatch) -> None:
    session = tmp_path / "session"
    session.mkdir()
    backend = _RecordingBackend()
    manager = _manager(tmp_path, monkeypatch, backend)

    with bind(session):
        await manager.spawn("do the thing", session_key="web:abc", workspace=session)

    await asyncio.gather(*manager._running_tasks.values())
    assert backend.workspace == session


@pytest.mark.asyncio
async def test_workdir_survives_the_turn_ending(tmp_path: Path, monkeypatch) -> None:
    """The captured value must not be re-read after the binding is released."""
    session = tmp_path / "session"
    session.mkdir()
    gate = asyncio.Event()
    backend = _RecordingBackend(gate)
    manager = _manager(tmp_path, monkeypatch, backend)

    with bind(session):
        await manager.spawn("do the thing", session_key="web:abc", workspace=session)

    gate.set()
    await asyncio.gather(*manager._running_tasks.values())
    assert backend.workspace == session
```

The second test is the one that matters: it releases the binding before the
backend ever runs, so an implementation that reads `workdir.current()` inside the
sub-agent task instead of capturing it at spawn time fails here.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_subagent_workdir.py -v`
Expected: FAIL — `recorded["workspace"]` is the fallback, not `session`.

- [ ] **Step 3: Thread the workspace through the manager**

In `raven/agent/subagent/manager.py`, add a `workspace: Path | None = None` keyword
to `spawn` (`:154`), stash it on the origin dict that `spawn` already builds for
`_run_subagent` (`:231`), carry it into `_run_subagent_inner` (`:253`), and use it
in both places that currently read `self.workspace`:

```python
        effective_workspace = workspace or self.workspace
```

```python
                executor = build_executor(self._sandbox_config, effective_workspace, self._owned_ids)
```

```python
            final_result = await backend.run(
                task,
                task_id=task_id,
                workspace=effective_workspace,
                executor=executor,
                session_key=session_key,
                instance=origin.get("instance"),
            )
```

In `raven/agent/tools/spawn.py`, capture the binding at call time and pass it:

```python
from raven.agent import workdir
```

At `raven/agent/tools/spawn.py:162` the tool already calls `self._manager.spawn(...)`;
add one keyword to that call:

```python
        return await self._manager.spawn(
            ...,
            workspace=workdir.current(),
        )
```

A third-party sub-agent with its own configured `cwd` still wins: `cli_agent.py:188`
resolves `cwd = self.cwd or str(workspace)`, which is deliberate and unchanged.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_subagent_workdir.py -v`
Expected: PASS, 2 tests.

- [ ] **Step 5: Write the failing test for DAG runs**

Append to `tests/test_subagent_workdir.py`:

```python
from raven.agent.subagent_dag import tool as dag_tool_mod
from raven.agent.subagent_dag.tool import SubAgentDagTool


@pytest.mark.asyncio
async def test_dag_run_uses_the_bound_workdir(tmp_path: Path, monkeypatch) -> None:
    session = tmp_path / "session"
    session.mkdir()
    recorded: dict = {}

    async def _fake_run_dag(spec, **kwargs):
        recorded.update(kwargs)
        return {"nodes": {}}

    monkeypatch.setattr(dag_tool_mod, "run_dag", _fake_run_dag)
    tool = SubAgentDagTool(
        workspace=tmp_path / "fallback",
        third_party_subagents=[],
    )

    with bind(session):
        await tool.execute(nodes=[{"id": "a", "agent": "stub", "prompt": "hi"}])

    assert recorded["workdir"] == str(session)
```

`execute` takes `nodes: list[dict]` (`raven/agent/subagent_dag/tool.py:313`), not a
`spec` object. If validation rejects the stub node before `run_dag` is reached,
read the node schema in `raven/agent/subagent_dag/_graph.py` and use a node that
validates — the assertion is about the `workdir` keyword, not the graph.

- [ ] **Step 6: Run it to verify it fails**

Run: `uv run pytest tests/test_subagent_workdir.py -k dag -v`
Expected: FAIL — records the fallback.

- [ ] **Step 7: Use the binding in the DAG tool**

In `raven/agent/subagent_dag/tool.py`, add the import and change the `run_dag` call
(line 340):

```python
from raven.agent import workdir
```

```python
                workdir=str(workdir.current() or self._workspace),
```

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_workdir.py -v`
Expected: PASS, 3 tests.

- [ ] **Step 9: Run the sub-agent and DAG suites**

Run: `uv run pytest tests/ -k "subagent or dag" -q`
Expected: PASS, no new failures.

- [ ] **Step 10: Commit**

```bash
git add raven/agent/subagent/manager.py raven/agent/tools/spawn.py \
        raven/agent/subagent_dag/tool.py tests/test_subagent_workdir.py
git commit -m "feat(agent): run sub-agents and DAG nodes in the session workdir

Captured at call time rather than read from the binding, because a sub-agent
outlives the turn that spawned it.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Task 5: Two-root fence for `restrict_to_workspace`

The fence is off by default (`tools.restrict_to_workspace = False`,
`raven/config/schema.py:731`). When on, it must now admit two roots: the session
working directory and agent home. A single root would lock the agent out of the
memory and skill paths its own system prompt gives it
(`raven/agent/context/builder.py:209-212`).

**Files:**
- Modify: `raven/agent/tools/filesystem.py:12-34`
- Modify: `raven/agent/tools/shell.py:56-65`, `:270-286`
- Modify: `raven/agent/tools/deliver.py:43-53`
- Modify: `raven/agent/loop/main.py:642-653`
- Test: `tests/test_tools_workdir.py` (extend)

**Interfaces:**
- Consumes: `AgentLoop.session_workdir` (Task 3), the binding (Task 1).
- Produces:
  - `_resolve_path(path, workspace=None, allowed_dirs=())`
  - `_FsTool(workspace=None, allowed_dirs=())`
  - `DeliverFilesTool(store, *, workspace=None, allowed_dirs=())`
  - `ExecTool(..., restrict_to_workspace=False, extra_allowed_dirs=())`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_tools_workdir.py`:

```python
@pytest.mark.asyncio
async def test_fence_allows_the_session_workdir(tmp_path: Path) -> None:
    home = tmp_path / "home"
    session = tmp_path / "session"
    home.mkdir()
    session.mkdir()
    tool = WriteFileTool(workspace=session, allowed_dirs=(session, home))

    with bind(session):
        await tool.execute(path=str(session / "a.txt"), content="x")

    assert (session / "a.txt").exists()


@pytest.mark.asyncio
async def test_fence_allows_agent_home(tmp_path: Path) -> None:
    """The agent must still reach the memory paths its system prompt names."""
    home = tmp_path / "home"
    session = tmp_path / "session"
    (home / "user_memory").mkdir(parents=True)
    session.mkdir()
    tool = ReadFileTool(workspace=session, allowed_dirs=(session, home))
    (home / "user_memory" / "profile.md").write_text("me", encoding="utf-8")

    with bind(session):
        result = await tool.execute(path=str(home / "user_memory" / "profile.md"))

    assert "me" in str(result)


@pytest.mark.asyncio
async def test_fence_rejects_outside_both_roots(tmp_path: Path) -> None:
    home = tmp_path / "home"
    session = tmp_path / "session"
    outside = tmp_path / "outside"
    for d in (home, session, outside):
        d.mkdir()
    tool = WriteFileTool(workspace=session, allowed_dirs=(session, home))

    with bind(session):
        result = await tool.execute(path=str(outside / "a.txt"), content="x")

    assert "outside allowed" in str(result)
    assert not (outside / "a.txt").exists()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_tools_workdir.py -k fence -v`
Expected: FAIL — `TypeError: __init__() got an unexpected keyword argument 'allowed_dirs'`

- [ ] **Step 3: Widen the fence to a tuple of roots**

In `raven/agent/tools/filesystem.py`:

```python
def _resolve_path(
    path: str,
    workspace: Path | None = None,
    allowed_dirs: tuple[Path, ...] = (),
) -> Path:
    """Resolve path against workspace (if relative) and enforce the allowed roots."""
    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        p = workspace / p
    resolved = p.resolve()
    if allowed_dirs:
        for allowed in allowed_dirs:
            try:
                resolved.relative_to(Path(allowed).resolve())
                return resolved
            except ValueError:
                continue
        roots = ", ".join(str(d) for d in allowed_dirs)
        raise PermissionError(f"Path {path} is outside allowed directories {roots}")
    return resolved
```

```python
class _FsTool(Tool):
    def __init__(self, workspace: Path | None = None, allowed_dirs: tuple[Path, ...] = ()):
        self._workspace = workspace
        self._allowed_dirs = allowed_dirs

    def _resolve(self, path: str) -> Path:
        return _resolve_path(path, workdir.current() or self._workspace, self._allowed_dirs)
```

Apply the same `allowed_dirs` change to `DeliverFilesTool.__init__`.

In `raven/agent/tools/shell.py`, add an `extra_allowed_dirs: tuple[Path, ...] = ()`
constructor argument, store it, and widen the containment test in
`_check_workspace_restriction`:

```python
        roots = [cwd_path, *(Path(d).resolve() for d in self.extra_allowed_dirs)]
        for raw in self._extract_absolute_paths(cmd):
            try:
                expanded = os.path.expandvars(raw.strip())
                p = Path(expanded).expanduser().resolve()
            except Exception:
                continue
            if not p.is_absolute():
                continue
            if any(root == p or root in p.parents for root in roots):
                continue
            return "Error: Command blocked by safety guard (path outside working dir)"
```

In `raven/agent/loop/main.py`, replace the single-root computation (line 642) and
the tool construction that follows:

```python
        allowed_dirs = (self.workspace,) if self.restrict_to_workspace else ()
        for cls in (ReadFileTool, WriteFileTool, EditFileTool, ListDirTool, GrepTool, FindTool):
            self.tools.register(cls(workspace=self.workspace, allowed_dirs=allowed_dirs))
```

and pass `extra_allowed_dirs=(self.workspace,)` to the `ExecTool` construction so
agent home stays reachable while cwd is the session directory.

Correction (caught during implementation): computing `allowed_dirs` once here and
handing it straight to the tool constructors, as drafted above, admits only agent
home -- tools are built in `AgentLoop.__init__`, before any turn's working
directory exists, so a fenced agent could never write inside its own session
directory. The implemented design keeps this construction-time tuple as the
static half of the fence, but folds the live bound root in at resolve time
instead, in `raven/agent/tools/filesystem.py`:

```python
def _with_current_root(allowed_dirs: tuple[Path, ...], bound: Path | None) -> tuple[Path, ...]:
    if not allowed_dirs or bound is None:
        return allowed_dirs
    return (bound, *allowed_dirs)


class _FsTool(Tool):
    def _resolve(self, path: str) -> Path:
        bound = workdir.current() if self._follow_binding else None
        current_root = bound or self._workspace
        return _resolve_path(path, current_root, _with_current_root(self._allowed_dirs, bound))
```

`ExecTool._check_workspace_restriction` follows the same pattern: it resolves
`cwd` from the live binding (or its own `working_dir`) on every call rather than
from a value captured once at construction.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_tools_workdir.py -v`
Expected: PASS, 9 tests.

- [ ] **Step 5: Find and fix every remaining caller**

Run: `grep -rn "allowed_dir=" raven/ tests/ --include=*.py`
Expected: no hits. Update any that remain, then re-run:
`uv run pytest tests/ -q`
Expected: PASS, no new failures.

- [ ] **Step 6: Commit**

```bash
git add raven/agent/tools/filesystem.py raven/agent/tools/shell.py \
        raven/agent/tools/deliver.py raven/agent/loop/main.py \
        tests/test_tools_workdir.py
git commit -m "feat(agent): fence tools on the session workdir and agent home

A single root would lock the agent out of the memory and skill paths its own
system prompt hands it.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Task 6: Checkpoints and sandbox mounts follow the working directory

Two components still point at agent home after Task 5, which would silently
contradict the spec: shadow-git checkpoints and the sandbox mount.

`CheckpointService` is per-workspace by construction — its shadow git dir must sit
strictly inside the workspace it protects, and the constructor rejects anything
else precisely so two loops on different workspaces cannot share a repo
(`raven/agent/loop/checkpoint.py:137-148`). So it becomes one instance per working
directory, created lazily and cached.

The sandbox needs only one mount, not one VM per session: every per-session
directory lives under `<agent home>/ws`, and a launch-directory process has
exactly one working directory. `WorkdirResolver.mount_root()` returns whichever
applies.

**Files:**
- Modify: `raven/agent/loop/main.py:494-508` (checkpoint construction), `:2217-2222` (commit site), `:540` (executor construction)
- Modify: `raven/sandbox/__init__.py:37-88` (`build_executor` gains `extra_volumes`)
- Test: `tests/test_agent_loop_workdir.py` (extend)

**Interfaces:**
- Consumes: `WorkdirResolver.mount_root()` (Task 1), `AgentLoop.session_workdir` (Task 3).
- Produces: `build_executor(sandbox_cfg, workspace, owned_ids=None, extra_volumes=())`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_agent_workdir.py` nothing; append to `tests/test_agent_loop_workdir.py`:

```python
def test_checkpoints_are_per_working_directory(tmp_path: Path) -> None:
    """Two sessions must not share one shadow git repo."""
    from raven.config.raven import RuntimeConfig

    runtime = RuntimeConfig()
    runtime.checkpoint.policy = "always"
    loop = AgentLoop(
        provider=_FakeChatProvider(),
        workspace=tmp_path,
        workdir_resolver=_resolver(tmp_path),
        runtime_config=runtime,
        interactive=True,
    )

    first = loop._checkpoint_for("web:one")
    second = loop._checkpoint_for("web:two")

    assert first is not second
    assert first is loop._checkpoint_for("web:one")
    assert first._workspace == tmp_path / "ws" / "web" / "one"


def test_sandbox_mounts_the_root_covering_every_session(tmp_path: Path, monkeypatch) -> None:
    from raven.sandbox import config as sandbox_config_mod  # noqa: F401

    recorded: dict = {}

    def _fake_build_executor(cfg, workspace, owned_ids=None, extra_volumes=()):
        recorded["workspace"] = workspace
        recorded["extra_volumes"] = list(extra_volumes)

        class _Stub:
            pass

        return _Stub()

    monkeypatch.setattr("raven.agent.loop.main.build_executor", _fake_build_executor)
    AgentLoop(
        provider=_FakeChatProvider(),
        workspace=tmp_path,
        workdir_resolver=_resolver(tmp_path),
    )

    assert recorded["workspace"] == tmp_path / "ws"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_agent_loop_workdir.py -k "checkpoint or sandbox" -v`
Expected: FAIL — `AttributeError: 'AgentLoop' object has no attribute '_checkpoint_for'`, and the executor is built with `tmp_path`.

- [ ] **Step 3: Make checkpoints per working directory**

In `raven/agent/loop/main.py`, replace the single `self._checkpoint` instance with a
cache plus an accessor. Keep the existing `ValueError` handling — a bad
`shadow_dir` must still disable the safety net rather than crash the turn:

```python
        self._checkpoints: dict[Path, "CheckpointService | None"] = {}
```

```python
    def _checkpoint_for(self, session_key: str) -> "CheckpointService | None":
        """The shadow-git service for this session's working directory.

        One per directory: CheckpointService puts its git dir inside the
        directory it protects, so sharing one across working directories would
        cross-contaminate their edited-file sets.
        """
        if not self._checkpoint_enabled:
            return None
        target = self.session_workdir(session_key)
        if target not in self._checkpoints:
            from raven.agent.loop.checkpoint import CheckpointService

            try:
                self._checkpoints[target] = CheckpointService(
                    target,
                    shadow_dir=self.runtime_config.checkpoint.shadow_dir,
                )
            except ValueError as exc:
                logger.warning("runtime.checkpoint disabled for {} -- {}", target, exc)
                self._checkpoints[target] = None
        return self._checkpoints[target]
```

Set `self._checkpoint_enabled` where `self._checkpoint` was previously assigned,
using the same `self._checkpoint_active(...)` gate. Update the two readers
(`:978` and `:2217-2222`) to call `self._checkpoint_for(session_key)`; both already
have the session key in scope.

- [ ] **Step 4: Mount the root that covers every session**

In `raven/sandbox/__init__.py`, add the parameter and merge it into the volumes the
boxlite executor receives:

```python
def build_executor(
    sandbox_cfg: SandboxConfig | None,
    workspace: Path,
    owned_ids: set[str] | None = None,
    extra_volumes: tuple[tuple[str, str, str], ...] = (),
) -> SandboxExecutor:
```

```python
            extra_volumes=[*sandbox_cfg.extra_volumes, *[list(v) for v in extra_volumes]],
```

In `raven/agent/loop/main.py:540`, build the loop-level executor on the mount root,
and keep agent home reachable inside the VM when it is not already covered:

```python
        mount_root = self._workdir_resolver.mount_root() if self._workdir_resolver else workspace
        home_volume = (
            ()
            if workspace.is_relative_to(mount_root)
            else ((str(workspace), "/agent-home", "rw"),)
        )
        self._executor = build_executor(sandbox_config, mount_root, self._owned_ids, home_volume)
```

Correction (caught during implementation): `workspace in mount_root.parents`, as
drafted above, has the containment direction backwards -- it asks whether
`mount_root` is an ancestor of `workspace`, when the condition that should skip
the extra mount is the opposite: whether `workspace` sits inside `mount_root`.
Backwards, it would skip mounting agent home in the most common configuration
(agent home containing the per-session mount root). The implemented condition,
`workspace.is_relative_to(mount_root)`, checks containment in the right
direction; the shipped code applies it through `workdir.is_within(workspace,
mount_root)`, which does the same comparison against resolved paths.

- [ ] **Step 4b: Refuse a sandboxed turn whose workdir is outside the mount**

Added after the Task 1 review found the gap. `mount_root()` covers the policy
default and an explicit `-w`, but `resolve()` has a third source: a persisted
`Session.metadata["workdir"]`, which by design may point anywhere (that is the
whole point of the per-session override). The VM's volumes are fixed when the box
is created, so a session pinned outside the mount cannot be served by adding a
volume later. Refuse loudly instead of running the turn against a path the VM
cannot see.

Write the failing test first, in `tests/test_agent_loop_workdir.py`:

```python
@pytest.mark.asyncio
async def test_sandboxed_turn_refuses_a_workdir_outside_the_mount(tmp_path, monkeypatch):
    """A pinned directory outside the VM mount is a hard error, not a silent miss."""
    outside = tmp_path / "outside"
    outside.mkdir()
    sessions = SessionManager(tmp_path / "home")
    session = sessions.get_or_create("web:abc")
    session.metadata["workdir"] = str(outside)
    sessions.save(session)

    resolver = WorkdirResolver(
        WorkdirPolicy.PER_SESSION,
        agent_home=tmp_path / "home",
        session_root=tmp_path / "home" / "ws",
        sessions=sessions,
    )
    loop = AgentLoop(
        provider=_FakeChatProvider(),
        workspace=tmp_path / "home",
        workdir_resolver=resolver,
    )
    monkeypatch.setattr(type(loop._executor), "is_sandboxed", property(lambda self: True))

    with pytest.raises(ValueError, match="outside the sandbox mount"):
        loop.session_workdir("web:abc")
```

Then guard in `AgentLoop.session_workdir`:

```python
        resolved = self._workdir_resolver.resolve(session_key)
        root = self._workdir_resolver.mount_root()
        if self._executor.is_sandboxed and resolved != root and root not in resolved.parents:
            raise ValueError(
                f"session {session_key} is pinned to {resolved}, which is outside the sandbox "
                f"mount {root}; clear the override or restart with a wider workspace root"
            )
        return resolved
```

Unsandboxed runs (`backend="none"`, the default) are unaffected: `is_sandboxed`
is `False` on `DirectExecutor`, so an override anywhere keeps working.

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_agent_loop_workdir.py -v`
Expected: PASS.

- [ ] **Step 6: Run the sandbox and checkpoint suites**

Run: `uv run pytest tests/test_sandbox_unit.py tests/ -k "checkpoint" -q`
Expected: PASS, no new failures.

- [ ] **Step 7: Commit**

```bash
git add raven/agent/loop/main.py raven/sandbox/__init__.py tests/test_agent_loop_workdir.py
git commit -m "feat(agent): point checkpoints and the sandbox mount at the workdir

One CheckpointService per working directory, since its shadow git lives inside
the tree it protects. One sandbox mount still covers every session, because all
per-session directories share a root.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Task 7: Entrypoint flags and policies

**Files:**
- Modify: `raven/cli/_helpers.py:237-253`
- Modify: `raven/cli/agent_commands.py:170`, `:248`, `:331-334`
- Modify: `raven/cli/tui_commands.py` (option list near `:963`, `AgentLoop` call at `:407-409`)
- Modify: `raven/cli/gateway_commands.py:102`, `:222-225`
- Modify: `raven/cli/sentinel_commands.py` (help text at `:142`, `:306`, `:717`, `:990`, `:1042`, `:1114`)
- Test: `tests/test_cli_agent_commands.py`, `tests/test_cli_tui_commands.py`, `tests/test_cli_gateway_commands.py`

**Interfaces:**
- Consumes: `WorkdirPolicy`, `WorkdirResolver`, `validate_override` (Task 1); `AgentLoop(workdir_resolver=...)` (Task 3).
- Produces: every entrypoint passes `workdir_resolver=` to `AgentLoop`, which `tests/test_cli_agent_loop_parity.py` then requires of all three.

- [ ] **Step 1: Write the failing tests**

In `tests/test_cli_agent_commands.py` (the file already has `runner = CliRunner()`
and `from raven.cli.commands import app`):

```python
def test_home_flag_moves_agent_home(tmp_path):
    from raven.cli._helpers import load_runtime_config

    config = load_runtime_config(None, home=str(tmp_path / "elsewhere"))
    assert config.agents.defaults.workspace == str(tmp_path / "elsewhere")


def test_agent_help_documents_both_directories():
    r = runner.invoke(app, ["agent", "--help"])
    assert r.exit_code == 0
    assert "--home" in r.output
    assert "Working directory" in r.output
```

In `tests/test_cli_tui_commands.py`:

```python
def test_tui_exposes_a_workspace_flag():
    from typer.testing import CliRunner

    from raven.cli import tui_commands

    r = CliRunner(mix_stderr=False).invoke(tui_commands.tui_app, ["--help"])
    assert r.exit_code == 0
    assert "--workspace" in r.output
    assert "--home" in r.output
```

In `tests/test_cli_gateway_commands.py`:

```python
def test_gateway_help_describes_the_session_root():
    r = runner.invoke(app, ["gateway", "--help"])
    assert r.exit_code == 0
    assert "per-session workspaces" in r.output
```

The behavioural assertion for the gateway resolver is the parity test in Step 8,
not a runtime one: `gateway()` cannot be driven under unit test at all, as
`tests/test_cli_agent_loop_parity.py:20-24` explains.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_cli_agent_commands.py tests/test_cli_tui_commands.py tests/test_cli_gateway_commands.py -q`
Expected: FAIL — `load_runtime_config() got an unexpected keyword argument 'home'`, and the TUI has no `--workspace` option.

- [ ] **Step 3: Split the config override**

In `raven/cli/_helpers.py`, replace the `workspace` override with a `home` one:

```python
def load_runtime_config(config: str | None = None, home: str | None = None) -> Config:
    """Load config and optionally override agent home."""
    ...
    loaded = load_config(config_path)
    if home:
        loaded.agents.defaults.workspace = home
    return loaded
```

Update the four call sites `grep -rn "load_runtime_config(" raven/` reports.

- [ ] **Step 4: Wire `raven agent`**

In `raven/cli/agent_commands.py`, keep `--workspace/-w` but change its help text to
`"Working directory for this run (default: current directory)"`, add
`home: str | None = typer.Option(None, "--home", help="Agent home directory (memory, skills, transcripts)")`,
and build the resolver:

```python
        config = load_runtime_config(config, home=home)
        workdir_resolver = WorkdirResolver(
            WorkdirPolicy.LAUNCH_DIR,
            agent_home=config.workspace_path,
            launch_dir=Path.cwd(),
            explicit_workdir=validate_override(workspace, config.workspace_path) if workspace else None,
            sessions=session_manager,
        )
```

and pass `workdir_resolver=workdir_resolver` to `AgentLoop(...)`.

`explicit_workdir` is read-only: nothing writes it to session metadata, and nothing
should start to. A `-w` that persisted would make a later launch from a different
directory silently reuse the old one, which is the opposite of what the launch-
directory default promises.

- [ ] **Step 5: Wire `raven tui` the same way**

Add `--workspace/-w` and `--home` to the TUI command with identical help text and
the identical `WorkdirPolicy.LAUNCH_DIR` resolver, passing `workdir_resolver=` to
its `AgentLoop(...)` call.

- [ ] **Step 6: Wire `raven gateway`**

In `raven/cli/gateway_commands.py`, change `--workspace/-w` help to
`"Root directory for per-session workspaces (default: <agent home>/ws)"`, add
`--home`, and build:

```python
        workdir_resolver = WorkdirResolver(
            WorkdirPolicy.PER_SESSION,
            agent_home=config.workspace_path,
            session_root=Path(workspace).expanduser() if workspace else config.workspace_path / "ws",
            sessions=session_manager,
        )
```

passing `workdir_resolver=workdir_resolver` to `AgentLoop(...)`.

- [ ] **Step 7: Update the sentinel help text**

In `raven/cli/sentinel_commands.py`, change each of the six `--workspace/-w` help
strings to `"Agent home directory"`. Do not change their behaviour: those commands
read attention and behaviour files out of agent home and have no working directory.

- [ ] **Step 8: Run the tests to verify they pass**

Run: `uv run pytest tests/test_cli_agent_commands.py tests/test_cli_tui_commands.py tests/test_cli_gateway_commands.py tests/test_cli_agent_loop_parity.py -v`
Expected: PASS. The parity test proves all three entrypoints pass the new kwarg; if
one was missed it fails with an undeclared asymmetry for `workdir_resolver`.

- [ ] **Step 9: Smoke-test the launch-directory default by hand**

```bash
mkdir -p /tmp/raven-wd-check && cd /tmp/raven-wd-check
uv run --directory /Evermind/sh_evermind/xuedizhan/Raven raven agent --help | grep -A1 -- "--workspace"
```
Expected: help text reads "Working directory for this run".

- [ ] **Step 10: Commit**

```bash
git add raven/cli/_helpers.py raven/cli/agent_commands.py raven/cli/tui_commands.py \
        raven/cli/gateway_commands.py raven/cli/sentinel_commands.py \
        tests/test_cli_agent_commands.py tests/test_cli_tui_commands.py \
        tests/test_cli_gateway_commands.py
git commit -m "feat(cli): default tui and agent to the launch directory

-w now selects the working directory (the session root on gateway); agent home
moves to the new --home. Sentinel keeps its own -w, help text clarified.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Task 8: Per-session override over RPC, HTTP and the web UI

Mirrors the per-session model chain exactly. Read
`raven/web_rpc/methods_config.py:163-215` before starting — the comment there
explains why this must live in the gateway process.

**Files:**
- Modify: `raven/web_rpc/methods_config.py` (next to `_session_model_get` / `_session_model_set`)
- Modify: `ui-webui/service/raven_config_routes.py:181-195`
- Create: `ui-webui/frontend/src/api/ravenSessionWorkdir.ts`
- Modify: `ui-webui/frontend/src/api/index.ts`, `ui-webui/frontend/src/pages/chat/ChatViewport.tsx`
- Test: `tests/test_web_rpc_session_workdir.py`

**Interfaces:**
- Consumes: `validate_override` (Task 1), `AgentLoop.session_workdir` (Task 3).
- Produces: `raven.session.workdir.get` -> `{"workdir": str | None, "default": str}`; `raven.session.workdir.set` -> `{"ok": True, "workdir": str | None}`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_web_rpc_session_workdir.py`:

```python
"""The per-session workdir RPC pair, mirroring the per-session model pair."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.workdir import WorkdirPolicy, WorkdirResolver
from raven.session.manager import SessionManager
from raven.rpc.dispatcher import Dispatcher
from raven.web_rpc.methods_config import register_config_methods


class _FakeChatProvider:
    def get_default_model(self) -> str:
        return "stub-model"


async def _dispatch(d: Dispatcher, method: str, params: dict, rid: int = 1) -> dict:
    """Same helper shape as tests/test_web_rpc_config.py:39."""
    return await d.dispatch({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})


@pytest.fixture
def agent(tmp_path: Path) -> AgentLoop:
    resolver = WorkdirResolver(
        WorkdirPolicy.PER_SESSION,
        agent_home=tmp_path,
        session_root=tmp_path / "ws",
        sessions=SessionManager(tmp_path),
    )
    return AgentLoop(
        provider=_FakeChatProvider(),
        workspace=tmp_path,
        workdir_resolver=resolver,
        session_manager=SessionManager(tmp_path),
    )


@pytest.fixture
def dispatcher(agent: AgentLoop) -> Dispatcher:
    d = Dispatcher()
    register_config_methods(d, agent=agent)
    return d


@pytest.mark.asyncio
async def test_get_reports_none_and_the_default(dispatcher, agent, tmp_path):
    resp = await _dispatch(dispatcher, "raven.session.workdir.get", {"session_key": "web:abc"})
    assert "error" not in resp, resp
    assert resp["result"]["workdir"] is None
    assert resp["result"]["default"] == str(tmp_path / "ws" / "web" / "abc")


@pytest.mark.asyncio
async def test_set_persists_to_session_metadata(dispatcher, agent, tmp_path):
    target = tmp_path / "project"
    target.mkdir()

    resp = await _dispatch(
        dispatcher,
        "raven.session.workdir.set",
        {"session_key": "web:abc", "workdir": str(target)},
    )

    assert "error" not in resp, resp
    assert agent.sessions.get_or_create("web:abc").metadata["workdir"] == str(target)


@pytest.mark.asyncio
async def test_set_rejects_a_relative_path(dispatcher):
    resp = await _dispatch(
        dispatcher,
        "raven.session.workdir.set",
        {"session_key": "web:abc", "workdir": "relative/dir"},
    )

    assert "error" in resp
    assert "absolute" in str(resp["error"])


@pytest.mark.asyncio
async def test_set_rejects_while_work_is_in_flight(dispatcher, agent, tmp_path):
    target = tmp_path / "project"
    target.mkdir()
    agent.subagents._session_tasks["web:abc"] = {"task-1"}

    resp = await _dispatch(
        dispatcher,
        "raven.session.workdir.set",
        {"session_key": "web:abc", "workdir": str(target)},
    )

    assert "error" in resp
    assert "in flight" in str(resp["error"])


@pytest.mark.asyncio
async def test_null_clears_the_override(dispatcher, agent, tmp_path):
    session = agent.sessions.get_or_create("web:abc")
    session.metadata["workdir"] = str(tmp_path)

    resp = await _dispatch(
        dispatcher, "raven.session.workdir.set", {"session_key": "web:abc", "workdir": None}
    )

    assert "error" not in resp, resp
    assert "workdir" not in agent.sessions.get_or_create("web:abc").metadata
```

Errors come back as a JSON-RPC error object, not a raised exception: the
dispatcher catches handler exceptions and returns `{"error": ...}`
(`raven/rpc/dispatcher.py:104-127`). Assert on the response, never with
`pytest.raises`.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_web_rpc_session_workdir.py -v`
Expected: FAIL — the methods are not registered.

- [ ] **Step 3: Register the RPC pair**

In `raven/web_rpc/methods_config.py`, directly after `_session_model_set`, add:

```python
    async def _session_workdir_get(params: dict) -> dict:
        key = params.get("session_key", "")
        session = _sessions().get_or_create(key)
        return {
            "workdir": session.metadata.get("workdir"),
            "default": str(agent.session_workdir(key)),
        }

    async def _session_workdir_set(params: dict) -> dict:
        key = params.get("session_key", "")
        raw = params.get("workdir")
        if raw is not None:
            if not isinstance(raw, str) or not raw.strip():
                raise ValueError("workdir must be a non-empty string, or null to clear")
            # Rebinding under a running turn would strand its sub-agents between
            # two directories, so refuse rather than queue the change.
            if agent.subagents.has_active(key):
                raise RuntimeError(f"session {key} has work in flight; retry when it is idle")
            raw = str(validate_override(raw.strip(), agent.workspace))
        session = _sessions().get_or_create(key)
        if raw is None:
            session.metadata.pop("workdir", None)
        else:
            session.metadata["workdir"] = raw
        _sessions().save(session)
        return {"ok": True, "workdir": raw}

    dispatcher.register("raven.session.workdir.get", _session_workdir_get)
    dispatcher.register("raven.session.workdir.set", _session_workdir_set)
```

Add `has_active(session_key) -> bool` to `SubagentManager`, returning whether
`self._session_tasks.get(session_key)` is non-empty.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_web_rpc_session_workdir.py -v`
Expected: PASS, 5 tests.

- [ ] **Step 5: Add the HTTP routes**

In `ui-webui/service/raven_config_routes.py`, directly after the model routes:

```python
    @router.get("/sessions/{session_key}/workdir")
    async def get_session_workdir(session_key: str) -> dict:
        client = await GatewayClient.shared()
        return await client.call("raven.session.workdir.get", {"session_key": session_key})

    @router.put("/sessions/{session_key}/workdir")
    async def set_session_workdir(session_key: str, body: dict = Body(...)) -> dict:
        client = await GatewayClient.shared()
        try:
            return await client.call(
                "raven.session.workdir.set",
                {"session_key": session_key, "workdir": body.get("workdir")},
            )
        except Exception as exc:  # invalid path / busy session / transport
            raise HTTPException(status_code=400, detail=str(exc)) from exc
```

- [ ] **Step 6: Add the frontend client**

Create `ui-webui/frontend/src/api/ravenSessionWorkdir.ts`:

```typescript
import { client } from './client';

export const ravenSessionWorkdirApi = {
	/**
	 * `workdir` is the session's own override; `default` is where the session
	 * works while that override is unset.
	 */
	get: (sessionKey: string) =>
		client.get<{ workdir: string | null; default: string }>(
			`/raven/sessions/${encodeURIComponent(sessionKey)}/workdir`,
		),
	set: (sessionKey: string, workdir: string | null) =>
		client.put<{ ok: boolean; workdir: string | null }>(
			`/raven/sessions/${encodeURIComponent(sessionKey)}/workdir`,
			{ workdir },
		),
};
```

Export it from `ui-webui/frontend/src/api/index.ts` alongside `ravenSessionModelApi`,
and add the control to `ChatViewport.tsx` next to the model selector wiring at
`:751` and `:798`, calling `ravenSessionWorkdirApi.set(\`web:${sessionId}\`, value)`
and surfacing the 400 detail as an inline error.

- [ ] **Step 7: Type-check and build the frontend**

Run: `cd ui-webui/frontend && pnpm tsc --noEmit`
Expected: no errors.

- [ ] **Step 8: Commit**

```bash
git add raven/web_rpc/methods_config.py raven/agent/subagent/manager.py \
        ui-webui/service/raven_config_routes.py ui-webui/frontend/src/api \
        ui-webui/frontend/src/pages/chat/ChatViewport.tsx \
        tests/test_web_rpc_session_workdir.py
git commit -m "feat(web_rpc): add a per-session working directory override

Mirrors the per-session model chain. Rejected while the session has work in
flight, so a running sub-agent cannot straddle two directories.

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Task 9: Terminology and changelog

AGENTS.md section 6 requires a new domain term to be defined in `CONTEXT.md` in the
same change that coins it.

**Files:**
- Modify: `CONTEXT.md:454` and the surrounding glossary
- Modify: the changelog file that `ls CHANGELOG*` finds, if one exists

- [ ] **Step 1: Rewrite the Workspace entry**

Read `CONTEXT.md:450-470` first. Replace the single "Workspace" entry with three:

- **Agent home** — the per-agent filesystem tree (default `~/.raven/workspace`)
  holding user memory, skills, session transcripts, the Skill Hub cache and the
  memory store. Seeded by `sync_workspace_templates()`. One per agent, never per
  session. Set by `--home` or `agents.defaults.workspace`.
- **Session workspace** — the directory a session's turns read and write files in.
  Shared by the session's leader and every sub-agent it spawns. On `raven tui` and
  `raven agent` it defaults to the launch directory; on `raven gateway` it defaults
  to `<agent home>/ws/<channel>/<chat_id>/`. Overridable per session and persisted
  in `Session.metadata["workdir"]`.
- **Workdir policy** — which default an entrypoint uses: `LAUNCH_DIR` or
  `PER_SESSION` (`raven/agent/workdir.py`).

Add `_Avoid_: "workspace" unqualified` to the Agent home entry, since the old
single meaning is what the two new terms replace.

- [ ] **Step 2: Verify the cross-references still resolve**

Run: `grep -rn "Workspace" CONTEXT-MAP.md CONTEXT.md | head -20`
Expected: no dangling pointer to a heading that no longer exists; fix any that
point at the removed entry.

- [ ] **Step 3: Record the behaviour changes**

Run: `ls CHANGELOG* 2>/dev/null`
If a changelog exists, add entries for: the `-w` semantic change and the new
`--home`; `tui` and `agent` now working in the launch directory; gateway sessions
now working in per-session directories with existing artifacts left in place; and
shadow-git checkpoints now following the working directory, which means they
snapshot a real project when the agent is launched inside one. If no changelog
exists, skip this step and carry the same points into the PR description.

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add CONTEXT.md
git commit -m "docs(*): split workspace into agent home and session workspace

Co-authored-by: Claude (claude-opus-5) <noreply@anthropic.com>"
```

---

## Verification checklist

Run before proposing the PR:

- [ ] `uv run pytest -q` passes
- [ ] `uv run pytest tests/test_cli_agent_loop_parity.py -v` passes with no new `LEDGER` entry, proving all three entrypoints pass `workdir_resolver`
- [ ] `grep -rn "allowed_dir=" raven/ tests/ --include=*.py` returns nothing
- [ ] `cd /tmp && uv run --directory <repo> raven agent -w /tmp/scratch --help` shows the new help text
- [ ] Gateway: start it, send a web message, confirm the file lands in `~/.raven/workspace/ws/web/<chat_id>/` and not at the agent-home root
- [ ] Gateway: set an override through the chat UI, restart the gateway, confirm the session still works in the overridden directory
- [ ] `make check-large-files` passes (AGENTS.md section 7)
