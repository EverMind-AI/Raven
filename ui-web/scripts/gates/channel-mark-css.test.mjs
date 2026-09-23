/* The channel mark keeps the letter tile's box, in both places it is drawn.
 *
 * components/ChannelMark.tsx stands in for `.pmtile` in the channels section's
 * two slots -- a list row (`.two-pane-hit`) and the picked channel's header
 * (`.two-pane-head`) -- and still draws that tile for an entrance with no mark.
 * `.pmtile` is sized per container, five rules beside its base one, so the
 * substitute is right only while its own box is the base one and while neither
 * slot gives the tile a box of its own. Which containers hold the mark is a fact
 * about the markup, measured in a browser (both slots draw the tile and the mark
 * 38x38 with an 11px radius, and no ancestor is one of the five containers); the
 * rest is a fact about the stylesheet, which happy-dom does not apply, so it is
 * read off page.css here. The expected box comes from `.pmtile`'s own rule, so
 * resizing the tile moves this guard with it instead of restating its numbers.
 */

import { describe, expect, it } from 'vitest'

import { rule, rules } from './css.mjs'

const BOX = ['width', 'height', 'border-radius']
const SLOTS = ['two-pane-hit', 'two-pane-head']

const boxOf = (body) => {
  const out = {}
  for (const decl of (body ?? '').split(';')) {
    const at = decl.indexOf(':')
    const prop = decl.slice(0, at).trim()
    if (at > 0 && BOX.includes(prop)) out[prop] = decl.slice(at + 1).trim()
  }
  return out
}

const sizedIn = (slot, klass) =>
  rules(new RegExp(`\\.${slot}\\b[^,]*\\.${klass}\\b`))
    .map(([, body]) => boxOf(body))
    .filter((box) => Object.keys(box).length)

describe('the channel mark s box', () => {
  it('is the letter tile s base box', () => {
    const tile = boxOf(rule('.pmtile'))
    expect(Object.keys(tile)).toEqual(BOX)
    expect(boxOf(rule('.channel-mark'))).toEqual(tile)
  })

  it('is whatever box each of its slots gives the letter tile', () => {
    for (const slot of SLOTS) expect([slot, sizedIn(slot, 'channel-mark')]).toEqual([slot, sizedIn(slot, 'pmtile')])
  })
})
