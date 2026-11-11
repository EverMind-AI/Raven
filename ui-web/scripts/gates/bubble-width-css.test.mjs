/* Where a reader-side bubble stops: at the turn it sits in, read off the
 * stylesheet. Both are shrink-wrapped flex items of a column, so their
 * max-content width is what wins -- a delivered body's markdown table, or a
 * runtime-opened brief's code block, is wider than the turn, and without a cap
 * the bubble keeps that width and overflows the turn to the left, where the
 * scroll container cuts the start off every line. happy-dom lays nothing out,
 * so no component test can see this, and a rule that repeats the selector
 * later in the sheet wins the cascade without touching the cap above it; both
 * are why the assertions below go through the sheet's own parser and pin one
 * rule each.
 */

import { describe, expect, it } from 'vitest'

import { decls, rules } from './css.mjs'

describe('the reader-side bubbles', () => {
  it("caps the reader's bubble at the turn it sits in", () => {
    /* A second exact rule for the class is a declaration of the same property
       tied on specificity, and the later one wins: it would replace the cap
       read below without touching it. */
    expect(rules(/^\.msg\.me$/), 'one .msg.me rule').toHaveLength(1)
    expect(decls('.msg.me').get('max-width')).toBe('100%')
  })

  it("caps the delivered bubble at the turn it sits in", () => {
    expect(rules(/^\.sdlv$/), 'one .sdlv rule').toHaveLength(1)
    expect(decls('.sdlv').get('max-width')).toBe('100%')
  })
})
