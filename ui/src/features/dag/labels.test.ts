import { describe, expect, it } from 'vitest'

import { fitLabels, sharedPrefix } from './labels'

/* A monospace measurer, which is what the labels are drawn in: one unit per
   character, so a width is a character count and the cases below read as one. */
const mono = (per = 1) => (s: string): number => s.length * per

describe('the shared prefix of a set of node ids', () => {
  it('is the common head cut back to a separator', () => {
    expect(sharedPrefix(['run-a12-fetch', 'run-a12-parse'])).toBe('run-a12-')
  })

  it('is empty for one id, since one id tells itself apart', () => {
    expect(sharedPrefix(['run-a12-fetch'])).toBe('')
  })

  it('is empty when the ids share nothing', () => {
    expect(sharedPrefix(['fetch', 'parse'])).toBe('')
  })

  /* Mid-word is worse than long: `read_clay` and `read_11x` share `read_1`
     going by characters, and cutting there leaves `1x` beside `clay`. */
  it('will not cut mid-word', () => {
    expect(sharedPrefix(['fetchone', 'fetchtwo'])).toBe('')
    expect(sharedPrefix(['read_clay', 'read_11x'])).toBe('read_')
  })

  /* A prefix that swallows a whole label leaves nothing to read. */
  it('is empty when it would empty a label', () => {
    expect(sharedPrefix(['a-b-', 'a-b-c'])).toBe('')
  })
})

describe('fitting node labels to the room they have', () => {
  it('leaves every label alone when they all fit', () => {
    const ids = ['run-a12-fetch', 'run-a12-parse']
    expect(fitLabels(ids, 20, mono())).toEqual(ids)
  })

  /* The whole graph loses the prefix, not just the label that overflowed:
     labels a reader compares have to be comparable. */
  it('drops the shared prefix from all of them, not only from the long one', () => {
    const out = fitLabels(['run-a12-fetch-metadata', 'run-a12-ok'], 14, mono())
    expect(out).toEqual(['fetch-metadata', 'ok'])
  })

  it('truncates with an ellipsis once the prefix is not enough', () => {
    const out = fitLabels(['run-a12-fetch-metadata', 'run-a12-ok'], 8, mono())
    expect(out).toEqual(['fetch-m…', 'ok'])
  })

  /* Measuring what is shown rather than the next candidate. At avail 14 the cut
     label is exactly 14 wide: it fits, so it keeps its last character instead of
     paying for an ellipsis it does not need. */
  it('keeps a label that exactly fits', () => {
    expect(fitLabels(['aaaaaaaaaaaaaa', 'a-bb'], 14, mono())).toEqual(['aaaaaaaaaaaaaa', 'a-bb'])
  })

  it('never truncates below one character plus the ellipsis', () => {
    expect(fitLabels(['abcdef', 'ab'], 0, mono())).toEqual(['a…', 'a…'])
  })

  /* Widths, not character counts: a proportional face measures per label, and
     the truncation has to follow the measurement rather than divide a total. */
  it('follows the measurer rather than the character count', () => {
    const wide = (s: string, i: number): number => s.length * (i === 0 ? 4 : 1)
    const out = fitLabels(['ab-cdef', 'ab-cdef'], 12, wide)
    expect(out[1]).toBe('cdef')
    expect((out[0] as string).endsWith('…')).toBe(true)
    expect((out[0] as string).length).toBeLessThan(4)
  })

  it('is a no-op for an empty graph', () => {
    expect(fitLabels([], 10, mono())).toEqual([])
  })
})
