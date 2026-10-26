/* The reader's moves between Scope Tabs, and what a tab is called.
 *
 * A move touches both stores -- the list's tab and selection, and the pane's
 * first block on an external agent's tab -- and the pane's store already
 * follows the list's, so the moves live here, beside both, rather than in
 * either. The surfaces call these; the stores keep the rules. The words a
 * tab shows are worked out here too, so the strip, the list and the pane
 * name a sub-agent the same way.
 */

import { t } from '../../i18n/t'
import { formatDuration } from '../../lib/duration'
import { statusKey } from './blocks'
import * as details from './detailStore'
import * as list from './store'

import type { TrajectoryState } from './store'
import type { JsonValue, TrajectoryEntry } from './types'

/** The blocks an external agent's entry opens on, best first: what the CLI agent said, else the ACP frames. */
export const EXTERNAL_BLOCKS = ['transcript', 'frames']

/* The tab a row leads to: the scope an entry opens, or the scope whose
   report started a turn -- on the turn's first row only, which is the row
   the list marks; the turn's end carries the same scope and leads nowhere. */
export function openTarget(entry: TrajectoryEntry): string | null {
  const opens = list.opensOf(entry)
  if (opens !== null) return opens
  return entry.turn_start ? list.reportedFromOf(entry) : null
}

/* Goes to a sub-agent's tab. A tab opened just now on an external agent's
   run lands on that agent's entry with the pane open on what the agent
   recorded, since that entry is all such a run has to show. */
export function enter(scopeId: string): void {
  if (!list.openScope(scopeId)) return
  const external = list.get().visible.find((e) => e.span_name === 'subagent.external')
  if (!external) return
  details.prefer(external.entry_id, EXTERNAL_BLOCKS)
  list.select(external.entry_id, { source: 'link' })
}

/** Opens the tab a row leads to; false for a row that leads nowhere. */
export function openFromEntry(entry: TrajectoryEntry): boolean {
  const target = openTarget(entry)
  if (target === null) return false
  enter(target)
  return true
}

export function close(scopeId: string): void {
  list.closeScope(scopeId)
}

/** Back to the main session, on the entry that opens the scope; false when the index no longer has it. */
export function locate(scopeId: string): boolean {
  const door = list.get().doors[scopeId]
  if (door === undefined) return false
  list.select(door, { source: 'link' })
  return true
}

/* ── what a tab is called ─────────────────────────────────────────────── */

/** How a scope's run stands, or `gone` when the index holds neither its door nor any of its steps. */
export type ScopeState = TrajectoryEntry['operation_status'] | 'gone'

/** The entry that opens the scope now, else the one it was opened from. */
export function scopeDoor(s: TrajectoryState, scopeId: string): TrajectoryEntry | null {
  const door = s.doors[scopeId]
  const at = door === undefined ? undefined : s.index[door]
  if (at !== undefined) return s.entries[at] ?? null
  return s.scopes.find((o) => o.id === scopeId)?.door ?? null
}

/* The door the index holds now, for how the run stands: the one the tab was
   opened from may say `running` long after the run ended. */
export function scopeState(s: TrajectoryState, scopeId: string): ScopeState {
  const door = s.doors[scopeId]
  const at = door === undefined ? undefined : s.index[door]
  const entry = at === undefined ? undefined : s.entries[at]
  if (entry) return entry.operation_status
  return (s.scopeSizes[scopeId] ?? 0) > 0 ? 'unknown' : 'gone'
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

/** A run's state in the catalogue's words. */
export const stateWord = (state: ScopeState): string => {
  if (state === 'gone') return t('gui.trajectory.scope.gone')
  const key = statusKey(state)
  return key ? t(key) : state
}

/** Everything the strip says about a run on hover, one fact a line; what is not known is left out. */
export function scopeTooltip(door: TrajectoryEntry | null, state: ScopeState): string {
  const meta = door?.meta ?? {}
  const agent = text(meta.agent)
  const instance = text(meta.instance)
  const lines = [
    text(meta.label),
    agent ? t('gui.trajectory.scope.agent', { name: agent }) : null,
    instance ? t('gui.trajectory.scope.instance', { name: instance }) : null,
    typeof door?.turn_number === 'number' ? t('gui.trajectory.scope.dispatched_in', { n: door.turn_number }) : null,
    stateWord(state),
    typeof door?.duration_ms === 'number' ? formatDuration(door.duration_ms) : null,
  ]
  return lines.filter((line): line is string => line !== null).join('\n')
}

/** The states a tab marks: running, failed and cancelled. A run that ended well, or whose end is unknown, carries none. */
export const scopeMark = (state: ScopeState): 'running' | 'error' | 'cancelled' | null =>
  state === 'running' || state === 'error' || state === 'cancelled' ? state : null

/* ── where the focus goes ─────────────────────────────────────────────── */

export const scopeTabId = (at: number): string => `trajectory-scope-tab-${at}`

/* A move may take away the element that had the focus -- a closed tab's
   button, a pane's button, a list replaced by its empty state. A store write
   commits synchronously, so once a move returns its target is on the page,
   and the focus is handed over right there rather than left for some later
   render to pick up. */

/** Focuses the tab now on screen in the strip, or the list when there is no strip. */
export function focusTab(): void {
  const s = list.get()
  const at = s.scope === null ? 0 : s.scopes.findIndex((o) => o.id === s.scope) + 1
  const tab = document.getElementById(scopeTabId(at))
  if (tab) tab.focus()
  else focusList()
}

/** Focuses the list, when it has rows to hold the focus. */
export function focusList(): void {
  document.querySelector<HTMLElement>('.trajectory-list')?.focus()
}
