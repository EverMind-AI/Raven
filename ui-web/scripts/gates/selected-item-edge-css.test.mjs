/* The card that marks the selected row, and the edge it wears.
 *
 * Inside the settings dialog the memory list sits on the same paper as the
 * rail beside it, and a selected row is the same white as the rail's
 * selected item -- which is not enough. White on white with no edge reads as
 * "nothing is selected": the row is there, the reader is not told. The rail's
 * item wears a hairline around it, and a list row wore none.
 *
 * The mark is one card that belongs to the list and slides onto whichever row
 * is selected, so a selection is seen to move rather than to blink. The row
 * itself paints nothing: a mark of its own under the sliding one would show
 * as a second mark.
 *
 * The rail's accent bar stays the rail's: it says which section is open, and
 * a row in a list is not a section.
 *
 * happy-dom applies no stylesheet, so no DOM test can see this -- the rule is
 * pinned here, in the sheets that draw it.
 */

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { decls, rules } from './css.mjs'

const SETTINGS_SHEET = readFileSync(
  new URL('../../src/features/settings/styles.css', import.meta.url),
  'utf8',
)

/* The parts of the edge a rule wears: the hairline around the item, and the
   accent bar down its left edge. */
const edge = (declarations) =>
  (declarations?.get('box-shadow') ?? '').split(',').map((part) => part.trim())

const hairline = (declarations) => edge(declarations).some((part) => /^inset 0 0 0 1px/.test(part))
const accentBar = (declarations) => edge(declarations).some((part) => /^inset 3px 0 0/.test(part))

describe('the card that marks the selected row', () => {
  it('wears a hairline, the way the rail item wears one', () => {
    expect(hairline(decls('.settings-nitem[aria-current="true"]', SETTINGS_SHEET))).toBe(true)
    expect(hairline(decls('.two-pane-sel'))).toBe(true)
  })

  it('leaves the accent bar to the rail, which is where a section is marked', () => {
    expect(accentBar(decls('.settings-nitem[aria-current="true"]', SETTINGS_SHEET))).toBe(true)
    expect(accentBar(decls('.two-pane-sel'))).toBe(false)
  })

  it('slides onto the row that is selected', () => {
    const card = decls('.two-pane-sel')
    expect(card?.get('transition')).toContain('transform')
    expect(card?.get('position')).toBe('absolute')
    /* Under the rows: what the reader reads is the row's own text. */
    expect(card?.get('z-index')).toBe('-1')
    expect(card?.get('pointer-events')).toBe('none')
  })

  it('does not move for a reader who asked for no motion', () => {
    const still = rules(/\.two-pane-sel/).find(([, body]) => body.includes('transition: none'))
    expect(still).toBeTruthy()
  })

  it('leaves the row itself unmarked, so one mark is all there is', () => {
    const row = decls('.two-pane-row[aria-current="true"]')
    expect(row?.get('background')).toBe('none')
    expect(row?.get('box-shadow')).toBeUndefined()
  })
})
