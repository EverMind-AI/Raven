/** Hands an upward overscroll back to the page.
 *
 * A nested scroller inside the transcript carries `overscroll-behavior:
 * contain`, and that is right at its BOTTOM edge: reading a stream to its end
 * should not then carry the whole conversation along with it. At the TOP edge
 * it has nothing to protect and takes something away -- the box is a third of
 * the window tall and the full width of the column, so a reader whose cursor is
 * anywhere over it and who scrolls back up gets nothing at all once the box
 * itself is at the top. The page stays where it is and the surface reads as
 * frozen.
 *
 * CSS cannot say "contain downwards only", and toggling the property from a
 * handler does not work either: the browser reads it when the gesture starts,
 * so a value written mid-gesture applies to the next one, by which time the
 * reader has already seen the page refuse to move. So the containment stays and
 * this hands the upward remainder to the page by hand.
 *
 * By hand means in pixels, which `deltaY` is not always in.
 */

/* A line, in pixels, when the wheel reports lines instead. The same 16 the
   playbook canvas uses: two readings of one device must not disagree. */
export const WHEEL_LINE_PX = 16

/* `deltaY` is in the unit `deltaMode` names, and only mode 0 is pixels. A mouse
   reporting lines sends about -3 for one notch, and a page-mode device sends
   -1; assigning either straight onto `scrollTop` moves the page a few pixels
   while the box goes on eating the rest of the gesture, which is the bug this
   file exists to fix, just quieter.

   A page is the scroller's own visible height -- that is what a page means to
   the thing being scrolled. `fallback` when it has none to report. */
export function pixelDelta(deltaY: number, deltaMode: number, pageHeight: number): number {
  if (deltaMode === 1) return deltaY * WHEEL_LINE_PX
  if (deltaMode === 2) return deltaY * (pageHeight || WHEEL_LINE_PX)
  return deltaY
}

/* The scroller the page itself uses. Walked for rather than named so the same
   card works wherever it is mounted -- the transcript's own `#scroll`, and the
   workspace pane that renders the same conversation. */
export function pageScroller(from: HTMLElement): HTMLElement | null {
  let el = from.parentElement
  while (el) {
    const style = el.ownerDocument.defaultView?.getComputedStyle(el)
    const y = style ? style.overflowY : ''
    if ((y === 'auto' || y === 'scroll') && el.scrollHeight > el.clientHeight) return el
    el = el.parentElement
  }
  return null
}

/* Whether this wheel is the one the box can no longer answer. `deltaY < 0` is
   upwards; `scrollTop <= 0` is the box already at its own top. Both, or the box
   is still the right target and nothing should be forwarded. */
export function isUpwardOverscroll(box: { scrollTop: number }, deltaY: number): boolean {
  return deltaY < 0 && box.scrollTop <= 0
}

/* Returns whether the page was moved, which is what a test can assert without a
   layout: happy-dom lays nothing out, so a scroll POSITION there is only ever
   the number this wrote. */
export function releaseUpward(box: HTMLElement, deltaY: number, deltaMode = 0): boolean {
  if (!isUpwardOverscroll(box, deltaY)) return false
  const page = pageScroller(box)
  if (!page) return false
  page.scrollTop += pixelDelta(deltaY, deltaMode, page.clientHeight)
  return true
}
