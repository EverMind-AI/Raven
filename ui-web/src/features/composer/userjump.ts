/* Where the up pill lands: the question the reader was reading before the
 * content at the top of the scrollport. A turn the top edge is already inside
 * counts as the answer above it, not as the question being looked for.
 *
 * The top edge that counts is the header's lower edge, not the scroller's:
 * the header floats over the transcript (page.css, .top), so a question
 * between the two is hidden behind the frost and has not been reached. The
 * jump lands it just under the band for the same reason. */

import { headerBand } from '../../chrome/behaviour/headerBand'

export function previousUserTop(scroll: HTMLElement): number | null {
  const box = scroll.getBoundingClientRect()
  const band = headerBand()
  const turns = scroll.querySelectorAll<HTMLElement>('.turn.me')
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    const el = turns[i] as HTMLElement
    const top = el.getBoundingClientRect().top
    if (top < box.top + band - 1) return Math.max(0, Math.round(scroll.scrollTop + top - box.top - band))
  }
  return null
}
