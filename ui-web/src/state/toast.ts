/* The transient notices appended to the page's standing #toasts host.
 *
 * The notices are state here -- one entry per notice that is still up, with the
 * host it was raised in -- and <Toasts/> (src/App.tsx) renders them into that
 * host, which the page's own root renders at the body. What
 * is here besides the list is the pair of lifetimes: a plain notice is read in
 * passing, one that offers an action has to outlast reaching for it.
 *
 * `show` commits synchronously, because a notice is the page's answer to
 * something the reader just did and the caller's next statement may be the one
 * that takes the page away.
 */

import { makeStore } from './store'

export interface ToastAction {
  label: string
  fn: () => void
}

export interface Toast {
  readonly id: number
  readonly text: string
  readonly action?: ToastAction
  /** The host it was raised in, so a replaced #toasts cannot move it. */
  readonly host: HTMLElement
}

/** How long a notice stays: long enough to read, longer when it offers a verb. */
export const TOAST_MS = 2600
export const TOAST_ACTION_MS = 5200

const store = makeStore<readonly Toast[]>([])

/** The notices that are still up, oldest first; `set` raises and drops them. */
export const { get, set, subscribe } = store

let made = 0

export function show(text: string, action?: ToastAction): void {
  const host = document.getElementById('toasts')
  if (!host) return
  const id = ++made
  set([...get(), { id, text, action, host }])
  window.setTimeout(() => drop(id), action ? TOAST_ACTION_MS : TOAST_MS)
}

/** Takes one notice down, by id: a lapsed lifetime, or an action taken. */
export function drop(id: number): void {
  if (!get().some((t) => t.id === id)) return
  set(get().filter((t) => t.id !== id))
}

/* The action, then the notice comes down -- the order the button's own handler
   had, so an action that raises a second notice is not dropping that one. */
export function run(id: number): void {
  get().find((t) => t.id === id)?.action?.fn()
  drop(id)
}

/** Test seam only: the notices and the id counter are the module's. */
export function _resetForTests(): void {
  store._resetForTests()
  made = 0
}
