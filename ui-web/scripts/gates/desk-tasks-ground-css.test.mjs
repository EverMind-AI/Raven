/* The desk palette's ground while its Tasks tab is showing.
 *
 * The Tasks board paints on --surface (features/tasks/styles.css's `.tkboard`),
 * but the palette itself was --ink. The drag strip and the body's 5px padding
 * are transparent, so that --ink showed through as a white band above and
 * below the board -- the chrome and the content disagreeing about which surface
 * they were on. happy-dom applies no stylesheet, so no DOM test can see it;
 * the stylesheet is the only place this can be pinned.
 *
 * The secondary rules are part of the same fact: once the frame is --surface,
 * the selected tab and the unread count have to move one step up the ramp to
 * stay distinct, the header's rule takes the stronger line, and the flat-list
 * fallback's hover takes that same step instead of disappearing into the
 * ground.
 */

import { describe, expect, it } from 'vitest'

import { decls } from './css.mjs'

const TAB = '.desk-palette[data-tab="tasks"]'

describe('the desk palette takes the Tasks tab\'s ground', () => {
  it('paints the frame with the same surface as the board', () => {
    expect(decls(TAB)?.get('background')).toBe('var(--surface)')
  })

  it('keeps the selected tab and the unread count distinct on that ground', () => {
    expect(decls(`${TAB} .desk-tabs button[aria-selected="true"]`)?.get('background')).toBe('var(--raised)')
    expect(decls(`${TAB} .desk-count`)?.get('border-color')).toBe('var(--surface)')
  })

  it('keeps the header rule and the flat-list hover visible', () => {
    expect(decls(`${TAB} .desk-drag`)?.get('border-bottom-color')).toBe('var(--line)')
    expect(decls(`${TAB} .salist.tasks .sarow:hover`)?.get('background')).toBe('var(--raised)')
  })
})
