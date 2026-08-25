# Sub-Agent DAG Orchestration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the main agent orchestrate a DAG of CLI sub-agents that pass messages through local workspace files, with the main agent as the graph's sink.

**Architecture:** A new `subagent/_dag/` sub-package exposes a `run_subagent_dag` `ToolBase`. It parses a graph spec, runs a deterministic ready-set scheduler over existing `CliSubAgentTool`s, and passes inter-node messages through files written via the session workspace `BackendBase`. Prompts are delivered by file (`{prompt_file}`); each node's stdout is captured to `<node>.out.md`; the main agent receives terminal outputs (full) plus the file manifest. All changes are additive; the existing star-topology sub-agent path is untouched.

**Tech Stack:** Python 3.11+, pydantic v2, `asyncio`, `unittest`/`IsolatedAsyncioTestCase`, existing `agentscope.tool.BackendBase`/`CliSubAgentTool`.

## Global Constraints

- Python 3.11+; black line length **79**; flake8 / pylint / mypy / docstring checks must pass (`pre-commit run --all-files`).
- Docstrings **English only**, strict `Args:` / `Returns:` template with backtick-typed params.
- Internal files/classes/functions are **`_`-prefixed**; public surface is re-exported only through `__init__.py`.
- **Lazy imports** for any non-core third-party lib (none expected here; `time`/`uuid`/`json`/`re`/`asyncio` are stdlib and may be imported at file top).
- Tests: assert the **whole data structure**; use `AnyString`/`AnyValue` from `tests/utils.py` for nondeterministic fields (`run_id`, timestamps, paths).
- Test files are named `tests/*_test.py`. Commit messages use Conventional Commits.
- Additive-only: do not change `agent/_agent.py` or the `app/` team model.

---

### Task 1: Relax `CliSubAgentConfig` to accept `{prompt_file}`

**Files:**
- Modify: `src/agentscope/subagent/_base.py:103-189`
- Test: `tests/subagent_config_test.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a `CliSubAgentConfig` whose `command`/`resume_command` are valid when they contain `{prompt}` **or** `{prompt_file}` (stateful still requires `{agent_id}`).

- [ ] **Step 1: Write the failing test**

Append to `tests/subagent_config_test.py`:

```python
def test_prompt_file_placeholder_is_accepted() -> None:
    """A {prompt_file}-only command (no {prompt}) is valid."""
    from agentscope.subagent import CliSubAgentConfig

    cfg = CliSubAgentConfig(
        name="cc",
        description="d",
        command="claude --file {prompt_file}",
    )
    assert cfg.command == "claude --file {prompt_file}"


def test_command_without_any_prompt_placeholder_is_rejected() -> None:
    """A command with neither placeholder is rejected."""
    import pytest

    from agentscope.subagent import CliSubAgentConfig

    with pytest.raises(ValueError):
        CliSubAgentConfig(name="cc", description="d", command="claude run")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/subagent_config_test.py::test_prompt_file_placeholder_is_accepted -v`
Expected: FAIL — `command template must contain the '{prompt}' placeholder`.

- [ ] **Step 3: Implement the relaxation**

In `src/agentscope/subagent/_base.py`, replace the body of `_require_prompt_placeholder` (currently lines ~103-120):

```python
    @field_validator("command")
    @classmethod
    def _require_prompt_placeholder(cls, value: str) -> str:
        """Ensure the command carries a prompt-delivery placeholder.

        A command is valid when it contains ``{prompt}`` (inline
        delivery) or ``{prompt_file}`` (file delivery, used by the DAG
        orchestrator). At least one must be present.

        Args:
            value (`str`):
                The command template to validate.

        Returns:
            `str`:
                The validated command template.
        """
        if "{prompt}" not in value and "{prompt_file}" not in value:
            raise ValueError(
                "command template must contain '{prompt}' or "
                "'{prompt_file}'",
            )
        return value
```

In `_require_literal_executable` (currently ~122-153), broaden the first-token guard so neither placeholder may be the executable:

```python
        if "{prompt}" in tokens[0] or "{prompt_file}" in tokens[0]:
            raise ValueError(
                "command's first token must be a literal executable, "
                "not a prompt placeholder",
            )
```

In `_validate_stateful` (currently ~155-189), replace the inner placeholder loop so each stateful template must contain `{agent_id}` **and** at least one of `{prompt}`/`{prompt_file}`:

```python
        for label, template in (
            ("command", self.command),
            ("resume_command", self.resume_command),
        ):
            if "{agent_id}" not in template:
                raise ValueError(
                    f"stateful {label} must contain the '{{agent_id}}' "
                    f"placeholder",
                )
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
            if not tokens or "{prompt}" in tokens[0] or (
                "{prompt_file}" in tokens[0]
            ):
                raise ValueError(
                    f"{label}'s first token must be a literal executable",
                )
        return self
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/subagent_config_test.py -v`
Expected: PASS (new tests + all pre-existing config tests).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_base.py tests/subagent_config_test.py
git commit -m "feat(subagent): accept {prompt_file} in CLI sub-agent command config"
```

---

### Task 2: Extend `CliSubAgentTool` with `{prompt_file}` + output capture

**Files:**
- Modify: `src/agentscope/subagent/_tool.py` (`_MAX_OUTPUT_CHARS`, `_build_argv`, `call`, add `command_uses_prompt_file`)
- Test: `tests/subagent_tool_test.py`

**Interfaces:**
- Consumes: `BackendBase.write_file(path, data: bytes)`.
- Produces:
  - `CliSubAgentTool.call(prompt, instance=None, prompt_file=None, output_file=None)` — when the command template uses `{prompt_file}`, the path is substituted (a temp file is written from `prompt` when `prompt_file` is `None`); when `output_file` is set, the **full untruncated** stdout is written there.
  - `CliSubAgentTool.command_uses_prompt_file: bool` — property; `True` when the create command (and resume command, if stateful) contains `{prompt_file}`.
  - `_MAX_OUTPUT_CHARS == 128000`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/subagent_tool_test.py`:

```python
class CliSubAgentToolPromptFileTest(unittest.TestCase):
    """{prompt_file} delivery + full-output capture."""

    def test_prompt_file_substituted_and_output_written(self) -> None:
        """prompt_file path lands in argv; full stdout is written."""

        class _Backend:
            def __init__(self) -> None:
                self.calls: list[list[str]] = []
                self.written: dict[str, bytes] = {}

            async def exec_shell(self, argv, cwd=None, timeout=None):
                self.calls.append(argv)
                return ExecResult(exit_code=0, stdout=b"RESULT", stderr=b"")

            async def write_file(self, path, data) -> None:
                self.written[path] = data

        backend = _Backend()
        tool = CliSubAgentTool(
            name="cc",
            description="d",
            command="cat {prompt_file}",
            backend=backend,
        )
        self.assertTrue(tool.command_uses_prompt_file)
        chunks = asyncio.run(
            _collect(
                tool.call(
                    prompt="ignored",
                    prompt_file="/w/n.prompt.md",
                    output_file="/w/n.out.md",
                ),
            ),
        )
        self.assertEqual(backend.calls, [["cat", "/w/n.prompt.md"]])
        self.assertEqual(backend.written["/w/n.out.md"], b"RESULT")
        self.assertEqual(chunks[-1].content[0].text, "RESULT")

    def test_max_output_chars_is_128k(self) -> None:
        """The in-context return cap is 128000."""
        from agentscope.subagent._tool import _MAX_OUTPUT_CHARS

        self.assertEqual(_MAX_OUTPUT_CHARS, 128000)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/subagent_tool_test.py::CliSubAgentToolPromptFileTest -v`
Expected: FAIL — `command_uses_prompt_file` attribute missing / `_MAX_OUTPUT_CHARS` is 30000.

- [ ] **Step 3: Implement the changes**

In `src/agentscope/subagent/_tool.py`:

Change the constant:

```python
_MAX_OUTPUT_CHARS = 128000
```

Add a property after `is_stateful` (near line 92):

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

Extend `_build_argv` to substitute `{prompt_file}` — add a `prompt_file` parameter and one replace line:

```python
    def _build_argv(
        self,
        prompt: str,
        template: str,
        agent_id: str | None = None,
        prompt_file: str | None = None,
    ) -> list[str]:
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

(Keep the existing docstring; add `prompt_file (\`str | None\`, optional): The path substituted for \`{prompt_file}\`.` under `Args:`.)

Update `call` signature and body. Replace the signature (line ~167) and add prompt-file materialization + output capture. New signature:

```python
    async def call(  # type: ignore[override]
        self,
        prompt: str,
        instance: str | None = None,
        prompt_file: str | None = None,
        output_file: str | None = None,
    ) -> AsyncGenerator[ToolChunk, None]:
```

Just before building argv (after the `metadata`/instance block that decides `template`), materialize a temp prompt file when needed, and thread `prompt_file` through both `_build_argv` calls:

```python
        if "{prompt_file}" in template and prompt_file is None:
            prompt_file = await self._write_temp_prompt(prompt)
```

For the stateful branch use:
`argv = self._build_argv(prompt, template, agent_id, prompt_file)`
For the stateless branch use:
`argv = self._build_argv(prompt, self._command, uuid.uuid4().hex, prompt_file)`
(the stateless branch must compute `template = self._command` before the materialization line, so restructure so `template` is set in both branches before the `{prompt_file}` check).

Add the helper method:

```python
    async def _write_temp_prompt(self, prompt: str) -> str:
        """Write ``prompt`` to a temp file under ``cwd`` and return its path.

        Args:
            prompt (`str`):
                The prompt text to persist.

        Returns:
            `str`:
                The path passed to the sub-agent for ``{prompt_file}``.
        """
        base = self._cwd or "."
        path = self._backend.join_path(
            base,
            f".ravenx_prompt_{uuid.uuid4().hex}.md",
        )
        await self._backend.write_file(path, prompt.encode("utf-8"))
        return path
```

On the success path, write the **full** (untruncated) output before truncating the returned chunk. Replace the tail of `call` (from `output = stdout` onward) so that after computing the full `output` string but **before** the `_MAX_OUTPUT_CHARS` truncation:

```python
        output = stdout
        if stderr:
            output = f"{output}\n{stderr}" if output else stderr
        if output_file is not None:
            await self._backend.write_file(
                output_file,
                output.encode("utf-8"),
            )
        if len(output) > _MAX_OUTPUT_CHARS:
            output = output[:_MAX_OUTPUT_CHARS] + "\n... (output truncated)"
```

(The error/timeout early-returns are unchanged; `output_file` is written only on the success path.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/subagent_tool_test.py -v`
Expected: PASS (new tests + all pre-existing tool tests, including `{prompt}` back-compat).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_tool.py tests/subagent_tool_test.py
git commit -m "feat(subagent): {prompt_file} delivery + full-output capture in CliSubAgentTool"
```

---

### Task 3: DAG errors + placeholder grammar parser

**Files:**
- Create: `src/agentscope/subagent/_dag/__init__.py` (empty for now, package marker)
- Create: `src/agentscope/subagent/_dag/_errors.py`
- Create: `src/agentscope/subagent/_dag/_placeholders.py`
- Test: `tests/subagent_dag_placeholders_test.py`

**Interfaces:**
- Produces:
  - `DagValidationError(ValueError)`.
  - `Placeholder` dataclass: `kind: str` (`"input" | "input_path" | "output" | "output_path" | "ref" | "ref_path"`), `name: str`, `raw: str`.
  - `parse_placeholders(template: str) -> list[Placeholder]` — raises `DagValidationError` on a malformed `{{ ... }}`.

- [ ] **Step 1: Write the failing test**

Create `tests/subagent_dag_placeholders_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for the DAG placeholder grammar parser."""

import pytest

from agentscope.subagent._dag._errors import DagValidationError
from agentscope.subagent._dag._placeholders import (
    Placeholder,
    parse_placeholders,
)


def test_parses_every_placeholder_kind() -> None:
    """All six placeholder forms parse to the right kind + name."""
    template = (
        "{{ inputs.topic }} {{ inputs.spec.path }} "
        "{{ collect.output }} {{ collect.output_path }} "
        "{{ ref:r1/collect.out.md }} {{ ref_path:r1/collect.out.md }}"
    )
    assert parse_placeholders(template) == [
        Placeholder(kind="input", name="topic", raw="{{ inputs.topic }}"),
        Placeholder(
            kind="input_path", name="spec", raw="{{ inputs.spec.path }}"
        ),
        Placeholder(kind="output", name="collect", raw="{{ collect.output }}"),
        Placeholder(
            kind="output_path",
            name="collect",
            raw="{{ collect.output_path }}",
        ),
        Placeholder(
            kind="ref", name="r1/collect.out.md", raw="{{ ref:r1/collect.out.md }}"
        ),
        Placeholder(
            kind="ref_path",
            name="r1/collect.out.md",
            raw="{{ ref_path:r1/collect.out.md }}",
        ),
    ]


def test_malformed_placeholder_raises() -> None:
    """An unrecognized placeholder body is rejected."""
    with pytest.raises(DagValidationError):
        parse_placeholders("{{ bogus.value }}")


def test_no_placeholders_returns_empty() -> None:
    """A plain template yields no placeholders."""
    assert parse_placeholders("just text") == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/subagent_dag_placeholders_test.py -v`
Expected: FAIL — `ModuleNotFoundError: agentscope.subagent._dag`.

- [ ] **Step 3: Create the package + modules**

Create `src/agentscope/subagent/_dag/__init__.py`:

```python
# -*- coding: utf-8 -*-
"""File-based sub-agent DAG orchestration (internal package)."""
```

Create `src/agentscope/subagent/_dag/_errors.py`:

```python
# -*- coding: utf-8 -*-
"""Errors raised while validating or running a sub-agent DAG."""


class DagValidationError(ValueError):
    """Raised when a submitted DAG spec is structurally invalid."""
```

Create `src/agentscope/subagent/_dag/_placeholders.py`:

```python
# -*- coding: utf-8 -*-
"""Grammar for the template placeholders used in DAG node prompts."""

import re
from dataclasses import dataclass

from ._errors import DagValidationError

_PLACEHOLDER_RE = re.compile(r"\{\{(.*?)\}\}")
_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+$")


@dataclass(frozen=True)
class Placeholder:
    """A single parsed ``{{ ... }}`` reference in a prompt template.

    Attributes:
        kind (`str`):
            One of ``"input"``, ``"input_path"``, ``"output"``,
            ``"output_path"``, ``"ref"``, ``"ref_path"``.
        name (`str`):
            The input key, dependency id, or file path referenced.
        raw (`str`):
            The exact ``{{ ... }}`` substring, used for substitution.
    """

    kind: str
    name: str
    raw: str


def parse_placeholders(template: str) -> list[Placeholder]:
    """Parse all ``{{ ... }}`` references from a prompt template.

    Args:
        template (`str`):
            The prompt template to scan.

    Returns:
        `list[Placeholder]`:
            The placeholders in order of appearance.

    Raises:
        `DagValidationError`:
            When a ``{{ ... }}`` body does not match the grammar.
    """
    result: list[Placeholder] = []
    for match in _PLACEHOLDER_RE.finditer(template):
        raw = match.group(0)
        body = match.group(1).strip()
        result.append(_parse_body(body, raw))
    return result


def _parse_body(body: str, raw: str) -> Placeholder:
    """Parse the inside of one ``{{ ... }}`` into a :class:`Placeholder`.

    Args:
        body (`str`):
            The trimmed text between the braces.
        raw (`str`):
            The full ``{{ ... }}`` substring.

    Returns:
        `Placeholder`:
            The parsed placeholder.

    Raises:
        `DagValidationError`:
            When ``body`` does not match a known form.
    """
    for prefix, kind in (("ref:", "ref"), ("ref_path:", "ref_path")):
        if body.startswith(prefix):
            path = body[len(prefix):].strip()
            if not path:
                raise DagValidationError(f"empty path in '{raw}'")
            return Placeholder(kind=kind, name=path, raw=raw)

    parts = body.split(".")
    if len(parts) == 2 and parts[0] == "inputs" and _NAME_RE.match(parts[1]):
        return Placeholder(kind="input", name=parts[1], raw=raw)
    if (
        len(parts) == 3
        and parts[0] == "inputs"
        and _NAME_RE.match(parts[1])
        and parts[2] == "path"
    ):
        return Placeholder(kind="input_path", name=parts[1], raw=raw)
    if len(parts) == 2 and _NAME_RE.match(parts[0]) and parts[1] == "output":
        return Placeholder(kind="output", name=parts[0], raw=raw)
    if (
        len(parts) == 2
        and _NAME_RE.match(parts[0])
        and parts[1] == "output_path"
    ):
        return Placeholder(kind="output_path", name=parts[0], raw=raw)
    raise DagValidationError(f"unrecognized placeholder '{raw}'")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/subagent_dag_placeholders_test.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_dag/__init__.py src/agentscope/subagent/_dag/_errors.py src/agentscope/subagent/_dag/_placeholders.py tests/subagent_dag_placeholders_test.py
git commit -m "feat(subagent): DAG errors + template placeholder parser"
```

---

### Task 4: DAG spec models + structural validation

**Files:**
- Create: `src/agentscope/subagent/_dag/_graph.py`
- Test: `tests/subagent_dag_graph_test.py`

**Interfaces:**
- Consumes: `parse_placeholders` (Task 3), `DagValidationError` (Task 3).
- Produces:
  - `DagNodeSpec` (pydantic): `id: str`, `subagent: str`, `prompt_template: str`, `depends_on: list[str] = []`, `inputs: dict[str, str | dict] = {}`, `instance: str | None = None`.
  - `SubAgentDagSpec` (pydantic): `nodes: list[DagNodeSpec]`.
  - `parse_dag_spec(data: dict) -> SubAgentDagSpec`.
  - `validate_and_order(spec: SubAgentDagSpec) -> list[str]` — topologically ordered node ids; raises `DagValidationError` on duplicate ids, unresolved deps, cycles, or a placeholder that references an undeclared dependency / unknown input key (default-deny).

- [ ] **Step 1: Write the failing test**

Create `tests/subagent_dag_graph_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for DAG spec parsing and structural validation."""

import pytest

from agentscope.subagent._dag._errors import DagValidationError
from agentscope.subagent._dag._graph import (
    parse_dag_spec,
    validate_and_order,
)


def _spec(nodes: list[dict]):
    """Parse a nodes list into a spec."""
    return parse_dag_spec({"nodes": nodes})


def test_valid_diamond_orders_topologically() -> None:
    """A → B,C → D returns ids with deps before dependents."""
    spec = _spec(
        [
            {"id": "A", "subagent": "s", "prompt_template": "go"},
            {
                "id": "B",
                "subagent": "s",
                "depends_on": ["A"],
                "prompt_template": "{{ A.output }}",
            },
            {
                "id": "C",
                "subagent": "s",
                "depends_on": ["A"],
                "prompt_template": "{{ A.output_path }}",
            },
            {
                "id": "D",
                "subagent": "s",
                "depends_on": ["B", "C"],
                "prompt_template": "{{ B.output }} {{ C.output }}",
            },
        ],
    )
    order = validate_and_order(spec)
    assert order.index("A") < order.index("B")
    assert order.index("A") < order.index("C")
    assert order.index("B") < order.index("D")
    assert order.index("C") < order.index("D")


def test_cycle_is_rejected() -> None:
    """A ↔ B is rejected as a cycle."""
    spec = _spec(
        [
            {
                "id": "A",
                "subagent": "s",
                "depends_on": ["B"],
                "prompt_template": "{{ B.output }}",
            },
            {
                "id": "B",
                "subagent": "s",
                "depends_on": ["A"],
                "prompt_template": "{{ A.output }}",
            },
        ],
    )
    with pytest.raises(DagValidationError):
        validate_and_order(spec)


def test_duplicate_ids_rejected() -> None:
    """Two nodes with the same id are rejected."""
    spec = _spec(
        [
            {"id": "A", "subagent": "s", "prompt_template": "x"},
            {"id": "A", "subagent": "s", "prompt_template": "y"},
        ],
    )
    with pytest.raises(DagValidationError):
        validate_and_order(spec)


def test_reference_to_undeclared_dependency_rejected() -> None:
    """{{ A.output }} without A in depends_on is default-denied."""
    spec = _spec(
        [
            {"id": "A", "subagent": "s", "prompt_template": "x"},
            {
                "id": "B",
                "subagent": "s",
                "prompt_template": "{{ A.output }}",
            },
        ],
    )
    with pytest.raises(DagValidationError):
        validate_and_order(spec)


def test_unknown_input_key_rejected() -> None:
    """{{ inputs.missing }} with no such input is rejected."""
    spec = _spec(
        [
            {
                "id": "A",
                "subagent": "s",
                "inputs": {"topic": "t"},
                "prompt_template": "{{ inputs.missing }}",
            },
        ],
    )
    with pytest.raises(DagValidationError):
        validate_and_order(spec)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/subagent_dag_graph_test.py -v`
Expected: FAIL — `ModuleNotFoundError` for `_graph`.

- [ ] **Step 3: Implement `_graph.py`**

Create `src/agentscope/subagent/_dag/_graph.py`:

```python
# -*- coding: utf-8 -*-
"""The sub-agent DAG spec models and structural validation."""

from pydantic import BaseModel, Field

from ._errors import DagValidationError
from ._placeholders import parse_placeholders


class DagNodeSpec(BaseModel):
    """One node in a sub-agent DAG.

    Attributes:
        id (`str`):
            Unique node id within the graph.
        subagent (`str`):
            Name of the ``CliSubAgentTool`` that runs this node.
        prompt_template (`str`):
            Template rendered into the node's prompt file.
        depends_on (`list[str]`):
            Ids of upstream nodes that must complete first.
        inputs (`dict[str, str | dict]`):
            Per-node inputs; each value is a literal string or a file
            reference of the form ``{"file": "<path>"}``.
        instance (`str | None`):
            Optional stateful sub-agent handle.
    """

    id: str
    subagent: str
    prompt_template: str
    depends_on: list[str] = Field(default_factory=list)
    inputs: dict[str, object] = Field(default_factory=dict)
    instance: str | None = None


class SubAgentDagSpec(BaseModel):
    """A whole sub-agent DAG: a flat list of nodes with edges."""

    nodes: list[DagNodeSpec]


def parse_dag_spec(data: dict) -> SubAgentDagSpec:
    """Parse a raw graph dict into a validated :class:`SubAgentDagSpec`.

    Args:
        data (`dict`):
            The ``{"nodes": [...]}`` mapping submitted by the agent.

    Returns:
        `SubAgentDagSpec`:
            The parsed spec (field-level validation only).
    """
    return SubAgentDagSpec.model_validate(data)


def validate_and_order(spec: SubAgentDagSpec) -> list[str]:
    """Validate graph structure and return a topological node order.

    Checks: unique ids; every ``depends_on`` resolves; the graph is
    acyclic; every ``{{ dep.output* }}`` references a declared
    dependency (default-deny); every ``{{ inputs.key }}`` references a
    declared input.

    Args:
        spec (`SubAgentDagSpec`):
            The parsed graph spec.

    Returns:
        `list[str]`:
            Node ids in a topological (dependencies-first) order.

    Raises:
        `DagValidationError`:
            When any structural rule is violated.
    """
    nodes = spec.nodes
    ids = [node.id for node in nodes]
    if len(ids) != len(set(ids)):
        raise DagValidationError("node ids must be unique")
    id_set = set(ids)
    by_id = {node.id: node for node in nodes}

    for node in nodes:
        for dep in node.depends_on:
            if dep not in id_set:
                raise DagValidationError(
                    f"node '{node.id}' depends on unknown '{dep}'",
                )
        _validate_refs(node)

    return _topological_order(by_id)


def _validate_refs(node: DagNodeSpec) -> None:
    """Enforce default-deny on a node's template references.

    Args:
        node (`DagNodeSpec`):
            The node whose ``prompt_template`` is checked.

    Raises:
        `DagValidationError`:
            On a dependency or input reference that is not declared.
    """
    declared_deps = set(node.depends_on)
    for ph in parse_placeholders(node.prompt_template):
        if ph.kind in ("output", "output_path") and ph.name not in (
            declared_deps
        ):
            raise DagValidationError(
                f"node '{node.id}' references undeclared dependency "
                f"'{ph.name}'",
            )
        if ph.kind in ("input", "input_path") and ph.name not in node.inputs:
            raise DagValidationError(
                f"node '{node.id}' references unknown input '{ph.name}'",
            )


def _topological_order(by_id: dict[str, DagNodeSpec]) -> list[str]:
    """Kahn topological sort; raise on a cycle.

    Args:
        by_id (`dict[str, DagNodeSpec]`):
            Nodes keyed by id.

    Returns:
        `list[str]`:
            A dependencies-first ordering of node ids.

    Raises:
        `DagValidationError`:
            When the graph contains a cycle.
    """
    indegree = {nid: len(node.depends_on) for nid, node in by_id.items()}
    dependents: dict[str, list[str]] = {nid: [] for nid in by_id}
    for nid, node in by_id.items():
        for dep in node.depends_on:
            dependents[dep].append(nid)

    ready = sorted(nid for nid, deg in indegree.items() if deg == 0)
    order: list[str] = []
    while ready:
        nid = ready.pop(0)
        order.append(nid)
        for child in sorted(dependents[nid]):
            indegree[child] -= 1
            if indegree[child] == 0:
                ready.append(child)
        ready.sort()

    if len(order) != len(by_id):
        raise DagValidationError("graph contains a cycle")
    return order
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/subagent_dag_graph_test.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_dag/_graph.py tests/subagent_dag_graph_test.py
git commit -m "feat(subagent): DAG spec models + structural validation"
```

---

### Task 5: Prompt template rendering

**Files:**
- Create: `src/agentscope/subagent/_dag/_render.py`
- Test: `tests/subagent_dag_render_test.py`

**Interfaces:**
- Consumes: `DagNodeSpec` (Task 4), `parse_placeholders` (Task 3), `DagValidationError` (Task 3), `BackendBase.read_file` / `abspath`.
- Produces: `async render_prompt(node: DagNodeSpec, *, backend, cwd: str, output_paths: dict[str, str]) -> str` — substitutes every placeholder; reads files via the backend; raises `DagValidationError` for `input_path` on a non-file input.

- [ ] **Step 1: Write the failing test**

Create `tests/subagent_dag_render_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for DAG prompt template rendering."""

import asyncio

from agentscope.subagent._dag._graph import DagNodeSpec
from agentscope.subagent._dag._render import render_prompt


class _ReadBackend:
    """Backend stub that serves canned file contents."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self._files = files

    def abspath(self, path: str, *, cwd: str) -> str:
        """Resolve a relative path against cwd (posix join)."""
        return path if path.startswith("/") else f"{cwd}/{path}"

    async def read_file(self, path: str) -> bytes:
        """Return canned bytes for path."""
        return self._files[path]


def test_renders_inputs_outputs_and_refs() -> None:
    """Every placeholder kind renders to content or path."""
    backend = _ReadBackend(
        {
            "/w/spec.md": b"SPEC",
            "/w/.ravenx_dag/r/A.out.md": b"AOUT",
            "/w/r0/old.out.md": b"OLD",
        },
    )
    node = DagNodeSpec(
        id="B",
        subagent="s",
        depends_on=["A"],
        inputs={"topic": "T", "spec": {"file": "spec.md"}},
        prompt_template=(
            "topic={{ inputs.topic }} spec={{ inputs.spec }} "
            "specpath={{ inputs.spec.path }} a={{ A.output }} "
            "apath={{ A.output_path }} old={{ ref:r0/old.out.md }} "
            "oldpath={{ ref_path:r0/old.out.md }}"
        ),
    )
    rendered = asyncio.run(
        render_prompt(
            node,
            backend=backend,
            cwd="/w",
            output_paths={"A": "/w/.ravenx_dag/r/A.out.md"},
        ),
    )
    assert rendered == (
        "topic=T spec=SPEC specpath=/w/spec.md a=AOUT "
        "apath=/w/.ravenx_dag/r/A.out.md old=OLD oldpath=r0/old.out.md"
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/subagent_dag_render_test.py -v`
Expected: FAIL — `ModuleNotFoundError` for `_render`.

- [ ] **Step 3: Implement `_render.py`**

Create `src/agentscope/subagent/_dag/_render.py`:

```python
# -*- coding: utf-8 -*-
"""Render a DAG node's prompt template into concrete prompt text."""

from typing import Any

from ._errors import DagValidationError
from ._graph import DagNodeSpec
from ._placeholders import Placeholder, parse_placeholders


async def render_prompt(
    node: DagNodeSpec,
    *,
    backend: Any,
    cwd: str,
    output_paths: dict[str, str],
) -> str:
    """Render ``node.prompt_template`` into the node's prompt text.

    Args:
        node (`DagNodeSpec`):
            The node whose template is rendered.
        backend (`BackendBase`):
            Backend used to read referenced files.
        cwd (`str`):
            Directory that relative reference paths resolve against.
        output_paths (`dict[str, str]`):
            Mapping of completed dependency id to its output file path.

    Returns:
        `str`:
            The fully substituted prompt text.

    Raises:
        `DagValidationError`:
            When ``inputs.<key>.path`` targets a non-file input.
    """
    rendered = node.prompt_template
    for ph in parse_placeholders(node.prompt_template):
        value = await _resolve(ph, node, backend=backend, cwd=cwd,
                               output_paths=output_paths)
        rendered = rendered.replace(ph.raw, value, 1)
    return rendered


async def _resolve(
    ph: Placeholder,
    node: DagNodeSpec,
    *,
    backend: Any,
    cwd: str,
    output_paths: dict[str, str],
) -> str:
    """Resolve one placeholder to its replacement string.

    Args:
        ph (`Placeholder`):
            The placeholder to resolve.
        node (`DagNodeSpec`):
            The owning node (for input lookup).
        backend (`BackendBase`):
            Backend used to read files.
        cwd (`str`):
            Directory for relative path resolution.
        output_paths (`dict[str, str]`):
            Dependency id to output file path.

    Returns:
        `str`:
            The replacement text.

    Raises:
        `DagValidationError`:
            On ``input_path`` for a literal (non-file) input.
    """
    if ph.kind == "input":
        return await _input_value(ph.name, node, backend, cwd)
    if ph.kind == "input_path":
        spec = node.inputs.get(ph.name)
        if not isinstance(spec, dict) or "file" not in spec:
            raise DagValidationError(
                f"input '{ph.name}' has no file path to reference",
            )
        return str(spec["file"])
    if ph.kind == "output":
        return await _read(backend, output_paths[ph.name], cwd)
    if ph.kind == "output_path":
        return output_paths[ph.name]
    if ph.kind == "ref":
        return await _read(backend, ph.name, cwd)
    # ph.kind == "ref_path"
    return ph.name


async def _input_value(
    key: str,
    node: DagNodeSpec,
    backend: Any,
    cwd: str,
) -> str:
    """Return an input's value: literal, or file contents for a file spec.

    Args:
        key (`str`):
            The input key.
        node (`DagNodeSpec`):
            The owning node.
        backend (`BackendBase`):
            Backend used to read a file input.
        cwd (`str`):
            Directory for relative path resolution.

    Returns:
        `str`:
            The literal string or the referenced file's contents.
    """
    spec = node.inputs.get(key)
    if isinstance(spec, dict) and "file" in spec:
        return await _read(backend, str(spec["file"]), cwd)
    return str(spec)


async def _read(backend: Any, path: str, cwd: str) -> str:
    """Read a backend file as UTF-8 text, resolving against ``cwd``.

    Args:
        backend (`BackendBase`):
            Backend used for the read.
        path (`str`):
            A relative or absolute path in the backend environment.
        cwd (`str`):
            Directory a relative ``path`` resolves against.

    Returns:
        `str`:
            The decoded file contents.
    """
    resolved = backend.abspath(path, cwd=cwd)
    data = await backend.read_file(resolved)
    return data.decode("utf-8", errors="replace")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/subagent_dag_render_test.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_dag/_render.py tests/subagent_dag_render_test.py
git commit -m "feat(subagent): DAG prompt template rendering"
```

---

### Task 6: Run store (files + manifest over the backend)

**Files:**
- Create: `src/agentscope/subagent/_dag/_store.py`
- Test: `tests/subagent_dag_store_test.py`

**Interfaces:**
- Consumes: `BackendBase.write_file` / `read_file` / `join_path` / `file_exists`.
- Produces:
  - `make_run_id() -> str` — `"<UTC ISO-ish>-<8 hex>"`.
  - `DagRunStore(backend, workdir: str, run_id: str)` with: `run_id`, `run_dir` (property), `prompt_path(node_id) -> str`, `output_path(node_id) -> str`, `async init(graph_json: str)`, `async write_text(path, text)`, `async read_text(path) -> str`, `async write_manifest(manifest: dict)`, `async append_index(entry: dict)`.

- [ ] **Step 1: Write the failing test**

Create `tests/subagent_dag_store_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for the DAG run store over a LocalBackend."""

import json
import tempfile
import unittest

from agentscope.subagent._dag._store import DagRunStore, make_run_id
from agentscope.tool import LocalBackend


class DagRunStoreTest(unittest.IsolatedAsyncioTestCase):
    """Store writes prompt/manifest/index files under the run dir."""

    async def test_paths_and_roundtrip(self) -> None:
        """Prompt text and manifest round-trip; paths are run-scoped."""
        with tempfile.TemporaryDirectory() as workdir:
            store = DagRunStore(LocalBackend(), workdir, "run1")
            self.assertTrue(store.run_dir.endswith(".ravenx_dag/run1"))
            self.assertEqual(
                store.prompt_path("A"),
                f"{store.run_dir}/A.prompt.md",
            )
            await store.init('{"nodes": []}')
            await store.write_text(store.prompt_path("A"), "hello A")
            self.assertEqual(
                await store.read_text(store.prompt_path("A")),
                "hello A",
            )
            await store.write_manifest({"A": {"status": "completed"}})
            manifest_raw = await store.read_text(
                f"{store.run_dir}/manifest.json",
            )
            self.assertEqual(
                json.loads(manifest_raw),
                {"A": {"status": "completed"}},
            )
            await store.append_index({"run_id": "run1"})
            await store.append_index({"run_id": "run2"})
            index_raw = await store.read_text(
                f"{workdir}/.ravenx_dag/index.json",
            )
            self.assertEqual(
                json.loads(index_raw),
                [{"run_id": "run1"}, {"run_id": "run2"}],
            )

    def test_run_id_shape(self) -> None:
        """make_run_id has a timestamp and an 8-char hex suffix."""
        rid = make_run_id()
        stamp, _, suffix = rid.rpartition("-")
        self.assertEqual(len(suffix), 8)
        self.assertTrue(stamp)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/subagent_dag_store_test.py -v`
Expected: FAIL — `ModuleNotFoundError` for `_store`.

- [ ] **Step 3: Implement `_store.py`**

Create `src/agentscope/subagent/_dag/_store.py`:

```python
# -*- coding: utf-8 -*-
"""Backend-backed storage for one DAG run's message files."""

import json
import time
import uuid
from typing import Any

_DAG_DIR = ".ravenx_dag"


def make_run_id() -> str:
    """Build a unique, sortable run id.

    Returns:
        `str`:
            ``"<UTC timestamp>-<8 hex>"``, e.g.
            ``"20260717T031500Z-1a2b3c4d"``.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    return f"{stamp}-{uuid.uuid4().hex[:8]}"


class DagRunStore:
    """Owns the on-disk layout for a single DAG run.

    All I/O goes through the workspace ``backend`` so it works for
    local, Docker, E2B, and remote backends alike.
    """

    def __init__(self, backend: Any, workdir: str, run_id: str) -> None:
        """Initialize the store.

        Args:
            backend (`BackendBase`):
                The session workspace backend.
            workdir (`str`):
                The session working directory (run dirs live under it).
            run_id (`str`):
                The unique id for this run.
        """
        self._backend = backend
        self._workdir = workdir
        self.run_id = run_id

    @property
    def run_dir(self) -> str:
        """The run-scoped directory ``<workdir>/.ravenx_dag/<run_id>``."""
        return self._backend.join_path(self._workdir, _DAG_DIR, self.run_id)

    def prompt_path(self, node_id: str) -> str:
        """Path of a node's rendered prompt file.

        Args:
            node_id (`str`):
                The node id.

        Returns:
            `str`:
                ``<run_dir>/<node_id>.prompt.md``.
        """
        return self._backend.join_path(self.run_dir, f"{node_id}.prompt.md")

    def output_path(self, node_id: str) -> str:
        """Path of a node's captured output file.

        Args:
            node_id (`str`):
                The node id.

        Returns:
            `str`:
                ``<run_dir>/<node_id>.out.md``.
        """
        return self._backend.join_path(self.run_dir, f"{node_id}.out.md")

    async def init(self, graph_json: str) -> None:
        """Create the run dir (implicitly) and persist ``graph.json``.

        Args:
            graph_json (`str`):
                The submitted graph spec, serialized as JSON.
        """
        await self.write_text(
            self._backend.join_path(self.run_dir, "graph.json"),
            graph_json,
        )

    async def write_text(self, path: str, text: str) -> None:
        """Write UTF-8 text to ``path`` (parent dirs auto-created).

        Args:
            path (`str`):
                Destination path in the backend environment.
            text (`str`):
                The text to write.
        """
        await self._backend.write_file(path, text.encode("utf-8"))

    async def read_text(self, path: str) -> str:
        """Read UTF-8 text from ``path``.

        Args:
            path (`str`):
                Path in the backend environment.

        Returns:
            `str`:
                The decoded contents.
        """
        data = await self._backend.read_file(path)
        return data.decode("utf-8", errors="replace")

    async def write_manifest(self, manifest: dict) -> None:
        """Persist the run manifest as ``manifest.json``.

        Args:
            manifest (`dict`):
                Node id to status/metadata mapping.
        """
        await self.write_text(
            self._backend.join_path(self.run_dir, "manifest.json"),
            json.dumps(manifest, ensure_ascii=False, indent=2),
        )

    async def append_index(self, entry: dict) -> None:
        """Append one run entry to the session-level ``index.json``.

        Args:
            entry (`dict`):
                A small summary of this run for later discovery.
        """
        path = self._backend.join_path(self._workdir, _DAG_DIR, "index.json")
        entries: list = []
        if await self._backend.file_exists(path):
            raw = await self.read_text(path)
            try:
                entries = json.loads(raw)
            except json.JSONDecodeError:
                entries = []
        entries.append(entry)
        await self.write_text(
            path,
            json.dumps(entries, ensure_ascii=False, indent=2),
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/subagent_dag_store_test.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_dag/_store.py tests/subagent_dag_store_test.py
git commit -m "feat(subagent): DAG run store over the workspace backend"
```

---

### Task 7: Ready-set scheduler / runner

**Files:**
- Create: `src/agentscope/subagent/_dag/_runner.py`
- Test: `tests/subagent_dag_runner_test.py`

**Interfaces:**
- Consumes: `SubAgentDagSpec` + `validate_and_order` (Task 4), `render_prompt` (Task 5), `DagRunStore` + `make_run_id` (Task 6), `DagValidationError` (Task 3), and a sub-agent object exposing `command_uses_prompt_file: bool` and `call(prompt, instance=None, prompt_file=None, output_file=None) -> AsyncGenerator[ToolChunk, None]` (Task 2).
- Produces:
  - `DagRunResult` dataclass: `run_id: str`, `dir: str`, `terminal_outputs: list[dict]`, `files: list[dict]`, `summary: dict`.
  - `async run_dag(spec, *, subagents: dict[str, Any], backend, workdir: str, max_concurrency: int = 5) -> DagRunResult`.

- [ ] **Step 1: Write the failing test**

Create `tests/subagent_dag_runner_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for the DAG ready-set scheduler."""

import tempfile
import unittest

from agentscope.message import TextBlock, ToolResultState
from agentscope.subagent._dag._graph import parse_dag_spec
from agentscope.subagent._dag._runner import run_dag
from agentscope.tool import LocalBackend, ToolChunk
from tests.utils import AnyString


class _FakeSubAgent:
    """Fake sub-agent: writes '<name>:<prompt>' to output_file."""

    command_uses_prompt_file = True

    def __init__(self, name: str, fail: bool = False) -> None:
        self._name = name
        self._fail = fail

    async def call(self, prompt, instance=None, prompt_file=None,
                   output_file=None):
        """Read the prompt file, echo a tagged result to output_file."""
        backend = LocalBackend()
        text = (await backend.read_file(prompt_file)).decode("utf-8")
        if self._fail:
            yield ToolChunk(
                content=[TextBlock(text="boom")],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return
        out = f"{self._name}:{text}"
        await backend.write_file(output_file, out.encode("utf-8"))
        yield ToolChunk(
            content=[TextBlock(text=out)],
            state=ToolResultState.RUNNING,
            is_last=True,
        )


class DagRunnerTest(unittest.IsolatedAsyncioTestCase):
    """The runner executes a diamond and cascades failures."""

    async def test_diamond_completes_and_passes_messages(self) -> None:
        """A → B,C → D; D sees B and C outputs; result is whole-structure."""
        with tempfile.TemporaryDirectory() as workdir:
            spec = parse_dag_spec(
                {
                    "nodes": [
                        {"id": "A", "subagent": "s", "prompt_template": "go"},
                        {
                            "id": "B",
                            "subagent": "s",
                            "depends_on": ["A"],
                            "prompt_template": "{{ A.output }}",
                        },
                        {
                            "id": "C",
                            "subagent": "s",
                            "depends_on": ["A"],
                            "prompt_template": "{{ A.output }}",
                        },
                        {
                            "id": "D",
                            "subagent": "s",
                            "depends_on": ["B", "C"],
                            "prompt_template": "{{ B.output }}|{{ C.output }}",
                        },
                    ],
                },
            )
            result = await run_dag(
                spec,
                subagents={"s": _FakeSubAgent("s")},
                backend=LocalBackend(),
                workdir=workdir,
            )
            self.assertEqual(result.summary["completed"], 4)
            self.assertEqual(len(result.terminal_outputs), 1)
            term = result.terminal_outputs[0]
            self.assertEqual(term["node"], "D")
            self.assertEqual(term["text"], "s:s:s:go|s:s:go")

    async def test_failure_cascades_to_dependents(self) -> None:
        """A fails → B is skipped; partial completion is reported."""
        with tempfile.TemporaryDirectory() as workdir:
            spec = parse_dag_spec(
                {
                    "nodes": [
                        {"id": "A", "subagent": "bad", "prompt_template": "go"},
                        {
                            "id": "B",
                            "subagent": "ok",
                            "depends_on": ["A"],
                            "prompt_template": "{{ A.output }}",
                        },
                    ],
                },
            )
            result = await run_dag(
                spec,
                subagents={
                    "bad": _FakeSubAgent("bad", fail=True),
                    "ok": _FakeSubAgent("ok"),
                },
                backend=LocalBackend(),
                workdir=workdir,
            )
            self.assertEqual(result.summary["failed"], 1)
            self.assertEqual(result.summary["skipped"], 1)
            statuses = {f["node"]: f["status"] for f in result.files}
            self.assertEqual(statuses, {"A": "failed", "B": "skipped"})
            self.assertEqual(result.run_id, AnyString())
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/subagent_dag_runner_test.py -v`
Expected: FAIL — `ModuleNotFoundError` for `_runner`.

- [ ] **Step 3: Implement `_runner.py`**

Create `src/agentscope/subagent/_dag/_runner.py`:

```python
# -*- coding: utf-8 -*-
"""The deterministic ready-set scheduler for a sub-agent DAG."""

import asyncio
from dataclasses import dataclass, field
from typing import Any

from ..._logging import logger
from ...message import ToolResultState
from ._errors import DagValidationError
from ._graph import SubAgentDagSpec, validate_and_order
from ._render import render_prompt
from ._store import DagRunStore, make_run_id


@dataclass
class DagRunResult:
    """The outcome of one DAG run, returned to the main agent.

    Attributes:
        run_id (`str`):
            The run id.
        dir (`str`):
            The run-scoped directory holding all message files.
        terminal_outputs (`list[dict]`):
            ``{"node": id, "text": <full output>}`` for each completed
            terminal (sink) node.
        files (`list[dict]`):
            ``{"node", "status", "prompt_file", "output_file"}`` per node.
        summary (`dict`):
            Counts: ``total`` / ``completed`` / ``failed`` / ``skipped``.
    """

    run_id: str
    dir: str
    terminal_outputs: list[dict] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


async def run_dag(
    spec: SubAgentDagSpec,
    *,
    subagents: dict[str, Any],
    backend: Any,
    workdir: str,
    max_concurrency: int = 5,
) -> DagRunResult:
    """Run a validated DAG, passing messages through files.

    Args:
        spec (`SubAgentDagSpec`):
            The graph to execute.
        subagents (`dict[str, Any]`):
            Sub-agent name to its ``CliSubAgentTool``.
        backend (`BackendBase`):
            Session workspace backend for all file I/O.
        workdir (`str`):
            Session working directory.
        max_concurrency (`int`, defaults to `5`):
            Maximum concurrent sub-agent processes.

    Returns:
        `DagRunResult`:
            The run outcome for the main agent.

    Raises:
        `DagValidationError`:
            When the graph is invalid or a node names an unknown
            sub-agent, or a node's sub-agent does not use
            ``{prompt_file}``.
    """
    validate_and_order(spec)
    by_id = {node.id: node for node in spec.nodes}
    for node in spec.nodes:
        tool = subagents.get(node.subagent)
        if tool is None:
            raise DagValidationError(
                f"node '{node.id}' names unknown sub-agent "
                f"'{node.subagent}'",
            )
        if not tool.command_uses_prompt_file:
            raise DagValidationError(
                f"sub-agent '{node.subagent}' must use '{{prompt_file}}' "
                f"to participate in a DAG",
            )

    store = DagRunStore(backend, workdir, make_run_id())
    await store.init(spec.model_dump_json())

    status: dict[str, str] = {nid: "pending" for nid in by_id}
    output_paths: dict[str, str] = {}
    errors: dict[str, str] = {}
    semaphore = asyncio.Semaphore(max_concurrency)
    instance_locks: dict[str, asyncio.Lock] = {}

    dependents: dict[str, list[str]] = {nid: [] for nid in by_id}
    for nid, node in by_id.items():
        for dep in node.depends_on:
            dependents[dep].append(nid)

    while True:
        _cascade_failures(by_id, status)
        ready = [
            nid
            for nid, st in status.items()
            if st == "pending"
            and all(status[d] == "completed" for d in by_id[nid].depends_on)
        ]
        if not ready:
            break
        await asyncio.gather(
            *(
                _run_node(
                    by_id[nid],
                    subagents[by_id[nid].subagent],
                    store=store,
                    backend=backend,
                    workdir=workdir,
                    output_paths=output_paths,
                    status=status,
                    errors=errors,
                    semaphore=semaphore,
                    instance_locks=instance_locks,
                )
                for nid in ready
            ),
        )

    return await _finalize(spec, by_id, status, errors, output_paths,
                          dependents, store)


def _cascade_failures(by_id: dict, status: dict[str, str]) -> None:
    """Mark pending nodes with a failed/skipped dependency as skipped.

    Args:
        by_id (`dict`):
            Nodes keyed by id.
        status (`dict[str, str]`):
            Mutable per-node status map.
    """
    changed = True
    while changed:
        changed = False
        for nid, node in by_id.items():
            if status[nid] != "pending":
                continue
            if any(status[d] in ("failed", "skipped") for d in node.depends_on):
                status[nid] = "skipped"
                changed = True


async def _run_node(
    node: Any,
    tool: Any,
    *,
    store: DagRunStore,
    backend: Any,
    workdir: str,
    output_paths: dict[str, str],
    status: dict[str, str],
    errors: dict[str, str],
    semaphore: asyncio.Semaphore,
    instance_locks: dict[str, asyncio.Lock],
) -> None:
    """Render, dispatch, and record one node.

    Args:
        node (`DagNodeSpec`):
            The node to run.
        tool (`CliSubAgentTool`):
            The sub-agent that runs it.
        store (`DagRunStore`):
            The run store.
        backend (`BackendBase`):
            Backend for rendering file reads.
        workdir (`str`):
            Session working directory (rendering cwd).
        output_paths (`dict[str, str]`):
            Completed dependency outputs (updated on success).
        status (`dict[str, str]`):
            Per-node status map (updated).
        errors (`dict[str, str]`):
            Per-node error text (updated on failure).
        semaphore (`asyncio.Semaphore`):
            Global concurrency cap.
        instance_locks (`dict[str, asyncio.Lock]`):
            Per-instance serialization locks.
    """
    async with semaphore:
        lock = None
        if node.instance is not None:
            lock = instance_locks.setdefault(node.instance, asyncio.Lock())
        if lock is not None:
            await lock.acquire()
        try:
            prompt = await render_prompt(
                node,
                backend=backend,
                cwd=workdir,
                output_paths=output_paths,
            )
            prompt_path = store.prompt_path(node.id)
            output_path = store.output_path(node.id)
            await store.write_text(prompt_path, prompt)
            last = None
            async for chunk in tool.call(
                prompt=prompt,
                instance=node.instance,
                prompt_file=prompt_path,
                output_file=output_path,
            ):
                last = chunk
            if last is not None and last.state == ToolResultState.ERROR:
                status[node.id] = "failed"
                errors[node.id] = last.content[0].text if last.content else ""
            else:
                status[node.id] = "completed"
                output_paths[node.id] = output_path
        except Exception as exc:  # noqa: BLE001 - record and continue
            logger.warning("DAG node %s failed: %s", node.id, exc)
            status[node.id] = "failed"
            errors[node.id] = str(exc)
        finally:
            if lock is not None:
                lock.release()


async def _finalize(
    spec: SubAgentDagSpec,
    by_id: dict,
    status: dict[str, str],
    errors: dict[str, str],
    output_paths: dict[str, str],
    dependents: dict[str, list[str]],
    store: DagRunStore,
) -> DagRunResult:
    """Assemble the result, write the manifest, and append the index.

    Args:
        spec (`SubAgentDagSpec`):
            The executed spec.
        by_id (`dict`):
            Nodes keyed by id.
        status (`dict[str, str]`):
            Final per-node status.
        errors (`dict[str, str]`):
            Per-node error text.
        output_paths (`dict[str, str]`):
            Completed node output paths.
        dependents (`dict[str, list[str]]`):
            Node id to its dependent ids.
        store (`DagRunStore`):
            The run store.

    Returns:
        `DagRunResult`:
            The assembled run result.
    """
    files: list[dict] = []
    manifest: dict = {}
    for node in spec.nodes:
        nid = node.id
        entry = {
            "node": nid,
            "status": status[nid],
            "prompt_file": store.prompt_path(nid)
            if status[nid] != "skipped"
            else None,
            "output_file": output_paths.get(nid),
        }
        files.append(entry)
        manifest[nid] = {
            "status": status[nid],
            "subagent": node.subagent,
            "depends_on": node.depends_on,
            "error": errors.get(nid),
        }

    terminal_outputs: list[dict] = []
    for nid in by_id:
        if not dependents[nid] and status[nid] == "completed":
            terminal_outputs.append(
                {"node": nid, "text": await store.read_text(
                    output_paths[nid])},
            )

    summary = {
        "total": len(by_id),
        "completed": sum(1 for s in status.values() if s == "completed"),
        "failed": sum(1 for s in status.values() if s == "failed"),
        "skipped": sum(1 for s in status.values() if s == "skipped"),
    }
    await store.write_manifest(manifest)
    await store.append_index({"run_id": store.run_id, "summary": summary})
    return DagRunResult(
        run_id=store.run_id,
        dir=store.run_dir,
        terminal_outputs=terminal_outputs,
        files=files,
        summary=summary,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/subagent_dag_runner_test.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_dag/_runner.py tests/subagent_dag_runner_test.py
git commit -m "feat(subagent): DAG ready-set scheduler with file message passing"
```

---

### Task 8: The `run_subagent_dag` orchestration tool

**Files:**
- Create: `src/agentscope/subagent/_dag/_tool.py`
- Modify: `src/agentscope/subagent/_dag/__init__.py`
- Test: `tests/subagent_dag_tool_test.py`

**Interfaces:**
- Consumes: `parse_dag_spec` (Task 4), `run_dag` + `DagRunResult` (Task 7), `DagValidationError` (Task 3), `ToolBase` / `ToolChunk` / `TextBlock` / `ToolResultState`, `PermissionDecision`.
- Produces:
  - `SubAgentDagTool(subagents: dict[str, Any], backend, workdir: str, max_concurrency: int = 5, description: str | None = None)` — `name = "run_subagent_dag"`; `call(nodes: list[dict]) -> AsyncGenerator[ToolChunk, None]`; the terminal chunk's `metadata` carries `run_id` / `dir` / `terminal_outputs` / `files` / `summary`.
  - `_dag/__init__.py` re-exports `SubAgentDagTool`.

- [ ] **Step 1: Write the failing test**

Create `tests/subagent_dag_tool_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for the run_subagent_dag orchestration tool."""

import tempfile
import unittest
from typing import AsyncGenerator

from agentscope.message import TextBlock, ToolResultState
from agentscope.subagent._dag import SubAgentDagTool
from agentscope.tool import LocalBackend, ToolChunk


class _FakeSubAgent:
    """Fake sub-agent echoing '<name>:<prompt>' to the output file."""

    command_uses_prompt_file = True

    def __init__(self, name: str) -> None:
        self._name = name

    async def call(self, prompt, instance=None, prompt_file=None,
                   output_file=None):
        """Echo a tagged result to output_file."""
        backend = LocalBackend()
        text = (await backend.read_file(prompt_file)).decode("utf-8")
        out = f"{self._name}:{text}"
        await backend.write_file(output_file, out.encode("utf-8"))
        yield ToolChunk(
            content=[TextBlock(text=out)],
            state=ToolResultState.RUNNING,
            is_last=True,
        )


async def _collect(
    gen: AsyncGenerator[ToolChunk, None],
) -> list[ToolChunk]:
    """Drain an async generator to a list."""
    return [chunk async for chunk in gen]


class SubAgentDagToolTest(unittest.IsolatedAsyncioTestCase):
    """The tool runs a graph and returns a structured result."""

    async def test_runs_two_node_chain(self) -> None:
        """A → B chain; metadata carries the full result structure."""
        with tempfile.TemporaryDirectory() as workdir:
            tool = SubAgentDagTool(
                subagents={"s": _FakeSubAgent("s")},
                backend=LocalBackend(),
                workdir=workdir,
            )
            self.assertEqual(tool.name, "run_subagent_dag")
            chunks = await _collect(
                tool.call(
                    nodes=[
                        {"id": "A", "subagent": "s", "prompt_template": "go"},
                        {
                            "id": "B",
                            "subagent": "s",
                            "depends_on": ["A"],
                            "prompt_template": "{{ A.output }}",
                        },
                    ],
                ),
            )
            meta = chunks[-1].metadata
            self.assertEqual(meta["summary"]["completed"], 2)
            self.assertEqual(
                meta["terminal_outputs"], [{"node": "B", "text": "s:s:go"}]
            )

    async def test_invalid_graph_yields_error(self) -> None:
        """A cyclic graph returns an ERROR chunk, not an exception."""
        with tempfile.TemporaryDirectory() as workdir:
            tool = SubAgentDagTool(
                subagents={"s": _FakeSubAgent("s")},
                backend=LocalBackend(),
                workdir=workdir,
            )
            chunks = await _collect(
                tool.call(
                    nodes=[
                        {
                            "id": "A",
                            "subagent": "s",
                            "depends_on": ["B"],
                            "prompt_template": "{{ B.output }}",
                        },
                        {
                            "id": "B",
                            "subagent": "s",
                            "depends_on": ["A"],
                            "prompt_template": "{{ A.output }}",
                        },
                    ],
                ),
            )
            self.assertEqual(chunks[-1].state, ToolResultState.ERROR)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/subagent_dag_tool_test.py -v`
Expected: FAIL — `ImportError: cannot import name 'SubAgentDagTool'`.

- [ ] **Step 3: Implement `_tool.py` and export it**

Create `src/agentscope/subagent/_dag/_tool.py`:

```python
# -*- coding: utf-8 -*-
"""The run_subagent_dag orchestration tool."""

from typing import Any, AsyncGenerator

from ...message import TextBlock, ToolResultState
from ...permission import (
    PermissionBehavior,
    PermissionContext,
    PermissionDecision,
)
from ...tool import ToolBase, ToolChunk
from ._errors import DagValidationError
from ._graph import parse_dag_spec
from ._runner import run_dag

_DEFAULT_DESCRIPTION = (
    "Orchestrate a DAG of sub-agents. Provide 'nodes': each node has an "
    "'id', a 'subagent' name, a 'prompt_template', optional 'depends_on' "
    "ids, optional 'inputs' (a literal string or {\"file\": path}), and an "
    "optional 'instance'. Templates may reference an upstream node's output "
    "with {{ <id>.output }} (contents) or {{ <id>.output_path }} (path), an "
    "input with {{ inputs.<key> }}, or a prior file with {{ ref:<path> }}. "
    "Only declared dependencies may be referenced. Intermediate outputs stay "
    "in files; you receive the terminal outputs and the full file list."
)

_NODE_SCHEMA = {
    "type": "object",
    "properties": {
        "id": {"type": "string", "description": "Unique node id."},
        "subagent": {
            "type": "string",
            "description": "Name of the sub-agent tool to run this node.",
        },
        "prompt_template": {
            "type": "string",
            "description": "Template rendered into the node's prompt file.",
        },
        "depends_on": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Ids of upstream nodes that must finish first.",
        },
        "inputs": {
            "type": "object",
            "description": (
                "Per-node inputs: a literal string or {\"file\": path}."
            ),
        },
        "instance": {
            "type": "string",
            "description": "Optional stateful sub-agent handle.",
        },
    },
    "required": ["id", "subagent", "prompt_template"],
}


class SubAgentDagTool(ToolBase):
    """Run a sub-agent DAG with file-based message passing."""

    is_read_only: bool = False
    is_concurrency_safe: bool = False

    def __init__(
        self,
        subagents: dict[str, Any],
        backend: Any,
        workdir: str,
        max_concurrency: int = 5,
        description: str | None = None,
    ) -> None:
        """Initialize the orchestration tool.

        Args:
            subagents (`dict[str, Any]`):
                Sub-agent name to its ``CliSubAgentTool``.
            backend (`BackendBase`):
                Session workspace backend for all file I/O.
            workdir (`str`):
                Session working directory (run dirs live under it).
            max_concurrency (`int`, defaults to `5`):
                Maximum concurrent sub-agent processes.
            description (`str | None`, optional):
                Override for the agent-facing tool description.
        """
        super().__init__()
        self.name = "run_subagent_dag"
        self.description = description or _DEFAULT_DESCRIPTION
        self._subagents = subagents
        self._backend = backend
        self._workdir = workdir
        self._max_concurrency = max_concurrency
        self.input_schema = {
            "type": "object",
            "properties": {
                "nodes": {
                    "type": "array",
                    "items": _NODE_SCHEMA,
                    "description": "The DAG nodes to run.",
                },
            },
            "required": ["nodes"],
        }

    async def call(  # type: ignore[override]
        self,
        nodes: list[dict],
    ) -> AsyncGenerator[ToolChunk, None]:
        """Run the DAG described by ``nodes``.

        Args:
            nodes (`list[dict]`):
                The node specs (see the tool description / schema).

        Yields:
            `ToolChunk`:
                A single terminal chunk; ``metadata`` carries
                ``run_id`` / ``dir`` / ``terminal_outputs`` / ``files`` /
                ``summary``.
        """
        try:
            spec = parse_dag_spec({"nodes": nodes})
            result = await run_dag(
                spec,
                subagents=self._subagents,
                backend=self._backend,
                workdir=self._workdir,
                max_concurrency=self._max_concurrency,
            )
        except DagValidationError as exc:
            yield ToolChunk(
                content=[TextBlock(text=f"Invalid DAG: {exc}")],
                state=ToolResultState.ERROR,
                is_last=True,
            )
            return

        lines = [
            f"DAG run {result.run_id} complete: {result.summary}",
            f"Files under: {result.dir}",
        ]
        for term in result.terminal_outputs:
            lines.append(f"\n## {term['node']}\n{term['text']}")
        yield ToolChunk(
            content=[TextBlock(text="\n".join(lines))],
            state=ToolResultState.RUNNING,
            is_last=True,
            metadata={
                "run_id": result.run_id,
                "dir": result.dir,
                "terminal_outputs": result.terminal_outputs,
                "files": result.files,
                "summary": result.summary,
            },
        )

    async def check_permissions(
        self,
        tool_input: dict[str, Any],
        context: PermissionContext,
    ) -> PermissionDecision:
        """Auto-run: DAG orchestration is always allowed.

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
            message="Sub-agent DAG runs automatically.",
        )
```

Replace `src/agentscope/subagent/_dag/__init__.py` with:

```python
# -*- coding: utf-8 -*-
"""File-based sub-agent DAG orchestration (internal package)."""

from ._errors import DagValidationError
from ._tool import SubAgentDagTool

__all__ = [
    "DagValidationError",
    "SubAgentDagTool",
]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/subagent_dag_tool_test.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_dag/_tool.py src/agentscope/subagent/_dag/__init__.py tests/subagent_dag_tool_test.py
git commit -m "feat(subagent): run_subagent_dag orchestration tool"
```

---

### Task 9: Wire the tool into the session factory + public export

**Files:**
- Modify: `src/agentscope/subagent/_agent_tools.py` (append `SubAgentDagTool` in `_factory`)
- Modify: `src/agentscope/subagent/__init__.py` (export `SubAgentDagTool`)
- Test: `tests/subagent_agent_tools_test.py`

**Interfaces:**
- Consumes: `SubAgentDagTool` (Task 8), existing `make_subagent_tool_factory` structure.
- Produces: the factory returns the existing `CliSubAgentTool`s **plus** one `SubAgentDagTool` when a backend + workdir resolve and at least one CLI sub-agent exists; `agentscope.subagent.SubAgentDagTool` is importable.

- [ ] **Step 1: Write the failing test**

Append to `tests/subagent_agent_tools_test.py` (mirror its existing fixtures for `storage`/`workspace_manager`; if it lacks a shared helper, construct the same fakes the file already uses). Minimal addition:

```python
def test_factory_appends_dag_tool() -> None:
    """The factory adds a run_subagent_dag tool alongside CLI tools."""
    import asyncio

    from agentscope.subagent import (
        SubAgentDagTool,
        make_subagent_tool_factory,
    )

    storage, workspace_manager = _make_storage_with_one_cli_subagent()
    factory = make_subagent_tool_factory(storage, workspace_manager)
    tools = asyncio.run(factory("u", "a", "s"))
    names = [t.name for t in tools]
    assert "run_subagent_dag" in names
    assert any(isinstance(t, SubAgentDagTool) for t in tools)
```

Add a `_make_storage_with_one_cli_subagent()` helper in the test module that returns a storage stub whose `list_subagents` yields one valid `CliSubAgentConfig` record (command `"cat {prompt_file}"`) and a workspace_manager stub whose `get_workspace` returns an object with `get_backend()` → `LocalBackend()` and `workdir` → a temp dir — matching the stubs already used elsewhere in this test file.

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/subagent_agent_tools_test.py::test_factory_appends_dag_tool -v`
Expected: FAIL — `run_subagent_dag` not in names (and/or import error for `SubAgentDagTool`).

- [ ] **Step 3: Implement the wiring**

In `src/agentscope/subagent/_agent_tools.py`, import the tool at the top:

```python
from ._dag import SubAgentDagTool
```

At the end of `_factory`, after the `for record in records:` loop builds `tools`, append the DAG tool when resolvable:

```python
        cli_tools = {t.name: t for t in tools}
        if cli_tools and backend is not None and session_workdir:
            tools.append(
                SubAgentDagTool(
                    subagents=cli_tools,
                    backend=backend,
                    workdir=session_workdir,
                ),
            )
        return tools
```

In `src/agentscope/subagent/__init__.py`, add the export:

```python
from ._dag import SubAgentDagTool
```

and add `"SubAgentDagTool"` to `__all__`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/subagent_agent_tools_test.py -v`
Expected: PASS (new test + pre-existing factory tests).

- [ ] **Step 5: Commit**

```bash
git add src/agentscope/subagent/_agent_tools.py src/agentscope/subagent/__init__.py tests/subagent_agent_tools_test.py
git commit -m "feat(subagent): expose run_subagent_dag via the session tool factory"
```

---

### Task 10: End-to-end integration test (real CLI) + docs pointer

**Files:**
- Create: `tests/subagent_dag_integration_test.py`
- Modify: `CLAUDE.md` (one line under the RavenX integration notes)
- Test: the integration test itself.

**Interfaces:**
- Consumes: the full public surface — `CliSubAgentTool` (Task 2), `SubAgentDagTool` (Task 8), `LocalBackend`.

- [ ] **Step 1: Write the failing integration test**

Create `tests/subagent_dag_integration_test.py`:

```python
# -*- coding: utf-8 -*-
"""End-to-end DAG run driving a real `cat` CLI sub-agent."""

import tempfile
import unittest
from typing import AsyncGenerator

from agentscope.subagent import CliSubAgentTool, SubAgentDagTool
from agentscope.tool import LocalBackend, ToolChunk
from tests.utils import AnyString


async def _collect(
    gen: AsyncGenerator[ToolChunk, None],
) -> list[ToolChunk]:
    """Drain an async generator to a list."""
    return [chunk async for chunk in gen]


class SubAgentDagIntegrationTest(unittest.IsolatedAsyncioTestCase):
    """A real `cat {prompt_file}` sub-agent runs a diamond DAG."""

    async def test_diamond_over_real_backend(self) -> None:
        """Messages flow A → B,C → D through real files on disk."""
        with tempfile.TemporaryDirectory() as workdir:
            backend = LocalBackend()
            cat = CliSubAgentTool(
                name="cat",
                description="echo prompt file",
                command="cat {prompt_file}",
                cwd=workdir,
                backend=backend,
            )
            tool = SubAgentDagTool(
                subagents={"cat": cat},
                backend=backend,
                workdir=workdir,
            )
            chunks = await _collect(
                tool.call(
                    nodes=[
                        {"id": "A", "subagent": "cat",
                         "prompt_template": "SEED"},
                        {"id": "B", "subagent": "cat", "depends_on": ["A"],
                         "prompt_template": "B<{{ A.output }}>"},
                        {"id": "C", "subagent": "cat", "depends_on": ["A"],
                         "prompt_template": "C<{{ A.output_path }}>"},
                        {"id": "D", "subagent": "cat",
                         "depends_on": ["B", "C"],
                         "prompt_template": "{{ B.output }}|{{ C.output }}"},
                    ],
                ),
            )
            meta = chunks[-1].metadata
            self.assertEqual(
                meta["summary"],
                {"total": 4, "completed": 4, "failed": 0, "skipped": 0},
            )
            self.assertEqual(meta["run_id"], AnyString())
            term = meta["terminal_outputs"][0]
            self.assertEqual(term["node"], "D")
            self.assertEqual(term["text"], "B<SEED>|C<" + AnyString() + ">")
            # The output files exist with full content on disk.
            b_out = await backend.read_file(
                next(f["output_file"] for f in meta["files"]
                     if f["node"] == "B"),
            )
            self.assertEqual(b_out.decode("utf-8"), "B<SEED>")
```

- [ ] **Step 2: Run test to verify it fails (then passes)**

Run: `pytest tests/subagent_dag_integration_test.py -v`
Expected: initially FAIL if any prior task is incomplete; with Tasks 1-9 done, it should PASS. If `term["text"]` comparison with an embedded `AnyString()` is brittle, assert `term["text"].startswith("B<SEED>|C<")` instead.

- [ ] **Step 3: Add the docs pointer**

In `CLAUDE.md`, under "Where RavenX sub-agents attach", append one bullet:

```markdown
4. **Sub-agent DAG orchestration** (`subagent/_dag/`, tool `run_subagent_dag`) — the leader submits a graph of CLI sub-agent nodes; a ready-set scheduler runs them, passing messages through workspace files (`{prompt_file}` in, `<node>.out.md` out). See `docs/superpowers/specs/2026-07-17-subagent-dag-orchestration-design.md`.
```

- [ ] **Step 4: Run the full sub-agent suite + lint**

Run: `pytest tests/subagent_dag_integration_test.py tests/subagent_tool_test.py tests/subagent_config_test.py tests/subagent_agent_tools_test.py -v`
Then: `pre-commit run --all-files`
Expected: all PASS; lint clean.

- [ ] **Step 5: Commit**

```bash
git add tests/subagent_dag_integration_test.py CLAUDE.md
git commit -m "test(subagent): end-to-end DAG integration + docs pointer"
```

---

## Self-Review

**1. Spec coverage:**
- Principle 1 (file I/O, reduced context) → Tasks 2, 7 (`.out.md` capture; only terminal outputs + file list returned).
- Principle 2 (prompt → file, `{prompt_file}`) → Tasks 1, 2, 6, 7.
- Principle 3 (upstream writes file, auto-fed downstream) → Tasks 5, 7.
- Principle 4 (templates, multi-upstream combine, file inputs) → Tasks 3, 4, 5.
- Principle 5 (main agent sink, terminal → main loop, full) → Tasks 7, 8.
- Principle 6 (full file list returned) → Tasks 7, 8.
- Principle 7 (reference prior-run files) → Tasks 3, 5 (`ref:`/`ref_path:`), 6 (`index.json`).
- Spec §4.3 validation (cycles, unknown subagent, missing `{prompt_file}`, default-deny) → Tasks 4, 7.
- Spec §5.5 backend I/O only → Tasks 6, 7 (no local `open()`).
- Spec §5.2 128k bump + output capture → Task 2.

**2. Placeholder scan:** No `TBD`/`TODO`; every code step shows complete code. The only "adapt to existing fixtures" note is Task 9's storage stub, which points at the concrete pattern already in that test file.

**3. Type consistency:** `command_uses_prompt_file` (Task 2) is consumed in Task 7. `render_prompt(node, *, backend, cwd, output_paths)` (Task 5) is called identically in Task 7. `DagRunStore` methods (Task 6) match Task 7 usage. `run_dag(spec, *, subagents, backend, workdir, max_concurrency)` (Task 7) matches Task 8's call. `parse_dag_spec` / `validate_and_order` (Task 4) signatures match their callers. `Placeholder(kind, name, raw)` (Task 3) matches `_graph`/`_render` usage.

## Execution Handoff

See the parent skill's handoff section.
