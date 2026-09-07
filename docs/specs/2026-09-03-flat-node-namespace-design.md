# Flat node namespace for spawn and DAG - design

Date: 2026-09-03
Status: implemented
Base: `aa273e58` on `origin/main`. Line anchors below were taken on that commit.

## Goal

Give `spawn` and `run_subagent_dag` one way to name a sub-agent task's input and
output, so a task dispatched by either surface can reference a task finished by
either surface, by the id the model chose for it.

Today only the DAG surface can do that, and only for DAG nodes.

## What is true today, measured

Two delegation surfaces write two different layouts under
`<session_dir>/subagents/`:

```
mas_dag/<run_id>/<node_id>.out.md        node artifacts, prefixed, inside a run dir
mas_dag/<run_id>/{graph,manifest}.json   run-scoped state
mas_dag/index.json                       the run-keyed registry
spawn/<call_id>/out.md                   call artifacts, fixed names, one dir per call
instances/<agent>/<handle>.jsonl         untouched by this design
direct/<agent>/<handle>/                 untouched by this design
```

`<call_id>` is `make_call_id` (`history.py:78`): a microsecond UTC stamp plus a
task id or eight hex. `<node_id>` is chosen by the model and constrained by
`_ID_PATTERN = r"^[A-Za-z0-9_-]+$"` (`dag_graph.py:19`), re-checked
independently in `dag_reader.py` before being joined into a path.

Counted on one developer machine: 169 `<node>.out.md`, 197 `<node>.prompt.md`,
68 `index.json`, 83 `graph.json` under `mas_dag/`; 85 spawn records, of which
**57 have an `out.md`**.

The reference forms available to a model, measured against a fixture where run
`r1` completed node `n1`:

| Form | DAG | spawn |
|---|---|---|
| `{{ n1.output }}` | resolves | refused |
| `{{ n1.output_path }}` | resolves | refused |
| `inputs {"node": "n1"}` | resolves | refused |
| `{{ ref:@runs/r1/n1.out.md }}` | resolves | resolves |
| `inputs {"file": "@runs/r1/n1.out.md"}` | resolves | resolves |

spawn refuses the id forms at `spawn_tool.py:337` via `needs_a_graph`, because
resolving an id needs `mas_dag/index.json`, which only the DAG surface reads.

Two further measurements shape the design:

- **The index gates readability, a path does not.** For a node recorded
  `failed`, `{{ dead.output }}` is refused with `failed in run 'r1' and wrote no
  output`, while `{{ ref:@runs/r1/dead.out.md }}` reads the leftover file
  without complaint.
- **Removing `index.json` breaks only the id forms.** The path forms keep
  working, because `_output_path` (`dag_render.py:249`) consults the index only
  to turn an id into a run id.

## The layout

```
<session_dir>/subagents/
├── nodes/
│   ├── <node_id>.prompt.md
│   ├── <node_id>.out.md
│   ├── <node_id>.error.md
│   ├── <node_id>.memory.json
│   ├── <node_id>.transcript.jsonl
│   └── <node_id>.attempt-<n>.{prompt.md,out.md,transcript.jsonl}
├── nodes.json
├── mas_dag/<run_id>/{graph,manifest}.json
├── instances/<agent>/<handle>.jsonl
└── direct/<agent>/<handle>/
```

Both surfaces write every node artifact into `nodes/`. `mas_dag/<run_id>/`
keeps only what is genuinely run-scoped. `spawn/` is removed.

A flat namespace is legal because a node id is already unique per conversation,
and it promotes that invariant from one the registry polices to one the
filesystem enforces: two nodes with one id now collide on a real path instead of
resolving to whichever ran last.

The `attempt-<n>` variants (`dag_store.py:295-375`) stay unambiguous flat,
because the node id already is.

### What this collapses

`_output_path` is a two-branch lookup today: this run's `output_paths` first,
else `SessionNodes.owner[node_id]` to get a run id, then
`output_path_in(root, run_id, node_id)`. Flat, both branches name the same file,
so **path resolution stops consulting the registry entirely**.

`output_path_in` (`dag_store.py:70`) and `memory_path_in` (`dag_store.py:94`)
lose their `run_id` parameter. The eight `DagRunStore` path methods rebase off
`nodes/` instead of `run_dir`.

## The registry

`mas_dag/index.json` is run-keyed today: one entry per run carrying `run_id`,
`nodes`, `status`, `summary`. A flat node namespace wants it node-keyed. It
moves to `<session_dir>/subagents/nodes.json`:

```json
{
  "nodes": {
    "market_scan": {
      "kind": "spawn",
      "status": "completed",
      "has_output": true,
      "started_at_ms": 1756900000000,
      "ended_at_ms": 1756900042000
    },
    "pricing_research": {
      "kind": "dag",
      "run_id": "20260903T134502123456Z-a1b2c3d4",
      "status": "completed",
      "has_output": true,
      "started_at_ms": 1756900100000,
      "ended_at_ms": 1756900190000
    },
    "deck_draft": {
      "kind": "dag",
      "run_id": "20260903T134502123456Z-a1b2c3d4",
      "status": "failed",
      "has_output": false,
      "started_at_ms": 1756900100000,
      "ended_at_ms": 1756900150000
    }
  },
  "runs": [
    {
      "run_id": "20260903T134502123456Z-a1b2c3d4",
      "summary": "1/2 completed",
      "nodes": ["pricing_research", "deck_draft"]
    }
  ]
}
```

`nodes` answers both questions `SessionNodes` splits today: `owner` becomes
`kind` plus an optional `run_id`, `state` becomes `status`. `runs` keeps the
per-run summary the DAG panel draws. `SessionNodes` gains a third map beside `owner` and `state`:

```python
owner: dict[str, str]        # node id -> the run that claimed it, or "spawn"
state: dict[str, str]        # node id -> completed / failed / skipped / cancelled / running / unrecorded
has_output: dict[str, bool]  # node id -> whether an out.md was written
```

`read_session_nodes` fills all three from the new file.

### `has_output`

`status == "completed"` cannot mean "there is a file to read": 57 of 85 spawn
records have an `out.md`, because `SpawnRecord.finish` writes one only when
`persisted_output(...)` is not None (`history.py:319`). Overriding a reported
`completed` to `failed` would misreport the run, so the writer records what it
knows instead, and

```python
def is_readable(self, node_id: str) -> bool:
    return self.state.get(node_id) == "completed" and self.has_output.get(node_id, False)
```

`is_readable` stays a pure function on registry contents; no I/O enters a
validator that has none today. The same field covers a DAG node that completed
with empty output.

### Ordering

`started_at_ms` in the registry is what makes a model-chosen directory name
affordable. The spawn listing sorts by directory name today
(`rpc/methods/subagent.py:332`), and `history_stamp` goes to microseconds and
bumps on ties specifically to keep that sort stable - its docstring records that
a second-accurate stamp made the newest-first listing test fail about one run in
six. A `node_id` carries no time order, so the listing reads the field instead.

`test_ids_minted_in_the_same_instant_still_sort_in_mint_order` and
`test_a_run_id_orders_against_a_call_id` change meaning rather than breaking:
ordering moves from the name to the field.

## Write protocol

One registry, one lock. `index_guard(root)` (`dag_store.py:31`) keys a per-loop
`asyncio.Lock` by root string; the root becomes `session_history_root(sdir)`, so
both writers contend on the same lock. It remains in-process only - two
gateways sharing one session still race, exactly as documented today.

**DAG claim** keeps its shape and changes its root (`dag_runner.py:266`):

```python
async with index_guard(history_root):
    session_nodes = await read_session_nodes(backend, history_root)
    validate_and_order(spec, roots, session_nodes)
    store = DagRunStore(...)
    await store.init(...)
```

Read, validate and claim stay inside one guard. Splitting them is what lets two
runs both pass the uniqueness check and both claim an id.

**spawn claim** is new and takes the same three steps, at the point
`SpawnRecord.open` writes the prompt today (`history.py:276`):

```python
async with index_guard(history_root):
    session_nodes = await read_session_nodes(backend, history_root)
    if node_id in session_nodes.owner:
        advice = (
            f"Rename it, or drop this node and reference '{node_id}' directly (no depends_on needed)"
            if session_nodes.is_readable(node_id)
            else "Rename it -- that run left it with no output, so there is nothing to reference either"
        )
        raise DagValidationError(
            f"node id '{node_id}' is already used by run "
            f"'{session_nodes.owner[node_id]}'; ids are unique per conversation. {advice}"
        )
    await claim_node(backend, history_root, node_id, kind="spawn", started_at_ms=...)
```

The refusal reuses DAG's existing message, including the branch on `is_readable`
that chooses between "rename it, or drop this node and reference it directly"
and "rename it - that run left it with no output". A second spelling of one rule
is the finding this repo's reviewers filed twice on merge request 450.

spawn returns before its sub-agent finishes, so a claimed id sits at `running`
until `finish` - a state the registry already models and `is_readable` already
refuses, with advice that says to wait rather than rename.

**Finalize.** `_record_outcome` (`dag_runner.py:820`) writes per-node `status`
and `has_output` into `nodes`, and the run summary into `runs`.
`SpawnRecord.finish` writes the same fields for its single node.

## Reference forms

Identical on both surfaces:

| Form | Resolves to |
|---|---|
| `{{ <node_id>.output }}` | contents of `nodes/<node_id>.out.md` |
| `{{ <node_id>.output_path }}` | that path |
| `inputs {"node": "<node_id>"}` with `{{ inputs.<k> }}` | same file, contents |
| the same with `{{ inputs.<k>.path }}` | same file, path |
| `{{ ref:@nodes/<node_id>.out.md }}` | same file, by path |
| `{{ ref:@nodes/<node_id>.prompt.md }}` | what that node was asked |
| `{{ ref:<path> }}` / `{{ ref_path:<path> }}` | any file inside the two roots |

`@nodes/` replaces `@runs/` in `prompt_paths.py` (`RUNS_PREFIX` at `:15`),
keeping the same lexical confinement: the prefix is checked against its own root
alone, so `@nodes/../..` is refused exactly as `@runs/../..` is.

Validation is one gate for both surfaces. An id must be readable, or - on the
DAG surface only - listed in this node's `depends_on`. The `_unreadable` message
family (`dag_graph.py:404`, `:486`) carries over unchanged, so failed, skipped,
cancelled, running and unrecorded keep their distinct advice.

## What is retired

- **`@runs/`**. Flat, `{{ <id>.output }}` and a path name the same single file,
  so the path form's original purpose - pinning one run when an id could be
  ambiguous - no longer exists. It is documented in `dag_tool.py`, in
  `SKILL.md`, and in `spawn_tool.py`, and all three change.
- **`spawn/`**. A spawn's artifacts are in `nodes/` and its metadata is in the
  registry, so the tree has nothing left to hold. `make_call_id` itself stays:
  `direct_chat.py:132` still mints direct-chat record dirs with it, and the
  `direct/` tree is out of scope here.
- **`needs_a_graph` and `node_form_refusal`** (`prompt_render.py:250`, `:277`).
  They exist only because spawn had no way to resolve a node id. Once spawn
  reads the registry the case they cover shrinks to "an id in this graph that
  has not run yet", which spawn never has and which `_unreadable` reports
  better. To be confirmed while implementing rather than assumed.

Retiring `@runs/` also removes the model's only placeholder route to
`graph.json` and `manifest.json`, which `SKILL.md` advertises today. Those stay
run-scoped and remain reachable by absolute path, which is the same mechanism
old history relies on below.

## Compatibility

**No migration.** `nodes.json` starts empty and every existing
`mas_dag/index.json` is ignored. Consequences, stated rather than discovered:

- **Every node id used in past conversations becomes free again.** A
  conversation that used `plan` last week can reuse it, and `{{ plan.output }}`
  names the new one. This is consistent - the old node was never going to be
  id-addressable after this change - but it is a visible behaviour change.
- Old artifacts stay readable by absolute path under the history root, which
  `roots` includes (`spawn_tool.py:353`); measured resolving before this design.
  With `@runs/` retired they need the full path, not a prefix.
- `rpc/methods/subagent.py:239` still enumerates `mas_dag/*` for the run list,
  because `graph.json` and `manifest.json` stay. The `spawn/*` walk at `:332` is
  replaced by a registry read.

## Files touched

| File | Change |
|---|---|
| `raven/agent/subagent/history.py` | `spawn_root` removed, `nodes_root` added, `SpawnRecord` writes flat and claims its id. `make_call_id` **stays** - `direct_chat.py:132` mints its record dirs with it |
| `raven/agent/subagent/dag_store.py` | path helpers lose `run_id`; registry becomes node-keyed and moves; `index_guard` root; `has_output` |
| `raven/agent/subagent/dag_render.py` | `_output_path` collapses to one branch |
| `raven/agent/subagent/dag_graph.py` | `has_output` in the reference checks |
| `raven/agent/subagent/prompt_paths.py` | `@nodes/` prefix; `@runs/` retired |
| `raven/agent/subagent/prompt_render.py` | node resolution moves in; two functions retired |
| `raven/agent/subagent/spawn_tool.py` | `node_id` field, claim, schema descriptions |
| `raven/agent/subagent/dag_runner.py` | claim root; finalize shape |
| `raven/agent/subagent/dag_reader.py` | node artifact paths; `@nodes` reads |
| `raven/agent/subagent/instance_records.py` | spawn directory walk becomes a registry read |
| `raven/rpc/methods/subagent.py` | listing source and per-node file reads |
| `raven/rpc/methods/instances.py` | `spawn_root` call site |
| `raven/memory_engine/skills/subagent-dag-orchestration/SKILL.md` | layout, reference forms, `@nodes/` |
| `rpc-schema/openrpc.json` | `call_id` is a required wire param (`:8661`, `:8667`); a spawn is addressed by node id after this |

`_call_dir` (`rpc/methods/subagent.py:165`) validates the id off the wire before
joining it into a path. That check survives unchanged - a node id is a stricter
set than a call id - but the parameter it guards is renamed.

## Testing

Failing test first for each.

1. A node's output lands at `nodes/<id>.out.md`, from both surfaces.
2. A spawn and a DAG run claiming one id concurrently: one claims it, the other
   gets the uniqueness refusal.
3. A spawn that returns no output is not readable; `{{ x.output }}` is refused
   rather than naming a missing file.
4. Cross-surface both ways: a spawn's output read by a later graph, a DAG node's
   output read by a later spawn.
5. `@nodes/../..` is refused lexically.
6. An existing `mas_dag/index.json` on disk does not make its ids taken.
7. The listing orders by `started_at_ms`, not by directory name.

## Corrections made while implementing

Three things this document got wrong, recorded here rather than quietly fixed,
because each was found by measuring rather than by reading.

- **A spawn keeps a per-node `meta.json`.** The layout above omits it. Two
  readers need it: `instance_records.spawn_exchanges` matches a record to an
  instance on `meta["agent"]` / `meta["handle"]`, and the listing reads the
  activity counters `finish` merges in. Folding those into the registry would
  put panel-shaped data into a validator's input.
- **The claim cannot live in `SpawnRecord.open`.** That method is synchronous
  where `index_guard` is async, its contract is to swallow I/O errors where a
  claim must refuse, and it runs inside the background task the tool has
  already returned from -- so a refusal raised there reaches the model a turn
  late. The claim is in `SpawnTool.execute`; the manager keeps an idempotent
  backstop for callers that never go through the tool.
- **`call_id` is a result and event field, not a request param.** The anchors
  cited in "Files touched" name `DirectTurn` and `subagent.status`;
  `subagent.context` takes `id`. It was not renamed: 822 occurrences across
  four surfaces, and none of the 30 client-side sorts keys on it.

## Risks

- **The uniqueness window is still per process.** `index_guard` does not take a
  file lock, so two gateways on one session can still claim one id. This design
  adds a second writer inside that window without widening it; closing it is a
  separate change.
- **13 files, three of them read paths shared with the `direct` tree.**
  `instance_records.py:92` and `:115` are near-identical loops over the spawn
  and direct trees, and `rpc/methods/subagent.py:102` reads
  `("out.md", "error.md")` for both. spawn's artifacts move and `direct`'s do
  not, so those readers must branch by tree rather than by filename.
- **Id reuse after the cutover** is the compatibility consequence above, and is
  the one user-visible behaviour change that is not additive.
- **Built in two phases, shipped as one change.** The refactor (flat artifacts,
  node-keyed registry) is separable from the feature (spawn as a second writer,
  the `node_id` field), and the commits keep that order. They are not separable
  as merge requests: the refactor retires `@runs/` while the feature is what
  gives a caller the `@nodes/` address to use instead, so landing the first
  alone would leave the model told to use a spelling nothing yet resolved.
