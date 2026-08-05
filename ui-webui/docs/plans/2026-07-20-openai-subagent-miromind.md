# OpenAI-API sub-agent type (MiroMind mirothinker deep-research) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a third sub-agent transport — invoked over an OpenAI-compatible HTTP Chat Completions API — alongside the existing CLI-invoked Claude Code / Codex, and ship MiroMind `mirothinker-1-7-deepresearch` as its first preset (backend + `/subagents` frontend).

**Architecture:** A new `OpenAISubAgentConfig` (`type="openai_subagent"`) plus a sibling `OpenAISubAgentTool` that POSTs a Chat Completions request instead of running a subprocess. The API key comes from the existing per-user `OpenAICompatibleCredential` store (never stored in the config/UI); the tool is stateful via a per-instance `messages[]` history file replayed on resume; it returns the answer plus `search_results` citations + `usage` as metadata. It is purely additive — the CLI path is untouched — and reuses the instance registry, DAG runner, and factory wiring.

**Tech Stack:** Python 3.11+, Pydantic v2, `httpx` (core dep), `pytest`/`unittest` (`IsolatedAsyncioTestCase`), React + TypeScript + Vite (web UI).

**Design doc:** `docs/superpowers/specs/2026-07-20-openai-subagent-miromind-design.md`

## Global Constraints

- **Python env:** the project's `ravenx` conda env is on `PATH` (`.../Miniconda3/envs/ravenx/bin/python`). Run tests with `python -m pytest …` from the repo root. File tools are sandboxed to the repo root.
- **Encapsulation:** all new implementation files/classes are `_`-prefixed; public surface is re-exported via `__init__.py` only.
- **Lazy imports:** import `httpx` at its point of use (inside the function that calls it), never at module top — even though `httpx` is a core dependency, this matches the subagent module's convention.
- **Docstrings:** English only, `Args:`/`Returns:` template with backtick-typed params.
- **Style:** black line length 79; the code below is already wrapped to fit.
- **Tests:** assert whole data structures (`model_dump()` / full dict), not field-by-field; use `AnyString()` / `AnyValue()` from `tests/utils.py` for nondeterministic fields (minted `agent_id`, temp file paths).
- **Commits:** Conventional Commits — `feat(subagent): …`, `feat(webui): …`, `test(subagent): …`.
- **Truncation invariant (copied verbatim from the CLI tool):** `_MAX_OUTPUT_CHARS = 128000`; the full reply is written to `output_file` untruncated, and only the in-context returned text is truncated with the suffix `"\n... (output truncated)"`.

---

### Task 1: `OpenAISubAgentConfig` config class + validators

**Files:**
- Modify: `src/agentscope/subagent/_base.py` (append a new class after `CliSubAgentConfig`)
- Test: `tests/subagent_openai_config_test.py` (create)

**Interfaces:**
- Produces: `OpenAISubAgentConfig(SubAgentConfigBase)` with fields
  `type: Literal["openai_subagent"]`, `name: str`, `description: str`,
  `credential_id: str`, `model: str`, `stateful: bool = True`,
  `system_prompt: str | None = None`, `temperature: float | None = None`,
  `max_tokens: int | None = None`, `timeout: int = 1200`.

- [ ] **Step 1: Write the failing test**

Create `tests/subagent_openai_config_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for OpenAISubAgentConfig validation."""

import unittest

from pydantic import ValidationError

from agentscope.subagent._base import OpenAISubAgentConfig


class OpenAISubAgentConfigTest(unittest.TestCase):
    """Validate the OpenAI-API sub-agent config schema."""

    def _valid(self, **overrides: object) -> dict:
        """Return a minimal valid payload with optional overrides."""
        data = {
            "type": "openai_subagent",
            "name": "miro_deepresearch",
            "description": "Delegate a deep-research question.",
            "credential_id": "cred-1",
            "model": "mirothinker-1-7-deepresearch",
        }
        data.update(overrides)
        return data

    def test_minimal_valid_config_defaults(self) -> None:
        """A minimal config validates and applies the documented defaults."""
        cfg = OpenAISubAgentConfig(**self._valid())
        self.assertEqual(cfg.type, "openai_subagent")
        self.assertTrue(cfg.stateful)
        self.assertIsNone(cfg.system_prompt)
        self.assertIsNone(cfg.temperature)
        self.assertIsNone(cfg.max_tokens)
        self.assertEqual(cfg.timeout, 1200)

    def test_invalid_name_rejected(self) -> None:
        """A name with spaces violates the ^[A-Za-z0-9_-]+$ rule."""
        with self.assertRaises(ValidationError):
            OpenAISubAgentConfig(**self._valid(name="bad name"))

    def test_empty_credential_id_rejected(self) -> None:
        """A blank credential_id is rejected."""
        with self.assertRaises(ValidationError):
            OpenAISubAgentConfig(**self._valid(credential_id="   "))

    def test_empty_model_rejected(self) -> None:
        """A blank model is rejected."""
        with self.assertRaises(ValidationError):
            OpenAISubAgentConfig(**self._valid(model=""))

    def test_temperature_out_of_range_rejected(self) -> None:
        """temperature must be within [0.0, 2.0]."""
        with self.assertRaises(ValidationError):
            OpenAISubAgentConfig(**self._valid(temperature=2.5))

    def test_non_positive_max_tokens_rejected(self) -> None:
        """max_tokens must be > 0 when provided."""
        with self.assertRaises(ValidationError):
            OpenAISubAgentConfig(**self._valid(max_tokens=0))

    def test_non_positive_timeout_rejected(self) -> None:
        """timeout must be > 0."""
        with self.assertRaises(ValidationError):
            OpenAISubAgentConfig(**self._valid(timeout=0))

    def test_credential_id_and_model_are_stripped(self) -> None:
        """Surrounding whitespace is trimmed on the referenced fields."""
        cfg = OpenAISubAgentConfig(
            **self._valid(credential_id=" cred-1 ", model=" m "),
        )
        self.assertEqual(cfg.credential_id, "cred-1")
        self.assertEqual(cfg.model, "m")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/subagent_openai_config_test.py -v`
Expected: FAIL — `ImportError: cannot import name 'OpenAISubAgentConfig'`.

- [ ] **Step 3: Write the implementation**

In `src/agentscope/subagent/_base.py`, append this class at the end of the file (after `CliSubAgentConfig`; `ConfigDict`, `Field`, `field_validator`, `Literal` are already imported at the top):

```python
class OpenAISubAgentConfig(SubAgentConfigBase):
    """A sub-agent invoked over an OpenAI-compatible Chat Completions API.

    Unlike :class:`CliSubAgentConfig` (which runs a subprocess), this
    delegates by POSTing an OpenAI Chat Completions request to a hosted
    endpoint. The endpoint + API key are supplied by a stored
    :class:`~agentscope.credential.OpenAICompatibleCredential` referenced
    by ``credential_id`` (nothing sensitive is stored on the config).
    """

    model_config = ConfigDict(title="OpenAI-API Sub-Agent")

    type: Literal["openai_subagent"] = "openai_subagent"
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

    credential_id: str = Field(
        description=(
            "Id of a stored OpenAI-compatible credential supplying the "
            "endpoint base URL and API key."
        ),
    )
    """Reference to an ``OpenAICompatibleCredential``."""

    model: str = Field(
        description=(
            "The model id to request, e.g. 'mirothinker-1-7-deepresearch'."
        ),
    )
    """The chat completions model id."""

    stateful: bool = Field(
        default=True,
        description=(
            "When true, the per-instance message history is persisted and "
            "replayed on resume (the endpoint itself is stateless). When "
            "false, every call is an independent one-shot request."
        ),
    )
    """Whether instances carry replayed conversation history."""

    system_prompt: str | None = Field(
        default=None,
        description="Optional system message prepended to the conversation.",
    )
    """The optional system prompt."""

    temperature: float | None = Field(
        default=None,
        ge=0.0,
        le=2.0,
        description="Optional sampling temperature in [0.0, 2.0].",
    )
    """The optional sampling temperature."""

    max_tokens: int | None = Field(
        default=None,
        gt=0,
        description="Optional maximum completion tokens.",
    )
    """The optional completion-token cap."""

    timeout: int = Field(
        default=1200,
        gt=0,
        description="Maximum seconds to wait for the API response.",
    )
    """The request timeout in seconds."""

    @field_validator("credential_id", "model")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        """Reject blank references and strip surrounding whitespace.

        Args:
            value (`str`):
                The ``credential_id`` or ``model`` value.

        Returns:
            `str`:
                The stripped, non-empty value.
        """
        if not value.strip():
            raise ValueError("must not be empty")
        return value.strip()
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/subagent_openai_config_test.py -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_base.py tests/subagent_openai_config_test.py
git commit -m "feat(subagent): add OpenAISubAgentConfig schema + validators"
```

---

### Task 2: Register the type in `SubAgentFactory` + export it

**Files:**
- Modify: `src/agentscope/subagent/_factory.py:6,17-19`
- Modify: `src/agentscope/subagent/__init__.py`
- Test: `tests/subagent_factory_test.py` (append a test class)

**Interfaces:**
- Consumes: `OpenAISubAgentConfig` (Task 1).
- Produces: `SubAgentFactory.from_dict({"type": "openai_subagent", …})`
  returns an `OpenAISubAgentConfig`; `agentscope.subagent.OpenAISubAgentConfig`
  is importable.

- [ ] **Step 1: Write the failing test**

Append to `tests/subagent_factory_test.py`:

```python
class OpenAISubAgentFactoryTest(unittest.TestCase):
    """The factory round-trips the openai_subagent type."""

    def test_from_dict_returns_openai_config(self) -> None:
        """A dict with type=openai_subagent deserializes to the class."""
        from agentscope.subagent import OpenAISubAgentConfig
        from agentscope.subagent._factory import SubAgentFactory

        cfg = SubAgentFactory.from_dict(
            {
                "type": "openai_subagent",
                "name": "miro",
                "description": "d",
                "credential_id": "c1",
                "model": "mirothinker-1-7-deepresearch",
            },
        )
        self.assertIsInstance(cfg, OpenAISubAgentConfig)

    def test_schemas_include_openai_subagent(self) -> None:
        """list_schemas exposes the openai_subagent discriminator."""
        from agentscope.subagent._factory import SubAgentFactory

        titles = {
            (s.get("properties", {}).get("type", {}) or {}).get("const")
            or (s.get("properties", {}).get("type", {}) or {}).get("default")
            for s in SubAgentFactory.list_schemas()
        }
        self.assertIn("openai_subagent", titles)
```

> Note: `unittest` is already imported at the top of the existing test file.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/subagent_factory_test.py::OpenAISubAgentFactoryTest -v`
Expected: FAIL — `ImportError` / `Unknown sub-agent type: 'openai_subagent'`.

- [ ] **Step 3: Register the class**

In `src/agentscope/subagent/_factory.py`, update the import on line 6 and the built-in list on lines 17-19:

```python
from ._base import (
    SubAgentConfigBase,
    CliSubAgentConfig,
    OpenAISubAgentConfig,
)
```

```python
    _classes: list[Type[SubAgentConfigBase]] = [
        CliSubAgentConfig,
        OpenAISubAgentConfig,
    ]
```

- [ ] **Step 4: Export from the package**

In `src/agentscope/subagent/__init__.py`, update the `_base` import and `__all__`:

```python
from ._base import (
    SubAgentConfigBase,
    CliSubAgentConfig,
    OpenAISubAgentConfig,
)
```

Add `"OpenAISubAgentConfig",` to the `__all__` list (immediately after `"CliSubAgentConfig",`).

- [ ] **Step 5: Run test to verify it passes**

Run: `python -m pytest tests/subagent_factory_test.py -v`
Expected: PASS (existing tests + 2 new).

- [ ] **Step 6: Commit**

```bash
git add src/agentscope/subagent/_factory.py src/agentscope/subagent/__init__.py tests/subagent_factory_test.py
git commit -m "feat(subagent): register + export OpenAISubAgentConfig"
```

---

### Task 3: `OpenAISubAgentTool` (stateless + stateful, HTTP transport)

**Files:**
- Create: `src/agentscope/subagent/_openai_tool.py`
- Test: `tests/subagent_openai_tool_test.py` (create)

**Interfaces:**
- Consumes: `SessionInstanceRegistry`-like objects (`lookup(handle) ->
  str | None`, `commit(handle, agent_id, name)`); a `BackendBase`-like
  object (`join_path`, `read_file`, `write_file`).
- Produces: `OpenAISubAgentTool(name, description, model, base_url, api_key,
  organization=None, stateful=True, system_prompt=None, temperature=None,
  max_tokens=None, timeout=1200, backend=None, cwd=None, registry=None,
  post=None, middlewares=None)`. Injectable `post` has signature
  `async (url: str, headers: dict, json_body: dict, timeout: float) ->
  tuple[int, dict]`. `call(prompt, instance=None, prompt_file=None,
  output_file=None)` yields one terminal `ToolChunk`. On success:
  `state=ToolResultState.RUNNING`, `content[0].text` = the (truncated) reply,
  `metadata = {"model", "citations", "usage"[, "instance", "agent_id",
  "action"]}`. Module constant `_MAX_OUTPUT_CHARS = 128000`.

> This task is one cohesive class. Steps 1-2 write the full test suite;
> Step 3 is a single larger "write the module" step (the class is ~230
> lines shown in full below); Steps 4-5 verify and commit.

- [ ] **Step 1: Write the failing tests**

Create `tests/subagent_openai_tool_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for the OpenAI-compatible HTTP sub-agent tool."""

import asyncio
import json
import posixpath
import unittest
from typing import AsyncGenerator

from agentscope.message import ToolResultState
from agentscope.permission import PermissionBehavior, PermissionContext
from agentscope.subagent._openai_tool import (
    OpenAISubAgentTool,
    _MAX_OUTPUT_CHARS,
)
from agentscope.tool import ToolChunk
from tests.utils import AnyString, AnyValue


def _ok_response(content: str = "ANSWER") -> dict:
    """Return a canned OpenAI-compatible success body with extensions."""
    return {
        "id": "chatcmpl-abc",
        "object": "chat.completion",
        "model": "mirothinker-1-7-deepresearch",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            },
        ],
        "search_results": [{"title": "T", "url": "u", "snippet": "s"}],
        "usage": {
            "prompt_tokens": 12,
            "completion_tokens": 34,
            "total_tokens": 46,
            "completion_tokens_details": {"reasoning_tokens": 7},
            "num_search_queries": 2,
        },
    }


class _FakeBackend:
    """In-memory backend recording writes; reads what was written."""

    def __init__(self) -> None:
        self.written: dict[str, bytes] = {}

    def join_path(self, *parts: str) -> str:
        """Join path parts (posix)."""
        return posixpath.join(*parts)

    async def read_file(self, path: str) -> bytes:
        """Return the previously written payload (KeyError if absent)."""
        return self.written[path]

    async def write_file(self, path: str, data: bytes) -> None:
        """Record the written payload by path."""
        self.written[path] = data


class _RecordingPost:
    """A fake ``post`` recording calls and returning a queued response."""

    def __init__(
        self,
        responses: list[tuple[int, dict]] | None = None,
        exc: Exception | None = None,
    ) -> None:
        self.calls: list[dict] = []
        self._responses = responses or [(200, _ok_response())]
        self._exc = exc
        self._i = 0

    async def __call__(
        self,
        url: str,
        headers: dict,
        json_body: dict,
        timeout: float,
    ) -> tuple[int, dict]:
        """Record the request; raise or return the next queued response."""
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "json": json_body,
                "timeout": timeout,
            },
        )
        if self._exc is not None:
            raise self._exc
        resp = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return resp


class _MemRegistry:
    """Registry stub with in-memory lookup/commit."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def lookup(self, handle: str) -> str | None:
        """Return the committed agent_id or None."""
        return self.store.get(handle)

    async def commit(
        self,
        handle: str,
        agent_id: str,
        prototype_name: str,
    ) -> None:
        """Persist handle -> agent_id."""
        self.store[handle] = agent_id


class _RaisingLookupRegistry:
    """Registry whose lookup always raises; records commits."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def lookup(self, handle: str) -> str | None:
        """Simulate a transient backend failure."""
        raise RuntimeError("redis down")

    async def commit(
        self,
        handle: str,
        agent_id: str,
        prototype_name: str,
    ) -> None:
        """Record a commit (must not be called in the fail-closed test)."""
        self.store[handle] = agent_id


async def _collect(
    gen: AsyncGenerator[ToolChunk, None],
) -> list[ToolChunk]:
    """Drain an async generator to a list."""
    return [chunk async for chunk in gen]


def _tool(post: object, **kw: object) -> OpenAISubAgentTool:
    """Build a tool with sensible test defaults."""
    return OpenAISubAgentTool(
        name="miro",
        description="Deep research.",
        model="mirothinker-1-7-deepresearch",
        base_url="https://api.miromind.ai/v1",
        api_key="sk_live_x",
        backend=_FakeBackend(),
        cwd="/work",
        post=post,
        **kw,
    )


class OpenAISubAgentToolStatelessTest(unittest.TestCase):
    """Stateless request-building, parsing, and metadata."""

    def test_max_output_chars_is_128k(self) -> None:
        """The in-context return cap matches the CLI tool."""
        self.assertEqual(_MAX_OUTPUT_CHARS, 128000)

    def test_schema_is_prompt_only_when_stateless(self) -> None:
        """No registry => stateless => prompt-only schema."""
        tool = _tool(_RecordingPost())
        self.assertEqual(tool.input_schema["required"], ["prompt"])
        self.assertNotIn("instance", tool.input_schema["properties"])
        self.assertFalse(tool.is_stateful)

    def test_request_shape_and_success_metadata(self) -> None:
        """The POST carries the right url/headers/body; reply+meta parse."""
        post = _RecordingPost()
        tool = _tool(post, stateful=False, registry=_MemRegistry())
        chunks = asyncio.run(_collect(tool.call(prompt="find AI trends")))

        self.assertEqual(len(post.calls), 1)
        call = post.calls[0]
        self.assertEqual(
            call["url"],
            "https://api.miromind.ai/v1/chat/completions",
        )
        self.assertEqual(call["headers"]["Authorization"], "Bearer sk_live_x")
        self.assertEqual(
            call["json"],
            {
                "model": "mirothinker-1-7-deepresearch",
                "messages": [{"role": "user", "content": "find AI trends"}],
                "stream": False,
            },
        )
        self.assertEqual(chunks[-1].state, ToolResultState.RUNNING)
        self.assertEqual(chunks[-1].content[0].text, "ANSWER")
        self.assertEqual(
            chunks[-1].metadata,
            {
                "model": "mirothinker-1-7-deepresearch",
                "citations": [{"title": "T", "url": "u", "snippet": "s"}],
                "usage": {
                    "reasoning_tokens": 7,
                    "num_search_queries": 2,
                    "total_tokens": 46,
                },
            },
        )

    def test_system_prompt_and_optional_params_are_sent(self) -> None:
        """system_prompt seeds messages; temperature/max_tokens forwarded."""
        post = _RecordingPost()
        tool = _tool(
            post,
            stateful=False,
            system_prompt="You are a researcher.",
            temperature=0.5,
            max_tokens=1024,
        )
        asyncio.run(_collect(tool.call(prompt="q")))
        self.assertEqual(
            post.calls[0]["json"],
            {
                "model": "mirothinker-1-7-deepresearch",
                "messages": [
                    {"role": "system", "content": "You are a researcher."},
                    {"role": "user", "content": "q"},
                ],
                "stream": False,
                "temperature": 0.5,
                "max_tokens": 1024,
            },
        )

    def test_prompt_file_contents_become_user_message(self) -> None:
        """A supplied prompt_file is read; its contents fill the prompt."""
        post = _RecordingPost()
        backend = _FakeBackend()
        backend.written["/w/p.md"] = b"FILE PROMPT"
        tool = OpenAISubAgentTool(
            name="miro",
            description="d",
            model="m",
            base_url="https://h/v1",
            api_key="k",
            backend=backend,
            post=post,
        )
        asyncio.run(
            _collect(tool.call(prompt="ignored", prompt_file="/w/p.md")),
        )
        self.assertEqual(
            post.calls[0]["json"]["messages"],
            [{"role": "user", "content": "FILE PROMPT"}],
        )

    def test_output_file_full_write_and_truncated_return(self) -> None:
        """output_file gets the full reply; the returned text is capped."""
        big = "X" * 130000
        post = _RecordingPost(responses=[(200, _ok_response(content=big))])
        backend = _FakeBackend()
        backend.written["/w/p.md"] = b"p"  # seed the prompt_file to read
        tool = OpenAISubAgentTool(
            name="miro",
            description="d",
            model="m",
            base_url="https://h/v1",
            api_key="k",
            backend=backend,
            post=post,
        )
        chunks = asyncio.run(
            _collect(
                tool.call(
                    prompt="ignored",
                    prompt_file="/w/p.md",
                    output_file="/w/out.md",
                ),
            ),
        )
        self.assertEqual(backend.written["/w/out.md"], big.encode("utf-8"))
        self.assertEqual(
            chunks[-1].content[0].text,
            "X" * 128000 + "\n... (output truncated)",
        )

    def test_http_exception_is_error_chunk(self) -> None:
        """A transport exception yields an ERROR chunk, no crash."""
        post = _RecordingPost(exc=RuntimeError("connreset"))
        tool = _tool(post)
        chunks = asyncio.run(_collect(tool.call(prompt="q")))
        self.assertEqual(chunks[-1].state, ToolResultState.ERROR)
        self.assertIn("connreset", chunks[-1].content[0].text)

    def test_non_2xx_error_envelope_is_error_chunk(self) -> None:
        """A 401 with an error envelope surfaces code + message."""
        body = {
            "error": {
                "code": "unauthorized",
                "message": "Invalid API key provided.",
                "type": "authentication_error",
            },
        }
        post = _RecordingPost(responses=[(401, body)])
        tool = _tool(post)
        chunks = asyncio.run(_collect(tool.call(prompt="q")))
        self.assertEqual(chunks[-1].state, ToolResultState.ERROR)
        self.assertIn("401", chunks[-1].content[0].text)
        self.assertIn("Invalid API key", chunks[-1].content[0].text)

    def test_finish_reason_error_is_error_chunk(self) -> None:
        """finish_reason=error yields an ERROR chunk."""
        body = _ok_response()
        body["choices"][0]["finish_reason"] = "error"
        post = _RecordingPost(responses=[(200, body)])
        tool = _tool(post)
        chunks = asyncio.run(_collect(tool.call(prompt="q")))
        self.assertEqual(chunks[-1].state, ToolResultState.ERROR)

    def test_malformed_response_is_error_chunk(self) -> None:
        """A body without choices yields an ERROR chunk, not an exception."""
        post = _RecordingPost(responses=[(200, {"object": "x"})])
        tool = _tool(post)
        chunks = asyncio.run(_collect(tool.call(prompt="q")))
        self.assertEqual(chunks[-1].state, ToolResultState.ERROR)

    def test_check_permissions_allows(self) -> None:
        """Auto-run: permissions always ALLOW."""
        tool = _tool(_RecordingPost())
        decision = asyncio.run(
            tool.check_permissions({"prompt": "q"}, PermissionContext()),
        )
        self.assertEqual(decision.behavior, PermissionBehavior.ALLOW)


class OpenAISubAgentToolStatefulTest(unittest.TestCase):
    """Stateful create/resume, history replay, and deferred commit."""

    def _tool(
        self,
        post: object,
        backend: _FakeBackend,
        registry: object,
    ) -> OpenAISubAgentTool:
        """Build a stateful tool sharing one backend + registry."""
        return OpenAISubAgentTool(
            name="miro",
            description="d",
            model="mirothinker-1-7-deepresearch",
            base_url="https://h/v1",
            api_key="k",
            stateful=True,
            backend=backend,
            cwd="/work",
            registry=registry,
            post=post,
        )

    def test_schema_requires_instance_when_stateful(self) -> None:
        """A stateful tool exposes a required instance handle."""
        tool = self._tool(_RecordingPost(), _FakeBackend(), _MemRegistry())
        self.assertTrue(tool.is_stateful)
        self.assertEqual(
            sorted(tool.input_schema["required"]),
            ["instance", "prompt"],
        )

    def test_create_then_resume_replays_history_and_commits(self) -> None:
        """Create persists history + commits; resume replays prior turns."""
        post = _RecordingPost(
            responses=[
                (200, _ok_response(content="FIRST")),
                (200, _ok_response(content="SECOND")),
            ],
        )
        backend = _FakeBackend()
        registry = _MemRegistry()
        tool = self._tool(post, backend, registry)

        first = asyncio.run(
            _collect(tool.call(prompt="draft", instance="researcher")),
        )
        aid = first[-1].metadata["agent_id"]
        second = asyncio.run(
            _collect(tool.call(prompt="revise", instance="researcher")),
        )

        # Create committed handle -> agent_id.
        self.assertEqual(registry.store, {"researcher": aid})
        # First request sent only the user turn.
        self.assertEqual(
            post.calls[0]["json"]["messages"],
            [{"role": "user", "content": "draft"}],
        )
        # Second request replayed the full prior transcript + new user turn.
        self.assertEqual(
            post.calls[1]["json"]["messages"],
            [
                {"role": "user", "content": "draft"},
                {"role": "assistant", "content": "FIRST"},
                {"role": "user", "content": "revise"},
            ],
        )
        # History file holds the full conversation after the resume.
        hist_path = f"/work/.ravenx_openai_{aid}.json"
        self.assertEqual(
            json.loads(backend.written[hist_path].decode("utf-8")),
            [
                {"role": "user", "content": "draft"},
                {"role": "assistant", "content": "FIRST"},
                {"role": "user", "content": "revise"},
                {"role": "assistant", "content": "SECOND"},
            ],
        )
        self.assertEqual(
            first[-1].metadata,
            {
                "model": "mirothinker-1-7-deepresearch",
                "citations": [{"title": "T", "url": "u", "snippet": "s"}],
                "usage": {
                    "reasoning_tokens": 7,
                    "num_search_queries": 2,
                    "total_tokens": 46,
                },
                "instance": "researcher",
                "agent_id": AnyString(),
                "action": "create",
            },
        )
        self.assertEqual(second[-1].metadata["action"], "resume")
        self.assertEqual(second[-1].content[0].text, "SECOND")

    def test_failed_create_is_not_committed(self) -> None:
        """A create that errors must not poison the handle."""
        post = _RecordingPost(responses=[(500, {"error": {"message": "x"}})])
        backend = _FakeBackend()
        registry = _MemRegistry()
        tool = self._tool(post, backend, registry)
        chunks = asyncio.run(
            _collect(tool.call(prompt="x", instance="w")),
        )
        self.assertEqual(chunks[-1].state, ToolResultState.ERROR)
        self.assertEqual(registry.store, {})

    def test_lookup_failure_fails_closed(self) -> None:
        """A lookup error yields ERROR, sends no request, commits nothing."""
        post = _RecordingPost()
        registry = _RaisingLookupRegistry()
        tool = self._tool(post, _FakeBackend(), registry)
        chunks = asyncio.run(
            _collect(tool.call(prompt="x", instance="w")),
        )
        self.assertEqual(chunks[-1].state, ToolResultState.ERROR)
        self.assertEqual(post.calls, [])
        self.assertEqual(registry.store, {})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/subagent_openai_tool_test.py -v`
Expected: FAIL — `ModuleNotFoundError: agentscope.subagent._openai_tool`.

- [ ] **Step 3: Write the module (full class)**

Create `src/agentscope/subagent/_openai_tool.py`:

```python
# -*- coding: utf-8 -*-
"""The OpenAI-compatible HTTP sub-agent tool."""

import json
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, AsyncGenerator, List

from ..message import TextBlock, ToolResultState
from ..permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from ..tool import BackendBase, LocalBackend, ToolBase, ToolChunk
from ..tool import ToolMiddlewareBase

_MAX_OUTPUT_CHARS = 128000

# (url, headers, json_body, timeout) -> (status_code, parsed_json)
HttpPost = Callable[[str, dict, dict, float], Awaitable[tuple[int, dict]]]


async def _default_post(
    url: str,
    headers: dict,
    json_body: dict,
    timeout: float,
) -> tuple[int, dict]:
    """POST ``json_body`` to ``url``; return ``(status, parsed_json)``.

    Args:
        url (`str`):
            The endpoint URL.
        headers (`dict`):
            Request headers.
        json_body (`dict`):
            The JSON request body.
        timeout (`float`):
            Request timeout in seconds.

    Returns:
        `tuple[int, dict]`:
            The HTTP status code and parsed JSON body. A synthetic
            ``{"error": {"message": ...}}`` envelope is returned when the
            response body is not JSON.
    """
    import httpx

    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout)) as client:
        resp = await client.post(url, headers=headers, json=json_body)
        try:
            data = resp.json()
        except Exception:  # noqa: BLE001 - non-JSON body
            data = {"error": {"message": resp.text}}
        return resp.status_code, data


class OpenAISubAgentTool(ToolBase):
    """Delegate a task to a sub-agent behind an OpenAI-compatible API.

    Instead of running a CLI (see :class:`CliSubAgentTool`), this tool
    POSTs an OpenAI Chat Completions request to a configured endpoint and
    returns the assistant's reply. When stateful, the per-instance
    ``messages`` history is persisted under the session workdir and
    replayed on resume (the endpoint itself is stateless). The final
    reply is returned; ``search_results`` (citations) and ``usage`` are
    attached as tool-result metadata.
    """

    is_read_only: bool = False
    is_concurrency_safe: bool = False
    offload_hint_label: str = "subagent_response"
    """Hint label shown when a backgrounded run delivers its result."""
    offload_noun: str = "Sub-Agent"
    """Human-readable noun used in offload notifications."""

    def __init__(
        self,
        name: str,
        description: str,
        model: str,
        base_url: str,
        api_key: str,
        organization: str | None = None,
        stateful: bool = True,
        system_prompt: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: int = 1200,
        backend: BackendBase | None = None,
        cwd: str | None = None,
        registry: Any = None,
        post: HttpPost | None = None,
        middlewares: List[ToolMiddlewareBase] | None = None,
    ) -> None:
        """Initialize the OpenAI-API sub-agent tool.

        Args:
            name (`str`):
                The tool name presented to the agent.
            description (`str`):
                The tool description presented to the agent.
            model (`str`):
                The chat completions model id to request.
            base_url (`str`):
                The OpenAI-compatible endpoint base URL (e.g. ending
                in ``/v1``).
            api_key (`str`):
                The plaintext API key (resolved per turn from the
                credential store).
            organization (`str | None`, optional):
                Optional organization id sent as ``OpenAI-Organization``.
            stateful (`bool`, defaults to `True`):
                When true (and a ``registry`` is set), instances persist
                and replay their message history on resume.
            system_prompt (`str | None`, optional):
                Optional system message prepended to the conversation.
            temperature (`float | None`, optional):
                Optional sampling temperature.
            max_tokens (`int | None`, optional):
                Optional maximum completion tokens.
            timeout (`int`, defaults to `1200`):
                Request timeout in seconds.
            backend (`BackendBase | None`, optional):
                Backend used for prompt/output/history file I/O. Defaults
                to :class:`LocalBackend`.
            cwd (`str | None`, optional):
                Directory for the prompt/history files.
            registry (`Any`, optional):
                A :class:`SessionInstanceRegistry`-like object. Required
                for the tool to be stateful.
            post (`HttpPost | None`, optional):
                Injectable async HTTP POST (for tests). Defaults to a
                real ``httpx`` call.
            middlewares (`List[ToolMiddlewareBase] | None`, optional):
                Tool middlewares wrapping execution.
        """
        super().__init__(middlewares=middlewares)
        self.name = name
        self.description = description
        self._model = model
        self._base_url = base_url
        self._api_key = api_key
        self._organization = organization
        self._stateful = stateful
        self._system_prompt = system_prompt
        self._temperature = temperature
        self._max_tokens = max_tokens
        self._timeout = timeout
        self._backend = backend or LocalBackend()
        self._cwd = cwd
        self._registry = registry
        self._post = post or _default_post
        self.input_schema = self._build_input_schema()

    @property
    def is_stateful(self) -> bool:
        """Whether this tool manages resumable instances."""
        return self._stateful and self._registry is not None

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
                    "A stable handle for this sub-agent instance. Choose a "
                    "short, semantic name that reflects the instance's "
                    "role (e.g. `researcher`, `db-migrator`), not a random "
                    "string. Reuse the same handle to continue the same "
                    "conversation (context is preserved); use a new handle "
                    "to start a fresh instance."
                ),
            }
            required.append("instance")
        return {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    async def _write_temp_prompt(self, prompt: str) -> str:
        """Write ``prompt`` to a temp file under ``cwd`` and return its path.

        Args:
            prompt (`str`):
                The prompt text to persist.

        Returns:
            `str`:
                The written prompt file path (read back to fill the
                user message).
        """
        base = self._cwd or "."
        path = self._backend.join_path(
            base,
            f".ravenx_prompt_{uuid.uuid4().hex}.md",
        )
        await self._backend.write_file(path, prompt.encode("utf-8"))
        return path

    def _history_path(self, agent_id: str) -> str:
        """Return the per-instance history file path.

        Args:
            agent_id (`str`):
                The provisioned conversation id.

        Returns:
            `str`:
                The JSON history file path under ``cwd``.
        """
        base = self._cwd or "."
        return self._backend.join_path(
            base,
            f".ravenx_openai_{agent_id}.json",
        )

    def _seed_history(self) -> list[dict]:
        """Return a fresh message list seeded with the system prompt.

        Returns:
            `list[dict]`:
                ``[{"role": "system", ...}]`` when a system prompt is
                configured, else ``[]``.
        """
        if self._system_prompt:
            return [{"role": "system", "content": self._system_prompt}]
        return []

    async def call(  # type: ignore[override]
        self,
        prompt: str,
        instance: str | None = None,
        prompt_file: str | None = None,
        output_file: str | None = None,
    ) -> AsyncGenerator[ToolChunk, None]:
        """Run the sub-agent over HTTP and yield its result.

        Args:
            prompt (`str`):
                The task to delegate.
            instance (`str | None`, optional):
                The instance handle (stateful only). An unknown/absent
                handle creates; a known handle resumes.
            prompt_file (`str | None`, optional):
                Path to a file holding the prompt. When ``None``,
                ``prompt`` is written to a temp file; the file's contents
                become the user message.
            output_file (`str | None`, optional):
                When set and the call succeeds, the full reply is written
                here (untruncated).

        Yields:
            `ToolChunk`:
                A single terminal chunk with the sub-agent's reply.
        """
        handle: str | None = None
        action: str | None = None
        agent_id: str | None = None
        created = False

        if self.is_stateful:
            handle = instance or uuid.uuid4().hex
            try:
                existing = await self._registry.lookup(handle)
            except Exception as exc:  # noqa: BLE001
                yield ToolChunk(
                    content=[
                        TextBlock(
                            text=(
                                f"Sub-Agent instance lookup failed: {exc}. "
                                "Not starting a new session (would orphan an "
                                "existing one); retry."
                            ),
                        ),
                    ],
                    state=ToolResultState.ERROR,
                    is_last=True,
                )
                return
            if existing is not None:
                agent_id = existing
                action = "resume"
            else:
                created = True
                action = "create"
                agent_id = str(uuid.uuid4())
        else:
            agent_id = str(uuid.uuid4())

        # Materialize + read the prompt (single source of truth + audit).
        try:
            if prompt_file is None:
                prompt_file = await self._write_temp_prompt(prompt)
            prompt_text = (await self._backend.read_file(prompt_file)).decode(
                "utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            yield ToolChunk(
                content=[
                    TextBlock(text=f"Sub-Agent prompt file error: {exc}"),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        # Build the message history.
        if self.is_stateful and action == "resume":
            try:
                raw = await self._backend.read_file(
                    self._history_path(agent_id),
                )
                history = json.loads(raw.decode("utf-8"))
                if not isinstance(history, list):
                    raise ValueError("history is not a list")
            except Exception:  # noqa: BLE001 - missing/corrupt history
                history = self._seed_history()
        else:
            history = self._seed_history()
        history.append({"role": "user", "content": prompt_text})

        # Build the request.
        url = self._base_url.rstrip("/") + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if self._organization:
            headers["OpenAI-Organization"] = self._organization
        body: dict = {
            "model": self._model,
            "messages": history,
            "stream": False,
        }
        if self._temperature is not None:
            body["temperature"] = self._temperature
        if self._max_tokens is not None:
            body["max_tokens"] = self._max_tokens

        try:
            status, data = await self._post(
                url,
                headers,
                body,
                float(self._timeout),
            )
        except Exception as exc:  # noqa: BLE001
            yield ToolChunk(
                content=[TextBlock(text=f"Sub-Agent HTTP error: {exc}")],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        if status < 200 or status >= 300:
            err = data.get("error") if isinstance(data, dict) else None
            code = err.get("code") if isinstance(err, dict) else None
            message = (
                err.get("message") if isinstance(err, dict) else str(data)
            )
            yield ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            f"Sub-Agent HTTP error ({status}/{code}): "
                            f"{message}"
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        # Parse the reply.
        try:
            choice = data["choices"][0]
            content = choice["message"]["content"]
            finish_reason = choice.get("finish_reason")
        except (KeyError, IndexError, TypeError) as exc:
            yield ToolChunk(
                content=[
                    TextBlock(text=f"Sub-Agent malformed response: {exc}"),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        if finish_reason in ("error", "cancelled") or not content:
            yield ToolChunk(
                content=[
                    TextBlock(
                        text=(
                            "Sub-Agent did not complete "
                            f"(finish_reason={finish_reason})."
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        # Success. Persist history + commit a successful create.
        if self.is_stateful:
            history.append({"role": "assistant", "content": content})
            try:
                await self._backend.write_file(
                    self._history_path(agent_id),
                    json.dumps(history).encode("utf-8"),
                )
            except Exception:  # noqa: BLE001 - best-effort persistence
                pass
            if created:
                assert handle is not None
                try:
                    await self._registry.commit(
                        handle,
                        agent_id,
                        self.name,
                    )
                except Exception:  # noqa: BLE001 - best-effort persistence
                    pass

        # Persist the full reply, then truncate the in-context return.
        if output_file is not None:
            await self._backend.write_file(
                output_file,
                content.encode("utf-8"),
            )
        output = content
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + "\n... (output truncated)"

        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        metadata: dict = {
            "model": self._model,
            "citations": data.get("search_results") or [],
            "usage": {
                "reasoning_tokens": details.get("reasoning_tokens"),
                "num_search_queries": usage.get("num_search_queries"),
                "total_tokens": usage.get("total_tokens"),
            },
        }
        if self.is_stateful:
            metadata["instance"] = handle
            metadata["agent_id"] = agent_id
            metadata["action"] = action

        yield ToolChunk(
            content=[TextBlock(text=output)],
            state=ToolResultState.RUNNING,
            is_last=True,
            metadata=metadata,
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

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/subagent_openai_tool_test.py -v`
Expected: PASS (all stateless + stateful tests).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_openai_tool.py tests/subagent_openai_tool_test.py
git commit -m "feat(subagent): add OpenAISubAgentTool (HTTP transport, stateful history)"
```

---

### Task 4: Wire the tool into `make_subagent_tool_factory`

**Files:**
- Modify: `src/agentscope/subagent/_agent_tools.py`
- Modify: `src/agentscope/subagent/__init__.py` (export `OpenAISubAgentTool`)
- Test: `tests/subagent_agent_tools_test.py` (append tests + extend `_FakeStorage`)

**Interfaces:**
- Consumes: `OpenAISubAgentConfig`, `OpenAISubAgentTool`,
  `CredentialFactory.from_dict`, `OpenAICompatibleCredential`,
  `storage.get_credential(user_id, credential_id) -> CredentialRecord | None`.
- Produces: the factory builds an `OpenAISubAgentTool` for each valid
  `openai_subagent` config with a resolvable OpenAI-compatible credential;
  the DAG tool is appended when any sub-agent tools (CLI or OpenAI) exist.

- [ ] **Step 1: Write the failing tests**

Append to `tests/subagent_agent_tools_test.py`. First, extend `_FakeStorage`
to serve credentials — replace its class body with:

```python
class _FakeStorage:
    """Storage returning fixed sub-agent records + credentials."""

    def __init__(
        self,
        records: list[SubAgentRecord],
        credentials: dict[str, dict] | None = None,
    ) -> None:
        self._records = records
        self._credentials = credentials or {}

    async def list_subagents(self, user_id: str) -> list[SubAgentRecord]:
        """Return the fixed records."""
        return self._records

    async def get_session(self, *args: object, **kwargs: object) -> None:
        """No session binding in these tests."""
        return None

    async def get_credential(
        self,
        user_id: str,
        credential_id: str,
    ) -> object:
        """Return a fixed credential record (or None)."""
        data = self._credentials.get(credential_id)
        if data is None:
            return None

        class _Rec:
            """Minimal credential record with a ``data`` attribute."""

        rec = _Rec()
        rec.data = data
        return rec
```

Then append these tests and helpers:

```python
def _openai_record(
    subagent_id: str,
    name: str,
    credential_id: str,
    model: str = "mirothinker-1-7-deepresearch",
) -> SubAgentRecord:
    """Build an openai_subagent record."""
    return SubAgentRecord(
        id=subagent_id,
        user_id="u1",
        data={
            "id": subagent_id,
            "type": "openai_subagent",
            "name": name,
            "description": "d",
            "credential_id": credential_id,
            "model": model,
            "stateful": True,
        },
    )


def _openai_compatible_cred() -> dict:
    """A valid OpenAI-compatible credential payload."""
    return {
        "type": "openai_compatible_credential",
        "name": "miromind",
        "api_key": "sk_live_x",
        "base_url": "https://api.miromind.ai/v1",
    }


class MakeSubAgentToolFactoryOpenAITest(IsolatedAsyncioTestCase):
    """The factory builds OpenAI tools and resolves credentials."""

    async def test_builds_openai_tool_with_resolved_credential(self) -> None:
        """A resolvable credential yields a wired OpenAISubAgentTool."""
        from agentscope.subagent import OpenAISubAgentTool

        backend = _Sentinel()
        storage = _FakeStorage(
            [_openai_record("sa-1", "miro", "cred-1")],
            credentials={"cred-1": _openai_compatible_cred()},
        )
        wm = _FakeWorkspaceManager(_FakeWorkspace(backend, "/ws/dir"))
        factory = make_subagent_tool_factory(storage, wm)
        tools = await factory("u1", "a1", "s1")
        oai = [t for t in tools if isinstance(t, OpenAISubAgentTool)]
        self.assertEqual(len(oai), 1)
        self.assertEqual(oai[0].name, "miro")
        # pylint: disable=protected-access
        self.assertEqual(oai[0]._base_url, "https://api.miromind.ai/v1")
        self.assertEqual(oai[0]._api_key, "sk_live_x")
        self.assertEqual(oai[0]._model, "mirothinker-1-7-deepresearch")
        self.assertEqual(oai[0]._cwd, "/ws/dir")
        self.assertTrue(oai[0].is_stateful)

    async def test_skips_openai_tool_when_credential_missing(self) -> None:
        """A missing credential skips the tool (not fatal)."""
        from agentscope.subagent import OpenAISubAgentTool

        storage = _FakeStorage(
            [_openai_record("sa-1", "miro", "nope")],
            credentials={},
        )
        wm = _FakeWorkspaceManager(None)
        factory = make_subagent_tool_factory(storage, wm)
        tools = await factory("u1", "a1", "s1")
        self.assertFalse(
            any(isinstance(t, OpenAISubAgentTool) for t in tools),
        )

    async def test_skips_openai_tool_when_credential_wrong_type(self) -> None:
        """A non-OpenAI-compatible credential skips the tool."""
        from agentscope.subagent import OpenAISubAgentTool

        storage = _FakeStorage(
            [_openai_record("sa-1", "miro", "cred-1")],
            credentials={
                "cred-1": {
                    "type": "anthropic_credential",
                    "name": "a",
                    "api_key": "sk",
                },
            },
        )
        wm = _FakeWorkspaceManager(None)
        factory = make_subagent_tool_factory(storage, wm)
        tools = await factory("u1", "a1", "s1")
        self.assertFalse(
            any(isinstance(t, OpenAISubAgentTool) for t in tools),
        )

    async def test_dag_tool_appended_for_openai_only(self) -> None:
        """A DAG tool is appended when only OpenAI tools exist."""
        backend = _Sentinel()
        storage = _FakeStorage(
            [_openai_record("sa-1", "miro", "cred-1")],
            credentials={"cred-1": _openai_compatible_cred()},
        )
        wm = _FakeWorkspaceManager(_FakeWorkspace(backend, "/ws/dir"))
        factory = make_subagent_tool_factory(storage, wm)
        tools = await factory("u1", "a1", "s1")
        self.assertIn("run_subagent_dag", [t.name for t in tools])
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `python -m pytest tests/subagent_agent_tools_test.py::MakeSubAgentToolFactoryOpenAITest -v`
Expected: FAIL — the factory does not yet build OpenAI tools (empty `oai`).

- [ ] **Step 3: Update the factory**

In `src/agentscope/subagent/_agent_tools.py`:

Replace the imports on lines 9-11:

```python
from ._base import CliSubAgentConfig, OpenAISubAgentConfig
from ._instance_registry import SessionInstanceRegistry
from ._tool import CliSubAgentTool
from ._openai_tool import OpenAISubAgentTool
from ..credential import CredentialFactory, OpenAICompatibleCredential
```

Replace the loop body (lines 104-122, the `if not isinstance(...)` gate and
the single `CliSubAgentTool` append) with an explicit two-branch build:

```python
            if isinstance(config, CliSubAgentConfig):
                tools.append(
                    CliSubAgentTool(
                        name=config.name,
                        description=config.description,
                        command=config.command,
                        resume_command=config.resume_command,
                        id_source=config.id_source,
                        session_id_pattern=config.session_id_pattern,
                        output_pattern=config.output_pattern,
                        transcript_format=config.transcript_format,
                        cwd=config.cwd or session_workdir,
                        env=config.env,
                        timeout=config.timeout,
                        backend=backend,
                        registry=SessionInstanceRegistry(
                            storage,
                            session_id,
                        ),
                    ),
                )
            elif isinstance(config, OpenAISubAgentConfig):
                cred_rec = await storage.get_credential(
                    user_id,
                    config.credential_id,
                )
                if cred_rec is None:
                    logger.warning(
                        "Skipping openai sub-agent %s: credential %s "
                        "not found",
                        record.id,
                        config.credential_id,
                    )
                    continue
                try:
                    cred = CredentialFactory.from_dict(cred_rec.data)
                except (ValueError, TypeError) as exc:
                    logger.warning(
                        "Skipping openai sub-agent %s: bad credential "
                        "%s: %s",
                        record.id,
                        config.credential_id,
                        exc,
                    )
                    continue
                if not isinstance(cred, OpenAICompatibleCredential):
                    logger.warning(
                        "Skipping openai sub-agent %s: credential %s is "
                        "not OpenAI-compatible",
                        record.id,
                        config.credential_id,
                    )
                    continue
                tools.append(
                    OpenAISubAgentTool(
                        name=config.name,
                        description=config.description,
                        model=config.model,
                        base_url=cred.base_url,
                        api_key=cred.api_key.get_secret_value(),
                        organization=cred.organization,
                        stateful=config.stateful,
                        system_prompt=config.system_prompt,
                        temperature=config.temperature,
                        max_tokens=config.max_tokens,
                        timeout=config.timeout,
                        backend=backend,
                        cwd=session_workdir,
                        registry=SessionInstanceRegistry(
                            storage,
                            session_id,
                        ),
                    ),
                )
            else:
                continue
```

Then generalize the DAG-tool gate (lines 123-131) — rename `cli_tools` to
`subagent_tools`:

```python
        subagent_tools = {t.name: t for t in tools}
        if subagent_tools and backend is not None and session_workdir:
            tools.append(
                SubAgentDagTool(
                    subagents=subagent_tools,
                    backend=backend,
                    workdir=session_workdir,
                ),
            )
```

- [ ] **Step 4: Export the tool**

In `src/agentscope/subagent/__init__.py`, add
`from ._openai_tool import OpenAISubAgentTool` (after the `_tool` import) and
`"OpenAISubAgentTool",` to `__all__` (after `"CliSubAgentTool",`).

- [ ] **Step 5: Run tests to verify they pass**

Run: `python -m pytest tests/subagent_agent_tools_test.py -v`
Expected: PASS (existing CLI tests + 4 new OpenAI tests).

- [ ] **Step 6: Commit**

```bash
git add src/agentscope/subagent/_agent_tools.py src/agentscope/subagent/__init__.py tests/subagent_agent_tools_test.py
git commit -m "feat(subagent): build OpenAISubAgentTool in the tool factory + DAG gate"
```

---

### Task 5: MiroMind flagship preset

**Files:**
- Modify: `src/agentscope/subagent/_presets.py`
- Test: `tests/subagent_presets_test.py` (append a test)

**Interfaces:**
- Produces: `list_subagent_presets()` includes a
  `{"preset_id": "miromind_deepresearch", "label": …, "data": {…}}` entry
  whose `data` is a valid `OpenAISubAgentConfig` payload once a
  `credential_id` is supplied.

- [ ] **Step 1: Write the failing test**

Append to `tests/subagent_presets_test.py`:

```python
class MiroMindPresetTest(unittest.TestCase):
    """The MiroMind flagship preset is present and valid."""

    def test_preset_present_and_round_trips(self) -> None:
        """The preset exists and validates once a credential_id is set."""
        from agentscope.subagent import (
            OpenAISubAgentConfig,
            list_subagent_presets,
        )

        presets = {p["preset_id"]: p for p in list_subagent_presets()}
        self.assertIn("miromind_deepresearch", presets)
        preset = presets["miromind_deepresearch"]
        self.assertEqual(preset["data"]["type"], "openai_subagent")
        self.assertEqual(
            preset["data"]["model"],
            "mirothinker-1-7-deepresearch",
        )
        # Ships with an empty credential_id (user must attach their own).
        self.assertEqual(preset["data"]["credential_id"], "")
        # With a credential_id filled in, it is a valid config.
        data = dict(preset["data"], credential_id="cred-1")
        cfg = OpenAISubAgentConfig(**data)
        self.assertTrue(cfg.stateful)
```

> Note: `unittest` is already imported at the top of the existing test file.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/subagent_presets_test.py::MiroMindPresetTest -v`
Expected: FAIL — `miromind_deepresearch` not in presets.

- [ ] **Step 3: Add the preset**

In `src/agentscope/subagent/_presets.py`, append this dict to the list
returned by `list_subagent_presets()` (after the Codex preset, before the
closing `]`):

```python
        {
            "preset_id": "miromind_deepresearch",
            "label": "MiroThinker Deep Research (MiroMind)",
            "data": {
                "type": "openai_subagent",
                "name": "miro_deepresearch",
                "description": (
                    "Delegate a deep-research question to MiroMind "
                    "mirothinker-1-7-deepresearch (web search, code "
                    "execution, tool use). Returns a sourced report with "
                    "citations. Stateful: reuse an instance handle to "
                    "continue the same research thread. Requires an "
                    "OpenAI-compatible credential (base_url "
                    "https://api.miromind.ai/v1)."
                ),
                "credential_id": "",
                "model": "mirothinker-1-7-deepresearch",
                "stateful": True,
                "system_prompt": None,
                "temperature": None,
                "max_tokens": None,
                "timeout": 1200,
            },
        },
```

Also update the module docstring's first line (line 2) to read: `"""Built-in
sub-agent presets (Claude Code, Codex, MiroMind)."""`

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/subagent_presets_test.py -v`
Expected: PASS (existing presets + MiroMind).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_presets.py tests/subagent_presets_test.py
git commit -m "feat(subagent): add MiroMind mirothinker deep-research preset"
```

---

### Task 6: Frontend — discriminated-union API types

**Files:**
- Modify: `examples/web_ui/frontend/src/api/types.ts:419-432`

**Interfaces:**
- Produces: `SubAgentData = CliSubAgentData | OpenAISubAgentData` (both keyed
  on `type`), consumed by the `/subagents` page (Task 7).

- [ ] **Step 1: Replace the `SubAgentData` interface**

In `examples/web_ui/frontend/src/api/types.ts`, replace the current
`SubAgentData` interface (lines 419-432) with a discriminated union:

```ts
export interface CliSubAgentData {
	type: 'cli_subagent';
	name: string;
	description: string;
	command: string;
	resume_command?: string | null;
	id_source?: 'provisioned' | 'derived';
	session_id_pattern?: string | null;
	output_pattern?: string | null;
	transcript_format?: 'text' | 'codex_jsonl';
	cwd?: string | null;
	env?: Record<string, string> | null;
	timeout?: number;
}

export interface OpenAISubAgentData {
	type: 'openai_subagent';
	name: string;
	description: string;
	credential_id: string;
	model: string;
	stateful?: boolean;
	system_prompt?: string | null;
	temperature?: number | null;
	max_tokens?: number | null;
	timeout?: number;
}

export type SubAgentData = CliSubAgentData | OpenAISubAgentData;
```

- [ ] **Step 2: Verify the types compile**

Run: `pnpm -C examples/web_ui/frontend build`
Expected: `tsc -b` reports errors ONLY in `src/pages/subagent/index.tsx`
(it still assumes the old flat shape — fixed in Task 7). If other files
error, reconcile before proceeding.

> If `pnpm` dependencies are not installed yet, first run
> `pnpm -C examples/web_ui/frontend install`.

- [ ] **Step 3: Commit**

```bash
git add examples/web_ui/frontend/src/api/types.ts
git commit -m "feat(webui): make SubAgentData a cli/openai discriminated union"
```

---

### Task 7: Frontend — `/subagents` page (type selector + OpenAI fields)

**Files:**
- Modify: `examples/web_ui/frontend/src/pages/subagent/index.tsx`

**Interfaces:**
- Consumes: `SubAgentData` union (Task 6), `useCredentials()` (returns
  `{ credentials: CredentialView[] }`; each `CredentialView` has `id` and
  `data` with `type` + `name`), preset `data.type` to route preset add.
- Produces: a page that creates/edits both `cli_subagent` and
  `openai_subagent` prototypes.

- [ ] **Step 1: Extend imports + FormState + EMPTY_FORM**

Add to the imports (near the existing `useSubagents` import):

```tsx
import { useCredentials } from '@/hooks/useCredentials';
```

Replace the `FormState` interface and `EMPTY_FORM` (lines 34-62) with:

```tsx
interface FormState {
	type: 'cli_subagent' | 'openai_subagent';
	name: string;
	description: string;
	// CLI fields
	command: string;
	resume_command: string;
	cwd: string;
	/** One `KEY=VALUE` pair per line; parsed into a record on submit. */
	env: string;
	/** Carried invisibly (set by presets), not rendered as inputs. */
	id_source: 'provisioned' | 'derived';
	session_id_pattern: string;
	output_pattern: string;
	transcript_format: 'text' | 'codex_jsonl';
	// OpenAI fields
	credential_id: string;
	model: string;
	stateful: boolean;
	system_prompt: string;
	temperature: string;
	max_tokens: string;
	// Shared
	timeout: string;
}

const EMPTY_FORM: FormState = {
	type: 'cli_subagent',
	name: '',
	description: '',
	command: '',
	resume_command: '',
	cwd: '',
	env: '',
	id_source: 'provisioned',
	session_id_pattern: '',
	output_pattern: '',
	transcript_format: 'text',
	credential_id: '',
	model: '',
	stateful: true,
	system_prompt: '',
	temperature: '',
	max_tokens: '',
	timeout: '600',
};
```

- [ ] **Step 2: Replace `toForm` and `toPayload`**

Replace `toForm` (lines 85-99) with a type-aware version:

```tsx
function toForm(view: SubAgentView): FormState {
	const d = view.data;
	if (d.type === 'openai_subagent') {
		return {
			...EMPTY_FORM,
			type: 'openai_subagent',
			name: d.name,
			description: d.description,
			credential_id: d.credential_id,
			model: d.model,
			stateful: d.stateful ?? true,
			system_prompt: d.system_prompt ?? '',
			temperature: d.temperature == null ? '' : String(d.temperature),
			max_tokens: d.max_tokens == null ? '' : String(d.max_tokens),
			timeout: String(d.timeout ?? 1200),
		};
	}
	return {
		...EMPTY_FORM,
		type: 'cli_subagent',
		name: d.name,
		description: d.description,
		command: d.command,
		resume_command: d.resume_command ?? '',
		cwd: d.cwd ?? '',
		env: envToText(d.env),
		id_source: d.id_source ?? 'provisioned',
		session_id_pattern: d.session_id_pattern ?? '',
		output_pattern: d.output_pattern ?? '',
		transcript_format: d.transcript_format ?? 'text',
		timeout: String(d.timeout ?? 600),
	};
}
```

Replace `toPayload` (lines 101-116) with a type-aware version:

```tsx
function toPayload(form: FormState): Record<string, unknown> {
	if (form.type === 'openai_subagent') {
		return {
			type: 'openai_subagent',
			name: form.name.trim(),
			description: form.description.trim(),
			credential_id: form.credential_id.trim(),
			model: form.model.trim(),
			stateful: form.stateful,
			system_prompt: form.system_prompt.trim() || null,
			temperature:
				form.temperature.trim() === ''
					? null
					: Number(form.temperature),
			max_tokens:
				form.max_tokens.trim() === '' ? null : Number(form.max_tokens),
			timeout: Number(form.timeout) || 1200,
		};
	}
	return {
		type: 'cli_subagent',
		name: form.name.trim(),
		description: form.description.trim(),
		command: form.command.trim(),
		resume_command: form.resume_command.trim() || null,
		id_source: form.id_source,
		session_id_pattern: form.session_id_pattern.trim() || null,
		output_pattern: form.output_pattern.trim() || null,
		transcript_format: form.transcript_format,
		cwd: form.cwd.trim() || null,
		env: textToEnv(form.env),
		timeout: Number(form.timeout) || 600,
	};
}
```

- [ ] **Step 3: Add the credential list + type-aware validation**

Inside the `SubAgentsPage` component, after the existing
`const { subagents, ... } = useSubagents();` line, add:

```tsx
	const { credentials } = useCredentials();
	const openaiCredentials = credentials.filter(
		(c) => c.data.type === 'openai_compatible_credential',
	);
```

Replace the validation block (lines 171-183, the `stateful`/`derived`/
`cmdOk`/`resumeOk`/`canSubmit` computations) with:

```tsx
	// Mirror the backend's rules. A stateful *provisioned* command needs
	// `{agent_id}`; a *derived* command must not (the CLI mints the id).
	const stateful = form.resume_command.trim().length > 0;
	const derived = form.id_source === 'derived';
	const cmdOk =
		form.command.includes('{prompt}') &&
		(!stateful || derived || form.command.includes('{agent_id}'));
	const resumeOk =
		!stateful ||
		(form.resume_command.includes('{prompt}') &&
			form.resume_command.includes('{agent_id}'));
	const cliOk = cmdOk && resumeOk;
	const openaiOk =
		!!form.credential_id.trim() && !!form.model.trim();
	const canSubmit =
		!!form.name.trim() &&
		!!form.description.trim() &&
		(form.type === 'openai_subagent' ? openaiOk : cliOk);
```

- [ ] **Step 4: Route preset add by type**

Replace `addFromPreset` (lines 160-169) with:

```tsx
	const addFromPreset = async (preset: SubAgentPreset) => {
		// OpenAI presets ship with an empty credential_id; open the
		// pre-filled form so the user attaches a credential before saving.
		if (preset.data.type === 'openai_subagent') {
			setForm(toForm({ id: '', data: preset.data } as SubAgentView));
			setError(null);
			setEditingId('');
			return;
		}
		setAddingPreset(preset.preset_id);
		try {
			await create(preset.data as unknown as Record<string, unknown>);
		} catch (err) {
			toast.error(String((err as Error)?.message ?? err));
		} finally {
			setAddingPreset(null);
		}
	};
```

- [ ] **Step 5: Add the stateful badge for OpenAI prototypes**

In the prototype list, replace the badge condition (line 268,
`{sa.data.resume_command && (`) with:

```tsx
									{(sa.data.type === 'cli_subagent'
										? sa.data.resume_command
										: sa.data.stateful) && (
```

- [ ] **Step 6: Render the type selector + conditional fields**

In the form (right pane), replace the CLI-specific field block — from the
`command` Label/Textarea through the `env` hint `<p>` (lines 323-360) — with
a type selector plus both field groups. Insert the type selector immediately
after the description Textarea (line 321), then the conditional groups:

```tsx
						{editingId === '' && (
							<>
								<Label className="text-xs">
									{t('subagent-sidebar.typeLabel')}
								</Label>
								<select
									className="border rounded px-2 py-1 text-sm bg-background"
									value={form.type}
									onChange={(e) =>
										setForm((f) => ({
											...f,
											type: e.target
												.value as FormState['type'],
										}))
									}
								>
									<option value="cli_subagent">
										{t('subagent-sidebar.typeCli')}
									</option>
									<option value="openai_subagent">
										{t('subagent-sidebar.typeOpenai')}
									</option>
								</select>
							</>
						)}

						{form.type === 'cli_subagent' ? (
							<>
								<Label className="text-xs">
									{t('subagent-sidebar.commandLabel')}
								</Label>
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
								<p
									className={
										stateful && !resumeOk
											? 'text-[11px] text-destructive'
											: 'text-[11px] text-muted-foreground'
									}
								>
									{t('subagent-sidebar.resumeHint')}
								</p>

								<Label className="text-xs">
									{t('subagent-sidebar.cwdLabel')}
								</Label>
								<Input value={form.cwd} onChange={set('cwd')} />

								<Label className="text-xs">
									{t('subagent-sidebar.envLabel')}
								</Label>
								<Textarea
									value={form.env}
									onChange={set('env')}
									rows={2}
									placeholder={'KEY=value'}
								/>
								<p className="text-[11px] text-muted-foreground">
									{t('subagent-sidebar.envHint')}
								</p>
							</>
						) : (
							<>
								<Label className="text-xs">
									{t('subagent-sidebar.credentialLabel')}
								</Label>
								<select
									className="border rounded px-2 py-1 text-sm bg-background"
									value={form.credential_id}
									onChange={set('credential_id')}
								>
									<option value="">
										{t('subagent-sidebar.credentialEmpty')}
									</option>
									{openaiCredentials.map((c) => (
										<option key={c.id} value={c.id}>
											{String(c.data.name ?? c.id)}
										</option>
									))}
								</select>

								<Label className="text-xs">
									{t('subagent-sidebar.modelLabel')}
								</Label>
								<Input
									value={form.model}
									onChange={set('model')}
									placeholder="mirothinker-1-7-deepresearch"
								/>

								<label className="flex items-center gap-2 text-xs">
									<input
										type="checkbox"
										checked={form.stateful}
										onChange={(e) =>
											setForm((f) => ({
												...f,
												stateful: e.target.checked,
											}))
										}
									/>
									{t('subagent-sidebar.statefulLabel')}
								</label>

								<Label className="text-xs">
									{t('subagent-sidebar.systemPromptLabel')}
								</Label>
								<Textarea
									value={form.system_prompt}
									onChange={set('system_prompt')}
									rows={2}
								/>

								<Label className="text-xs">
									{t('subagent-sidebar.temperatureLabel')}
								</Label>
								<Input
									value={form.temperature}
									onChange={set('temperature')}
									inputMode="decimal"
								/>

								<Label className="text-xs">
									{t('subagent-sidebar.maxTokensLabel')}
								</Label>
								<Input
									value={form.max_tokens}
									onChange={set('max_tokens')}
									inputMode="numeric"
								/>
							</>
						)}
```

> The existing `timeout` Label/Input (lines 362-363) stays as-is, below
> this block, shared by both types. The `set` helper handles `<select>`
> because its `onChange` event also exposes `target.value`.

- [ ] **Step 7: Verify the page compiles**

Run: `pnpm -C examples/web_ui/frontend build`
Expected: PASS — `tsc -b && vite build` completes with no type errors.

- [ ] **Step 8: Commit**

```bash
git add examples/web_ui/frontend/src/pages/subagent/index.tsx
git commit -m "feat(webui): support OpenAI-API sub-agents on the /subagents page"
```

---

### Task 8: Frontend — i18n keys (en + zh)

**Files:**
- Modify: `examples/web_ui/frontend/src/i18n/locales/en.json`
- Modify: `examples/web_ui/frontend/src/i18n/locales/zh.json`

**Interfaces:**
- Consumes: the `t('subagent-sidebar.*')` keys referenced in Task 7.
- Produces: `typeLabel`, `typeCli`, `typeOpenai`, `credentialLabel`,
  `credentialEmpty`, `modelLabel`, `statefulLabel`, `systemPromptLabel`,
  `temperatureLabel`, `maxTokensLabel` under `subagent-sidebar`.

- [ ] **Step 1: Add English keys**

In `examples/web_ui/frontend/src/i18n/locales/en.json`, inside the existing
`"subagent-sidebar"` object, add these keys (keep valid JSON — add a comma
after the previous last key):

```json
			"typeLabel": "Type",
			"typeCli": "CLI command",
			"typeOpenai": "OpenAI-compatible API",
			"credentialLabel": "Credential",
			"credentialEmpty": "Select an OpenAI-compatible credential…",
			"modelLabel": "Model",
			"statefulLabel": "Stateful (replay conversation on resume)",
			"systemPromptLabel": "System prompt (optional)",
			"temperatureLabel": "Temperature (optional)",
			"maxTokensLabel": "Max tokens (optional)"
```

- [ ] **Step 2: Add Chinese keys**

In `examples/web_ui/frontend/src/i18n/locales/zh.json`, inside the existing
`"subagent-sidebar"` object, add:

```json
			"typeLabel": "类型",
			"typeCli": "CLI 命令",
			"typeOpenai": "OpenAI 兼容 API",
			"credentialLabel": "凭证",
			"credentialEmpty": "选择一个 OpenAI 兼容凭证…",
			"modelLabel": "模型",
			"statefulLabel": "有状态（恢复时重放对话）",
			"systemPromptLabel": "系统提示（可选）",
			"temperatureLabel": "温度（可选）",
			"maxTokensLabel": "最大 token 数（可选）"
```

- [ ] **Step 3: Verify JSON + build**

Run: `python -m json.tool examples/web_ui/frontend/src/i18n/locales/en.json > /dev/null && python -m json.tool examples/web_ui/frontend/src/i18n/locales/zh.json > /dev/null && echo OK`
Expected: `OK` (both files are valid JSON).

Run: `pnpm -C examples/web_ui/frontend build`
Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add examples/web_ui/frontend/src/i18n/locales/en.json examples/web_ui/frontend/src/i18n/locales/zh.json
git commit -m "feat(webui): i18n for OpenAI-API sub-agent form (en + zh)"
```

---

## Final verification

- [ ] **Run the full sub-agent test suite**

Run: `python -m pytest tests/subagent_openai_config_test.py tests/subagent_openai_tool_test.py tests/subagent_factory_test.py tests/subagent_presets_test.py tests/subagent_agent_tools_test.py -v`
Expected: all PASS.

- [ ] **Run the broader suite to catch regressions**

Run: `python -m pytest tests/ -k subagent -v`
Expected: all PASS (CLI + DAG + OpenAI).

- [ ] **Lint the changed backend files**

Run: `pre-commit run --files src/agentscope/subagent/_base.py src/agentscope/subagent/_openai_tool.py src/agentscope/subagent/_factory.py src/agentscope/subagent/_agent_tools.py src/agentscope/subagent/_presets.py src/agentscope/subagent/__init__.py`
Expected: hooks pass (black/flake8/pylint/mypy/docstrings). Fix any issues.

- [ ] **Frontend build**

Run: `pnpm -C examples/web_ui/frontend build`
Expected: PASS.

## Spec coverage (self-review)

- Spec §4 config → Task 1. §5 factory registration → Task 2. §6 tool
  (create/resume, history, HTTP, parsing, metadata, truncation) → Task 3.
  §7 factory wiring + DAG gate → Task 4. §8 preset → Task 5. §9.1 TS types
  → Task 6. §9.2-9.5 page + validation + preset UX + badge → Task 7. §10
  tests → Tasks 1,3,4,5. i18n (§9.5) → Task 8. §11 files-touched all covered.
- Deferred by design (spec §2 out-of-scope): SSE streaming, full
  `reasoning_steps` trace, `-mini` preset, `mcp_servers`/`response_format`
  pass-through. Not planned — intentional.
