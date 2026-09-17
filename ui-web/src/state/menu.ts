/* The one context menu shared by rows and action buttons across the page.
 *
 * What is here is the menu's state -- which host it was raised in, and the rows
 * it was raised with -- plus the two things that are not rendering: the flag and
 * the position on the host element, and the pointerdown that closes it from
 * anywhere on the document. The rows themselves are rendered by <ContextMenu/>
 * (src/App.tsx) into div#menu, which that root renders as a standing host at the
 * end of stage C.
 *
 * `show` still commits synchronously, because the position is measured from the
 * menu AFTER its rows are in it: the clamps below keep the last row and the
 * right edge inside the viewport, and a menu measured before it had rows would
 * be placed as if it were empty.
 *
 * The host is the element found when the menu was raised, and the row that is
 * picked closes THAT element -- not whatever #menu answers with later. A test
 * pins the difference, and the reason it is worth pinning is that the menu
 * outlives the host in exactly one case: a page region redrawn under an open
 * menu leaves the rows the reader is pointing at in a detached host, and
 * closing the new one instead would leave those rows on screen.
 */

import { makeStore } from './store'

export interface MenuItem {
  label: string
  fn: () => void
  bad?: boolean
}

export interface MenuState {
  /** The host the menu was last raised in, or null while it never has been. */
  readonly host: HTMLElement | null
  readonly items: readonly (MenuItem | '-')[]
}

const host = (): HTMLElement | null => document.getElementById('menu')

const store = makeStore<MenuState>({ host: null, items: [] })

/** The rows to render, and where; `set` is what raises them. */
export const { get, set, subscribe, _resetForTests } = store

export function close(): void {
  const menu = host()
  if (menu) menu.dataset.open = 'false'
}

export function show(x: number, y: number, items: Array<MenuItem | '-'>): void {
  const menu = host()
  if (!menu) return
  set({ host: menu, items })
  menu.dataset.open = 'true'
  const rect = menu.getBoundingClientRect()
  menu.style.left = `${Math.min(x, window.innerWidth - rect.width - 8)}px`
  menu.style.top = `${Math.min(y, window.innerHeight - rect.height - 8)}px`
}

/* A picked row: the menu it was raised in comes down, then the action runs.
   The rows are left in the markup, the way the old handler left them -- the
   next menu replaces them, and nothing reads them while the flag is false. */
export function pick(item: MenuItem): void {
  const raised = get().host
  if (raised) raised.dataset.open = 'false'
  item.fn()
}

/* A pointer outside the menu closes it. Registered in the capture phase with
   the page's other document listeners (state/globalListeners.ts): the row the
   pointer landed on may stop the event, and the menu still has to come down. */
export function onPointerDown(event: PointerEvent): void {
  const target = event.target as Element | null
  if (!target?.closest('#menu')) close()
}
