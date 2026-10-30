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
}

export interface WaterfallTurn {
  /** Unique within its agent: the turn span for the host, the run and the
   *  turn span for a worker (see `foldAgents`). */
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
  /** The agent's identity: one per instance, so two runs of the same worker are
   *  one lane rather than two. `agent:<instance>` when the door names one, else
   *  `scope:<scope id>`; `main` for the host. */
  key: string
  /** The first scope this agent ran in; null for the host. */
  scope: string | null
  /** Every scope the agent ran in, in the order they started. */
  scopes: readonly (string | null)[]
  /** The row that opened this worker's scope; the caller names the agent from it. */
  door: TrajectoryEntry | null
  start: number
  end: number
  turns: readonly WaterfallTurn[]
  loose: readonly WaterfallRow[]
}

/** The agent key the collapse set and the DOM use; the host has no scope id. */
export const agentKey = (scope: string | null): string => scope ?? ''

/** A turn is opened by its position in its agent: two agents may name the same
 *  turn span, and they are two different rows. */
export const turnKey = (agent: number, turn: string): string => `${agent}:${turn}`

export interface Waterfall {
  agents: readonly WaterfallAgent[]
}


/** The instant a row starts: its operation's clock, else the event's. */
export function entryStart(entry: TrajectoryEntry): number {
  return stamp(entry.operation_start) ?? stamp(entry.event_time) ?? 0
}

/** The instant a row ends; a row still running ends at `now`. */
export function entryEnd(entry: TrajectoryEntry, now: number): number {
  if (entry.operation_status === 'running' && isLive(entry, now)) return now
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
}

/* Rows under a turn are siblings, whatever the span tree says: the page draws
   one flat list per turn, so a model call and the tool it triggered sit on the
   same line and read in time order. */
function rowOf(entry: TrajectoryEntry, now: number): Row {
  return { entry, start: entryStart(entry), end: entryEnd(entry, now) }
}

/** The rows of one bucket, sorted by start. Exported for the incremental index,
 *  which rebuilds a bucket at a time rather than the whole tree. */
export function buildRows(entries: readonly TrajectoryEntry[], now: number): WaterfallRow[] {
  return ordered(entries.map((entry) => rowOf(entry, now))).map((row) => ({ entry: row.entry }))
}

function ordered(rows: Row[]): Row[] {
  return rows.sort((a, b) => a.start - b.start || a.entry.entry_id.localeCompare(b.entry.entry_id))
}

export function buildTurn(id: string, entries: readonly TrajectoryEntry[], now: number): WaterfallTurn {
  const rows = ordered(entries.map((entry) => rowOf(entry, now)))
  const end = entries.find((entry) => entry.slot === 'turn.output')
  const start = rows[0]!.start
  const stop = Math.max(start, end === undefined ? Math.max(...rows.map((row) => row.end)) : entryEnd(end, now))
  const report = entries.find((entry) => entry.turn_start && entry.dispatched_by_subagent_scope !== null)
  const reportedFrom: string | null = report?.dispatched_by_subagent_scope ?? null
  return {
    id,
    ordinal: 0,
    preview: (rows.find((row) => row.entry.turn_start) ?? rows[0]!).entry.preview ?? null,
    start,
    end: stop,
    reportedFrom,
    failure: rows.some((row) => row.entry.failure_entry),
    rows: rows.map((row) => ({ entry: row.entry })),
  }
}

function scopeOf(scope: string | null, entries: readonly TrajectoryEntry[], now: number, door: TrajectoryEntry | null): WaterfallAgent {
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
  const built = [...turns].map(([id, rows]) => buildTurn(id, rows, now))
  built.sort((a, b) => a.start - b.start || a.id.localeCompare(b.id))
  const numbered = built.map((turn, at) => ({ ...turn, ordinal: at + 1 }))
  const looseRows = buildRows(loose, now)
  const starts = entries.map(entryStart)
  const ends = entries.map((entry) => entryEnd(entry, now))
  return {
    key: scope === null ? 'main' : `scope:${scope}`,
    scope,
    scopes: [scope],
    door,
    start: Math.min(...starts),
    end: Math.max(...ends),
    turns: numbered,
    loose: looseRows,
  }
}

/* What a worker is called as an agent. The instance when the door names one --
   the same instance run twice is one agent, not two. ACP runs record their
   instance on the external span rather than on the run the door is, so the
   agent's own name is the next best identity: every run of `Claude Code-Mirasim`
   is that agent even when the instance id never reaches this page. Only a door
   with neither keeps a lane of its own. */
export function agentIdentity(scope: string | null, door: TrajectoryEntry | null): string {
  if (scope === null) return 'main'
  const instance = door?.meta?.instance
  if (typeof instance === 'string' && instance.trim() !== '') return `agent:${instance.trim()}`
  const agent = door?.meta?.agent
  if (typeof agent === 'string' && agent.trim() !== '') return `agent:${agent.trim()}`
  return `scope:${scope}`
}

/** One agent per identity, its runs' turns in one sequence, numbered once. */
export function foldAgents(parts: readonly WaterfallAgent[]): WaterfallAgent[] {
  const groups = new Map<string, WaterfallAgent[]>()
  for (const part of parts) {
    const key = agentIdentity(part.scope, part.door)
    const list = groups.get(key)
    if (list) list.push(part)
    else groups.set(key, [part])
  }
  const agents: WaterfallAgent[] = []
  for (const [key, group] of groups) {
    group.sort((a, b) => a.start - b.start || (a.scope ?? '').localeCompare(b.scope ?? ''))
    /* A worker's steps carry the turn span that dispatched them, so two runs
       sent from one turn name the same span. The run is part of a worker
       turn's identity; without it the folded agent lists two turns under one
       id, which is one React key for two rows and one open state for both. */
    const turns = group
      .flatMap((part) => (part.scope === null ? part.turns : part.turns.map((turn) => ({ ...turn, id: `${part.scope}/${turn.id}` }))))
      .sort((a, b) => a.start - b.start || a.id.localeCompare(b.id))
    agents.push({
      key,
      scope: group[0]!.scope,
      scopes: group.map((part) => part.scope),
      door: group.find((part) => part.door !== null)?.door ?? null,
      start: Math.min(...group.map((part) => part.start)),
      end: Math.max(...group.map((part) => part.end)),
      turns: turns.map((turn, at) => ({ ...turn, ordinal: at + 1 })),
      loose: group.flatMap((part) => part.loose).sort((a, b) => entryStart(a.entry) - entryStart(b.entry)),
    })
  }
  agents.sort((a, b) => {
    if (a.scope === null) return b.scope === null ? 0 : -1
    if (b.scope === null) return 1
    return a.start - b.start || a.key.localeCompare(b.key)
  })
  return agents
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
  return { agents: foldAgents([...groups].map(([scope, rows]) => scopeOf(scope, rows, now, scope === null ? null : (doors.get(scope) ?? null)))) }
}

/** Whether a row is still running: the index says so, or no end was recorded
 *  -- and either way recent enough that the session can plausibly still be
 *  writing it. A running span carries its snapshot's end, which is when it was
 *  last heard from; a historical span that never wrote an end, or one whose
 *  snapshots stopped, must not keep the page's clock ticking forever. */
export const LIVE_WINDOW_MS = 30 * 60_000

export function isLive(entry: TrajectoryEntry, now: number): boolean {
  if (entry.operation_status === 'running') {
    return now - (stamp(entry.operation_end) ?? entryStart(entry)) <= LIVE_WINDOW_MS
  }
  if (entry.operation_end !== null && entry.operation_end !== undefined) return false
  return now - entryStart(entry) <= LIVE_WINDOW_MS
}

/** When an agent was doing something: its turns and its loose rows, merged
 *  where they touch. Their total is the agent's busy time, which is what its
 *  row reports -- a worker alive for a day that worked ten minutes of it
 *  reads ten minutes. */
export function busyRuns(agent: WaterfallAgent, now: number): Array<{ start: number; end: number }> {
  const spans = [
    ...agent.turns.map((turn) => ({ start: turn.start, end: turn.end })),
    ...agent.loose.map((row) => ({ start: entryStart(row.entry), end: entryEnd(row.entry, now) })),
  ].sort((a, b) => a.start - b.start)
  const merged: Array<{ start: number; end: number }> = []
  for (const span of spans) {
    const last = merged.at(-1)
    if (last !== undefined && span.start <= last.end) last.end = Math.max(last.end, span.end)
    else merged.push({ ...span })
  }
  return merged
}
