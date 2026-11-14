/* The search dialog's own two facts: whether it is showing, and what was typed.
 *
 * Module state rather than a component's, because three things outside any
 * component write it: the rail's magnifier, the page's shortcut handler and the
 * dialog's own field. The field is uncontrolled and the redraw is React's
 * subscription, so nothing here touches the DOM.
 *
 * The term is lowercased and trimmed at write time, so every reader compares
 * against the same shape.
 */

import { makeStore } from './store'

export interface FindState {
  /** Whether the dialog is showing. Served closed. */
  readonly open: boolean
  /** What the dialog filters by. */
  readonly query: string
}

const store = makeStore<FindState>({ open: false, query: '' })

export const { get, subscribe, _resetForTests } = store

/** A patch. A write that changes nothing notifies nobody. */
export function set(next: Partial<FindState>): void {
  const now = get()
  const merged = { ...now, ...next }
  if (merged.open === now.open && merged.query === now.query) return
  store.set(merged)
}

/* Search is a chore, so it hides until asked for, and a dialog that comes back
   with the last term still in it is a dialog the reader has to clear first. */
export function toggle(force?: boolean): void {
  if (force ?? !get().open) {
    set({ open: true })
    return
  }
  set({ open: false, query: '' })
}
