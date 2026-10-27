/* What a board card's agent line gives up first, read off the tasks sheet.
 *
 * A decision node's selection rides the agent line after the agent's name, its
 * handle and the tool-count chip, on a card 196px wide. The selection was the
 * one thing on that line able to shrink, so it went first, down to its arrow.
 * Now the handle and the later routes take only the room the line has left, the
 * agent's name gives way after them down to a floor, and the first route gives
 * way last, to an ellipsis. Route names have no length cap, so the first route
 * must be able to give way: held at its own width, it drove the name to 0px and
 * ran past the line's edge. happy-dom lays nothing out, so only the sheet can
 * say here; tests/test_web_task_card_route_real_browser.py measures
 * the same line in Chromium.
 */
import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { decls } from './css.mjs'

const sheet = readFileSync(new URL('../../src/features/tasks/styles.css', import.meta.url), 'utf8')

/** `flex: <grow> <shrink> <basis>` as numbers and the basis as written. */
function flex(selector) {
  const value = decls(selector, sheet).get('flex')
  if (value === 'none') return { grow: 0, shrink: 0, basis: 'auto' }
  const [grow, shrink, basis] = value.split(/\s+/)
  return { grow: Number(grow), shrink: Number(shrink), basis }
}

describe("a board card's agent line", () => {
  it("cuts a selection's arrow and first route last, to an ellipsis", () => {
    expect(flex('.tkrunsel')).toEqual({ grow: 0, shrink: 1, basis: 'auto' })
    expect(decls('.tkrunsel', sheet).get('min-width')).toBe('0')
    const shared = decls('.tkrunsel,\n.tkrunselrest', sheet)
    expect(shared.get('overflow')).toBe('hidden')
    expect(shared.get('text-overflow')).toBe('ellipsis')
  })

  it('gives the handle and the routes after the first only the room left over', () => {
    for (const selector of ['.tkrunhandle', '.tkrunselrest']) {
      expect(flex(selector), selector).toEqual({ grow: 1, shrink: 1, basis: '0' })
      expect(decls(selector, sheet).get('max-width'), selector).toBe('max-content')
    }
  })

  it('leaves the routes after the first a letter and an ellipsis, which a single letter cannot show', () => {
    expect(decls('.tkrunselrest', sheet).get('min-width')).toBe('2ch')
  })

  it("lets the agent's name give way after them, down to a floor it never shrinks below", () => {
    const rule = decls('.tkrunag', sheet)
    const ag = flex('.tkrunag')
    expect(ag.shrink).toBe(0)
    expect(ag.basis).toBe('6ch')
    expect(rule.get('max-width')).toBe('max-content')
    expect(rule.get('min-width')).toBeUndefined()
    expect(rule.get('overflow')).toBe('hidden')
    expect(rule.get('text-overflow')).toBe('ellipsis')
    for (const selector of ['.tkrunhandle', '.tkrunselrest']) {
      expect(ag.grow, selector).toBeGreaterThanOrEqual(1000 * flex(selector).grow)
    }
  })

  it('never shrinks the tool-count chip', () => {
    expect(decls('.tkrunchip', sheet).get('flex')).toBe('none')
  })

  /* A route longer than the line itself: whatever its length, the line's floor is
     the name's 6ch, the chip, and the later routes' 2ch, because the first route
     is the one part whose floor is nothing. `flex: none` with `max-width: 100%`
     gave it a floor of the whole line. */
  it('keeps no floor under the first route, so no route name is too long for the line', () => {
    const sel = decls('.tkrunsel', sheet)
    expect(sel.get('flex')).not.toBe('none')
    expect(sel.get('max-width')).toBeUndefined()
    expect(flex('.tkrunsel').shrink).toBeGreaterThan(0)
    expect(sel.get('min-width')).toBe('0')
  })
})
