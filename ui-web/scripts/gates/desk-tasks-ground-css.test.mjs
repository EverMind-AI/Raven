/* The desk palette's ground while its Tasks tab is showing.
 *
 * The palette is frosted glass (its ::after layer), and the Tasks board inside
 * it keeps that glass: `.tkboard` paints --surface on a page of its own, and
 * in the palette that opaque layer made the panel read a full step darker
 * than the rail beside the transcript. So inside the palette the board is
 * transparent, and the body alone carries one faint veil, --desk-board, which
 * is just enough step for the cards to stand on. The tab strip above it is the
 * palette's own glass on every tab, so switching tabs never repaints the strip
 * the reader's hand is on. happy-dom applies no stylesheet, so no DOM test can
 * see any of this; the stylesheet is the only place it can be pinned.
 *
 * The secondary rules are part of the same fact: the header's rule takes the
 * stronger line where the strip meets the board, and the flat-list fallback's
 * hover moves one step up the ramp instead of disappearing into the ground.
 * Neither the selected tab nor the unread count needs a Tasks-only step: both
 * sit on the strip like on any other tab, and the selected tab is the inverted
 * pill besides.
 */

import { describe, expect, it } from 'vitest'

import { decls } from './css.mjs'

const TAB = '.desk-palette[data-tab="tasks"]'

describe('the desk palette takes the Tasks tab\'s ground', () => {
  it('veils the body over the glass and leaves the board and the frame alone', () => {
    expect(decls(`${TAB} .desk-body`)?.get('background')).toBe('var(--desk-board)')
    expect(decls('.desk-palette .tkboard')?.get('background')).toBe('none')
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
