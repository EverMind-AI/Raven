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

  it('stops a pane the reader made taller at the bottom of the room under the header, and lifts that once moved or narrow', () => {
    expect(effective(['.trajectory-details'], 'max-height')).toBe('calc(100% - var(--wf-stats-h) - 20px)')
    expect(effective(['.trajectory-details', '.trajectory-details[data-moved]'], 'max-height')).toBe('none')
    expect(effective(['.trajectory-details', '.trajectory-details-over'], 'max-height')).toBe('none')
  })

  it('puts the size handle on the free corner: bottom-left where the pane opens, bottom-right once moved', () => {
    const moved = ['.trajectory-corner', '.trajectory-details[data-moved] .trajectory-corner']
    expect(['position', 'left', 'bottom'].map((side) => effective(['.trajectory-corner'], side))).toEqual(['absolute', '0', '0'])
    expect(['left', 'right', 'bottom'].map((side) => effective(moved, side))).toEqual(['auto', '0', '0'])
  })

  /* A touch drag on either would scroll the page, and a mouse drag on the head
     would select its name. */
  it('lets the head and the size handle be dragged by touch as well as by mouse', () => {
    const head = ['.trajectory-details:not(.trajectory-details-over) > .trajectory-head']
    expect(effective(head, 'touch-action')).toBe('none')
    expect(effective(head, 'user-select')).toBe('none')
    expect(effective(['.trajectory-corner'], 'touch-action')).toBe('none')
  })

  /* The sheet under the card is frosted nearly white, so the card's own paper
     cannot tell it apart: the edge has to. It is the desk palette's -- the
     page's line, and a shadow on every side, none of it tucked under the
     card by a negative spread. */
  it('stands apart from the frosted sheet by its edge, the way the desk palette does', () => {
    expect(effective(['.trajectory-details'], 'border')).toBe('1px solid var(--line)')
    const shadows = [...sheet.matchAll(/--trajectory-card-shadow\s*:\s*([^;]+);/g)].map((m) => m[1])
    expect(shadows).toHaveLength(3)
    for (const shadow of shadows) {
      for (const layer of shadow.split(/,(?![^(]*\))/)) {
        const lengths = layer.replace(/rgba?\([^)]*\)|#[0-9a-f]+/gi, '').trim().split(/\s+/)
        expect(Number.parseFloat(lengths[3] ?? '0'), layer).toBeGreaterThanOrEqual(0)
      }
    }
  })

  /* A fixed ceiling on the key column cut a key the reader had just widened
     the card to read. A share of the card grows with it, the same share in
     every section so their values still line up, with a cap so a short key
     in a very wide card is not left far from its value. */
  it('widens the key column of a key-value list with the card, the same in every section', () => {
    expect(effective(['.trajectory-kv'], 'grid-template-columns')).toMatch(/^minmax\(\d+px, min\(\d+%, \d+px\)\) minmax\(0, 1fr\)$/)
  })

  /* The narrow pane carries both classes, and is the same card over the whole area. */
  for (const pane of [['.trajectory-details'], ['.trajectory-details', '.trajectory-details-over']]) {
    it(`is a white card with rounded corners (${pane.join(' ')})`, () => {
      expect(effective(pane, 'background')).toBe('var(--paper)')
      expect(Number.parseFloat(effective(pane, 'border-radius') ?? '0')).toBeGreaterThan(0)
    })
  }
})
