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

let seqCounter = 0

const nextSeq = (): number => ++seqCounter

export const resetLiveAgents = (): void => {
  seqCounter = 0
  $liveAgents.set([])
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
    const terminalFirst = [...out].sort(
      (a, b) => Number(isTerminal(a.status)) - Number(isTerminal(b.status)) || b.seq - a.seq
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
      rows = upsert(
        rows,
        {
          endedAtMs: now,
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

  $liveAgents.set(prune(rows, now))
}

const WIRE_TO_LIVE: Record<string, LiveAgentStatus> = {
  cancelled: 'cancelled',
  error: 'failed',
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

  for (const item of items) {
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

    // Nothing to show for a run that is already over and was never on screen.
    if (isTerminal(status)) {
      continue
    }

    rows = upsert(rows, {
      agent: item.agent,
      callId: isDag ? undefined : item.id,
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
