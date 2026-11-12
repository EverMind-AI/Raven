/* The More group of the rail: the row that opens it, and whether it stands open.
 *
 * A store rather than a writer: <MoreFly/> (src/chrome/MoreFly.tsx) renders the
 * row and the popover from it, and the popover's own `data-open` is written by
 * that component from the flag here.
 *
 * The flag is the whole of it. What the group holds is a row that opens a
 * file, not a page, so there is nothing to mark and no page to answer for --
 * unlike the rows that open a module, which the page registry owns.
 *
 * `state/` may not reach into `features/` at runtime (the import-direction
 * gate), so the one fact this group would want from a domain -- whether a
 * package is on its way -- is asked of the domain by the component that draws
 * the popover, which is the direction the gate allows.
 */

import { makeStore } from './store'

export interface NavFlyState {
  /** Whether the group stands open. Served shut. */
  readonly open: boolean
}

const store = makeStore<NavFlyState>({ open: false })

export const { get, subscribe } = store

export function set(next: Partial<NavFlyState>): void {
  const now = get()
  if (next.open === now.open) return
  store.set({ ...now, ...next })
}

export function open(): void {
  set({ open: true })
}

export function close(): void {
  set({ open: false })
}

export const isOpen = (): boolean => get().open

export function toggle(): void {
  if (isOpen()) close()
  else open()
}

/* Test seam only: the group outlives a case's DOM. */
export function _resetForTests(): void {
  store._resetForTests()
}
