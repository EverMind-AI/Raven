// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setCode } from '../i18n/t'
import { mountPageRoot } from '../test/pageRoot'
import * as confirmStore from './confirm'
import { _resetForTests, clear, draw, get, set, toggle } from './ctxWindow'
import * as pageStore from './page'

vi.spyOn(pageStore, 'show').mockImplementation(() => {})
vi.spyOn(confirmStore, 'ask').mockImplementation((_t, _b, _l, fn) => fn())

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
  resetTranslator()
  setCode('en')
  _resetForTests()
  document.body.innerHTML = '<div class="dock"></div>'
  unmount = mountPageRoot()
})

afterEach(() => {
  unmount()
  unmount = () => {}
  _resetForTests()
  resetTranslator()
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
    expect(chip().dataset.tip).toBe('Context 50k/200k 25%')
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
    expect(chip().dataset.tip).toBe('Context 400k/200k 100%')
    expect(offset()).toBeCloseTo(0, 5)
  })

  /* The ring clamps at full, and the popover says the same percentage beside
     the same headline: one derivation, so a turn that reports more than the
     window cannot be 100% on the bar and 200% in the card. */
  it('carries the clamped percentage both the ring and the popover say', () => {
    set(400_000, 200_000)
    expect(get().pct).toBe(100)
    set(100_000, 200_000)
    expect(get().pct).toBe(50)
  })

  it('abbreviates a thousand once, and drops the decimal at ten', () => {
    set(1500, 9600)
    expect(chip().dataset.tip).toBe('Context 1.5k/9.6k 16%')
    set(12_000, 200_000)
    expect(chip().dataset.tip).toBe('Context 12k/200k 6%')
    set(900, 9600)
    expect(chip().dataset.tip).toBe('Context 900/9.6k 9%')
  })

  it('keeps the window it was told when a later report omits it', () => {
    set(50_000, 200_000)
    set(60_000, 0)
    expect(chip().dataset.tip).toBe('Context 60k/200k 30%')
  })

  it('draws nothing and throws nothing when the chip is not in the page', () => {
    set(50_000, 200_000)
    document.body.innerHTML = ''
    expect(() => draw()).not.toThrow()
  })
})

describe('the window the popover reads', () => {
  it('carries the parts a turn divided it into', () => {
    set(175_600, 200_000, { system: 4_200, tools: 12_800, conversation: 108_600 })
    expect(get().parts).toEqual({ system: 4_200, tools: 12_800, conversation: 108_600, files: 0, memory: 0, skills: 0 })
    expect(get().used).toBe(175_600)
    expect(get().max).toBe(200_000)
  })

  it('has no parts until a turn divides the window', () => {
    set(175_600, 200_000)
    expect(get().parts).toBe(null)
  })

  it('keeps the parts it was told when a later report brings none', () => {
    set(175_600, 200_000, { system: 4_200, tools: 12_800, conversation: 108_600 })
    set(180_000, 200_000)
    expect(get().parts).toEqual({ system: 4_200, tools: 12_800, conversation: 108_600, files: 0, memory: 0, skills: 0 })
  })

  it('drops the whole reading when the conversation it describes is left', () => {
    set(175_600, 200_000, { system: 4_200, tools: 12_800, conversation: 108_600 })
    toggle()
    clear()
    expect(get().parts).toBe(null)
    expect(get().used).toBe(0)
    expect(get().max).toBe(0)
    expect(get().pct).toBe(0)
    expect(get().open).toBe(false)
    /* The next conversation's report brings none, and must not bring these back. */
    set(12_000, 64_000)
    expect(get().parts).toBe(null)
    expect(get().used).toBe(12_000)
    expect(get().pct).toBe(19)
  })

  it('opens and closes the popover on the ring\'s click', () => {
    set(175_600, 200_000)
    expect(get().open).toBe(false)
    toggle()
    expect(get().open).toBe(true)
    toggle()
    expect(get().open).toBe(false)
  })

  it('has no popover to open while no window is known', () => {
    toggle()
    expect(get().open).toBe(false)
  })

  it('shuts the popover when the window goes away', () => {
    set(175_600, 200_000)
    toggle()
    expect(get().open).toBe(true)
    _resetForTests()
    draw()
    expect(get().open).toBe(false)
  })
})
