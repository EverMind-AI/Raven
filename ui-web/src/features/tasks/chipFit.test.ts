import { describe, expect, it } from 'vitest'

import { fitChips } from './chipFit'

describe('fitChips', () => {
  it('shows every chip when they already fit in the rows', () => {
    expect(fitChips([100, 100, 100], 310, 5, 40, 1)).toBe(3)
    expect(fitChips([100, 100, 100, 100], 205, 5, 40, 2)).toBe(4)
  })

  it('keeps room on the last row for the +N chip', () => {
    /* Two rows of two 100px chips each; the fifth wraps. Folded, the second
       row has to carry the +N chip, so it keeps one file chip, not two. */
    expect(fitChips([100, 100, 100, 100, 100], 205, 5, 40, 2)).toBe(3)
  })

  it('packs short names further than long ones in the same width', () => {
    const short = Array(12).fill(60)
    const long = Array(12).fill(180)
    expect(fitChips(short, 400, 5, 40, 2)).toBeGreaterThan(fitChips(long, 400, 5, 40, 2))
  })

  it('gives a chip wider than the strip a line of its own', () => {
    expect(fitChips([500, 50, 50], 300, 5, 40, 2)).toBe(3)
    expect(fitChips([500, 50, 500], 300, 5, 40, 2)).toBe(2)
  })

  it('counts the gap between chips, not only their widths', () => {
    /* 3 x 100 is exactly 300, but two 5px gaps push the third over. */
    expect(fitChips([100, 100, 100, 100], 300, 5, 40, 1)).toBe(2)
    expect(fitChips([100, 100, 100], 310, 5, 40, 1)).toBe(3)
  })
})
