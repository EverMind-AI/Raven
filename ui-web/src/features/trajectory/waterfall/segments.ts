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

/* One bar per turn and clock: the list re-renders on every scroll frame, and a
   turn whose rows and clock have not moved has the same bar it had. Keyed by the
   turn object, which the model rebuilds whenever its rows change. */
const painted = new WeakMap<WaterfallTurn, { now: number; segments: Segment[] }>()

export function turnSegments(turn: WaterfallTurn, now: number): Segment[] {
  const held = painted.get(turn)
  if (held !== undefined && held.now === now) return held.segments
  const segments = sweep(turn, now)
  painted.set(turn, { now, segments })
  return segments
}

/* Every row's start and end as an edge, in time order, and one open count per
   kind: each stretch between two edges is painted by the highest-priority kind
   open across it. Sorting the edges is the whole cost, so a turn of k rows is
   k log k rather than a scan of every row for every stretch. */
function sweep(turn: WaterfallTurn, now: number): Segment[] {
  if (turn.end <= turn.start) return []
  const edges: Array<{ at: number; delta: number; rank: number }> = []
  for (const row of turn.rows) {
    const kind = segmentOf(row)
    if (kind === 'input' || kind === 'waiting') continue
    const from = Math.max(turn.start, entryStart(row.entry))
    const to = Math.min(turn.end, entryEnd(row.entry, now))
    if (to <= from) continue
    const rank = PRIORITY.indexOf(kind)
    edges.push({ at: from, delta: 1, rank }, { at: to, delta: -1, rank })
  }
  edges.sort((a, b) => a.at - b.at)
  const open = PRIORITY.map(() => 0)
  const segments: Segment[] = []
  let from = turn.start
  const paint = (to: number): void => {
    if (to <= from) return
    const rank = open.findIndex((count) => count > 0)
    const kind = rank < 0 ? 'waiting' : PRIORITY[rank]!
    const last = segments.at(-1)
    if (last && last.kind === kind && last.end === from) last.end = to
    else segments.push({ start: from, end: to, kind })
    from = to
  }
  for (let i = 0; i < edges.length; ) {
    const at = edges[i]!.at
    paint(at)
    for (; i < edges.length && edges[i]!.at === at; i++) open[edges[i]!.rank]! += edges[i]!.delta
  }
  paint(turn.end)
  return segments
}
