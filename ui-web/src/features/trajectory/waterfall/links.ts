/* The two cross-agent rules: a dispatch (the host spawning a worker) and a
 * report (a worker's result opening a turn on its host).
 *
 * A worker's spans live in their own trace, so `parent_span_id` never reaches
 * the row that spawned it; the relationship is carried by
 * `opens_subagent_scope` on the spawn and `dispatched_by_subagent_scope` on the
 * turn a report started. This module turns those into row-to-row rules the
 * page can draw as one dashed line each.
 */

import { entryStart } from './model'

import type { DisplayRow } from './Waterfall'
import type { Waterfall } from './model'

export interface Link {
  /** The real instant the rule stands at. */
  at: number
  fromRow: number
  toRow: number
  kind: 'dispatch' | 'report'
}

export function crossLinks(rows: readonly DisplayRow[], waterfall: Waterfall): Link[] {
  const at = new Map<string, number>()
  rows.forEach((row, index) => {
    if (row.kind === 'span' && row.row !== null) at.set(`entry:${row.row.entry.entry_id}`, index)
    if (row.kind === 'agent') at.set(`agent:${row.agent}`, index)
    if (row.kind === 'turn' && row.turn !== null) at.set(`turn:${row.turn}`, index)
  })
  const links: Link[] = []
  waterfall.agents.forEach((agent, index) => {
    if (agent.scope === null) return
    const toRow = at.get(`agent:${index}`)
    if (toRow === undefined) return
    const door = agent.door
    if (door === null) return
    /* A folded turn hides the spawning row; the rule then starts at the turn
       that holds it, so the worker still says where it came from. */
    const fromRow = at.get(`entry:${door.entry_id}`)
      ?? (door.turn_span_id === null || door.turn_span_id === undefined ? undefined : at.get(`turn:${door.turn_span_id}`))
    if (fromRow !== undefined) links.push({ at: entryStart(door), fromRow, toRow, kind: 'dispatch' })
  })
  rows.forEach((row, index) => {
    if (row.kind !== 'turn' || row.turnRow === null || row.turnRow.reportedFrom === null) return
    const child = waterfall.agents.findIndex((agent) => agent.scope === row.turnRow!.reportedFrom)
    if (child < 0) return
    const childAgent = waterfall.agents[child]!
    const fromRow = at.get(`agent:${child}`)
    if (fromRow === undefined) return
    links.push({ at: childAgent.end, fromRow, toRow: index, kind: 'report' })
  })
  return links
}
