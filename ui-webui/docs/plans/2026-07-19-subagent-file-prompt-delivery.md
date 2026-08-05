# Unified file-based prompt delivery — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Collapse the two prompt placeholders into one — the prompt always travels as a local file, the tool reads it and inlines the contents into `{prompt}` — so `{prompt}`-only CLIs (Claude Code, Codex) can be DAG nodes.

**Architecture:** Remove the `{prompt_file}` placeholder, the `command_uses_prompt_file` property, and the DAG `command_uses_prompt_file` guard. `CliSubAgentTool.call` materializes the prompt as a file (writing a temp file for direct calls; the DAG runner already writes one), reads it, and substitutes the contents into `{prompt}`. Config validation requires `{prompt}`.

**Tech Stack:** Python 3.11+, Pydantic v2, `unittest`/`pytest`.

## Global Constraints

- **Single prompt mechanism:** command templates contain **`{prompt}`** (and `{agent_id}` when stateful). No `{prompt_file}`. The `{prompt}` value ALWAYS comes from reading a file.
- **Direct calls** (no `prompt_file`): persist the inline prompt via `_write_temp_prompt`, then read it back — the write-then-read is an intentional invariant + audit file, not redundancy.
- **DAG calls:** the runner still renders each node's prompt, writes it to the node's prompt file, passes that path as `tool.call(prompt_file=…)`, and records it in the manifest. Only the sub-agent-shape guard is removed.
- **Security unchanged:** argv built token-by-token, no shell; the prompt occupies a single argv token.
- **Breaking edge (accepted):** an existing stored prototype whose command uses `{prompt_file}` now fails validation; the factory already skips malformed configs with a warning. No built-in preset is affected (presets use `{prompt}`).
- **No frontend change:** `/subagents` `cmdOk` already requires `{prompt}` and offers no `{prompt_file}` affordance (verified) — do not touch the frontend.
- **Style/CI:** black (line length **79**), flake8, pylint, mypy, docstring checks. Run `pre-commit run --files <changed files>` before every commit; fix the code, never disable checks. Backend tests: `conda run -n ravenx python -m pytest tests/<file> -v`.
- **Tests:** assert against the whole data structure; use `AnyString` from `tests/utils.py` for nondeterministic fields.
- **Shared-fake consequence:** because every `call()` now writes+reads a prompt file, the tool-test fake backends that only implement `exec_shell` (`_RecordingBackend`, `_CannedBackend`) and `_FakeBackend` (whose `read_file`/`write_file` currently `raise`) must gain working `join_path`/`write_file`/`read_file` (Task 3). With `read_file` returning what `write_file` stored, every existing argv assertion is unchanged (same prompt text in, same token out).
- **Commits:** `git add` only the files named in the task (never `git add -A`; never stage `.webapp_logs/*.pid`). End every commit message with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- **Task order matters:** Task 2 (remove the DAG guard) MUST precede Task 3 (remove the `command_uses_prompt_file` property) — otherwise the real-CLI integration test hits `AttributeError` between commits.

---

### Task 1: Config validation requires `{prompt}` (drop `{prompt_file}`)

**Files:**
- Modify: `src/agentscope/subagent/_base.py`
- Test: `tests/subagent_config_test.py`

**Interfaces:**
- Produces: `CliSubAgentConfig` now rejects a command/resume_command lacking `{prompt}`; `{prompt_file}` is no longer a recognized placeholder.

- [ ] **Step 1: Flip the two failing tests**

In `tests/subagent_config_test.py`, replace `test_prompt_file_placeholder_is_accepted` with:

```python
    def test_prompt_file_placeholder_is_rejected(self) -> None:
        """A {prompt_file}-only command (no {prompt}) is now rejected."""
        with self.assertRaises(ValidationError):
            CliSubAgentConfig(
                name="cc",
                description="d",
                command="claude --file {prompt_file}",
            )
```

Replace `test_stateful_prompt_file_placeholder_is_accepted` with:

```python
    def test_stateful_prompt_file_placeholder_is_rejected(self) -> None:
        """A stateful {prompt_file} command (no {prompt}) is now rejected."""
        with self.assertRaises(ValidationError):
            CliSubAgentConfig(
                name="cc",
                description="d",
                command="claude --file {prompt_file} --session {agent_id}",
                resume_command=(
                    "claude --file {prompt_file} --resume {agent_id}"
                ),
            )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n ravenx python -m pytest tests/subagent_config_test.py -v`
Expected: FAIL — the two renamed tests error because `{prompt_file}`-only commands are still accepted (no `ValidationError` raised).

- [ ] **Step 3: Update the validators in `_base.py`**

In `_require_prompt_placeholder`, change the docstring's second paragraph and the check body. Replace:

```python
        A command is valid when it contains ``{prompt}`` (inline
        delivery) or ``{prompt_file}`` (file delivery, used by the DAG
        orchestrator). At least one must be present.
```
with:
```python
        A command is valid when it contains ``{prompt}``. The prompt's
        contents are substituted for this placeholder (read from a file
        by the tool).
```

Replace:
```python
        if "{prompt}" not in value and "{prompt_file}" not in value:
            raise ValueError(
                "command template must contain '{prompt}' or "
                "'{prompt_file}'",
            )
        return value
```
with:
```python
        if "{prompt}" not in value:
            raise ValueError("command template must contain '{prompt}'")
        return value
```

In `_require_literal_executable`, replace:
```python
        if "{prompt}" in tokens[0] or "{prompt_file}" in tokens[0]:
```
with:
```python
        if "{prompt}" in tokens[0]:
```

In `_validate_stateful`, replace the per-template loop body's placeholder checks. Replace:
```python
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
```
with:
```python
            if "{prompt}" not in template:
                raise ValueError(
                    f"stateful {label} must contain '{{prompt}}'",
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
```

- [ ] **Step 4: Update the `{prompt_file}` docstrings in `_base.py`**

Class docstring — replace:
```python
    The ``command`` is a template whose ``{prompt}`` placeholder is
    replaced with the task the unified agent wants to delegate, e.g.
    ``claude -p {prompt} --dangerously-skip-permissions``. The prompt
    may instead be delivered via a ``{prompt_file}`` placeholder, which
    is replaced with the path to a file containing the task.
```
with:
```python
    The ``command`` is a template whose ``{prompt}`` placeholder is
    replaced with the task the unified agent wants to delegate, e.g.
    ``claude -p {prompt} --dangerously-skip-permissions``. The tool
    reads the prompt from a file and substitutes its contents for
    ``{prompt}``.
```

`command` field description — replace:
```python
            "Shell command template. Must contain '{prompt}' or "
            "'{prompt_file}', replaced with the delegated task or a "
            "path to a file containing it, respectively."
```
with:
```python
            "Shell command template. Must contain '{prompt}', replaced "
            "with the delegated task (read from a file by the tool)."
```

`command` field trailing docstring — replace:
```python
    """The command template containing ``{prompt}`` or ``{prompt_file}``."""
```
with:
```python
    """The command template containing ``{prompt}``."""
```

`resume_command` field description — replace:
```python
            "and `resume_command` must then contain '{agent_id}' and "
            "either '{prompt}' or '{prompt_file}'. When empty, the "
            "prototype is stateless."
```
with:
```python
            "and `resume_command` must then contain '{agent_id}' and "
            "'{prompt}'. When empty, the prototype is stateless."
```

`resume_command` field trailing docstring — replace:
```python
    """The optional resume command template containing ``{agent_id}``
    and either ``{prompt}`` or ``{prompt_file}``; presence makes the
    prototype stateful."""
```
with:
```python
    """The optional resume command template containing ``{agent_id}``
    and ``{prompt}``; presence makes the prototype stateful."""
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `conda run -n ravenx python -m pytest tests/subagent_config_test.py tests/subagent_presets_test.py tests/subagent_factory_test.py -v`
Expected: PASS (the two flipped tests now raise; presets/factory unaffected — they use `{prompt}`).

- [ ] **Step 6: Pre-commit + commit**

```bash
pre-commit run --files src/agentscope/subagent/_base.py tests/subagent_config_test.py
git add src/agentscope/subagent/_base.py tests/subagent_config_test.py
git commit -m "$(cat <<'EOF'
feat(subagent): require {prompt}; drop {prompt_file} from config validation

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: Remove the DAG `{prompt_file}` guard

**Files:**
- Modify: `src/agentscope/subagent/_dag/_runner.py`
- Test: `tests/subagent_dag_runner_test.py`, `tests/subagent_dag_tool_test.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `run_dag` no longer rejects a sub-agent for lacking `{prompt_file}`. Every valid config (which has `{prompt}`) may be a DAG node.

- [ ] **Step 1: Remove the guard test and its fake**

In `tests/subagent_dag_runner_test.py`, delete the `_FakeNonPromptFileSubAgent` class (the subclass with `command_uses_prompt_file = False`) and the test `test_non_prompt_file_subagent_rejected` in full.

- [ ] **Step 2: Run the runner tests to verify they still pass except the deleted one**

Run: `conda run -n ravenx python -m pytest tests/subagent_dag_runner_test.py -v`
Expected: PASS — the guard test is gone; the remaining tests still pass (the guard is still present in `_runner.py`, but no remaining test exercises the removed branch).

Note: if any remaining test still references `_FakeNonPromptFileSubAgent`, it was only the deleted test — confirm none remain (`grep -n _FakeNonPromptFileSubAgent tests/subagent_dag_runner_test.py` returns nothing).

- [ ] **Step 3: Remove the guard from `_runner.py`**

In `run_dag`, delete this block:

```python
        if not tool.command_uses_prompt_file:
            raise DagValidationError(
                f"sub-agent '{node.subagent}' must use '{{prompt_file}}' "
                f"to participate in a DAG",
            )
```

The surrounding loop keeps its unknown-sub-agent check:

```python
    for node in spec.nodes:
        tool = subagents.get(node.subagent)
        if tool is None:
            raise DagValidationError(
                f"node '{node.id}' names unknown sub-agent "
                f"'{node.subagent}'",
            )
```

In the `run_dag` docstring's `Raises:` section, remove the clause `a node's sub-agent does not use ``{prompt_file}``,` so it reads: "When the graph is invalid, a node names an unknown sub-agent, or ``max_concurrency`` is less than 1."

- [ ] **Step 4: Drop the now-dead `command_uses_prompt_file` attrs from the DAG test fakes**

In `tests/subagent_dag_runner_test.py`, remove the `command_uses_prompt_file = True` class attribute from `_FakeSubAgent`, `_TrackingSubAgent`, and `_FakeEmptySubAgent` (the runner no longer reads it; the fakes still read `prompt_file` to produce output).

In `tests/subagent_dag_tool_test.py`, remove the `command_uses_prompt_file = True` class attribute from `_FakeSubAgent`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `conda run -n ravenx python -m pytest tests/subagent_dag_runner_test.py tests/subagent_dag_tool_test.py tests/subagent_dag_integration_test.py -v`
Expected: PASS. The integration test still uses `cat {prompt_file}` and still works — the tool's `{prompt_file}` support is removed only in Task 3.

- [ ] **Step 6: Pre-commit + commit**

```bash
pre-commit run --files src/agentscope/subagent/_dag/_runner.py tests/subagent_dag_runner_test.py tests/subagent_dag_tool_test.py
git add src/agentscope/subagent/_dag/_runner.py tests/subagent_dag_runner_test.py tests/subagent_dag_tool_test.py
git commit -m "$(cat <<'EOF'
feat(subagent): remove the DAG {prompt_file} participation guard

Any valid sub-agent (all use {prompt}) may now be a DAG node; the tool
reads the node's prompt file.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Tool reads the prompt file into `{prompt}` (remove `{prompt_file}`)

**Files:**
- Modify: `src/agentscope/subagent/_tool.py`
- Test: `tests/subagent_tool_test.py`, `tests/subagent_dag_integration_test.py`

**Interfaces:**
- Consumes: the DAG guard is gone (Task 2); config requires `{prompt}` (Task 1).
- Produces: `CliSubAgentTool.call` resolves the prompt from a file (temp file for direct calls) and inlines it into `{prompt}`. `command_uses_prompt_file` and `_build_argv`'s `prompt_file` parameter are removed. `_build_argv(prompt, template, agent_id=None)` is the new signature.

- [ ] **Step 1: Give the shared fake backends file I/O, then rework the delivery tests**

First, `import posixpath` at the top of `tests/subagent_tool_test.py` (next to the other stdlib imports).

Then update the two plain fake backends so a temp-prompt write+read works. In `_RecordingBackend`, change `__init__` and append three methods:

```python
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.written: dict[str, bytes] = {}

    async def exec_shell(  # pylint: disable=unused-argument
        self,
        argv: list[str],
        cwd: str | None = None,
        timeout: float | None = None,
    ) -> ExecResult:
        """Record argv and return a successful ExecResult."""
        self.calls.append(argv)
        return ExecResult(exit_code=0, stdout=b"ok", stderr=b"")

    def join_path(self, *parts: str) -> str:
        """Join path parts (posix)."""
        return posixpath.join(*parts)

    async def read_file(self, path: str) -> bytes:
        """Return the previously written payload."""
        return self.written[path]

    async def write_file(self, path: str, data: bytes) -> None:
        """Record the written payload by path."""
        self.written[path] = data
```

In `_CannedBackend`, add `self.written: dict[str, bytes] = {}` to `__init__` (after `self.calls = []`) and append the same three methods (`join_path`, `read_file`, `write_file`) verbatim as above.

In `_FakeBackend`, add `self.written: dict[str, bytes] = {}` to `__init__` and replace the two raising stubs (`join_path` is inherited from `BackendBase`):

```python
    async def read_file(self, path: str) -> bytes:
        """Return the previously written payload."""
        return self.written[path]

    async def write_file(self, path: str, data: bytes) -> None:
        """Record the written payload by path."""
        self.written[path] = data
```

Finally, replace the entire `CliSubAgentToolPromptFileTest` class with the following (keeps the 128k constant check, replaces `cat {prompt_file}` delivery with the new `{prompt}` + file-read model, and gives each inner fake a `read_file`):

```python
class CliSubAgentToolPromptDeliveryTest(unittest.TestCase):
    """The {prompt} value always comes from reading a file."""

    def test_provided_prompt_file_contents_become_prompt(self) -> None:
        """A supplied prompt_file is read; its contents fill {prompt}."""

        class _Backend:
            """Fake backend recording argv/reads/writes."""

            def __init__(self) -> None:
                self.calls: list[list[str]] = []
                self.written: dict[str, bytes] = {}
                self.reads: list[str] = []

            async def exec_shell(  # pylint: disable=unused-argument
                self,
                argv: list[str],
                cwd: str | None = None,
                timeout: float | None = None,
            ) -> ExecResult:
                """Record argv and return a fixed successful result."""
                self.calls.append(argv)
                return ExecResult(exit_code=0, stdout=b"RESULT", stderr=b"")

            async def read_file(self, path: str) -> bytes:
                """Record the read and return fixed file contents."""
                self.reads.append(path)
                return b"FILE PROMPT"

            async def write_file(self, path: str, data: bytes) -> None:
                """Record the written payload by path."""
                self.written[path] = data

        backend = _Backend()
        tool = CliSubAgentTool(
            name="cc",
            description="d",
            command="mycli {prompt}",
            backend=backend,
        )
        chunks = asyncio.run(
            _collect(
                tool.call(
                    prompt="ignored inline",
                    prompt_file="/w/n.prompt.md",
                    output_file="/w/n.out.md",
                ),
            ),
        )
        # The file was read; its contents (not the inline arg) were inlined.
        self.assertEqual(backend.reads, ["/w/n.prompt.md"])
        self.assertEqual(backend.calls, [["mycli", "FILE PROMPT"]])
        self.assertEqual(backend.written["/w/n.out.md"], b"RESULT")
        self.assertEqual(chunks[-1].content[0].text, "RESULT")

    def test_direct_call_writes_then_reads_temp_prompt(self) -> None:
        """No prompt_file: the inline prompt is persisted, then read back."""
        import posixpath

        class _Backend:
            """Fake backend recording argv/writes; reads what it wrote."""

            def __init__(self) -> None:
                self.calls: list[list[str]] = []
                self.written: dict[str, bytes] = {}

            async def exec_shell(  # pylint: disable=unused-argument
                self,
                argv: list[str],
                cwd: str | None = None,
                timeout: float | None = None,
            ) -> ExecResult:
                """Record argv and return a fixed successful result."""
                self.calls.append(argv)
                return ExecResult(exit_code=0, stdout=b"RESULT", stderr=b"")

            def join_path(self, *parts: str) -> str:
                """Delegate to posixpath.join for the temp prompt path."""
                return posixpath.join(*parts)

            async def read_file(self, path: str) -> bytes:
                """Return the previously written payload."""
                return self.written[path]

            async def write_file(self, path: str, data: bytes) -> None:
                """Record the written payload by path."""
                self.written[path] = data

        backend = _Backend()
        tool = CliSubAgentTool(
            name="cc",
            description="d",
            command="mycli {prompt}",
            backend=backend,
            cwd="/work",
        )
        chunks = asyncio.run(_collect(tool.call(prompt="the real prompt")))
        temp_paths = [
            p
            for p in backend.written
            if p.startswith("/work/.ravenx_prompt_")
        ]
        self.assertEqual(len(temp_paths), 1)
        self.assertEqual(backend.written[temp_paths[0]], b"the real prompt")
        self.assertEqual(backend.calls, [["mycli", "the real prompt"]])
        self.assertEqual(chunks[-1].content[0].text, "RESULT")

    def test_max_output_chars_is_128k(self) -> None:
        """The in-context return cap is 128000."""
        from agentscope.subagent._tool import _MAX_OUTPUT_CHARS

        self.assertEqual(_MAX_OUTPUT_CHARS, 128000)

    def test_output_file_gets_full_text_chunk_is_truncated(self) -> None:
        """output_file holds the full text; the returned chunk is capped."""

        class _Backend:
            """Fake backend returning oversized stdout; reads/writes files."""

            def __init__(self) -> None:
                self.written: dict[str, bytes] = {}

            async def exec_shell(  # pylint: disable=unused-argument
                self,
                argv: list[str],
                cwd: str | None = None,
                timeout: float | None = None,
            ) -> ExecResult:
                """Return an oversized successful result."""
                return ExecResult(
                    exit_code=0,
                    stdout=b"X" * 130000,
                    stderr=b"",
                )

            async def read_file(self, path: str) -> bytes:
                """Return fixed prompt-file contents."""
                return b"p"

            async def write_file(self, path: str, data: bytes) -> None:
                """Record the written payload by path."""
                self.written[path] = data

        backend = _Backend()
        tool = CliSubAgentTool(
            name="cc",
            description="d",
            command="mycli {prompt}",
            backend=backend,
        )
        chunks = asyncio.run(
            _collect(
                tool.call(
                    prompt="ignored",
                    prompt_file="/w/n.prompt.md",
                    output_file="/w/n.out.md",
                ),
            ),
        )
        self.assertEqual(backend.written["/w/n.out.md"], b"X" * 130000)
        text = chunks[-1].content[0].text
        suffix = "... (output truncated)"
        self.assertEqual(text, "X" * 128000 + f"\n{suffix}")

    def test_output_file_not_written_on_nonzero_exit(self) -> None:
        """A non-zero exit yields ERROR and does not write output_file."""

        class _Backend:
            """Fake backend returning a failed result; reads/writes files."""

            def __init__(self) -> None:
                self.written: dict[str, bytes] = {}

            async def exec_shell(  # pylint: disable=unused-argument
                self,
                argv: list[str],
                cwd: str | None = None,
                timeout: float | None = None,
            ) -> ExecResult:
                """Return a non-zero exit result."""
                return ExecResult(exit_code=1, stdout=b"partial", stderr=b"boom")

            async def read_file(self, path: str) -> bytes:
                """Return fixed prompt-file contents."""
                return b"p"

            async def write_file(self, path: str, data: bytes) -> None:
                """Record the written payload by path."""
                self.written[path] = data

        backend = _Backend()
        tool = CliSubAgentTool(
            name="cc",
            description="d",
            command="mycli {prompt}",
            backend=backend,
        )
        chunks = asyncio.run(
            _collect(
                tool.call(
                    prompt="ignored",
                    prompt_file="/w/n.prompt.md",
                    output_file="/w/n.out.md",
                ),
            ),
        )
        self.assertEqual(chunks[-1].state, ToolResultState.ERROR)
        self.assertNotIn("/w/n.out.md", backend.written)
```

- [ ] **Step 2: Run the tool tests to verify the reworked ones fail, others still pass**

Run: `conda run -n ravenx python -m pytest tests/subagent_tool_test.py -v`
Expected: the new `CliSubAgentToolPromptDeliveryTest` cases FAIL — the tool does not yet read `prompt_file` into `{prompt}` (`test_provided_prompt_file_contents_become_prompt` sees argv `["mycli", "ignored inline"]` and `reads == []`; `test_direct_call_writes_then_reads_temp_prompt` sees no temp file written). All OTHER tool tests (stateless/stateful/derived/codex_jsonl/output/lookup) still PASS: the old tool only writes a temp file for `{prompt_file}` commands (none here), so the newly-added fake methods are dormant.

- [ ] **Step 3: Remove `command_uses_prompt_file` from `_tool.py`**

Delete the whole property:

```python
    @property
    def command_uses_prompt_file(self) -> bool:
        """Whether the command delivers the prompt via a file.

        Returns:
            `bool`:
                ``True`` when the create command (and the resume
                command, when stateful) contains ``{prompt_file}``.
        """
        if "{prompt_file}" not in self._command:
            return False
        if self._resume_command is not None:
            return "{prompt_file}" in self._resume_command
        return True
```

- [ ] **Step 4: Drop `{prompt_file}` from `_build_argv`**

Change the signature and body. Replace:

```python
    def _build_argv(
        self,
        prompt: str,
        template: str,
        agent_id: str | None = None,
        prompt_file: str | None = None,
    ) -> list[str]:
        """Build the argv for a delegated ``prompt``.

        Substitutes ``{prompt}`` (and ``{agent_id}``/``{prompt_file}``
        when provided) inside each token, then prepends an
        ``env KEY=VAL`` prefix when env vars are configured.

        Args:
            prompt (`str`):
                The task to delegate.
            template (`str`):
                The command template to expand.
            agent_id (`str | None`, optional):
                The CLI session id substituted for ``{agent_id}``.
            prompt_file (`str | None`, optional):
                The path substituted for ``{prompt_file}``.

        Returns:
            `list[str]`:
                The argv to run without a shell.
        """
        argv = []
        for tok in shlex.split(template):
            tok = tok.replace("{prompt}", prompt)
            if agent_id is not None:
                tok = tok.replace("{agent_id}", agent_id)
            if prompt_file is not None:
                tok = tok.replace("{prompt_file}", prompt_file)
            argv.append(tok)
        if self._env:
            argv = [
                "env",
                *(f"{key}={value}" for key, value in self._env.items()),
                *argv,
            ]
        return argv
```
with:
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
                The prompt text substituted for ``{prompt}``.
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

- [ ] **Step 5: Update `_write_temp_prompt`'s docstring**

Replace its `Returns:` text:
```python
        Returns:
            `str`:
                The path passed to the sub-agent for ``{prompt_file}``.
```
with:
```python
        Returns:
            `str`:
                The path of the written prompt file (read back to fill
                ``{prompt}``).
```

- [ ] **Step 6: Rewrite the prompt-resolution block in `call()`**

Update the `call()` docstring's `prompt_file` arg entry — replace:
```python
            prompt_file (`str | None`, optional):
                The path substituted for ``{prompt_file}``. When the
                template uses ``{prompt_file}`` and this is ``None``,
                ``prompt`` is written to a generated temp file first.
```
with:
```python
            prompt_file (`str | None`, optional):
                Path to a file holding the prompt. When ``None`` (a
                direct call), ``prompt`` is written to a generated temp
                file. The file's contents are read and substituted for
                ``{prompt}``.
```

Then replace the delivery block:
```python
        if "{prompt_file}" in template and prompt_file is None:
            prompt_file = await self._write_temp_prompt(prompt)

        argv = self._build_argv(prompt, template, agent_id, prompt_file)
```
with:
```python
        try:
            if prompt_file is None:
                prompt_file = await self._write_temp_prompt(prompt)
            prompt_text = (
                await self._backend.read_file(prompt_file)
            ).decode("utf-8")
        except Exception as exc:  # noqa: BLE001
            yield ToolChunk(
                content=[
                    TextBlock(
                        text=f"Sub-Agent prompt file error: {exc}",
                    ),
                ],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        argv = self._build_argv(prompt_text, template, agent_id)
```

- [ ] **Step 7: Run the tool tests to verify they pass**

Run: `conda run -n ravenx python -m pytest tests/subagent_tool_test.py -v`
Expected: PASS (the reworked `CliSubAgentToolPromptDeliveryTest` plus all other tool tests — the stateless/stateful/derived/codex_jsonl tests are unaffected because they pass no `prompt_file`, so the tool writes+reads a temp file and inlines the same text).

- [ ] **Step 8: Switch the integration test to a `{prompt}` CLI**

In `tests/subagent_dag_integration_test.py`, change the sub-agent command and the class/docstring wording. Replace:
```python
class SubAgentDagIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """A real `cat {prompt_file}` sub-agent runs a diamond DAG."""
```
with:
```python
class SubAgentDagIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """A real `printf %s {prompt}` sub-agent runs a diamond DAG."""
```
and replace:
```python
            cat = CliSubAgentTool(
                name="cat",
                description="echo prompt file",
                command="cat {prompt_file}",
                cwd=workdir,
                backend=backend,
            )
```
with:
```python
            cat = CliSubAgentTool(
                name="cat",
                description="echo the prompt",
                command="printf %s {prompt}",
                cwd=workdir,
                backend=backend,
            )
```

(The rest is unchanged: `printf %s <prompt>` echoes the tool-inlined prompt exactly as `cat` echoed the file, so the diamond-DAG assertions — `term["text"].startswith("B<SEED>|C<")`, `endswith(">")`, and `b_out == b"B<SEED>"` — still hold. This modified end-to-end test is the regression proof that a `{prompt}`-only sub-agent now runs as a DAG node.)

- [ ] **Step 9: Run the integration + full sub-agent suite**

Run: `conda run -n ravenx python -m pytest tests/subagent_dag_integration_test.py tests/subagent_tool_test.py tests/subagent_dag_tool_test.py tests/subagent_dag_runner_test.py -v`
Expected: PASS.

- [ ] **Step 10: Pre-commit + commit**

```bash
pre-commit run --files src/agentscope/subagent/_tool.py tests/subagent_tool_test.py tests/subagent_dag_integration_test.py
git add src/agentscope/subagent/_tool.py tests/subagent_tool_test.py tests/subagent_dag_integration_test.py
git commit -m "$(cat <<'EOF'
feat(subagent): read the prompt file into {prompt}; drop {prompt_file}

The tool materializes the prompt as a file (temp file for direct calls),
reads it, and inlines the contents into {prompt}. Removes the
{prompt_file} placeholder and command_uses_prompt_file. claude/codex
({prompt}-only) now run as DAG nodes.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Final verification (after all tasks)

- [ ] Run the whole sub-agent backend suite:

```bash
conda run -n ravenx python -m pytest tests/subagent_config_test.py tests/subagent_factory_test.py tests/subagent_transcript_test.py tests/subagent_tool_test.py tests/subagent_agent_tools_test.py tests/subagent_presets_test.py tests/subagent_router_test.py tests/subagent_dag_tool_test.py tests/subagent_dag_graph_test.py tests/subagent_dag_placeholders_test.py tests/subagent_dag_render_test.py tests/subagent_dag_runner_test.py tests/subagent_dag_store_test.py tests/subagent_dag_integration_test.py tests/subagent_instance_registry_test.py -q
```

Expected: all green.

- [ ] Confirm no residual `{prompt_file}` / `command_uses_prompt_file` in source: `grep -rn "prompt_file\|command_uses_prompt_file" src/agentscope/subagent/` should show ONLY the DAG runner's internal prompt-file bookkeeping (the `DagRunResult.prompt_file` manifest field, `store.prompt_path`, and `tool.call(prompt_file=…)`) — NOT the removed placeholder/guard/property.
- [ ] Dispatch the final whole-branch review (superpowers:requesting-code-review) against `git merge-base main HEAD`.

## Notes for the executor

- **Task order 1 → 2 → 3 is mandatory** (Task 2 removes the DAG guard before Task 3 removes the property, so the real-CLI integration test never hits `AttributeError`).
- Do NOT touch the frontend — `cmdOk` already requires `{prompt}` (verified at `examples/web_ui/frontend/src/pages/subagent/index.tsx:176-177`).
- Do NOT stage `.webapp_logs/*.pid`. Never `git add -A`.
- The DAG runner keeps writing each node's prompt file and passing its path; only the shape guard is removed.
