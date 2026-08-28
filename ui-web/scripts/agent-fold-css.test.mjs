/* The roster's fold slot must keep its width on a head that cannot fold.
 *
 * The list is one column of agents, a few of which have conversations under
 * them. When the slot was removed from the flow for the rest (`hidden`, which
 * is `display: none`), the foldable heads carried their icon and their name
 * 12px right of everyone else's and the list read as two vertical lines.
 *
 * Pinned here because no DOM test can see it: happy-dom does no layout, so a
 * rule that occupies space versus one that does not are the same assertion
 * there. The markup half -- that every head has the slot -- is pinned in
 * SubagentsPage.test.tsx; this is the half that says the slot still takes up
 * room when it is empty.
 */

import { readFileSync } from 'node:fs'

import { describe, expect, it } from 'vitest'

const css = readFileSync(new URL('../src/styles/page.css', import.meta.url), 'utf8')

function rule(selector) {
  const rules = css.replace(/\/\*[\s\S]*?\*\//g, ' ')
  for (const match of rules.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    if (match[1].trim() === selector) return match[2]
  }
  return null
}

describe('the agent roster fold slot', () => {
  it('hides the glyph of an unfoldable head without collapsing its slot', () => {
    const empty = rule('.agent-fold[data-empty="true"]')
    expect(empty).toBeTruthy()
    expect(/visibility:\s*hidden/.test(empty)).toBe(true)
    expect(/display:\s*none/.test(empty)).toBe(false)
  })

  it('keeps that slot a fixed width, so every head starts at one x', () => {
    const fold = rule('.agent-fold')
    expect(fold).toBeTruthy()
    expect(/width:\s*\d/.test(fold)).toBe(true)
    expect(/flex:\s*none/.test(fold)).toBe(true)
  })

  /* The attribute selector is the only thing standing between this and the
     rule it replaced; a stray `[hidden]` rule on the same class would take the
     slot back out of the flow. */
  it('has no rule left that removes the slot', () => {
    expect(rule('.agent-fold[hidden]')).toBe(null)
  })
})
