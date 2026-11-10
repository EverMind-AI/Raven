/* The desk palette's ground while its Tasks tab is showing.
 *
 * The Tasks board paints on --surface (features/tasks/styles.css's `.tkboard`),
 * but the palette itself is --ink. The body's 5px padding is transparent, so
 * that --ink showed through as a white band above and below the board -- the
 * chrome and the content disagreeing about which surface they were on. The
 * body takes the board's ground, then, and only the body: the tab strip above
 * it is the palette's own white on every tab, so switching tabs never repaints
 * the strip the reader's hand is on, and the notch -- which inherits the
 * frame's ground -- stays the colour of the strip it grows out of. happy-dom
 * applies no stylesheet, so no DOM test can see any of this; the stylesheet is
 * the only place it can be pinned.
 *
 * The secondary rules are part of the same fact: the header's rule takes the
 * stronger line where white meets the board, and the flat-list fallback's hover
 * moves one step up the ramp instead of disappearing into the ground. Neither
 * the selected tab nor the unread count needs a Tasks-only step: both sit on
 * the white strip like on any other tab, and the selected tab is the inverted
 * pill besides.
 */

import { describe, expect, it } from 'vitest'

import { decls } from './css.mjs'

const TAB = '.desk-palette[data-tab="tasks"]'

describe('the desk palette takes the Tasks tab\'s ground', () => {
  it('paints the body with the board\'s surface and leaves the frame white', () => {
    expect(decls(`${TAB} .desk-body`)?.get('background')).toBe('var(--surface)')
    expect(decls(TAB)).toBeFalsy()
    expect(decls(`${TAB} .desk-drag`)?.get('background')).toBeUndefined()
  })

  it('keeps the selected tab and the unread count as on any other tab', () => {
    expect(decls('.desk-tabs button[aria-selected="true"]')?.get('background')).toBe('var(--text)')
    expect(decls(`${TAB} .desk-tabs button[aria-selected="true"]`)).toBeFalsy()
    expect(decls(`${TAB} .desk-count`)).toBeFalsy()
  })

  it('keeps the header rule and the flat-list hover visible', () => {
    expect(decls(`${TAB} .desk-drag`)?.get('border-bottom-color')).toBe('var(--line)')
    expect(decls(`${TAB} .salist.tasks .sarow:hover`)?.get('background')).toBe('var(--raised)')
  })
})
