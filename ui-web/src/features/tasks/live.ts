/* Pure reducers: one turn event, applied to the rows `tasks.list` last
 * answered with.
 *
 * Contract §5.1. Every function here is a plain `(rows, payload) -> rows`
 * (or, where the row's terminal fields need the server's own arithmetic, that
 * plus a `{kind, id}` the caller should reconcile through `one()`) -- no
 * store, no gateway, no clock read from a global. `store.ts` is the only
 * caller, and it is the only thing here allowed a side effect.
 *
 * All five are idempotent on (kind, id[, node, status]): `turn.subscribe`
 * replays the current turn's whole buffer into a fresh subscription
 * (contract §5.1's "reconnection" note), and applying the same frame twice
 * must land on the same rows it landed on the first time. Every reducer below
 * either sets a field to the value the event carries (replaying the same
 * event sets it to the same value again) or is guarded by an existence check
 * before it inserts a row.
 */

import type {
  DagNodeUpdatedEvent, DagRunCompletedEvent, DagRunReplannedEvent, DagRunStartedEvent, SubagentStatusEvent,
} from '../../rpc/generated'
import type { TaskCounts, TaskKind, TaskNode, TaskRow, TaskStatus } from './types'

export type RunStartedPayload = DagRunStartedEvent['payload']
export type NodeUpdatedPayload = DagNodeUpdatedEvent['payload']
export type RunCompletedPayload = DagRunCompletedEvent['payload']
export type RunReplannedPayload = DagRunReplannedEvent['payload']
export type SubagentStatusPayload = SubagentStatusEvent['payload']

export interface LiveResult {
  rows: TaskRow[]
  /* Set when the frame settled something a snapshot alone cannot finish --
     the server's own tokens, files and final error text. `store.ts` reads
     this and reconciles that one task through `one(kind, id)`. */
  refetch?: { kind: TaskKind; id: string }
}

/* A run id is `<UTC stamp><6-digit microseconds>Z-<hex>` (`make_run_id`,
   `dag_store.py:393`). `dag.run_started` carries no timestamp of its own, so
   until the first node reports one this is the best a row can show. */
const RUN_ID_STAMP = /^(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})(\d{3})\d{3}Z-/

export function startOfRunId(id: string): number | null {
  const m = RUN_ID_STAMP.exec(id)
  if (!m) return null
  const [, y, mo, d, h, mi, s, ms] = m
  return Date.UTC(Number(y), Number(mo) - 1, Number(d), Number(h), Number(mi), Number(s), Number(ms))
}

const blankCounts = (): TaskCounts => (
  { total: 0, pending: 0, running: 0, completed: 0, failed: 0, skipped: 0, cancelled: 0, interrupted: 0, exception: 0 }
)

export function countsOf(nodes: readonly TaskNode[]): TaskCounts {
  const c = blankCounts()
  c.total = nodes.length
  nodes.forEach((n) => {
    switch (n.status) {
      case 'pending': c.pending += 1; break
      case 'running': c.running += 1; break
      case 'completed': c.completed += 1; break
      case 'failed': c.failed += 1; break
      case 'skipped': c.skipped += 1; break
      case 'cancelled': c.cancelled += 1; break
      case 'interrupted': c.interrupted += 1; break
      case 'exception': c.exception += 1; break
    }
  })
  return c
}

/* The task-status ladder, contract §2.5 rules 1-5 (rule 0, the replan link,
   is a fact of `graph.json` a live node status cannot carry and is applied by
   `applyRunReplanned` directly). */
export function deriveStatus(nodes: readonly TaskNode[]): TaskStatus {
  if (nodes.some((n) => n.status === 'failed')) return 'failed'
  if (nodes.some((n) => n.status === 'interrupted')) return 'interrupted'
  if (nodes.some((n) => n.status === 'pending' || n.status === 'running' || n.status === 'exception')) return 'running'
  if (nodes.some((n) => n.status === 'cancelled' || n.status === 'skipped')) return 'cancelled'
  return 'completed'
}

const blankNode = (id: string): TaskNode => ({
  node_id: id, node_summary: null, agent: '', instance: null, status: 'pending', depends_on: [],
  started_at: null, ended_at: null, error: null, tokens_in: null, tokens_out: null,
  tool_call_count: null, tool_failure_count: null, has_output: null, prompt_template: null, files: [],
})

/* The graph arrives whole, before any node runs: a spawn row at `pending`,
   built the same way, follows from `subagent.status` instead. Guarded so a
   replayed frame -- the same run's `dag.run_started` twice -- inserts once. */
export function applyRunStarted(rows: readonly TaskRow[], p: RunStartedPayload): TaskRow[] {
  if (rows.some((r) => r.kind === 'dag' && r.id === p.run_id)) return [...rows]
  const nodes: TaskNode[] = (p.nodes || []).map((n) => ({
    ...blankNode(n.id),
    node_summary: n.node_summary ?? null,
    /* `subagent -> agent`: the event's own name for the field, renamed once
       at the boundary rather than carried into the row (contract §7). */
    agent: n.subagent,
    instance: n.instance ?? null,
    depends_on: n.depends_on || [],
  }))
  const row: TaskRow = {
    id: p.run_id, kind: 'dag', task_summary: p.task_summary ?? null, status: 'running',
    started_at: startOfRunId(p.run_id) ?? Date.now(), ended_at: null, agent: null, handle: null,
    counts: countsOf(nodes), nodes,
  }
  return [row, ...rows]
}

/* Moves one node. Timestamps only where the event actually carries them --
   a skipped/cancelled transition has none (contract §5.1) -- and the row's
   own status and counts are recomputed from the whole node list every time. */
export function applyNodeUpdated(rows: readonly TaskRow[], p: NodeUpdatedPayload): TaskRow[] {
  return rows.map((row) => {
    if (row.kind !== 'dag' || row.id !== p.run_id) return row
    const nodes = row.nodes.map((n) => (n.node_id === p.node
      ? { ...n, status: p.status, started_at: p.started_at ?? n.started_at, ended_at: p.ended_at ?? n.ended_at }
      : n))
    return { ...row, nodes, counts: countsOf(nodes), status: deriveStatus(nodes) }
  })
}

/* Ends the frame. `files` absent (or empty) is the hard-cancel shape --
   `manifest.json` never got written, so there is nothing to fold into the
   nodes and the row's own ending is `cancelled` outright; otherwise every
   node named in `files` takes its final status and error. Either way the row
   is provisional until `one(kind, id)` re-reads it: this frame carries no
   tokens, no output files, and (on a hard cancel) no per-node error text. */
export function applyRunCompleted(rows: readonly TaskRow[], p: RunCompletedPayload): LiveResult {
  const at = rows.findIndex((r) => r.kind === 'dag' && r.id === p.run_id)
  if (at < 0) return { rows: [...rows] }
  const row = rows[at]!
  const files = p.files || []
  const nodes = files.length
    ? row.nodes.map((n) => {
      const f = files.find((x) => x.node === n.node_id)
      return f ? { ...n, status: f.status, error: f.error ?? n.error } : n
    })
    : row.nodes
  const next: TaskRow = {
    ...row, nodes, counts: countsOf(nodes), status: files.length ? deriveStatus(nodes) : 'cancelled',
    ended_at: Date.now(),
  }
  return { rows: rows.map((r, i) => (i === at ? next : r)), refetch: { kind: 'dag', id: p.run_id } }
}

/* The superseded run reads as cancelled the moment its replacement is
   announced -- `started: true` is the optimistic read for the frame that just
   arrived; `one(kind, id)` corrects it if the successor never actually came
   up (contract §2.5 rule 0). The new row is not built here: it arrives on its
   own `dag.run_started`. */
export function applyRunReplanned(rows: readonly TaskRow[], p: RunReplannedPayload): LiveResult {
  const at = rows.findIndex((r) => r.kind === 'dag' && r.id === p.run_id)
  if (at < 0) return { rows: [...rows] }
  const next: TaskRow = {
    ...rows[at]!, status: 'cancelled',
    replan: { run_id: p.replan_run_id, from_node: p.from_node, reason: p.reason, started: true },
  }
  return { rows: rows.map((r, i) => (i === at ? next : r)), refetch: { kind: 'dag', id: p.run_id } }
}

/* A spawn's own lifecycle. Two id spaces meet here: `task_id` is the
   manager's handle on the run and is all a `pending` frame carries, while
   `TaskRow.id` is the model's own record id, first seen on `call_id` from
   `running` onward. A row is filed under `task_id` until that arrives and
   renamed onto `call_id` the moment it does -- never `payload.instance`,
   which is absent unless the caller named one. A row that goes straight from
   `pending` to `cancelled` never reached `running`, so it never got a disk
   record and is dropped rather than kept as a dead entry. */
export function applySubagentStatus(rows: readonly TaskRow[], p: SubagentStatusPayload): LiveResult {
  const byCallId = p.call_id ? rows.findIndex((r) => r.kind === 'spawn' && r.id === p.call_id) : -1
  const byTaskId = rows.findIndex((r) => r.kind === 'spawn' && r.id === p.task_id)
  const everRunning = byCallId >= 0
  const at = everRunning ? byCallId : byTaskId

  if (p.status === 'cancelled' && !everRunning) {
    if (at < 0) return { rows: [...rows] }
    return { rows: rows.filter((_, i) => i !== at) }
  }

  const id = p.call_id || p.task_id
  const handle = p.instance ?? p.task_id
  const base = at >= 0 ? rows[at]!.nodes[0] || blankNode(id) : blankNode(id)
  const node: TaskNode = {
    ...base, node_id: id, agent: p.agent, instance: handle, status: p.status,
    started_at: p.started_at ?? base.started_at, ended_at: p.ended_at ?? base.ended_at,
  }
  const row: TaskRow = at >= 0
    ? {
      ...rows[at]!, id, handle, status: deriveStatus([node]),
      started_at: node.started_at ?? rows[at]!.started_at, ended_at: node.ended_at ?? rows[at]!.ended_at,
      nodes: [node], counts: countsOf([node]),
    }
    : {
      id, kind: 'spawn', task_summary: p.label || null, status: deriveStatus([node]),
      started_at: node.started_at ?? Date.now(), ended_at: node.ended_at ?? null,
      agent: p.agent, handle, counts: countsOf([node]), nodes: [node],
    }
  const nextRows = at >= 0 ? rows.map((r, i) => (i === at ? row : r)) : [row, ...rows]
  const terminal = p.status === 'completed' || p.status === 'failed' || p.status === 'cancelled'
  return { rows: nextRows, refetch: terminal ? { kind: 'spawn', id } : undefined }
}
