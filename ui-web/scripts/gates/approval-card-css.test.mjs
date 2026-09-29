/* The approval card's answers cannot be pushed out of it.
 *
 * The card is capped at 300px and hides what overflows it, so its layout
 * decides whether the buttons of a blocking prompt can be clipped. They are
 * safe only while three rules hold together: the body between the question
 * and the answers is the part that gives way (it may shrink and scrolls), the
 * answers row never shrinks, and the evidence has a cap of its own rather than
 * being the one child that shrinks. Drop any of them and a long why sentence
 * with a long suggested rule, on a narrow window, puts the answers under the
 * card's bottom edge.
 *
 * Pinned here rather than in a DOM test because happy-dom does no layout: a
 * clipped row and a visible one are the same tree. That the row is a sibling
 * of the body and not inside it is the DOM half, asserted in approve.test.ts.
 */

import { describe, expect, it } from 'vitest'

import { decls } from './css.mjs'

describe('the approval card layout', () => {
  it('caps the card and hides its overflow, which is what makes the rest matter', () => {
    const card = decls('.csheet.perm')
    expect(card?.get('max-height')).toBe('300px')
    expect(card?.get('overflow')).toBe('hidden')
  })

  it('lets the body shrink and scroll', () => {
    const body = decls('.csheet.perm .body')
    expect(body?.get('min-height')).toBe('0')
    expect(body?.get('overflow-y')).toBe('auto')
    expect(body?.get('flex')).toBe('1')
  })

  it('never shrinks the answers row', () => {
    expect(decls('.csheet.perm .cp-acts')?.get('flex')).toBe('none')
  })

  it('caps the evidence on its own instead of letting it collapse', () => {
    const what = decls('.csheet.perm .what')
    expect(what?.get('flex')).toBe('none')
    expect(what?.get('max-height')).toBe('140px')
  })
})
