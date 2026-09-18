/* The tasks panel's state, outside React.
 *
 * The page drives this panel imperatively -- a tab click draws it, a session
 * switch resets it, the composer chips select into it -- so the state lives
 * where those callers can reach it and the component subscribes. Same shape as
 * the subagents island it replaces.
 */

import { sources } from '../../state/sources'
import { makeStore } from '../../state/store'

import type { TaskRow, TaskState, TasksSource } from './types'

export interface TasksState {
  rows: TaskRow[]
  /* Which row is expanded. The list is the floating panel and a selection is
     the docked one, so this single field is also which of the two the panel
     is showing. */
  open: string | null
  /* Which node each task's pane is describing, keyed by task id.
     Per pane, not one field: the desk opens a pane per task (`task:<id>`), so a
     single selected node would move every open pane when the reader picked in
     one of them -- with node ids that differ the other pane's card vanished,
     and with ids two tasks share it opened a card nobody asked it for. */
  nodes: Record<string, string>
  /* Pointed at from either side: the composer chips and the list light each
     other up, and neither owns the pointer. */
  hover: string | null
  /* Which tab of the node card is showing. Lives here rather than in the card
     because the desk remounts the pane on every repaint, and because a reader
     who chose a tab keeps it as they move between nodes. */
  tab: 'flow' | 'order'
  /* Whether that choice was the reader's. Until it is, the card decides per
     node -- flow for one that has run, the order for one that has not -- and a
     default that changed under a reader who had picked would undo the pick. */
  tabPinned: boolean
  loaded: boolean
}

const initial: TasksState = {
  rows: [], open: null, nodes: {}, hover: null, tab: 'flow', tabPinned: false, loaded: false,
}

const store = makeStore<TasksState>(initial)

export const { get, set, subscribe, _resetForTests } = store

const patch = (p: Partial<TasksState>): void => { store.set({ ...store.get(), ...p }) }

/* Absence is a state this panel is in, not a wiring mistake, which is why this
   reads `sources.tasks` rather than going through `ds()`: that throws on a
   missing source, correctly, for a surface whose server side exists. There is
   no `tasks.*` method on the contract yet, so the honest answer on a page whose
   gateway has none is an empty list (features/tasks/source.ts). */
export const source = (): TasksSource | null => sources.tasks ?? null

/* A different session is a different set of tasks. Carrying them across would
   attribute one conversation's background work to another, which is the same
   reason the subagents list was reset here. */
export function reset(): void {
  store.set(initial)
}

export const rows = (): TaskRow[] => store.get().rows

export async function refresh(): Promise<void> {
  const src = source()
  /* Unasked, which is not the same as empty. The strip above the composer is in
     the first frame -- App renders synchronously, and the page's wiring runs
     after it -- so its own effect asks before `sources.tasks` is on. Claiming a
     load there answered "no tasks" for the rest of the session, because the
     effect only asks again when `loaded` goes back to false. The wiring asks
     once the seam is on (src/app/install.ts). */
  if (!src || !src.list) return
  const got = await src.list()
  patch({ rows: Array.isArray(got) ? got : [], loaded: true })
}

/* The two groups the panel shows, in the spec's order. A row is finished when
   it is not running -- failure is an ending, and grouping it with the live
   work would put a red dot where the reader looks for progress. */
export const running = (list: TaskRow[] = store.get().rows): TaskRow[] => list.filter((r) => r.state === 'run')
export const settled = (list: TaskRow[] = store.get().rows): TaskRow[] => list.filter((r) => r.state !== 'run')

export const byId = (id: string | null): TaskRow | null =>
  (id && store.get().rows.find((r) => r.id === id)) || null

export const opened = (): TaskRow | null => byId(store.get().open)

/* Which row the list has expanded. The selection inside a task is the pane's
   own and is not touched here: a pane opens on the steps, and the node it
   describes is whichever one the reader picked in that pane. */
export const open = (id: string | null): void => patch({ open: id })

export const close = (): void => patch({ open: null })

/** Which node that task's pane is describing, once the reader has picked one. */
export const nodeOf = (task: string): string | null => store.get().nodes[task] ?? null

/** Picks in one pane, leaving what every other pane is describing where it is. */
export function pickNode(task: string, id: string | null): void {
  const nodes = { ...store.get().nodes }
  if (id) nodes[task] = id
  else delete nodes[task]
  patch({ nodes })
}

/* Only the reader pins a tab. The card reads `tabPinned` to know whether its
   own per-node default still applies. */
export const pickTab = (tab: 'flow' | 'order'): void => patch({ tab, tabPinned: true })

export const hover = (id: string | null): void => patch({ hover: id })

/* Which dot a row and its chip wear. The page's own vocabulary, not a fourth
   one: `run` already pulses amber, `ok` is moss and `bad` is clay, and a status
   colour that means one thing in this list and another two rows above it in the
   same panel is worse than a colour that differs from the mock. One function
   because the list and the composer must never disagree. */
export const dot = (s: TaskState): string => (s === 'run' ? 'run' : s === 'fail' ? 'bad' : 'ok')
