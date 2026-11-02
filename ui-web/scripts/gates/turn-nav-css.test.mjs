/* The turn nav's card rides above the composer, not under it.
 *
 * The card is a child of .transcript-nav, and that box is stacked (absolute
 * with a z-index of its own), so a z-index on the card would stay inside it:
 * what decides whether the card is painted over the composer is the nav's
 * level beside .dock's, both children of the chat's stacking context. A card
 * hanging from a mark near the bottom of the band overlaps the composer by
 * half its height, and at the dock's 5 against the nav's 4 it was drawn under
 * the glass. Paint order is not in the DOM, and happy-dom applies no
 * stylesheet, so the order is pinned here.
 */

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { rule } from './css.mjs'

const sheet = (path) => readFileSync(new URL(`../../src/${path}`, import.meta.url), 'utf8')
const zOf = (source, selector) => {
  const found = rule(selector, source)?.match(/z-index:\s*(\d+)/)
  return found ? Number(found[1]) : null
}

describe('the turn nav over the composer', () => {
  it('keeps the card above the dock, which it can overlap', () => {
    const nav = zOf(sheet('features/transcript/styles.css'), '.transcript-nav')
    const dock = zOf(sheet('styles/page.css'), '.dock')
    expect(nav).not.toBeNull()
    expect(dock).not.toBeNull()
    expect(nav).toBeGreaterThan(dock)
  })
})
