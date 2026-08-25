# Dispatch task summary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every model-authored dispatch state, in one user-facing line, what it is dispatching -- `task_summary` on `spawn` and `run_subagent_dag`, `node_summary` on each DAG node -- and delete `spawn`'s optional `label` from the tool face.

**Architecture:** The obligation lives on the pydantic models (`DagNodeSpec`, `SubAgentDagSpec`, `PlaybookSpec`), so one declaration binds the DAG tool, playbook files and playbook generation at once. A node's field is blank-tolerant at parse and refused by `validate_and_order`, matching the existing rule for `subagent`/`prompt_template`; graph-level fields use `min_length=1`. Summaries are display text for the user: they reach handles, rows, records and announcements, never a sub-agent's prompt.

**Tech Stack:** Python 3.12+, pydantic v2, uv, pytest; TypeScript, React/Ink, vitest; OpenRPC codegen.

**Spec:** `docs/specs/2026-08-24-dispatch-task-summary-design.md`

## Global Constraints

- Run tests with `uv run pytest ...` only, never bare `pytest` (AGENTS.md 4, 5.4).
- Update the existing test files named per task. Do not create new test files (AGENTS.md 5.1, 5.4).
- Comments in English, and only where the logic is non-obvious or a constraint is hidden (AGENTS.md 1). Match the density of the lines you are editing.
- **Do not commit unless the user has explicitly asked for a commit** (AGENTS.md 3.4). Each task ends with a commit step; when you reach it, report the task as done and ask. Never amend a prior commit.
- `ui-tui/src/rpc/generated.ts` is auto-generated. Never hand-edit it; regenerate with `cd ui-tui && npm run gen:rpc`.
- The wire keys `label` on `SubagentCall` and `SubagentDeliveredEvent` are required contract fields and must NOT be renamed. Only what fills them changes.
- No `max_length` anywhere. Length is advisory, carried in each field's schema description.
- Neither summary enters a sub-agent's prompt.
- Field description wording is fixed by the spec (section 1). Copy it verbatim.

---

### Task 1: `node_summary` on the node model, and every node that builds one

Adding a field to `DagNodeSpec` binds playbook files too (`playbook/types.py:43` aliases `NodeSpec` to it), so every construction site moves in this one task or the suite goes red.

**Files:**
- Modify: `raven/agent/subagent_dag/_graph.py:34` (`_REQUIRED_NON_BLANK`), `:94-101` (fields), and the class docstring's `Attributes:` block
- Modify: `raven/agent/subagent_dag/tool.py:150-211` (`_NODE_SCHEMA`)
- Modify: `raven/playbook/executor.py:474-481` (forward the field at dispatch)
- Modify: `raven/playbook/builtin/topic-briefing/playbook.md` (3 nodes)
- Modify: `tests/test_playbook_validate.py:12`, `tests/test_playbook_types.py:16`, `tests/test_playbook_tool.py:43`, `tests/test_playbook_store.py:35`, `tests/test_cli_playbook_commands.py:132`, `tests/test_playbook_executor.py` (10 sites: lines 50, 56, 106, 157, 201, 202, 427, 532, 559, 578)
- Test: `tests/test_subagent_dag_core.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `DagNodeSpec.node_summary: str` (default `""`, camel alias `nodeSummary`); `_REQUIRED_NON_BLANK` now `("subagent", "prompt_template", "node_summary")`; `_NODE_SCHEMA["required"]` now `["id", "subagent", "node_summary", "prompt_template"]`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_subagent_dag_core.py`, extend the existing blank-field parametrize (it currently reads `["subagent", "prompt_template"]`) and add two new tests:

```python
@pytest.mark.parametrize("field", ["subagent", "prompt_template", "node_summary"])
def test_a_blank_required_field_parses_but_never_runs(field: str) -> None:
```

```python
def test_node_summary_survives_the_parse_and_reaches_the_spec() -> None:
    spec = parse_dag_spec(
        {
            "task_summary": "compare the two vendors",
            "nodes": [
                {
                    "id": "a",
                    "subagent": "x",
                    "node_summary": "read the pricing pages",
                    "prompt_template": "hello",
                }
            ],
        }
    )
    assert spec.nodes[0].node_summary == "read the pricing pages"


def test_a_long_node_summary_is_accepted_because_length_is_advisory() -> None:
    # No `max_length`: the ceiling lives in the field's schema description, and a
    # verbose summary is clipped where it is displayed rather than rejected here.
    spec = parse_dag_spec(
        {
            "task_summary": "s",
            "nodes": [{"id": "a", "subagent": "x", "node_summary": "w" * 400, "prompt_template": "hi"}],
        }
    )
    assert len(spec.nodes[0].node_summary) == 400
```

Note both new tests pass `task_summary`, which Task 2 introduces. Until Task 2 lands, `SubAgentDagSpec` has `extra="forbid"` and will reject the key -- so write these two tests now, watch them fail, and expect them to keep failing on that key until Task 2. Only the parametrize extension must pass at the end of this task.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_dag_core.py -k "blank_required_field or node_summary" -v`
Expected: FAIL. The parametrize case fails with no error raised (a blank `node_summary` is not yet refused); the two new tests fail on `extra="forbid"` rejecting `task_summary`.

- [ ] **Step 3: Add the field and the refusal**

In `raven/agent/subagent_dag/_graph.py`, add to the `DagNodeSpec` field block after `subagent`:

```python
    node_summary: str = ""
```

Extend the tuple at `:34`, keeping its existing comment:

```python
_REQUIRED_NON_BLANK = ("subagent", "prompt_template", "node_summary")
```

Add to the class docstring's `Attributes:` block, in the field order:

```
        node_summary (`str`):
            One line, for the user, on what this node is asked to do. Blank
            survives the parse and is refused by ``validate_and_order`` for the
            same reason the two fields above are: a playbook may leave it for
            the model to fill.
```

- [ ] **Step 4: Put the field in the model's own schema**

In `raven/agent/subagent_dag/tool.py`, insert into `_NODE_SCHEMA["properties"]` between `subagent` and `prompt_template`:

```python
        "node_summary": {
            "type": "string",
            "minLength": 1,
            "description": (
                "One line telling the user what this node is asked to do, written before its "
                "prompt. Around 200 characters at most. It is this node's row in the run, read "
                "by someone watching the graph."
            ),
        },
```

and change `required`:

```python
    "required": ["id", "subagent", "node_summary", "prompt_template"],
```

- [ ] **Step 5: Forward it at playbook dispatch**

In `raven/playbook/executor.py`, add one key to the `tool_nodes` dict:

```python
            tool_nodes.append(
                {
                    "id": run_node.id,
                    "subagent": run_node.subagent,
                    "node_summary": run_node.node_summary,
                    "prompt_template": _with_skills(run_node),
                    "depends_on": list(run_node.depends_on),
                    **({"instance": run_node.instance} if run_node.instance else {}),
                }
            )
```

- [ ] **Step 6: Give the builtin's three nodes a summary**

In `raven/playbook/builtin/topic-briefing/playbook.md`, add a `nodeSummary` line to each node, after `subagent`:

```yaml
nodes:
  - id: scan_recent
    subagent: raven
    nodeSummary: Sweep what changed recently on the topic, with citations
    promptTemplate: >-
```
```yaml
  - id: scan_players
    subagent: raven
    nodeSummary: Map who matters on the topic and where they disagree
    promptTemplate: >-
```
```yaml
  - id: brief
    subagent: raven
    nodeSummary: Merge both research files into one cited briefing
    dependsOn: [scan_recent, scan_players]
    promptTemplate: >-
```

- [ ] **Step 7: Fill the 15 test constructions**

Each `NodeSpec(...)` needs a `node_summary=`. Use a summary that describes that node, not a placeholder. The single-node factories:

```python
# tests/test_playbook_types.py:16
nodes=[NodeSpec(id="scan", subagent="research-raven", node_summary="research the target", prompt_template="research ${params.target}")],
```
```python
# tests/test_playbook_store.py:35 -- same shape as above
nodes=[NodeSpec(id="scan", subagent="research-raven", node_summary="research the target", prompt_template="research ${params.target}")],
```
```python
# tests/test_cli_playbook_commands.py:132
nodes=[NodeSpec(id="scan", subagent="research-raven", node_summary="scan the target", prompt_template="scan ${params.target}")],
```
```python
# tests/test_playbook_tool.py:43
nodes=[NodeSpec(id="pull", subagent="data-raven", node_summary="pull the week's feedback", prompt_template="pull ${params.week_of}")],
```

`tests/test_playbook_validate.py:12` is a helper -- give it a default so its callers are untouched:

```python
def _node(nid, subagent="research-raven", template="do it", **over):
    return NodeSpec(id=nid, subagent=subagent, node_summary=f"step {nid}", prompt_template=template, **over)
```

Check the helper's real signature before editing and keep it; only the `NodeSpec(...)` call changes. In `tests/test_playbook_executor.py`, add `node_summary=` to all 10 sites, e.g.:

```python
            NodeSpec(
                id="pull",
                subagent="data-raven",
                node_summary="pull the feedback for the week",
                prompt_template="pull the feedback for ${params.week_of}",
                skills=["sql-queries"],
            ),
```

- [ ] **Step 8: Run the affected suites**

Run: `uv run pytest tests/test_subagent_dag_core.py tests/test_playbook_executor.py tests/test_playbook_types.py tests/test_playbook_tool.py tests/test_playbook_validate.py tests/test_playbook_store.py tests/test_cli_playbook_commands.py -q`
Expected: everything passes except the two new `task_summary` tests from Step 1, which fail on `extra="forbid"` until Task 2. Confirm that is the only failure and that the message names `task_summary`.

- [ ] **Step 9: Commit**

Per Global Constraints, report and ask first. Files: `raven/agent/subagent_dag/_graph.py`, `raven/agent/subagent_dag/tool.py`, `raven/playbook/executor.py`, `raven/playbook/builtin/topic-briefing/playbook.md`, the six test files.
Message: `feat(agent): make a dag node say what it is asked to do`

---

### Task 2: `task_summary` on the graph, the tool entry, and the playbook spec

`SubAgentDagSpec.task_summary` and `PlaybookSpec.task_summary` land together: the executor cannot satisfy the first without the second.

**Files:**
- Modify: `raven/agent/subagent_dag/_graph.py:104-121` (`SubAgentDagSpec`)
- Modify: `raven/agent/subagent_dag/tool.py:576-600` (`parameters`), `:682` and `:691-697` (`execute` / `_execute`), `:714` (parse call)
- Modify: `raven/playbook/types.py:106-125` (`PlaybookSpec`)
- Modify: `raven/playbook/executor.py:490` (pass it at dispatch)
- Modify: `raven/playbook/builtin/topic-briefing/playbook.md`, `raven/playbook/builtin/deep-dive/playbook.md`
- Modify: `tests/test_playbook_matcher.py:162`, `tests/test_cli_playbook_commands.py:127`, `tests/test_playbook_types.py:11`, `tests/test_playbook_tool.py:46`, `tests/test_playbook_validate.py:16` and `:134`, `tests/test_playbook_store.py:30`, `tests/test_playbook_executor.py:40` and `:250`
- Test: `tests/test_subagent_dag_core.py`, `tests/test_playbook_types.py`

**Interfaces:**
- Consumes: `DagNodeSpec.node_summary` (Task 1).
- Produces: `SubAgentDagSpec.task_summary: str`; `PlaybookSpec.task_summary: str` (camel alias `taskSummary`, machine-block field); `SubAgentDagTool.execute(nodes, task_summary, background=True, confirm=False)`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_subagent_dag_core.py`:

```python
def test_a_graph_without_a_task_summary_is_rejected() -> None:
    with pytest.raises(DagValidationError):
        parse_dag_spec({"nodes": [{"id": "a", "subagent": "x", "node_summary": "s", "prompt_template": "hi"}]})


def test_a_blank_task_summary_is_rejected_at_parse() -> None:
    # Unlike a node's summary, the graph-level one has no gap-filling path: it is
    # written by the model through the schema or by the playbook executor, so
    # blank can be refused at the boundary.
    with pytest.raises(DagValidationError):
        parse_dag_spec(
            {
                "task_summary": "",
                "nodes": [{"id": "a", "subagent": "x", "node_summary": "s", "prompt_template": "hi"}],
            }
        )
```

In `tests/test_playbook_types.py`:

```python
def test_a_playbook_needs_a_task_summary_in_either_mode() -> None:
    with pytest.raises(ValidationError):
        _dag(task_summary="")


def test_task_summary_is_a_machine_field_not_frontmatter() -> None:
    spec = _dag()
    assert spec.block_dump()["taskSummary"] == spec.task_summary
    assert PlaybookSpec.FRONTMATTER_FIELDS == ("name", "description")
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_subagent_dag_core.py tests/test_playbook_types.py -k "task_summary" -v`
Expected: FAIL. The parse tests raise nothing; the playbook tests fail on an unexpected keyword `task_summary`.

- [ ] **Step 3: Declare the two graph-level fields**

In `raven/agent/subagent_dag/_graph.py`, add to `SubAgentDagSpec` above `nodes`:

```python
    task_summary: str = Field(min_length=1)
    nodes: list[DagNodeSpec]
```

and add to that class's docstring a sentence saying what it is: one line, for the user, on what the whole graph is for.

In `raven/playbook/types.py`, add to `PlaybookSpec` after `description`:

```python
    task_summary: str = Field(min_length=1)
    """One line, for the user, on what running this playbook dispatches. Distinct
    from ``description``, which is matched against to decide whether to run it at
    all."""
```

- [ ] **Step 4: Take it through the tool entry**

In `raven/agent/subagent_dag/tool.py`, insert into `parameters()["properties"]` **before** `nodes`:

```python
                "task_summary": {
                    "type": "string",
                    "minLength": 1,
                    "description": (
                        "One line telling the user what this whole graph is for, written before "
                        "the nodes. Around 200 characters at most. Summarise the goal, not a list "
                        "of the nodes."
                    ),
                },
```

and change `required` to `["task_summary", "nodes"]`. Then thread it through both entries:

```python
    async def execute(
        self, nodes: list[dict], task_summary: str = "", background: bool = True, confirm: bool = False, **kwargs: Any
    ) -> str:
```
```python
        return await self._execute(nodes, background, confirm=confirm, task_summary=task_summary)
```
```python
    async def _execute(
        self,
        nodes: list[dict],
        background: bool,
        confirm: bool = False,
        task_summary: str = "",
    ) -> str:
```
```python
            spec = parse_dag_spec({"task_summary": task_summary, "nodes": nodes, "confirm": confirm})
```

`task_summary` keeps a `""` default on the python signature even though the schema requires it: the schema is what the model is held to, and a default means a caller that omits it gets the `DagValidationError` from `parse_dag_spec` rather than a `TypeError`.

- [ ] **Step 5: Pass it from the playbook executor**

In `raven/playbook/executor.py`:

```python
        receipt = await self._dag_tool.execute(
            tool_nodes,
            task_summary=spec.task_summary,
            background=self._background,
```

Keep the existing `confirm=` argument and its comment untouched.

- [ ] **Step 6: Give both builtins a `taskSummary`**

In each `yaml playbook-spec` block, after `mode:`:

```yaml
# raven/playbook/builtin/topic-briefing/playbook.md
taskSummary: Research a topic from two angles in parallel and merge them into one cited briefing
```
```yaml
# raven/playbook/builtin/deep-dive/playbook.md
taskSummary: Investigate a subject in depth and report what was found
```

Read `deep-dive`'s existing `prompts:` value first and make the summary match what it actually dispatches.

- [ ] **Step 7: Fill the 9 spec constructions**

Each factory takes one new line in its `base` dict, e.g.:

```python
# tests/test_playbook_types.py:11
    base = dict(
        name="competitor-scan",
        description="research one competitor on the market and technology fronts in parallel",
        task_summary="scan one competitor on the market and technology fronts",
        mode="dag",
```

Do the same for `tests/test_playbook_executor.py:40` (`_dag_spec`) and `:250` (`_prompt_spec`), `tests/test_playbook_tool.py:46`, `tests/test_playbook_validate.py:16` and `:134`, `tests/test_playbook_store.py:30`, `tests/test_playbook_matcher.py:162`, `tests/test_cli_playbook_commands.py:127`. Write a summary that fits each fixture's subject.

- [ ] **Step 8: Run the suites, including Task 1's deferred tests**

Run: `uv run pytest tests/test_subagent_dag_core.py tests/test_playbook_executor.py tests/test_playbook_types.py tests/test_playbook_tool.py tests/test_playbook_validate.py tests/test_playbook_store.py tests/test_playbook_matcher.py tests/test_cli_playbook_commands.py tests/test_rpc_dag.py tests/test_agent_loop_playbook_entry.py tests/test_rpc_instances.py -q`
Expected: all pass, including the two tests Task 1 left failing.

- [ ] **Step 9: Commit** (report and ask first)

Message: `feat(agent): require a dispatching graph to say what it is for`

---

### Task 3: spawn's tool face, the manager, and the two sentinel callers

**Files:**
- Modify: `raven/agent/tools/spawn.py:124-163` (`parameters`), `:199-241` (`execute`)
- Modify: `raven/agent/subagent/manager.py:485-495` (signature), `:533` (fallback), `:562-567`, `:833-857`, `:890-912`, `:976-996`, `:1042-1083`
- Modify: `raven/proactive_engine/sentinel/executor/spawn.py:80-89`, `raven/proactive_engine/sentinel/executor/action_executor.py:302-308`
- Test: `tests/test_subagent_manager.py`, `tests/test_proactive_spawn.py`, `tests/test_action_executor_tool_spawn.py`

**Interfaces:**
- Consumes: nothing from Tasks 1-2.
- Produces: `SpawnTool.execute(task_summary, task, subagent=None, instance=None)`; `SubagentManager.spawn(task, task_summary=None, ...)`; spawn record meta key `task_summary`.

- [ ] **Step 1: Write the failing tests**

In `tests/test_subagent_manager.py`, following the file's existing manager fixture and record-reading helper:

```python
async def test_the_record_meta_carries_the_task_summary(...) -> None:
    await manager.spawn(task="do the thing", task_summary="do the thing, briefly")
    meta = _read_meta(...)
    assert meta["task_summary"] == "do the thing, briefly"
    assert "label" not in meta


async def test_a_caller_without_a_summary_still_gets_one_derived(...) -> None:
    # The sentinel paths have no model to author one, so the manager keeps its
    # own fallback rather than making the field required here.
    await manager.spawn(task="x" * 50)
    meta = _read_meta(...)
    assert meta["task_summary"] == "x" * 30 + "..."
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_subagent_manager.py -k task_summary -v`
Expected: FAIL with an unexpected keyword argument `task_summary`.

- [ ] **Step 3: Replace `label` on the tool face**

In `raven/agent/tools/spawn.py`, in `parameters()`, delete the `label` entry and build `props` with `task_summary` first:

```python
        props: dict[str, Any] = {
            "task_summary": {
                "type": "string",
                "minLength": 1,
                "description": (
                    "One line telling the user what you are dispatching, written before the task. "
                    "Around 200 characters at most. They read it in listings and announcements and "
                    "the sub-agent never does, so write it for them -- no ids, no internal shorthand."
                ),
            },
            "task": {
                "type": "string",
                "description": "The task for the subagent to complete",
            },
        }
```

and both `required` branches:

```python
            "required": ["task_summary", "task", "subagent"] if names else ["task_summary", "task"],
```

- [ ] **Step 4: Rename it through `execute`**

```python
    async def execute(
        self,
        task_summary: str,
        task: str,
        subagent: str | None = None,
        instance: str | None = None,
        **kwargs: Any,
    ) -> str:
```

Inside, the mint and the manager call:

```python
            instance = mint_handle(task_summary, fallback=subagent or GENERIC_AGENT)
```
```python
        result = await self._manager.spawn(
            task=task,
            task_summary=task_summary,
```

Leave the `agent`-from-`kwargs` compatibility block and its docstring alone.

- [ ] **Step 5: Rename the manager's parameter and its internal chain**

In `raven/agent/subagent/manager.py`, `spawn`'s signature keeps the parameter optional:

```python
    async def spawn(
        self,
        task: str,
        task_summary: str | None = None,
```

The fallback keeps its shape under the new name:

```python
        display_summary = task_summary or task[:30] + ("..." if len(task) > 30 else "")
```

Rename the local through its uses (`:562`, `:566`, `:567`) and the `label: str` parameter of `_run_subagent`, `_run_subagent_inner` and `_announce_result` to `task_summary`, so one term runs end to end (AGENTS.md 6). Change the record meta key only:

```python
                "task_summary": task_summary,
```

**Do not** touch the `_emit_delivered` payload key at `:1083` or `:1110`. `label` there is a required field of the `SubagentDeliveredEvent` wire contract; only what fills it may change.

- [ ] **Step 6: Update the two sentinel callers**

```python
# raven/proactive_engine/sentinel/executor/spawn.py
            task_id = await self.subagent_manager.spawn(
                task=decision.spawn_task,
                task_summary=label,
```
```python
# raven/proactive_engine/sentinel/executor/action_executor.py
            ack = await self.subagent_manager.spawn(
                task=task_description,
                task_summary=option.title or None,
```

Leave `_build_label` and its output unchanged: `option.title` is documented as a "short user-facing title" (`sentinel/types.py:175`) and `sentinel: <reason>` tells a user Raven acted on its own and why. Both already are what this field is for.

- [ ] **Step 7: Run the suites**

Run: `uv run pytest tests/test_subagent_manager.py tests/test_proactive_spawn.py tests/test_action_executor_tool_spawn.py tests/test_agent_loop_disabled_tools.py -q`
Expected: all pass.

- [ ] **Step 8: Confirm no `label=` caller survives**

Run: `grep -rn "spawn(.*label=" --include=*.py raven/ tests/`
Expected: no output.

- [ ] **Step 9: Commit** (report and ask first)

Message: `feat(agent): require spawn to summarise the task it dispatches`

---

### Task 4: the RPC read sites

**Files:**
- Modify: `raven/rpc/methods/subagent.py:131`, `:151-161` (`_label_from_prompt` docstring), `:290` (dag row), `:436`
- Test: `tests/test_rpc_subagent_calls.py`

**Interfaces:**
- Consumes: the `task_summary` meta key (Task 3); `node_summary` in `graph.json` (Task 1).
- Produces: nothing new. The wire field stays `SubagentCall.label`.

- [ ] **Step 1: Write the failing tests**

Following the file's existing directory/meta fixture:

```python
def test_a_row_prefers_the_task_summary(tmp_path) -> None:
    row = _row(meta={"task_summary": "compare the two vendors"})
    assert row["label"] == "compare the two vendors"


def test_a_row_still_reads_a_legacy_label(tmp_path) -> None:
    # Records written before the rename keep their row instead of degrading to
    # the prompt's first line.
    row = _row(meta={"label": "older spawn"})
    assert row["label"] == "older spawn"


def test_a_row_with_neither_falls_back_to_the_prompt(tmp_path) -> None:
    row = _row(meta={}, prompt="first line of the prompt\nsecond")
    assert row["label"] == "first line of the prompt"
```

And for the dag rows, following the file's existing run-directory fixture:

```python
def test_a_dag_row_is_named_by_its_node_summary(tmp_path) -> None:
    # `graph.json` is this row's only source for what the node was asked, and it
    # has carried `node_summary` since the field landed on the node model.
    rows = _dag_rows(graph={"nodes": [{"id": "scan", "subagent": "x", "node_summary": "read the pricing pages"}]})
    assert rows[0]["label"] == "read the pricing pages"


def test_a_dag_row_from_an_older_run_still_shows_its_node_id(tmp_path) -> None:
    rows = _dag_rows(graph={"nodes": [{"id": "scan", "subagent": "x"}]})
    assert rows[0]["label"] == "scan"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `uv run pytest tests/test_rpc_subagent_calls.py -k "task_summary or legacy_label or falls_back" -v`
Expected: FAIL -- the first returns the prompt's first line because `task_summary` is not read.

- [ ] **Step 3: Read three levels at both sites**

```python
# raven/rpc/methods/subagent.py:131
    label = str(meta.get("task_summary") or meta.get("label") or "") or _label_from_prompt(directory)
```
```python
# raven/rpc/methods/subagent.py:436
        "label": str(meta.get("task_summary") or meta.get("label") or "") or _label_from_prompt(directory),
```

Update `_label_from_prompt`'s docstring first line to say it is read only when the meta carries no summary, keeping its second paragraph about the per-poll file cost.

- [ ] **Step 3b: Name the dag rows by their node summary**

The dag branch builds `node` from `graph.json` directly (`:247` reads the file, `:252` takes `nodes`), so the field is in hand with no wire change. At `:290`:

```python
                    "label": str(node.get("node_summary") or "") or nid,
```

Keep the comment above `"id"` untouched -- it explains the `run/node` pair, which is unchanged.

- [ ] **Step 4: Run the suite**

Run: `uv run pytest tests/test_rpc_subagent_calls.py -q`
Expected: all pass.

- [ ] **Step 5: Commit** (report and ask first)

Message: `fix(rpc): show a spawn's own summary on its row`

---

### Task 5: the wire contract and the two server-side emitters

`node_summary` has to be declared in the OpenRPC schema before it can travel: both node shapes are closed, and a CI drift check holds schema, pydantic and generated TS together.

**Files:**
- Modify: `rpc-schema/openrpc.json` (`DagRunStartedEvent.payload.nodes.items`, `DagSnapshotNode`)
- Modify: `raven/rpc/models.py:503-507` (`DagRunStartedNode`), `:574-585` (`DagSnapshotNode`)
- Regenerate: `ui-tui/src/rpc/generated.ts`
- Modify: `raven/agent/subagent_dag/runner.py:279-284`, `raven/agent/subagent_dag/_reader.py:128`
- Test: `tests/test_subagent_dag_runner.py`, `tests/test_rpc_schema_match.py`

**Interfaces:**
- Consumes: `DagNodeSpec.node_summary` (Task 1).
- Produces: optional `node_summary: string` on both wire node shapes; `dag_run_started` payload nodes carry it; `read_run`'s per-node dict carries it.

- [ ] **Step 1: Write the failing test**

In `tests/test_subagent_dag_runner.py`, following the file's existing publisher-capture fixture:

```python
async def test_the_started_event_names_each_node(...) -> None:
    # The event is the authoritative live source for a row's subject: unlike the
    # prompt template, it needs no correlation with the tool call.
    events = await _run_and_capture(
        {
            "task_summary": "compare the two vendors",
            "nodes": [{"id": "a", "subagent": "x", "node_summary": "read the pricing pages", "prompt_template": "hi"}],
        }
    )
    started = next(e for e in events if e[0] == "dag_run_started")
    assert started[1]["nodes"][0]["node_summary"] == "read the pricing pages"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `uv run pytest tests/test_subagent_dag_runner.py -k names_each_node -v`
Expected: FAIL with `KeyError: 'node_summary'`.

- [ ] **Step 3: Declare it in the schema**

In `rpc-schema/openrpc.json`, add to `components.schemas.DagRunStartedEvent.properties.payload.properties.nodes.items.properties`:

```json
              "node_summary": {
                "description": "One line, for the user, on what this node was asked to do. Absent on a run that predates the field.",
                "type": "string"
              }
```

Leave that object's `required` list as `["id", "subagent", "depends_on"]`. Add the same property to `components.schemas.DagSnapshotNode.properties`, beside `prompt_template`.

- [ ] **Step 4: Mirror it in the pydantic models**

```python
# raven/rpc/models.py:503
class DagRunStartedNode(_Strict):
    id: str
    subagent: str
    depends_on: list[str]
    instance: str | None = None
    node_summary: str | None = None
```
```python
# raven/rpc/models.py:574, beside prompt_template
    node_summary: str | None = None
```

`prompt_template` next door carries no `Field(description=...)`, so a bare annotation is the consistent choice here.

- [ ] **Step 5: Regenerate the TypeScript**

Run: `cd ui-tui && npm run gen:rpc`
Expected: `ui-tui/src/rpc/generated.ts` gains `node_summary?: string` on both interfaces. Never hand-edit this file.

- [ ] **Step 6: Emit it from the runner and the reader**

```python
# raven/agent/subagent_dag/runner.py:279-284
                    {
                        "id": node.id,
                        "subagent": node.subagent,
                        "node_summary": node.node_summary,
                        "depends_on": node.depends_on,
                        "instance": node.instance,
                    }
```
```python
# raven/agent/subagent_dag/_reader.py:128, beside prompt_template
                "node_summary": node.get("node_summary"),
```

The reader takes `graph.json` as a plain dict, which is why an older run degrades to `None` here rather than failing.

- [ ] **Step 7: Run the suites and the drift checks**

Run: `uv run pytest tests/test_subagent_dag_runner.py tests/test_rpc_schema_match.py tests/test_rpc_dag.py -q`
Expected: all pass.

Run: `cd ui-tui && npm run lint:rpc`
Expected: no drift reported.

- [ ] **Step 8: Commit** (report and ask first)

Message: `feat(rpc): carry a dag node's summary on the wire`

---

### Task 6: the TUI row

**Files:**
- Modify: `ui-tui/src/domain/dagRun.ts:22-40` (`DagRunNode`), `:61-71` (`fromStart`), `:188-200` (`foldDagSnapshot`)
- Modify: `ui-tui/src/lib/dagStatus.ts:55-72` (`dagNodeSummary`)
- Modify: `ui-tui/src/components/dagPanel.tsx:159`
- Test: `ui-tui/src/__tests__/dagStatus.test.ts`, `ui-tui/src/__tests__/dagPanel.test.tsx`

**Interfaces:**
- Consumes: `node_summary` on both wire shapes (Task 5).
- Produces: `DagRunNode.nodeSummary?: string`; `dagNodeSummary(nodeSummary, promptTemplate, room)`.

- [ ] **Step 1: Write the failing tests**

In `ui-tui/src/__tests__/dagStatus.test.ts`:

```ts
it('prefers the summary the node was dispatched with', () => {
  expect(dagNodeSummary('read the pricing pages', '# Heading\nsomething else', 80)).toBe('read the pricing pages')
})

it('falls back to the template heuristic for a run that carried no summary', () => {
  expect(dagNodeSummary(undefined, 'read the pricing pages\nmore detail', 80)).toBe('read the pricing pages')
})

it('clips a long summary to the room it is given', () => {
  expect(dagNodeSummary('w'.repeat(400), undefined, 20)).toHaveLength(20)
})
```

Check `clipToWidth`'s exact boundary behaviour before asserting the length, and match the assertions the file already makes on it.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd ui-tui && npx vitest run src/__tests__/dagStatus.test.ts`
Expected: FAIL -- `dagNodeSummary` takes `(promptTemplate, room)`, so the first argument is read as the template.

- [ ] **Step 3: Take the summary first in the helper**

In `ui-tui/src/lib/dagStatus.ts`:

```ts
export const dagNodeSummary = (
  nodeSummary: string | undefined,
  promptTemplate: string | undefined,
  room: number
): string => {
  const given = (nodeSummary ?? '').trim()
  if (given) {
    return clipToWidth(given, room)
  }

  const first = (promptTemplate ?? '')
    .split('\n')
    .map(line => line.trim().replace(LINE_FURNITURE_RE, '').replace(PLACEHOLDER_RE, '…').trim())
    .find(line => line && line !== '…' && !SECTION_LABEL_RE.test(line))

  return first ? clipToWidth(first, room) : ''
}
```

Update the doc comment above it: the heuristic is now the fallback for a run whose graph carried no summary, not the primary source.

- [ ] **Step 4: Carry the field through the state**

Add to `DagRunNode` beside `promptTemplate`:

```ts
  /** One line, for the user, on what this node was asked to do. Absent on a run
   * that predates the field. */
  nodeSummary?: string
```

In `fromStart`:

```ts
    ...(node.node_summary ? { nodeSummary: node.node_summary } : {}),
```

In `foldDagSnapshot`'s node mapper, mirror how `template` is recovered so a snapshot never blanks a row that was reading fine:

```ts
    const summary = file.node_summary ?? prev?.nodes.find(node => node.id === file.node)?.nodeSummary
```

and spread `...(summary ? { nodeSummary: summary } : {})` beside the existing template spread.

- [ ] **Step 5: Pass both at the call site**

```ts
// ui-tui/src/components/dagPanel.tsx:159
  const summary = dagNodeSummary(node.nodeSummary, node.promptTemplate, room)
```

Leave `dagNodeToggleKey(runId, node)` alone: expandability still keys off `promptTemplate`, because a summary is the row and the template is what expanding reveals.

- [ ] **Step 6: Run the TUI tests**

Run: `cd ui-tui && npx vitest run src/__tests__/dagStatus.test.ts src/__tests__/dagPanel.test.tsx`
Expected: all pass. If other `__tests__` files cover `dagRun.ts`, add them to the run.

- [ ] **Step 7: Typecheck**

Run: `cd ui-tui && npx tsc --noEmit -p tsconfig.json`
Expected: no errors. Any other `dagNodeSummary` caller surfaces here.

- [ ] **Step 8: Commit** (report and ask first)

Message: `feat(tui): name a dag row by the summary its node was given`

---

### Task 7: the domain terms

Two new terms enter the vocabulary, so `CONTEXT.md` gains them in the same change (AGENTS.md 6).

**Files:**
- Modify: `CONTEXT.md` (insert in `### Agent Core`, immediately before the `**Tool** (agent/tools/)` entry at line 143)

**Interfaces:**
- Consumes: everything above.
- Produces: nothing in code.

- [ ] **Step 1: Add the two entries**

```markdown
**Task summary** (`task_summary`, on `spawn`, `run_subagent_dag` and `PlaybookSpec`):
the one line stating what is being dispatched, written before the prompt it summarises.
The user is who reads it — it names the dispatch in instance handles, sub-agent rows,
spawn records and announcements, and never reaches the sub-agent's own input. On a
playbook it sits beside `description`, which answers a different question: `description`
is matched against to decide whether to run the playbook at all, `task_summary` says what
running it dispatches.
_Avoid_: `label` for this on the spawn path — the tool parameter is gone. The wire field
`SubagentCall.label` keeps its name and is filled from the summary.

**Node summary** (`node_summary`, on `DagNodeSpec`): the same obligation for one node of a
graph, and the node row's subject. Blank survives parsing so a playbook can leave it for
the model to fill, and `validate_and_order` refuses it before any node runs. It replaces
the first-line-of-the-template guess a row used to make.
```

- [ ] **Step 2: Verify the anchor still holds**

Run: `grep -n "^\*\*Task summary\*\*\|^\*\*Node summary\*\*\|^\*\*Tool\*\*" CONTEXT.md`
Expected: the two new entries print immediately before the `Tool` entry.

- [ ] **Step 3: Full suite**

Run: `uv run pytest -q`
Expected: no failures. Compare against the baseline recorded when this worktree was set up (441 passed across the 13 affected files) rather than assuming zero pre-existing failures elsewhere.

- [ ] **Step 4: Lint**

Run: `make lint-python`
Expected: clean. This repo's pre-commit hooks are disabled (`core.hooksPath` points at a missing directory), so formatting is not applied at commit time and CI will fail on it if you skip this.

- [ ] **Step 5: Commit** (report and ask first)

Message: `docs(agent): define the dispatch summary terms`

---

## Self-Review

**Spec coverage.** Section 1 -> Tasks 1-2. Section 2 -> Tasks 1-3. Section 3 -> Task 3. Section 4 -> Task 2. Section 5 -> Tasks 1-2. Section 6 -> Tasks 4 (the dag row's label), 5 and 6. Section 7 -> no task, and correctly so: it states that nothing is added to a sub-agent's prompt, which is verified by the absence of any edit to prompt assembly. Migration -> Tasks 1-2. Testing -> per task. Domain terms -> Task 7. Risks -> no task.

**Type consistency.** `node_summary` (python, wire, schema) / `nodeSummary` (playbook file, TS state) is the same split `prompt_template` / `promptTemplate` already has, and is deliberate. `dagNodeSummary` takes `(nodeSummary, promptTemplate, room)` in Task 6 Step 3 and is called that way in Step 5. `SubagentManager.spawn`'s parameter is `task_summary` in Task 3 Steps 5-6. The record meta key is `task_summary` in Task 3 Step 5 and read as such in Task 4 Step 3.

**Known cross-task dependency.** Task 1 Step 1 writes two tests that cannot pass until Task 2 Step 3, because `SubAgentDagSpec` forbids extra keys. This is called out in both places; do not "fix" it inside Task 1 by dropping the key.
