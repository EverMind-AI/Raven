/* The one slot a tab coming back to the front is spent on.
 *
 * `state/globalListeners.ts` owns every listener the page holds on the
 * document and the window, in one declared order, and it may not import an
 * island. An island that wants to hear the page become visible again -- to
 * read a live value it stopped polling while nobody was looking -- fills this
 * slot from the page's own wiring (src/app/install.ts) instead, the way
 * `state/page.ts`'s show slots are filled. One function rather than a set,
 * because a listener set is `makeStore`'s to keep (store-shape); a second
 * island wanting the same signal composes with the first in the installer.
 */

let handler: (() => void) | null = null

/** Registers what the page spends on becoming visible or focused again. */
export function onVisibleAgain(fn: () => void): void {
  handler = fn
}

/** Spent by the two listeners `globalListeners.ts` declares for it. */
export function fire(): void {
  if (handler) handler()
}

export function _resetForTests(): void {
  handler = null
}
