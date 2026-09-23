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
 * component and spent by the stylesheet on elements that are not its children.
 * A DOM test can see the variable arrive; only the stylesheet says where it
 * goes, and dropping any of these rules would put the panel back on the text
 * with every unit test still green.
 *
 * The fallback is the other half. `deskReserve` answers 0 by REMOVING the
 * property -- for a detached panel, for a chat too narrow to spare the width --
 * so a rule without `, 0px` would resolve to an invalid value and drop the
 * declaration, taking the composer's own 36px with it.
 *
 * Where the inset is spent moved once. It used to be one `padding-right` on
 * div.scroll, which is the scroll container: animating a padding there re-lays
 * the whole transcript out on every frame, and the artefact was a turn left
 * painted at its old offset under the new one. So the scroller's own box is
 * fixed now and its three columns take the inset themselves -- a cap and a
 * slide on the two that centre, a cap alone on the one that does not -- which
 * is the same geometry frame for frame and composites instead of relaying out.
 * That equivalence is arithmetic, so it is asserted as the pair of
 * declarations rather than as a pixel: `min(cap, 100% - R)` with
 * `translate: -R/2` is "centred in what is left, narrowing once R eats the cap".
 */

import { describe, expect, it } from 'vitest'

import { css, rule } from './css.mjs'

describe('the chat makes room for the anchored desk', () => {
  it('insets the scroller\'s two centred columns by the reserve', () => {
    /* Both cap and slide, or the column either overlaps the panel or slides
       off the scroller's left edge. */
    const body = rule('.chat:not([data-fresh]) .scroll > .col, .chat:not([data-fresh]) .scroll > .flash')
    expect(body).not.toBeNull()
    expect(body).toMatch(/max-width:\s*min\(calc\(var\(--col\) \+ 72px\),\s*calc\(100% - var\(--desk-reserve,\s*0px\)\)\)/)
    expect(body).toMatch(/translate:\s*calc\(var\(--desk-reserve,\s*0px\) \/ -2\)\s*0/)
  })

  it('caps the banner host without sliding it, since it is not centred', () => {
    /* A slide here would move the card twice: the host is full width and
       left-aligned, so narrowing it re-centres the card inside on its own. */
    const body = rule('.chat:not([data-fresh]) .scroll > #bannerHost')
    expect(body).not.toBeNull()
    expect(body).toMatch(/max-width:\s*calc\(100% - var\(--desk-reserve,\s*0px\)\)/)
    expect(body).not.toMatch(/translate/)
  })

  it('leaves the scroll container itself alone, so nothing relays out per frame', () => {
    expect(rule('.chat:not([data-fresh]) .scroll')).toBeNull()
  })

  it('insets the composer by the reserve on top of its own 36px', () => {
    /* Its own inset is not the desk's to spend: `.dock { padding: 0 36px }` is
       what lines the composer card up with the prose edge, and a rule here that
       replaced it rather than adding to it would move the card every time the
       desk opened. */
    const body = rule('.chat:not([data-fresh]) .dock')
    expect(body).not.toBeNull()
    expect(body).toMatch(/padding-right:\s*calc\(36px \+ var\(--desk-reserve,\s*0px\)\)/)
  })

  it('centres the back-to-bottom pill on the column rather than on the chat', () => {
    /* The pill is a child of div.chat, so a plain `left: 50%` is the chat's
       centre -- half the reserve to the right of the text it belongs to. */
    const body = rule('.backpill')
    expect(body).not.toBeNull()
    expect(body).toMatch(/left:\s*calc\(50% - var\(--desk-reserve,\s*0px\) \/ 2\)/)
  })

  it('moves them together, so the composer stays under the column', () => {
    /* The panel does not reach the composer -- it ends far above it. The
       composer moves because it is centred UNDER the column, and a column that
       slides while the composer does not reads as a layout fault. */
    const body = rule('.chat:not([data-fresh]) .scroll > *, .chat:not([data-fresh]) .dock')
    expect(body).not.toBeNull()
    const list = /transition:([^;]*)/.exec(body)
    expect(list).not.toBeNull()
    for (const prop of ['translate', 'max-width', 'padding-right']) {
      expect(list[1]).toContain(prop)
    }
  })

  it('does not animate the inset for a reader who asked for no motion', () => {
    /* A 162px slide of everything they are reading is exactly the motion that
       setting is for. */
    const reduced = css.match(/@media \(prefers-reduced-motion: reduce\) \{[^}]*\.chat:not\(\[data-fresh\]\) \.scroll > \*[^}]*\}/)
    expect(reduced).not.toBeNull()
    expect(reduced[0]).toMatch(/transition:\s*none/)
  })

  it('leaves the landing page alone, which has no transcript to keep off', () => {
    /* The inset there moved the composer out from under the wordmark and the
       crows, which are placed by rules of their own -- three centres on one
       screen. A plain `.chat .scroll > *` or `.chat .dock` would bring it back. */
    expect(rule('.chat .scroll > *')).toBeNull()
    expect(rule('.chat .dock')).toBeNull()
    expect(rule('.chat:not([data-fresh]) .scroll > *, .chat:not([data-fresh]) .dock')).not.toBeNull()
  })
})
