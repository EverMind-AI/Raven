/* Where the up pill lands: the question the reader was reading before the
 * content at the top of the scrollport. A turn the top edge is already inside
 * counts as the answer above it, not as the question being looked for. */

export function previousUserTop(scroll: HTMLElement): number | null {
  const box = scroll.getBoundingClientRect()
  const turns = scroll.querySelectorAll<HTMLElement>('.turn.me')
  for (let i = turns.length - 1; i >= 0; i -= 1) {
    const el = turns[i] as HTMLElement
    const top = el.getBoundingClientRect().top
    if (top < box.top - 1) return Math.max(0, Math.round(scroll.scrollTop + top - box.top))
  }
  return null
}
