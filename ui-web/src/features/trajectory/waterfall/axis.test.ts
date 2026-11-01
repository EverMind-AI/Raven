/* The axis's arithmetic, as properties: gaps collapse, occupied time does not,
 * and a projection round-trips inside an occupied run. */

import { describe, expect, it } from 'vitest'

import { AxisIndex, IDLE_MS, barOf, createAxis, presetWindow, zoomViewport } from './axis'

it('merges overlapping intervals into one occupied run', () => {
  const axis = createAxis([{ start: 0, end: 10 }, { start: 5, end: 20 }], { idleMs: 100 })
  expect(axis.length).toBe(20)
})

it('collapses a gap longer than the idle threshold to the gap width', () => {
  const axis = createAxis(
    [{ start: 0, end: 1000 }, { start: 1000 + IDLE_MS * 10, end: 1000 + IDLE_MS * 10 + 1000 }],
    { gapWidth: 50 },
  )
  expect(axis.length).toBe(1000 + 50 + 1000)
  expect(axis.gaps).toHaveLength(1)
  expect(axis.gaps[0]!.end - axis.gaps[0]!.start).toBe(IDLE_MS * 10)
})

it('keeps a gap shorter than the threshold at its true width', () => {
  const axis = createAxis([{ start: 0, end: 1000 }, { start: 2000, end: 3000 }], { idleMs: IDLE_MS })
  expect(axis.length).toBe(3000)
  expect(axis.gaps).toHaveLength(0)
})

it('a stretch covered by another agent is occupied, not a gap', () => {
  const axis = createAxis(
    [
      { start: 0, end: 1000 },
      { start: 1000 + IDLE_MS * 10, end: 1000 + IDLE_MS * 10 + 1000 },
      { start: 1000, end: 1000 + IDLE_MS * 10 },
    ],
    { idleMs: IDLE_MS },
  )
  expect(axis.gaps).toHaveLength(0)
})

it('project and unproject round-trip inside an occupied run', () => {
  const axis = createAxis([{ start: 0, end: 1000 }])
  expect(axis.unproject(axis.project(420))).toBeCloseTo(420, 6)
})

it('a time inside a collapsed gap maps inside the gap position', () => {
  const axis = createAxis(
    [{ start: 0, end: 1000 }, { start: 1000 + IDLE_MS * 10, end: 1000 + IDLE_MS * 10 + 1000 }],
    { gapWidth: 50 },
  )
  const p = axis.project(1000 + IDLE_MS * 5)
  expect(p).toBeGreaterThan(1000)
  expect(p).toBeLessThan(1050)
})

it('zoomViewport keeps the anchor at the same relative position', () => {
  const next = zoomViewport({ from: 0, to: 100 }, 2, 25, 100)
  expect(next.from).toBeCloseTo(12.5, 6)
  expect(next.to).toBeCloseTo(62.5, 6)
})

it('barOf clips a bar to the viewport and returns null outside it', () => {
  const axis = createAxis([{ start: 0, end: 1000 }])
  expect(barOf(axis, { from: 0, to: 1000 }, 200, 400)).toEqual({ left: 0.2, width: 0.2 })
  expect(barOf(axis, { from: 500, to: 1000 }, 0, 100)).toBeNull()
})

it('an axis fed a page at a time equals the one built from the whole list', () => {
  const spans = [
    { start: 0, end: 1000 },
    { start: 1000 + IDLE_MS * 5, end: 1000 + IDLE_MS * 5 + 800 },
    { start: 400, end: 900 },
    { start: 1000 + IDLE_MS * 2, end: 1000 + IDLE_MS * 2 + 100 },
  ]
  const whole = createAxis(spans)
  const index = new AxisIndex()
  index.add([spans[0]!, spans[2]!])
  index.add([spans[1]!])
  index.add([spans[3]!])
  const grown = index.axis()
  expect(grown.length).toBe(whole.length)
  expect(grown.gaps).toEqual(whole.gaps)
  for (const time of [0, 500, 1200, 1000 + IDLE_MS * 3]) {
    expect(grown.project(time)).toBeCloseTo(whole.project(time), 6)
  }
})

it('an insert that bridges two runs merges them into one', () => {
  const index = new AxisIndex()
  index.add([{ start: 0, end: 1000 }, { start: 2000, end: 3000 }])
  index.add([{ start: 900, end: 2100 }])
  const axis = index.axis()
  expect(axis.gaps).toHaveLength(0)
  expect(axis.length).toBe(3000)
})

describe('a preset window', () => {
  const HOUR = 3_600_000

  it('takes its length about the middle of the window on screen', () => {
    const axis = createAxis([{ start: 0, end: 10 * HOUR }])
    expect(presetWindow(axis, { from: 0, to: axis.length }, HOUR)).toEqual({ start: 4.5 * HOUR, end: 5.5 * HOUR })
  })

  it('stays inside the session, against whichever end it would cross', () => {
    const axis = createAxis([{ start: 0, end: 10 * HOUR }])
    expect(presetWindow(axis, { from: 0, to: axis.project(HOUR) }, 3 * HOUR)).toEqual({ start: 0, end: 3 * HOUR })
    expect(presetWindow(axis, { from: axis.project(9 * HOUR), to: axis.length }, 3 * HOUR)).toEqual({ start: 7 * HOUR, end: 10 * HOUR })
  })

  it('moves a middle that falls in an idle fold onto the nearer side of it', () => {
    const axis = createAxis([{ start: 0, end: HOUR }, { start: 6 * HOUR, end: 7 * HOUR }])
    const fold = axis.gaps[0]!
    /* Both middles land inside the 15 s the fold is drawn as: 1.5 h into the
       idle five, then 3.5 h. */
    const early = { from: fold.from - 10_000, to: fold.to + 4000 }
    expect(presetWindow(axis, early, HOUR)).toEqual({ start: 0.5 * HOUR, end: 1.5 * HOUR })
    const late = { from: fold.from - 4000, to: fold.to + 10_000 }
    expect(presetWindow(axis, late, HOUR)).toEqual({ start: 5.5 * HOUR, end: 6.5 * HOUR })
  })

  it('is the whole session when the session is no longer than the preset', () => {
    const axis = createAxis([{ start: 0, end: HOUR }])
    expect(presetWindow(axis, { from: 0, to: axis.length }, 3 * HOUR)).toBeNull()
  })
})
