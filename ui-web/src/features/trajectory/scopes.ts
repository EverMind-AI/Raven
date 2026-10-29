/* The reader's moves into a Sub-agent Scope, and what a sub-agent run is called.
 *
 * A move touches both stores -- the list's scope and selection, and the pane's
 * first block on an external agent's run -- and the pane's store already
 * follows the list's, so the moves live here, beside both, rather than in
 * either. The surfaces call these; the stores keep the rules. A run's name is
 * worked out here too, so the waterfall, its overview and the pane name a
 * sub-agent the same way.
 */

import { t } from '../../i18n/t'
import * as details from './detailStore'
import * as list from './store'

import type { TrajectoryState } from './store'
import type { JsonValue, TrajectoryEntry } from './types'

/** The blocks an external agent's entry opens on, best first: what the CLI agent said, else the ACP frames. */
export const EXTERNAL_BLOCKS = ['transcript', 'frames']

/* The scope a row leads to: the scope an entry opens, or the scope whose
   report started a turn -- on the turn's first row only, which is the row
   that carries the mark; the turn's end carries the same scope and leads nowhere. */
export function openTarget(entry: TrajectoryEntry): string | null {
  const opens = list.opensOf(entry)
  if (opens !== null) return opens
  return entry.turn_start ? list.reportedFromOf(entry) : null
}

/* Goes to a Sub-agent Scope. One entered for the first time on an external
   agent's run lands on that agent's entry with the pane open on what the
   agent recorded, since that entry is all such a run has to show. */
export function enter(scopeId: string): void {
  if (!list.openScope(scopeId)) return
  const external = list.get().visible.find((e) => e.span_name === 'subagent.external')
  if (!external) return
  details.prefer(external.entry_id, EXTERNAL_BLOCKS)
  list.select(external.entry_id, { source: 'link' })
}

/** Goes to the scope a row leads to; false for a row that leads nowhere. */
export function openFromEntry(entry: TrajectoryEntry): boolean {
  const target = openTarget(entry)
  if (target === null) return false
  enter(target)
  return true
}

/* ── what a run is called ─────────────────────────────────────────────── */

/** The entry that opens the scope now, else the one it was opened from. */
export function scopeDoor(s: TrajectoryState, scopeId: string): TrajectoryEntry | null {
  const door = s.doors[scopeId]
  const at = door === undefined ? undefined : s.index[door]
  if (at !== undefined) return s.entries[at] ?? null
  return s.scopes.find((o) => o.id === scopeId)?.door ?? null
}

const text = (value: JsonValue | undefined): string | null => {
  if (typeof value === 'string') return value.trim() ? value : null
  return typeof value === 'number' ? String(value) : null
}

/* The agent and the task when both are known, else whichever names the run:
   the task, its id, the graph tool that ran it, or the bare word. */
export function scopeTitle(door: TrajectoryEntry | null): string {
  const meta = door?.meta ?? {}
  const agent = text(meta.agent)
  const label = text(meta.label)
  if (agent && label) return `${agent} \u00b7 ${label}`
  return label ?? text(meta.task_id) ?? text(meta.tool) ?? t('gui.trajectory.scope.untitled')
}

/* ── where the focus goes ─────────────────────────────────────────────── */

/* A move may take away the element that had the focus -- the pane's button
   that made it, say. A store write commits synchronously, so once a move
   returns its target is on the page, and the focus is handed over right there
   rather than left for some later render to pick up. */

/** Focuses the rows' scroller, when it has rows to hold the focus. */
export function focusList(): void {
  document.querySelector<HTMLElement>('.trajectory-list')?.focus()
}
