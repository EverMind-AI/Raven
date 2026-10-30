/* The details pane floats over the rows as a white card.
 *
 * Opening the pane must not take width from the waterfall: the rows keep the
 * whole column and the pane is laid over their right side, a card on the
 * page's own paper with rounded corners, at every width. A pane that sat in
 * the row as a flex item narrowed the axis under the reader's eye each time it
 * opened. Pinned against the trajectory sheet because happy-dom does no layout:
 * a floating pane and one in the row are the same tree there.
 */

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { rules } from './css.mjs'

const sheet = readFileSync(new URL('../../src/features/trajectory/styles.css', import.meta.url), 'utf8')

/* The value a property ends on for an element carrying these classes: their
   rules in sheet order, then the trajectory view's, which outrank them while
   the view is up. */
const effective = (classes, property) => {
  let value = null
  for (const scope of ['', 'html[data-traj] ']) {
    for (const [selector, body] of rules(null, sheet)) {
      const parts = selector.split(',').map((part) => part.trim())
      if (!classes.some((name) => parts.includes(scope + name))) continue
      const found = [...body.matchAll(new RegExp(`(?:^|;)\\s*${property}\\s*:\\s*([^;]+)`, 'g'))].at(-1)
      if (found) value = found[1].trim()
    }
  }
  return value
}

describe('the details pane', () => {
  it('floats over the rows instead of taking a share of the row', () => {
    expect(effective(['.trajectory-details'], 'position')).toBe('absolute')
  })

  it('starts below the header, so the window presets and the fold button stay in reach', () => {
    /* One variable sizes the header and places the card: the two cannot drift. */
    expect(effective(['.trajectory-wf-stats'], 'height')).toBe('var(--wf-stats-h)')
    expect(effective(['.trajectory-details'], 'top')).toMatch(/^calc\(var\(--wf-stats-h\) \+ \d+px\)$/)
  })

  /* The narrow pane carries both classes, and is the same card over the whole area. */
  for (const pane of [['.trajectory-details'], ['.trajectory-details', '.trajectory-details-over']]) {
    it(`is a white card with rounded corners (${pane.join(' ')})`, () => {
      expect(effective(pane, 'background')).toBe('var(--paper)')
      expect(Number.parseFloat(effective(pane, 'border-radius') ?? '0')).toBeGreaterThan(0)
    })
  }
})
