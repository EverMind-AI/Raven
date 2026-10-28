/* A turn's bar is a composite of what happened inside it, not a sum: the
 * children's intervals are swept once and each stretch is painted by priority,
 * so a collapsed turn still says whether the model, a tool or a worker held the
 * time. The priority is the reference implementation's: a failure outranks the
 * handoff it happened in, a worker outranks the dispatcher's own model call,
 * and everything outranks the waiting nobody claimed. */

import { entryEnd, entryStart } from './model'

import type { WaterfallRow, WaterfallTurn } from './model'

export type ActivityKind = 'failure' | 'subagent' | 'model' | 'tool' | 'waiting' | 'input'

export interface Segment {
  start: number
  end: number
  kind: ActivityKind
}

const PRIORITY: readonly ActivityKind[] = ['failure', 'subagent', 'model', 'tool', 'waiting']

/** What a row contributes to a bar. A span that opens a scope is a handoff,
 *  whatever it is called: it is the moment another agent took over. */
export function segmentOf(row: WaterfallRow): ActivityKind {
  const entry = row.entry
  if (entry.failure_entry || entry.operation_status === 'error') return 'failure'
  if (entry.opens_subagent_scope !== null && entry.opens_subagent_scope !== undefined) return 'subagent'
  if (entry.kind === 'user.input') return 'input'
  if (entry.kind.startsWith('llm.')) return 'model'
  if (entry.kind.startsWith('tool.')) return 'tool'
  return 'waiting'
}

export function turnSegments(turn: WaterfallTurn, now: number): Segment[] {
  if (turn.end <= turn.start) return []
  const parts: Segment[] = []
  for (const row of turn.rows) {
    const kind = segmentOf(row)
    if (kind === 'input' || kind === 'waiting') continue
    const from = Math.max(turn.start, entryStart(row.entry))
    const to = Math.min(turn.end, entryEnd(row.entry, now))
    if (to > from) parts.push({ start: from, end: to, kind })
  }
  const points = [...new Set([turn.start, turn.end, ...parts.flatMap((part) => [part.start, part.end])])].sort((a, b) => a - b)
  const starts = [...parts].sort((a, b) => a.start - b.start)
  const segments: Segment[] = []
  for (let at = 0; at < points.length - 1; at++) {
    const from = points[at]!
    const to = points[at + 1]!
    const active = new Set<ActivityKind>()
    for (const part of starts) {
      if (part.start > from) break
      if (part.end > from) active.add(part.kind)
    }
    const kind = PRIORITY.find((candidate) => active.has(candidate)) ?? 'waiting'
    const last = segments.at(-1)
    if (last && last.kind === kind && last.end === from) last.end = to
    else segments.push({ start: from, end: to, kind })
  }
  return segments
}
