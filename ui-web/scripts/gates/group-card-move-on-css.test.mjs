/* Move on is always on a folded group card.
 *
 * The desk's Tasks board (features/tasks/BoardList.tsx) renders Move on in
 * every folded Task Group card's foot, and the reader reaches for it without first
 * pointing at the card: it is the card's one verb, so it is drawn whether or
 * not the pointer or the focus is on the card, in the foot's own flow at the
 * line's right end. A stylesheet that faded it, hid it or lifted it out of the
 * line until hover would keep every DOM test green, because happy-dom applies
 * no stylesheet -- so the stylesheet is where this is pinned.
 */

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'

import { rules } from './css.mjs'

const sheet = readFileSync(new URL('../../src/features/tasks/styles.css', import.meta.url), 'utf8')

/* Every rule whose last compound is the Move on button, as `selector { body }`. */
const moveOn = rules(null, sheet).filter(([selector]) =>
  selector.split(',').some((part) => /\.tkmove(?:$|[:[])/.test(part.trim().split(/[\s>+~]+/).pop() || '')))

describe('Move on on a folded group card', () => {
  it('is styled at all, so the checks below are reading something', () => {
    expect(moveOn.some(([selector]) => selector.includes('.tkgfoot'))).toBe(true)
  })

  it('is never faded, hidden or taken out of the line', () => {
    const hiding = moveOn
      .filter(([, body]) => /opacity:\s*0(?:\.0*)?\s*(?:;|$)|visibility:\s*hidden|display:\s*none|position:\s*absolute/.test(body))
      .map(([selector]) => selector)
    expect(hiding).toEqual([])
  })
})
