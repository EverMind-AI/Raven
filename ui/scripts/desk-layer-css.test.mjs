/* The three layers a drag puts on the desk, in the order a reader needs them.
 *
 * The resting panes are the ground, the drop indicator draws on top of them --
 * it is the answer to "where would this land" and is worthless behind the thing
 * it is answering about -- and the pane in hand rides above both.
 *
 * That order only holds while a pane's INSIDE cannot reach the grid's layers.
 * A pane's composer carries the page composer's own `.dock` z-index of 5, and
 * with no stacking context on the pane that 5 was measured against the grid:
 * it beat the indicator's 3, and dragging over a sub-agent's window drew the
 * target rectangle behind that window's composer. No DOM test can see this --
 * paint order is not in the DOM -- so the stylesheet is where it is pinned.
 */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('../src/styles/page.css', import.meta.url), 'utf8')

/* One rule's declarations, by its exact selector. Comments come out first: what
   sits between two rules is captured as the next one's selector, prose and all. */
function rule(selector) {
  const rules = css.replace(/\/\*[\s\S]*?\*\//g, ' ')
  for (const match of rules.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (match[1].trim() === selector) return match[2]
  }
  return null
}

const zOf = (selector) => {
  const body = rule(selector)
  const found = body && body.match(/z-index:\s*(\d+)/)
  return found ? Number(found[1]) : null
}

/* Every rule that gives the pane ELEMENT a stacking level, as
   `selector { z-index: N }`. The pane's own layers being sealed in is worth
   nothing if the pane then takes a level of its own -- that puts it back among
   the grid's layers, above the indicator's 3, isolation and all.
   Read from the last compound of each selector, not from the whole string: a
   z-index on something INSIDE a pane (`.desk-pane > header`) stays inside it
   and is not this invariant. `-lift` and `-settle` are the two that carry one
   on purpose. */
function paneLevels() {
  const rules = css.replace(/\/\*[\s\S]*?\*\//g, ' ')
  const out = []
  for (const match of rules.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const z = match[2].match(/z-index:\s*(-?\d+)/)
    if (!z) continue
    for (const part of match[1].split(',')) {
      const target = part.trim().split(/[\s>+~]+/).filter(Boolean).pop() || ''
      if (!target.includes('.desk-pane')) continue
      if (target.includes('.desk-pane-lift') || target.includes('.desk-pane-settle')) continue
      out.push(`${part.trim()} { z-index: ${z[1]} }`)
    }
  }
  return out
}

describe('the desk layers while a pane is being dragged', () => {
  it('keeps a pane a stacking context, so its own z-indexes stay its own', () => {
    const pane = rule('.desk-pane')
    expect(pane).toBeTruthy()
    expect(pane).toMatch(/isolation:\s*isolate/)
  })

  it('draws the drop indicator above the resting panes and below the pane in hand', () => {
    const drop = zOf('.desk-drop')
    const lift = zOf('.desk-pane-lift')
    expect(drop).toBeGreaterThan(0)
    expect(lift).toBeGreaterThan(drop)
    /* And no rule gives a resting pane a level of its own, however it spells
       the selector. Checked against the bare `.desk-pane` alone, this passed
       while `.desk-grid .desk-pane { z-index: 1 }` -- or the same rule repeated
       inside a @media block -- put every pane back above the indicator. */
    expect(paneLevels()).toEqual([])
  })
})
