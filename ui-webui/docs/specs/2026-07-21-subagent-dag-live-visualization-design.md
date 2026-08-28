# Sub-agent DAG live visualization — design

**Date:** 2026-07-21
**Status:** Approved (design)
**Builds on:** the sub-agent DAG orchestration tool (`src/agentscope/subagent/_dag/`), the sub-agent tool factory (`src/agentscope/subagent/_agent_tools.py`), the out-of-band `CustomEvent` → SSE pattern (`src/agentscope/app/middleware/_state_change_middleware.py`, `src/agentscope/app/_bus_ops.py`), and the web-UI tool-renderer / cross-message-context patterns (`examples/web_ui/frontend/`).

## 1. Motivation

When the leader agent calls `run_subagent_dag`, the web UI currently shows the
generic tool-call box: a spinner while it runs, then a scrollable `<pre>` of the
text summary once it returns. The *shape* of the orchestration — which sub-agents
run, how they depend on each other, which finished, which failed, which were
skipped — is invisible except as prose.

This design renders that DAG **as an interactive graph inside the tool-call box**
and **animates it per-node in real time** as the run progresses: nodes light up
as each sub-agent starts, turn green/red/grey as they complete/fail/skip. Clicking
a node reveals its status, sub-agent, output-file path, error, and (for terminal
nodes) its inline output. On history view / page reload the graph rebuilds from
the tool result's final metadata.

## 2. Scope

**In scope:**
- **Backend:** the DAG runner emits live per-node status as out-of-band
  `CustomEvent`s on the session channel, mirroring `StateChangeMiddleware`. This
  requires threading an optional `progress_publisher` through
  `SubAgentDagTool` → `run_dag` → `_run_group` → `_run_node`, and injecting the
  app-global message bus into `make_subagent_tool_factory`.
- **Backend (correctness fix):** the success-path terminal chunk currently sets
  `state=ToolResultState.RUNNING`; change it to `SUCCESS` so the tool-call box
  chrome resolves instead of spinning forever.
- **Frontend:** route the two new `CustomEvent` names, hold a live per-run node
  overlay in `ChatViewport`, share it via a new React context, and add a
  `run_subagent_dag` tool renderer that draws an interactive **React Flow**
  (`@xyflow/react`) graph — animating from the live overlay while running, then
  reconciling to the authoritative final metadata.
- **Frontend:** click-a-node inline detail sourced entirely from existing result
  metadata (no new endpoint, no extra content pushed into the agent's context).
- **Tests:** backend unit tests for the emitted event sequence + the state fix;
  frontend pure-helper tests where a runner exists; an end-to-end browser smoke.

**Out of scope / unchanged:**
- **Reconnect durability of the live animation mid-run.** The live overlay is an
  ephemeral in-memory overlay. A client that connects/reloads mid-run may miss
  earlier per-node transitions, but the final graph is always correct from the
  tool result metadata. A durable `SessionProjection`/Redis projector (the
  `SubagentHitlProjector` pattern) is a documented follow-up, not v1.
- **Right-dock "DAG runs" panel** (user chose inline-in-the-box only).
- **Downloadable node output files** (would need a path-confined workdir file
  read/download route; user chose inline metadata detail only).
- **Keying the overlay by `tool_call_id`** — a tool's `call()` has no access to
  its own `tool_call_id` (no contextvar; `is_state_injected` injects only agent
  state). We key by the server-generated `run_id` + a single-active-run
  invariant instead (§7).
- Any change to the DAG's file-based message-passing semantics, the node input
  schema, the scheduler's ordering, or the SSE transport itself. We ride existing
  rails (`publish_session_event` → SSE `/stream` → `useMessages.processEvent`).

## 3. Architecture

Two cooperating halves (backend emit; frontend consume + render), plus a small
one-line correctness fix.

**Data flow:**

```
run_dag (backend)
  ├─ at start:            publish CustomEvent(dag_run_started, {run_id, nodes:[{id,subagent,depends_on}]})
  ├─ per node (running):  publish CustomEvent(dag_node_updated, {run_id, node, status:"running"})
  ├─ per node (done/err): publish CustomEvent(dag_node_updated, {run_id, node, status:"completed"|"failed"})
  ├─ per cascade:         publish CustomEvent(dag_node_updated, {run_id, node, status:"skipped"})
  └─ tool returns ToolChunk(state=SUCCESS, is_last=True,
                            metadata={run_id, dir, terminal_outputs,
                                      files:[{node,subagent,depends_on,status,prompt_file,output_file,error}], summary})
        │
   publish_session_event(bus, session_id, event)   (src/agentscope/app/_bus_ops.py:39-65)
        │   dual-writes replay log + live pub/sub channel
        ▼
   SSE  GET /sessions/{sid}/stream   (src/agentscope/app/_router/_session.py:695-851)
        ▼
   sessionApi.streamEvents  →  useMessages.processEvent   (examples/web_ui/frontend/src/hooks/useMessages.ts:168-189)
        ├─ CUSTOM dag_run_started / dag_node_updated  → onDag* callbacks → ChatViewport dagRuns overlay (LIVE)
        └─ TOOL_RESULT_END metadata                   → appendEvent copies onto tool_result block (AUTHORITATIVE)
        ▼
   RunSubagentDagRenderer  (keyed "run_subagent_dag" in tool-renderers/index.tsx)
        result?.metadata?.files present?  → draw AUTHORITATIVE graph from metadata.files
        else                              → draw LIVE graph from dagRuns[latestRunId] (+ structure from dag_run_started/call.input)
```

**Verified round-trip facts:**
- `CustomEvent` is generic — `{ type: CUSTOM, name: str, value: dict }`
  (`src/agentscope/event/_event.py:485-516`); the frontend types it generically
  too (`{ type: EventType.CUSTOM; name: string; value: Record<string,unknown> }`),
  and the SSE client `JSON.parse`s every `data:` frame with no name allow-list
  (`examples/web_ui/frontend/src/api/session.ts:91-95`). **A new event name needs
  no SDK/package bump.**
- `publish_session_event` fans out on exactly the session channel the `/stream`
  endpoint consumes, the same path `state_updated`/`team_updated` already use
  (`src/agentscope/app/middleware/_state_change_middleware.py:98-115`).
- The run's final per-node status already lands in `tool_result` metadata
  (`src/agentscope/subagent/_dag/_tool.py:172-178`) and survives history fetch —
  this is the reconciliation source of truth.

## 4. Backend — live per-node event emission

### 4.1 The publisher signature

A single optional callback threaded through the runner:

```python
# async (event_name, value) -> None ; never raises to the caller
ProgressPublisher = Callable[[str, dict], Awaitable[None]]
```

It stays **optional (default `None`)** because `SubAgentDagTool`/`run_dag` are
re-exported from `subagent/__init__.py` and used standalone and in tests without
a bus. Unlike `StateChangeMiddleware` (whose failures are swallowed by
`ChatService.run`), the runner has **no outer catch** around the publish call, so
every publish is wrapped in `try/except Exception → logger.warning(exc_info=True)`
inside the publisher itself — a bus hiccup must never fail a node.

### 4.2 Threading (all keyword-only, non-breaking)

1. **`SubAgentDagTool.__init__`** (`src/agentscope/subagent/_dag/_tool.py:81`):
   add `progress_publisher: ProgressPublisher | None = None`; store it.
   **`SubAgentDagTool.call`** (`_tool.py:140`): pass
   `progress_publisher=self._progress_publisher` into `run_dag(...)`.
2. **`run_dag`** (`src/agentscope/subagent/_dag/_runner.py:42`): add keyword-only
   `progress_publisher=None`. Forward into `_run_group`.
3. **`_run_group`** (`_runner.py:175`): add `progress_publisher` kw; forward to
   `_run_node`.
4. **`_run_node`** (`_runner.py:230`): add `progress_publisher` kw.

### 4.3 Emission points

- **`dag_run_started`** — once, in `run_dag` right after `store` is created and
  `by_id` is built (before the scheduling loop). Value:
  `{ "run_id": store.run_id, "nodes": [ {"id": n.id, "subagent": n.subagent, "depends_on": n.depends_on} for n in spec.nodes ] }`.
  This gives the frontend the full structure + `run_id` immediately, independent
  of parsing partially-streamed `call.input`.
- **`running`** — in `_run_node`, right after `async with semaphore:` and before
  `render_prompt`/dispatch. Value: `{run_id, node: node.id, status: "running"}`
  (`run_id` via `store.run_id`, a public attr).
- **`completed` / `failed`** — in `_run_node` at the existing status assignments
  (`_runner.py:288-302`).
- **`skipped`** — **not** in `_run_node` (skipped nodes never enter it). In
  `run_dag`'s loop, after each `_cascade_failures(by_id, status)` pass
  (`_runner.py:100`), diff `status` against a `set` of already-published node
  ids and emit `skipped` for each newly-skipped node.

All payloads share the shape `{run_id, node, status}` with
`status ∈ {running, completed, failed, skipped}`.

### 4.4 Wiring the bus into the factory

- **`make_subagent_tool_factory`** (`src/agentscope/subagent/_agent_tools.py:59`):
  add optional `message_bus: "MessageBus" | None = None`.
- Inside the per-turn `_factory` closure (`_agent_tools.py:82`, where `session_id`
  is in scope), just before constructing `SubAgentDagTool` (`~line 197`), build:

  ```python
  progress_publisher = None
  if message_bus is not None:
      async def progress_publisher(name, value, _bus=message_bus, _sid=session_id):
          # lazy import: subagent must not import app at module top
          # (precedent: `from ..app._tool import DeliverFiles` at _agent_tools.py:99)
          from ..app._bus_ops import publish_session_event
          from ..event import CustomEvent
          try:
              event = CustomEvent(name=name, value=value)
              await publish_session_event(_bus, _sid, event.model_dump(mode="json"))
          except Exception:
              logger.warning("dag progress publish failed", exc_info=True)
  ```

  Pass `progress_publisher=progress_publisher` to `SubAgentDagTool(...)`. The
  publisher pairs the app-global `_bus` (closed over) with the per-turn `_sid`
  (inner) exactly as `StateChangeMiddleware` pairs bus + session_id.
- **`examples/agent_service/main.py`** (`~line 75-100`): hoist
  `message_bus = InMemoryMessageBus()` to a variable; pass the **same instance**
  to both `create_app(message_bus=message_bus, ...)` and
  `make_subagent_tool_factory(storage, workspace_manager, message_bus)` so DAG
  events land on the channel the SSE stream reads.

*Rationale for this over widening `AgentToolFactory`:* injecting the bus at
factory construction is minimal and matches the existing `DeliverFiles`
lazy-import precedent; widening the `AgentToolFactory` signature (adding a 4th
positional to `_types.py`, `get_toolkit` at `_toolkit.py:236`, and every factory)
is invasive and touches unrelated tools.

### 4.5 Enrich result metadata into a self-describing graph

The authoritative (result-present) render must draw structure + status + detail
**from metadata alone** — on reload the ephemeral live overlay is gone and
`call.input` parsing should not be a load-bearing dependency. Today each returned
`files` entry is `{node, status, prompt_file, output_file}`, but `_finalize`
already computes `subagent`, `depends_on`, and `error` for the on-disk `manifest`
(`src/agentscope/subagent/_dag/_runner.py:362-369`). Enrich each returned `files`
entry to `{node, subagent, depends_on, status, prompt_file, output_file, error}`
by reading the same values it already has in scope; **cap `error` at ~500 chars**
so metadata stays far under the context/offload limit. This is additive
(back-compatible) and makes the frontend depend only on metadata for the final
graph — nodes, edges, statuses, and failure reasons all in one place.

### 4.6 Terminal-state fix

`src/agentscope/subagent/_dag/_tool.py:170` yields the success-path terminal chunk
with `state=ToolResultState.RUNNING`. The shared `ToolCallRow` shimmers/spins
while `!result || result.state === "running"`
(`examples/web_ui/frontend/src/components/chat/tool-renderers/_shared.tsx`), so a
finished DAG box would spin forever. Change it to `state=ToolResultState.SUCCESS`
(the validation-error path already yields `ERROR`). Per-node failures remain
visible as red nodes in the graph and in `summary.failed`; the *tool call itself*
completed, so `SUCCESS` is correct. The frontend additionally gates "DAG done" on
**metadata presence**, not on `result.state`, so it is robust even if this state
ever regresses.

## 5. Event contract

Two new `CustomEvent` names on the session channel:

| name | value | when |
|------|-------|------|
| `dag_run_started` | `{ run_id: str, nodes: [{ id: str, subagent: str, depends_on: string[] }] }` | once, at run start |
| `dag_node_updated` | `{ run_id: str, node: str, status: "running"｜"completed"｜"failed"｜"skipped" }` | on each node transition |

The final `tool_result` metadata remains the authoritative record, with `files`
enriched per §4.5:
`{ run_id, dir, terminal_outputs: [{node, text}], files: [{node, subagent, depends_on, status, prompt_file, output_file, error}], summary: {total, completed, failed, skipped} }`.

## 6. Frontend — consume, hold, render

### 6.1 New dependency

Add `@xyflow/react` (v12, React 19-compatible) to
`examples/web_ui/frontend/package.json` and `pnpm install`. Import its stylesheet
once (`@xyflow/react/dist/style.css`) from the DAG component module. This is the
only new dependency; layout is computed in-house (§6.5), so no `dagre`/`elk`.

### 6.2 Event routing (`src/hooks/useMessages.ts`)

- Add `onDagRunStarted?` and `onDagNodeUpdated?` to the options type (beside
  `onTeamUpdated`/`onStateUpdated`).
- In the `CUSTOM` block of `processEvent` (`useMessages.ts:168-189`), add two
  branches dispatching to those `optionsRef` callbacks, then `return` (never
  reaching `appendEvent` — custom events must not touch msg content). Options are
  read via `optionsRef`, so callback-identity churn is safe.

### 6.3 Live overlay state (`src/pages/chat/ChatViewport.tsx`)

- `const [dagRuns, setDagRuns] = useState<Record<string, DagRunLive>>({})` where
  `DagRunLive = { nodes?: DagNodeSpecLite[]; byNode: Record<string, {status: string}> }`
  keyed by `run_id`.
- `const [latestDagRunId, setLatestDagRunId] = useState<string | null>(null)`.
- `handleDagRunStarted(value)` → set `dagRuns[run_id]` with `nodes` + all-pending
  `byNode`, and `setLatestDagRunId(run_id)`.
- `handleDagNodeUpdated(value)` → merge `byNode[node] = {status}` into
  `dagRuns[run_id]`, and `setLatestDagRunId(run_id)`.
- Pass both handlers into the `useMessages(...)` options object.
- **Reset on session switch:** `useEffect(() => { setDagRuns({}); setLatestDagRunId(null); }, [sessionId])`
  (mirroring the `setTasksContext(null)` resets), so a prior session's DAG never
  bleeds through.
- Wrap the existing subtree in a new `<DagRunsContext.Provider value={{dagRuns, latestDagRunId}}>`
  right alongside the existing `<SubagentNamesContext.Provider>`.

*Note:* custom-event handlers call `setDagRuns` immediately (they bypass the
`requestAnimationFrame` msg-batch), which is good for animation latency. DAG node
churn is low-frequency (each node is a CLI sub-agent taking seconds–minutes), so
no throttle is needed; if that ever changes, throttle inside the handler.

### 6.4 New context (`src/components/chat/DagRunsContext.tsx`)

Mirror `SubagentNamesContext`:
`export const DagRunsContext = createContext<{dagRuns: Record<string, DagRunLive>; latestDagRunId: string | null}>({dagRuns: {}, latestDagRunId: null})`
+ `useDagRuns()` hook.

### 6.5 New feature dir (`src/components/dag/`)

- **`deriveDag.ts`** — types + typed reader (mirroring `deriveDeliverables.ts`):
  `DagManifest { run_id, dir?, files: {node,status,output_file,prompt_file}[], summary, terminal_outputs: {node,text}[] }`;
  `readDagManifest(metadata) → DagManifest | null` guarding
  `Array.isArray(metadata?.files)`.
- **`layoutDag.ts`** — pure layered layout: longest-path depth from roots → `x`
  column; stable order within a layer → `y`. Returns React Flow `nodes` (with
  `position`) + `edges` (from `depends_on`). Deterministic; no external layout lib.
- **`DagGraph.tsx`** — the React Flow canvas:
  - Fixed-height container (e.g. `h-80`) inside the collapsible body.
  - Custom node type = a status card (§6.7). Edges = `depends_on` arrows.
  - `fitView`; `<Controls />`; `<MiniMap />` shown only when node count exceeds a
    threshold.
  - **Scroll-zoom disabled** (`zoomOnScroll={false}`, `panOnScroll={false}`) so
    the canvas never hijacks page scroll; zoom via the Controls buttons.
  - Nodes non-connectable / non-deletable (display only); dragging allowed for
    exploration.
  - `onNodeClick` → set selected node id → render the detail panel (§6.8) below
    the canvas.
  - Theme-aware node/edge colors via Tailwind classes + the app's CSS vars.

### 6.6 Tool renderer (`src/components/chat/tool-renderers/RunSubagentDagRenderer.tsx`)

Register under key `run_subagent_dag` in `tool-renderers/index.tsx:22-31`
(the key must equal the backend tool name at `_dag/_tool.py:104`).

- `renderHeader(pair, t)` — `run_subagent_dag` label + node count + a compact
  summary chip (e.g. `4 nodes · 3 done · 1 running`).
- `renderBody(pair, t)` — returns a **small inner component** `<DagBody pair={pair} />`
  (not raw JSX), because it needs `useDagRuns()` and `renderBody` is a plain
  function, not a React component. `DagBody`:
  1. `const manifest = readDagManifest(pair.result?.metadata)`.
  2. **Authoritative branch:** if `manifest` present → build the graph entirely
     from `manifest.files` — nodes, edges (`depends_on`), per-node `status`,
     `subagent`, and `error` are all in each entry (§4.5), so this branch needs
     neither the live overlay nor `call.input` and is fully reload-safe.
  3. **Live branch:** else → read `{dagRuns, latestDagRunId}` from context; use
     `dagRuns[latestDagRunId]` for structure (`nodes` from `dag_run_started`) +
     per-node status; if the overlay is empty (before the first event), fall back
     to `parseInput(pair.call.input).nodes` with all-pending.
  4. Render `<DagGraph nodes edges selected onSelect />`.
  - If neither a manifest nor a live overlay nor parseable `call.input` is
    available, `return undefined` so the row falls back to the generic body
    (mirrors `DeliverFilesRenderer`'s absent-metadata fallback).

### 6.7 Node visuals

Custom node card shows: `id` (title), `subagent` (subtitle), and a status
color/icon:

| status | treatment |
|--------|-----------|
| pending | muted border, no icon |
| running | accent border + pulsing/spinner icon |
| completed | green border + ✓ |
| failed | red border + ✕ |
| skipped | grey, dashed border |

### 6.8 Node detail panel (click)

`onNodeClick` opens an inline detail block below the canvas, sourced only from
existing metadata:
- status, `subagent`, `depends_on` (upstream nodes);
- `output_file` path and `prompt_file` path (from `manifest.files`);
- `error` text when `status === "failed"` (the capped error from
  `manifest.files[node].error`, §4.5);
- inline output for terminal nodes (from `manifest.terminal_outputs[node]`).

During the **live** window only status/subagent/depends_on are available (no
files/errors yet); the full detail set fills in once the result lands. No new
endpoint; no file bytes pushed into the agent context.

### 6.9 Group summary + i18n

- Add `tool.summary.dag_one` / `tool.summary.dag_other` keys to
  `src/i18n/locales/en.json` and `zh.json` (kept lowercase to match siblings).
- Add a `run_subagent_dag` branch to `summarizeToolGroup`
  (`src/components/chat/MessageBubble.tsx:322-355`) so a collapsed group counts
  DAG calls meaningfully instead of the generic "called N tools".

## 7. Correctness under concurrency

**Single-active-run invariant:** the leader agent's ReAct loop executes tool
calls sequentially and blocks on each; `run_subagent_dag` is one blocking call
that yields a single terminal chunk. So at most one DAG run executes per session
at any instant.

- A **running** box (no `pair.result`) binds to `latestDagRunId` — under the
  invariant, that is exactly the run that box is executing.
- Once the box's result arrives (`metadata.files` present), it renders from
  metadata (authoritative) and ignores the overlay.
- **Past** runs in history each render from their own `metadata` — the overlay is
  irrelevant to them.
- On reload / session switch, the overlay is empty and every box renders from its
  own metadata; the reset effect (§6.3) prevents stale bleed-through.

This avoids needing `tool_call_id` (unavailable to the tool) while remaining
correct for the realistic execution model.

## 8. Error handling & edge cases

- **Publisher failure** (bus down / serialization error): swallowed + logged
  inside the publisher; the node proceeds. Live animation degrades to
  final-metadata-only.
- **`progress_publisher=None`** (standalone/test use, or app without a bus):
  runner behaves exactly as today; no events emitted.
- **Skipped-node cascade:** emitted from `run_dag` via the status diff, so
  skipped nodes are never silently dropped from the animation.
- **Partial/late connect:** a client joining mid-run may miss early transitions;
  the final metadata still yields the correct graph. (Durable projector is a
  follow-up.)
- **Partial-JSON `call.input`:** the live graph never depends on it — structure
  comes from `dag_run_started.nodes`; `call.input` is only a last-resort fallback
  and is parsed defensively (`parseInput` returns `{}` on failure).
- **Terminal chunk state:** now `SUCCESS`; the box chrome resolves and the DAG
  "done" state is gated on metadata presence regardless.
- **Large graphs:** React Flow `fitView` + `MiniMap` handle many nodes; the
  fixed-height container scrolls/zooms internally, never the page.

## 9. Testing & acceptance

**Backend (pytest, `tests/*_test.py`; whole-structure assertions per CLAUDE.md;
`AnyString`/`AnyValue` only for `run_id` and other nondeterministic fields):**
- `tests/subagent_dag_progress_test.py` (new): run a small DAG through `run_dag`
  with a **capturing fake publisher** (records `(name, value)` tuples), including
  a node that fails and cascades a skip. Assert the **whole ordered event list**:
  one `dag_run_started` with the full node structure, then `running`→terminal for
  each executed node, and a `skipped` for the cascaded node — with `run_id`
  matched via `AnyString`. Assert `progress_publisher=None` emits nothing and the
  run result is identical. Assert a publisher that raises does **not** fail the
  node (status still `completed`).
- Extend the existing DAG tool/runner tests: the success-path terminal chunk now
  has `state=SUCCESS` (validation error still `ERROR`), and the whole-structure
  assertions on `metadata.files` are updated for the enriched entries
  (`subagent`/`depends_on`/`error`), including a failed node with a capped
  `error` string.

**Frontend:** unit-test the pure `readDagManifest` and `layoutDag` helpers and the
`dagRuns` reducer logic if the web_ui has a test runner (none configured today);
otherwise rely on the smoke.

**End-to-end acceptance smoke** (via the `verify` skill): boot per the RavenX
web-UI setup (ravenx env, redis, service on :8001, `pnpm dev`); drive the leader
to run a multi-node DAG (with at least one dependency and, ideally, one failure →
cascade skip). Confirm: the graph appears with correct edges as the call starts;
nodes animate running→done/failed/skipped **live** during the run; clicking a
node shows its detail; the box resolves to "done" (no perpetual spinner); and a
**reload** rebuilds the identical final graph from metadata.

## 10. Files touched

**Backend (new):**
- `tests/subagent_dag_progress_test.py` — emitted-event-sequence tests.

**Backend (edit):**
- `src/agentscope/subagent/_dag/_tool.py` — `progress_publisher` param + pass to
  `run_dag`; terminal chunk `state=SUCCESS`.
- `src/agentscope/subagent/_dag/_runner.py` — thread `progress_publisher` through
  `run_dag`/`_run_group`/`_run_node`; emit `dag_run_started`, `running`,
  `completed`/`failed`, and cascade `skipped`; enrich `_finalize`'s returned
  `files` entries with `subagent`/`depends_on`/capped `error` (§4.5).
- `src/agentscope/subagent/_agent_tools.py` — `message_bus` param on
  `make_subagent_tool_factory`; build the per-session publisher in `_factory`;
  pass to `SubAgentDagTool`.
- `examples/agent_service/main.py` — hoist `message_bus` to a variable; pass to
  both `create_app` and the factory.

**Frontend (new):**
- `src/components/chat/DagRunsContext.tsx` — context + `useDagRuns()`.
- `src/components/dag/deriveDag.ts` — types + `readDagManifest`.
- `src/components/dag/layoutDag.ts` — pure layered layout → nodes/edges.
- `src/components/dag/DagGraph.tsx` — React Flow canvas + custom node + detail.
- `src/components/chat/tool-renderers/RunSubagentDagRenderer.tsx` — the renderer.

**Frontend (edit):**
- `package.json` — add `@xyflow/react`.
- `src/hooks/useMessages.ts` — `onDagRunStarted`/`onDagNodeUpdated` options + two
  `CUSTOM` branches.
- `src/pages/chat/ChatViewport.tsx` — `dagRuns`/`latestDagRunId` state, handlers,
  reset effect, `DagRunsContext.Provider`.
- `src/components/chat/tool-renderers/index.tsx` — register `run_subagent_dag`.
- `src/components/chat/MessageBubble.tsx` — `summarizeToolGroup` branch.
- `src/i18n/locales/en.json`, `zh.json` — `tool.summary.dag_*` keys.

No change to: the DAG node input schema, the scheduler ordering / file-based
message passing, the event/SSE/message-bus machinery, sub-agent CLI tools, team
tools, or the workspace managers.

## 11. Decisions

1. **Live per-node progress** (user choice) — over final-state-only or a phased
   rollout. Requires backend event emission + factory bus injection. (2026-07-21)
2. **Out-of-band `CustomEvent` transport** — over tool-native interim
   `ToolChunk`s. `CustomEvent` carries **structured** per-node status cleanly and
   mirrors the working `StateChangeMiddleware` precedent; interim `ToolChunk`s
   would deliver status only as text deltas (interim chunk metadata is not
   surfaced per-chunk). (2026-07-21)
3. **React Flow (`@xyflow/react`)** (user choice) — over hand-rolled SVG or
   dagre+SVG. Interactive pan/zoom/drag + minimap; one new frontend dep; in-house
   layered layout so no `dagre`/`elk`. (2026-07-21)
4. **Inline node detail from existing metadata** (user choice) — status,
   sub-agent, output-file path, error, terminal output; no new endpoint, no file
   bytes into the agent context. (2026-07-21)
5. **Inline in the tool-call box only** (user choice) — no right-dock panel in
   v1. (2026-07-21)
6. **Key the live overlay by `run_id` + single-active-run invariant** — because
   the tool cannot see its own `tool_call_id`; correct under the leader's
   sequential blocking execution model. (2026-07-21)
7. **Inject the bus at factory construction** — over widening `AgentToolFactory`;
   minimal and matches the `DeliverFiles` lazy-import precedent. (2026-07-21)
8. **Fix `state=RUNNING → SUCCESS`** on the success-path terminal chunk — a
   pre-existing quirk that would leave the box spinning forever; low-risk
   correctness fix folded into this work. The frontend also gates "done" on
   metadata presence for defense in depth. (2026-07-21)
9. **Enrich `metadata.files` with `subagent`/`depends_on`/capped `error`** — a
   small, back-compatible extension of the chosen "inline detail from existing
   metadata" so the authoritative graph (nodes, edges, statuses, failure
   reasons) is fully self-describing from metadata and reload-safe, without
   leaning on `call.input` parsing. The values already exist in `_finalize`'s
   on-disk manifest; `error` is capped (~500 chars) to stay under the offload
   limit. (2026-07-21)
