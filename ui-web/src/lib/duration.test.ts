/* Exact boundary coverage for the compact duration wording shared by islands --
   matched to the prototype's own `fmtDur`: whole seconds, then minutes with a
   zero-padded seconds tail, and nothing past that (no decimal, no hour tier). */

import { describe, expect, it } from 'vitest'

import { formatDuration } from './duration'

describe('the shared duration formatter', () => {
  it('reports whole seconds under a minute, rounded rather than truncated', () => {
    expect(formatDuration(1000)).toBe('1s')
    expect(formatDuration(9999)).toBe('10s')
    expect(formatDuration(10000)).toBe('10s')
  })

  it('pads the seconds once minutes are present, with no hour tier', () => {
    expect(formatDuration(59_499)).toBe('59s')
    expect(formatDuration(59_500)).toBe('1m00s')
    expect(formatDuration(61_000)).toBe('1m01s')
    expect(formatDuration(3_661_000)).toBe('61m01s')
  })
})
