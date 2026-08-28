# Durable & live sub-agent run state — design

**Date:** 2026-07-21
**Status:** Approved (design)
**Builds on:** the sub-agent DAG live-visualization feature
(`docs/superpowers/specs/2026-07-21-subagent-dag-live-visualization-design.md`;
`src/agentscope/subagent/_dag/`, `examples/web_ui/frontend/src/components/dag/`,
`.../DagRunsContext.tsx`, `.../RunSubagentDagRenderer.tsx`), the durable
projection primitive (`src/agentscope/app/_service/_session_projection.py`) and
its one existing consumer `SubagentHitlProjector`
(`src/agentscope/app/_service/_projectors/_subagent_hitl.py`), the stateful
sub-agent instance system (`src/agentscope/subagent/_tool.py`,
`_openai_tool.py`, `_instance_registry.py`) and its web-UI monitor
(`examples/web_ui/frontend/src/components/subagent/SubagentInstanceMonitor.tsx`,
`deriveInstances.ts`), and the out-of-band `CustomEvent` → SSE path
(`src/agentscope/app/_bus_ops.py`, `src/agentscope/app/_router/_session.py`).

## 1. Motivation

The DAG live-visualization live-smoke surfaced two gaps, both rooted in the same
fact: our live UI state is **ephemeral** and does not survive a page reload or a
mid-run reconnect.

- **(A) The DAG graph does not survive reload.** The live per-node overlay lives
  only in `ChatViewport` React state. Worse, `run_subagent_dag` is offloaded to a
  background task after ~10s (its CLI sub-agents take minutes), so the offload
  middleware persists a **metadata-less placeholder** as the tool result and
  delivers the real result later as a text-only `HintBlock` — the authoritative
  manifest (nodes, statuses, output files, errors) never lands on the
  `tool_result` block. On reload of a finished offloaded run the graph renders
  all-pending. The event replay log that could rehydrate it is trimmed at the end
  of every run (`_service/_chat.py:658`).

- **(B) The "Instances" monitor has no run-status and no transport identity.** The
  chat right-dock `SubagentInstanceMonitor` derives per-session instances from the
  transcript, but there is no running/completed/failed state per instance. The
  sub-agent tools even signal success as `ToolResultState.RUNNING`, and offloaded
  calls (i.e. all the long ones) deliver their real outcome as a `HintBlock`, not
  a paired `tool_result` — so a client **cannot** derive true status from the
  transcript. The three transports (Claude Code, Codex, MiroMind) are also not
  visually distinguished.

This design makes both **durable and live** by mirroring the DAG live pattern onto
the existing `SessionProjection` durable store — the same mechanism that already
lets pending HITL cards survive a reload (`_router/_session.py:751-790`). Each
run's node state (and each instance's status) is written to a per-session Redis
hash and re-injected as `CustomEvent`s when the SSE stream (re)connects.

## 2. Scope

**In scope:**

- **Backend — DAG durability:** write a self-contained per-run projection entry
  (structure + live node statuses + final manifest) to the durable
  `SessionProjection` hash, from the existing DAG progress closure; emit a new
  terminal `dag_run_completed` event carrying the authoritative manifest so it
  travels durably (independent of the dropped `tool_result` metadata); replay the
  DAG projection on SSE connect.
- **Backend — instance status:** emit `subagent_instance_updated` events
  (`running`/`completed`/`failed`) from both `CliSubAgentTool` and
  `OpenAISubAgentTool` via a publisher wired in `make_subagent_tool_factory`
  (mirroring the DAG bus wiring); write a durable per-instance projection entry;
  replay it on SSE connect.
- **Backend — cleanup:** purge both projection kinds when a session is deleted
  (the delete cascade does not touch the projection hash today).
- **Frontend — DAG:** carry the manifest on the live overlay; add a
  `dag_run_completed` handler; **bind each DAG box to its own `run_id`** (order-
  based, replacing the `latestRunId` heuristic, which is unsafe once multiple
  runs' projections replay on reload); reconcile
  `tool_result` manifest → bound-run manifest → bound-run statuses → call-args.
- **Frontend — instances:** a new `SubagentInstancesContext` live overlay;
  `SubagentInstanceMonitor` merges its transcript-derived instance **list** with
  the overlay **status**, adding an animated status badge and a transport-type
  badge per row (and per exchange).
- **Tests:** backend unit tests for the projection write/replay/purge and the
  instance emit-on-every-path; frontend `pnpm build` + `pnpm lint` + a browser
  smoke.

**Out of scope / unchanged:**

- **The general `ToolOffloadMiddleware`.** We route DAG durability *around* the
  metadata drop via the projection, rather than reworking the shared middleware's
  `HintBlock` delivery to preserve `ToolResponse.metadata` (which would land on a
  *separate* `HintBlock`, not the original `tool_result` block, so it would not
  reach the renderer anyway — see §4.1). Smaller blast radius; no behavioural
  change for other offloaded tools.
- **A server-authoritative instance list.** The instance **list** stays derived
  from the durable transcript (message history already survives reload); only the
  **status** rides the projection. No `status` field is added to
  `SubAgentInstanceRecord`, and no `list_subagent_instances` HTTP endpoint is
  built.
- **The `/subagents` rail page** (a prototype/config manager) — the live list
  stays in the chat "Instances" dock (user choice).
- **Mid-run *live-animation completeness* for events emitted after the run's turn
  ended and log was trimmed** is handled by the durable projection replay (that is
  the point); we do **not** additionally add a `Last-Event-Id` resume cursor to
  the raw event log.
- No change to the DAG scheduler / file-based message passing, the sub-agent CLI
  or HTTP transports themselves, the instance-registry create/resume semantics, or
  the event/SSE/message-bus machinery. We ride existing rails.

## 3. Architecture

Both features are one shape: an out-of-band `CustomEvent` drives the **live**
overlay (already working); a matching write to the durable `SessionProjection`
hash makes it survive **reload**; an SSE-connect replay block re-emits the stored
state as the same `CustomEvent`s so the frontend rebuilds with no new plumbing.

**Verified round-trip facts (from the run loop and SSE generator):**

- **Out-of-band events do NOT flow through `EventProjector`.** `ChatService`
  calls `_project_event` only on events yielded by `agent.reply_stream`
  (`_service/_chat.py:579,626`). `dag_*` (and the new `subagent_instance_*`)
  events are published *directly* to the bus by a per-turn closure — they never
  enter `reply_stream`. Therefore the durable write **must be done inside the
  publishing closure itself**, calling `SessionProjection.upsert(...)` +
  `.publish(...)` directly (the exact write pair `SubagentHitlProjector` uses,
  just invoked from the closure rather than a projector). We do **not** add an
  `EventProjector`.
- **The projection hash is durable; the replay log is not.** `SessionProjection`
  is backed by a per-session Redis hash `agentscope:session:projection:{sid}`
  with fields `{kind}:{entry_id}`, **no TTL**
  (`_service/_session_projection.py`). The event replay log
  (`agentscope:session:events:{sid}`) is trimmed to nothing at the end of every
  run (`_service/_chat.py:658`), so it can never be the durable carrier.
- **The SSE generator already replays a projection on connect.** `_sse_generator`
  §1b lists `SessionProjection.list(sid, SubagentHitlProjector.KIND)` and yields
  each as a synthetic `CustomEvent` *after* the log replay and *before* the live
  subscription (`_router/_session.py:751-790`). We add §1c (`dag_run`) and §1d
  (`subagent_instance`) blocks right after it, in the same shape.
- **`CustomEvent` is generic** (`{type:"custom", name, value}`) and the frontend
  parses every `data:` frame with no name allow-list, so new event names need no
  SDK/package bump (established by the DAG feature).
- **Offloaded background tasks keep publishing after the turn ends.** The DAG
  already animates live for offloaded runs, so the closure's `publish` reaches
  live subscribers; its `upsert` to the durable hash lands identically. Trim at
  run end is irrelevant to the hash.

**Data flow (DAG durability):**

```
run_subagent_dag  (backend, possibly in an offloaded background task)
  ├─ dag_run_started {run_id, nodes:[{id,subagent,depends_on}]}
  ├─ dag_node_updated {run_id, node, status:"running"|"completed"|"failed"|"skipped"}   (per node)
  └─ dag_run_completed {run_id, manifest:{dir,files,terminal_outputs,summary}}           (NEW, once at end)
        │
   progress closure (subagent/_agent_tools.py):
        ├─ SessionProjection.publish(sid, name, value)        → LIVE overlay (as today)
        └─ SessionProjection.upsert(sid, "dag_run", run_id,   → DURABLE hash
              {run_id, created_at, nodes, byNode:{id:status}, manifest?})
        ▼
   SSE GET /sessions/{sid}/stream:
        §1  log replay (in-progress run only)
        §1c dag_run projection replay → dag_run_started → dag_node_updated* → dag_run_completed
        §2  live subscribe
        ▼
   useMessages.processEvent CUSTOM → onDagRunStarted/onDagNodeUpdated/onDagRunCompleted
        → ChatViewport dagRuns overlay (byNode + optional manifest, keyed by run_id)
        ▼
   RunSubagentDagRenderer.DagBody, bound to THIS box's run_id (§7.1):
        result.metadata manifest? → else bound-run manifest? → else bound-run byNode → else call-args
```

**Data flow (instance status):** identical, with `subagent_instance_updated`
events keyed by the instance **handle** (which is present in the tool-call input,
so no run-id-style binding problem — §7.2), projection kind `subagent_instance`,
consumed by a new `SubagentInstancesContext` and rendered by
`SubagentInstanceMonitor`.

## 4. Backend — DAG reload-durability

### 4.1 Why a projection, not an offload fix

The manifest is dropped because the offload middleware delivers the real result as
a `HintBlock` (a later system message), leaving the original `tool_result` block a
metadata-less placeholder (`_tool_offload_middleware.py:398-407, 343-361`).
Preserving `ToolResponse.metadata` would attach it to the `HintBlock`, **not** the
`tool_result` block the renderer keys on — so it still would not reach the graph.
The durable projection is the viable carrier and is exactly what the SSE replay is
built for.

### 4.2 Emit the manifest as a terminal event

`SubAgentDagTool.call` already assembles the authoritative manifest for the
terminal `ToolChunk.metadata` (`_dag/_tool.py:178-189`:
`{run_id, dir, terminal_outputs, files, summary}`). Right where that dict is built
(and before/with yielding the terminal chunk), emit one more progress event via
the already-threaded publisher, guarded by the same swallow-all pattern the DAG
runner uses for progress (a publish must never fail the tool):

```python
# _dag/_tool.py, after building `metadata`, inside the same call()
if progress_publisher is not None:
    try:
        await progress_publisher("dag_run_completed",
                                 {"run_id": metadata["run_id"], "manifest": metadata})
    except Exception:
        logger.warning("dag_run_completed publish failed", exc_info=True)
```

`files` entries are already enriched with `subagent`/`depends_on`/capped `error`
(DAG feature §4.5), so the manifest is fully self-describing. This runs inside the
offloaded background task, so it fires even when offloaded.

### 4.3 Durable write from the progress closure

The DAG progress closure `_publish_dag_progress`
(`subagent/_agent_tools.py:207-240`, gated on `message_bus is not None`) today
only does `publish_session_event`. Extend it to also maintain a self-contained
per-run projection entry via a `SessionProjection(message_bus)`:

- Keep a per-closure accumulator `runs: dict[str, dict]` (the closure outlives the
  turn while an offloaded task runs, and a DAG run is one tool call, so in-closure
  accumulation is sufficient — no read-modify-write against Redis per node).
- `dag_run_started`: seed `runs[run_id] = {"run_id", "created_at": <iso>,
  "nodes": value["nodes"], "byNode": {n["id"]: "pending" for n in nodes}}`.
- `dag_node_updated`: `runs[run_id]["byNode"][node] = status`.
- `dag_run_completed`: `runs[run_id]["manifest"] = value["manifest"]`.
- After each mutation: `await projection.upsert(sid, "dag_run", run_id, runs[run_id])`
  **and** `await projection.publish(sid, name, value)` for the live event. Both
  wrapped by the existing swallow-all guard (a projection hiccup must never fail a
  node).

`created_at` uses the wall clock at emit time; it exists solely to order runs for
the frontend binding (§7.1). Because each entry is self-contained, the unordered
hash `list()` at replay time is fine.

*Note on `created_at` and determinism:* the value is produced by the running
service (not a workflow/replayable context), so `datetime.now().isoformat()` is
appropriate here; it is nondeterministic and tests assert it with `AnyString`.

### 4.4 SSE replay (§1c)

In `_sse_generator`, right after the §1b HITL block (`_router/_session.py:790`),
add (where `_sse(evt)` is shorthand for the existing
`f"data: {json.dumps(evt.model_dump(mode='json'), ensure_ascii=False)}\n\n"`
framing used by §1b):

```python
# 1c. Replay durable DAG-run projections (design §4.4). Self-contained
#     per-run entries survive the run-end log trim and page reloads.
for entry in await projection.list(session_id, "dag_run"):
    yield _sse(CustomEvent(name="dag_run_started",
                           value={"run_id": entry["run_id"], "nodes": entry["nodes"]}))
    for node_id, st in entry.get("byNode", {}).items():
        if st != "pending":
            yield _sse(CustomEvent(name="dag_node_updated",
                                   value={"run_id": entry["run_id"], "node": node_id, "status": st}))
    if entry.get("manifest") is not None:
        yield _sse(CustomEvent(name="dag_run_completed",
                               value={"run_id": entry["run_id"], "manifest": entry["manifest"]}))
```

Ordering (started → updates → completed) is generator-controlled, so the
hash-unordered `list()` does not matter. The frontend handlers are idempotent
merges, so re-replaying while a run is still live is safe.

### 4.5 Purge on session delete

The `delete_session` cascade (`storage/_redis_storage.py:966-1005`) does not touch
the projection hash. Add a scoped purge —
`SessionProjection(message_bus).purge(sid, "dag_run")` (and the instance kind,
§5.4) — at the **app-layer session-delete entry point** where the `message_bus` is
in scope (the delete route/service, alongside wherever `SubagentHitlProjector`
cleanup runs). Scoped by kind so unrelated projections survive. *(The exact call
site is pinned during planning by locating the session-delete handler that has the
bus; `RedisStorage.delete_session` itself has no bus and is the wrong layer.)*

## 5. Backend — instance live status

### 5.1 Emit points (both tools, every path)

A stateful sub-agent call is one create-or-resume invocation. Status semantics:
`running` = invocation in flight, `completed` = invocation returned, `failed` =
invocation errored. Emit only for **stateful, handle-bearing** calls (stateless
one-shot calls have no instance identity and are not shown in the monitor).

- **`running`** — in `CliSubAgentTool.call` (`_tool.py:217+`) and
  `OpenAISubAgentTool.call` (`_openai_tool.py:246+`), right after
  `registry.lookup()` decides create vs resume and the `handle` is known, **before**
  the `exec_shell`/HTTP dispatch. `agent_id` may still be `None` at this point for
  a derived-id CLI (Codex mints it during exec); emit it as-known.
- **`completed`** — at the success terminal chunk (`_tool.py:417-431`,
  `_openai_tool.py:470-484`), with the resolved `agent_id`/`action`.
- **`failed`** — at **every** `yield ToolChunk(..., state=ERROR); return`
  early-return (lookup failure, prompt IO, exec failure, timeout, non-zero exit,
  HTTP error, malformed response, `finish_reason in {error, cancelled}`, empty
  content, Codex id-extraction failure).

Guarded by the same swallow-all wrapper; a publish failure never changes tool
behaviour.

### 5.2 Event payload & transport discriminant

```
subagent_instance_updated  value = {
  handle:     str,                       # input.instance (the stable key)
  agent_id:   str | null,                # CLI session id / codex thread_id / uuid4
  prototype:  str,                       # config.name (the tool name)
  transport:  "cli" | "codex" | "openai",# 3-way, computed from config
  status:     "running" | "completed" | "failed",
  action:     "create" | "resume" | null # known at running-time for CLI resume; else on terminal
}
```

`transport` is computed from the tool's own config: `openai_subagent` →
`"openai"`; `cli_subagent` with `transcript_format == "codex_jsonl"` → `"codex"`;
other `cli_subagent` → `"cli"` (Claude Code / generic CLI — `transcript_format`
is the only data-level Codex marker). The frontend maps these to a badge label +
colour (§8.3). *(Labeling is heuristic for custom CLI prototypes; confirmable.)*

### 5.3 Publisher wiring

Mirror the DAG bus wiring in `make_subagent_tool_factory`
(`subagent/_agent_tools.py`). In the per-turn `_factory` closure (where
`session_id` is in scope and `message_bus` is the injected app-global), build a
`_publish_instance_progress` closure identical in shape to `_publish_dag_progress`
(lazy-import `publish_session_event`/`CustomEvent`, gated on
`message_bus is not None`) that does `projection.upsert(sid, "subagent_instance",
handle, payload)` + `projection.publish(sid, "subagent_instance_updated",
payload)`. Thread a `progress_publisher` param into both `CliSubAgentTool`
(`:139`) and `OpenAISubAgentTool` (`:192`) constructors, defaulting to `None` so
standalone/test construction is unchanged.

The projection entry is the latest payload for that `handle` (last write wins) —
`running` then `completed`/`failed`. That is exactly the "current status" the
monitor wants after reload.

### 5.4 SSE replay (§1d) & purge

After §1c, add §1d: `for entry in await projection.list(session_id,
"subagent_instance"): yield _sse(CustomEvent(name="subagent_instance_updated",
value=entry))`. Add `SessionProjection.purge(sid, "subagent_instance")` at the
same session-delete site as §4.5.

## 6. Event contract

Two new `CustomEvent` names (plus the DAG feature's existing two, unchanged):

| name | value | when |
|------|-------|------|
| `dag_run_completed` | `{ run_id: str, manifest: {dir, files:[{node,subagent,depends_on,status,prompt_file,output_file,error}], terminal_outputs:[{node,text}], summary:{total,completed,failed,skipped}} }` | once, at DAG run end (also replayed) |
| `subagent_instance_updated` | `{ handle, agent_id\|null, prototype, transport:"cli"｜"codex"｜"openai", status:"running"｜"completed"｜"failed", action:"create"｜"resume"｜null }` | on each instance invocation transition |

Durable projection entries (Redis hash `agentscope:session:projection:{sid}`):

| kind | entry_id | payload |
|------|----------|---------|
| `dag_run` | `run_id` | `{run_id, created_at, nodes:[{id,subagent,depends_on}], byNode:{id:status}, manifest?}` |
| `subagent_instance` | `handle` | the latest `subagent_instance_updated` value |

## 7. Frontend — DAG

### 7.1 Binding a box to its own run (correctness)

The DAG feature keyed the live overlay off `latestRunId` under a single-active-run
invariant. That breaks once **multiple** runs' projections replay on reload: every
result-less/offloaded box would read the newest run. Replace it with an
**order-based binding**, correct under the same invariant:

- The overlay `dagRuns` is keyed by `run_id`, each entry carrying `created_at`.
- `ChatViewport` (which holds `msgs`) computes a `Map<tool_call_id, run_id>`:
  take the `run_subagent_dag` tool-call blocks in transcript order, **excluding
  those whose `tool_result` is an error** (a validation-error call produced no run,
  so it must not consume a run slot), and zip them with the `dag_run` overlay
  entries ordered by `created_at`. The k-th run-producing DAG call ↔ the k-th run.
- Expose the map via `DagRunsContext`. `DagBody` looks up `pair.call.id` → `run_id`
  → overlay entry.
- Under single-active-run (the leader executes DAGs sequentially and blocks), this
  ordering is exactly chronological, so the binding is correct live, in history,
  and after reload. If counts ever mismatch (e.g. a run that started then errored
  at the tool level — rare; validation errors happen *before* a run exists), the
  last box falls back to the newest run.

### 7.2 Overlay, handlers, reconciliation

- `DagRunLive` gains `created_at: string` and optional `manifest: DagManifest`.
- `useMessages` (`hooks/useMessages.ts` CUSTOM branch): add an `onDagRunCompleted`
  option + branch alongside the existing `onDagRunStarted`/`onDagNodeUpdated`.
- `ChatViewport`: `handleDagRunStarted` stores `created_at`;
  `handleDagRunCompleted(value)` sets `dagRuns[run_id].manifest`. Keep the
  reset-on-`sessionId` effect. Provide the §7.1 binding map through the context.
- `RunSubagentDagRenderer.DagBody` reconciliation, using the **bound** run
  (not `latestRunId`):
  1. `readDagManifest(pair.result?.metadata)` → authoritative (non-offloaded runs);
  2. else bound-run `manifest` (offloaded / reloaded runs — **full fidelity**);
  3. else bound-run `byNode` statuses (live, pre-completion) with structure from
     the entry's `nodes`;
  4. else not-error → planned all-pending from `call.input`;
  5. else empty → error-text fallback (invalid DAG).

Branch 2 restores clickable per-node detail (output files, terminal output,
errors) after reload, satisfying the full-fidelity choice.

## 8. Frontend — instances

### 8.1 Overlay context

New `src/components/chat/SubagentInstancesContext.tsx` mirroring `DagRunsContext`:
`{ instances: Record<handle, InstanceStatus> }` where `InstanceStatus =
{ status: 'running'|'completed'|'failed'; transport: string; agentId?: string;
prototype: string; action?: string }`; plus `useSubagentInstances()`.
`ChatViewport` owns `const [subagentInstances, setSubagentInstances] =
useState<Record<string, InstanceStatus>>({})`, a `handleSubagentInstanceUpdated`
merge handler wired into `useMessages`, the reset-on-`sessionId` effect, and a
`<SubagentInstancesContext.Provider>`.

### 8.2 Monitor merge

`SubagentInstanceMonitor` calls `useSubagentInstances()` and merges by `handle`:
the instance **list** (and exchange history) still comes from
`deriveInstances(msgs, statefulNames)` (durable via message history); the **status
+ transport** come from the overlay (durable via projection). Status is the SSOT
from the overlay, **not** derived from the transcript (the offload reason in §1B).

### 8.3 Row & exchange visuals

Each instance row gains:
- a **transport-type badge** from `transport` (`cli`→"Claude Code"/"CLI",
  `codex`→"Codex", `openai`→"MiroMind"), with a distinct colour;
- an **animated status badge**: `running` = accent + spinner, `completed` =
  green ✓, `failed` = red ✕ (reusing the DAG node status treatment).

The expanded exchange history shows the per-exchange status where available.
`i18n` keys for the status labels and transport labels are added to `en.json`
/`zh.json` under the existing `subagent-monitor` group.

## 9. Correctness, ordering, idempotency

- **Idempotency:** overlays are keyed maps updated by merge, so a mid-run
  reconnect that re-replays live events (or the projection then the live tail) is
  safe.
- **Ordering:** the replay generator emits DAG events started → updates →
  completed deterministically; instance entries are single-payload. Hash order is
  irrelevant.
- **Single-active-run:** unchanged assumption; §7.1 makes the DAG binding correct
  under it even across reload. Instances key by `handle` (present in the call
  input and the event), so they have no ordering dependency at all.
- **Offload:** the long CLI/HTTP calls are exactly what gets offloaded; the
  background task's `publish` + `upsert` both run after the turn ends, and
  durability rides the hash (immune to the run-end log trim).

## 10. Error handling & edge cases

- **Publisher / projection failure:** swallowed + logged in the closure; the node
  / invocation proceeds; live+durable state degrades gracefully.
- **`progress_publisher=None`** (no bus, standalone/tests): tools and runner
  behave exactly as today; nothing emitted or persisted.
- **Derived-id CLI (Codex):** `running` may carry `agent_id=null`; the terminal
  `completed` carries the extracted id. If id extraction fails, a `failed` is
  emitted and no instance is committed (matches current behaviour).
- **Stateless calls:** no `handle` → no instance events (not shown in the monitor).
- **Invalid DAG:** validation error → no `run_id`, no projection entry, no run
  slot consumed in §7.1; the box shows "Invalid DAG…" text (branch 5).
- **Projection growth:** one entry per DAG run and one per instance handle, per
  session, no TTL — modest and matching the HITL precedent; bounded by session
  lifetime and purged on session delete (§4.5/§5.4). A `dag_run` entry carries the
  full manifest (incl. `terminal_outputs` text) — the same payload the
  `tool_result` metadata already holds for non-offloaded runs, so this persists in
  Redis what history already persists elsewhere; no extra cap beyond the existing
  ~500-char `error` cap.
- **Reconnect mid-run:** §1 log replay covers the in-flight run's recent events;
  §1c/§1d cover everything durable (including offloaded runs whose events post-date
  the trim). Together they rebuild the current picture.

## 11. Testing & acceptance

**Backend (pytest, `tests/*_test.py`; whole-structure assertions per CLAUDE.md;
`AnyString`/`AnyValue` for `run_id`/`agent_id`/`created_at`):**

- `tests/subagent_dag_projection_test.py` (new): run a small DAG through the
  progress closure with a **fake `SessionProjection`** (records `upsert`/`publish`
  calls). Assert the whole final `dag_run` entry: `nodes`, terminal `byNode`
  statuses (incl. a failed node and a cascaded `skipped`), and the attached
  `manifest`. Assert `dag_run_completed` is emitted once with the manifest.
- Replay unit: given a stored `dag_run` entry, assert the generator yields the
  exact ordered `dag_run_started` → `dag_node_updated`* (skipping `pending`) →
  `dag_run_completed` events.
- `tests/subagent_instance_progress_test.py` (new): drive `CliSubAgentTool` and
  `OpenAISubAgentTool` (with fakes) through create-success, resume-success, and
  representative error paths; assert the emitted `subagent_instance_updated`
  sequence per path (`running` then `completed`/`failed`), the `transport`
  discriminant for each of the three configs, and that the projection entry for
  the handle holds the latest payload. Assert stateless calls emit nothing and
  `progress_publisher=None` is a no-op.
- Purge: assert the session-delete path purges `dag_run` + `subagent_instance`
  kinds (and leaves other kinds intact).

**Frontend:** no unit runner → `pnpm build` + `pnpm lint`; unit-test the pure
binding-map and reconciliation helpers if extracted.

**End-to-end acceptance smoke** (via the `verify` skill; RavenX web-UI setup —
ravenx env, redis, service on :8001, `pnpm dev`):

1. Run a multi-node DAG with a dependency and a failure→skip; watch it animate;
   **reload the page** and confirm the finished graph is fully restored (colours +
   clickable per-node output/error), not all-pending.
2. Run a **second** DAG in the same session; reload; confirm **each** box shows its
   own graph (no cross-render).
3. Invoke stateful sub-agents of all three transports; confirm the "Instances"
   dock shows each with a transport badge and an animated running→done/failed
   status; **reload** and confirm the statuses persist.

## 12. Files touched

**Backend (new):**
- `tests/subagent_dag_projection_test.py`
- `tests/subagent_instance_progress_test.py`

**Backend (edit):**
- `src/agentscope/subagent/_dag/_tool.py` — emit `dag_run_completed` with the
  manifest.
- `src/agentscope/subagent/_agent_tools.py` — extend `_publish_dag_progress` to
  `upsert`/`publish` the durable `dag_run` entry (accumulator); add
  `_publish_instance_progress`; thread `progress_publisher` into both sub-agent
  tool constructors.
- `src/agentscope/subagent/_tool.py`, `src/agentscope/subagent/_openai_tool.py` —
  accept `progress_publisher`; emit `running`/`completed`/`failed` on every path;
  compute `transport`.
- `src/agentscope/app/_router/_session.py` — SSE §1c (`dag_run`) + §1d
  (`subagent_instance`) replay blocks.
- The app-layer session-delete handler — purge `dag_run` + `subagent_instance`
  projection kinds (location pinned in the plan).

**Frontend (new):**
- `src/components/chat/SubagentInstancesContext.tsx` — context + hook.

**Frontend (edit):**
- `src/hooks/useMessages.ts` — `onDagRunCompleted` + `onSubagentInstanceUpdated`
  options and `CUSTOM` branches.
- `src/pages/chat/ChatViewport.tsx` — manifest on `DagRunLive`,
  `handleDagRunCompleted`, the §7.1 `tool_call_id`→`run_id` binding map,
  `subagentInstances` state + handler + reset + Provider.
- `src/components/chat/DagRunsContext.tsx` — carry `created_at` + `manifest` +
  the binding map.
- `src/components/chat/tool-renderers/RunSubagentDagRenderer.tsx` — bound-run
  reconciliation (§7.2).
- `src/components/dag/deriveDag.ts` / `layoutDag.ts` — types only if manifest
  fields grow (no behavioural change expected).
- `src/components/subagent/SubagentInstanceMonitor.tsx`,
  `src/components/subagent/deriveInstances.ts` — merge overlay status; status +
  transport badges.
- `src/i18n/locales/en.json`, `zh.json` — instance status + transport labels.

No change to: the DAG scheduler / file-based message passing, the sub-agent
transports, the instance-registry create/resume semantics, `SubAgentInstanceRecord`,
or the event/SSE/message-bus machinery.

## 13. Decisions

1. **Durable `SessionProjection` hash + SSE-connect replay** (user-requested) —
   over the trimmed event log (not durable) or an offload-middleware rework
   (lands on the wrong block, §4.1). Reuses the working `SubagentHitlProjector`
   pattern. (2026-07-21)
2. **Write the projection directly in the publishing closure**, not via an
   `EventProjector` — because out-of-band `dag_*`/instance events never reach
   `_project_event` (§3, verified in the run loop). (2026-07-21)
3. **Emit the manifest as a terminal `dag_run_completed` event** so it travels
   durably to the projection, independent of the dropped `tool_result` metadata.
   (2026-07-21)
4. **Full-fidelity DAG reload** (user choice) — persist the whole manifest so a
   reloaded run restores structure, statuses, and per-node click-through. (2026-07-21)
5. **Order-based per-box `run_id` binding** replacing the `latestRunId` heuristic —
   required for correctness once multiple runs' projections replay on reload;
   correct under the single-active-run invariant (§7.1). (2026-07-21)
6. **Instances list stays transcript-derived; only status rides the projection**
   — over a server-authoritative registry + list endpoint (YAGNI; the transcript
   already survives reload; status keys cleanly by `handle`). (2026-07-21)
7. **Live list in the chat "Instances" dock** (user choice) — not the `/subagents`
   prototype page. (2026-07-21)
8. **Three-way `transport` discriminant computed from config**
   (`openai`/`codex`/`cli`) — heuristic where `transcript_format=="codex_jsonl"`
   marks Codex; surfaced as a per-row badge. (2026-07-21)
