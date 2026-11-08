/* Where the up pill lands: the question the reader was reading before the
 * content at the top of the scrollport. A turn the top edge is already inside
 * counts as the answer above it, not as the question being looked for.
 *
 * The top edge that counts is the title bar's lower edge, not the
 * scroller's: the header floats over the transcript and its frost is the row
 * itself (page.css, .chat > .top), so a question under that row is hidden
 * behind the frost and has not been reached. The jump lands it a lead under
 * that edge for the same reason. */

import { HEADER_LEAD, headerBand } from '../../lib/dom'

export function previousUserTop(scroll: HTMLElement): number | null {
  const box = scroll.getBoundingClientRect()
  const band = headerBand(scroll)
  const turns = scroll.querySelectorAll<HTMLElement>('.turn.me')
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    const el = turns[i] as HTMLElement
    const top = el.getBoundingClientRect().top
    if (top < box.top + band - 1) return Math.max(0, Math.round(scroll.scrollTop + top - box.top - band - HEADER_LEAD))
  }
  return null
}
