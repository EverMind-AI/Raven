# DAG reused-node display

Date: 2026-08-01
Scope: `ui-webui/frontend` only. No kernel, manifest, or event-contract change.

## Problem

When a DAG run fails partway, the agent retries by submitting a *second*, smaller
DAG that reuses the first run's surviving outputs instead of recomputing them.
The reuse is expressed as a cross-run file reference in the retry graph:

```json
"inputs": {
  "evermind":  {"file": ".ravenx_dag/20260731T083916Z-f4867c2b/evermind_research.out.md"},
  "benchmark": {"file": ".ravenx_dag/20260731T083916Z-f4867c2b/benchmark_research.out.md"},
  "geo":       {"file": ".ravenx_dag/20260731T083916Z-f4867c2b/geo_plan.out.md"}
}
```

The second run's graph therefore contains only the nodes it re-executes, and the
web UI draws exactly those. The three reused upstream artefacts are invisible:
the retry DAG looks like it invented `synthesize_retry`'s inputs out of nothing,
and a reader cannot tell which work was carried over from the failed run.

Observed in run `20260731T084615Z-5ab41d2f` (3 nodes) reusing
`20260731T083916Z-f4867c2b` (6 nodes: 3 completed, 1 failed, 2 skipped).

## Goal

Draw a reused upstream node in the consuming run's graph as a distinct,
non-executed node with status `reused`, wired into the node that consumes it.

## Where the information lives

Reuse is fully derivable client-side, from data the frontend already holds:

| Fact | Source | Note |
|---|---|---|
| The reference path | `pair.call.input.nodes[].inputs[key].file`, and `{{ ref: p }}` / `{{ ref_path: p }}` in `prompt_template` | The two file-reference channels the render grammar supports (`_placeholders.py`). Both are in the tool-call arguments, which live in the transcript and survive a reload. |
| Origin run id + node id | Parsed out of the path `.ravenx_dag/<run_id>/<node_id>.out.md` | `run_id` and `node_id` both have fixed charsets, so the path is unambiguous. |
| The origin node's subagent, status, timings | `DagRunsContext.dagRuns[originRunId]` | Already restored for every run this session touched (`restoreDagRuns.ts`), so no extra request. |
| The origin node's prompt + output | `getDagNode(originRunId, nodeId)` | Already run-id-parameterised; works for any run dir on disk. |

Neither the manifest nor the `dag_*` events carry `inputs`, so a backend-derived
alternative would need a kernel change plus an event-contract change, and would
still not repair runs that have already finished. Deriving in the frontend is
both smaller and retroactive.

## Design

### Data model

`DagNodeStatus` gains a sixth member, `reused`. It is client-derived only: the
runner never emits it, and `toStatus` deliberately does not accept it off the
wire, so no manifest or event can push a node into it.

`DagVizNode` gains two fields:

- `reusedFrom?: { runId: string; nodeId: string } | null` — set on a reused
  ("ghost") node, naming where its output came from.
- `reuseDeps?: string[]` — on a *consuming* node, the synthetic ids of the ghost
  nodes it reads. Kept separate from `depends_on` so the detail pane's
  "depends on" line still lists only real in-run dependencies.

A ghost node's id is `reused:<runId>:<nodeId>`. The prefix is required: the
origin run may contain a node whose id collides with one in the current run.

### Derivation

New pure module `deriveReusedNodes.ts`:

- `parseReusedOutputRef(path)` — `.ravenx_dag/<runId>/<nodeId>.out.md` to
  `{ runId, nodeId }`, or `null`.
- `collectReuseRefs(callNodes)` — consumer node id to the refs it reads, scanning
  both file inputs and `ref` / `ref_path` placeholders.
- `withReusedNodes(vizNodes, callNodes, currentRunId, dagRuns)` — appends the
  deduplicated ghost nodes and stamps `reuseDeps` on their consumers.

A ref naming the current run is dropped (that is an in-run dependency, not a
reuse). Two consumers reading the same origin node produce one ghost with two
edges. Only *directly referenced* nodes become ghosts — the origin run's own
ancestors are not expanded, which would balloon the graph without adding
information about what this run reused.

`withReusedNodes` is applied once, after `DagBody` has built `vizNodes`, so all
four of its branches (manifest, live overlay, planned-from-args, errored) get
ghosts on the same code path.

### Layout

`layoutDag` computes depth and edges over `[...depends_on, ...reuseDeps]`. A
ghost node has no dependencies of its own, so it lands in the leftmost column
ahead of its consumer. Reuse edges are dashed; dependency edges are unchanged.

### Rendering

- Node box: dashed border, muted foreground, a cycle-arrow icon, and a `title`
  naming the origin run and node. Its subagent line and duration are filled from
  the origin run when `dagRuns` has it, so the box reads the same as a real node
  and never changes size (the fixed-size box is load-bearing — see the
  `DagStatusNode` comment on duration-tick jitter).
- Detail pane: a `reused from` line naming origin run + node, and the prompt /
  output fetch keyed on `reusedFrom.runId` instead of the current run.
- Summary strip: a client-computed `reused` chip, shown only when the count is
  non-zero. `total` and the card's node count keep counting only the run's own
  nodes — the backend summary is reported as-is, not inflated.

### i18n

Every status the UI shows as text is translated, not just the new one: the
detail pane and the summary chips previously rendered raw English status names.
`DAG_STATUS_LABEL_KEY` in `deriveDag.ts` is a `Record<DagNodeStatus, string>` of
translation keys, so a status added later without a label is a compile error
rather than a key that renders raw. Keys added to `en.json` / `zh.json`:
`dag.status.*` (all seven statuses), `dag.summary.total`, `dag.reusedFrom`, and
`dag.reusedFromNode` (the node box's hover title).

Deliberately separate from the existing `subagent-monitor.nodeStatus.*`: that
namespace labels the instance *registry's* vocabulary, which has `idle` and
`cancelled` and no `reused`, in shorthand tuned for a narrow side panel
(`completed` reads "done" there). The overlap is not an invitation to share one
namespace between two different vocabularies.

## Non-goals

- No transitive expansion of the origin run's ancestor chain.
- No change to the backend manifest, the `dag_*` events, or the summary the
  runner computes.
- No cross-session reuse: if the origin run is not in `dagRuns`, the ghost still
  renders from the path alone, just without subagent or timing.

## Verification

- `pnpm -C frontend lint` (0 errors) and `pnpm -C frontend build`.
- Load the session that produced `20260731T084615Z-5ab41d2f` and confirm the
  second DAG shows three dashed `reused` nodes feeding `synthesize_retry` and
  `build_html_retry`, and that clicking one shows the first run's output.
