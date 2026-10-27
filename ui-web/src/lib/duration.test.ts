/* Exact boundary coverage for the compact duration wording shared by islands --
   the TUI's `fmtDuration` behaviour: whole seconds, then minutes with a
   space-separated seconds tail, then hours and minutes. */

import { describe, expect, it } from 'vitest'

import { formatDuration } from './duration'

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
