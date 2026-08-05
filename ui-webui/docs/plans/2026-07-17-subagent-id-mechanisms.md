# Per-Agent Sub-Agent ID Provisioning Mechanisms + Presets — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Codify two CLI sub-agent id-provisioning strategies — `provisioned` (caller mints the id, Claude Code) and `derived` (the CLI mints it and RavenX parses it from stdout, Codex) — plus reply extraction, deferred registry persistence, and built-in Claude/Codex presets reachable from the `/subagents` page.

**Architecture:** Additive fields on `CliSubAgentConfig` (`id_source`, `session_id_pattern`, `output_pattern`) drive a strategy-aware `CliSubAgentTool.call()`. The per-session registry becomes two-phase (`lookup` + deferred `commit`). Presets live in a new backend module, are served by `GET /subagent/presets`, and are instantiated by a one-click "Add from preset" control that POSTs to the existing create endpoint. Defaults keep every existing prototype byte-identical.

**Tech Stack:** Python 3.11 / Pydantic v2 / FastAPI (backend); pytest (`unittest`-style, `IsolatedAsyncioTestCase`); React + TypeScript + Vite + shadcn/ui (frontend).

## Global Constraints

- **Backward compatible:** `id_source` defaults to `"provisioned"`; `session_id_pattern` and `output_pattern` default to `None`. Existing stored prototypes and the Claude preset behave exactly as before. No data migration.
- **Encapsulation:** internal files/classes/functions are `_`-prefixed; public surface is only what `subagent/__init__.py` and `_router/_schema/__init__.py` re-export.
- **Lazy imports:** third-party libs imported at point of use (not applicable to stdlib `re`/`shlex`/`uuid`, which stay at file top as today).
- **Docstrings:** English only, strict `Args:`/`Returns:` template with backtick-typed params.
- **Tests:** assert the **whole** data structure (`model_dump()` / full dict / tuple), not field-by-field; use `AnyString`/`AnyValue` from `tests/utils.py` for nondeterministic fields.
- **Lint/format:** black (line length 79), flake8, pylint, mypy, docstring checks. Do not skip pre-commit or disable checks file-wide.
- **Commit hygiene:** commit only the files named in each task; never `git add -A`; never touch `.gitignore` or unrelated files. End every commit message with:
  `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`
- **Codex reference output** (real `codex v0.144.5`), the source of the preset regexes:
  ```
  session id: 019f7035-0a1c-7143-9931-b6488c43f0b3
  --------
  user
  hello
  warning: Codex could not find bubblewrap on PATH. ...
  codex
  Hello! What would you like to work on?
  tokens used
  2,859
  ```
- `session_id_pattern` (Codex): `(?im)^session id:\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})`
- `output_pattern` (Codex): `(?s)\ncodex\n(.*?)\ntokens used`
- Backend test run: `python -m pytest tests/<file> -v` (use the repo's `ravenx` env python).

---

### Task 1: Config model — `id_source` + two regex fields + strategy-aware validation

**Files:**
- Modify: `src/agentscope/subagent/_base.py`
- Test: `tests/subagent_config_test.py`

**Interfaces:**
- Consumes: `CliSubAgentConfig` (existing).
- Produces: `CliSubAgentConfig` gains fields `id_source: Literal["provisioned","derived"] = "provisioned"`, `session_id_pattern: str | None = None`, `output_pattern: str | None = None`. Validation rules per spec §3.1. `model_dump()` now includes these three keys.

- [ ] **Step 1: Update the existing whole-dict dump test + add the failing new-behavior tests**

In `tests/subagent_config_test.py`, replace the body of `test_defaults_and_dump`'s expected dict to include the three new keys, and append the new tests to the class:

```python
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
                "resume_command": None,
                "id_source": "provisioned",
                "session_id_pattern": None,
                "output_pattern": None,
                "cwd": None,
                "env": None,
                "timeout": 600,
            },
        )

    def test_derived_config_is_valid(self) -> None:
        """A well-formed derived (Codex-style) config validates."""
        config = CliSubAgentConfig(
            name="codex",
            description="d",
            command=(
                "codex -a never exec -s workspace-write "
                "-c 'sandbox_workspace_write.network_access=true' {prompt}"
            ),
            resume_command=(
                "codex -a never exec -s workspace-write "
                "-c 'sandbox_workspace_write.network_access=true' "
                "resume {agent_id} {prompt}"
            ),
            id_source="derived",
            session_id_pattern=r"(?im)^session id:\s*([0-9a-f-]{36})",
            output_pattern=r"(?s)\ncodex\n(.*?)\ntokens used",
        )
        self.assertEqual(config.id_source, "derived")

    def test_derived_without_resume_command_is_rejected(self) -> None:
        """id_source 'derived' requires a resume_command."""
        with self.assertRaises(ValidationError):
            CliSubAgentConfig(
                name="codex",
                description="d",
                command="codex exec {prompt}",
                id_source="derived",
                session_id_pattern=r"(id:(\w+))",
            )

    def test_derived_command_with_agent_id_is_rejected(self) -> None:
        """A derived command must not carry {agent_id}."""
        with self.assertRaises(ValidationError):
            CliSubAgentConfig(
                name="codex",
                description="d",
                command="codex exec --id {agent_id} {prompt}",
                resume_command="codex resume {agent_id} {prompt}",
                id_source="derived",
                session_id_pattern=r"(?im)^id:\s*(\w+)",
            )

    def test_derived_without_pattern_is_rejected(self) -> None:
        """A derived prototype requires a session_id_pattern."""
        with self.assertRaises(ValidationError):
            CliSubAgentConfig(
                name="codex",
                description="d",
                command="codex exec {prompt}",
                resume_command="codex resume {agent_id} {prompt}",
                id_source="derived",
            )

    def test_bad_regex_pattern_is_rejected(self) -> None:
        """An uncompilable session_id_pattern is rejected."""
        with self.assertRaises(ValidationError):
            CliSubAgentConfig(
                name="codex",
                description="d",
                command="codex exec {prompt}",
                resume_command="codex resume {agent_id} {prompt}",
                id_source="derived",
                session_id_pattern=r"(unclosed",
            )

    def test_pattern_without_exactly_one_group_is_rejected(self) -> None:
        """An output_pattern with the wrong group count is rejected."""
        with self.assertRaises(ValidationError):
            CliSubAgentConfig(
                name="cc",
                description="d",
                command="claude -p {prompt}",
                output_pattern=r"no group here",
            )

    def test_provisioned_stateful_still_requires_agent_id(self) -> None:
        """The existing provisioned rule is unchanged."""
        with self.assertRaises(ValidationError):
            CliSubAgentConfig(
                name="cc",
                description="d",
                command="claude -p {prompt}",
                resume_command="claude -p {prompt} --resume {agent_id}",
            )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/subagent_config_test.py -v`
Expected: FAIL — `test_defaults_and_dump` (missing keys) plus the new tests error/fail because the fields and rules do not exist yet.

- [ ] **Step 3: Add the three fields to `CliSubAgentConfig`**

In `src/agentscope/subagent/_base.py`, add `import re` at the top of the stdlib imports (after `import shlex`). Then insert the three fields immediately after the `resume_command` field (before `cwd`):

```python
    id_source: Literal["provisioned", "derived"] = Field(
        default="provisioned",
        description=(
            "How the CLI session id is provisioned. 'provisioned': "
            "RavenX mints a uuid and injects it into `command` via "
            "'{agent_id}' (e.g. Claude Code's --session-id). 'derived': "
            "the CLI mints the id itself; `command` carries no "
            "'{agent_id}' and RavenX extracts it from the create run's "
            "stdout using `session_id_pattern`."
        ),
    )
    """The id-provisioning strategy."""

    session_id_pattern: str | None = Field(
        default=None,
        description=(
            "Regex with exactly one capture group, matched against the "
            "create run's stdout to extract the CLI-minted session id. "
            "Required when `id_source` is 'derived'."
        ),
    )
    """The derived-id extraction regex."""

    output_pattern: str | None = Field(
        default=None,
        description=(
            "Optional regex with exactly one capture group. When set, "
            "group 1 of the first match against stdout is returned as "
            "the sub-agent reply (strips CLI transcript wrappers)."
        ),
    )
    """The optional reply-extraction regex."""
```

- [ ] **Step 4: Add the pattern-compilation validator and rewrite `_validate_stateful`**

Still in `_base.py`, add a new validator immediately before `_validate_stateful`:

```python
    @model_validator(mode="after")
    def _validate_patterns(self) -> "CliSubAgentConfig":
        """Ensure each regex pattern compiles with exactly one group.

        Returns:
            `CliSubAgentConfig`:
                The validated config instance.
        """
        for label, pattern in (
            ("session_id_pattern", self.session_id_pattern),
            ("output_pattern", self.output_pattern),
        ):
            if pattern is None:
                continue
            try:
                compiled = re.compile(pattern)
            except re.error as error:
                raise ValueError(
                    f"{label} is not a valid regex: {error}",
                ) from error
            if compiled.groups != 1:
                raise ValueError(
                    f"{label} must have exactly one capture group, "
                    f"found {compiled.groups}",
                )
        return self
```

Then replace the entire `_validate_stateful` method body with:

```python
    @model_validator(mode="after")
    def _validate_stateful(self) -> "CliSubAgentConfig":
        """Validate id_source and the create/resume template pair.

        Stateless prototypes must be 'provisioned' and carry no
        `session_id_pattern`. Stateful 'provisioned' prototypes require
        `{agent_id}` in both templates (unchanged). Stateful 'derived'
        prototypes forbid `{agent_id}` in `command`, require it in
        `resume_command`, and require a `session_id_pattern`.

        Returns:
            `CliSubAgentConfig`:
                The validated config instance.
        """
        if not self.resume_command:
            if self.id_source == "derived":
                raise ValueError(
                    "id_source 'derived' requires a resume_command "
                    "(derived instances are stateful)",
                )
            if self.session_id_pattern is not None:
                raise ValueError(
                    "session_id_pattern is only valid for a stateful "
                    "'derived' prototype",
                )
            return self

        for label, template in (
            ("command", self.command),
            ("resume_command", self.resume_command),
        ):
            if "{prompt}" not in template and "{prompt_file}" not in template:
                raise ValueError(
                    f"stateful {label} must contain '{{prompt}}' or "
                    f"'{{prompt_file}}'",
                )
            try:
                tokens = shlex.split(template)
            except ValueError as error:
                raise ValueError(
                    f"{label} is not valid shell syntax: {error}",
                ) from error
            if (
                not tokens
                or "{prompt}" in tokens[0]
                or "{prompt_file}" in tokens[0]
            ):
                raise ValueError(
                    f"{label}'s first token must be a literal executable",
                )

        if "{agent_id}" not in self.resume_command:
            raise ValueError(
                "stateful resume_command must contain the '{agent_id}' "
                "placeholder",
            )

        if self.id_source == "provisioned":
            if "{agent_id}" not in self.command:
                raise ValueError(
                    "provisioned stateful command must contain the "
                    "'{agent_id}' placeholder",
                )
        else:  # derived
            if "{agent_id}" in self.command:
                raise ValueError(
                    "derived command must not contain '{agent_id}' (the "
                    "CLI mints the session id itself)",
                )
            if not self.session_id_pattern:
                raise ValueError(
                    "derived prototype requires a session_id_pattern",
                )
        return self
```

Also add `Literal` to the `from typing import` line if not already present (it is: `from typing import Literal`).

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python -m pytest tests/subagent_config_test.py -v`
Expected: PASS (all, including the preserved old tests).

- [ ] **Step 6: Commit**

```bash
git add src/agentscope/subagent/_base.py tests/subagent_config_test.py
git commit -m "feat(subagent): id_source strategy + session_id/output patterns on config

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Registry — replace eager `resolve` with `lookup` + deferred `commit`

**Files:**
- Modify: `src/agentscope/subagent/_instance_registry.py`
- Test: `tests/subagent_instance_registry_test.py`

**Interfaces:**
- Consumes: storage duck-type (`get_subagent_instance(session_id, handle)` → record-with-`.agent_id` or `None`; `upsert_subagent_instance(session_id, handle, agent_id, prototype_name)`).
- Produces: `SessionInstanceRegistry.lookup(handle: str) -> str | None` (read-only) and `SessionInstanceRegistry.commit(handle: str, agent_id: str, prototype_name: str) -> None`. The old `resolve()` is removed.

- [ ] **Step 1: Rewrite the registry test for lookup/commit**

Replace the whole body of the `SessionInstanceRegistryTest` class in `tests/subagent_instance_registry_test.py` with:

```python
class SessionInstanceRegistryTest(unittest.TestCase):
    """Validate deferred lookup/commit resolution."""

    def test_lookup_none_then_commit_then_lookup(self) -> None:
        """lookup is None before commit and returns the id after."""
        storage = _FakeStorage()
        registry = SessionInstanceRegistry(storage, "s1")

        async def _run() -> tuple[str | None, str | None]:
            before = await registry.lookup("writer")
            await registry.commit("writer", "AID-1", "claude_code")
            after = await registry.lookup("writer")
            return before, after

        before, after = asyncio.run(_run())
        self.assertEqual((before, after), (None, "AID-1"))

    def test_lookup_does_not_write(self) -> None:
        """A bare lookup persists nothing."""
        storage = _FakeStorage()
        registry = SessionInstanceRegistry(storage, "s1")

        async def _run() -> dict:
            await registry.lookup("writer")
            return storage._by_key  # pylint: disable=protected-access

        self.assertEqual(asyncio.run(_run()), {})

    def test_distinct_handles_get_distinct_ids(self) -> None:
        """Committed handles resolve independently."""
        storage = _FakeStorage()
        registry = SessionInstanceRegistry(storage, "s1")

        async def _run() -> tuple[str | None, str | None]:
            await registry.commit("writer", "A", "claude_code")
            await registry.commit("writer2", "B", "claude_code")
            return (
                await registry.lookup("writer"),
                await registry.lookup("writer2"),
            )

        self.assertEqual(asyncio.run(_run()), ("A", "B"))
```

Remove the now-unused `from utils import AnyString` import at the top of the file.

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/subagent_instance_registry_test.py -v`
Expected: FAIL — `AttributeError: 'SessionInstanceRegistry' object has no attribute 'lookup'`.

- [ ] **Step 3: Rewrite the registry**

Replace the whole body of `src/agentscope/subagent/_instance_registry.py` with:

```python
# -*- coding: utf-8 -*-
"""Per-session registry mapping instance handles to CLI session ids."""

from typing import Any


class SessionInstanceRegistry:
    """Resolves a sub-agent instance handle to a stable CLI session id.

    Scoped to one chat session. Backed by a storage object exposing
    ``get_subagent_instance`` / ``upsert_subagent_instance`` (duck-typed
    so this module does not import ``app.storage``). Persistence is
    deferred: the tool calls :meth:`lookup` to decide create-vs-resume
    and only :meth:`commit` after a create run succeeds, so a failed
    create never poisons the handle.
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

    async def lookup(self, handle: str) -> str | None:
        """Return the CLI session id bound to ``handle``, or ``None``.

        Args:
            handle (`str`):
                The agent-chosen instance handle.

        Returns:
            `str | None`:
                The stored ``agent_id`` when the handle is known, else
                ``None`` (the caller should run the create command).
        """
        record = await self._storage.get_subagent_instance(
            self._session_id,
            handle,
        )
        return record.agent_id if record is not None else None

    async def commit(
        self,
        handle: str,
        agent_id: str,
        prototype_name: str,
    ) -> None:
        """Persist ``handle -> agent_id`` after a successful create.

        Args:
            handle (`str`):
                The agent-chosen instance handle.
            agent_id (`str`):
                The CLI session id to bind to the handle.
            prototype_name (`str`):
                The prototype ``name`` this instance is of.
        """
        await self._storage.upsert_subagent_instance(
            self._session_id,
            handle,
            agent_id,
            prototype_name,
        )
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `python -m pytest tests/subagent_instance_registry_test.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_instance_registry.py tests/subagent_instance_registry_test.py
git commit -m "feat(subagent): two-phase instance registry (lookup + deferred commit)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Tool runtime — two-phase lifecycle, derived id parse, output extraction

**Files:**
- Modify: `src/agentscope/subagent/_tool.py`
- Test: `tests/subagent_tool_test.py`

**Interfaces:**
- Consumes: `SessionInstanceRegistry.lookup`/`commit` (Task 2).
- Produces: `CliSubAgentTool.__init__` gains `id_source: str = "provisioned"`, `session_id_pattern: str | None = None`, `output_pattern: str | None = None`. Runtime behavior per spec §3.2. Success `metadata` shape unchanged: `{"instance", "agent_id", "action"}` (stateful) or `{}` (stateless); `agent_id` may be `None` on a derived-create parse miss.

- [ ] **Step 1: Update the stateful test stubs and add derived/poison tests**

In `tests/subagent_tool_test.py`, replace the `_FixedRegistry` class with a lookup/commit stub and add a canned backend near the top (after `_RecordingBackend`):

```python
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
        prototype_name: str,  # pylint: disable=unused-argument
    ) -> None:
        """Persist handle -> agent_id."""
        self.store[handle] = agent_id


class _CannedBackend:
    """Fake backend returning a fixed ExecResult and recording argv."""

    def __init__(
        self,
        stdout: bytes = b"",
        stderr: bytes = b"",
        exit_code: int = 0,
    ) -> None:
        self._result = ExecResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
        )
        self.calls: list[list[str]] = []

    async def exec_shell(  # pylint: disable=unused-argument
        self,
        argv: list[str],
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        """Record argv and return the canned result."""
        self.calls.append(argv)
        return self._result
```

Then replace `CliSubAgentToolStatefulTest._make_tool` to use `_MemRegistry` and rewrite `test_create_then_resume` to capture the minted id:

```python
    def _make_tool(
        self,
    ) -> tuple[CliSubAgentTool, _RecordingBackend]:
        """Build a stateful tool with recording backend + registry."""
        backend = _RecordingBackend()
        tool = CliSubAgentTool(
            name="cc",
            description="d",
            command="claude -p {prompt} --session-id {agent_id}",
            resume_command="claude -p {prompt} --resume {agent_id}",
            backend=backend,
            registry=_MemRegistry(),
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
        """First call uses command, second reuses the committed id."""
        tool, backend = self._make_tool()

        first = asyncio.run(
            _collect(tool.call(prompt="draft", instance="writer")),
        )
        aid = first[-1].metadata["agent_id"]
        second = asyncio.run(
            _collect(tool.call(prompt="revise", instance="writer")),
        )

        self.assertEqual(
            backend.calls,
            [
                ["claude", "-p", "draft", "--session-id", aid],
                ["claude", "-p", "revise", "--resume", aid],
            ],
        )
        self.assertEqual(
            first[-1].metadata,
            {"instance": "writer", "agent_id": aid, "action": "create"},
        )
        self.assertEqual(
            second[-1].metadata,
            {"instance": "writer", "agent_id": aid, "action": "resume"},
        )
```

Add a new test class at the end of the file for the derived + poison behavior:

```python
_CODEX_STDOUT = (
    b"session id: 019f7035-0a1c-7143-9931-b6488c43f0b3\n"
    b"--------\nuser\nhi\ncodex\nHELLO WORLD\ntokens used\n5\n"
)
_SESSION_RE = (
    r"(?im)^session id:\s*"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
_OUTPUT_RE = r"(?s)\ncodex\n(.*?)\ntokens used"


class CliSubAgentToolDerivedTest(unittest.TestCase):
    """Derived id parsing, output extraction, and deferred commit."""

    def _make_tool(
        self,
        backend: _CannedBackend,
        registry: _MemRegistry,
    ) -> CliSubAgentTool:
        """Build a derived (Codex-style) stateful tool."""
        return CliSubAgentTool(
            name="codex",
            description="d",
            command="codex exec {prompt}",
            resume_command="codex resume {agent_id} {prompt}",
            id_source="derived",
            session_id_pattern=_SESSION_RE,
            output_pattern=_OUTPUT_RE,
            backend=backend,
            registry=registry,
        )

    def test_create_parses_id_extracts_reply_and_commits(self) -> None:
        """Create parses the id, extracts the reply, resume reuses id."""
        backend = _CannedBackend(stdout=_CODEX_STDOUT)
        registry = _MemRegistry()
        tool = self._make_tool(backend, registry)

        first = asyncio.run(
            _collect(tool.call(prompt="draft", instance="w")),
        )
        second = asyncio.run(
            _collect(tool.call(prompt="revise", instance="w")),
        )

        aid = "019f7035-0a1c-7143-9931-b6488c43f0b3"
        self.assertEqual(first[-1].content[0].text, "HELLO WORLD")
        self.assertEqual(
            first[-1].metadata,
            {"instance": "w", "agent_id": aid, "action": "create"},
        )
        self.assertEqual(registry.store, {"w": aid})
        self.assertEqual(
            backend.calls,
            [
                ["codex", "exec", "draft"],
                ["codex", "resume", aid, "revise"],
            ],
        )
        self.assertEqual(
            second[-1].metadata,
            {"instance": "w", "agent_id": aid, "action": "resume"},
        )

    def test_parse_miss_warns_and_does_not_commit(self) -> None:
        """A create with no id yields a warning and no persistence."""
        backend = _CannedBackend(
            stdout=b"user\nhi\ncodex\nHELLO\ntokens used\n5\n",
        )
        registry = _MemRegistry()
        tool = self._make_tool(backend, registry)

        chunks = asyncio.run(
            _collect(tool.call(prompt="draft", instance="w")),
        )
        text = chunks[-1].content[0].text
        self.assertTrue(text.startswith("HELLO"))
        self.assertIn("not resumable", text)
        self.assertEqual(registry.store, {})
        self.assertEqual(chunks[-1].metadata["agent_id"], None)

    def test_provisioned_create_failure_is_not_committed(self) -> None:
        """A failed provisioned create must not poison the handle."""
        backend = _CannedBackend(stderr=b"boom", exit_code=1)
        registry = _MemRegistry()
        tool = CliSubAgentTool(
            name="cc",
            description="d",
            command="claude -p {prompt} --session-id {agent_id}",
            resume_command="claude -p {prompt} --resume {agent_id}",
            backend=backend,
            registry=registry,
        )
        chunks = asyncio.run(
            _collect(tool.call(prompt="x", instance="w")),
        )
        self.assertEqual(chunks[-1].state, ToolResultState.ERROR)
        self.assertEqual(registry.store, {})


class CliSubAgentToolOutputPatternTest(unittest.TestCase):
    """output_pattern extraction on a stateless tool + fallback."""

    def test_extracts_when_matched(self) -> None:
        """A matched output_pattern returns only group 1."""
        backend = _CannedBackend(stdout=b"noise\ncodex\nREPLY\ntokens used\n")
        tool = CliSubAgentTool(
            name="cc",
            description="d",
            command="codex exec {prompt}",
            output_pattern=_OUTPUT_RE,
            backend=backend,
        )
        chunks = asyncio.run(_collect(tool.call(prompt="hi")))
        self.assertEqual(chunks[-1].content[0].text, "REPLY")

    def test_falls_back_to_raw_when_no_match(self) -> None:
        """A non-matching output_pattern returns the raw output."""
        backend = _CannedBackend(stdout=b"just some text")
        tool = CliSubAgentTool(
            name="cc",
            description="d",
            command="codex exec {prompt}",
            output_pattern=_OUTPUT_RE,
            backend=backend,
        )
        chunks = asyncio.run(_collect(tool.call(prompt="hi")))
        self.assertEqual(chunks[-1].content[0].text, "just some text")
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/subagent_tool_test.py -v`
Expected: FAIL — `CliSubAgentTool.__init__` rejects `id_source`/`session_id_pattern`/`output_pattern`; stateful tests fail because `call()` still calls `resolve()`.

- [ ] **Step 3: Add the new `__init__` params and compile the patterns**

In `src/agentscope/subagent/_tool.py`, add `import re` at the top (after `import shlex`). Extend `__init__`'s signature — add these three params right after `resume_command`:

```python
        resume_command: str | None = None,
        id_source: str = "provisioned",
        session_id_pattern: str | None = None,
        output_pattern: str | None = None,
        cwd: str | None = None,
```

And in the `__init__` body, after `self._resume_command = resume_command`, add:

```python
        self._id_source = id_source
        self._session_id_re = (
            re.compile(session_id_pattern) if session_id_pattern else None
        )
        self._output_re = re.compile(output_pattern) if output_pattern else None
```

Update the `__init__` docstring `Args:` block to document the three params (English, backtick-typed) — e.g.:

```
            id_source (`str`, defaults to `"provisioned"`):
                Id strategy: ``"provisioned"`` (RavenX mints the id and
                injects it via ``{agent_id}``) or ``"derived"`` (the CLI
                mints it and it is parsed from stdout).
            session_id_pattern (`str | None`, optional):
                Regex (one capture group) extracting the CLI-minted id
                from a derived create run's stdout.
            output_pattern (`str | None`, optional):
                Regex (one capture group) extracting the real reply from
                stdout; when unset, the raw output is returned.
```

- [ ] **Step 4: Rewrite `call()` for the two-phase lifecycle**

Replace the entire `call()` method in `_tool.py` with:

```python
    async def call(  # type: ignore[override]
        self,
        prompt: str,
        instance: str | None = None,
        prompt_file: str | None = None,
        output_file: str | None = None,
    ) -> AsyncGenerator[ToolChunk, None]:
        """Run the sub-agent and yield its result.

        Args:
            prompt (`str`):
                The task to delegate to the sub-agent.
            instance (`str | None`, optional):
                The instance handle (stateful prototypes only). An
                unknown/absent handle creates; a known handle resumes.
            prompt_file (`str | None`, optional):
                The path substituted for ``{prompt_file}``. When the
                template uses ``{prompt_file}`` and this is ``None``,
                ``prompt`` is written to a generated temp file first.
            output_file (`str | None`, optional):
                When set and the sub-agent exits successfully, the final
                (post-extraction, untruncated) output is written here.

        Yields:
            `ToolChunk`:
                A single terminal chunk with the sub-agent's output.
        """
        handle: str | None = None
        action: str | None = None
        agent_id: str | None = None
        created = False
        template = self._command
        if self.is_stateful:
            handle = instance or uuid.uuid4().hex
            try:
                existing = await self._registry.lookup(handle)
            except Exception:  # noqa: BLE001 - degrade to a create run
                existing = None
            if existing is not None:
                agent_id = existing
                assert self._resume_command is not None
                template = self._resume_command
                action = "resume"
            else:
                created = True
                action = "create"
                agent_id = (
                    None
                    if self._id_source == "derived"
                    else uuid.uuid4().hex
                )
        else:
            agent_id = uuid.uuid4().hex

        if "{prompt_file}" in template and prompt_file is None:
            prompt_file = await self._write_temp_prompt(prompt)

        argv = self._build_argv(prompt, template, agent_id, prompt_file)

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
                            f"Sub-Agent timed out after " f"{self._timeout}s."
                        ),
                    ),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        if not result.ok():
            output = stdout
            if stderr:
                output = f"{output}\n{stderr}" if output else stderr
            if len(output) > _MAX_OUTPUT_CHARS:
                output = output[:_MAX_OUTPUT_CHARS] + "\n... (output truncated)"
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

        # Success. 1) Parse the CLI-minted id for a derived create.
        if created and self._id_source == "derived":
            if self._session_id_re is not None:
                match = self._session_id_re.search(stdout)
                if match is not None:
                    agent_id = match.group(1)

        # 2) Deferred persistence: commit only a successful create.
        if created and self.is_stateful and agent_id is not None:
            assert handle is not None
            try:
                await self._registry.commit(handle, agent_id, self.name)
            except Exception:  # noqa: BLE001 - best-effort persistence
                pass

        # 3) Extract the reply, or fall back to the raw combined output.
        combined = stdout
        if stderr:
            combined = f"{combined}\n{stderr}" if combined else stderr
        if self._output_re is not None:
            match = self._output_re.search(stdout)
            output = match.group(1).strip() if match is not None else combined
        else:
            output = combined

        # 4) Append a parse-miss warning last, so extraction cannot drop it.
        if created and self._id_source == "derived" and agent_id is None:
            output = (
                f"{output}\n\n[RavenX] Warning: could not extract a session "
                "id from the create output; this instance is not resumable. "
                "Use a new instance handle to recreate."
            )

        # 5) Persist the full output, then truncate the in-context return.
        if output_file is not None:
            await self._backend.write_file(
                output_file,
                output.encode("utf-8"),
            )
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + "\n... (output truncated)"

        if self.is_stateful:
            metadata = {
                "instance": handle,
                "agent_id": agent_id,
                "action": action,
            }
        else:
            metadata = {}

        yield ToolChunk(
            content=[TextBlock(text=output)],
            state=ToolResultState.RUNNING,
            is_last=True,
            metadata=metadata,
        )
```

- [ ] **Step 5: Run the full tool test file to verify all pass**

Run: `python -m pytest tests/subagent_tool_test.py -v`
Expected: PASS (the six preserved stateless/prompt_file tests + the updated stateful tests + the new derived/output tests).

- [ ] **Step 6: Commit**

```bash
git add src/agentscope/subagent/_tool.py tests/subagent_tool_test.py
git commit -m "feat(subagent): strategy-aware tool runtime (derived id parse + reply extraction)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Wire config fields into the tool factory

**Files:**
- Modify: `src/agentscope/subagent/_agent_tools.py:106-118`
- Test: `tests/subagent_agent_tools_test.py`

**Interfaces:**
- Consumes: `CliSubAgentConfig.id_source`/`session_id_pattern`/`output_pattern` (Task 1); `CliSubAgentTool.__init__` params (Task 3).
- Produces: no new public surface; the factory now forwards the three fields.

- [ ] **Step 1: Extend the `_record` helper and add a wiring test**

In `tests/subagent_agent_tools_test.py`, extend the `_record` helper signature/body to carry the new fields, then add a test. Replace the `_record` function with:

```python
def _record(
    subagent_id: str,
    name: str,
    command: str,
    cwd: str | None = None,
    resume_command: str | None = None,
    id_source: str = "provisioned",
    session_id_pattern: str | None = None,
    output_pattern: str | None = None,
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
            "resume_command": resume_command,
            "id_source": id_source,
            "session_id_pattern": session_id_pattern,
            "output_pattern": output_pattern,
            "cwd": cwd,
            "env": None,
            "timeout": 600,
        },
    )
```

Add this test to `MakeSubAgentToolFactoryTest`:

```python
    async def test_derived_config_wires_patterns(self) -> None:
        """A derived config forwards id_source + patterns to the tool."""
        storage = _FakeStorage(
            [
                _record(
                    "sa-1",
                    "codex",
                    "codex exec {prompt}",
                    resume_command="codex resume {agent_id} {prompt}",
                    id_source="derived",
                    session_id_pattern=r"(?im)^id:\s*(\w+)",
                    output_pattern=r"(?s)\ncodex\n(.*?)\ntokens used",
                ),
            ],
        )
        wm = _FakeWorkspaceManager(None)
        factory = make_subagent_tool_factory(storage, wm)
        tools = await factory("u1", "a1", "s1")
        cli = [t for t in tools if isinstance(t, CliSubAgentTool)]
        self.assertEqual(len(cli), 1)
        # pylint: disable=protected-access
        self.assertEqual(cli[0]._id_source, "derived")
        self.assertIsNotNone(cli[0]._session_id_re)
        self.assertIsNotNone(cli[0]._output_re)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/subagent_agent_tools_test.py::MakeSubAgentToolFactoryTest::test_derived_config_wires_patterns -v`
Expected: FAIL — `cli[0]._id_source` is the default `"provisioned"` because the factory does not forward the field yet.

- [ ] **Step 3: Forward the fields in the factory**

In `src/agentscope/subagent/_agent_tools.py`, extend the `CliSubAgentTool(...)` construction inside `_factory` to pass the three fields (insert after `resume_command=config.resume_command,`):

```python
            tools.append(
                CliSubAgentTool(
                    name=config.name,
                    description=config.description,
                    command=config.command,
                    resume_command=config.resume_command,
                    id_source=config.id_source,
                    session_id_pattern=config.session_id_pattern,
                    output_pattern=config.output_pattern,
                    cwd=config.cwd or session_workdir,
                    env=config.env,
                    timeout=config.timeout,
                    backend=backend,
                    registry=SessionInstanceRegistry(storage, session_id),
                ),
            )
```

- [ ] **Step 4: Run the whole factory test file to verify all pass**

Run: `python -m pytest tests/subagent_agent_tools_test.py -v`
Expected: PASS (existing tests still pass — the extended `_record` adds keys with defaults; new test passes).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_agent_tools.py tests/subagent_agent_tools_test.py
git commit -m "feat(subagent): forward id_source + patterns from config to tool

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Built-in presets module

**Files:**
- Create: `src/agentscope/subagent/_presets.py`
- Modify: `src/agentscope/subagent/__init__.py`
- Test: `tests/subagent_presets_test.py`

**Interfaces:**
- Consumes: `CliSubAgentConfig` / `SubAgentFactory.from_dict` (validate presets).
- Produces: `list_subagent_presets() -> list[dict]` where each dict is `{"preset_id": str, "label": str, "data": dict}` and `data` is a valid `CliSubAgentConfig` payload with no `id`. Exported from `agentscope.subagent`.

- [ ] **Step 1: Write the failing presets test**

Create `tests/subagent_presets_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for the built-in CLI sub-agent presets."""

import re
import unittest

from agentscope.subagent import (
    CliSubAgentConfig,
    SubAgentFactory,
    list_subagent_presets,
)

_CODEX_SAMPLE = (
    "OpenAI Codex v0.144.5\n"
    "--------\n"
    "session id: 019f7035-0a1c-7143-9931-b6488c43f0b3\n"
    "--------\n"
    "user\n"
    "hello\n"
    "codex\n"
    "Hello! What would you like to work on?\n"
    "tokens used\n"
    "2,859\n"
)


class SubAgentPresetsTest(unittest.TestCase):
    """Validate the built-in presets."""

    def test_preset_ids_and_labels(self) -> None:
        """The two canonical presets are present and ordered."""
        presets = list_subagent_presets()
        self.assertEqual(
            [(p["preset_id"], p["label"]) for p in presets],
            [("claude_code", "Claude Code"), ("codex", "Codex")],
        )

    def test_each_preset_round_trips_through_factory(self) -> None:
        """Every preset payload builds a valid CliSubAgentConfig."""
        for preset in list_subagent_presets():
            config = SubAgentFactory.from_dict(preset["data"])
            self.assertIsInstance(config, CliSubAgentConfig)
            self.assertEqual(config.name, preset["preset_id"])

    def test_codex_patterns_match_real_output(self) -> None:
        """The Codex regexes extract the id and the reply from a sample."""
        codex = next(
            p for p in list_subagent_presets() if p["preset_id"] == "codex"
        )
        data = codex["data"]
        self.assertEqual(data["id_source"], "derived")
        sid = re.search(data["session_id_pattern"], _CODEX_SAMPLE)
        out = re.search(data["output_pattern"], _CODEX_SAMPLE)
        self.assertEqual(
            sid.group(1),
            "019f7035-0a1c-7143-9931-b6488c43f0b3",
        )
        self.assertEqual(
            out.group(1).strip(),
            "Hello! What would you like to work on?",
        )
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/subagent_presets_test.py -v`
Expected: FAIL — `ImportError: cannot import name 'list_subagent_presets'`.

- [ ] **Step 3: Create the presets module**

Create `src/agentscope/subagent/_presets.py`:

```python
# -*- coding: utf-8 -*-
"""Built-in CLI sub-agent presets (Claude Code, Codex).

Each preset is a ready-to-instantiate ``CliSubAgentConfig`` payload
(without an ``id``, which the create endpoint mints). The frontend's
"Add from preset" control POSTs a preset's ``data`` to
``POST /subagent/``.
"""

_CODEX_SESSION_ID_PATTERN = (
    r"(?im)^session id:\s*"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})"
)
_CODEX_OUTPUT_PATTERN = r"(?s)\ncodex\n(.*?)\ntokens used"

_CODEX_COMMAND = (
    "codex -a never exec -s workspace-write "
    "-c 'sandbox_workspace_write.network_access=true' {prompt}"
)
_CODEX_RESUME_COMMAND = (
    "codex -a never exec -s workspace-write "
    "-c 'sandbox_workspace_write.network_access=true' "
    "resume {agent_id} {prompt}"
)


def list_subagent_presets() -> list[dict]:
    """Return the built-in CLI sub-agent presets.

    Returns:
        `list[dict]`:
            One dict per preset with keys ``preset_id`` (stable id),
            ``label`` (human name), and ``data`` (a valid
            ``CliSubAgentConfig`` payload without an ``id``).
    """
    return [
        {
            "preset_id": "claude_code",
            "label": "Claude Code",
            "data": {
                "type": "cli_subagent",
                "name": "claude_code",
                "description": (
                    "Delegate a coding task to Claude Code. Stateful: a "
                    "new instance handle starts a fresh session; reusing "
                    "a handle resumes it."
                ),
                "command": (
                    "claude -p {prompt} --permission-mode auto "
                    "--session-id {agent_id}"
                ),
                "resume_command": (
                    "claude -p {prompt} --permission-mode auto "
                    "--resume {agent_id}"
                ),
                "id_source": "provisioned",
                "session_id_pattern": None,
                "output_pattern": None,
                "cwd": None,
                "env": None,
                "timeout": 600,
            },
        },
        {
            "preset_id": "codex",
            "label": "Codex",
            "data": {
                "type": "cli_subagent",
                "name": "codex",
                "description": (
                    "Delegate a coding task to OpenAI Codex. Stateful: "
                    "the session id is derived from the create run's "
                    "output and reused on resume."
                ),
                "command": _CODEX_COMMAND,
                "resume_command": _CODEX_RESUME_COMMAND,
                "id_source": "derived",
                "session_id_pattern": _CODEX_SESSION_ID_PATTERN,
                "output_pattern": _CODEX_OUTPUT_PATTERN,
                "cwd": None,
                "env": None,
                "timeout": 600,
            },
        },
    ]
```

- [ ] **Step 4: Export the helper**

In `src/agentscope/subagent/__init__.py`, add the import and `__all__` entry:

```python
from ._presets import list_subagent_presets
```

and add `"list_subagent_presets",` to `__all__`.

- [ ] **Step 5: Run the test to verify it passes**

Run: `python -m pytest tests/subagent_presets_test.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/agentscope/subagent/_presets.py src/agentscope/subagent/__init__.py tests/subagent_presets_test.py
git commit -m "feat(subagent): built-in Claude Code + Codex presets

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Preset API endpoint

**Files:**
- Modify: `src/agentscope/app/_router/_schema/_subagent.py`
- Modify: `src/agentscope/app/_router/_schema/__init__.py`
- Modify: `src/agentscope/app/_router/_subagent.py`
- Test: `tests/subagent_router_test.py`

**Interfaces:**
- Consumes: `list_subagent_presets()` (Task 5).
- Produces: `GET /subagent/presets` → `ListSubAgentPresetsResponse { presets: [ SubAgentPreset { preset_id, label, data } ] }`.

- [ ] **Step 1: Write the failing router test**

Add to `tests/subagent_router_test.py`, inside `SubAgentRouterTest`:

```python
    def test_presets_lists_claude_and_codex(self) -> None:
        """GET /subagent/presets returns both built-in presets."""
        client = make_client()
        resp = client.get("/subagent/presets", headers=HEADERS)
        self.assertEqual(resp.status_code, 200)
        presets = resp.json()["presets"]
        self.assertEqual(
            [p["preset_id"] for p in presets],
            ["claude_code", "codex"],
        )
        codex = next(p for p in presets if p["preset_id"] == "codex")
        self.assertEqual(codex["data"]["id_source"], "derived")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `python -m pytest tests/subagent_router_test.py::SubAgentRouterTest::test_presets_lists_claude_and_codex -v`
Expected: FAIL — 404 (route not defined).

- [ ] **Step 3: Add the response schemas**

In `src/agentscope/app/_router/_schema/_subagent.py`, append:

```python
class SubAgentPreset(BaseModel):
    """A built-in sub-agent preset returned to the frontend."""

    preset_id: str = Field(description="Stable preset identifier.")
    label: str = Field(description="Human-readable preset name.")
    data: dict = Field(description="Ready-to-create config payload.")


class ListSubAgentPresetsResponse(BaseModel):
    """Response body for listing built-in sub-agent presets."""

    presets: list[SubAgentPreset] = Field(
        description="Built-in sub-agent presets.",
    )
```

- [ ] **Step 4: Export the schemas**

In `src/agentscope/app/_router/_schema/__init__.py`, extend the `from ._subagent import (...)` block and `__all__`:

```python
from ._subagent import (
    CreateSubAgentRequest,
    CreateSubAgentResponse,
    UpdateSubAgentRequest,
    SubAgentView,
    ListSubAgentsResponse,
    ListSubAgentSchemasResponse,
    ListSubAgentPresetsResponse,
    SubAgentPreset,
)
```

and add `"ListSubAgentPresetsResponse",` and `"SubAgentPreset",` under the `# Sub-Agent` section of `__all__`.

- [ ] **Step 5: Add the endpoint**

In `src/agentscope/app/_router/_subagent.py`, update the imports:

```python
from ._schema import (
    CreateSubAgentRequest,
    CreateSubAgentResponse,
    ListSubAgentPresetsResponse,
    ListSubAgentSchemasResponse,
    ListSubAgentsResponse,
    SubAgentPreset,
    SubAgentView,
    UpdateSubAgentRequest,
)
from ..storage import StorageBase
from ...subagent import SubAgentFactory, list_subagent_presets
```

Then add the endpoint just after `list_subagent_schemas` (so it sits with the other GETs, before the `/{subagent_id}` PATCH/DELETE routes):

```python
@subagent_router.get(
    "/presets",
    response_model=ListSubAgentPresetsResponse,
    summary="List built-in sub-agent presets",
)
async def list_presets() -> ListSubAgentPresetsResponse:
    """Return the built-in CLI sub-agent presets.

    Returns:
        `ListSubAgentPresetsResponse`: The built-in presets.
    """
    return ListSubAgentPresetsResponse(
        presets=[SubAgentPreset(**preset) for preset in list_subagent_presets()],
    )
```

- [ ] **Step 6: Run the whole router test file to verify all pass**

Run: `python -m pytest tests/subagent_router_test.py -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/agentscope/app/_router/_schema/_subagent.py src/agentscope/app/_router/_schema/__init__.py src/agentscope/app/_router/_subagent.py tests/subagent_router_test.py
git commit -m "feat(subagent): GET /subagent/presets endpoint

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Frontend — preset types, API, "Add from preset" control, form pass-through

**Files:**
- Modify: `examples/web_ui/frontend/src/api/types.ts:419-452`
- Modify: `examples/web_ui/frontend/src/api/subagent.ts`
- Modify: `examples/web_ui/frontend/src/pages/subagent/index.tsx`
- Modify: `examples/web_ui/frontend/src/i18n/locales/en.json`
- Modify: `examples/web_ui/frontend/src/i18n/locales/zh.json`

**Interfaces:**
- Consumes: `GET /subagent/presets` (Task 6).
- Produces: `subagentApi.listPresets()`; a preset dropdown on `/subagents`; the form carries `id_source`/`session_id_pattern`/`output_pattern` invisibly.

- [ ] **Step 1: Extend the API types**

In `src/api/types.ts`, replace the `SubAgentData` interface and add preset types:

```typescript
export interface SubAgentData {
	type: 'cli_subagent';
	name: string;
	description: string;
	command: string;
	resume_command?: string | null;
	id_source?: 'provisioned' | 'derived';
	session_id_pattern?: string | null;
	output_pattern?: string | null;
	cwd?: string | null;
	env?: Record<string, string> | null;
	timeout?: number;
}

export interface SubAgentPreset {
	preset_id: string;
	label: string;
	data: SubAgentData;
}

export interface SubAgentPresetsResponse {
	presets: SubAgentPreset[];
}
```

- [ ] **Step 2: Add the API method**

In `src/api/subagent.ts`, add the import and method:

```typescript
import { client } from './client';
import type {
	CreateSubAgentRequest,
	CreateSubAgentResponse,
	SubAgentListResponse,
	SubAgentPresetsResponse,
	SubAgentView,
	UpdateSubAgentRequest,
} from './types';

export const subagentApi = {
	list: () => client.get<SubAgentListResponse>('/subagent/'),

	presets: () => client.get<SubAgentPresetsResponse>('/subagent/presets'),

	create: (body: CreateSubAgentRequest) =>
		client.post<CreateSubAgentResponse>('/subagent/', body),

	update: (subagentId: string, body: UpdateSubAgentRequest) =>
		client.patch<SubAgentView>(`/subagent/${subagentId}`, body),

	delete: (subagentId: string) => client.delete(`/subagent/${subagentId}`),
};
```

- [ ] **Step 3: Carry the strategy fields through the form + add the preset dropdown**

In `src/pages/subagent/index.tsx`, make the following edits.

(a) Extend the imports at the top — add the dropdown menu, the `useEffect` hook, and the `subagentApi` + preset type:

```typescript
import { Bot, ChevronDown, Loader2, Plus, Trash2, X } from 'lucide-react';
import { useEffect, useState } from 'react';

import { subagentApi } from '@/api';
import type { SubAgentPreset, SubAgentView } from '@/api';
import { DeleteDialog } from '@/components/dialog/DeleteDialog';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import {
	DropdownMenu,
	DropdownMenuContent,
	DropdownMenuItem,
	DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Empty, EmptyHeader, EmptyTitle } from '@/components/ui/empty';
```

(b) Extend `FormState` with the three carried fields:

```typescript
interface FormState {
	name: string;
	description: string;
	command: string;
	resume_command: string;
	cwd: string;
	/** One `KEY=VALUE` pair per line; parsed into a record on submit. */
	env: string;
	timeout: string;
	/** Carried invisibly (set by presets), not rendered as inputs. */
	id_source: 'provisioned' | 'derived';
	session_id_pattern: string;
	output_pattern: string;
}
```

(c) Extend `EMPTY_FORM`:

```typescript
const EMPTY_FORM: FormState = {
	name: '',
	description: '',
	command: '',
	resume_command: '',
	cwd: '',
	env: '',
	timeout: '600',
	id_source: 'provisioned',
	session_id_pattern: '',
	output_pattern: '',
};
```

(d) Extend `toForm` and `toPayload` to round-trip the carried fields:

```typescript
function toForm(view: SubAgentView): FormState {
	return {
		name: view.data.name,
		description: view.data.description,
		command: view.data.command,
		resume_command: view.data.resume_command ?? '',
		cwd: view.data.cwd ?? '',
		env: envToText(view.data.env),
		timeout: String(view.data.timeout ?? 600),
		id_source: view.data.id_source ?? 'provisioned',
		session_id_pattern: view.data.session_id_pattern ?? '',
		output_pattern: view.data.output_pattern ?? '',
	};
}

function toPayload(form: FormState): Record<string, unknown> {
	return {
		type: 'cli_subagent',
		name: form.name.trim(),
		description: form.description.trim(),
		command: form.command.trim(),
		resume_command: form.resume_command.trim() || null,
		id_source: form.id_source,
		session_id_pattern: form.session_id_pattern.trim() || null,
		output_pattern: form.output_pattern.trim() || null,
		cwd: form.cwd.trim() || null,
		env: textToEnv(form.env),
		timeout: Number(form.timeout) || 600,
	};
}
```

(e) Inside the component, after the existing `useState` hooks, load presets and make validation strategy-aware. Replace the `stateful`/`cmdOk`/`resumeOk`/`canSubmit` block with:

```typescript
	const [presets, setPresets] = useState<SubAgentPreset[]>([]);

	useEffect(() => {
		subagentApi
			.presets()
			.then((res) => setPresets(res.presets))
			.catch(() => setPresets([]));
	}, []);

	const addFromPreset = async (preset: SubAgentPreset) => {
		setError(null);
		try {
			await create(preset.data as unknown as Record<string, unknown>);
		} catch (err) {
			setError(String((err as Error)?.message ?? err));
		}
	};

	// Mirror the backend's rules. A stateful *provisioned* command needs
	// `{agent_id}`; a *derived* command must not (the CLI mints the id).
	// Any stateful `resume_command` needs `{prompt}` + `{agent_id}`.
	const stateful = form.resume_command.trim().length > 0;
	const derived = form.id_source === 'derived';
	const cmdOk =
		form.command.includes('{prompt}') &&
		(!stateful || derived || form.command.includes('{agent_id}'));
	const resumeOk =
		!stateful ||
		(form.resume_command.includes('{prompt}') &&
			form.resume_command.includes('{agent_id}'));
	const canSubmit = !!form.name.trim() && !!form.description.trim() && cmdOk && resumeOk;
```

(f) Add the "Add from preset" dropdown next to the `+` button. Replace the header button row (the `<div className="flex items-center justify-between">` containing the title + `+`) with:

```tsx
						<div className="flex items-center justify-between">
							<div className="text-lg font-semibold">{t('subagent-sidebar.title')}</div>
							<div className="flex items-center gap-x-1">
								{presets.length > 0 && (
									<DropdownMenu>
										<DropdownMenuTrigger asChild>
											<Button size="sm" variant="outline">
												{t('subagent-sidebar.addFromPreset')}
												<ChevronDown className="size-4" />
											</Button>
										</DropdownMenuTrigger>
										<DropdownMenuContent align="end">
											{presets.map((preset) => (
												<DropdownMenuItem
													key={preset.preset_id}
													onClick={() => addFromPreset(preset)}
												>
													{preset.label}
												</DropdownMenuItem>
											))}
										</DropdownMenuContent>
									</DropdownMenu>
								)}
								<Button size="icon-sm" variant="outline" onClick={openCreate}>
									<Plus />
								</Button>
							</div>
						</div>
```

- [ ] **Step 4: Add the i18n key (both locales, with parity)**

In `src/i18n/locales/en.json`, inside the `"subagent-sidebar"` object, add:

```json
			"addFromPreset": "Add from preset",
```

In `src/i18n/locales/zh.json`, inside the `"subagent-sidebar"` object, add:

```json
			"addFromPreset": "从预设添加",
```

- [ ] **Step 5: Verify the build and lint are green**

Run: `pnpm -C examples/web_ui/frontend build`
Expected: `tsc -b` + `vite build` succeed with no type errors.

Run: `pnpm -C examples/web_ui/frontend lint`
Expected: no lint errors in the changed files.

- [ ] **Step 6: Verify en/zh key parity**

Run:
```bash
cd examples/web_ui/frontend && node -e "const en=require('./src/i18n/locales/en.json'),zh=require('./src/i18n/locales/zh.json');const f=(o,p='')=>Object.entries(o).flatMap(([k,v])=>v&&typeof v==='object'?f(v,p+k+'.'):[p+k]);const E=new Set(f(en)),Z=new Set(f(zh));console.log('en-only',[...E].filter(x=>!Z.has(x)));console.log('zh-only',[...Z].filter(x=>!E.has(x)));"
```
Expected: `en-only []` and `zh-only []`.

- [ ] **Step 7: Commit**

```bash
git add examples/web_ui/frontend/src/api/types.ts examples/web_ui/frontend/src/api/subagent.ts examples/web_ui/frontend/src/pages/subagent/index.tsx examples/web_ui/frontend/src/i18n/locales/en.json examples/web_ui/frontend/src/i18n/locales/zh.json
git commit -m "feat(webui): add-from-preset control + carry id_source/patterns in sub-agent form

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

## Final verification (after all tasks)

- [ ] Run the full sub-agent backend suite:
  `python -m pytest tests/subagent_config_test.py tests/subagent_instance_registry_test.py tests/subagent_tool_test.py tests/subagent_agent_tools_test.py tests/subagent_presets_test.py tests/subagent_router_test.py -v`
  Expected: all PASS.
- [ ] `pre-commit run --all-files` (or at least on the changed files) is clean.
- [ ] Frontend `pnpm build` + `pnpm lint` green (from Task 7).
