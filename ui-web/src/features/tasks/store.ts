/* The tasks panel's state, outside React.
 *
 * The page drives this panel imperatively -- a tab click draws it, a session
 * switch resets it, the composer strip and the desk light each other up --
 * so the state lives where those callers can reach it and the component
 * subscribes. Rows are addressed by (kind, id): a spawn's id and a dag's run
 * id share no namespace and can theoretically collide.
 */

import { current as sessionCurrent } from '../../lib/session'
import { sources } from '../../state/sources'
import { makeStore } from '../../state/store'
import * as live from './live'

import type { TaskKind, TaskRow, TasksSource } from './types'

export interface TasksState {
  rows: TaskRow[]
  loaded: boolean
  /* Which node each task's pane is showing, keyed by the pane's own id
     (`task:<kind>:<id>`). Per pane, not one field: the desk opens a pane per
     task, so a single selected-node field would move every open pane when
     the reader picked in one of them. */
  nodes: Record<string, string>
  /* Pointed at from either side: the composer strip and the list light each
     other up, and neither owns the pointer. */
  hover: string | null
  tab: 'context' | 'order'
  tabPinned: boolean
}

const initial: TasksState = {
  rows: [], loaded: false, nodes: {}, hover: null, tab: 'context', tabPinned: false,
}

const store = makeStore<TasksState>(initial)

export const { get, set, subscribe, _resetForTests } = store

const patch = (p: Partial<TasksState>): void => { store.set({ ...store.get(), ...p }) }

export const source = (): TasksSource | null => sources.tasks ?? null

/* A different conversation is a different set of tasks. Carrying them across
   would attribute one conversation's background work to another. */
export function reset(): void {
  store.set(initial)
}

export const rows = (): TaskRow[] => store.get().rows

export const rowKey = (row: TaskRow): string => `${row.kind}:${row.id}`

export const byKey = (kind: TaskKind, id: string): TaskRow | null =>
  store.get().rows.find((r) => r.kind === kind && r.id === id) || null

export async function refresh(): Promise<void> {
  const src = source()
  const key = sessionCurrent()
  /* No source or no open conversation reads as no tasks, not as an error: the
     strip above the composer is in the first frame, before the wiring runs,
     and a draft has no session for a task to be filed under. */
  if (!src || !key) { patch({ rows: [], loaded: true }); return }
  const got = await src.list(key)
  patch({ rows: Array.isArray(got) ? got : [], loaded: true })
}

/* The two groups the panel shows. A row is finished when it is not running --
   failure is an ending, and grouping it with the live work would put a red
   dot where the reader looks for progress. */
export const running = (list: TaskRow[] = store.get().rows): TaskRow[] => list.filter((r) => r.status === 'running')
export const settled = (list: TaskRow[] = store.get().rows): TaskRow[] => list.filter((r) => r.status !== 'running')

/** Which node that task's pane is describing, once the reader has picked one. */
export const nodeOf = (paneId: string): string | null => store.get().nodes[paneId] ?? null

/** Picks in one pane, leaving what every other pane is describing where it is. */
export function pickNode(paneId: string, id: string | null): void {
  const nodes = { ...store.get().nodes }
  if (id) nodes[paneId] = id
  else delete nodes[paneId]
  patch({ nodes })
}

/* Only the reader pins a tab; the card decides per node until they do
   (context for one that has run, the work order for one that has not). */
export const pickTab = (tab: 'context' | 'order'): void => patch({ tab, tabPinned: true })

export const hover = (id: string | null): void => patch({ hover: id })

/* The server's own read for one row, folded back over whatever a live event
   already guessed. Used after every terminal live event and after a stop: a
   frame carries no tokens, no files and no final error text, and a stop's own
   answer is a bare `found` flag. */
async function reconcile(kind: TaskKind, id: string): Promise<void> {
  const src = source()
  if (!src) return
  const row = await src.one(kind, id).catch(() => null)
  if (!row) return
  const now = store.get().rows
  const at = now.findIndex((r) => r.kind === kind && r.id === id)
  patch({ rows: at >= 0 ? now.map((r, i) => (i === at ? row : r)) : [row, ...now] })
}

function apply(next: live.LiveResult): void {
  patch({ rows: next.rows })
  if (next.refetch) void reconcile(next.refetch.kind, next.refetch.id)
}

/* One consumer per live event the contract names (§5.1), called from
   state/session/stages.ts beside the transcript's own calls for the same
   frames. Each wraps its reducer in `live.ts` -- pure there, applied here. */
export function onRunStarted(p: live.RunStartedPayload): void {
  patch({ rows: live.applyRunStarted(store.get().rows, p) })
}
export function onNodeUpdated(p: live.NodeUpdatedPayload): void {
  patch({ rows: live.applyNodeUpdated(store.get().rows, p) })
}
export function onRunCompleted(p: live.RunCompletedPayload): void {
  apply(live.applyRunCompleted(store.get().rows, p))
}
export function onRunReplanned(p: live.RunReplannedPayload): void {
  apply(live.applyRunReplanned(store.get().rows, p))
}
export function onSubagentStatus(p: live.SubagentStatusPayload): void {
  apply(live.applySubagentStatus(store.get().rows, p))
}

/** Stop a running task -- `subagent.interrupt` for a dag, `subagent.cancel_instance`
    for a spawn, dispatched by `row.kind` -- then reconcile it. */
export async function stop(row: TaskRow): Promise<void> {
  const src = source()
  if (!src) return
  await src.stop(row)
  await reconcile(row.kind, row.id)
}
