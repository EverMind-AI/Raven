/* The page's shape: agents, the turns inside each, and the rows inside each
 * turn.
 *
 * An agent is a lifetime, not a turn: a sub-agent keeps running after the turn
 * that dispatched it has ended, and the report that ends it arrives as a new
 * turn on its dispatcher. So the host's turns and a worker's turns are two
 * separate sequences, and the wire's `turn_number` -- one ordinal over every
 * `session.turn` in the loaded set -- is not the number this page prints.
 * Turns are numbered here, per agent.
 *
 * Rows that belong to no turn (a `memory.store` that outlived its turn, a
 * `plugin.load` between turns) are real work with real times, and live on the
 * agent as `loose` rather than being forced into a neighbouring turn.
 */

import type { TrajectoryEntry } from '../types'

export interface WaterfallRow {
  entry: TrajectoryEntry
  depth: number
}

export interface WaterfallTurn {
  id: string
  ordinal: number
  preview: string | null
  start: number
  end: number
  reportedFrom: string | null
  failure: boolean
  rows: readonly WaterfallRow[]
}

export interface WaterfallAgent {
  scope: string | null
  /** The row that opened this worker's scope; the caller names the agent from it. */
  door: TrajectoryEntry | null
  start: number
  end: number
  turns: readonly WaterfallTurn[]
  loose: readonly WaterfallRow[]
}

export interface Waterfall {
  agents: readonly WaterfallAgent[]
}

const DEPTH_LIMIT = 32

/** The instant a row starts: its operation's clock, else the event's. */
export function entryStart(entry: TrajectoryEntry): number {
  return stamp(entry.operation_start) ?? stamp(entry.event_time) ?? 0
}

/** The instant a row ends; a row still running ends at `now`. */
export function entryEnd(entry: TrajectoryEntry, now: number): number {
  return stamp(entry.operation_end) ?? now
}

function stamp(value: string | null | undefined): number | null {
  if (!value) return null
  const ms = Date.parse(value)
  return Number.isFinite(ms) ? ms : null
}

interface Row {
  entry: TrajectoryEntry
  start: number
  end: number
  depth: number
}

function rowOf(entry: TrajectoryEntry, bySpan: ReadonlyMap<string, string | null>, now: number): Row {
  let depth = 0
  let parent: string | null = entry.parent_span_id ?? null
  const seen = new Set<string>([entry.span_id])
  while (parent !== null && depth < DEPTH_LIMIT && bySpan.has(parent) && !seen.has(parent)) {
    seen.add(parent)
    depth += 1
    parent = bySpan.get(parent) ?? null
  }
  return { entry, start: entryStart(entry), end: entryEnd(entry, now), depth }
}

function ordered(rows: Row[]): Row[] {
  return rows.sort((a, b) => a.start - b.start || a.entry.entry_id.localeCompare(b.entry.entry_id))
}

function turnOf(id: string, entries: readonly TrajectoryEntry[], now: number): WaterfallTurn {
  const bySpan = new Map<string, string | null>()
  for (const entry of entries) if (!bySpan.has(entry.span_id)) bySpan.set(entry.span_id, entry.parent_span_id ?? null)
  const rows = ordered(entries.map((entry) => rowOf(entry, bySpan, now)))
  const end = entries.find((entry) => entry.slot === 'turn.output')
  const start = rows[0]!.start
  const stop = Math.max(start, end === undefined ? Math.max(...rows.map((row) => row.end)) : entryEnd(end, now))
  const report = entries.find((entry) => entry.turn_start && entry.dispatched_by_subagent_scope !== null)
  const reportedFrom: string | null = report?.dispatched_by_subagent_scope ?? null
  return {
    id,
    ordinal: 0,
    preview: rows[0]!.entry.preview ?? null,
    start,
    end: stop,
    reportedFrom,
    failure: rows.some((row) => row.entry.failure_entry),
    rows: rows.map((row) => ({ entry: row.entry, depth: row.depth })),
  }
}

function agentOf(scope: string | null, entries: readonly TrajectoryEntry[], now: number, door: TrajectoryEntry | null): WaterfallAgent {
  const turns = new Map<string, TrajectoryEntry[]>()
  const loose: TrajectoryEntry[] = []
  for (const entry of entries) {
    const turnId = entry.turn_span_id ?? null
    if (turnId === null) {
      loose.push(entry)
      continue
    }
    const bucket = turns.get(turnId)
    if (bucket) bucket.push(entry)
    else turns.set(turnId, [entry])
  }
  const built = [...turns].map(([id, rows]) => turnOf(id, rows, now))
  built.sort((a, b) => a.start - b.start || a.id.localeCompare(b.id))
  const numbered = built.map((turn, at) => ({ ...turn, ordinal: at + 1 }))
  const bySpan = new Map<string, string | null>()
  for (const entry of loose) if (!bySpan.has(entry.span_id)) bySpan.set(entry.span_id, entry.parent_span_id ?? null)
  const looseRows = ordered(loose.map((entry) => rowOf(entry, bySpan, now))).map((row) => ({ entry: row.entry, depth: row.depth }))
  const starts = entries.map(entryStart)
  const ends = entries.map((entry) => entryEnd(entry, now))
  return {
    scope,
    door,
    start: Math.min(...starts),
    end: Math.max(...ends),
    turns: numbered,
    loose: looseRows,
  }
}

export function buildWaterfall(entries: readonly TrajectoryEntry[], now: number): Waterfall {
  if (!entries.length) return { agents: [] }
  const groups = new Map<string | null, TrajectoryEntry[]>()
  for (const entry of entries) {
    const key = entry.subagent_scope ?? null
    const bucket = groups.get(key)
    if (bucket) bucket.push(entry)
    else groups.set(key, [entry])
  }
  /* The door belongs to the dispatcher's group, not the worker's, so it is
     found across every entry rather than inside the agent. */
  const doors = new Map<string, TrajectoryEntry>()
  for (const entry of entries) {
    const opens = entry.opens_subagent_scope
    if (opens !== null && opens !== undefined && !doors.has(opens)) doors.set(opens, entry)
  }
  const agents = [...groups].map(([scope, rows]) => agentOf(scope, rows, now, scope === null ? null : (doors.get(scope) ?? null)))
  agents.sort((a, b) => {
    if (a.scope === null) return b.scope === null ? 0 : -1
    if (b.scope === null) return 1
    return a.start - b.start || a.scope.localeCompare(b.scope)
  })
  return { agents }
}
