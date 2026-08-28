/* One-path stroke icons, as SVG elements and as a component.
 *
 * Ten lines and no state, which is why it sits here rather than in whichever
 * module happened to need it first: the sheets, the workspace rows and the
 * agent panels all draw the same chevrons and crosses, and building them by
 * hand in each place is how two of them end up 1.8px apart.
 *
 * The concat copy left after its final caller migrated was deleted rather than
 * published from here: a publish would be another name to retire later.
 */

import type { JSX } from 'react'

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

/* The paths this bundle draws so far, named so a reader can tell which glyph a
   call means without decoding the coordinates. */
export const CROSS = 'M7 7l10 10M17 7 7 17'
export const CHEVRON_DOWN = 'M6.5 10 12 15.5 17.5 10'
export const SEND = 'M5 12h13M12 5l7 7-7 7'

/* The send arrow, which is not drawn like the others: it sits inside a 30px
   filled disc, so it is set small and heavy rather than at this file's 24px
   hairline weight. Both composers wear it -- the page's own and a sub-agent's
   -- and they used to build it apart, so the sub-agent's came out 20px at 1.8
   against the page's 14px at 2.4: the same arrow, visibly a different button.
   The size and the weight live here with the path, and
   `composer/store.ts::ICON_SEND` builds its string from the same three
   numbers (SEND_PX / SEND_STROKE), with a test holding the two renderings
   attribute for attribute. */
export const SEND_PX = 14
export const SEND_STROKE = 2.4

export function SendGlyph(): JSX.Element {
  return (
    <svg width={SEND_PX} height={SEND_PX} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth={SEND_STROKE} aria-hidden="true">
      <path d={SEND} />
    </svg>
  )
}

/* The same glyph for a React caller, attribute for attribute. Two of them
   because the page has two kinds of caller, not because they are two glyphs:
   whatever `ico()` builds imperatively, this renders declaratively, and a
   difference between them would show up as an icon 1.8px off its twin.

   The transcript keeps a local copy of this (`Ico` in TranscriptPage.tsx). It
   goes when the dag card and the dag sheet become one renderer -- that change
   touches both files, and moving it before then would be a rename in a file
   nobody needs to open. */
export function Glyph({ d, cls }: { d: string; cls?: string }): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
      aria-hidden="true" {...(cls ? { className: cls } : {})}>
      <path d={d} />
    </svg>
  )
}
