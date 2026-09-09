# Dispatch task summary - design

Date: 2026-08-24
Status: designed
Base: `feat/subagent_everos_memory_record` at `b7e9dbc8`; `origin/main` at
`3c060830`.

## Goal

Every dispatch a model makes must state, in one line, what it is dispatching.
`spawn` gains a required `task_summary`; `run_subagent_dag` gains a required
graph-level `task_summary` and a required per-node `node_summary`. Both summaries
are declared ahead of the prompt they summarise, so the model commits to an
intent before it writes the body.

`spawn`'s optional `label` is removed from the tool face. It was the same idea
without the obligation: absent on most calls, and every consumer had to guess a
replacement from the task text.

## Non-goals

- Renaming or replacing anything on `PlaybookSpec`. Its `name` is the addressing
  key (`store.py:130` builds `<root>/<name>/playbook.md` from it) and its
  `description` answers "when should this template be picked" -- the question a
  router asks, not the one a summary answers. A playbook gains a `task_summary`
  of its own (section 5) and the two coexist.
- Putting either summary into what a sub-agent reads. See section 7.
- Changing how a summary is authored. Nothing generates one; the model writes it
  or the call is rejected.

## What is already there

`label` (`tools/spawn.py:130`) is optional and display-only. It reaches four
places: the minted instance handle (`spawn.py:227`), the manager's display
fallback and announcement (`manager.py:533`, `:1072`), the spawn record's meta
(`manager.py:912`), and the hub row the RPC builds from that meta
(`rpc/methods/subagent.py:131`).

The DAG has no counterpart. A node row is named by its id, and the TUI
approximates a summary by clipping the first non-furniture line out of the
prompt template (`ui-tui/src/lib/dagStatus.ts:64`). That heuristic is what
`node_summary` replaces.

Two facts shape the design:

- `DagNodeSpec` is also a playbook's node model (`playbook/types.py:43`,
  `NodeSpec = DagNodeSpec`), and `playbook/prompt.py:267` derives its forced-call
  schema from `PlaybookSpec.model_json_schema()`. A required field on the model
  therefore binds playbook files and playbook generation at once, with no second
  declaration.
- `SubagentManager.spawn` has two callers that are not model tool calls:
  `proactive_engine/sentinel/executor/spawn.py:83` and
  `.../action_executor.py:302`. Neither has a model to author a summary.

## Design

### 1. The two model fields

Both are required, declared with no default so pydantic rejects their absence:

| Model | Field | Declaration |
|---|---|---|
| `DagNodeSpec` (`subagent_dag/_graph.py:94`) | `node_summary` | `str = ""`, plus `"node_summary"` added to `_REQUIRED_NON_BLANK` (`_graph.py:34`) |
| `SubAgentDagSpec` (`subagent_dag/_graph.py:120`) | `task_summary` | `str = Field(min_length=1)` |
| `PlaybookSpec` (`playbook/types.py:115`) | `task_summary` | `str = Field(min_length=1)` |

The node field and the graph fields are declared differently on purpose, and the
difference is not cosmetic.

A node field cannot reject blank at parse time. `_graph.py:24-28` states the
constraint for the two fields already in this position: a playbook may ship a
node with a field deliberately left for the model to fill, `load_playbook` can
only report that gap if the file loads, and "a pattern that rejected empty would
make the first impossible". So `node_summary` joins `subagent` and
`prompt_template` in `_REQUIRED_NON_BLANK` and is refused in
`validate_and_order` (`_graph.py:216-223`), whose message already tells the model
to call `load_playbook` again with `fills`. That refusal runs inside
`_execute` ahead of dispatch, so a blank summary still costs zero sub-agent runs
and is fixable in the caller's own turn.

The graph-level fields have no gap-filling path -- `SubAgentDagSpec.task_summary`
is filled by the model through the tool schema or by the executor from
`spec.task_summary`, and a playbook's own is authored in the file -- so
`min_length=1` is the right check there. It also matches the precedent one field
over: `PlaybookSpec.description` is already `min_length=1`, so a playbook that
declares neither summary nor description simply does not load.

For the model's own face, `_NODE_SCHEMA` carries `"minLength": 1` on
`node_summary` beside its place in `required`. `Tool._validate` recurses through
arrays and objects (`agent/tools/base.py:332-343`), so a model that submits a
blank one is told `nodes[0].node_summary must be at least 1 chars` before the
graph is parsed at all -- the earliest of the three gates, and the only one whose
message names the node.

A summary is display text, and the user is its reader. That fixes what it is
for: it is not a note the model leaves itself, not an input to the sub-agent
(section 7), and not a machine key -- it is the line a person sees when they
look at what was dispatched.

**Length is advisory, not validated.** There is no `max_length`. A summary is
held to one line by what each field's schema description asks for, which is the
only text the dispatching model actually reads; a verbose one is clipped where
it is displayed rather than rejected at the boundary. The wording is part of the
contract, so it is fixed here:

- `spawn.task_summary`: "One line telling the user what you are dispatching,
  written before the task. Around 200 characters at most. They read it in
  listings and announcements and the sub-agent never does, so write it for them
  -- no ids, no internal shorthand."
- `run_subagent_dag.task_summary`: "One line telling the user what this whole
  graph is for, written before the nodes. Around 200 characters at most.
  Summarise the goal, not a list of the nodes."
- `node_summary`: "One line telling the user what this node is asked to do,
  written before its prompt. Around 200 characters at most. It is this node's
  row in the run, read by someone watching the graph."

`node_summary` summarises what this node is asked to do. `task_summary`
summarises the dispatch as a whole -- for `spawn`, the one task; for a graph,
what the graph is for, not a concatenation of its nodes.

### 2. Schema order and required lists

The summary precedes the prompt everywhere.

- `spawn` (`tools/spawn.py:124`): properties ordered `task_summary`, `task`,
  `subagent`, `instance`; `required` becomes
  `["task_summary", "task", "subagent"]`, and `["task_summary", "task"]` on the
  empty-roster branch that cannot require `subagent`.
- `_NODE_SCHEMA` (`subagent_dag/tool.py:150`): properties ordered `id`,
  `subagent`, `node_summary`, `prompt_template`, `depends_on`, `inputs`,
  `instance`; `required` becomes `["id", "subagent", "node_summary",
  "prompt_template"]`.
- The DAG tool's own parameters (`subagent_dag/tool.py:576`): properties ordered
  `task_summary`, `nodes`, `background`, `confirm`; `required` becomes
  `["task_summary", "nodes"]`.

### 3. spawn: `label` splits in two

The tool face loses `label`. The manager keeps the parameter under the new name,
because of the two sentinel callers:

- `SubagentManager.spawn` (`manager.py:488`): parameter renamed `label` ->
  `task_summary`, still `str | None = None`, and the `task[:30]` fallback at
  `manager.py:533` stays. One term end to end (AGENTS.md 6), without forcing a
  summary on a caller that has no model to write one.
- `sentinel/executor/spawn.py:83` and `action_executor.py:302` pass
  `task_summary=` instead of `label=`; what they pass is unchanged
  (`_build_label(decision)`, `option.title or None`). Both already satisfy the
  field as defined: `option.title` is documented as a "short user-facing title"
  (`sentinel/types.py:175`) and is the menu line the user picked, and
  `_build_label` (`spawn.py:130`) produces `sentinel: <reason clipped to 40>`,
  which tells a user that Raven acted on its own and why. Neither is what a
  model would write, and neither has to be -- the obligation to author one falls
  on the tool face, where a model is filling a schema.
- `tools/spawn.py`: `task_summary` is required here, so `mint_handle(label or
  task, fallback=subagent or GENERIC_AGENT)` at `:227` becomes
  `mint_handle(task_summary, fallback=subagent or GENERIC_AGENT)`.
- The spawn record's meta key (`manager.py:912`) becomes `task_summary`.
- `rpc/methods/subagent.py:131` and `:436` read `task_summary`, then the legacy
  `label`, then `_label_from_prompt`. Records already on disk keep their hub row
  instead of degrading to a prompt's first line.

### 4. The DAG entry

`SubAgentDagTool.execute` (`subagent_dag/tool.py:682`) takes `task_summary` as a
named parameter and threads it into the one parse site,
`parse_dag_spec({"nodes": nodes, "confirm": confirm})` at `:714`, which becomes
`{"task_summary": task_summary, "nodes": nodes, "confirm": confirm}`.

Nothing else in the run path needs a change to persist it: `runner.py:239`
writes `graph.json` from `spec.model_dump_json()`, so both new fields land on
disk under their snake_case names, which is what `_reader.py` already reads
node fields by.

### 5. Playbook fall-out

- Dag-mode playbook files must give every node a `node_summary`. The two
  shipped builtins that once carried in-repo examples were deleted from the
  builtin layer on `main` mid-branch (`chore(playbook): drop the packaged
  builtin playbooks`), and the rebase resolved their edits to deletion, so
  what remains in-repo is the test constructions named in the Migration
  section.
- A prompt-mode playbook carries no nodes, so it gains no `nodeSummary`, but
  its composition still needed a fix in three places, not one.
  `compose_tool()` (`prompt.py:256`) derives the node schema from
  `DagNodeSpec` itself, so the field needs its own `Field(description=...)`
  (`_graph.py:101`) or the model sees an undocumented property; the field's
  default keeps it out of that schema's `required` array regardless of the
  description, so `build_compose_prompt`'s own prose (`prompt.py:291`) has to
  name `nodeSummary` too, the same way it already named `subagent` and
  `promptTemplate`; and because neither a description nor prose is enforced,
  `validate_graph_nodes` (`validate.py:80-82`) rejects a composed graph that
  still omits it, inside `_compose`'s repair rounds rather than at dag
  dispatch. It does still need `taskSummary` of its own -- see below.
- `executor.py:474-481` adds `"node_summary": run_node.node_summary` to each
  `tool_nodes` dict.
- `PlaybookSpec` gains its own `task_summary: str = Field(min_length=1)`,
  required, beside the `description` it already has. The two answer different
  questions and both are needed: `description` is read for retrieval and trigger
  matching (`router.py:66` tokenises it, `generator.py:164` uses it as the
  query), so it says when this template should be picked; `task_summary` says
  what a run of it dispatches, and is what reaches the graph.
- The new field is a machine field, not frontmatter. `FRONTMATTER_FIELDS` stays
  `("name", "description")` and `store.py:244` keeps writing exactly those two,
  so `block_dump()` (`types.py:155`) carries `task_summary` into the
  `yaml playbook-spec` block on its own, spelled `taskSummary` by the camel
  alias generator -- the same split `promptTemplate` already has between the
  file and the model-facing schema.
- `executor.py:493` passes `task_summary=spec.task_summary`.
- The field is required on the spec regardless of `mode`, so a prompt-mode
  playbook is bound as tightly as a dag-mode one; with the shipped builtins
  gone from the builtin layer, the requirement now binds user-authored
  playbooks only. Playbook generation needs no change to ask for it --
  `prompt.py:39` derives the forced-call schema from the same model.

### 6. Live and resumed reads

`node_summary` becomes the node row's subject, with today's heuristic kept only
for runs that predate the field.

- `runner.py:279-284` adds `"node_summary": node.node_summary` to each node of
  the `dag_run_started` payload. The event is the authoritative live source: it
  needs no correlation with the tool call, unlike `promptTemplate`, which the
  TUI can only recover from call args.
- Both wire shapes are contract types, not free-form dicts, and both are closed
  (`additionalProperties: false` in the schema, `_Strict` in pydantic), so the
  field has to be declared in three places that a CI drift check holds
  together: `rpc-schema/openrpc.json` is the source of truth,
  `raven/rpc/models.py` carries the pydantic side (`DagRunStartedNode:503` for
  the live event, `DagSnapshotNode:574` for the resumed read), and
  `ui-tui/src/rpc/generated.ts` is regenerated from the schema with
  `cd ui-tui && npm run gen:rpc` -- never hand-edited. `tests/test_rpc_schema_match.py`
  and `npm run lint:rpc` fail on any of the three drifting. The field is
  optional on both wire types: a client may be reading a run that predates it.
- `_reader.py:128` adds `"node_summary": node.get("node_summary")` beside
  `prompt_template`, read from `graph.json`. That is the resume route, and it is
  why an old run degrades rather than breaks -- the reader takes `graph.json` as
  a plain dict and `_resume.py` overlays registry state, so neither path
  re-validates against the model.
- `rpc/methods/subagent.py:290` sets the dag row's `label` to the node summary,
  falling back to the node id.
- TUI: `DagRunNode` (`ui-tui/src/domain/dagRun.ts:29`) gains `nodeSummary?`,
  populated in `fromStart` from the event and in the run-dir path at `:194`.
  `dagNodeSummary` (`ui-tui/src/lib/dagStatus.ts:69`) takes the summary first and
  falls back to its prompt-template heuristic; `dagPanel.tsx:134` passes both.
  `dagNodeToggleKey` (`ui-tui/src/lib/dagOpenNodes.ts:32`) now keys
  expandability off `promptTemplate` or whether the node has started -- a
  summary is the row, its trace or template is what expanding reveals.

### 7. What the sub-agent sees

Neither summary enters a sub-agent's input. `spawn` passes `prompt=task`
(`manager.py:930`) and a node passes its rendered template; both already carry
the whole task. Prepending a one-line summary would change every sub-agent's
input for no gain and risks the summary reading as an instruction.

## Migration

No data migration. A stored playbook file predating `taskSummary` gets the
field backfilled from `description` in `store.py`'s `_migrate_legacy_nodes`
before validation. Runs and spawn records already on disk are read by paths
that do not validate against these models (section 6), and the RPC readers
fall back through the legacy key (section 3). What must be edited in-repo is
the set of node literals and playbook specs: the 15 `NodeSpec(...)`
constructions across
`tests/test_playbook_executor.py` (10), `tests/test_playbook_types.py`,
`tests/test_playbook_tool.py`, `tests/test_playbook_validate.py`,
`tests/test_playbook_store.py` and `tests/test_cli_playbook_commands.py`, and
the 9 `PlaybookSpec(...)` constructions, which also reach
`tests/test_playbook_matcher.py`. `tests/fixtures/playbook/cases.yaml` needs
nothing: it holds live-LLM generation cases, not node literals, and is read only
by `tests/integration/test_playbook_real_llm.py` under an OpenRouter
credential.

## Testing

Existing files only (AGENTS.md 5.1, 5.4). Per file:

- `tests/test_subagent_dag_core.py`: `task_summary` rejected when missing or
  empty; `node_summary` blank parses and is refused by `validate_and_order`,
  which is the existing `test_a_blank_required_field_parses_but_never_runs`
  extended by one parameter; a 400-char summary accepted, since the length is
  advisory; schema property order and `minLength`; the value reaching
  `graph.json`.
- `tests/test_subagent_dag_runner.py`: the `dag_run_started` payload carries
  `node_summary`.
- `tests/test_subagent_manager.py`: the record's meta key, and the `task[:30]`
  fallback still applying when the manager is called without a summary.
- `tests/test_rpc_subagent_calls.py`: three-level fallback on a spawn row; a dag row
  labelled by node summary.
- `tests/test_playbook_executor.py`: `node_summary` forwarded per node and
  `task_summary` taken from `spec.task_summary`.
- `tests/test_playbook_types.py`: a playbook without `task_summary` is rejected
  in either mode; a dag-mode one without node summaries is rejected;
  `task_summary` round-trips through the spec block as `taskSummary` while the
  frontmatter still holds only `name` and `description`.
- `ui-tui/src/__tests__/dagPanel.test.tsx` and `dagStatus.test.ts`: summary
  preferred, heuristic used when it is absent.

## Domain terms

Two new terms, added to `CONTEXT.md` under `### Agent Core` next to the
sub-agent entries (AGENTS.md 6):

- **Task summary** (`task_summary`, on `spawn`, `run_subagent_dag` and
  `PlaybookSpec`): the one line stating what is being dispatched, written before
  the prompt. Not a sub-agent input; it names the dispatch in handles, rows,
  records and announcements, and the user is who reads it. On a playbook it sits
  beside `description`, which
  is a different question -- `description` is matched against to decide whether
  to run the playbook at all, `task_summary` describes what running it does.
- **Node summary** (`node_summary`, on `DagNodeSpec`): the same obligation for
  one node of a graph. It is the node row's subject, replacing the first-line
  heuristic taken from the prompt template.

`label` leaves the vocabulary on the spawn path.

## Risks

- Nothing stops a verbose summary, so a row can be handed more text than it can
  show. Accepted, and the reason the cap is advisory: a rejected dispatch costs
  a turn, an over-long one costs nothing because every display site already
  clips (`dagNodeSummary` through `clipToWidth`, `_label_from_prompt` at 80
  chars). If summaries drift long in practice, the fix is the field's wording,
  not a validator.
- Required fields on the playbook models mean a third-party playbook file
  written against today's schema stops validating -- `taskSummary` on every
  file, `nodeSummary` on every node of a dag-mode one. The loud-failure risk
  this bullet accepted is retired by the migration at `store.py`: a stored
  file predating `taskSummary` loads with its summary taken from
  `description`, and a dag-mode node without `nodeSummary` loads and reports
  the blank as a fillable gap. Nothing stops loading silently any more; the
  only residue is a row that shows less than today's writer would have
  produced.
- Two spellings of one idea exist for as long as old spawn records do
  (`task_summary` in new meta, `label` in old). Bounded to the two RPC read
  sites, and no writer emits the old key after this change.
