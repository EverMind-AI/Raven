/* The channel mark is one box, in both places it is drawn.
 *
 * components/ChannelMark.tsx draws the channels section's two slots -- a list
 * row (`.two-pane-hit`) and the picked channel's header (`.two-pane-head`) --
 * and an entrance with no mark of its own gets the plain one in the same frame.
 * The box used to be read off `.pmtile`, the letter tile the mark replaced, so
 * resizing the tile moved this guard with it. The tile is gone, so the mark's
 * own rule is the box, and two things are left to hold: that it declares a
 * whole one, and that neither slot quietly gives it a second. Which containers
 * hold the mark is a fact about the markup, measured in a browser (both slots
 * draw it 38x38 with an 11px radius); the rest is a fact about the stylesheet,
 * which happy-dom does not apply, so it is read off page.css here.
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
  it('is declared whole, on the mark itself', () => {
    expect(Object.keys(boxOf(rule('.channel-mark')))).toEqual(BOX)
  })

  it('is not overridden by either slot it is drawn in', () => {
    for (const slot of SLOTS) expect([slot, sizedIn(slot, 'channel-mark')]).toEqual([slot, []])
  })
})
