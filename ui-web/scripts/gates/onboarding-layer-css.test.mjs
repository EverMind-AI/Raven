/* The confirm veil against the onboarding layer.
 *
 * The wizard is a fixed, opaque surface at the top of the overlay ladder, and
 * the confirm dialog opens in the page's veil, which sits far below it. A
 * question the wizard asks -- a stale agent's migrate -- opened behind the
 * wizard: the veil went up, the focus went into a button nobody could see, and
 * the connect it guarded was dead. No DOM test can see this -- paint order is
 * not in the DOM -- so the stylesheet is where it is pinned: while the wizard's
 * host is shown, the veil stands one above it and still under the toast.
 */

import { describe, expect, it } from 'vitest'

import { decls, rule } from './css.mjs'

const token = (name) => {
  const root = decls(':root')
  const raw = root && root.get(name)
  return raw ? Number(raw) : null
}

describe('the confirm veil while the wizard is open', () => {
  it('rests under the onboarding layer, which is why the lift exists', () => {
    expect(token('--z-veil')).toBeLessThan(token('--z-onboarding'))
    expect(rule('.veil')).toMatch(/z-index:\s*var\(--z-veil\)/)
    expect(rule('#onb')).toMatch(/z-index:\s*var\(--z-onboarding\)/)
  })

  it('is lifted one above the wizard only while the host is shown, and stays under the toast', () => {
    const lifted = rule('body:has(#onb:not([hidden])) #veil')
    expect(lifted).toBeTruthy()
    expect(lifted).toMatch(/z-index:\s*calc\(var\(--z-onboarding\)\s*\+\s*1\)/)
    expect(token('--z-onboarding') + 1).toBeLessThan(token('--z-toast'))
  })
})

describe('the model picker while the wizard is open', () => {
  it('rests at the picker layer, under the wizard', () => {
    expect(token('--z-picker')).toBeLessThan(token('--z-onboarding'))
    expect(rule('.mpick')).toMatch(/z-index:\s*var\(--z-picker\)/)
  })

  it('is lifted one above the wizard only while the host is shown', () => {
    /* The roles card's pill opens the composer's picker, a body-level
       surface; the wizard's model step is the one place that pill sits inside
       #onb, and the picker opened behind it there. */
    const lifted = rule('body:has(#onb:not([hidden])) .mpick')
    expect(lifted).toBeTruthy()
    expect(lifted).toMatch(/z-index:\s*calc\(var\(--z-onboarding\)\s*\+\s*1\)/)
  })
})
