/* A filled `.mini` button grows to its label the way a plain one does.
 *
 * `.go` names two things in the stylesheet: the composer's Send disc, whose
 * rule fixes it at 30px, and the filled variant of a `.mini` button. The two
 * rules tie on specificity, and `.mini` restated the disc's height, radius,
 * display and colours but not its width -- so a `.mini.go` was 30px wide and
 * only its min-width floor made it any wider. A label wider than that floor
 * (Check again, or a spinner beside Connecting) ran out of both sides of its
 * frame.
 *
 * Pinned here because no DOM test can see it: happy-dom does no layout, so a
 * button whose label overflows it and one that holds it are the same assertion
 * there.
 */

import { describe, expect, it } from 'vitest'

import { decls, rules } from './css.mjs'

describe('the mini button width', () => {
  it('sizes a mini button to its label rather than to the Send disc', () => {
    expect(decls('.mini')?.get('width')).toBe('auto')
  })

  /* A tie on specificity goes to the later rule, so the width above holds only
     while `.mini` is written after the rule it has to override. */
  it('comes after the Send disc rule it overrides', () => {
    const order = rules(null).map(([selector]) => selector)
    expect(order.indexOf('.go')).toBeGreaterThan(-1)
    expect(order.indexOf('.mini')).toBeGreaterThan(order.indexOf('.go'))
  })
})
