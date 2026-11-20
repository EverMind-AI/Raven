/* The edge a selected item carries.
 *
 * Inside the settings dialog the memory list sits on the same paper as the
 * rail beside it, and a selected row is the same white as the rail's
 * selected item -- which is not enough. White on white with no edge reads as
 * "nothing is selected": the row is there, the reader is not told. The rail's
 * item wears a hairline around it, and a list row wore none.
 *
 * The rail's accent bar stays the rail's: it says which section is open, and
 * a row in a list is not a section.
 *
 * happy-dom applies no stylesheet, so no DOM test can see this -- the rule is
 * pinned here, in the sheets that draw it.
 */

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { decls } from './css.mjs'

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

describe('a selected item in the settings dialog', () => {
  it('wears a hairline, in the rail and in a list row alike', () => {
    expect(hairline(decls('.settings-nitem[aria-current="true"]', SETTINGS_SHEET))).toBe(true)
    expect(hairline(decls('.two-pane-row[aria-current="true"]'))).toBe(true)
  })

  it('leaves the accent bar to the rail, which is where a section is marked', () => {
    expect(accentBar(decls('.settings-nitem[aria-current="true"]', SETTINGS_SHEET))).toBe(true)
    expect(accentBar(decls('.two-pane-row[aria-current="true"]'))).toBe(false)
  })
})
