/* One-path stroke icons, as SVG elements.
 *
 * Ten lines and no state, which is why it sits here rather than in whichever
 * module happened to need it first: the sheets, the workspace rows and the
 * agent panels all draw the same chevrons and crosses, and building them by
 * hand in each place is how two of them end up 1.8px apart.
 *
 * The concat layers keep their own copy (demo/100-workspace.js) for the four
 * call sites still out there. That copy goes when the last of them does -- it is
 * not published from here, because a publish is a name to retire later and this
 * one has no reason to cross the boundary.
 */

const NS = 'http://www.w3.org/2000/svg'

export function ico(d: string, cls?: string): SVGSVGElement {
  const s = document.createElementNS(NS, 'svg')
  s.setAttribute('viewBox', '0 0 24 24')
  s.setAttribute('fill', 'none')
  s.setAttribute('stroke', 'currentColor')
  s.setAttribute('stroke-width', '1.8')
  s.setAttribute('aria-hidden', 'true')
  if (cls) s.setAttribute('class', cls)
  const p = document.createElementNS(NS, 'path')
  p.setAttribute('d', d)
  s.appendChild(p)
  return s
}

/* The two paths this bundle draws so far, named so a reader can tell which
   glyph a call means without decoding the coordinates. */
export const CROSS = 'M7 7l10 10M17 7 7 17'
export const CHEVRON_DOWN = 'M6.5 10 12 15.5 17.5 10'
