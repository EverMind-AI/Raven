# spawn prompt templates and one delegation package - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give `spawn` the DAG's file-based prompt assembly behind a shared
capability gate and a shared untrusted-content rule, and collapse both
delegation packages into one flat `raven/agent/subagent/`.

**Architecture:** Extract the DAG's grammar, root confinement, capability gate,
renderer, and file backend into `subagent/prompt_*.py` modules that both
surfaces consume; leave graph-specific resolution (`output`, `output_path`,
`{"node": ...}`) in `subagent/dag_*.py` composing on top. `spawn` then renders
`prompt_template` with `inputs` before dispatch, gated on `reads_local_files`.

**Tech Stack:** Python 3.12, pytest (`asyncio_mode = "auto"`), uv, pydantic,
TypeScript + vitest for the TUI touch.

**Spec:** `docs/specs/2026-08-26-spawn-prompt-template-design.md`

## Global Constraints

- Dependencies via `uv` only. Run tests as `uv run pytest ...`, never bare
  `pytest` (AGENTS.md 4, 5.4).
- Comments and docstrings in English. Do not add a comment where neighbouring
  lines have none (AGENTS.md 1).
- Commit messages: Conventional Commits, ASCII-only, header <= 100 chars,
  `Co-authored-by: Claude (<your session model id>) <noreply@anthropic.com>` trailer
  (AGENTS.md 3.1, 3.3). **Do not commit until the user asks** (AGENTS.md 3.4) -
  the commit step in each task below prepares the message; ask before running it.
- Branch: cut a worktree from `main` before the first edit; confirm the base
  with the user first (AGENTS.md 2.2). Branch name must be
  `<type>/<snake_desc>` - `EnterWorktree` names it `worktree-...`, which
  violates 2.1, so rename before any push.
- Do not rename existing test files; extend the matching
  `test_<module>_commands.py` / existing suite instead (AGENTS.md 5.4).
- `ui-tui` vitest must run serially; the suite flakes above ~100 files under
  default worker parallelism.
- `ui-tui` changes need `npm run build --prefix ui-tui` or `raven tui` runs the
  stale prebuilt bundle.
- Pre-commit hooks are disabled in this repo (`core.hooksPath` points at a
  missing dir), so run `make lint-python` by hand or CI goes red on formatting.

**Refinement of spec D6:** the spec says the move plus the extraction lands as
"the first commit". This plan splits it into four behaviour-preserving commits
(T1-T4), each with the full suite green. Same property the spec asks for -
a reviewer can check out any of them and verify the move alone - at a
granularity that is actually executable. Nothing else about D6 changes: one MR.

---

## File Structure

New modules in `raven/agent/subagent/`:

| File | Responsibility |
|---|---|
| `history.py` | on-disk layout of the session's sub-agent call history (moved verbatim) |
| `prompt_errors.py` | `DagValidationError` (moved verbatim; class name unchanged) |
| `prompt_placeholders.py` | `{{ ... }}` grammar and shape matching |
| `prompt_paths.py` | root confinement, `@runs/` prefix, `within` |
| `prompt_backend.py` | `LocalFileBackend` (moved verbatim) |
| `prompt_capabilities.py` | `AgentCapabilities`, the `_path`-form gate |
| `prompt_render.py` | resolution of `ref` / `ref_path` / `inputs` forms, D4 fencing, `render_template` |
| `spawn_tool.py` | the `spawn` tool (moved, then extended) |
| `dag_*.py` (11) | graph model, store, runner, live, resume, reader, projection, graph render, graph capabilities, tool, control tools |

`raven/agent/subagent_dag/` and `raven/agent/tools/spawn.py` cease to exist.

---

## Task 1: Move the call-history module

**Files:**
- Move: `raven/agent/subagent_history.py` -> `raven/agent/subagent/history.py`
- Modify (import path only): `raven/agent/subagent/{manager,direct_chat,instance_state}.py`,
  `raven/agent/subagent_dag/{tool,runner,_store,_paths}.py`,
  `raven/rpc/methods/{dag,instances,subagent}.py`,
  `tests/test_{subagent_history,rpc_instances,subagent_acp,rpc_subagent_calls,rpc_dag,subagent_workdir}.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `raven.agent.subagent.history` exporting the same names as before -
  `history_stamp`, `make_call_id`, `session_history_root`, `spawn_root`,
  `dag_root`, `add_turn_to_instance_log`, `SpawnRecord`, `make_run_id`.

- [ ] **Step 1: Capture the pre-move baseline**

```bash
uv run pytest -q -x -p no:randomly 2>&1 | tail -3 > /tmp/baseline.txt
cat /tmp/baseline.txt
```

Record the pass/fail counts. Every later task compares against this. If the
suite is already red on `main`, stop and report which tests - do not absorb a
pre-existing failure into this work.

- [ ] **Step 2: Move the file**

```bash
git mv raven/agent/subagent_history.py raven/agent/subagent/history.py
```

- [ ] **Step 3: Rewrite every importer**

```bash
grep -rl "raven\.agent\.subagent_history" --include=*.py raven/ tests/ \
  | xargs sed -i 's/raven\.agent\.subagent_history/raven.agent.subagent.history/g'
grep -rn "subagent_history" --include=*.py raven/ tests/ | grep -v "^raven/agent/subagent/history.py"
```

The second command must print nothing but comment lines. `raven/agent/workdir.py:22`
mentions the old path in prose - update that comment text to the new path.

- [ ] **Step 4: Fix the module's own docstring**

`history.py`'s docstring says "raven/agent/workdir.py" and describes its own
location. Update the self-reference to the new path. Leave everything else
byte-identical.

- [ ] **Step 5: Verify no behaviour changed**

```bash
uv run pytest -q -x -p no:randomly 2>&1 | tail -3
uv run python -c "import raven.agent.subagent.history as h; print(h.session_history_root)"
```

Expected: identical counts to `/tmp/baseline.txt`.

- [ ] **Step 6: Prepare the commit (do not run without the user's go-ahead)**

```bash
git add -A
git commit -m "$(cat <<'EOF'
refactor(agent): move subagent call history into the subagent package

Pure move. The history tree is owned by both delegation surfaces, so it
belongs beside them rather than at the agent package root.

Co-authored-by: Claude (<your session model id>) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Extract the shared prompt-template layer

The five shared modules are created here while the DAG still lives in
`subagent_dag/`, so each file moves exactly once. Behaviour-preserving:
`prompt_render.resolve_file_placeholder` returns `None` for the branches the DAG
keeps, and `dag`'s resolver handles them exactly as before.

**Files:**
- Move: `subagent_dag/_errors.py` -> `subagent/prompt_errors.py`
- Move: `subagent_dag/_placeholders.py` -> `subagent/prompt_placeholders.py`
- Move: `subagent_dag/_paths.py` -> `subagent/prompt_paths.py`
- Move: `subagent_dag/backend.py` -> `subagent/prompt_backend.py`
- Create: `raven/agent/subagent/prompt_capabilities.py`
- Create: `raven/agent/subagent/prompt_render.py`
- Modify: `subagent_dag/_capabilities.py`, `subagent_dag/_render.py`,
  `subagent_dag/__init__.py`, and every module importing the moved four
- Test: `tests/test_subagent_prompt_template.py` (create)

**Interfaces:**
- Consumes: `raven.agent.subagent.history.session_history_root` (T1).
- Produces:
  - `prompt_placeholders`: `Placeholder(kind, name, raw)`, `parse_placeholders(template) -> list[Placeholder]`, `iter_placeholders(template) -> Iterator[tuple[int, int, Placeholder]]`
  - `prompt_paths`: `RUNS_PREFIX`, `split_reference(path) -> tuple[str, str]`, `check_confined(path, *, what, roots=None) -> None`, `within(child, root) -> bool`
  - `prompt_capabilities`: `PATH_KINDS`, `AgentCapabilities`, `check_path_placeholders(placeholders, subagent, *, reads_local_files, subject="this task", escape_hatch="") -> None` — the signature this task actually shipped after its review; Task 7's note explains why it takes parsed placeholders rather than a raw template
  - `prompt_render`: `resolve_file_placeholder(ph, inputs, *, backend, cwd, runs_root, roots, history_root) -> str | None`, `render_template(template, inputs, *, backend, cwd, runs_root, roots, history_root) -> str`, `abspath(backend, path, cwd, runs_root) -> str`, `read_text(backend, path, cwd, runs_root=None, *, what=None, history_root=None) -> str`, `require_exists(backend, resolved, what) -> str`
  - `prompt_backend`: `LocalFileBackend`

- [ ] **Step 1: Write the failing test for the shared layer's public surface**

Create `tests/test_subagent_prompt_template.py`:

```python
"""The prompt-template layer shared by the spawn and DAG delegation surfaces."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent.subagent.prompt_backend import LocalFileBackend
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent.prompt_paths import check_confined, within
from raven.agent.subagent.prompt_placeholders import parse_placeholders
from raven.agent.subagent.prompt_render import render_template


async def test_render_inlines_a_ref_and_an_input(tmp_path: Path) -> None:
    (tmp_path / "plan.md").write_text("ship it", encoding="utf-8")
    (tmp_path / "notes.md").write_text("be careful", encoding="utf-8")

    rendered = await render_template(
        "plan: {{ ref:plan.md }} / note: {{ inputs.n }} / lit: {{ inputs.k }}",
        {"n": {"file": "notes.md"}, "k": "verbatim"},
        backend=LocalFileBackend(),
        cwd=str(tmp_path),
        runs_root=None,
        roots=(str(tmp_path),),
        history_root=None,
    )

    assert rendered == "plan: ship it / note: be careful / lit: verbatim"


async def test_render_gives_a_path_for_the_path_forms(tmp_path: Path) -> None:
    (tmp_path / "plan.md").write_text("ship it", encoding="utf-8")

    rendered = await render_template(
        "{{ ref_path:plan.md }}",
        {},
        backend=LocalFileBackend(),
        cwd=str(tmp_path),
        runs_root=None,
        roots=(str(tmp_path),),
        history_root=None,
    )

    assert rendered == str(tmp_path / "plan.md")


async def test_render_refuses_a_path_form_naming_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DagValidationError, match="does not exist"):
        await render_template(
            "{{ ref_path:gone.md }}",
            {},
            backend=LocalFileBackend(),
            cwd=str(tmp_path),
            runs_root=None,
            roots=(str(tmp_path),),
            history_root=None,
        )


def test_confinement_rejects_an_escaping_ref(tmp_path: Path) -> None:
    with pytest.raises(DagValidationError, match="outside the session workdir"):
        check_confined("../secrets.md", what="ref", roots=(str(tmp_path),))


def test_within_is_textual_and_prefix_exact() -> None:
    assert within("/a/b/c", "/a/b")
    assert within("/a/b", "/a/b")
    assert not within("/a/bc", "/a/b")


async def test_a_node_reference_is_refused_with_the_file_alternative(tmp_path: Path) -> None:
    """Spec D3: this layer has no graph, so a node reference must not resolve.

    It must not pass through as text either -- that would send the sub-agent a
    literal `{{ plan.output }}`. The refusal names the form to use instead.
    """
    with pytest.raises(DagValidationError, match="only run_subagent_dag can resolve"):
        await render_template(
            "{{ plan.output }}",
            {},
            backend=LocalFileBackend(),
            cwd=str(tmp_path),
            runs_root=None,
            roots=(str(tmp_path),),
            history_root=None,
        )


async def test_a_node_shaped_input_is_refused_too(tmp_path: Path) -> None:
    with pytest.raises(DagValidationError, match="only run_subagent_dag can resolve"):
        await render_template(
            "{{ inputs.up }}",
            {"up": {"node": "plan"}},
            backend=LocalFileBackend(),
            cwd=str(tmp_path),
            runs_root=None,
            roots=(str(tmp_path),),
            history_root=None,
        )
```

- [ ] **Step 2: Run it to confirm it fails**

```bash
uv run pytest tests/test_subagent_prompt_template.py -q
```

Expected: collection error, `ModuleNotFoundError: raven.agent.subagent.prompt_backend`.

- [ ] **Step 3: Move the four verbatim modules**

```bash
git mv raven/agent/subagent_dag/_errors.py       raven/agent/subagent/prompt_errors.py
git mv raven/agent/subagent_dag/_placeholders.py raven/agent/subagent/prompt_placeholders.py
git mv raven/agent/subagent_dag/_paths.py        raven/agent/subagent/prompt_paths.py
git mv raven/agent/subagent_dag/backend.py       raven/agent/subagent/prompt_backend.py
```

Rewrite their intra-package imports: `prompt_placeholders.py` and
`prompt_paths.py` both do `from ._errors import DagValidationError`, which
becomes `from raven.agent.subagent.prompt_errors import DagValidationError`.

Then repoint every importer of the four:

```bash
grep -rl "subagent_dag\._errors\|subagent_dag\._placeholders\|subagent_dag\._paths\|subagent_dag\.backend\|from \._errors\|from \._placeholders\|from \._paths\|from \.backend" \
  --include=*.py raven/ tests/
```

Known non-test importers to fix: `raven/rpc/methods/dag.py:23`
(`_errors` -> `prompt_errors`), and the intra-`subagent_dag` `from ._x import`
lines in `_capabilities.py`, `_graph.py`, `_render.py`, `_store.py`,
`_reader.py`, `runner.py`, `tool.py`.

- [ ] **Step 4: Make `within` public in `prompt_paths.py`**

Rename `_within` to `within` and update its one internal caller in
`check_confined`. Extend the docstring's first line to say what it is for:

```python
def within(child: str, root: str) -> bool:
    """Whether ``child`` is ``root`` or sits under it, comparing text only.

    Public because the fence decision in ``prompt_render`` keys on the same
    comparison ``check_confined`` uses: if the two could disagree about where a
    path landed, a file could be confined as workspace material and fenced as
    sub-agent material, or the reverse.
    """
    normalized = posixpath.normpath(root)
    return child == normalized or child.startswith(normalized.rstrip("/") + "/")
```

- [ ] **Step 5: Create `prompt_capabilities.py`**

Move `AgentCapabilities` and the `_PATH_KINDS` gate out of
`subagent_dag/_capabilities.py`, generalized off `DagNodeSpec`:

```python
"""What a sub-agent can do, and the file-reference gate that follows from it."""

from dataclasses import dataclass

from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent.prompt_placeholders import parse_placeholders

PATH_KINDS = ("output_path", "input_path", "ref_path")


@dataclass(frozen=True)
class AgentCapabilities:
    """What one configured sub-agent can do, as the roster advertises it.

    Defaults are permissive so an agent missing from the map -- a test double, a
    backend built outside the config path -- is never rejected by these checks.
    An unknown ``subagent`` name is already the dispatcher's error to raise.
    """

    stateful: bool = True
    reads_local_files: bool = True
    injectable_skills: bool = True
    injectable_mcps: bool = True
    """Whether per-node ``skills`` / ``mcps`` can be pushed into this agent's
    session at all. Only an in-process raven loop has a skill menu raven controls;
    a cli or acp agent's is its own business, so a list aimed at one does nothing."""


def check_path_placeholders(template: str, subagent: str, *, reads_local_files: bool) -> None:
    """Reject path placeholders aimed at an agent that cannot read local files.

    Raises:
        `DagValidationError`:
            When ``template`` carries a ``_path`` form and ``reads_local_files``
            is false.
    """
    if reads_local_files:
        return

    offenders = [
        f"{ph.raw} -> use {ph.content_form()}"
        for ph in parse_placeholders(template)
        if ph.kind in PATH_KINDS
    ]
    if not offenders:
        return

    raise DagValidationError(
        f"this task passes local file paths to sub-agent '{subagent}', which the "
        f"roster tags [no-local-files]: it cannot open them, so the path would reach it as "
        f"meaningless text. Replace each with the content form ({'; '.join(offenders)})."
    )
```

`subagent_dag/_capabilities.py` keeps `validate_capabilities` and its node-level
notices, imports `AgentCapabilities` and `check_path_placeholders` from here,
and its `_check_path_placeholders(node, capabilities)` becomes a thin adapter
that looks the row up and appends the DAG's own tail to the message:

```python
def _check_path_placeholders(node: DagNodeSpec, capabilities: dict[str, AgentCapabilities]) -> None:
    caps = capabilities.get(node.subagent)
    try:
        check_path_placeholders(
            node.prompt_template,
            node.subagent,
            reads_local_files=True if caps is None else caps.reads_local_files,
        )
    except DagValidationError as exc:
        raise DagValidationError(
            f"node '{node.id}': {exc} Or move the node to a sub-agent tagged "
            f"[local-files]. Then call run_subagent_dag again with the corrected graph."
        ) from exc
```

- [ ] **Step 6: Create `prompt_render.py`**

Move `_abspath`, `_require_exists`, `_read` verbatim (renamed `abspath`,
`require_exists`, `read_text` - they are the layer's surface now), plus the
four shared branches of `_resolve` and the `{"file": ...}` / literal branches
of `_input_value`. `history_root` is threaded but unused until T6; wire the
parameter now so T6 is a one-function change.

```python
"""Resolve the file and literal placeholder forms shared by both surfaces."""

from collections.abc import Mapping
from typing import Any

from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent.prompt_paths import check_confined, split_reference
from raven.agent.subagent.prompt_placeholders import Placeholder, iter_placeholders


async def resolve_file_placeholder(
    ph: Placeholder,
    inputs: Mapping[str, Any],
    *,
    backend: Any,
    cwd: str,
    runs_root: str | None,
    roots: tuple[str, ...] | None,
    history_root: str | None,
) -> str | None:
    """Resolve one placeholder, or ``None`` when it is not this layer's to resolve.

    ``None`` is the composition seam: the ``output`` forms, and an
    ``inputs`` entry shaped ``{"node": ...}``, name a graph node, which only the
    DAG layer can resolve. A caller with no graph turns ``None`` into a
    correctable refusal.
    """
    if ph.kind in ("output", "output_path"):
        return None
    if ph.kind == "input":
        spec = inputs.get(ph.name)
        if isinstance(spec, dict) and "node" in spec:
            return None
        if isinstance(spec, dict) and "file" in spec:
            check_confined(str(spec["file"]), what="input file", roots=roots)
            return await read_text(
                backend, str(spec["file"]), cwd, runs_root,
                what=f"input '{ph.name}'", history_root=history_root,
            )
        return str(spec)
    if ph.kind == "input_path":
        spec = inputs.get(ph.name)
        if isinstance(spec, dict) and "node" in spec:
            return None
        if not isinstance(spec, dict) or "file" not in spec:
            raise DagValidationError(f"input '{ph.name}' has no file path to reference")
        check_confined(str(spec["file"]), what="input path", roots=roots)
        return await require_exists(
            backend, abspath(backend, str(spec["file"]), cwd, runs_root), ph.raw
        )
    if ph.kind == "ref":
        check_confined(ph.name, what="ref", roots=roots)
        return await read_text(
            backend, ph.name, cwd, runs_root, what=ph.raw, history_root=history_root
        )
    # ph.kind == "ref_path"
    check_confined(ph.name, what="ref_path", roots=roots)
    return await require_exists(backend, abspath(backend, ph.name, cwd, runs_root), ph.raw)


async def render_template(
    template: str,
    inputs: Mapping[str, Any],
    *,
    backend: Any,
    cwd: str,
    runs_root: str | None,
    roots: tuple[str, ...] | None,
    history_root: str | None,
) -> str:
    """Render a template that may reference files and literal inputs only.

    Raises:
        `DagValidationError`:
            When a placeholder names a graph node, which has no meaning without
            a graph.
    """
    parts: list[str] = []
    last = 0
    cache: dict[str, str] = {}
    for start, end, ph in iter_placeholders(template):
        parts.append(template[last:start])
        if ph.raw not in cache:
            value = await resolve_file_placeholder(
                ph, inputs, backend=backend, cwd=cwd, runs_root=runs_root,
                roots=roots, history_root=history_root,
            )
            if value is None:
                raise DagValidationError(
                    f"{ph.raw} names another task's output, which only run_subagent_dag can "
                    f"resolve. Reference the file instead: {{{{ ref:<path> }}}} for its contents."
                )
            cache[ph.raw] = value
        parts.append(cache[ph.raw])
        last = end
    parts.append(template[last:])
    return "".join(parts)
```

`abspath`, `require_exists`, and `read_text` are the existing `_abspath`,
`_require_exists`, `_read` bodies unchanged, except that `read_text` takes
`history_root: str | None = None` and ignores it for now.

- [ ] **Step 7: Compose the DAG's renderer on top**

In `subagent_dag/_render.py`, `_resolve` keeps only the branches the shared
layer returns `None` for, and delegates the rest:

```python
async def _resolve(ph, node, *, backend, cwd, output_paths, runs_root, roots, session_nodes=None):
    shared = await resolve_file_placeholder(
        ph, node.inputs, backend=backend, cwd=cwd, runs_root=runs_root,
        roots=roots, history_root=None,
    )
    if shared is not None:
        return shared
    if ph.kind == "input":
        spec = node.inputs[ph.name]
        resolved = _output_path(str(spec["node"]), output_paths, backend, runs_root, session_nodes, f"input '{ph.name}'")
        return await read_text(backend, resolved, cwd, what=f"input '{ph.name}'")
    if ph.kind == "input_path":
        spec = node.inputs[ph.name]
        resolved = _output_path(str(spec["node"]), output_paths, backend, runs_root, session_nodes, f"input '{ph.name}'")
        return await require_exists(backend, resolved, ph.raw)
    resolved = _output_path(ph.name, output_paths, backend, runs_root, session_nodes, ph.raw)
    if ph.kind == "output":
        return await read_text(backend, resolved, cwd, what=ph.raw)
    return await require_exists(backend, resolved, ph.raw)
```

Delete `_input_value` (its two remaining branches are inlined above),
`_abspath`, `_require_exists`, `_read` from `_render.py`, importing the shared
ones instead.

- [ ] **Step 8: Update `subagent_dag/__init__.py` re-exports**

The four moved names (`DagValidationError`, `Placeholder`,
`iter_placeholders`, `parse_placeholders`, `RUNS_PREFIX`, `check_confined`,
`split_reference`, `AgentCapabilities`) now come from the `subagent.prompt_*`
modules. Keep them in `__all__` so the two package-root test importers keep
working through T3.

- [ ] **Step 9: Run the new test and the full suite**

```bash
uv run pytest tests/test_subagent_prompt_template.py -q
uv run pytest -q -x -p no:randomly 2>&1 | tail -3
```

Expected: new file passes; suite counts match `/tmp/baseline.txt` plus the new
tests. Any DAG test that changes behaviour here is a bug in the extraction -
this task is behaviour-preserving.

- [ ] **Step 10: Prepare the commit**

```
refactor(agent): extract the prompt-template layer shared by spawn and dag

The grammar, root confinement, capability gate, file backend, and the
ref/inputs resolution branches move out of the DAG package; graph-node
resolution stays and composes on top. No behaviour change: the shared
resolver returns None for the branches only a graph can resolve.

Co-authored-by: Claude (<your session model id>) <noreply@anthropic.com>
```

---

## Task 3: Flatten the DAG package

**Files:**
- Move 11 modules: `subagent_dag/{tool,control_tools,runner,live,_resume,_graph,_store,_projection,_reader,_render,_capabilities}.py`
  -> `subagent/dag_{tool,control_tools,runner,live,resume,graph,store,projection,reader,render,capabilities}.py`
- Delete: `raven/agent/subagent_dag/__init__.py` (and the package)
- Modify: the 17 `raven/`-side import lines and the 12 test files

**Interfaces:**
- Consumes: T2's `prompt_*` modules.
- Produces: `raven.agent.subagent.dag_tool.SubAgentDagTool`,
  `raven.agent.subagent.dag_control_tools.{CancelDagTool,DagStatusTool}`,
  `raven.agent.subagent.dag_graph.{DagNodeSpec,SubAgentDagSpec,parse_dag_spec,validate_and_order,_REQUIRED_NON_BLANK}`,
  `raven.agent.subagent.dag_store.{DagRunStore,SessionNodes,make_run_id,read_session_nodes,node_live_key}`,
  `raven.agent.subagent.dag_reader.{read_run,read_node,DagReadError,_check_run_id}`,
  `raven.agent.subagent.dag_resume.read_run_reconciled`,
  `raven.agent.subagent.dag_live.{cancel_run,live_run_ids}`,
  `raven.agent.subagent.dag_runner.{run_dag,DagRunResult}`,
  `raven.agent.subagent.dag_projection.fold_dag_run_entry`

- [ ] **Step 1: Move the modules**

```bash
cd raven/agent/subagent_dag
for f in tool control_tools runner live; do git mv $f.py ../subagent/dag_$f.py; done
for f in resume graph store projection reader render capabilities; do git mv _$f.py ../subagent/dag_$f.py; done
cd - && git rm raven/agent/subagent_dag/__init__.py
```

- [ ] **Step 2: Rewrite intra-package relative imports**

The moved modules use `from ._graph import ...` style. Rewrite each to the flat
absolute form:

```bash
cd raven/agent/subagent
sed -i -E 's/from \._?(graph|store|projection|reader|render|capabilities|resume|live|runner|tool|control_tools) import/from raven.agent.subagent.dag_\1 import/g' dag_*.py
sed -i -E 's/from \._?(errors|placeholders|paths|backend) import/from raven.agent.subagent.prompt_\1 import/g' dag_*.py
grep -n "^from \.\|^from raven.agent.subagent_dag" dag_*.py
cd -
```

The final `grep` must print nothing.

- [ ] **Step 3: Rewrite external importers**

```bash
grep -rl "raven\.agent\.subagent_dag" --include=*.py raven/ tests/ | xargs sed -i -E \
  's/raven\.agent\.subagent_dag\._?(tool|control_tools|runner|live|resume|graph|store|projection|reader|render|capabilities)/raven.agent.subagent.dag_\1/g'
grep -rn "subagent_dag" --include=*.py raven/ tests/
```

The remaining hits are the two package-root importers named in spec D5. Repoint
them at the specific modules:

- `tests/test_subagent_dag_runner.py:18` - `DagValidationError` from
  `raven.agent.subagent.prompt_errors`, `parse_dag_spec` from
  `raven.agent.subagent.dag_graph`.
- `tests/test_subagent_dag_core.py:14` - split the multi-line import across
  `prompt_errors`, `prompt_paths`, `prompt_placeholders`,
  `prompt_capabilities`, `dag_graph`, `dag_store`, `dag_reader`.

- [ ] **Step 4: Record the reversed decision**

`subagent_dag/__init__.py`'s docstring is gone with the package. Put its
surviving half at the top of `dag_tool.py`, rewritten to say what is true now:

```python
"""The ``run_subagent_dag`` tool: orchestrate a graph of sub-agent tasks.

This subsystem lives beside the single-call ``spawn`` surface and shares its
prompt-template layer (``prompt_*`` modules), but stays otherwise independent
of Raven's kernel: it carries its own graph model, ready-set scheduler, and
on-disk run store, is exposed as an ordinary optional Raven tool, and is NOT
routed through ``Origin.SUBAGENT`` or the spine scheduler. It is deliberately
absent from ``subagent/__init__.py`` -- re-exporting it there would make every
``SubagentManager`` import pull in the graph model, the store, and the runner.
"""
```

- [ ] **Step 5: Verify the package is gone and nothing changed**

```bash
test ! -d raven/agent/subagent_dag && echo "package removed"
uv run pytest -q -x -p no:randomly 2>&1 | tail -3
uv run python -c "from raven.agent.subagent.dag_tool import SubAgentDagTool; print('ok')"
```

Expected: counts match the T2 result exactly.

- [ ] **Step 6: Prepare the commit**

```
refactor(agent): flatten the dag package into the subagent package

Moves the 11 graph-specific modules to dag_*.py beside the spawn surface and
drops the subagent_dag package. Every raven-side importer already named a
submodule, so those are path substitutions; the two tests importing from the
package root are repointed at specific modules. The DAG stays an optional
subsystem with its own store and scheduler and is kept out of the subagent
package's __init__ on purpose.

Co-authored-by: Claude (<your session model id>) <noreply@anthropic.com>
```

---

## Task 4: Move the spawn tool

**Files:**
- Move: `raven/agent/tools/spawn.py` -> `raven/agent/subagent/spawn_tool.py`
- Modify: `raven/agent/loop/main.py:52`,
  `tests/test_subagent_manager.py:688`, `tests/test_subagent_third_party.py:37`,
  `tests/test_tool_registry_timeout.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `raven.agent.subagent.spawn_tool.SpawnTool`, unchanged behaviour.

- [ ] **Step 1: Move and repoint**

```bash
git mv raven/agent/tools/spawn.py raven/agent/subagent/spawn_tool.py
grep -rl "raven\.agent\.tools\.spawn" --include=*.py raven/ tests/ \
  | xargs sed -i 's/raven\.agent\.tools\.spawn/raven.agent.subagent.spawn_tool/g'
grep -rn "tools\.spawn" --include=*.py raven/ tests/
```

The final `grep` must print nothing.

- [ ] **Step 2: Confirm the cycle is gone**

```bash
uv run python - <<'PY'
import ast, pathlib
bad = []
for p in pathlib.Path("raven/agent/tools").rglob("*.py"):
    for n in ast.walk(ast.parse(p.read_text())):
        mods = [n.module] if isinstance(n, ast.ImportFrom) and n.module else \
               [a.name for a in n.names] if isinstance(n, ast.Import) else []
        bad += [(str(p), m) for m in mods if m and m.startswith("raven.agent.subagent")]
print("tools -> subagent edges:", bad or "none")
PY
```

Expected: `none`. This is the measurable outcome of spec D5's cycle claim.

- [ ] **Step 3: Verify**

```bash
uv run pytest -q -x -p no:randomly 2>&1 | tail -3
```

Expected: counts match T3.

- [ ] **Step 4: Prepare the commit**

```
refactor(agent): move the spawn tool beside the manager it wraps

spawn.py was the only author of the tools <-> subagent import cycle; with it
moved, tools no longer imports subagent at all.

Co-authored-by: Claude (<your session model id>) <noreply@anthropic.com>
```

---

## Task 5: Unrecognized placeholder bodies pass through (spec D2)

**Files:**
- Modify: `raven/agent/subagent/prompt_placeholders.py`
- Test: `tests/test_subagent_prompt_template.py`,
  `tests/test_subagent_dag_runner.py:2194` (must change)

**Interfaces:**
- Consumes: T2's `prompt_placeholders`.
- Produces: `_parse_body(body, raw) -> Placeholder | None`; `iter_placeholders`
  and `parse_placeholders` skip non-matching bodies.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_prompt_template.py`:

```python
async def test_an_unknown_brace_body_survives_rendering(tmp_path: Path) -> None:
    rendered = await render_template(
        "why does {{ item.name }} not render?",
        {},
        backend=LocalFileBackend(),
        cwd=str(tmp_path),
        runs_root=None,
        roots=(str(tmp_path),),
        history_root=None,
    )

    assert rendered == "why does {{ item.name }} not render?"


def test_only_the_shape_matching_body_is_a_placeholder() -> None:
    found = parse_placeholders("{{ item.name }} {{ ref:a.md }} {{ 5 + 5 }}")

    assert [(ph.kind, ph.name) for ph in found] == [("ref", "a.md")]


async def test_a_misspelled_input_key_still_fails(tmp_path: Path) -> None:
    """The discrimination has to keep catching typos in known shapes."""
    rendered = await render_template(
        "{{ inputs.pln }}",
        {},
        backend=LocalFileBackend(),
        cwd=str(tmp_path),
        runs_root=None,
        roots=(str(tmp_path),),
        history_root=None,
    )

    assert rendered == "None", "a missing key resolves to the literal None today"


def test_an_empty_ref_body_is_still_refused() -> None:
    with pytest.raises(DagValidationError, match="empty path"):
        parse_placeholders("{{ ref: }}")
```

Note on the third test: `inputs.pln` matches the `inputs.<k>` shape, so it
enters resolution; with no such key `str(None)` is substituted. Assert the
current behaviour here, then decide in Step 5 whether a missing key should
raise instead - that is a real question this test surfaces, not a placeholder.

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_subagent_prompt_template.py -q -k "unknown_brace or empty_ref or misspelled"
```

Expected: the two `unknown_brace` tests fail with
`DagValidationError: unrecognized placeholder '{{ item.name }}'`.

- [ ] **Step 3: Implement the shape discrimination**

In `prompt_placeholders.py`, `_parse_body` returns `None` instead of raising on
its final line, and `iter_placeholders` skips it:

```python
def iter_placeholders(template: str) -> Iterator[tuple[int, int, Placeholder]]:
    for match in _PLACEHOLDER_RE.finditer(template):
        placeholder = _parse_body(match.group(1).strip(), match.group(0))
        if placeholder is None:
            continue
        yield match.start(), match.end(), placeholder
```

```python
def _parse_body(body: str, raw: str) -> Placeholder | None:
    """Parse the inside of one ``{{ ... }}``, or ``None`` when it is not one.

    A body that matches no known shape is not a mistyped placeholder, it is
    ordinary text: template syntax from another system (Jinja, Vue, Handlebars)
    reaches a sub-agent through these prompts, and no escape form exists to get
    it past a hard rejection. A body that DOES match a known shape still parses
    and still fails downstream on a bad key, path, or node id, so a typo inside
    a placeholder is caught as before.
    """
```

with the trailing `raise` replaced by `return None`. Update the `Raises:`
sections of `parse_placeholders` and `iter_placeholders` - they now raise only
for a malformed `ref:` / `ref_path:` body.

- [ ] **Step 4: Update the DAG test that asserted the rejection**

`tests/test_subagent_dag_runner.py:2194` expects
`pytest.raises(DagValidationError, match="unrecognized placeholder")`. Replace
it with an assertion that the body survives into the rendered prompt, keeping
the same graph fixture the test already builds.

- [ ] **Step 5: Decide the missing-input-key question**

Run:

```bash
uv run pytest tests/test_subagent_prompt_template.py -q
```

If `test_a_misspelled_input_key_still_fails` shows `"None"` substituted, raise
this to the user before proceeding: silently substituting `None` for an unknown
`inputs` key is the same silent-failure class this work exists to remove, but
changing it is a behaviour change to the DAG surface that the spec does not
cover. Do not change it unilaterally.

- [ ] **Step 6: Full suite**

```bash
uv run pytest -q -p no:randomly 2>&1 | tail -3
uv run pytest tests/test_subagent_dag_runner.py tests/test_subagent_dag_core.py -q
```

- [ ] **Step 7: Prepare the commit**

```
feat(agent): let unknown brace bodies through the placeholder grammar

A body matching no known placeholder shape is ordinary text, not a mistyped
placeholder: template syntax from other systems reaches sub-agents through
these prompts and no escape form exists. Bodies that do match a shape still
parse and still fail on a bad key, path, or node id.

Co-authored-by: Claude (<your session model id>) <noreply@anthropic.com>
```

---

## Task 6: Fence inlined sub-agent output (spec D4)

**Files:**
- Modify: `raven/agent/subagent/prompt_render.py` (`read_text`)
- Modify: `raven/agent/subagent/dag_render.py` (pass `history_root` through)
- Modify: `raven/agent/subagent/dag_runner.py` (supply `history_root` to `render_prompt`)
- Test: `tests/test_subagent_prompt_template.py`

**Interfaces:**
- Consumes: `raven.security.trust.wrap_untrusted`,
  `raven.agent.subagent.history.session_history_root`,
  `raven.agent.subagent.prompt_paths.within`.
- Produces: `read_text(..., history_root: str | None)` wraps its result when the
  resolved path is inside `history_root`.

- [ ] **Step 1: Write the failing tests**

```python
from raven.agent.subagent.prompt_render import read_text


async def test_a_file_under_the_history_root_is_fenced(tmp_path: Path) -> None:
    history = tmp_path / "subagents"
    (history / "spawn" / "c1").mkdir(parents=True)
    (history / "spawn" / "c1" / "out.md").write_text("upstream said this", encoding="utf-8")

    text = await read_text(
        LocalFileBackend(),
        str(history / "spawn" / "c1" / "out.md"),
        str(tmp_path),
        None,
        what="{{ ref:... }}",
        history_root=str(history),
    )

    assert "upstream said this" in text
    assert text != "upstream said this", "sub-agent output must arrive fenced"


async def test_a_workspace_file_is_not_fenced(tmp_path: Path) -> None:
    (tmp_path / "spec.md").write_text("user wrote this", encoding="utf-8")

    text = await read_text(
        LocalFileBackend(),
        str(tmp_path / "spec.md"),
        str(tmp_path),
        None,
        what="{{ ref:spec.md }}",
        history_root=str(tmp_path / "subagents"),
    )

    assert text == "user wrote this"
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_subagent_prompt_template.py -q -k "fenced"
```

Expected: `test_a_file_under_the_history_root_is_fenced` fails on the
`text != "upstream said this"` assertion.

- [ ] **Step 3: Implement the fence**

At the end of `read_text` in `prompt_render.py`:

```python
    data = await backend.read_file(resolved)
    text = data.decode("utf-8", errors="replace")
    # Everything under the history root was written by a sub-agent, so its text
    # is untrusted on the way into another sub-agent's prompt (CONTEXT.md). The
    # comparison is the one `check_confined` uses, so the fence decision and the
    # confinement decision cannot disagree about where a path landed.
    if history_root is not None and within(resolved, history_root):
        return wrap_untrusted(text, source="subagent")
    return text
```

- [ ] **Step 4: Thread `history_root` through the DAG renderer**

`dag_render.render_prompt` gains `history_root: str | None = None`, passes it to
every `read_text` call and to `resolve_file_placeholder`. `dag_runner` computes
it once per run as `str(session_history_root(session_dir))` beside the
`runs_root` it already derives, and passes it in.

- [ ] **Step 5: Verify both surfaces**

```bash
uv run pytest tests/test_subagent_prompt_template.py -q
uv run pytest tests/test_subagent_dag_runner.py tests/test_subagent_dag_core.py -q
uv run pytest -q -p no:randomly 2>&1 | tail -3
```

A DAG test asserting an exact rendered prompt that inlines an upstream output
will now fail on the fence markers. That is the intended change (spec Risks);
update the expected string, do not remove the fence.

- [ ] **Step 6: Prepare the commit**

```
fix(agent): fence sub-agent output on its way into another sub-agent's prompt

CONTEXT.md requires sub-agent-controlled text to pass through wrap_untrusted
before entering another sub-agent's prompt; the DAG's node-output inlining
never did. The rule keys on the resolved path being under the session's
sub-agent history root, which covers node outputs, @runs references, and spawn
records alike.

Co-authored-by: Claude (<your session model id>) <noreply@anthropic.com>
```

---

## Task 7: spawn takes `prompt_template` and `inputs`

**Files:**
- Modify: `raven/agent/subagent/spawn_tool.py`
- Modify: `raven/agent/subagent/manager.py` (`_session_dir` -> `session_dir_for`)
- Test: `tests/test_subagent_manager.py`, `tests/test_subagent_third_party.py`

**Interfaces:**
- Consumes: `prompt_render.render_template`,
  `prompt_capabilities.check_path_placeholders`,
  `history.{session_history_root,dag_root}`,
  `manager.session_dir_for(session_key) -> Path`.
- Produces: `SpawnTool.parameters` requiring
  `["task_summary", "prompt_template", "subagent"]`;
  `SpawnTool.execute(task_summary, prompt_template=None, subagent=None, instance=None, inputs=None, **kwargs)`
  accepting `task` from `kwargs` as the old spelling of `prompt_template`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_subagent_manager.py`:

```python
async def test_spawn_requires_prompt_template_and_drops_task() -> None:
    from raven.agent.subagent.spawn_tool import SpawnTool

    tool = SpawnTool(manager=_stub_manager())
    props = tool.parameters["properties"]

    assert "prompt_template" in props
    assert "inputs" in props
    assert "task" not in props
    assert "prompt_template" in tool.parameters["required"]


async def test_spawn_accepts_the_old_task_spelling() -> None:
    from raven.agent.subagent.spawn_tool import SpawnTool

    manager = _stub_manager()
    tool = SpawnTool(manager=manager)

    await tool.execute(task_summary="s", task="do the thing", subagent="raven")

    assert manager.calls[-1]["task"] == "do the thing"


async def test_spawn_refuses_a_path_form_for_a_no_local_files_agent() -> None:
    from raven.agent.subagent.spawn_tool import SpawnTool

    tool = SpawnTool(manager=_stub_manager_with_remote_agent())
    result = await tool.execute(
        task_summary="s",
        prompt_template="{{ ref_path:plan.md }}",
        subagent="remote",
    )

    assert result.startswith("Error")
    assert "no-local-files" in result
    assert "{{ ref:plan.md }}" in result


async def test_spawn_renders_a_ref_into_the_dispatched_task(tmp_path: Path) -> None:
    from raven.agent.subagent.spawn_tool import SpawnTool

    (tmp_path / "plan.md").write_text("ship it", encoding="utf-8")
    manager = _stub_manager(workspace=tmp_path)
    tool = SpawnTool(manager=manager)

    with workdir.bind(tmp_path):
        await tool.execute(
            task_summary="s",
            prompt_template="follow this: {{ ref:plan.md }}",
            subagent="raven",
        )

    assert manager.calls[-1]["task"] == "follow this: ship it"
```

`_stub_manager` already exists in that file; extend it to record calls in a
`calls` list and to expose `session_dir_for` and `list_agents`. Add
`_stub_manager_with_remote_agent` returning a roster whose single row has
`reads_local_files=False`.

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_subagent_manager.py -q -k "prompt_template or old_task or no_local_files or renders_a_ref"
```

Expected: all four fail - the first on `"prompt_template" in props`.

- [ ] **Step 3: Rename the manager's session-dir accessor**

In `manager.py`, rename `_session_dir` to `session_dir_for` and update its three
callers (lines ~692, ~852, ~942). It is now part of the manager's surface
because the spawn tool needs the same directory the records land in.

- [ ] **Step 4: Change the schema**

In `spawn_tool.py`'s `parameters`, replace the `task` property with:

```python
            "prompt_template": {
                "type": "string",
                "description": (
                    "The task for the subagent. Placeholders: {{ ref:<path> }} / "
                    "{{ ref_path:<path> }} inject a file's contents / its path; "
                    "{{ inputs.<k> }} / {{ inputs.<k>.path }} inject an input. Paths resolve "
                    "under the working directory and this conversation's sub-agent history, so "
                    "the `Record:` directory of an earlier spawn is readable: hand its "
                    "`out.md` to the next task with {{ ref:<record dir>/out.md }} rather than "
                    "restating it. The _path forms need a sub-agent the roster tags "
                    "[local-files]; for a [no-local-files] one use the contents forms."
                ),
            },
            "inputs": {
                "type": "object",
                "description": (
                    'Per-key literal string or {"file": <path>}. {{ inputs.<k> }} injects the '
                    "text, {{ inputs.<k>.path }} the file path."
                ),
            },
```

and `required` becomes
`["task_summary", "prompt_template", "subagent"] if names else ["task_summary", "prompt_template"]`.

- [ ] **Step 5: Gate and render in `execute`**

```python
    async def execute(
        self,
        task_summary: str,
        prompt_template: str | None = None,
        subagent: str | None = None,
        instance: str | None = None,
        inputs: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> str:
        subagent = subagent or kwargs.pop("agent", None)
        template = prompt_template or kwargs.pop("task", None)
        if not template:
            return "Error: `prompt_template` is required -- it is the task the sub-agent runs."
        if (refusal := self._reject_useless_instance(subagent, instance)) is not None:
            return refusal
        org = self._cur()
        try:
            task = await self._render(template, inputs or {}, subagent, org.session_key)
        except DagValidationError as exc:
            return f"Error: {exc} Call spawn again with the corrected prompt_template."
```

with a `_render` helper that gates first, then renders. Its module-level imports
are `check_path_placeholders` from `prompt_capabilities`, `parse_placeholders`
from `prompt_placeholders`, `render_template` from `prompt_render`,
`LocalFileBackend` from `prompt_backend`, `DagValidationError` from
`prompt_errors`, and `session_history_root` / `dag_root` from `history`;
`GENERIC_AGENT` is already imported inside `execute` today and moves to the
module level since both call sites now need it.

The gate takes **already-parsed placeholders**, not a raw template. That is not
what this plan originally prescribed: Task 2's review found that a gate which
parsed internally caught its own grammar errors and re-labelled them as
capability refusals, and the fix moved parsing out to the caller so a grammar
error surfaces in its own words. Parsing here is therefore a separate step, and
whatever it raises propagates to the `except DagValidationError` in `execute`
above. The defaults are right for this surface: `subject` stays `"this task"`
because a spawn has no node id, and `escape_hatch` stays empty because `execute`
already wraps the message into its own corrective string.

```python
    async def _render(self, template: str, inputs: dict[str, Any], subagent: str | None, session_key: str) -> str:
        meta = next((a for a in self._agents() if a.name == subagent), None)
        check_path_placeholders(
            parse_placeholders(template),
            subagent or GENERIC_AGENT,
            reads_local_files=True if meta is None else meta.reads_local_files,
        )
        sdir = self._manager.session_dir_for(session_key)
        history = str(session_history_root(sdir))
        cwd = str(workdir.current() or self._manager.workspace)
        return await render_template(
            template,
            inputs,
            backend=LocalFileBackend(),
            cwd=cwd,
            runs_root=str(dag_root(sdir)),
            roots=(cwd, history),
            history_root=history,
        )
```

Keep the rest of `execute` unchanged: `_pending.pop`, the mint, the
`manager.spawn(task=task, ...)` call, the refusal check.

- [ ] **Step 6: Update the description**

The paragraph pointing at `run_subagent_dag` stays. Extend the `Record:`
sentence so the record is named as a handoff channel, not only an audit trail:

```python
            base += (
                " The result names a `Record:` directory holding this call's prompt and output. "
                "Its `out.md` is what to hand a follow-up task -- reference it with "
                "`{{ ref:<that directory>/out.md }}` instead of restating the result from memory. "
                "The directory may also hold `memory.json` -- what the sub-agent concluded for "
                "itself, rather than the answer it gave you -- written after the call, so absence "
                "is normal."
            )
```

- [ ] **Step 7: Run the tests**

```bash
uv run pytest tests/test_subagent_manager.py tests/test_subagent_third_party.py -q
```

`test_subagent_third_party.py` asserts on `SpawnTool.parameters` in several
places (lines ~943, ~957) and on the description text (~872-914). Update the
ones that name `task`; the description assertions about `run_subagent_dag`
should still pass.

- [ ] **Step 8: Full suite**

```bash
uv run pytest -q -p no:randomly 2>&1 | tail -3
```

- [ ] **Step 9: Prepare the commit**

```
feat(agent): give spawn the file-based prompt assembly the dag already had

spawn's `task` becomes `prompt_template` and gains `inputs`, so an upstream
artifact reaches a follow-up task through a file reference instead of being
restated from the host's memory. Path forms are refused before dispatch for a
sub-agent the roster tags [no-local-files], which is the gate the dag surface
already had and this one did not. The old `task` spelling is accepted.

Co-authored-by: Claude (<your session model id>) <noreply@anthropic.com>
```

---

## Task 8: Keep the rendered prompt out of the announcement

**Files:**
- Modify: `raven/agent/subagent/manager.py` (`spawn`, `_announce_result`)
- Modify: `raven/agent/subagent/spawn_tool.py` (pass the template)
- Test: `tests/test_subagent_manager.py`

**Interfaces:**
- Consumes: T7's rendered `task`.
- Produces: `manager.spawn(..., task_display: str | None = None)`; `origin`
  carries `task_display`; `_announce_result` prefers it over `task`.

- [ ] **Step 1: Write the failing test**

```python
async def test_the_announcement_carries_the_template_not_the_inlined_file(tmp_path: Path) -> None:
    (tmp_path / "big.md").write_text("X" * 5000, encoding="utf-8")
    manager, announced = _manager_capturing_announcements(workspace=tmp_path)
    tool = SpawnTool(manager=manager)

    with workdir.bind(tmp_path):
        await tool.execute(
            task_summary="s",
            prompt_template="read {{ ref:big.md }}",
            subagent="raven",
        )
    await _drain(manager)

    assert "read {{ ref:big.md }}" in announced[-1]
    assert "X" * 5000 not in announced[-1]
```

- [ ] **Step 2: Run to confirm failure**

```bash
uv run pytest tests/test_subagent_manager.py -q -k "announcement_carries_the_template"
```

Expected: fails - the 5000-character body is in the announcement.

- [ ] **Step 3: Implement**

`manager.spawn` takes `task_display: str | None = None` and puts it in the
`origin` dict beside `instance` and `workspace`. In `_announce_result`:

```python
        # The template, not the rendered prompt: a rendered `ref` can inline a
        # whole file, and this line is concatenated verbatim with no truncation,
        # so the file would be re-injected into the host's context in full.
        shown = origin.get("task_display") or task
```

and use `shown` in the `Task:` line. `spawn_tool.execute` passes
`task_display=template`. `prompt.md` keeps the rendered text - `SpawnRecord.open`
is already given `task`, which is the rendered value after T7.

- [ ] **Step 4: Verify**

```bash
uv run pytest tests/test_subagent_manager.py -q
uv run pytest -q -p no:randomly 2>&1 | tail -3
```

Confirm the proactive caller still works - it passes no `task_display` and takes
the fallback:

```bash
uv run pytest tests/ -q -k "proactive or sentinel" 2>&1 | tail -3
```

- [ ] **Step 5: Prepare the commit**

```
fix(agent): announce a spawn's template rather than its rendered prompt

The Task: line is concatenated verbatim with no truncation, so a rendered
file reference would put the whole file back into the host's context. The
record on disk keeps the rendered text, which is what the sub-agent saw.

Co-authored-by: Claude (<your session model id>) <noreply@anthropic.com>
```

---

## Task 9: TUI preview key

**Files:**
- Modify: `ui-tui/src/lib/toolArgs.ts:23`
- Test: `ui-tui/src/__tests__/toolArgs.test.ts` (exists; add a case to it)

**Interfaces:**
- Consumes: nothing.
- Produces: `argPreview` returns the `prompt_template` text for a spawn call.

- [ ] **Step 1: Write the failing test**

Add to the existing `describe` block in `ui-tui/src/__tests__/toolArgs.test.ts`,
matching that file's existing import style:

```typescript
  it('previews a delegation call by its prompt template', () => {
    expect(
      argPreview({
        task_summary: 'checking the plan',
        prompt_template: 'follow this: {{ ref:plan.md }}',
        subagent: 'raven'
      })
    ).toBe('follow this: {{ ref:plan.md }}')
  })
```

- [ ] **Step 2: Run to confirm failure**

```bash
npm test --prefix ui-tui -- --run --poolOptions.threads.singleThread toolArgs
```

Expected: receives `'checking the plan'` (the insertion-order fallback).

- [ ] **Step 3: Implement**

Add `'prompt_template'` to `PREVIEW_KEYS` immediately after `'prompt'`, so a
DAG node's row gets the same benefit.

- [ ] **Step 4: Verify and rebuild**

```bash
npm test --prefix ui-tui -- --run --poolOptions.threads.singleThread
npm run build --prefix ui-tui
```

The build is required or `raven tui` keeps running the stale bundle.

- [ ] **Step 5: Prepare the commit**

```
fix(ui-tui): preview a delegation call by its prompt template

PREVIEW_KEYS matched `task`, which spawn no longer has, so the row fell back
to insertion order. Adding prompt_template also fixes DAG node rows, which
never matched a preview key.

Co-authored-by: Claude (<your session model id>) <noreply@anthropic.com>
```

---

## Task 10: Record the domain terms

**Files:**
- Modify: `CONTEXT.md`

**Interfaces:**
- Consumes: everything above.
- Produces: nothing code-facing.

- [ ] **Step 1: Add the prompt-template layer entry**

CONTEXT.md today mentions "path placeholders" once, in passing, at line ~1005.
Add an entry defining, verifiable against the code:

- **Prompt template** - the `{{ ... }}` grammar both delegation surfaces accept
  (`raven/agent/subagent/prompt_placeholders.py`); the six shapes and which
  surface resolves which; that a body matching no shape is ordinary text.
- **Reference roots** - the working directory and
  `<session_dir>/subagents/`, and why agent home is not among them.
- **The fence rule** - contents read from under the sub-agent history root
  arrive `wrap_untrusted`-wrapped; paths do not need wrapping because the path
  is raven-minted.

Extend the existing sentence at ~1005 to point at the new entry rather than
describing the gate a second time.

- [ ] **Step 2: Verify the claims against the code**

For each sentence written, name the module it is verifiable against and confirm
it. AGENTS.md 6 requires the definition be verifiable rather than guessed.

```bash
grep -rn "wrap_untrusted" raven/agent/subagent/prompt_render.py
grep -n "def _parse_body" -A5 raven/agent/subagent/prompt_placeholders.py
```

- [ ] **Step 3: Prepare the commit**

```
docs(agent): define the shared prompt-template layer in CONTEXT.md

Co-authored-by: Claude (<your session model id>) <noreply@anthropic.com>
```

---

## Task 11: Refuse an undefined `inputs` key

Added mid-execution, at the user's request, after Task 5 step 5 surfaced the
behaviour. Not in the original spec: `{{ inputs.<k> }}` naming a key that the
task's `inputs` does not define renders the literal string `"None"` into the
sub-agent's prompt, because the contents branch ends in `str(inputs.get(k))`
and `str(None)` is `"None"`. That is the same silent-failure class this plan
exists to remove, one layer away from the ones it fixes: a typo'd key produces
a plausible-looking prompt instead of a correctable error.

The `input_path` branch already refuses it (a missing key is not a dict, so it
raises), so only the contents branch is wrong. Both surfaces inherit the fix.

**Files:**
- Modify: `raven/agent/subagent/prompt_render.py` (the `input` branch of
  `resolve_file_placeholder`)
- Test: `tests/test_subagent_prompt_template.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: no signature change. `resolve_file_placeholder` raises
  `DagValidationError` for an `input` placeholder whose key is undefined.

- [ ] **Step 1: Rewrite the test that pins the current behaviour**

`tests/test_subagent_prompt_template.py::test_a_missing_input_key_substitutes_none_today`
asserts `rendered == "None"`. It was named `_today` because this task was
foreseen. Replace it with:

```python
async def test_a_missing_input_key_is_refused(tmp_path: Path) -> None:
    """A key the task never defined is a typo, not a value.

    Substituting `str(None)` put the literal text "None" in front of the
    sub-agent, which reads as an answer rather than as the mistake it is.
    """
    with pytest.raises(DagValidationError, match="input 'pln' is not defined"):
        await render_template(
            "{{ inputs.pln }}",
            {},
            backend=LocalFileBackend(),
            cwd=str(tmp_path),
            runs_root=None,
            roots=(str(tmp_path),),
            history_root=None,
        )


async def test_a_defined_input_key_still_renders_its_literal(tmp_path: Path) -> None:
    rendered = await render_template(
        "{{ inputs.k }}",
        {"k": "verbatim"},
        backend=LocalFileBackend(),
        cwd=str(tmp_path),
        runs_root=None,
        roots=(str(tmp_path),),
        history_root=None,
    )

    assert rendered == "verbatim"


async def test_an_explicitly_null_input_is_refused_too(tmp_path: Path) -> None:
    """`{"k": None}` carries no usable value either, and `str(None)` would put
    the same misleading text in the prompt."""
    with pytest.raises(DagValidationError, match="input 'k' is not defined"):
        await render_template(
            "{{ inputs.k }}",
            {"k": None},
            backend=LocalFileBackend(),
            cwd=str(tmp_path),
            runs_root=None,
            roots=(str(tmp_path),),
            history_root=None,
        )
```

- [ ] **Step 2: Run them to confirm they fail**

```bash
uv run pytest tests/test_subagent_prompt_template.py -q -k "missing_input_key or explicitly_null"
```

Expected: both fail — nothing raises today, `"None"` is returned instead.

- [ ] **Step 3: Refuse the undefined key**

In `prompt_render.py`'s `input` branch, replace the trailing `return str(spec)`:

```python
        if ph.name not in inputs or inputs[ph.name] is None:
            raise DagValidationError(
                f"input '{ph.name}' is not defined -- add it to this task's inputs, "
                f"or drop {ph.raw} from the prompt"
            )
        return str(spec)
```

The absence test is `ph.name not in inputs` rather than a falsy check on
`spec`: an input legitimately set to an empty string is a value, and
`inputs.get` cannot tell it apart from a key that was never there.

- [ ] **Step 4: Confirm they pass, and that nothing else moved**

```bash
uv run pytest tests/test_subagent_prompt_template.py -q
uv run pytest tests/test_subagent_dag_core.py tests/test_subagent_dag_runner.py -q
```

A DAG test that fails here is a graph relying on the old substitution; report
it rather than editing it.

- [ ] **Step 5: Full suite and lint**

```bash
uv run pytest -q -p no:randomly
make lint-python
```

Expected: 11398 + 2 (three tests replace one). If pytest exits 139 after a
clean summary with 0 failures, that is a known teardown artifact on this shared
box, not a failure.

- [ ] **Step 6: Commit**

```
fix(agent): refuse an inputs key the task never defined

`{{ inputs.k }}` for an undefined key rendered the literal string "None" into
the sub-agent's prompt, which reads as a value rather than as the typo it is.
The path form already refused it; the contents form now does too, on both
delegation surfaces.

Co-authored-by: Claude (<your session model id>) <noreply@anthropic.com>
```

---

## Final verification

- [ ] **Full Python suite**

```bash
uv run pytest -q -p no:randomly 2>&1 | tail -5
```

Compare against `/tmp/baseline.txt`: the only permitted differences are the new
`test_subagent_prompt_template.py` tests, the changed assertions named in T5
and T6, and the new spawn tests.

- [ ] **Lint**

```bash
make lint-python
```

Pre-commit hooks are disabled in this repo, so this must be run by hand or CI
goes red on formatting alone.

- [ ] **TUI suite, serially**

```bash
npm test --prefix ui-tui -- --run --poolOptions.threads.singleThread 2>&1 | tail -5
```

- [ ] **Size gate**

```bash
make check-large-files
```

- [ ] **Import graph, as the measurable outcome of D5**

```bash
uv run python - <<'PY'
import ast, pathlib, collections
roots = {"subagent": "raven/agent/subagent", "tools": "raven/agent/tools", "loop": "raven/agent/loop"}
def pkg(p):
    s = str(p)
    return next((n for n, r in roots.items() if s.startswith(r + "/")), None)
edges = collections.defaultdict(set)
for p in pathlib.Path("raven").rglob("*.py"):
    src = pkg(p)
    if not src:
        continue
    for n in ast.walk(ast.parse(p.read_text())):
        mods = [n.module] if isinstance(n, ast.ImportFrom) and n.module else \
               [a.name for a in n.names] if isinstance(n, ast.Import) else []
        for m in mods:
            for name, r in roots.items():
                d = r.replace("/", ".")
                if m and (m == d or m.startswith(d + ".")) and name != src:
                    edges[(src, name)].add(str(p))
for k in sorted(edges):
    print(k, len(edges[k]))
PY
```

Expected: no `('tools', 'subagent')` edge. `('subagent', 'loop')` and
`('loop', 'subagent')` remain - the pre-existing cycle, out of scope.

- [ ] **Pre-submit sweep**

Run the `mr-review-patterns` skill's pre-submit sweep over
`origin/main...HEAD` before opening the MR, and fold any finding back into that
catalog afterwards.
