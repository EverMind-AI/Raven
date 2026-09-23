/* Keeping a selection that starts in the transcript inside the transcript.
 *
 * Selecting a whole line there -- a paragraph select, or a word select on the
 * last line -- used to run past the end of the transcript and into the
 * composer, because a paragraph selection ends at the next selectable position
 * in document order and the last transcript line is the last text in the scroll
 * region. Every empty box on the way (the seam, the wordmark, the dock, the
 * queue strip, the field) then got a selection rect of its own, which reads as
 * a blue band over blank space above the input, and the copy picked up their
 * line breaks. `user-select: none` on the composer is not the fix -- it makes
 * the search skip further rather than stop, so the bands grow -- and
 * `user-select: contain`, which would be, is not implemented in Chromium. So
 * clamp instead: a selection that starts inside the transcript ends inside it.
 *
 * Idempotent by construction, which is what keeps it from looping on the
 * selectionchange it causes.
 */

/* The last text node with anything in it: an empty one would give the selection
   an end the reader cannot see. */
function lastTextNodeIn(root: Element): Text | null {
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (node) =>
      (node as Text).data && (node as Text).data.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT,
  })
  let last: Text | null = null
  for (let node = walk.nextNode(); node; node = walk.nextNode()) last = node as Text
  return last
}

/** The selection as it stands, pulled back to the transcript's last text. */
export function clamp(): void {
  const scroll = document.getElementById('scroll')
  const selection = document.getSelection()
  if (!scroll || !selection || !selection.rangeCount || selection.isCollapsed) return
  const range = selection.getRangeAt(0)
  if (!scroll.contains(range.startContainer) || scroll.contains(range.endContainer)) return
  const tail = lastTextNodeIn(scroll)
  if (!tail) return
  const clamped = range.cloneRange()
  /* A range the document has moved on from cannot be re-ended; leaving the
     selection as it is beats throwing at the reader. */
  try { clamped.setEnd(tail, tail.data.length) } catch { return }
  selection.removeAllRanges()
  selection.addRange(clamped)
}
