/* The tasks panel's state, outside React.
 *
 * The page drives this panel imperatively -- a tab click draws it, a session
 * switch resets it, the composer chips select into it -- so the state lives
 * where those callers can reach it and the component subscribes. Same shape as
 * the subagents island it replaces.
 */

import { makeStore } from '../../state/store'
import { sources } from '../../state/sources'

import type { TaskRow, TaskState, TasksSource } from './types'

export interface TasksState {
  rows: TaskRow[]
  /* Which row is expanded. The list is the floating panel and a selection is
     the docked one, so this single field is also which of the two the panel
     is showing. */
  open: string | null
  /* Which node of the open task the detail panel is describing. */
  node: string | null
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
  rows: [], open: null, node: null, hover: null, tab: 'flow', tabPinned: false, loaded: false,
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
  if (!src || !src.list) {
    patch({ rows: [], loaded: true })
    return
  }
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

export function open(id: string | null): void {
  const task = byId(id)
  /* The first node is selected with the task, so the detail panel never opens
     onto an empty right half. */
  patch({ open: id, node: task && task.nodes.length ? (task.nodes[0] as { id: string }).id : null })
}

export const close = (): void => patch({ open: null, node: null })

export const pickNode = (id: string | null): void => patch({ node: id })

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
