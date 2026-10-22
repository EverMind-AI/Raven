/* What a board card's agent line gives up first, read off the tasks sheet.
 *
 * A decision node's selection rides the agent line after the agent's name, its
 * handle and the tool-count chip, on a card 196px wide. The selection was the
 * one thing on that line able to shrink, so it went first, down to its arrow.
 * Now the arrow and the first route keep their width, the handle and the later
 * routes take only the room the line has left, and the agent's name gives way
 * after them. happy-dom lays nothing out, so only the sheet can say.
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { decls } from './css.mjs'

const sheet = readFileSync(new URL('../../src/features/tasks/styles.css', import.meta.url), 'utf8')

describe("a board card's agent line", () => {
  it("keeps a selection's arrow and first route whole", () => {
    expect(decls('.tkrunsel', sheet).get('flex')).toBe('none')
  })

  it('gives the handle and the routes after the first only the room left over', () => {
    for (const selector of ['.tkrunhandle', '.tkrunselrest']) {
      const rule = decls(selector, sheet)
      expect(rule.get('flex'), selector).toBe('1 1 0')
      expect(rule.get('max-width'), selector).toBe('max-content')
    }
  })

  it('leaves the routes after the first a letter and an ellipsis, which a single letter cannot show', () => {
    expect(decls('.tkrunselrest', sheet).get('min-width')).toBe('2ch')
  })

  it("lets the agent's name give way after them, with an ellipsis", () => {
    const rule = decls('.tkrunag', sheet)
    expect(rule.get('min-width')).toBe('0')
    expect(rule.get('text-overflow')).toBe('ellipsis')
  })

  it('never shrinks the tool-count chip', () => {
    expect(decls('.tkrunchip', sheet).get('flex')).toBe('none')
  })
})
