/* Taking the boot splash down.
 *
 * #splash is markup, not a component, and stays that way: src/page.html paints
 * it as the literal first frame, before this bundle has been evaluated, so
 * nothing React does could be what puts it on screen. All the page has to do is
 * lift it at the right moment, which is this. A floor on its display time keeps
 * a fast boot from flashing it for two frames.
 *
 * Was hideSplash in legacy/demo/160-boot.js, where it sat beside the demo
 * shell's own boot steps; two of its three callers are the page's boot sequence
 * and the connection surface. The clock it measures from is marked by whoever
 * put the splash up (the demo shell's install, today); before that mark a call
 * lifts it at once, which is what an unset clock did there too.
 */

let started = 0

/** When the splash went up, for the floor below to measure from. */
export function markStart(): void {
  started = Date.now()
}

export function hideSplash(minMs?: number): void {
  const s = document.getElementById('splash')
  if (!s) return
  const wait = Math.max(0, (minMs == null ? 600 : minMs) - (Date.now() - started))
  setTimeout(() => {
    s.dataset.off = '1'
    setTimeout(() => s.remove(), 560)
  }, wait)
}
