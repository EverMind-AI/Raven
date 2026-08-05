# CLI Sub-Agent Dispatch Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a user register CLI "sub-agents" (e.g. `claude -p {prompt} --dangerously-skip-permissions`) that the unified agent can call as ordinary tools, managed from a new right-sidebar panel.

**Architecture:** A new `agentscope.subagent` package holds the config models (`CliSubAgentConfig`), a `SubAgentFactory` (mirrors `CredentialFactory`), a `CliSubAgentTool` that shells out (mirrors the built-in `Bash` tool), and a `make_subagent_tool_factory` closure that turns per-user stored configs into tools via the existing `extra_agent_tools` hook. Persistence mirrors credentials (Redis CRUD + `/subagent/` REST). The web UI gets a `SubagentPanel` dock panel mirroring the MCP panel. No changes to `Toolkit`, `get_toolkit`, or `ChatService`.

**Tech Stack:** Python 3.11+, Pydantic v2, FastAPI, redis/fakeredis, `unittest.IsolatedAsyncioTestCase`, pytest; React 18 + TypeScript + Vite (pnpm).

**Spec:** `docs/superpowers/specs/2026-07-15-cli-subagent-dispatch-design.md`

## Global Constraints

- Python formatting: **black, line length 79**; flake8 + pylint + mypy clean (pre-commit).
- **Encapsulation:** internal files/classes are `_`-prefixed; public surface is re-exported via `__init__.py`.
- **Lazy imports:** third-party libs (not in `[project.dependencies]`) imported at point of use. `agentscope` internal modules may be imported at file top.
- **Docstrings:** English only, `Args:`/`Returns:` template with backtick-typed params.
- **Tests:** live in `tests/*_test.py`; assert **whole data structures**; use `AnyString`/`AnyValue` from `tests/utils.py` for random ids/timestamps.
- **Commits:** Conventional Commits — `feat/fix/test/refactor/docs(subagent): …`.
- Work on branch `feat/cli-subagent-dispatch` (already created; the design spec is committed there).
- Sub-Agent config decisions (locked): **user-global** scope, **generic command template** with a `{prompt}` placeholder, **auto-run** (tool permission = ALLOW).

---

### Task 1: Sub-Agent config models

**Files:**
- Create: `src/agentscope/subagent/_base.py`
- Test: `tests/subagent_config_test.py`

**Interfaces:**
- Produces:
  - `SubAgentConfigBase(BaseModel)` — field `id: str` (default via `_generate_id`).
  - `CliSubAgentConfig(SubAgentConfigBase)` — `type: Literal["cli_subagent"]`, `name: str` (pattern `^[A-Za-z0-9_-]+$`), `description: str`, `command: str` (must contain `{prompt}`), `cwd: str | None`, `env: dict[str, str] | None`, `timeout: int` (default 600). Validator `_require_prompt_placeholder`.

- [ ] **Step 1: Write the failing test**

```python
# tests/subagent_config_test.py
# -*- coding: utf-8 -*-
"""Tests for the sub-agent config models."""
import unittest

from pydantic import ValidationError

from agentscope.subagent import CliSubAgentConfig


class CliSubAgentConfigTest(unittest.TestCase):
    """Validate the CLI sub-agent config model."""

    def test_defaults_and_dump(self) -> None:
        """A minimal config fills defaults and round-trips through dump."""
        config = CliSubAgentConfig(
            id="sa-1",
            name="claude_code",
            description="Delegate a coding task to Claude Code.",
            command="claude -p {prompt} --dangerously-skip-permissions",
        )
        self.assertEqual(
            config.model_dump(),
            {
                "id": "sa-1",
                "type": "cli_subagent",
                "name": "claude_code",
                "description": "Delegate a coding task to Claude Code.",
                "command": (
                    "claude -p {prompt} --dangerously-skip-permissions"
                ),
                "cwd": None,
                "env": None,
                "timeout": 600,
            },
        )

    def test_command_must_contain_prompt_placeholder(self) -> None:
        """A command without ``{prompt}`` is rejected."""
        with self.assertRaises(ValidationError):
            CliSubAgentConfig(
                name="bad",
                description="no placeholder",
                command="claude -p --dangerously-skip-permissions",
            )

    def test_name_pattern_rejects_spaces(self) -> None:
        """A name with spaces/invalid chars is rejected."""
        with self.assertRaises(ValidationError):
            CliSubAgentConfig(
                name="claude code",
                description="bad name",
                command="claude -p {prompt}",
            )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/subagent_config_test.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'agentscope.subagent'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/agentscope/subagent/_base.py
# -*- coding: utf-8 -*-
"""Sub-Agent config base classes."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .._utils._common import _generate_id


class SubAgentConfigBase(BaseModel):
    """The base class for all sub-agent configs.

    A sub-agent config is user-authored data describing an external
    agent the unified agent may delegate to. Subclasses set a unique
    ``type`` discriminator so :class:`SubAgentFactory` can round-trip
    them through storage.
    """

    id: str = Field(
        default_factory=_generate_id,
        description="The sub-agent id.",
    )


class CliSubAgentConfig(SubAgentConfigBase):
    """A sub-agent invoked by running a CLI command.

    The ``command`` is a template whose ``{prompt}`` placeholder is
    replaced with the task the unified agent wants to delegate, e.g.
    ``claude -p {prompt} --dangerously-skip-permissions``.
    """

    model_config = ConfigDict(title="CLI Sub-Agent")

    type: Literal["cli_subagent"] = "cli_subagent"
    """The sub-agent type discriminator."""

    name: str = Field(
        pattern=r"^[A-Za-z0-9_-]+$",
        description=(
            "Tool name shown to the agent. Letters, digits, '_' and '-'."
        ),
    )
    """The tool name presented to the agent."""

    description: str = Field(
        description=(
            "Agent-readable description used to decide when to delegate "
            "to this sub-agent."
        ),
    )
    """The tool description presented to the agent."""

    command: str = Field(
        description=(
            "Shell command template. The '{prompt}' placeholder is "
            "replaced with the delegated task."
        ),
    )
    """The command template containing ``{prompt}``."""

    cwd: str | None = Field(
        default=None,
        description="Optional working directory for the sub-agent.",
    )
    """The working directory."""

    env: dict[str, str] | None = Field(
        default=None,
        description="Optional extra environment variables.",
    )
    """Extra environment variables."""

    timeout: int = Field(
        default=600,
        gt=0,
        description="Maximum seconds to wait for the sub-agent.",
    )
    """The execution timeout in seconds."""

    @field_validator("command")
    @classmethod
    def _require_prompt_placeholder(cls, value: str) -> str:
        """Ensure the command template contains ``{prompt}``.

        Args:
            value (`str`):
                The command template to validate.

        Returns:
            `str`:
                The validated command template.
        """
        if "{prompt}" not in value:
            raise ValueError(
                "command template must contain the '{prompt}' placeholder",
            )
        return value
```

Also create the package init so the import in the test resolves:

```python
# src/agentscope/subagent/__init__.py
# -*- coding: utf-8 -*-
"""The sub-agent module."""
from ._base import SubAgentConfigBase, CliSubAgentConfig

__all__ = [
    "SubAgentConfigBase",
    "CliSubAgentConfig",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/subagent_config_test.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_base.py src/agentscope/subagent/__init__.py tests/subagent_config_test.py
git commit -m "feat(subagent): add CLI sub-agent config models"
```

---

### Task 2: Sub-Agent factory

**Files:**
- Create: `src/agentscope/subagent/_factory.py`
- Modify: `src/agentscope/subagent/__init__.py`
- Test: `tests/subagent_factory_test.py`

**Interfaces:**
- Consumes: `SubAgentConfigBase`, `CliSubAgentConfig` (Task 1).
- Produces: `SubAgentFactory` with classmethods `from_dict(data: dict) -> SubAgentConfigBase`, `register(subagent_cls) -> None`, `get_subagent_class(provider: str) -> Type[SubAgentConfigBase] | None`, `list_schemas() -> list[dict]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/subagent_factory_test.py
# -*- coding: utf-8 -*-
"""Tests for the sub-agent factory."""
import unittest

from agentscope.subagent import (
    CliSubAgentConfig,
    SubAgentFactory,
)


class SubAgentFactoryTest(unittest.TestCase):
    """Validate factory deserialization and schema listing."""

    def test_from_dict_round_trip(self) -> None:
        """``from_dict`` reconstructs the typed config."""
        config = SubAgentFactory.from_dict(
            {
                "type": "cli_subagent",
                "name": "claude_code",
                "description": "Delegate coding.",
                "command": "claude -p {prompt}",
            },
        )
        self.assertIsInstance(config, CliSubAgentConfig)
        self.assertEqual(config.name, "claude_code")

    def test_from_dict_unknown_type(self) -> None:
        """An unknown ``type`` raises ``ValueError``."""
        with self.assertRaises(ValueError):
            SubAgentFactory.from_dict({"type": "nope", "name": "x"})

    def test_list_schemas_contains_cli_type(self) -> None:
        """``list_schemas`` returns a JSON schema per registered type."""
        schemas = SubAgentFactory.list_schemas()
        titles = [s.get("title") for s in schemas]
        self.assertIn("CLI Sub-Agent", titles)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/subagent_factory_test.py -v`
Expected: FAIL — `ImportError: cannot import name 'SubAgentFactory'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/agentscope/subagent/_factory.py
# -*- coding: utf-8 -*-
"""The sub-agent factory class."""
from typing import Type, get_args, get_type_hints

from ._base import SubAgentConfigBase, CliSubAgentConfig


class SubAgentFactory:
    """Registry and deserializer for :class:`SubAgentConfigBase` types.

    Mirrors :class:`~agentscope.credential.CredentialFactory`: built-in
    types are pre-registered; call :meth:`register` to add custom types
    before starting the app.
    """

    _classes: list[Type[SubAgentConfigBase]] = [
        CliSubAgentConfig,
    ]

    @classmethod
    def register(cls, subagent_cls: Type[SubAgentConfigBase]) -> None:
        """Register a custom :class:`SubAgentConfigBase` subclass.

        Args:
            subagent_cls (`Type[SubAgentConfigBase]`):
                The subclass to register. Must define a ``type`` field
                with a unique ``Literal`` default.
        """
        if subagent_cls not in cls._classes:
            cls._classes.append(subagent_cls)

    @classmethod
    def get_subagent_class(
        cls,
        provider: str,
    ) -> Type[SubAgentConfigBase] | None:
        """Return the config class for the given ``type``, or ``None``.

        Args:
            provider (`str`):
                The ``type`` discriminator value (e.g. ``"cli_subagent"``).

        Returns:
            `Type[SubAgentConfigBase] | None`:
                The matching subclass, or ``None`` if not found.
        """
        for candidate in cls._classes:
            hints = get_type_hints(candidate)
            type_hint = hints.get("type")
            if type_hint is None:
                continue
            args = get_args(type_hint)
            if args and args[0] == provider:
                return candidate
        return None

    @classmethod
    def from_dict(cls, data: dict) -> SubAgentConfigBase:
        """Deserialize a config dict (from storage) to a typed instance.

        Args:
            data (`dict`):
                Raw dict containing a ``"type"`` key.

        Returns:
            `SubAgentConfigBase`:
                A typed subclass instance.
        """
        target = cls.get_subagent_class(data.get("type"))
        if target is None:
            raise ValueError(
                f"Unknown sub-agent type: {data.get('type')!r}",
            )
        return target.model_validate(data)

    @classmethod
    def list_schemas(cls) -> list[dict]:
        """Return JSON schemas for all registered sub-agent types.

        Returns:
            `list[dict]`:
                One ``model_json_schema()`` per registered type.
        """
        return [candidate.model_json_schema() for candidate in cls._classes]
```

Update the package init:

```python
# src/agentscope/subagent/__init__.py
# -*- coding: utf-8 -*-
"""The sub-agent module."""
from ._base import SubAgentConfigBase, CliSubAgentConfig
from ._factory import SubAgentFactory

__all__ = [
    "SubAgentConfigBase",
    "CliSubAgentConfig",
    "SubAgentFactory",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/subagent_factory_test.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_factory.py src/agentscope/subagent/__init__.py tests/subagent_factory_test.py
git commit -m "feat(subagent): add SubAgentFactory"
```

---

### Task 3: The `CliSubAgentTool`

**Files:**
- Create: `src/agentscope/subagent/_tool.py`
- Modify: `src/agentscope/subagent/__init__.py`
- Test: `tests/subagent_tool_test.py`

**Interfaces:**
- Consumes: `ToolBase`, `ToolChunk`, `BackendBase`, `LocalBackend`, `ExecResult` from `agentscope.tool`; `TextBlock`, `ToolResultState` from `agentscope.message`; `PermissionBehavior`, `PermissionContext`, `PermissionDecision` from `agentscope.permission`.
- Produces: `CliSubAgentTool(ToolBase)`.
  - `__init__(self, name: str, description: str, command: str, cwd: str | None = None, env: dict[str, str] | None = None, timeout: int = 600, backend: BackendBase | None = None, middlewares=None)`.
  - `input_schema` = one required `prompt: string`.
  - `call(self, prompt: str) -> AsyncGenerator[ToolChunk, None]`.
  - class attrs `is_read_only = False`, `is_concurrency_safe = False`.

- [ ] **Step 1: Write the failing test**

```python
# tests/subagent_tool_test.py
# -*- coding: utf-8 -*-
"""Tests for the CLI sub-agent tool."""
from unittest.async_case import IsolatedAsyncioTestCase

from agentscope.message import ToolResultState
from agentscope.permission import PermissionBehavior, PermissionContext
from agentscope.subagent import CliSubAgentTool
from agentscope.tool import BackendBase, ExecResult


class _FakeBackend(BackendBase):
    """A backend that records the argv it was asked to run."""

    def __init__(self, result: ExecResult) -> None:
        self._result = result
        self.calls: list[dict] = []

    async def exec_shell(
        self,
        command: list[str],
        *,
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        """Record the call and return the canned result."""
        self.calls.append(
            {"command": command, "cwd": cwd, "timeout": timeout},
        )
        return self._result

    async def read_file(self, path: str) -> bytes:
        """Unused."""
        raise NotImplementedError

    async def write_file(self, path: str, data: bytes) -> None:
        """Unused."""
        raise NotImplementedError


def _make_tool(command: str, backend: _FakeBackend, **kw) -> CliSubAgentTool:
    """Build a tool with sensible defaults for tests."""
    return CliSubAgentTool(
        name="claude_code",
        description="Delegate a coding task.",
        command=command,
        backend=backend,
        **kw,
    )


class CliSubAgentToolTest(IsolatedAsyncioTestCase):
    """Validate argv substitution, env, and result mapping."""

    async def test_standalone_prompt_token_becomes_single_argv(self) -> None:
        """A standalone ``{prompt}`` token is replaced as one argv item."""
        backend = _FakeBackend(ExecResult(0, b"pong\n", b""))
        tool = _make_tool(
            "claude -p {prompt} --dangerously-skip-permissions",
            backend,
        )
        chunks = [c async for c in tool.call(prompt="say hi now")]
        self.assertEqual(
            backend.calls[0]["command"],
            ["claude", "-p", "say hi now",
             "--dangerously-skip-permissions"],
        )
        self.assertEqual(chunks[-1].state, ToolResultState.RUNNING)
        self.assertEqual(chunks[-1].content[0].text, "pong\n")

    async def test_embedded_prompt_token(self) -> None:
        """An embedded ``--task={prompt}`` stays a single argv item."""
        backend = _FakeBackend(ExecResult(0, b"ok", b""))
        tool = _make_tool("agent run --task={prompt}", backend)
        _ = [c async for c in tool.call(prompt="build X")]
        self.assertEqual(
            backend.calls[0]["command"],
            ["agent", "run", "--task=build X"],
        )

    async def test_env_prefix_is_prepended(self) -> None:
        """``env`` is applied via an ``env KEY=VAL`` argv prefix."""
        backend = _FakeBackend(ExecResult(0, b"ok", b""))
        tool = _make_tool(
            "claude -p {prompt}",
            backend,
            env={"ANTHROPIC_API_KEY": "sk-x"},
        )
        _ = [c async for c in tool.call(prompt="hi")]
        self.assertEqual(
            backend.calls[0]["command"],
            ["env", "ANTHROPIC_API_KEY=sk-x", "claude", "-p", "hi"],
        )

    async def test_nonzero_exit_is_error_chunk(self) -> None:
        """A non-zero exit yields an ERROR chunk carrying stderr."""
        backend = _FakeBackend(ExecResult(2, b"", b"boom"))
        tool = _make_tool("claude -p {prompt}", backend)
        chunks = [c async for c in tool.call(prompt="hi")]
        self.assertEqual(chunks[-1].state, ToolResultState.ERROR)
        self.assertIn("boom", chunks[-1].content[0].text)

    async def test_timeout_is_error_chunk(self) -> None:
        """A backend timeout (exit -1, 'timed out') yields ERROR."""
        backend = _FakeBackend(ExecResult(-1, b"", b"timed out"))
        tool = _make_tool("claude -p {prompt}", backend)
        chunks = [c async for c in tool.call(prompt="hi")]
        self.assertEqual(chunks[-1].state, ToolResultState.ERROR)
        self.assertIn("timed out", chunks[-1].content[0].text)

    async def test_check_permissions_allows(self) -> None:
        """Auto-run: permissions always ALLOW."""
        backend = _FakeBackend(ExecResult(0, b"", b""))
        tool = _make_tool("claude -p {prompt}", backend)
        decision = await tool.check_permissions(
            {"prompt": "hi"},
            PermissionContext(),
        )
        self.assertEqual(decision.behavior, PermissionBehavior.ALLOW)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/subagent_tool_test.py -v`
Expected: FAIL — `ImportError: cannot import name 'CliSubAgentTool'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/agentscope/subagent/_tool.py
# -*- coding: utf-8 -*-
"""The CLI sub-agent tool."""
import shlex
from typing import Any, AsyncGenerator, List

from ..message import TextBlock, ToolResultState
from ..permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from ..tool import BackendBase, LocalBackend, ToolBase, ToolChunk
from ..tool import ToolMiddlewareBase

_MAX_OUTPUT_CHARS = 30000


class CliSubAgentTool(ToolBase):
    """A tool that delegates a task to an external CLI sub-agent.

    Runs a fixed command template (with a ``{prompt}`` placeholder) via
    the execution backend and returns the sub-agent's stdout. Modeled on
    the built-in :class:`~agentscope.tool.Bash` tool, but the argv is
    fixed by config and the delegated prompt is passed as a single argv
    token (no shell), so the prompt cannot inject shell metacharacters.
    """

    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def __init__(
        self,
        name: str,
        description: str,
        command: str,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        timeout: int = 600,
        backend: BackendBase | None = None,
        middlewares: List[ToolMiddlewareBase] | None = None,
    ) -> None:
        """Initialize the CLI sub-agent tool.

        Args:
            name (`str`):
                The tool name presented to the agent.
            description (`str`):
                The tool description presented to the agent.
            command (`str`):
                The command template containing ``{prompt}``.
            cwd (`str | None`, optional):
                Working directory for the sub-agent process.
            env (`dict[str, str] | None`, optional):
                Extra environment variables applied via an ``env`` prefix.
            timeout (`int`, defaults to `600`):
                Maximum seconds to wait for the sub-agent.
            backend (`BackendBase | None`, optional):
                The execution backend. Defaults to :class:`LocalBackend`.
            middlewares (`List[ToolMiddlewareBase] | None`, optional):
                Tool middlewares wrapping execution.
        """
        super().__init__(middlewares=middlewares)
        self.name = name
        self.description = description
        self.input_schema = {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": (
                        "The task to delegate to the sub-agent."
                    ),
                },
            },
            "required": ["prompt"],
        }
        self._command = command
        self._cwd = cwd
        self._env = env
        self._timeout = timeout
        self._backend = backend or LocalBackend()

    def _build_argv(self, prompt: str) -> list[str]:
        """Build the argv for a delegated ``prompt``.

        Substitutes ``{prompt}`` inside each token (so both a standalone
        ``{prompt}`` and an embedded ``--task={prompt}`` work), then
        prepends an ``env KEY=VAL`` prefix when env vars are configured.

        Args:
            prompt (`str`):
                The task to delegate.

        Returns:
            `list[str]`:
                The argv to run without a shell.
        """
        argv = [tok.replace("{prompt}", prompt) for tok in
                shlex.split(self._command)]
        if self._env:
            argv = [
                "env",
                *(f"{key}={value}" for key, value in self._env.items()),
                *argv,
            ]
        return argv

    async def call(  # type: ignore[override]
        self,
        prompt: str,
    ) -> AsyncGenerator[ToolChunk, None]:
        """Run the sub-agent and yield its result.

        Args:
            prompt (`str`):
                The task to delegate to the sub-agent.

        Yields:
            `ToolChunk`:
                A single terminal chunk with the sub-agent's output.
        """
        argv = self._build_argv(prompt)
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

        stdout = result.stdout.decode("utf-8", errors="replace").replace(
            "\r\n",
            "\n",
        )
        stderr = result.stderr.decode("utf-8", errors="replace").replace(
            "\r\n",
            "\n",
        )

        if result.exit_code == -1 and result.stderr == b"timed out":
            yield ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            f"Sub-Agent timed out after "
                            f"{self._timeout}s."
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        output = stdout
        if stderr:
            output = f"{output}\n{stderr}" if output else stderr
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + "\n... (output truncated)"

        if not result.ok():
            yield ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            f"Sub-Agent exited with code "
                            f"{result.exit_code}.\n{output}"
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        yield ToolChunk(
            content=[TextBlock(text=output)],
            state=ToolResultState.RUNNING,
            is_last=True,
        )

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """Auto-run: always allow sub-agent calls.

        Args:
            tool_input (`dict[str, Any]`):
                The tool input for this invocation.
            context (`PermissionContext`):
                The permission context.

        Returns:
            `PermissionDecision`:
                An ALLOW decision.
        """
        return PermissionDecision(
            behavior=PermissionBehavior.ALLOW,
            message="Sub-Agent calls run automatically.",
        )
```

Update the package init:

```python
# src/agentscope/subagent/__init__.py
# -*- coding: utf-8 -*-
"""The sub-agent module."""
from ._base import SubAgentConfigBase, CliSubAgentConfig
from ._factory import SubAgentFactory
from ._tool import CliSubAgentTool

__all__ = [
    "SubAgentConfigBase",
    "CliSubAgentConfig",
    "SubAgentFactory",
    "CliSubAgentTool",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/subagent_tool_test.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_tool.py src/agentscope/subagent/__init__.py tests/subagent_tool_test.py
git commit -m "feat(subagent): add CliSubAgentTool"
```

---

### Task 4: Storage record + StorageBase abstract methods + RedisStorage impl

**Files:**
- Create: `src/agentscope/app/storage/_model/_subagent.py`
- Modify: `src/agentscope/app/storage/_model/__init__.py`
- Modify: `src/agentscope/app/storage/_base.py`
- Modify: `src/agentscope/app/storage/_redis_storage.py`
- Test: `tests/subagent_storage_test.py`

**Interfaces:**
- Consumes: `SubAgentConfigBase` (Task 1); `_RecordBase`, `_generate_id`.
- Produces:
  - `SubAgentRecord(_RecordBase)` — `user_id: str`, `data: dict`.
  - `StorageBase.upsert_subagent(user_id, config: SubAgentConfigBase) -> str`, `list_subagents(user_id) -> list[SubAgentRecord]`, `get_subagent(user_id, subagent_id) -> SubAgentRecord | None`, `delete_subagent(user_id, subagent_id) -> bool` (abstract + RedisStorage impl).
  - `RedisStorage.KeyConfig.subagent`, `.subagent_index`.

- [ ] **Step 1: Write the failing test**

```python
# tests/subagent_storage_test.py
# -*- coding: utf-8 -*-
# pylint: disable=protected-access
"""RedisStorage tests for sub-agent CRUD (fakeredis)."""
from unittest.async_case import IsolatedAsyncioTestCase

import fakeredis.aioredis

from agentscope.app.storage import RedisStorage
from agentscope.subagent import CliSubAgentConfig


def make_storage() -> RedisStorage:
    """Create a RedisStorage backed by fakeredis."""
    storage = RedisStorage.__new__(RedisStorage)
    storage._client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    storage.key_ttl = None
    storage.key_config = RedisStorage.KeyConfig()
    return storage


def make_config(subagent_id: str = "sa-1") -> CliSubAgentConfig:
    """Create a test CLI sub-agent config."""
    return CliSubAgentConfig(
        id=subagent_id,
        name="claude_code",
        description="Delegate coding.",
        command="claude -p {prompt}",
    )


class SubAgentStorageTest(IsolatedAsyncioTestCase):
    """Validate sub-agent CRUD round-trips."""

    async def test_crud_round_trip(self) -> None:
        """upsert → list → get → delete behaves as expected."""
        storage = make_storage()
        returned_id = await storage.upsert_subagent("u1", make_config())
        self.assertEqual(returned_id, "sa-1")

        listed = await storage.list_subagents("u1")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].id, "sa-1")
        self.assertEqual(listed[0].data["name"], "claude_code")

        got = await storage.get_subagent("u1", "sa-1")
        self.assertIsNotNone(got)
        self.assertEqual(got.data["command"], "claude -p {prompt}")

        self.assertTrue(await storage.delete_subagent("u1", "sa-1"))
        self.assertEqual(await storage.list_subagents("u1"), [])
        self.assertFalse(await storage.delete_subagent("u1", "sa-1"))

    async def test_upsert_updates_existing(self) -> None:
        """A second upsert with the same id updates data in place."""
        storage = make_storage()
        await storage.upsert_subagent("u1", make_config())
        updated = make_config()
        updated.description = "new description"
        await storage.upsert_subagent("u1", updated)
        listed = await storage.list_subagents("u1")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].data["description"], "new description")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/subagent_storage_test.py -v`
Expected: FAIL — `AttributeError: 'RedisStorage' object has no attribute 'upsert_subagent'` (or an abstract-method error).

- [ ] **Step 3a: Add the record model**

```python
# src/agentscope/app/storage/_model/_subagent.py
# -*- coding: utf-8 -*-
"""The sub-agent record."""
from pydantic import Field

from ...._utils._common import _generate_id
from ._base import _RecordBase


class SubAgentRecord(_RecordBase):
    """The record used for storing sub-agent configs."""

    user_id: str = Field(
        default_factory=_generate_id,
    )

    data: dict
    """The sub-agent config data."""
```

Register it in `src/agentscope/app/storage/_model/__init__.py` — add the import next to `_credential` and add `"SubAgentRecord"` to `__all__`:

```python
from ._subagent import SubAgentRecord
```

- [ ] **Step 3b: Add abstract methods to `StorageBase`**

In `src/agentscope/app/storage/_base.py`, add `SubAgentRecord` to the `._model` import block and `SubAgentConfigBase` to the imports (next to `from ...credential import CredentialBase`):

```python
from ...subagent import SubAgentConfigBase
```

Then add these four abstract methods (place them immediately after `delete_credential`, before `upsert_agent`):

```python
    @abstractmethod
    async def upsert_subagent(
        self,
        user_id: str,
        config: SubAgentConfigBase,
    ) -> str:
        """Create or update a sub-agent config for the given user.

        Args:
            user_id (`str`):
                The owner user id.
            config (`SubAgentConfigBase`):
                The sub-agent config; its ``id`` is the primary key.

        Returns:
            `str`:
                The id of the created or updated record.
        """

    @abstractmethod
    async def list_subagents(self, user_id: str) -> list[SubAgentRecord]:
        """List all sub-agent configs for a given user.

        Args:
            user_id (`str`):
                The user id.

        Returns:
            `list[SubAgentRecord]`:
                All sub-agent records for the user.
        """

    @abstractmethod
    async def get_subagent(
        self,
        user_id: str,
        subagent_id: str,
    ) -> SubAgentRecord | None:
        """Fetch a single sub-agent record by id.

        Args:
            user_id (`str`):
                The owner user id.
            subagent_id (`str`):
                The sub-agent id.

        Returns:
            `SubAgentRecord | None`:
                The record, or ``None`` if not found.
        """

    @abstractmethod
    async def delete_subagent(
        self,
        user_id: str,
        subagent_id: str,
    ) -> bool:
        """Delete a sub-agent record.

        Args:
            user_id (`str`):
                The owner user id.
            subagent_id (`str`):
                The sub-agent id.

        Returns:
            `bool`:
                ``True`` if deleted, ``False`` if not found.
        """
```

- [ ] **Step 3c: Implement them on `RedisStorage`**

In `src/agentscope/app/storage/_redis_storage.py`: add `SubAgentRecord` to the `._model` import block; add `from ...subagent import SubAgentConfigBase`; add two key templates in `KeyConfig` (next to the credential keys):

```python
        subagent: str = (
            "agentscope:user:{user_id}:subagent:{subagent_id}"
        )
        subagent_index: str = "agentscope:user:{user_id}:subagents"
```

Add the four methods (place them right after `delete_credential`, before `upsert_agent`):

```python
    async def upsert_subagent(
        self,
        user_id: str,
        config: SubAgentConfigBase,
    ) -> str:
        """Create or update a sub-agent config record for the user.

        Args:
            user_id (`str`):
                The owner user id.
            config (`SubAgentConfigBase`):
                The sub-agent config; its ``id`` is the primary key.

        Returns:
            `str`:
                The id of the created or updated record.
        """
        data_dump = config.model_dump(mode="json")
        key = self._key(
            self.key_config.subagent,
            user_id=user_id,
            subagent_id=config.id,
        )
        raw = await self._client.get(key)
        if raw:
            record = SubAgentRecord.model_validate_json(raw)
            record.data = data_dump
            record.updated_at = datetime.now()
        else:
            record = SubAgentRecord(
                id=config.id,
                user_id=user_id,
                data=data_dump,
            )
        index_key = self._key(
            self.key_config.subagent_index,
            user_id=user_id,
        )
        await self._set_with_ttl(key, record.model_dump_json())
        await self._client.sadd(index_key, record.id)
        return record.id

    async def list_subagents(self, user_id: str) -> list[SubAgentRecord]:
        """Return all sub-agent records belonging to the given user.

        Args:
            user_id (`str`):
                The owner user id.

        Returns:
            `list[SubAgentRecord]`:
                All sub-agent records for the user.
        """
        index_key = self._key(
            self.key_config.subagent_index,
            user_id=user_id,
        )
        ids = await self._client.smembers(index_key)
        records = []
        for subagent_id in ids:
            raw = await self._client.get(
                self._key(
                    self.key_config.subagent,
                    user_id=user_id,
                    subagent_id=subagent_id,
                ),
            )
            if raw:
                records.append(SubAgentRecord.model_validate_json(raw))
        return records

    async def get_subagent(
        self,
        user_id: str,
        subagent_id: str,
    ) -> SubAgentRecord | None:
        """Fetch a single sub-agent record by id."""
        key = self._key(
            self.key_config.subagent,
            user_id=user_id,
            subagent_id=subagent_id,
        )
        raw = await self._client.get(key)
        return SubAgentRecord.model_validate_json(raw) if raw else None

    async def delete_subagent(
        self,
        user_id: str,
        subagent_id: str,
    ) -> bool:
        """Delete a sub-agent record and remove it from the user index.

        Args:
            user_id (`str`):
                The owner user id.
            subagent_id (`str`):
                The id of the sub-agent to delete.

        Returns:
            `bool`:
                ``True`` if the record existed and was deleted.
        """
        key = self._key(
            self.key_config.subagent,
            user_id=user_id,
            subagent_id=subagent_id,
        )
        index_key = self._key(
            self.key_config.subagent_index,
            user_id=user_id,
        )
        deleted = await self._client.delete(key)
        await self._client.srem(index_key, subagent_id)
        return deleted > 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/subagent_storage_test.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/app/storage/_model/_subagent.py src/agentscope/app/storage/_model/__init__.py src/agentscope/app/storage/_base.py src/agentscope/app/storage/_redis_storage.py tests/subagent_storage_test.py
git commit -m "feat(subagent): add sub-agent storage (record + Redis CRUD)"
```

---

### Task 5: REST schemas + router

**Files:**
- Create: `src/agentscope/app/_router/_schema/_subagent.py`
- Modify: `src/agentscope/app/_router/_schema/__init__.py`
- Create: `src/agentscope/app/_router/_subagent.py`
- Modify: `src/agentscope/app/_router/__init__.py`
- Modify: `src/agentscope/app/_app.py`
- Test: `tests/subagent_router_test.py`

**Interfaces:**
- Consumes: `SubAgentFactory` (Task 2); `SubAgentRecord`, storage subagent methods (Task 4); `get_current_user_id`, `get_storage` (`deps.py`).
- Produces:
  - Schemas: `CreateSubAgentRequest{data}`, `CreateSubAgentResponse{subagent_id}`, `UpdateSubAgentRequest{data}`, `SubAgentView{id,data,created_at,updated_at}`, `ListSubAgentsResponse{subagents,total}`, `ListSubAgentSchemasResponse{schemas}`.
  - `subagent_router` (prefix `/subagent`): `GET /schemas`, `GET /`, `POST /` (201), `PATCH /{subagent_id}`, `DELETE /{subagent_id}` (204).

- [ ] **Step 1: Write the failing test**

```python
# tests/subagent_router_test.py
# -*- coding: utf-8 -*-
"""Router tests for the sub-agent CRUD endpoints (FastAPI TestClient)."""
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from agentscope.app._router import subagent_router
from agentscope.app.storage import SubAgentRecord
from agentscope.subagent import SubAgentConfigBase

HEADERS = {"X-User-ID": "u1"}


class _FakeStorage:
    """Minimal in-memory stand-in for the subagent storage surface."""

    def __init__(self) -> None:
        self._records: dict[str, SubAgentRecord] = {}

    async def upsert_subagent(
        self,
        user_id: str,
        config: SubAgentConfigBase,
    ) -> str:
        """Store the config as a record keyed by its id."""
        self._records[config.id] = SubAgentRecord(
            id=config.id,
            user_id=user_id,
            data=config.model_dump(mode="json"),
        )
        return config.id

    async def list_subagents(self, user_id: str) -> list[SubAgentRecord]:
        """Return all stored records."""
        return list(self._records.values())

    async def get_subagent(
        self,
        user_id: str,
        subagent_id: str,
    ) -> SubAgentRecord | None:
        """Return one record or None."""
        return self._records.get(subagent_id)

    async def delete_subagent(
        self,
        user_id: str,
        subagent_id: str,
    ) -> bool:
        """Delete one record."""
        return self._records.pop(subagent_id, None) is not None


def make_client() -> TestClient:
    """Build a minimal app exposing only the subagent router."""
    app = FastAPI()
    app.state.storage = _FakeStorage()
    app.include_router(subagent_router)
    return TestClient(app)


class SubAgentRouterTest(unittest.TestCase):
    """Validate the CRUD endpoints round-trip."""

    def test_create_list_delete(self) -> None:
        """POST then GET then DELETE."""
        client = make_client()
        body = {
            "data": {
                "type": "cli_subagent",
                "name": "claude_code",
                "description": "Delegate coding.",
                "command": "claude -p {prompt}",
            },
        }
        created = client.post("/subagent/", json=body, headers=HEADERS)
        self.assertEqual(created.status_code, 201)
        subagent_id = created.json()["subagent_id"]
        self.assertTrue(subagent_id)

        listed = client.get("/subagent/", headers=HEADERS)
        self.assertEqual(listed.status_code, 200)
        payload = listed.json()
        self.assertEqual(payload["total"], 1)
        self.assertEqual(
            payload["subagents"][0]["data"]["name"],
            "claude_code",
        )

        deleted = client.delete(
            f"/subagent/{subagent_id}",
            headers=HEADERS,
        )
        self.assertEqual(deleted.status_code, 204)
        self.assertEqual(
            client.get("/subagent/", headers=HEADERS).json()["total"],
            0,
        )

    def test_create_rejects_bad_command(self) -> None:
        """A command without {prompt} is a 422."""
        client = make_client()
        body = {
            "data": {
                "type": "cli_subagent",
                "name": "bad",
                "description": "x",
                "command": "claude -p",
            },
        }
        resp = client.post("/subagent/", json=body, headers=HEADERS)
        self.assertEqual(resp.status_code, 422)

    def test_delete_missing_is_404(self) -> None:
        """Deleting an unknown id returns 404."""
        client = make_client()
        resp = client.delete("/subagent/nope", headers=HEADERS)
        self.assertEqual(resp.status_code, 404)

    def test_schemas_lists_cli_type(self) -> None:
        """GET /subagent/schemas returns the registered schemas."""
        client = make_client()
        resp = client.get("/subagent/schemas", headers=HEADERS)
        self.assertEqual(resp.status_code, 200)
        titles = [s.get("title") for s in resp.json()["schemas"]]
        self.assertIn("CLI Sub-Agent", titles)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/subagent_router_test.py -v`
Expected: FAIL — `ImportError: cannot import name 'subagent_router'`.

- [ ] **Step 3a: Add the schemas**

```python
# src/agentscope/app/_router/_schema/_subagent.py
# -*- coding: utf-8 -*-
"""Request / response schemas for the sub-agent router."""
from datetime import datetime

from pydantic import BaseModel, Field


class CreateSubAgentRequest(BaseModel):
    """Request body for creating a sub-agent config."""

    data: dict = Field(description="Sub-Agent config payload.")


class CreateSubAgentResponse(BaseModel):
    """Response body after creating a sub-agent config."""

    subagent_id: str = Field(
        description="Server-assigned sub-agent identifier.",
    )


class UpdateSubAgentRequest(BaseModel):
    """Request body for updating a sub-agent config."""

    data: dict = Field(description="New sub-agent config payload.")


class SubAgentView(BaseModel):
    """A sub-agent config as returned to the frontend."""

    id: str = Field(description="The sub-agent id.")
    data: dict = Field(description="The sub-agent config payload.")
    created_at: datetime = Field(description="Creation time.")
    updated_at: datetime = Field(description="Last update time.")


class ListSubAgentsResponse(BaseModel):
    """Response body for listing sub-agent configs."""

    subagents: list[SubAgentView] = Field(
        description="Sub-Agent config records.",
    )
    total: int = Field(description="Total number of sub-agents.")


class ListSubAgentSchemasResponse(BaseModel):
    """Response body for listing sub-agent type schemas."""

    schemas: list[dict] = Field(
        description="JSON schemas for all registered sub-agent types.",
    )
```

Register them in `src/agentscope/app/_router/_schema/__init__.py` — add the import block and the six names to `__all__`:

```python
from ._subagent import (
    CreateSubAgentRequest,
    CreateSubAgentResponse,
    UpdateSubAgentRequest,
    SubAgentView,
    ListSubAgentsResponse,
    ListSubAgentSchemasResponse,
)
```

- [ ] **Step 3b: Add the router**

```python
# src/agentscope/app/_router/_subagent.py
# -*- coding: utf-8 -*-
"""Sub-Agent router — CRUD endpoints for CLI sub-agent configs."""
from fastapi import APIRouter, Depends, HTTPException, status

from ..deps import get_current_user_id, get_storage
from ._schema import (
    CreateSubAgentRequest,
    CreateSubAgentResponse,
    ListSubAgentSchemasResponse,
    ListSubAgentsResponse,
    SubAgentView,
    UpdateSubAgentRequest,
)
from ..storage import StorageBase
from ...subagent import SubAgentFactory

subagent_router = APIRouter(
    prefix="/subagent",
    tags=["subagent"],
    responses={404: {"description": "Not found"}},
)


@subagent_router.get(
    "/schemas",
    response_model=ListSubAgentSchemasResponse,
    summary="List JSON schemas for all sub-agent types",
)
async def list_subagent_schemas() -> ListSubAgentSchemasResponse:
    """Return JSON schemas for all registered sub-agent types."""
    return ListSubAgentSchemasResponse(
        schemas=SubAgentFactory.list_schemas(),
    )


@subagent_router.get(
    "/",
    response_model=ListSubAgentsResponse,
    summary="List all sub-agents",
)
async def list_subagents(
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> ListSubAgentsResponse:
    """Return all sub-agent configs owned by the authenticated user.

    Args:
        user_id (`str`): Injected authenticated user ID.
        storage (`StorageBase`): Injected storage backend.

    Returns:
        `ListSubAgentsResponse`: All sub-agent configs for the user.
    """
    records = await storage.list_subagents(user_id)
    views = [
        SubAgentView(
            id=record.id,
            data=record.data,
            created_at=record.created_at,
            updated_at=record.updated_at,
        )
        for record in records
    ]
    return ListSubAgentsResponse(subagents=views, total=len(views))


@subagent_router.post(
    "/",
    response_model=CreateSubAgentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a new sub-agent",
)
async def create_subagent(
    body: CreateSubAgentRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> CreateSubAgentResponse:
    """Store a new sub-agent config.

    Args:
        body (`CreateSubAgentRequest`): The sub-agent payload.
        user_id (`str`): Injected authenticated user ID.
        storage (`StorageBase`): Injected storage backend.

    Returns:
        `CreateSubAgentResponse`: The server-assigned sub-agent id.
    """
    try:
        config = SubAgentFactory.from_dict(body.data)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    subagent_id = await storage.upsert_subagent(user_id, config)
    return CreateSubAgentResponse(subagent_id=subagent_id)


@subagent_router.patch(
    "/{subagent_id}",
    response_model=SubAgentView,
    summary="Update a sub-agent",
)
async def update_subagent(
    subagent_id: str,
    body: UpdateSubAgentRequest,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> SubAgentView:
    """Replace the payload of an existing sub-agent config.

    Args:
        subagent_id (`str`): The sub-agent to update.
        body (`UpdateSubAgentRequest`): New sub-agent payload.
        user_id (`str`): Injected authenticated user ID.
        storage (`StorageBase`): Injected storage backend.

    Returns:
        `SubAgentView`: The updated record.

    Raises:
        `HTTPException`: 404 if the sub-agent does not exist; 422 on an
            invalid payload.
    """
    existing = await storage.get_subagent(user_id, subagent_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sub-Agent {subagent_id!r} not found.",
        )
    try:
        config = SubAgentFactory.from_dict(body.data)
    except (ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc
    config.id = subagent_id
    await storage.upsert_subagent(user_id, config)
    updated = await storage.get_subagent(user_id, subagent_id)
    if updated is None:
        raise RuntimeError(
            f"Sub-Agent {subagent_id!r} disappeared after upsert.",
        )
    return SubAgentView(
        id=updated.id,
        data=updated.data,
        created_at=updated.created_at,
        updated_at=updated.updated_at,
    )


@subagent_router.delete(
    "/{subagent_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a sub-agent",
)
async def delete_subagent(
    subagent_id: str,
    user_id: str = Depends(get_current_user_id),
    storage: StorageBase = Depends(get_storage),
) -> None:
    """Permanently delete a sub-agent config.

    Args:
        subagent_id (`str`): The sub-agent to delete.
        user_id (`str`): Injected authenticated user ID.
        storage (`StorageBase`): Injected storage backend.

    Raises:
        `HTTPException`: 404 if the sub-agent does not exist.
    """
    deleted = await storage.delete_subagent(user_id, subagent_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Sub-Agent {subagent_id!r} not found.",
        )
```

Register the router in `src/agentscope/app/_router/__init__.py` (add the import + `"subagent_router"` to `__all__`):

```python
from ._subagent import subagent_router
```

Register it in `src/agentscope/app/_app.py` — add `subagent_router` to the import from `._router` (near the other router imports) and add it to the `include_router` loop tuple (lines 257-268):

```python
    for router in (
        agent_router,
        chat_router,
        credential_router,
        subagent_router,
        knowledge_base_router,
        schedule_router,
        session_router,
        workspace_router,
        model_router,
        tts_model_router,
    ):
        app.include_router(router)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/subagent_router_test.py -v`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/app/_router/_schema/_subagent.py src/agentscope/app/_router/_schema/__init__.py src/agentscope/app/_router/_subagent.py src/agentscope/app/_router/__init__.py src/agentscope/app/_app.py tests/subagent_router_test.py
git commit -m "feat(subagent): add /subagent CRUD router"
```

---

### Task 6: Agent-tool factory (attach to the toolkit)

**Files:**
- Create: `src/agentscope/subagent/_agent_tools.py`
- Modify: `src/agentscope/subagent/__init__.py`
- Test: `tests/subagent_agent_tools_test.py`

**Interfaces:**
- Consumes: `SubAgentFactory`, `CliSubAgentConfig`, `CliSubAgentTool` (Tasks 2-3); storage's `list_subagents`/`get_session`, `workspace_manager.get_workspace(...)`, `workspace.get_backend()`.
- Produces: `make_subagent_tool_factory(storage, workspace_manager) -> Callable[[str, str, str], Awaitable[list[ToolBase]]]` — the `extra_agent_tools` factory. Builds one `CliSubAgentTool` per stored config, binds the session workspace backend (falling back to `LocalBackend`), and skips malformed configs (logged).

- [ ] **Step 1: Write the failing test**

```python
# tests/subagent_agent_tools_test.py
# -*- coding: utf-8 -*-
"""Tests for make_subagent_tool_factory."""
from unittest.async_case import IsolatedAsyncioTestCase

from agentscope.app.storage import SubAgentRecord
from agentscope.subagent import CliSubAgentTool, make_subagent_tool_factory


class _Sentinel:
    """A stand-in backend object we can identity-check."""


class _FakeWorkspace:
    """Workspace exposing a fixed backend."""

    def __init__(self, backend: object) -> None:
        self._backend = backend

    def get_backend(self) -> object:
        """Return the fixed backend."""
        return self._backend


class _FakeWorkspaceManager:
    """Workspace manager returning a fixed workspace."""

    def __init__(self, workspace: object | None) -> None:
        self._workspace = workspace

    async def get_workspace(self, *args, **kwargs) -> object:
        """Return the fixed workspace or raise if none configured."""
        if self._workspace is None:
            raise RuntimeError("no workspace")
        return self._workspace


class _FakeStorage:
    """Storage returning fixed sub-agent records and no session."""

    def __init__(self, records: list[SubAgentRecord]) -> None:
        self._records = records

    async def list_subagents(self, user_id: str) -> list[SubAgentRecord]:
        """Return the fixed records."""
        return self._records

    async def get_session(self, *args, **kwargs) -> None:
        """No session binding in these tests."""
        return None


def _record(subagent_id: str, name: str, command: str) -> SubAgentRecord:
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
            "cwd": None,
            "env": None,
            "timeout": 600,
        },
    )


class MakeSubAgentToolFactoryTest(IsolatedAsyncioTestCase):
    """Validate the extra_agent_tools factory."""

    async def test_builds_one_tool_per_config(self) -> None:
        """Each stored config yields one CliSubAgentTool."""
        backend = _Sentinel()
        storage = _FakeStorage(
            [
                _record("sa-1", "claude_code", "claude -p {prompt}"),
                _record("sa-2", "codex", "codex exec {prompt}"),
            ],
        )
        wm = _FakeWorkspaceManager(_FakeWorkspace(backend))
        factory = make_subagent_tool_factory(storage, wm)
        tools = await factory("u1", "a1", "s1")
        self.assertEqual(len(tools), 2)
        self.assertTrue(all(isinstance(t, CliSubAgentTool) for t in tools))
        self.assertEqual({t.name for t in tools}, {"claude_code", "codex"})
        # Backend from the workspace is bound to each tool.
        self.assertIs(tools[0]._backend, backend)

    async def test_skips_malformed_config(self) -> None:
        """A malformed config (bad type) is skipped, not fatal."""
        storage = _FakeStorage(
            [
                _record("sa-1", "claude_code", "claude -p {prompt}"),
                SubAgentRecord(
                    id="sa-bad",
                    user_id="u1",
                    data={"type": "unknown_kind"},
                ),
            ],
        )
        wm = _FakeWorkspaceManager(None)  # forces LocalBackend fallback
        factory = make_subagent_tool_factory(storage, wm)
        tools = await factory("u1", "a1", "s1")
        self.assertEqual(len(tools), 1)
        self.assertEqual(tools[0].name, "claude_code")

    async def test_falls_back_to_local_backend(self) -> None:
        """When the workspace can't be resolved, the tool still builds."""
        storage = _FakeStorage(
            [_record("sa-1", "claude_code", "claude -p {prompt}")],
        )
        wm = _FakeWorkspaceManager(None)
        factory = make_subagent_tool_factory(storage, wm)
        tools = await factory("u1", "a1", "s1")
        self.assertEqual(len(tools), 1)
        # A LocalBackend was created (not None).
        self.assertIsNotNone(tools[0]._backend)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/subagent_agent_tools_test.py -v`
Expected: FAIL — `ImportError: cannot import name 'make_subagent_tool_factory'`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/agentscope/subagent/_agent_tools.py
# -*- coding: utf-8 -*-
"""Factory that turns stored sub-agent configs into toolkit tools."""
from collections.abc import Awaitable, Callable
from typing import Any

from .._logging import logger
from ._factory import SubAgentFactory
from ._base import CliSubAgentConfig
from ._tool import CliSubAgentTool
from ..tool import BackendBase, ToolBase


async def _resolve_backend(
    storage: Any,
    workspace_manager: Any,
    user_id: str,
    agent_id: str,
    session_id: str,
) -> BackendBase | None:
    """Best-effort resolution of the session's workspace backend.

    Returns ``None`` on any failure so the tool falls back to a
    :class:`LocalBackend`. This keeps sub-agents running in the same
    environment as the session's ``Bash`` tool when possible, and stays
    robust for the local demo.

    Args:
        storage (`Any`): The app storage backend.
        workspace_manager (`Any`): The workspace manager.
        user_id (`str`): The owner user id.
        agent_id (`str`): The agent id.
        session_id (`str`): The session id.

    Returns:
        `BackendBase | None`: The resolved backend, or ``None``.
    """
    try:
        session = await storage.get_session(user_id, agent_id, session_id)
        workspace_id = session.config.workspace_id if session else None
        workspace = await workspace_manager.get_workspace(
            user_id,
            agent_id,
            session_id,
            workspace_id,
        )
        return workspace.get_backend()
    except Exception:  # noqa: BLE001  # pragma: no cover - defensive
        return None


def make_subagent_tool_factory(
    storage: Any,
    workspace_manager: Any,
) -> Callable[[str, str, str], Awaitable[list[ToolBase]]]:
    """Build an ``extra_agent_tools`` factory backed by stored configs.

    The returned async factory reads the user's sub-agent configs and
    returns one :class:`CliSubAgentTool` per valid config, binding the
    session's workspace backend when resolvable.

    Args:
        storage (`StorageBase`):
            The app storage backend (used to read sub-agent configs).
        workspace_manager (`WorkspaceManagerBase`):
            The workspace manager (used to resolve the session backend).

    Returns:
        `Callable[[str, str, str], Awaitable[list[ToolBase]]]`:
            An ``AgentToolFactory`` suitable for
            ``create_app(extra_agent_tools=...)``.
    """

    async def _factory(
        user_id: str,
        agent_id: str,
        session_id: str,
    ) -> list[ToolBase]:
        """Build sub-agent tools for one chat turn."""
        records = await storage.list_subagents(user_id)
        backend = await _resolve_backend(
            storage,
            workspace_manager,
            user_id,
            agent_id,
            session_id,
        )
        tools: list[ToolBase] = []
        for record in records:
            try:
                config = SubAgentFactory.from_dict(record.data)
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "Skipping malformed sub-agent %s: %s",
                    record.id,
                    exc,
                )
                continue
            if not isinstance(config, CliSubAgentConfig):
                continue
            tools.append(
                CliSubAgentTool(
                    name=config.name,
                    description=config.description,
                    command=config.command,
                    cwd=config.cwd,
                    env=config.env,
                    timeout=config.timeout,
                    backend=backend,
                ),
            )
        return tools

    return _factory
```

Update the package init to export it:

```python
# src/agentscope/subagent/__init__.py
# -*- coding: utf-8 -*-
"""The sub-agent module."""
from ._base import SubAgentConfigBase, CliSubAgentConfig
from ._factory import SubAgentFactory
from ._tool import CliSubAgentTool
from ._agent_tools import make_subagent_tool_factory

__all__ = [
    "SubAgentConfigBase",
    "CliSubAgentConfig",
    "SubAgentFactory",
    "CliSubAgentTool",
    "make_subagent_tool_factory",
]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/subagent_agent_tools_test.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_agent_tools.py src/agentscope/subagent/__init__.py tests/subagent_agent_tools_test.py
git commit -m "feat(subagent): add extra_agent_tools factory"
```

---

### Task 7: Wire the factory into the example service + full backend check

**Files:**
- Modify: `examples/agent_service/main.py`

**Interfaces:**
- Consumes: `make_subagent_tool_factory` (Task 6).

- [ ] **Step 1: Add the import + kwarg**

In `examples/agent_service/main.py`, add the import near the other `agentscope` imports:

```python
from agentscope.subagent import make_subagent_tool_factory
```

Then pass it to `create_app(...)` — add one kwarg (the `storage` and `workspace_manager` locals already exist above the call):

```python
app = create_app(
    storage=storage,
    message_bus=InMemoryMessageBus(),
    workspace_manager=LocalWorkspaceManager(
        basedir=os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "workspaces",
        ),
        default_mcps=default_mcps,
    ),
    knowledge_base_manager=CollectionPerKbManager(
        storage=storage,
        vector_store=vector_store,
    ),
    extra_agent_tools=make_subagent_tool_factory(
        storage,
        # Resolved lazily inside the factory via app.state; the manager
        # instance below is the same object create_app stores.
        None,
    ),
    custom_subagent_templates=[
        # ... unchanged ...
    ],
    extra_middlewares=[
        # ... unchanged ...
    ],
)
```

> **NOTE for the implementer:** the `workspace_manager` is constructed inline in the current `main.py`. Refactor it to a local variable first so the factory can capture the *same* instance:
>
> ```python
> workspace_manager = LocalWorkspaceManager(
>     basedir=os.path.join(
>         os.path.dirname(os.path.abspath(__file__)),
>         "workspaces",
>     ),
>     default_mcps=default_mcps,
> )
>
> app = create_app(
>     storage=storage,
>     message_bus=InMemoryMessageBus(),
>     workspace_manager=workspace_manager,
>     knowledge_base_manager=CollectionPerKbManager(
>         storage=storage,
>         vector_store=vector_store,
>     ),
>     extra_agent_tools=make_subagent_tool_factory(
>         storage,
>         workspace_manager,
>     ),
>     custom_subagent_templates=[...],   # unchanged
>     extra_middlewares=[...],           # unchanged
> )
> ```
>
> Use this refactored form (drop the `None` placeholder in the first snippet).

- [ ] **Step 2: Verify the example imports cleanly**

Run: `python -c "import ast; ast.parse(open('examples/agent_service/main.py').read()); print('parse ok')"`
Expected: `parse ok`

Then verify the wiring symbol resolves:
Run: `python -c "from agentscope.subagent import make_subagent_tool_factory; print('import ok')"`
Expected: `import ok`

- [ ] **Step 3: Run the full backend test + lint gate**

Run: `pytest tests/subagent_config_test.py tests/subagent_factory_test.py tests/subagent_tool_test.py tests/subagent_storage_test.py tests/subagent_router_test.py tests/subagent_agent_tools_test.py -v`
Expected: PASS (all).

Run: `pre-commit run --files src/agentscope/subagent/_base.py src/agentscope/subagent/_factory.py src/agentscope/subagent/_tool.py src/agentscope/subagent/_agent_tools.py src/agentscope/subagent/__init__.py src/agentscope/app/storage/_base.py src/agentscope/app/storage/_redis_storage.py src/agentscope/app/storage/_model/_subagent.py src/agentscope/app/storage/_model/__init__.py src/agentscope/app/_router/_subagent.py src/agentscope/app/_router/_schema/_subagent.py src/agentscope/app/_router/_schema/__init__.py src/agentscope/app/_router/__init__.py src/agentscope/app/_app.py examples/agent_service/main.py`
Expected: all hooks pass (fix any black/flake8/pylint/mypy findings in-place, then re-run).

- [ ] **Step 4: Commit**

```bash
git add examples/agent_service/main.py
git commit -m "feat(subagent): wire sub-agent tool factory into example service"
```

---

### Task 8: Frontend — API types + client module + barrel

**Files:**
- Modify: `examples/web_ui/frontend/src/api/types.ts`
- Create: `examples/web_ui/frontend/src/api/subagent.ts`
- Modify: `examples/web_ui/frontend/src/api/index.ts`

**Interfaces:**
- Produces: TS types `SubAgentView`, `CreateSubAgentRequest`, `CreateSubAgentResponse`, `UpdateSubAgentRequest`, `SubAgentListResponse`; `subagentApi` with `list`, `create`, `update`, `delete`.

- [ ] **Step 1: Add the types**

Append to `examples/web_ui/frontend/src/api/types.ts` (after the Skill section, before Schedule):

```ts
// ─── Sub-Agent ──────────────────────────────────────────────────────────────

export interface SubAgentData {
	type: 'cli_subagent';
	name: string;
	description: string;
	command: string;
	cwd?: string | null;
	env?: Record<string, string> | null;
	timeout?: number;
}

export interface SubAgentView {
	id: string;
	data: SubAgentData;
	created_at: string;
	updated_at: string;
}

export interface CreateSubAgentRequest {
	data: Record<string, unknown>;
}

export interface CreateSubAgentResponse {
	subagent_id: string;
}

export interface UpdateSubAgentRequest {
	data: Record<string, unknown>;
}

export interface SubAgentListResponse {
	subagents: SubAgentView[];
	total: number;
}
```

- [ ] **Step 2: Add the API module**

```ts
// examples/web_ui/frontend/src/api/subagent.ts
import { client } from './client';
import type {
	CreateSubAgentRequest,
	CreateSubAgentResponse,
	SubAgentListResponse,
	SubAgentView,
	UpdateSubAgentRequest,
} from './types';

export const subagentApi = {
	list: () => client.get<SubAgentListResponse>('/subagent/'),

	create: (body: CreateSubAgentRequest) =>
		client.post<CreateSubAgentResponse>('/subagent/', body),

	update: (subagentId: string, body: UpdateSubAgentRequest) =>
		client.patch<SubAgentView>(`/subagent/${subagentId}`, body),

	delete: (subagentId: string) => client.delete(`/subagent/${subagentId}`),
};
```

- [ ] **Step 3: Export it from the barrel**

In `examples/web_ui/frontend/src/api/index.ts`, add:

```ts
export { subagentApi } from './subagent';
```

- [ ] **Step 4: Typecheck**

Run: `pnpm -C examples/web_ui/frontend exec tsc --noEmit`
Expected: no errors. (If the project lacks a bare `tsc`, run `pnpm -C examples/web_ui/frontend build` instead — it type-checks.)

- [ ] **Step 5: Commit**

```bash
git add examples/web_ui/frontend/src/api/types.ts examples/web_ui/frontend/src/api/subagent.ts examples/web_ui/frontend/src/api/index.ts
git commit -m "feat(webui): add sub-agent API types and client"
```

---

### Task 9: Frontend — `useSubagents` hook

**Files:**
- Create: `examples/web_ui/frontend/src/hooks/useSubagents.ts`

**Interfaces:**
- Consumes: `subagentApi` (Task 8).
- Produces: `useSubagents()` → `{ subagents, loading, error, refetch, create, remove }`.

- [ ] **Step 1: Write the hook**

```ts
// examples/web_ui/frontend/src/hooks/useSubagents.ts
import { useCallback, useEffect, useState } from 'react';

import { subagentApi } from '@/api';
import type { SubAgentView } from '@/api';

export function useSubagents() {
	const [subagents, setSubagents] = useState<SubAgentView[]>([]);
	const [loading, setLoading] = useState(false);
	const [error, setError] = useState<Error | null>(null);

	const refetch = useCallback(async () => {
		setLoading(true);
		setError(null);
		try {
			const res = await subagentApi.list();
			setSubagents(res.subagents);
		} catch (e) {
			setError(e as Error);
		} finally {
			setLoading(false);
		}
	}, []);

	useEffect(() => {
		refetch();
	}, [refetch]);

	const create = useCallback(
		async (data: Record<string, unknown>) => {
			await subagentApi.create({ data });
			await refetch();
		},
		[refetch],
	);

	const remove = useCallback(
		async (subagentId: string) => {
			await subagentApi.delete(subagentId);
			await refetch();
		},
		[refetch],
	);

	return { subagents, loading, error, refetch, create, remove };
}
```

- [ ] **Step 2: Typecheck**

Run: `pnpm -C examples/web_ui/frontend exec tsc --noEmit`
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add examples/web_ui/frontend/src/hooks/useSubagents.ts
git commit -m "feat(webui): add useSubagents hook"
```

---

### Task 10: Frontend — `AddSubagentDialog`

**Files:**
- Create: `examples/web_ui/frontend/src/components/dialog/AddSubagentDialog.tsx`

**Interfaces:**
- Consumes: UI primitives (Dialog/Input/Label/Button), `useTranslation`.
- Produces: `AddSubagentDialog({ children, onAdd })` where `onAdd(data: Record<string, unknown>) => Promise<void>`.

- [ ] **Step 1: Write the dialog** (modeled on `AddSkillDialog.tsx`, with the sub-agent field set)

```tsx
// examples/web_ui/frontend/src/components/dialog/AddSubagentDialog.tsx
import { CircleAlert, Loader2, PlusCircle } from 'lucide-react';
import { useState, type ReactNode } from 'react';

import { Button } from '@/components/ui/button';
import {
	Dialog,
	DialogContent,
	DialogDescription,
	DialogFooter,
	DialogHeader,
	DialogTitle,
	DialogTrigger,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { useTranslation } from '@/i18n/useI18n';

interface AddSubagentDialogProps {
	children: ReactNode;
	onAdd: (data: Record<string, unknown>) => Promise<void>;
}

export function AddSubagentDialog({ children, onAdd }: AddSubagentDialogProps) {
	const { t } = useTranslation();
	const [open, setOpen] = useState(false);
	const [name, setName] = useState('');
	const [description, setDescription] = useState('');
	const [command, setCommand] = useState('');
	const [cwd, setCwd] = useState('');
	const [timeout, setTimeoutValue] = useState('600');
	const [loading, setLoading] = useState(false);
	const [error, setError] = useState<string | null>(null);

	const canSubmit =
		name.trim() !== '' &&
		description.trim() !== '' &&
		command.includes('{prompt}');

	const reset = () => {
		setName('');
		setDescription('');
		setCommand('');
		setCwd('');
		setTimeoutValue('600');
		setError(null);
	};

	const handleSubmit = async () => {
		if (!canSubmit) {
			setError(t('dialog-subagent-add.promptHint'));
			return;
		}
		setLoading(true);
		setError(null);
		try {
			await onAdd({
				type: 'cli_subagent',
				name: name.trim(),
				description: description.trim(),
				command: command.trim(),
				cwd: cwd.trim() || null,
				timeout: Number(timeout) || 600,
			});
			reset();
			setOpen(false);
		} catch (e) {
			setError((e as Error).message);
		} finally {
			setLoading(false);
		}
	};

	return (
		<Dialog open={open} onOpenChange={setOpen}>
			<DialogTrigger asChild>{children}</DialogTrigger>
			<DialogContent className="!w-[560px] !max-w-[560px]">
				<DialogHeader>
					<DialogTitle>{t('dialog-subagent-add.title')}</DialogTitle>
					<DialogDescription>
						{t('dialog-subagent-add.description')}
					</DialogDescription>
				</DialogHeader>
				<div className="flex flex-col gap-y-3">
					<div className="flex flex-col gap-y-1.5">
						<Label htmlFor="sa-name">
							{t('dialog-subagent-add.nameLabel')}
						</Label>
						<Input
							id="sa-name"
							placeholder="claude_code"
							value={name}
							onChange={(e) => setName(e.target.value)}
						/>
					</div>
					<div className="flex flex-col gap-y-1.5">
						<Label htmlFor="sa-desc">
							{t('dialog-subagent-add.descLabel')}
						</Label>
						<Input
							id="sa-desc"
							placeholder={t('dialog-subagent-add.descPlaceholder')}
							value={description}
							onChange={(e) => setDescription(e.target.value)}
						/>
					</div>
					<div className="flex flex-col gap-y-1.5">
						<Label htmlFor="sa-cmd">
							{t('dialog-subagent-add.commandLabel')}
						</Label>
						<Input
							id="sa-cmd"
							placeholder="claude -p {prompt} --dangerously-skip-permissions"
							value={command}
							onChange={(e) => setCommand(e.target.value)}
						/>
						<span className="text-muted-foreground text-xs">
							{t('dialog-subagent-add.promptHint')}
						</span>
					</div>
					<div className="flex gap-x-3">
						<div className="flex flex-1 flex-col gap-y-1.5">
							<Label htmlFor="sa-cwd">
								{t('dialog-subagent-add.cwdLabel')}
							</Label>
							<Input
								id="sa-cwd"
								placeholder="/path/to/workdir"
								value={cwd}
								onChange={(e) => setCwd(e.target.value)}
							/>
						</div>
						<div className="flex w-32 flex-col gap-y-1.5">
							<Label htmlFor="sa-timeout">
								{t('dialog-subagent-add.timeoutLabel')}
							</Label>
							<Input
								id="sa-timeout"
								type="number"
								value={timeout}
								onChange={(e) => setTimeoutValue(e.target.value)}
							/>
						</div>
					</div>
					{error && <p className="text-destructive text-sm">{error}</p>}
				</div>
				<DialogFooter>
					<Button
						variant="ghost"
						onClick={() => setOpen(false)}
						disabled={loading}
					>
						<CircleAlert className="size-3.5" />
						{t('common.cancel')}
					</Button>
					<Button onClick={handleSubmit} disabled={loading || !canSubmit}>
						{loading ? (
							<Loader2 className="size-3.5 animate-spin" />
						) : (
							<PlusCircle className="size-3.5" />
						)}
						{loading ? t('dialog-mcp-create.adding') : t('common.add')}
					</Button>
				</DialogFooter>
			</DialogContent>
		</Dialog>
	);
}
```

- [ ] **Step 2: Typecheck**

Run: `pnpm -C examples/web_ui/frontend exec tsc --noEmit`
Expected: no errors (i18n keys used here are added in Task 12; typecheck passes regardless since `t()` takes any string).

- [ ] **Step 3: Commit**

```bash
git add examples/web_ui/frontend/src/components/dialog/AddSubagentDialog.tsx
git commit -m "feat(webui): add AddSubagentDialog"
```

---

### Task 11: Frontend — `SubagentPanel`

**Files:**
- Create: `examples/web_ui/frontend/src/components/panel/SubagentPanel.tsx`

**Interfaces:**
- Consumes: `SubAgentView` (Task 8), `AddSubagentDialog` (Task 10), `DeleteDialog`, `PanelEmpty`, UI primitives.
- Produces: `SubagentPanel({ subagents, loading, onAdd, onRemove })` where `onAdd(data) => Promise<void>`, `onRemove(id) => Promise<void>`.

- [ ] **Step 1: Write the panel** (modeled on `McpPanel.tsx`)

```tsx
// examples/web_ui/frontend/src/components/panel/SubagentPanel.tsx
import { Bot, PlusCircle, Search, SearchX } from 'lucide-react';
import { useState } from 'react';

import type { SubAgentView } from '@/api';
import { AddSubagentDialog } from '@/components/dialog/AddSubagentDialog';
import { DeleteDialog } from '@/components/dialog/DeleteDialog.tsx';
import { PanelEmpty } from '@/components/panel/PanelEmpty';
import { Button } from '@/components/ui/button';
import { InputGroup, InputGroupAddon, InputGroupInput } from '@/components/ui/input-group';
import { Item, ItemActions, ItemContent, ItemDescription, ItemTitle } from '@/components/ui/item';
import { Kbd } from '@/components/ui/kbd';
import { useTranslation } from '@/i18n/useI18n.ts';
import { Trash } from 'lucide-react';

interface SubagentPanelProps {
	subagents: SubAgentView[];
	loading?: boolean;
	onAdd: (data: Record<string, unknown>) => Promise<void>;
	onRemove: (id: string) => Promise<void>;
}

export function SubagentPanel({
	subagents,
	loading = false,
	onAdd,
	onRemove,
}: SubagentPanelProps) {
	const { t } = useTranslation();
	const [search, setSearch] = useState('');
	const [deleteOpen, setDeleteOpen] = useState(false);
	const [deleteTarget, setDeleteTarget] = useState<SubAgentView | null>(null);

	const filtered = search
		? subagents.filter((s) =>
				s.data.name.toLowerCase().includes(search.toLowerCase()),
			)
		: subagents;

	return (
		<div className="flex flex-col flex-1 min-h-0 gap-y-2">
			<span className="text-muted-foreground text-sm">
				{t('panel.subagent.description')}
			</span>
			<InputGroup>
				<InputGroupInput
					placeholder={t('panel.subagent.searchPlaceholder')}
					value={search}
					onChange={(e) => setSearch(e.target.value)}
				/>
				<InputGroupAddon align="inline-end">
					<Search />
				</InputGroupAddon>
			</InputGroup>

			{loading ? (
				<div className="flex flex-1 items-center justify-center">
					<p className="text-muted-foreground text-sm">{t('panel.loading')}</p>
				</div>
			) : filtered.length === 0 ? (
				<PanelEmpty
					icon={search ? SearchX : Bot}
					title={search ? t('panel.search.emptyTitle') : t('panel.subagent.emptyTitle')}
					description={
						search
							? t('panel.search.emptyDescription', { query: search })
							: t('panel.subagent.emptyDescription')
					}
				/>
			) : (
				<div className="flex flex-col flex-1 min-h-0 overflow-y-auto gap-y-2">
					{filtered.map((sa) => (
						<Item key={sa.id} variant="outline">
							<ItemContent>
								<ItemTitle className="flex items-center gap-x-2">
									<Bot className="size-4 shrink-0" />
									{sa.data.name}
								</ItemTitle>
								<ItemDescription className="flex flex-col gap-y-1">
									<span>{sa.data.description}</span>
									<Kbd className="w-fit">{sa.data.command}</Kbd>
								</ItemDescription>
							</ItemContent>
							<ItemActions>
								<Button
									variant="outline"
									size="icon-sm"
									onClick={() => {
										setDeleteTarget(sa);
										setDeleteOpen(true);
									}}
								>
									<Trash />
								</Button>
							</ItemActions>
						</Item>
					))}
				</div>
			)}

			<AddSubagentDialog onAdd={onAdd}>
				<Button variant="default">
					<PlusCircle />
					{t('panel.subagent.add')}
				</Button>
			</AddSubagentDialog>

			<DeleteDialog
				open={deleteOpen}
				onOpenChange={setDeleteOpen}
				title={t('common.deleteTitle', {
					entity: t('panel.subagent.entity'),
					name: deleteTarget?.data.name ?? '',
				})}
				description={t('common.deleteDescription')}
				onConfirm={async () => {
					if (deleteTarget) await onRemove(deleteTarget.id);
				}}
			/>
		</div>
	);
}
```

- [ ] **Step 2: Typecheck**

Run: `pnpm -C examples/web_ui/frontend exec tsc --noEmit`
Expected: no errors. (If `Kbd` does not accept a `className` prop, drop it — check `@/components/ui/kbd`.)

- [ ] **Step 3: Commit**

```bash
git add examples/web_ui/frontend/src/components/panel/SubagentPanel.tsx
git commit -m "feat(webui): add SubagentPanel"
```

---

### Task 12: Frontend — register the panel + i18n

**Files:**
- Modify: `examples/web_ui/frontend/src/components/panel/PanelDock.tsx`
- Modify: `examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx`
- Modify: `examples/web_ui/frontend/src/i18n/locales/en.json`
- Modify: `examples/web_ui/frontend/src/i18n/locales/zh.json`

**Interfaces:**
- Consumes: `SubagentPanel` (Task 11), `useSubagents` (Task 9).

- [ ] **Step 1: Add the `PanelKey`**

In `examples/web_ui/frontend/src/components/panel/PanelDock.tsx` line 16:

```ts
export type PanelKey = 'plan' | 'mcp' | 'skill' | 'permission' | 'knowledge' | 'subagent';
```

- [ ] **Step 2: Wire ChatViewport — imports**

In `examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx`, add to the imports:

```tsx
import { Bot } from 'lucide-react';
import { SubagentPanel } from '@/components/panel/SubagentPanel';
import { useSubagents } from '@/hooks/useSubagents';
```

(Add `Bot` to the existing `lucide-react` import line rather than a second import if the linter requires it.)

- [ ] **Step 3: Wire ChatViewport — hook + descriptor + dep array**

After the `useWorkspace(...)` destructure (around line 173), add:

```tsx
	const {
		subagents,
		loading: subagentsLoading,
		create: addSubagent,
		remove: removeSubagent,
	} = useSubagents();
```

Add a `subagent` descriptor inside the `panels` `useMemo` object (after the `skill` entry, before `permission`):

```tsx
			subagent: {
				title: t('panel.subagent.title'),
				icon: <Bot className="size-4" />,
				content: (
					<SubagentPanel
						subagents={subagents}
						loading={subagentsLoading}
						onAdd={addSubagent}
						onRemove={removeSubagent}
					/>
				),
			},
```

Add these to the `useMemo` dependency array (after `removeSkill`):

```tsx
			subagents,
			subagentsLoading,
			addSubagent,
			removeSubagent,
```

- [ ] **Step 4: Wire ChatViewport — dropdown toggle**

In the panel dropdown (after the `skill` `DropdownMenuCheckboxItem`, before `permission`), add:

```tsx
												<DropdownMenuCheckboxItem
													checked={isPanelOpen('subagent')}
													onCheckedChange={() => togglePanel('subagent')}
													onSelect={(e) => e.preventDefault()}
												>
													<Bot />
													{t('panel.subagent.title')}
												</DropdownMenuCheckboxItem>
```

- [ ] **Step 5: Add i18n keys**

In `examples/web_ui/frontend/src/i18n/locales/en.json`, add a `subagent` block inside `panel` (after `skill`):

```json
		"subagent": {
			"title": "Sub-Agents",
			"description": "CLI sub-agents the agent can delegate tasks to.",
			"searchPlaceholder": "Search sub-agents",
			"add": "Add sub-agent",
			"entity": "sub-agent",
			"emptyTitle": "No sub-agents",
			"emptyDescription": "Add a CLI sub-agent (e.g. Claude Code) to delegate tasks to."
		}
```

And a top-level `dialog-subagent-add` block (next to `dialog-skill-add`):

```json
	"dialog-subagent-add": {
		"title": "Add sub-agent",
		"description": "Register a CLI command the agent can delegate tasks to.",
		"nameLabel": "Name",
		"descLabel": "Description",
		"descPlaceholder": "When should the agent use this sub-agent?",
		"commandLabel": "Command",
		"promptHint": "Use {prompt} where the delegated task should be inserted.",
		"cwdLabel": "Working directory (optional)",
		"timeoutLabel": "Timeout (s)"
	}
```

Mirror both blocks in `examples/web_ui/frontend/src/i18n/locales/zh.json` with Chinese translations:

```json
		"subagent": {
			"title": "子智能体",
			"description": "智能体可委派任务的命令行子智能体。",
			"searchPlaceholder": "搜索子智能体",
			"add": "添加子智能体",
			"entity": "子智能体",
			"emptyTitle": "暂无子智能体",
			"emptyDescription": "添加一个命令行子智能体（如 Claude Code）以委派任务。"
		}
```

```json
	"dialog-subagent-add": {
		"title": "添加子智能体",
		"description": "注册一个智能体可委派任务的命令行命令。",
		"nameLabel": "名称",
		"descLabel": "描述",
		"descPlaceholder": "智能体在什么情况下应使用该子智能体？",
		"commandLabel": "命令",
		"promptHint": "使用 {prompt} 标记委派任务应插入的位置。",
		"cwdLabel": "工作目录（可选）",
		"timeoutLabel": "超时（秒）"
	}
```

- [ ] **Step 6: Typecheck / build**

Run: `pnpm -C examples/web_ui/frontend exec tsc --noEmit`
Expected: no errors. (The `Record<PanelKey, PanelDescriptor>` type forces the new `subagent` descriptor to exist — this is the compile-time safety net.)

Then validate the JSON files parse:
Run: `python -c "import json; json.load(open('examples/web_ui/frontend/src/i18n/locales/en.json')); json.load(open('examples/web_ui/frontend/src/i18n/locales/zh.json')); print('json ok')"`
Expected: `json ok`

- [ ] **Step 7: Commit**

```bash
git add examples/web_ui/frontend/src/components/panel/PanelDock.tsx examples/web_ui/frontend/src/pages/chat/ChatViewport.tsx examples/web_ui/frontend/src/i18n/locales/en.json examples/web_ui/frontend/src/i18n/locales/zh.json
git commit -m "feat(webui): register Sub-Agents panel and i18n"
```

---

### Task 13: End-to-end manual verification

**Files:** none (verification only).

- [ ] **Step 1: Boot the stack** (per `MEMORY.md` → ravenx-webui-run-setup)

Redis on :6379, agent service on :8001, web UI dev server. Confirm the service starts without errors.

- [ ] **Step 2: Register a sub-agent via REST**

Run:
```bash
curl -s -X POST http://localhost:8001/subagent/ \
  -H 'X-User-ID: demo' -H 'Content-Type: application/json' \
  -d '{"data":{"type":"cli_subagent","name":"claude_code","description":"Delegate a coding task to Claude Code.","command":"claude -p {prompt} --dangerously-skip-permissions"}}'
echo
curl -s http://localhost:8001/subagent/ -H 'X-User-ID: demo'
```
Expected: the POST returns `{"subagent_id":"..."}`; the GET returns `total: 1` with the config in `data`.

- [ ] **Step 3: Verify in the UI**

Open the web UI, open the right-sidebar panel switcher, enable **Sub-Agents**. Confirm the `claude_code` entry appears; confirm the **Add sub-agent** dialog creates a new entry and it shows up; confirm delete removes it.

- [ ] **Step 4: Verify the tool attaches**

Start a chat session for user `demo` and confirm the agent's available tools include `claude_code` (e.g. ask the agent "what tools do you have?" or inspect a tool-call). If `claude` is installed on the host, ask the agent to delegate a trivial task and confirm the sub-agent runs and returns output.

- [ ] **Step 5: No commit** (verification only). Record results in the PR description.

---

## Self-Review

**1. Spec coverage:**
- §4.1 ToolBase-not-MCP → Task 3 (`CliSubAgentTool` shells out). ✓
- §4.2 extra_agent_tools hook, no core changes → Task 6 + Task 7. ✓
- §4.3 subagent package (base/factory/tool/agent_tools/init) → Tasks 1,2,3,6. ✓
- §4.3 command substitution (per-token, embedded), env prefix, timeout 600, ALLOW, concurrency=False → Task 3 (tests cover all). ✓
- §4.4 storage (record, KeyConfig keys, StorageBase, RedisStorage) → Task 4. ✓
- §4.5 REST (schemas + router + registration) → Task 5. ✓
- §4.6 wiring → Task 7. ✓
- §4.7 frontend (types, api, hook, panel, dialog, PanelDock, ChatViewport, i18n) → Tasks 8-12. ✓
- §5 data flow → Task 13 (E2E). ✓
- §6 error handling (bad command 422 + skip, non-zero/timeout ERROR, truncation) → Tasks 3,5,6. ✓
- §7 security (single argv token, workspace backend) → Task 3, Task 6. ✓
- §8 testing → each backend task is TDD; frontend uses typecheck. ✓

**2. Placeholder scan:** No "TBD"/"handle edge cases"/"similar to Task N". The one embedded prose NOTE (Task 7) gives the exact refactored code block. ✓

**3. Type consistency:** `SubAgentConfigBase`/`CliSubAgentConfig`, `SubAgentFactory.from_dict`, `SubAgentRecord{user_id,data}`, storage `upsert_subagent(user_id, config)`/`list_subagents`/`get_subagent`/`delete_subagent`, `make_subagent_tool_factory(storage, workspace_manager)`, `CliSubAgentTool(name, description, command, cwd, env, timeout, backend)`, FE `subagentApi.{list,create,update,delete}` and `SubAgentView{id,data,created_at,updated_at}` are used identically across tasks. ✓
