// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Live Agents: the session's delegated runs (spawns and dag nodes) that are
// pending, running, or recently finished — folded from `subagent.status` and
// `dag.*` turn events, reconciled against `subagent.list` on the boundaries
// events cannot cover (cold start, reconnect, a missed terminal frame).
//
// Deliberately NOT turn-scoped: a background spawn outlives the turn that made
// it, and `$turnState.subagents` is cleared by `idle()` at every turn end. This
// store is what the status bar reads, and what the Agents Overlay merges into
// its live view.
//
// `$dagRuns` is the run-level layer over the same events: one record per
// `run_subagent_dag` graph, holding every node's last known status. It is kept
// beside the rows rather than folded out of them because the rows are pruned
// once they settle -- counts read off a pruned list would shrink back down as a
// finished graph aged out, which is the one thing a finished graph's line must
// not do.

import { atom } from 'nanostores'

import type { DagNodeUpdatedEvent, DagRunCompletedEvent, DagRunStartedEvent, SubagentCall } from '../rpc/generated.js'
import type { SubagentProgress } from '../types.js'

export type LiveAgentStatus = 'cancelled' | 'completed' | 'failed' | 'pending' | 'running' | 'skipped'

export interface LiveAgentRow {
  agent?: string
  /** Spawn rows: the record id `subagent.context` reads. Absent while pending. */
  callId?: string
  endedAtMs?: number
  /** Spawn: the manager's task id (or a list row's record id when seeded from disk); dag: `runId/nodeId`. */
  id: string
  instance?: string
  kind: 'dag-node' | 'spawn'
  label: string
  nodeId?: string
  runId?: string
  /** Insertion order — render order never reshuffles while rows update. */
  seq: number
  /** Local clock, stamped when this client saw the row turn terminal. Retention
   * keys off this rather than the wire's `ended_at`: the wire clock is the
   * server's, and a skewed one would prune a row the moment it finished. */
  settledAtMs?: number
  startedAtMs?: number
  status: LiveAgentStatus
}

const TERMINAL: ReadonlySet<LiveAgentStatus> = new Set(['cancelled', 'completed', 'failed', 'skipped'])

export const isTerminal = (status: LiveAgentStatus): boolean => TERMINAL.has(status)

/** Finished rows linger this long so a just-ended run is still inspectable. */
const TERMINAL_RETENTION_MS = 10 * 60 * 1000
/** A pending row whose events went silent (gateway restart) has no record on
 * disk for the reconcile to disown; age it out instead. */
const PENDING_RETENTION_MS = 30 * 60 * 1000
const MAX_ROWS = 300

export const $liveAgents = atom<LiveAgentRow[]>([])

/**
 * One `run_subagent_dag` run, at graph granularity.
 *
 * Survives its nodes: a graph's line stays on the strip after the graph is
 * over, which is what `nodes` is for -- it is the whole node set as last
 * reported, so the counts a finished run shows are the ones it finished with.
 */
export interface LiveDagRun {
  endedAtMs?: number
  /** Node id -> last known status, for every node the run declared. */
  nodes: Record<string, LiveAgentStatus>
  runId: string
  /** Insertion order, as on `LiveAgentRow`. */
  seq: number
  startedAtMs?: number
  /** The graph's one-line goal (`task_summary`), when the run reported one. */
  summary?: string
}

export const $dagRuns = atom<LiveDagRun[]>([])

/** Runs kept per session. Well past what the strip draws, since the Agents
 *  Overlay reads the same records and a graph is a coarse unit to begin with. */
const MAX_DAG_RUNS = 20

let seqCounter = 0

const nextSeq = (): number => ++seqCounter

export const resetLiveAgents = (): void => {
  seqCounter = 0
  $liveAgents.set([])
  $dagRuns.set([])
}

const prune = (rows: LiveAgentRow[], nowMs: number): LiveAgentRow[] => {
  let out = rows.filter(r => {
    if (isTerminal(r.status)) {
      return nowMs - (r.settledAtMs ?? nowMs) < TERMINAL_RETENTION_MS
    }

    if (r.status === 'pending' && r.startedAtMs === undefined && r.kind === 'spawn') {
      return nowMs - pendingSince(r) < PENDING_RETENTION_MS
    }

    return true
  })

  if (out.length > MAX_ROWS) {
    // Terminal rows are trimmed by chronology, not by insertion order: a
    // snapshot can re-seed a row this cap pruned on the previous poll, and
    // that row draws the freshest seq -- trimming by seq then rotates pruned
    // old rows back in and evicts genuinely newer ones on every
    // reconciliation. The start clock is the one reading both a seeded row
    // and an event-fed row carry, but it is optional (a record whose meta was
    // unreadable has none) and shareable (graph nodes start together), so the
    // final tie-break is the row id -- stable across polls where insertion
    // order is not, and the same tie-break the server's own newest-first list
    // order uses.
    const startedOf = (r: LiveAgentRow) => r.startedAtMs ?? r.endedAtMs ?? 0
    const terminalFirst = [...out].sort(
      (a, b) =>
        Number(isTerminal(a.status)) - Number(isTerminal(b.status)) ||
        startedOf(b) - startedOf(a) ||
        (a.id < b.id ? 1 : a.id > b.id ? -1 : 0)
    )
    out = terminalFirst.slice(0, MAX_ROWS).sort((a, b) => a.seq - b.seq)
  }

  return out
}

// Pending rows carry no timestamp from the wire; remember when we first saw them.
const pendingFirstSeen = new Map<string, number>()

const pendingSince = (row: LiveAgentRow): number => {
  const seen = pendingFirstSeen.get(row.id)

  if (seen !== undefined) {
    return seen
  }

  const now = Date.now()
  pendingFirstSeen.set(row.id, now)

  return now
}

const upsert = (
  rows: LiveAgentRow[],
  row: Omit<LiveAgentRow, 'seq' | 'settledAtMs'>,
  allowDowngrade = false
): LiveAgentRow[] => {
  const settledAtMs = isTerminal(row.status) ? Date.now() : undefined
  const existing = rows.find(r => r.id === row.id)

  if (!existing) {
    return [...rows, { ...row, seq: nextSeq(), settledAtMs }]
  }

  if (!allowDowngrade && isTerminal(existing.status) && !isTerminal(row.status)) {
    return rows
  }

  return rows.map(r =>
    r.id === row.id
      ? {
          ...r,
          ...row,
          callId: row.callId ?? r.callId,
          endedAtMs: row.endedAtMs ?? r.endedAtMs,
          settledAtMs: r.settledAtMs ?? settledAtMs,
          startedAtMs: row.startedAtMs ?? r.startedAtMs
        }
      : r
  )
}

export interface SubagentStatusWire {
  agent: string
  call_id?: string
  ended_at?: number
  instance?: string
  label: string
  started_at?: number
  status: string
  task_id: string
}

export const applySubagentStatus = (p: SubagentStatusWire): void => {
  const status = p.status as LiveAgentStatus
  const now = Date.now()

  let rows = $liveAgents.get()

  // A reconcile may have seeded this run from disk under its record id before
  // its first event arrived; the event's task_id row supersedes that seed.
  if (p.call_id) {
    rows = rows.filter(
      r => !(r.kind === 'spawn' && r.id !== p.task_id && (r.callId === p.call_id || r.id === p.call_id))
    )
  }

  rows = upsert(rows, {
    agent: p.agent,
    callId: p.call_id,
    endedAtMs: p.ended_at,
    id: p.task_id,
    instance: p.instance,
    kind: 'spawn',
    label: p.label,
    startedAtMs: p.started_at,
    status
  })

  if (isTerminal(status)) {
    pendingFirstSeen.delete(p.task_id)
  }

  $liveAgents.set(prune(rows, now))
}

type DagEvent = DagNodeUpdatedEvent | DagRunCompletedEvent | DagRunStartedEvent

const DAG_TO_LIVE: Record<string, LiveAgentStatus> = {
  cancelled: 'cancelled',
  completed: 'completed',
  failed: 'failed',
  pending: 'pending',
  running: 'running',
  skipped: 'skipped'
}

/** Node statuses folded onto a run: what its line counts. */
export interface DagRunCounts {
  done: number
  failed: number
  pending: number
  running: number
  total: number
}

export const dagRunCounts = (run: LiveDagRun): DagRunCounts => {
  const counts = { done: 0, failed: 0, pending: 0, running: 0, total: 0 }

  for (const status of Object.values(run.nodes)) {
    counts.total += 1

    if (status === 'running') {
      counts.running += 1
    } else if (status === 'pending') {
      counts.pending += 1
    } else {
      counts.done += 1

      if (status === 'failed' || status === 'cancelled') {
        counts.failed += 1
      }
    }
  }

  return counts
}

/** A run with no node left to move. `total === 0` is a run we know nothing about
 *  yet, which is not the same as one that is over. */
export const isDagRunSettled = (run: LiveDagRun): boolean => {
  const values = Object.values(run.nodes)

  return values.length > 0 && values.every(isTerminal)
}

const mergeNodeStatus = (
  nodes: Record<string, LiveAgentStatus>,
  node: string,
  status: LiveAgentStatus,
  allowDowngrade = false
): Record<string, LiveAgentStatus> => {
  const existing = nodes[node]

  if (existing !== undefined && !allowDowngrade && isTerminal(existing) && !isTerminal(status)) {
    return nodes
  }

  return { ...nodes, [node]: status }
}

/**
 * Fold one change into a run's record, creating it when `seed` allows.
 *
 * `seed` is false for the paths that only ever *update*: a node frame for a run
 * whose `run_started` this client never saw would otherwise open a record whose
 * node set is however many frames happened to arrive, and a line reading
 * "1 done" for a six-node graph is worse than no line.
 */
const patchDagRun = (runId: string, seed: boolean, mut: (run: LiveDagRun) => LiveDagRun): void => {
  const runs = $dagRuns.get()
  const existing = runs.find(r => r.runId === runId)

  if (!existing && !seed) {
    return
  }

  const base: LiveDagRun = existing ?? { nodes: {}, runId, seq: nextSeq() }
  const next = mut(base)
  // A run whose `run_completed` never arrived still ends here, when its last
  // node does -- the line prints an elapsed time either way.
  const stamped: LiveDagRun =
    isDagRunSettled(next) && next.endedAtMs === undefined ? { ...next, endedAtMs: Date.now() } : next

  const kept = existing ? runs.map(r => (r.runId === runId ? stamped : r)) : [...runs, stamped]

  $dagRuns.set(kept.length > MAX_DAG_RUNS ? kept.slice(kept.length - MAX_DAG_RUNS) : kept)
}

const applyDagRunEvent = (event: DagEvent, now: number): void => {
  if (event.type === 'dag.run_started') {
    const { nodes, run_id, task_summary } = event.payload

    patchDagRun(run_id, true, run => ({
      ...run,
      nodes: nodes.reduce((acc, node) => mergeNodeStatus(acc, node.id, 'pending'), run.nodes),
      startedAtMs: run.startedAtMs ?? now,
      summary: task_summary ?? run.summary
    }))

    return
  }

  if (event.type === 'dag.node_updated') {
    const { node, run_id, status } = event.payload

    // The local clock, not the frame's `started_at`: a run's elapsed time is
    // measured against `Date.now()`, and mixing the server's clock into the
    // start of that subtraction is how skew turns into a wrong duration. The
    // wire timestamps stay on the node rows, which is where they are shown.
    patchDagRun(run_id, false, run => ({
      ...run,
      nodes: mergeNodeStatus(run.nodes, node, DAG_TO_LIVE[status] ?? 'running'),
      startedAtMs: run.startedAtMs ?? now
    }))

    return
  }

  // run_completed: the manifest is authoritative for every node, as it is for
  // the rows.
  patchDagRun(event.payload.run_id, true, run => ({
    ...run,
    endedAtMs: now,
    nodes: event.payload.files.reduce(
      (acc, file) => mergeNodeStatus(acc, file.node, DAG_TO_LIVE[file.status] ?? 'completed', true),
      run.nodes
    ),
    startedAtMs: run.startedAtMs ?? now
  }))
}

export const applyDagEvent = (event: DagEvent): void => {
  const now = Date.now()
  let rows = $liveAgents.get()

  if (event.type === 'dag.run_started') {
    for (const node of event.payload.nodes) {
      rows = upsert(rows, {
        agent: node.subagent,
        id: `${event.payload.run_id}/${node.id}`,
        instance: node.instance ?? undefined,
        kind: 'dag-node',
        label: node.id,
        nodeId: node.id,
        runId: event.payload.run_id,
        status: 'pending'
      })
    }
  } else if (event.type === 'dag.node_updated') {
    const { ended_at, node, run_id, status } = event.payload
    rows = upsert(rows, {
      endedAtMs: ended_at ?? undefined,
      id: `${run_id}/${node}`,
      kind: 'dag-node',
      label: node,
      nodeId: node,
      runId: run_id,
      startedAtMs: event.payload.started_at ?? undefined,
      status: DAG_TO_LIVE[status] ?? 'running'
    })
  } else {
    // run_completed: the manifest is authoritative for every node, including
    // ones whose individual terminal frame this client never saw.
    for (const file of event.payload.files) {
      // The run's end is not the node's: a node that finished minutes earlier
      // keeps the end its own update reported, or its elapsed time grows to the
      // whole run's (and the tree's total to the sum of them).
      const known = rows.find(r => r.id === `${event.payload.run_id}/${file.node}`)?.endedAtMs

      rows = upsert(
        rows,
        {
          endedAtMs: known ?? now,
          id: `${event.payload.run_id}/${file.node}`,
          kind: 'dag-node',
          label: file.node,
          nodeId: file.node,
          runId: event.payload.run_id,
          status: DAG_TO_LIVE[file.status] ?? 'completed'
        },
        true
      )
    }
  }

  applyDagRunEvent(event, now)
  $liveAgents.set(prune(rows, now))
}

const WIRE_TO_LIVE: Record<string, LiveAgentStatus> = {
  cancelled: 'cancelled',
  error: 'failed',
  // Server-inferred for a non-terminal row of a run that died with its
  // gateway; never on the event wire, only in `subagent.list` snapshots.
  interrupted: 'cancelled',
  ok: 'completed',
  queued: 'pending',
  run: 'running',
  skipped: 'skipped'
}

const isoToMs = (iso?: string): number | undefined => {
  if (!iso) {
    return undefined
  }

  const ms = Date.parse(iso)

  return Number.isFinite(ms) ? ms : undefined
}

/**
 * Fold the dag rows of a `subagent.list` snapshot into `$dagRuns`.
 *
 * A run is seeded from disk only when the snapshot still shows it working, on
 * the same terms as a spawn row: a session's whole history of finished graphs
 * would otherwise arrive on the strip the moment the session was opened. A run
 * already on the strip is only *updated*, so its counts survive the reconcile
 * that follows the frame that finished it.
 */
const reconcileDagRuns = (items: SubagentCall[]): void => {
  const byRun = new Map<string, SubagentCall[]>()

  for (const item of items) {
    if (item.kind !== 'dag' || !item.run_id || !item.node) {
      continue
    }

    byRun.set(item.run_id, [...(byRun.get(item.run_id) ?? []), item])
  }

  for (const [runId, nodes] of byRun) {
    const statuses = nodes.map(n => ({ node: n.node!, status: WIRE_TO_LIVE[n.status] }))
    const seed = statuses.some(s => s.status !== undefined && !isTerminal(s.status))
    const reported = new Set(statuses.map(s => s.node))
    // The one place a run's start comes off the wire: a graph this client is
    // meeting for the first time has already been running, and its own first
    // node start is the only reading of when.
    const startedAt = nodes
      .map(n => isoToMs(n.started_at))
      .filter((ms): ms is number => ms !== undefined)
      .sort((a, b) => a - b)[0]

    patchDagRun(runId, seed, run => {
      // A node the snapshot does not name is disowned on the same terms as its
      // row: the graph died with the gateway that was running it. Only ever
      // against a snapshot that named *some* node of this run, so a read that
      // came back holding nothing cannot cancel a graph that is still working.
      let folded = Object.entries(run.nodes).reduce(
        (acc, [node, status]) =>
          reported.has(node) || isTerminal(status) ? acc : mergeNodeStatus(acc, node, 'cancelled', true),
        run.nodes
      )

      folded = statuses.reduce(
        (acc, s) => (s.status === undefined ? acc : mergeNodeStatus(acc, s.node, s.status)),
        folded
      )

      return { ...run, nodes: folded, startedAtMs: run.startedAtMs ?? startedAt }
    })
  }
}

/**
 * Fold a `subagent.list` snapshot in. The disk is authoritative on the
 * boundaries events cannot cover: it seeds runs this client never saw start,
 * settles rows whose terminal frame was missed, and disowns active rows whose
 * record the list no longer reports (a run that died with its gateway).
 *
 * A dag row is disowned on the same terms as a spawn, keyed by its own id
 * (`run_id/node`, which is what the list reports it under). Without that it was
 * unreachable by either path -- this filter skipped it and `prune` ages only a
 * pending spawn -- so switching session while a graph ran left its nodes
 * `running` for the life of the process: a permanent strip row, and a permanent
 * count in the ratio the spawn HUD colours itself by.
 */
export const reconcileFromList = (items: SubagentCall[]): void => {
  const now = Date.now()
  let rows = $liveAgents.get()

  reconcileDagRuns(items)

  const listIds = new Set(items.map(i => i.id))

  rows = rows.filter(r => {
    if (isTerminal(r.status)) {
      return true
    }

    if (r.kind === 'dag-node') {
      return listIds.has(r.id)
    }

    if (r.kind !== 'spawn' || !r.callId) {
      return true
    }

    return listIds.has(r.callId) || listIds.has(r.id)
  })

  // `subagent.list` answers newest first; seeding in that order hands the
  // newest rows the lowest `seq`, and the row-cap prune keeps terminal rows by
  // descending `seq` -- so a resume that crossed the cap kept the oldest rows
  // and dropped the newest. Seed oldest-first, so seq reflects chronology
  // whatever order the wire chooses.
  const ordered = [...items].sort(
    (a, b) => (isoToMs(a.started_at) ?? 0) - (isoToMs(b.started_at) ?? 0) || (a.id < b.id ? -1 : 1)
  )

  for (const item of ordered) {
    const status = WIRE_TO_LIVE[item.status]

    if (!status) {
      continue
    }

    const isDag = item.kind === 'dag'
    const existing = rows.find(r => (isDag ? r.id === item.id : r.id === item.id || r.callId === item.id))

    if (existing) {
      if (isTerminal(status) && !isTerminal(existing.status)) {
        rows = rows.map(r =>
          r.id === existing.id ? { ...r, endedAtMs: isoToMs(item.ended_at) ?? now, settledAtMs: now, status } : r
        )
      }

      continue
    }

    // A run that was over before this client saw it is seeded too: the Agents
    // Overlay reads these rows for the whole delegation record, and after a
    // resume the disk is its only account. The strip stays a live monitor
    // regardless -- a settled row is never drawn there, however it arrived.
    rows = upsert(rows, {
      agent: item.agent,
      callId: isDag ? undefined : item.id,
      endedAtMs: isoToMs(item.ended_at),
      id: item.id,
      instance: item.instance,
      kind: isDag ? 'dag-node' : 'spawn',
      label: item.label,
      nodeId: isDag ? item.node : undefined,
      runId: isDag ? item.run_id : undefined,
      startedAtMs: isoToMs(item.started_at),
      status
    })
  }

  $liveAgents.set(prune(rows, now))
}

export const liveAgentCounts = (rows: LiveAgentRow[]): { pending: number; running: number } => {
  let running = 0
  let pending = 0

  for (const r of rows) {
    if (r.status === 'running') {
      running += 1
    } else if (r.status === 'pending') {
      pending += 1
    }
  }

  return { pending, running }
}

const LIVE_TO_PROGRESS: Record<LiveAgentStatus, SubagentProgress['status']> = {
  cancelled: 'interrupted',
  completed: 'completed',
  failed: 'failed',
  pending: 'queued',
  running: 'running',
  skipped: 'interrupted'
}

/** Shape one row the way the Agents Overlay's tree renderer expects. */
export const toSubagentProgress = (row: LiveAgentRow): SubagentProgress => ({
  depth: 0,
  durationSeconds:
    row.endedAtMs !== undefined && row.startedAtMs !== undefined
      ? Math.max(0, (row.endedAtMs - row.startedAtMs) / 1000)
      : undefined,
  goal:
    row.kind === 'dag-node'
      ? `${row.label}${row.agent ? ` · ${row.agent}` : ''} · dag ${row.runId ?? ''}`
      : `${row.label}${row.agent ? ` · ${row.agent}` : ''}`,
  id: row.id,
  index: row.seq,
  instance: row.agent && row.instance ? { agent: row.agent, handle: row.instance } : undefined,
  liveRef:
    row.kind === 'dag-node'
      ? { kind: 'dag', nodeId: row.nodeId ?? row.label, runId: row.runId ?? '' }
      : { callId: row.callId, kind: 'spawn' },
  notes: [],
  parentId: null,
  startedAt: row.startedAtMs,
  status: LIVE_TO_PROGRESS[row.status],
  taskCount: 1,
  thinking: [],
  toolCount: 0,
  tools: []
})
