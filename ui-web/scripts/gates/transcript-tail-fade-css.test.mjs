/* Where the transcript stops being painted, above the composer.
 *
 * The composer is a card floating over the scroller (`.dock` is absolute), so
 * a line scrolled up does not run out of room -- it runs into the card. The
 * card is opaque, which cut the line mid-glyph at its top edge, and the gutter
 * below the card left the NEXT line legible underneath the field the reader is
 * typing in. A ramp in the scroller's own alpha is what ends both.
 *
 * Pinned here rather than in a DOM test for the reason every CSS gate in this
 * directory exists: happy-dom does no layout and applies no stylesheet, so a
 * transcript that dissolves before the composer and one that slides under it
 * are the same assertion there. Only the stylesheet says which happens.
 *
 * The arithmetic is the whole invariant, so it is asserted as the declaration
 * rather than as a pixel:
 *
 *   opaque .. 100% - lift - fade | ramp | 100% - lift .. transparent
 *
 * `--lift` is the distance from the chat column's bottom edge up to the top of
 * the highest docked thing, published by dockLift (features/composer/store.ts)
 * and already spent by `.col` as bottom padding and by `.backpill` as its
 * offset. Spending the SAME number here is what makes the three agree: the
 * clear edge lands exactly on the top of the composer however tall it has
 * grown, and the padding guarantees the tail of the transcript sits above the
 * ramp at rest, so the fade bites only while the reader is scrolled up -- which
 * is the only time anything was overlapping.
 *
 * Three ways to break it that all still render something plausible, which is
 * why each is its own assertion below: dropping the `, 96px` fallback (the
 * property is unset until the first dockLift, and `var()` with no fallback
 * makes the calc invalid at computed-value time, which drops the whole
 * declaration and the mask with it); letting the two spellings drift, so
 * WebKit fades at a different place than everyone else; and moving the mask
 * onto `.col`, which is the SCROLLED box -- a mask there is painted over the
 * content's own border box and travels up with the text instead of standing
 * still over the composer.
 */

import { describe, expect, it } from 'vitest'

import { decls, rule, rules } from './css.mjs'

/* `calc(100% - var(--lift, 96px))` and the same minus one ramp, tolerant about
   whitespace but not about which numbers are in them. */
const CLEAR = String.raw`calc\(100% - var\(--lift,\s*96px\)\)`
const RAMP = String.raw`calc\(100% - var\(--lift,\s*96px\) - var\(--tail-fade\)\)`
const GRADIENT = new RegExp(
  String.raw`linear-gradient\(180deg,\s*#000\s+${RAMP},\s*transparent\s+${CLEAR}\)`
)

describe('the transcript fades out above the composer', () => {
  it('masks the scrollport, so the ramp stands still while the text moves', () => {
    /* `.scroll` is the element with `overflow-y: auto`. A mask is painted over
       the element's own border box, and a scroll container's border box does
       not scroll -- which is what makes one gradient hold one screen position
       for every scroll offset. */
    const scroll = decls('.scroll')
    expect(scroll).not.toBeNull()
    expect(scroll.get('overflow-y')).toBe('auto')

    const masked = rules(/^\.scroll$/).find((r) => r[1].includes('mask-image'))
    expect(masked).toBeDefined()
    expect(masked[1]).toMatch(GRADIENT)
  })

  it('clears exactly at the top of whatever is docked, not at a constant', () => {
    /* A constant would be wrong the moment the composer grows a line, a queued
       row appears or a clarify sheet stacks above it -- which is precisely when
       there is more of the card for the text to collide with. */
    const masked = rules(/^\.scroll$/).find((r) => r[1].includes('mask-image'))
    expect(masked[1]).toMatch(new RegExp(String.raw`transparent\s+${CLEAR}`))
  })

  it('keeps the fallback, or an unpublished --lift drops the whole declaration', () => {
    /* dockLift writes `--lift` on `.chat` and only on a change, so the property
       is absent for every frame before the composer's first measurement. A
       `var(--lift)` with no fallback is not "0" there: the calc is invalid at
       computed-value time and the declaration is thrown away, mask and all. */
    const masked = rules(/^\.scroll$/).find((r) => r[1].includes('mask-image'))
    const uses = masked[1].match(/var\(--lift[^)]*\)/g)
    expect(uses).not.toBeNull()
    for (const use of uses) expect(use).toMatch(/var\(--lift,\s*96px\)/)
  })

  it('says the same thing to WebKit, so the two cannot drift', () => {
    /* Safari still needs the prefix, and two hand-written copies of one
       gradient are two places for a number to be edited in only one of them. */
    const masked = rules(/^\.scroll$/).find((r) => r[1].includes('mask-image'))
    const std = /(?:^|[;\s])mask-image:([^;]*)/.exec(masked[1])
    const webkit = /-webkit-mask-image:([^;]*)/.exec(masked[1])
    expect(std).not.toBeNull()
    expect(webkit).not.toBeNull()
    expect(std[1].trim()).toBe(webkit[1].trim())
  })

  it('reserves the same --lift as padding, so the tail is never under the ramp', () => {
    /* This is what keeps the fade from hiding the newest message. `.col` ends
       its column `--lift` plus a margin above the chat's bottom edge, so at the
       tail the last line is already clear of the ramp and the mask costs
       nothing; it only engages once the reader scrolls back. */
    const col = rule('.col')
    expect(col).not.toBeNull()
    expect(col).toMatch(/padding:[^;]*calc\(var\(--lift,\s*96px\) \+ 26px\)/)
  })

  it('does not mask the scrolled column, which would travel with the text', () => {
    /* `.col` is inside the scrollport and moves with it. A ramp there would be
       a band fixed to the CONTENT -- it would fade a different message every
       frame and leave the composer overlapped again. */
    expect(rule('.col')).not.toMatch(/mask/)
  })
})
