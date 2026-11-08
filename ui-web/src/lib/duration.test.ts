/* Exact boundary coverage for the compact duration wording shared by islands --
   the TUI's `fmtDuration` behaviour: whole seconds, then minutes with a
   space-separated seconds tail, then hours and minutes. */

import { describe, expect, it } from 'vitest'

import { formatDuration, widestDurations } from './duration'

describe('the shared duration formatter', () => {
  it('reports whole seconds under a minute, truncated rather than rounded', () => {
    expect(formatDuration(0)).toBe('0s')
    expect(formatDuration(999)).toBe('0s')
    expect(formatDuration(1000)).toBe('1s')
    expect(formatDuration(2500)).toBe('2s')
    expect(formatDuration(9999)).toBe('9s')
    expect(formatDuration(59_999)).toBe('59s')
  })

  it('spells minutes and unpadded seconds once a minute is present', () => {
    expect(formatDuration(60_000)).toBe('1m 0s')
    expect(formatDuration(61_000)).toBe('1m 1s')
    expect(formatDuration(65_000)).toBe('1m 5s')
    expect(formatDuration(3_599_000)).toBe('59m 59s')
  })

  it('drops the seconds once an hour is present', () => {
    expect(formatDuration(3_600_000)).toBe('1h 0m')
    expect(formatDuration(3_661_000)).toBe('1h 1m')
    expect(formatDuration(7_320_000)).toBe('2h 2m')
  })
})

describe('the widest spellings up to a duration', () => {
  it('names its own, the instant before its leading figure turned over, and each shorter shape at its widest', () => {
    expect(widestDurations(0)).toEqual(['0s'])
    expect(widestDurations(45_000)).toEqual(['45s'])
    expect(widestDurations(303_000)).toEqual(['5m 3s', '4m 59s', '59s'])
    expect(widestDurations(21_300_000)).toEqual(['5h 55m', '4h 59m', '59s', '9m 59s', '59m 59s'])
  })

  it('leaves no moment of a rising count longer than every spelling of its shape it names', () => {
    /* Digits are tabular, so within one shape a longer spelling is a wider
       one; across shapes the cell takes the widest of those it holds. */
    const shapeOf = (text: string): string => text.replace(/\d/g, '')
    const longest = new Map<string, number>()
    const misses: string[] = []
    for (let t = 0; t <= 400_000; t += t < 20_000 ? 1 : 60) {
      const text = formatDuration(t * 1000)
      longest.set(shapeOf(text), Math.max(longest.get(shapeOf(text)) ?? 0, text.length))
      const held = widestDurations(t * 1000)
      for (const [shape, length] of longest) {
        if (!held.some((copy) => shapeOf(copy) === shape && copy.length >= length)) misses.push(`${t}s: ${shape}`)
      }
    }
    expect(misses).toEqual([])
  })
})
