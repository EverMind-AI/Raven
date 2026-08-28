import { describe, expect, it } from 'vitest'

import { roughWidth, sharedPrefix, trimShared } from './labels'

/* One em per character, so a latin label's width below is 0.6 per character
   and a full-width one's is 1. */
const EM = 1

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

describe('the namespace a set of labels can spare', () => {
  it('leaves every label alone when none of them is long enough to cut', () => {
    const ids = ['run-a12-fetch', 'run-a12-parse']
    expect(trimShared(ids, 20, EM)).toEqual(ids)
  })

  /* The whole graph loses the prefix, not just the label that overflowed:
     labels a reader compares have to be comparable. */
  it('drops the shared prefix from all of them, not only from the long one', () => {
    expect(trimShared(['run-a12-fetch-metadata', 'run-a12-ok'], 12, EM))
      .toEqual(['fetch-metadata', 'ok'])
  })

  /* Nothing is cut to length here. A label that is still too long after the
     prefix goes is handed over whole, and the box it is drawn in ends it --
     at the real width, in the real font, which is not a thing this file can
     know and used to pretend to. */
  it('hands over what is left whole, however long it is', () => {
    expect(trimShared(['run-a12-fetch-metadata-and-normalise', 'run-a12-ok'], 8, EM))
      .toEqual(['fetch-metadata-and-normalise', 'ok'])
  })

  it('leaves labels alone when they share no namespace to give up', () => {
    const ids = ['fetch-metadata-and-normalise', 'parse']
    expect(trimShared(ids, 8, EM)).toEqual(ids)
  })
})

describe('the rough width a label sets', () => {
  /* Rough on purpose: it decides whether a namespace is worth stripping, and
     nothing else. A full-width character takes about an em, a latin one about
     six tenths. */
  it('counts a CJK character as about twice a latin one', () => {
    expect(roughWidth('abcd', 10)).toBeCloseTo(24)
    expect(roughWidth('\u8c03\u7814', 10)).toBeCloseTo(20)
  })

  it('is what decides a graph of CJK labels needs its namespace cut', () => {
    const ids = ['scan-\u4ea7\u54c1\u5b9a\u4f4d\u4e0e\u6280\u672f\u67b6\u6784', 'scan-\u5b9a\u4ef7']
    expect(trimShared(ids, 60, 11)).toEqual(['\u4ea7\u54c1\u5b9a\u4f4d\u4e0e\u6280\u672f\u67b6\u6784', '\u5b9a\u4ef7'])
  })
})
