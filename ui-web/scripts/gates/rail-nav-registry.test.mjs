/* Every rail button a page can light up is one the rail actually writes to.
 *
 * `markNew` clears and sets `aria-current` over a list of button ids, and the
 * list used to be written out by hand -- deliberately, because `capsPage`
 * lights skillBtn or plugBtn depending on which tab stands open and a set
 * derived from the page registry would leave a stale mark on whichever of the
 * two it could not see. The cost of naming it was that adding a page to the
 * registry and forgetting the list produced a page with no selected state at
 * all, and nothing failed: the unit tests around the rail carry their own copy
 * of the registry as a fixture, so a page missing from BOTH the fixture and the
 * production list was invisible to them. That is how the playbook page shipped
 * unmarked.
 *
 * Both lists are one table now (state/pages.ts): the rail's mark walks
 * NAV_BUTTONS, the nav strip renders a row per button, and the page store
 * answers which of a page's two the open tab means. So what is left to check is
 * that nothing has gone back to writing a list of its own -- and that the two
 * readers really read it.
 */

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

const src = (rel) => readFileSync(new URL(`../../src/${rel}`, import.meta.url), 'utf8')

const pages = src('state/pages.ts')
const rail = src('features/rail/store.ts')
const strip = src('chrome/Rail.tsx')

/** The buttons the one table declares, the draft row's included. */
function declared() {
  const table = pages.slice(pages.indexOf('const DECLARED = ['), pages.indexOf('] as const'))
  const buttons = new Set(['newBtn'])
  for (const row of table.matchAll(/navButtons:\s*\[([^\]]*)\]/g)) {
    for (const button of row[1].matchAll(/'([^']+)'/g)) buttons.add(button[1])
  }
  return buttons
}

describe('the rail nav registry', () => {
  it('declares every button once, in the page table', () => {
    const buttons = declared()
    /* The three the page has: the draft row, and the two module rows. */
    expect([...buttons].sort()).toEqual(['agentsBtn', 'newBtn', 'personaBtn'])
  })

  it('has the mark walk that table rather than a list of its own', () => {
    expect(rail).toContain("import { NAV_BUTTONS } from '../../state/pages'")
    expect(rail).toContain('for (const id of NAV_BUTTONS) {')
    /* No second list: a literal array of button ids in this file is the thing
       that used to go stale. */
    expect(rail.match(/\['newBtn'[^\]]*\]/g)).toBe(null)
  })

  it('renders a nav row for every button but the draft', () => {
    const rows = [...strip.matchAll(/^\s+button: '(\w+)',$/gm)].map((m) => m[1])
    const own = [...declared()].filter((button) => button !== 'newBtn')
    expect(rows.sort()).toEqual(own.sort())
    /* The draft row is the strip's own markup, because it opens no page: it
       starts a conversation. */
    expect(strip).toContain('id="newBtn"')
  })
})
