/* The figures the waterfall's header prints, and the second line under each.
 *
 * Worked out from the tree the rows are drawn from, so a figure and the rows
 * it summarises cannot disagree. Calls are counted by span of a trace -- span
 * ids are unique only within their trace -- because one model call writes its
 * input, its thinking and its output as three entries of one span.
 * A share of time is read from the turn bars' own sweep (`segments.ts`), so the
 * header's "of busy time" and a turn's own breakdown are the same arithmetic.
 */

import { entryStart, spanKey } from './model'
import { turnSegments } from './segments'

import type { TrajectoryEntry } from '../types'
import type { Waterfall } from './model'

export interface Figures {
  start: number
  end: number
  /** The main agent, and every worker it sent: an external agent's run is a sub-agent too. */
  agents: { main: number; sub: number }
  turns: number
  medianTurn: number | null
  llm: number
  /** The model's share of every turn's time, or null when the turns took none. */
  modelShare: number | null
  tool: number
  /** The two tools called most, each with its share of the tool calls. */
  topTools: Array<{ name: string; share: number }>
  errors: number
  /** The earliest failure, the one the header's jump lands on. */
  firstError: string | null
}

function median(values: number[]): number | null {
  if (!values.length) return null
  const sorted = [...values].sort((a, b) => a - b)
  const middle = sorted.length >> 1
  return sorted.length % 2 ? sorted[middle]! : (sorted[middle - 1]! + sorted[middle]!) / 2
}

export function summarize(waterfall: Waterfall, shown: readonly TrajectoryEntry[], now: number): Figures {
  const agents = { main: 0, sub: 0 }
  const lengths: number[] = []
  let turnTime = 0
  let modelTime = 0
  let start = Infinity
  let end = -Infinity
  for (const agent of waterfall.agents) {
    if (agent.scope === null) agents.main += 1
    else agents.sub += 1
    start = Math.min(start, agent.start)
    end = Math.max(end, agent.end)
    for (const turn of agent.turns) {
      lengths.push(turn.end - turn.start)
      turnTime += turn.end - turn.start
      for (const segment of turnSegments(turn, now)) {
        if (segment.kind === 'model') modelTime += segment.end - segment.start
      }
    }
  }
  const llmSpans = new Set<string>()
  const toolNames = new Map<string, string | null>()
  let errors = 0
  let firstError: TrajectoryEntry | null = null
  for (const entry of shown) {
    if (entry.kind.startsWith('llm.')) llmSpans.add(spanKey(entry))
    else if (entry.kind.startsWith('tool.')) {
      const key = spanKey(entry)
      const name = typeof entry.meta?.tool === 'string' && entry.meta.tool !== '' ? entry.meta.tool : null
      if (!toolNames.has(key) || toolNames.get(key) === null) toolNames.set(key, name)
    }
    if (!entry.failure_entry) continue
    errors += 1
    if (firstError === null || entryStart(entry) < entryStart(firstError)) firstError = entry
  }
  const perTool = new Map<string, number>()
  for (const name of toolNames.values()) if (name !== null) perTool.set(name, (perTool.get(name) ?? 0) + 1)
  const topTools = [...perTool]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .slice(0, 2)
    .map(([name, calls]) => ({ name, share: calls / toolNames.size }))
  return {
    start: Number.isFinite(start) ? start : 0,
    end: Number.isFinite(end) ? end : 0,
    agents,
    turns: lengths.length,
    medianTurn: median(lengths),
    llm: llmSpans.size,
    modelShare: turnTime > 0 ? Math.min(1, modelTime / turnTime) : null,
    tool: toolNames.size,
    topTools,
    errors,
    firstError: firstError?.entry_id ?? null,
  }
}
