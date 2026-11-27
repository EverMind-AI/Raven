// @vitest-environment happy-dom
/* The orb stack on the waiting row: how many it draws, what it tells a
   screen reader and a pointer, and how an orb comes and goes. */
import { act, cleanup, render } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { t } from '../../i18n/t'
import { openOverlap, orbsThatFit, SubagentOrbs } from './SubagentOrbs'

import type { SubagentOrb } from './store'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const NOW = 1_000_000
/* Long enough to cross one faked animation frame whatever the offset: with the
   system time set, the fake clock's frame boundary can sit up to 31 ms out. */
const FRAME = 40
const orb = (key: string, name = key, startedAt: number | null = NOW - 1000): SubagentOrb =>
  ({ key, name, preset: null, own: false, startedAt })
const many = (n: number): SubagentOrb[] => Array.from({ length: n }, (_, i) => orb(`k${i}`, `Agent ${i}`))

const orbEls = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('.cp-orb:not(.cp-more)')]
const keyed = (key: string): HTMLElement[] => orbEls().filter((el) => el.dataset.key === key)

/* Layout is faked: happy-dom measures nothing. The stack starts at x 300 and
   the row it sits in ends `room` px later. */
let room = 85
let resized: (() => void) | null = null
const rect = (left: number, right: number) => ({ left, right, width: right - left, top: 0, bottom: 20, height: 20, x: left, y: 0, toJSON: () => ({}) })

beforeEach(() => {
  room = 85
  resized = null
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    return (this.classList.contains('cp-orbs') ? rect(300, 300) : rect(0, 300 + room)) as DOMRect
  })
  vi.stubGlobal('ResizeObserver', class { constructor(cb: () => void) { resized = cb } observe(): void {} disconnect(): void {} })
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'requestAnimationFrame', 'cancelAnimationFrame', 'Date'] })
  vi.setSystemTime(NOW)
})
afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('the orb stack', () => {
  it('shows every orb the row has room for, and folds only the rest', () => {
    room = 85
    render(<SubagentOrbs orbs={many(6)} />)
    expect(orbEls()).toHaveLength(2)
    expect(document.querySelector('.cp-more')?.textContent).toBe('+4')
  })

  it('shows them all, with no fold, when they all fit', () => {
    room = 200
    render(<SubagentOrbs orbs={many(9)} />)
    expect(orbEls()).toHaveLength(9)
    expect(document.querySelector('.cp-more')).toBeNull()
  })

  it('unfolds and folds again as the row widens and narrows', () => {
    room = 85
    render(<SubagentOrbs orbs={many(9)} />)
    expect(orbEls()).toHaveLength(2)
    room = 200
    act(() => { resized?.() })
    expect(orbEls()).toHaveLength(9)
    expect(document.querySelector('.cp-more')).toBeNull()
    room = 60
    act(() => { resized?.() })
    act(() => { vi.advanceTimersByTime(280) })
    expect(orbEls()).toHaveLength(1)
    expect(document.querySelector('.cp-more')?.textContent).toBe('+8')
  })

  it('opens on hover only as far as the row allows', () => {
    room = 104
    render(<SubagentOrbs orbs={many(7)} />)
    const stack = document.querySelector('.cp-orbs') as HTMLElement
    expect(stack.style.getPropertyValue('--ov-open')).toBe('7.8px')
  })

  it('names every running subagent for a screen reader', () => {
    const orbs = many(6)
    render(<SubagentOrbs orbs={orbs} />)
    const stack = document.querySelector('.cp-orbs') as HTMLElement
    expect(stack.getAttribute('role')).toBe('img')
    expect(stack.getAttribute('aria-label')).toBe(
      t('gui.live.orbs_label_n', { n: 6, names: orbs.map((o) => o.name).join(', ') }),
    )
  })

  it('names a lone subagent without pluralising the count', () => {
    render(<SubagentOrbs orbs={[orb('c', 'Codex')]} />)
    const stack = document.querySelector('.cp-orbs') as HTMLElement
    expect(stack.getAttribute('aria-label')).toBe('1 subagent running: Codex')
  })

  it('tips each orb with its name and elapsed time', () => {
    render(<SubagentOrbs orbs={[orb('c', 'Codex', NOW - 75_000)]} />)
    expect(orbEls()[0]?.dataset.tip).toBe('Codex · 1m 15s')
  })

  /* Once the host turn has ended nothing else repaints the waiting row, so
     the stack keeps its own clock for the hover label. */
  it('keeps the elapsed time current while nothing else repaints', () => {
    render(<SubagentOrbs orbs={[orb('c', 'Codex', NOW - 75_000)]} />)
    expect(orbEls()[0]?.dataset.tip).toBe('Codex · 1m 15s')
    act(() => { vi.advanceTimersByTime(2000) })
    expect(orbEls()[0]?.dataset.tip).toBe('Codex · 1m 17s')
  })

  it('lets a finished orb leave before dropping it', () => {
    const { rerender } = render(<SubagentOrbs orbs={[orb('a'), orb('b'), orb('c')]} />)
    act(() => { vi.advanceTimersByTime(FRAME) })
    rerender(<SubagentOrbs orbs={[orb('a'), orb('c')]} />)
    expect(keyed('b')).toHaveLength(1)
    expect(keyed('b')[0]?.hasAttribute('data-leave')).toBe(true)
    act(() => { vi.advanceTimersByTime(280) })
    expect(keyed('b')).toHaveLength(0)
    expect(orbEls().map((el) => el.dataset.key)).toEqual(['a', 'c'])
  })

  it('slides a folded orb in when one leaves', () => {
    room = 120
    const six = many(6)
    const { rerender } = render(<SubagentOrbs orbs={six} />)
    act(() => { vi.advanceTimersByTime(FRAME) })
    expect(keyed('k4')).toHaveLength(0)
    rerender(<SubagentOrbs orbs={six.slice(1)} />)
    expect(keyed('k4')[0]?.hasAttribute('data-enter')).toBe(true)
    expect(document.querySelector('.cp-more')).toBeNull()
    act(() => { vi.advanceTimersByTime(FRAME) })
    expect(keyed('k4')[0]?.hasAttribute('data-enter')).toBe(false)
  })

  it('does not duplicate a key that returns while leaving', () => {
    const { rerender } = render(<SubagentOrbs orbs={[orb('a'), orb('b')]} />)
    rerender(<SubagentOrbs orbs={[orb('a')]} />)
    act(() => { vi.advanceTimersByTime(100) })
    rerender(<SubagentOrbs orbs={[orb('a'), orb('b')]} />)
    expect(keyed('b')).toHaveLength(1)
    expect(keyed('b')[0]?.hasAttribute('data-leave')).toBe(false)
    act(() => { vi.advanceTimersByTime(400) })
    expect(keyed('b')).toHaveLength(1)
  })

  it('draws nothing once the last orb has left', () => {
    const { rerender } = render(<SubagentOrbs orbs={[orb('a')]} />)
    rerender(<SubagentOrbs orbs={[]} />)
    act(() => { vi.advanceTimersByTime(280) })
    expect(document.querySelector('.cp-orbs')).toBeNull()
  })
})

describe('how many orbs fit', () => {
  /* n orbs at 30 px overlapping by 9 take 9 + 21n; the +N chip adds 28.5. */
  it('draws them all while 9 + 21n fits', () => {
    expect(orbsThatFit(7, 200)).toBe(7)
    expect(orbsThatFit(5, 114)).toBe(5)
  })

  it('leaves room for the chip once they do not', () => {
    expect(orbsThatFit(5, 113)).toBe(3)
    expect(orbsThatFit(7, 120)).toBe(3)
  })

  it('keeps one face however narrow the row', () => {
    expect(orbsThatFit(4, 10)).toBe(1)
    expect(orbsThatFit(0, 100)).toBe(0)
  })

  it('opens the hover spread only as far as the room allows', () => {
    expect(openOverlap(7, false, 240)).toBe(-4.5)
    expect(openOverlap(7, false, 104)).toBe(9)
    expect(openOverlap(7, false, 200)).toBeCloseTo(1.67, 2)
    expect(openOverlap(3, true, 200)).toBe(-4.5)
    expect(openOverlap(1, false, 10)).toBe(-4.5)
  })
})
