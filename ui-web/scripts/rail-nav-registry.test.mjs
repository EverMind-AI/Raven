/* Every rail button a page can light up is one the rail actually writes to.
 *
 * `markNew` clears and sets `aria-current` over a NAMED list of button ids, not
 * one derived from the page registry -- deliberately, because `capsPage` lights
 * skillBtn or plugBtn depending on which tab stands open, and a derived set
 * would leave a stale mark on whichever of the two it could not see.
 *
 * The cost of naming it is that adding a page to NAV_OF and forgetting this list
 * produces a page with no selected state at all, and nothing fails: the unit
 * tests around the rail carry their own copy of the registry as a fixture, so a
 * page missing from BOTH the fixture and the production list is invisible to
 * them. That is how the playbook page shipped unmarked. This reads the two real
 * sources instead. */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const caps = readFileSync(new URL('../src/demo/120-capabilities.js', import.meta.url), 'utf8')
const rail = readFileSync(new URL('../src/features/rail/store.ts', import.meta.url), 'utf8')

function navOf() {
  const block = caps.match(/const NAV_OF = \{(.*?)\n\};/s)
  if (!block) throw new Error('NAV_OF is absent from demo/120-capabilities.js')
  const out = new Map()
  for (const line of block[1].split('\n')) {
    const named = line.match(/^\s*(\w+)\s*:\s*'([^']+)'/)
    if (named) {
      out.set(named[1], [named[2]])
      continue
    }
    /* capsPage's button is chosen at read time; take every id it can return. */
    const computed = line.match(/^\s*(\w+)\s*:\s*\(\)\s*=>/)
    if (computed) out.set(computed[1], [...line.matchAll(/'(\w+Btn)'/g)].map((m) => m[1]))
  }
  if (!out.size) throw new Error('NAV_OF parsed to nothing')
  return out
}

function marked() {
  const list = rail.match(/for \(const id of \[([^\]]+)\]\) \{/)
  if (!list) throw new Error('the markNew button list is absent from features/rail/store.ts')
  return new Set([...list[1].matchAll(/'([^']+)'/g)].map((m) => m[1]))
}

describe('the rail nav registry', () => {
  it('writes the mark on every button a page can light', () => {
    const writes = marked()
    const missing = []
    for (const [page, buttons] of navOf()) {
      for (const b of buttons) if (!writes.has(b)) missing.push(`${page} -> ${b}`)
    }
    expect(missing).toEqual([])
  })

  it('clears the mark on the draft row too, which no page owns', () => {
    /* Not in NAV_OF, so the loop above cannot vouch for it, and it is the one
       button that has to be un-marked when a page opens over the chat. */
    expect(marked().has('newBtn')).toBe(true)
  })
})
