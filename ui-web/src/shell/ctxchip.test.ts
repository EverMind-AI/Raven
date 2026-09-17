// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { _resetForTests, draw, set } from './ctxchip'
import { resetShell, setShell } from './bridge'
import { mountPageRoot } from '../test/pageRoot'

import type { Shell } from './bridge'

/* The catalogue's own shape for the tooltip, so the test reads what a reader
   would see rather than a key. */
const fake: Shell = {
  T: (key, vars) =>
    key === 'gui.ctx.tip' && vars
      ? `context ${vars.used}/${vars.max} ${vars.pct}%`
      : key,
  confirmAsk: (_t, _b, _l, fn) => fn(),
  showPage: () => {},
}

const chip = (): HTMLElement => document.getElementById('ctxChip') as HTMLElement
const fg = (): Element => chip().querySelector('.fg') as Element
const offset = (): number => Number(fg().getAttribute('stroke-dashoffset'))
const RING = 47.75

let unmount = (): void => {}

/* The chip is the page root's now (src/chrome/CtxChip.tsx), so the markup gives
   the container page.html carries and the root renders the button into it --
   radius included, since half the offset arithmetic below is that radius.

   The store's reset is what a fresh document used to do for free: the window it
   was last told is module state and `set` deliberately keeps it, so there is no
   argument that clears it back to "no window known yet". */
beforeEach(() => {
  _resetForTests()
  setShell(fake)
  document.body.innerHTML = '<div class="dock"></div>'
  unmount = mountPageRoot()
})

afterEach(() => {
  unmount()
  unmount = () => {}
  _resetForTests()
  resetShell()
  document.body.innerHTML = ''
})

describe('the context ring', () => {
  it('stays hidden until a real window is known', () => {
    set(1200, 0)
    expect(chip().hidden).toBe(true)
  })

  it('shows itself once a window arrives, and says both numbers', () => {
    set(50_000, 200_000)
    expect(chip().hidden).toBe(false)
    expect(chip().dataset.tip).toBe('context 50k/200k 25%')
  })

  it('gives the mouse and a screen reader the same sentence', () => {
    set(50_000, 200_000)
    expect(chip().getAttribute('aria-label')).toBe(chip().dataset.tip)
  })

  it('spends the dash in proportion to the fill', () => {
    set(0, 200_000)
    expect(offset()).toBeCloseTo(RING, 5)
    set(100_000, 200_000)
    expect(offset()).toBeCloseTo(RING / 2, 5)
    set(200_000, 200_000)
    expect(offset()).toBeCloseTo(0, 5)
  })

  it('warms at 70 and goes hot at 90, and cools back down again', () => {
    set(138_000, 200_000) // 69%
    expect([...chip().classList]).not.toContain('warm')
    set(140_000, 200_000) // 70%
    expect([...chip().classList]).toContain('warm')
    set(180_000, 200_000) // 90%
    expect([...chip().classList]).toContain('hot')
    expect([...chip().classList]).not.toContain('warm')
    set(100_000, 200_000) // 50%
    expect([...chip().classList]).not.toContain('warm')
    expect([...chip().classList]).not.toContain('hot')
  })

  it('cannot draw a ring past full, however much the turn reports', () => {
    set(400_000, 200_000)
    expect(chip().dataset.tip).toBe('context 400k/200k 100%')
    expect(offset()).toBeCloseTo(0, 5)
  })

  it('abbreviates a thousand once, and drops the decimal at ten', () => {
    set(1500, 9600)
    expect(chip().dataset.tip).toBe('context 1.5k/9.6k 16%')
    set(12_000, 200_000)
    expect(chip().dataset.tip).toBe('context 12k/200k 6%')
    set(900, 9600)
    expect(chip().dataset.tip).toBe('context 900/9.6k 9%')
  })

  it('keeps the window it was told when a later report omits it', () => {
    set(50_000, 200_000)
    set(60_000, 0)
    expect(chip().dataset.tip).toBe('context 60k/200k 30%')
  })

  it('draws nothing and throws nothing when the chip is not in the page', () => {
    set(50_000, 200_000)
    document.body.innerHTML = ''
    expect(() => draw()).not.toThrow()
  })
})
