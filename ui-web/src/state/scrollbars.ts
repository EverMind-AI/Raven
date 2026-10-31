/* The one verb a view has for the overlay bars: ask for a scroller's bar now,
 * rather than waiting for the reader to scroll it.
 *
 * The behaviour lives in chrome/behaviour/scrollbars.ts, and a feature may not
 * import it -- the import gate reads that as an upward edge. What a view asks
 * for is registered instead: src/app/install.ts puts the module on the slot at
 * boot, and a slot nobody filled is a view that asks nothing (a test's case,
 * the boot goldens' first frame).
 */

type ShowBar = (el: HTMLElement) => void

let show: ShowBar | null = null

/** Fills the slot with the module that draws the bars. src/app/install.ts. */
export function onShowBar(fn: ShowBar): void {
  show = fn
}

/** Asks for this scroller's bar now, at the size its content has today. */
export function showBar(el: HTMLElement): void {
  if (show) show(el)
}

/* Test seam only: the slot belongs to the boot. */
export function _resetForTests(): void {
  show = null
}
