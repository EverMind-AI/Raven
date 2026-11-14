/* Where a settings row's control sits.
 *
 * A row is a grid: the label takes what is left of the card, the control keeps
 * the width it needs. The label column was `1fr`, whose floor is the cell's own
 * min-content -- so a label that cannot wrap (the chat role's parameters
 * disclosure carries a nowrap summary) pushed the control column out of the
 * card: that row's model pill sat 17px right of every other row's and ran
 * past the card's edge, which read as a broken column rather than a long
 * label. `minmax(0, 1fr)` lets the label give way and ellipsize instead, and
 * every row's control lands on the same line. happy-dom lays nothing out, so
 * the sheet is where it is pinned.
 */

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { decls } from './css.mjs'

const sheet = readFileSync(new URL('../../src/features/settings/styles.css', import.meta.url), 'utf8')

describe('a settings row', () => {
  it('keeps its control column in place, whatever the label says', () => {
    expect(decls('.settings-row', sheet).get('grid-template-columns')).toBe('minmax(0,1fr) auto')
  })
})
