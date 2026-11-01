/* The tree, built as the session arrives instead of from scratch on every
 * page.
 *
 * The snapshot walk hands the page one page at a time; rebuilding every agent,
 * every turn and every row after each one costs O(pages x rows), which is what
 * a large session feels as a slow first paint. This index keeps the buckets it
 * already built and refreshes only the turns a new page touched.
 *
 * It is the same tree `buildWaterfall` returns -- incremental.test.ts holds the
 * two equal -- so nothing downstream knows which one it is looking at.
 */

import { buildRows, buildTurn, entryEnd, entryStart, foldAgents } from './model'

import type { TrajectoryEntry } from '../types'
import type { Waterfall, WaterfallAgent, WaterfallRow, WaterfallTurn } from './model'

interface TurnBucket {
  id: string
  entries: TrajectoryEntry[]
  dirty: boolean
  cached: WaterfallTurn | null
}

interface AgentBucket {
  turns: Map<string, TurnBucket>
  loose: TrajectoryEntry[]
  looseDirty: boolean
  looseCached: WaterfallRow[] | null
  start: number
  end: number
}

export class WaterfallIndex {
  private readonly agents = new Map<string | null, AgentBucket>()
  private readonly doors = new Map<string, TrajectoryEntry>()
  private now: number

  constructor(now: number) {
    this.now = now
  }

  /** Appends entries in the order the index lists them. */
  add(entries: readonly TrajectoryEntry[]): void {
    for (const entry of entries) {
      const opens = entry.opens_subagent_scope
      if (opens !== null && opens !== undefined && !this.doors.has(opens)) this.doors.set(opens, entry)
      const scope = entry.subagent_scope ?? null
      let agent = this.agents.get(scope)
      if (agent === undefined) {
        agent = { turns: new Map(), loose: [], looseDirty: false, looseCached: null, start: Infinity, end: -Infinity }
        this.agents.set(scope, agent)
      }
      const start = entryStart(entry)
      const end = entryEnd(entry, this.now)
      agent.start = Math.min(agent.start, start)
      agent.end = Math.max(agent.end, end)
      const turnId = entry.turn_span_id ?? null
      if (turnId === null) {
        agent.loose.push(entry)
        agent.looseDirty = true
        continue
      }
      const bucket = agent.turns.get(turnId)
      if (bucket === undefined) agent.turns.set(turnId, { id: turnId, entries: [entry], dirty: true, cached: null })
      else {
        bucket.entries.push(entry)
        bucket.dirty = true
      }
    }
  }

  /** The clock a running row is cut at. Only the turns of rows whose end reads
   *  the clock are re-cut: a row without an end, and a running one, which runs
   *  to the present while its last snapshot is fresh (`entryEnd`). */
  setNow(now: number): void {
    if (now === this.now) return
    this.now = now
    for (const agent of this.agents.values()) {
      let end = -Infinity
      for (const bucket of agent.turns.values()) {
        for (const entry of bucket.entries) {
          if (entry.operation_end === null || entry.operation_end === undefined || entry.operation_status === 'running') bucket.dirty = true
          end = Math.max(end, entryEnd(entry, now))
        }
      }
      for (const entry of agent.loose) end = Math.max(end, entryEnd(entry, now))
      agent.end = Math.max(agent.end, end)
    }
  }

  waterfall(): Waterfall {
    const built: Array<{ agent: WaterfallAgent; start: number }> = []
    for (const [scope, bucket] of this.agents) {
      const turns: WaterfallTurn[] = []
      for (const turn of bucket.turns.values()) {
        if (turn.dirty || turn.cached === null) {
          turn.cached = buildTurn(turn.id, turn.entries, this.now)
          turn.dirty = false
        }
        turns.push(turn.cached)
      }
      turns.sort((a, b) => a.start - b.start || a.id.localeCompare(b.id))
      const numbered = turns.map((turn, at) => ({ ...turn, ordinal: at + 1 }))
      if (bucket.looseDirty || bucket.looseCached === null) {
        bucket.looseCached = buildRows(bucket.loose, this.now)
        bucket.looseDirty = false
      }
      built.push({
        start: bucket.start,
        agent: {
          key: scope === null ? 'main' : `scope:${scope}`,
          scope,
          scopes: [scope],
          door: scope === null ? null : (this.doors.get(scope) ?? null),
          start: bucket.start,
          end: bucket.end,
          turns: numbered,
          loose: bucket.looseCached,
        },
      })
    }
    /* Runs of the same instance are one agent; the fold is the model's rule. */
    return { agents: foldAgents(built.sort((a, b) => a.start - b.start).map((entry) => entry.agent)) }
  }
}
