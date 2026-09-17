/* Exact boundary coverage for the compact duration wording shared by islands. */

import { describe, expect, it } from 'vitest'

import { formatDuration } from './duration'

describe('the shared duration formatter', () => {
  it('keeps tenths only while the elapsed time is under ten seconds', () => {
    expect(formatDuration(1000)).toBe('1.0s')
    expect(formatDuration(9999)).toBe('10.0s')
    expect(formatDuration(10000)).toBe('10s')
  })

  it('pads the smaller fields once minutes or hours are present', () => {
    expect(formatDuration(59_499)).toBe('59s')
    expect(formatDuration(59_500)).toBe('1m00s')
    expect(formatDuration(61_000)).toBe('1m01s')
    expect(formatDuration(3_661_000)).toBe('1h01m01s')
  })
})
