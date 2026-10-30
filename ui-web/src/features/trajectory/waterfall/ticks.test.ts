// @vitest-environment happy-dom
/* The ruler's marks in wall-clock time. Instants are built from local fields,
 * so every expectation holds in whatever zone the suite runs in. */

import { describe, expect, it } from 'vitest'

import * as lang from '../../../state/lang'
import { createAxis } from './axis'
import { LABEL_PX, clockTicks, tickLabel } from './ticks'

const at = (hour: number, minute = 0, day = 28): number => new Date(2026, 9, day, hour, minute).getTime()
const hhmm = (ms: number): string => {
  const d = new Date(ms)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

describe('the clock ruler', () => {
  it('marks a three-hour window on the quarter hours, the whole hours major', () => {
    const axis = createAxis([{ start: at(9, 0), end: at(19, 0) }])
    const view = { from: axis.project(at(10, 12)), to: axis.project(at(13, 12)) }
    const ticks = clockTicks(axis, view, 1200)
    expect(ticks.map((tick) => hhmm(tick.time))).toEqual([
      '10:15', '10:30', '10:45', '11:00', '11:15', '11:30', '11:45', '12:00', '12:15', '12:30', '12:45', '13:00',
    ])
    expect(ticks.filter((tick) => tick.major).map((tick) => hhmm(tick.time))).toEqual(['11:00', '12:00', '13:00'])
    expect(ticks[3]!.left).toBeCloseTo(48 / 180, 6)
  })

  it('takes a longer step when the same window has less room', () => {
    const axis = createAxis([{ start: at(9, 0), end: at(19, 0) }])
    const view = { from: axis.project(at(10, 0)), to: axis.project(at(13, 0)) }
    const ticks = clockTicks(axis, view, 400)
    expect(ticks.map((tick) => hhmm(tick.time))).toEqual(['10:00', '11:00', '12:00', '13:00'])
  })

  it('puts no mark inside a folded idle stretch', () => {
    const axis = createAxis([{ start: at(9, 0), end: at(10, 0) }, { start: at(15, 0), end: at(16, 0) }])
    const ticks = clockTicks(axis, { from: 0, to: axis.length }, 900)
    const times = ticks.map((tick) => tick.time)
    expect(times.length).toBeGreaterThan(2)
    expect(times.every((time) => time <= at(10, 0) || time >= at(15, 0))).toBe(true)
  })

  it('puts no mark inside a fold even with the window zoomed onto it', () => {
    /* Zoomed in, the fold fills the ruler, and a mark inside it would have all
       the room it wants -- and name an instant nothing ran in. */
    const axis = createAxis([{ start: at(9, 0), end: at(10, 0) }, { start: at(15, 0), end: at(16, 0) }])
    const fold = axis.gaps[0]!
    const ticks = clockTicks(axis, { from: fold.from - 1000, to: fold.to + 1000 }, 1200)
    expect(ticks.length).toBeGreaterThan(0)
    expect(ticks.every((tick) => tick.time <= at(10, 0) || tick.time >= at(15, 0))).toBe(true)
  })

  it('keeps two marks a fold has pulled together from printing over each other', () => {
    const axis = createAxis([{ start: at(9, 0), end: at(10, 0) }, { start: at(15, 0), end: at(16, 0) }])
    const width = 900
    const ticks = clockTicks(axis, { from: 0, to: axis.length }, width)
    expect(ticks.length).toBeGreaterThan(2)
    const gaps = ticks.slice(1).map((tick, index) => (tick.left - ticks[index]!.left) * width)
    expect(Math.min(...gaps)).toBeGreaterThanOrEqual(LABEL_PX * 0.6)
  })

  it('marks midnight major, so a window across two days shows where one ends', () => {
    const axis = createAxis([{ start: at(20, 0), end: at(4, 0, 29) }])
    const ticks = clockTicks(axis, { from: 0, to: axis.length }, 900)
    const midnight = ticks.find((tick) => tick.time === at(0, 0, 29))
    expect(midnight?.major).toBe(true)
    expect(ticks.filter((tick) => tick.major)).toHaveLength(1)
  })

  it('steps in seconds once the window is a minute long', () => {
    const axis = createAxis([{ start: at(10, 0), end: at(10, 1) }])
    const ticks = clockTicks(axis, { from: 0, to: axis.length }, 1200)
    expect(ticks).toHaveLength(13)
    expect(ticks[1]!.time - ticks[0]!.time).toBe(5000)
    /* On a seconds ruler the whole minutes are the major marks. */
    expect(ticks.filter((tick) => tick.major).map((tick) => tick.time)).toEqual([at(10, 0), at(10, 1)])
  })

  it('labels a mark as finely as its step: minutes, seconds, tenths, and the day at midnight', () => {
    lang.set('en')
    const ms = (h: number, m: number, s: number, tenths = 0): number => new Date(2026, 9, 28, h, m, s, tenths * 100).getTime()
    expect(tickLabel(ms(10, 30, 0), 15 * 60_000)).toBe('10:30')
    expect(tickLabel(ms(10, 0, 5), 5000)).toBe('10:00:05')
    expect(tickLabel(ms(10, 0, 5, 2), 200)).toBe('10:00:05.2')
    expect(tickLabel(at(0, 0, 29), 60 * 60_000)).toBe('Oct 29')
    expect(tickLabel(at(0, 0, 29), 15 * 60_000)).toBe('Oct 29')
    lang._resetForTests()
  })

  it('marks a long, mostly idle session with a handful of marks, not one per idle minute', () => {
    const threeDays = 3 * 24 * 3_600_000
    const axis = createAxis([{ start: at(9, 0), end: at(9, 20) }, { start: at(9, 0) + threeDays, end: at(9, 20) + threeDays }])
    const ticks = clockTicks(axis, { from: 0, to: axis.length }, 1200)
    expect(ticks.length).toBeGreaterThan(2)
    expect(ticks.length).toBeLessThanOrEqual(12)
    expect(ticks.every((tick) => tick.left >= 0 && tick.left <= 1)).toBe(true)
  })

  it('has nothing to mark on an empty window', () => {
    const axis = createAxis([{ start: at(9, 0), end: at(10, 0) }])
    expect(clockTicks(axis, { from: 5, to: 5 }, 900)).toEqual([])
  })
})
