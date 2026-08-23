// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { Shell } from './bridge'

/* Imported per case rather than once for the file. The writer owns its two
   numbers, and `set` deliberately keeps the window it was last told -- so
   there is no argument that clears `max` back to nothing, and a shared module
   would carry one case's window into the next. A fresh module is the only
   honest way to start from "no window known yet". */
let draw: (typeof import('./ctxchip'))['draw']
let set: (typeof import('./ctxchip'))['set']

/* The catalogue's own shape for the tooltip, so the test reads what a reader
   would see rather than a key. */
function install(): void {
  const fake: Shell = {
    T: (key, vars) =>
      key === 'gui.ctx.tip' && vars
        ? `context ${vars.used}/${vars.max} ${vars.pct}%`
        : key,
    toast: () => {},
    menuAt: () => {},
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: () => {},
  }
  window.RavenShell = fake
}

const chip = (): HTMLElement => document.getElementById('ctxChip') as HTMLElement
const fg = (): Element => chip().querySelector('.fg') as Element
const offset = (): number => Number(fg().getAttribute('stroke-dashoffset'))
const RING = 47.75

beforeEach(async () => {
  vi.resetModules()
  ;({ draw, set } = await import('./ctxchip'))
  install()
  /* page.html's own markup for the chip, r included -- the radius is half of
     the offset arithmetic this file asserts on. */
  document.body.innerHTML =
    '<button class="chip ctx" id="ctxChip" hidden>' +
    '<svg class="ring" viewBox="0 0 20 20" aria-hidden="true">' +
    '<circle class="bg" cx="10" cy="10" r="7.6"></circle>' +
    '<circle class="fg" cx="10" cy="10" r="7.6"></circle>' +
    '</svg></button>'
})

afterEach(() => {
  delete window.RavenShell
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
