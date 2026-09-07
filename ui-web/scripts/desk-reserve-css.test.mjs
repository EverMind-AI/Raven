/* The chat's right inset while the anchored desk is over it.
 *
 * The panel is `position: fixed`, so nothing about it reaches the layout: the
 * transcript column is centred by `margin: 0 auto` in the whole chat and the
 * panel lands on the top band of it. Measured on the running page in a 900px
 * window with the default 300px panel, as the intersection of the two rects:
 * nothing at 1920px, 13x330 at 1600px, 93x330 at 1440px, 173x330 at 1280px.
 *
 * `--desk-reserve` is what closes that, and the reason it is pinned here rather
 * than in a DOM test is the same reason the layer order is: this is a fact about
 * paint and layout, and the property is set on `document.documentElement` by one
 * component and spent by the stylesheet on two elements that are not its
 * children. A DOM test can see the variable arrive; only the stylesheet says
 * where it goes, and dropping either rule would put the panel back on the text
 * with every unit test still green.
 *
 * The fallback is the other half. `deskReserve` answers 0 by REMOVING the
 * property -- for a detached panel, for a chat too narrow to spare the width --
 * so a rule without `, 0px` would resolve to an invalid value and drop the
 * declaration, taking the composer's own 22px with it.
 */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('../src/styles/page.css', import.meta.url), 'utf8')

function rule(selector) {
  const rules = css.replace(/\/\*[\s\S]*?\*\//g, ' ')
  for (const match of rules.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (match[1].trim() === selector) return match[2]
  }
  return null
}

describe('the chat makes room for the anchored desk', () => {
  it('insets the scroller by the reserve, and by nothing without one', () => {
    const body = rule('.chat:not([data-fresh]) .scroll')
    expect(body).not.toBeNull()
    expect(body).toMatch(/padding-right:\s*var\(--desk-reserve,\s*0px\)/)
  })

  it('insets the composer by the reserve on top of its own 22px', () => {
    /* Its own inset is not the desk's to spend: `.dock { padding: 0 22px }` is
       what lines the composer card up with the prose edge, and a rule here that
       replaced it rather than adding to it would move the card every time the
       desk opened. */
    const body = rule('.chat:not([data-fresh]) .dock')
    expect(body).not.toBeNull()
    expect(body).toMatch(/padding-right:\s*calc\(22px \+ var\(--desk-reserve,\s*0px\)\)/)
  })

  it('moves the two together, so the composer stays under the column', () => {
    /* The panel does not reach the composer -- it ends far above it. The
       composer moves because it is centred UNDER the column, and a column that
       slides while the composer does not reads as a layout fault. */
    const body = rule('.chat:not([data-fresh]) .scroll, .chat:not([data-fresh]) .dock')
    expect(body).not.toBeNull()
    expect(body).toMatch(/transition:\s*padding-right/)
  })

  it('does not animate the inset for a reader who asked for no motion', () => {
    /* A 162px slide of everything they are reading is exactly the motion that
       setting is for. */
    const reduced = css.match(/@media \(prefers-reduced-motion: reduce\) \{[^}]*\.chat:not\(\[data-fresh\]\) \.scroll[^}]*\}/)
    expect(reduced).not.toBeNull()
    expect(reduced[0]).toMatch(/transition:\s*none/)
  })

  it('leaves the landing page alone, which has no transcript to keep off', () => {
    /* The inset there moved the composer out from under the wordmark and the
       crows, which are placed by rules of their own -- three centres on one
       screen. A plain `.chat .scroll` would bring that back. */
    expect(rule('.chat .scroll')).toBeNull()
    expect(rule('.chat .dock')).toBeNull()
    expect(rule('.chat:not([data-fresh]) .scroll')).not.toBeNull()
  })
})
