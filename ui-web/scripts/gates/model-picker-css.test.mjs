/* The agents page's model picker: its provider column, read straight off page.css.
 *
 * The column was a 170px track and a heading wrapped inside it -- a Mirasim
 * agent's model choices are headed by the agent ("Claude Code"), the route
 * ("Mirasim") and the user's keys ("Own key"), and a selected heading is bold
 * beside a count and a dot. happy-dom applies no stylesheet, so the component
 * test can only pin that the name is an element of its own; the width and the
 * one-line rule are facts about paint, and this is where they are held.
 */

import { describe, expect, it } from 'vitest'

import { decls } from './css.mjs'

/* Measured in Chrome, the worst case: bold, a two-digit count and the dot,
   plus the column's padding and rule. "Claude Code" and "Antigravity" are the
   widest headings the Mirasim agents carry. */
const WIDEST_AGENT_HEADING = 191

describe('the model picker s provider column', () => {
  it('is wide enough for the widest heading a Mirasim agent carries', () => {
    const track = /^(\d+)px\s/.exec(decls('.model-picker-body').get('grid-template-columns'))
    expect(track, 'the first track is a px width').toBeTruthy()
    expect(Number(track[1])).toBeGreaterThanOrEqual(WIDEST_AGENT_HEADING)
  })

  it('keeps a heading on one line, giving way at its end', () => {
    const name = decls('.model-picker-prov .model-picker-name')
    expect(name, 'the heading has a rule of its own').toBeTruthy()
    expect(name.get('white-space')).toBe('nowrap')
    expect(name.get('overflow')).toBe('hidden')
    expect(name.get('text-overflow')).toBe('ellipsis')
    /* A flex item does not shrink below its text without this, which is what
       made the ellipsis never fire. */
    expect(name.get('min-width')).toBe('0')
  })
})
