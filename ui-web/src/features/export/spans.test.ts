import { describe, expect, it } from 'vitest'

import { segments } from './spans'

const hits = (text: string, spans: number[][]): string[] => segments(text, spans).filter((p) => p.hit).map((p) => p.text)
const whole = (text: string, spans: number[][]): string => segments(text, spans).map((p) => p.text).join('')

describe('cutting a context at code points', () => {
  it('marks exactly the hit after an astral character', () => {
    const text = '\u{1F600} token'
    expect(hits(text, [[2, 7]])).toEqual(['token'])
    expect(whole(text, [[2, 7]])).toBe(text)
  })

  it('counts several astral characters before a hit, and several hits in one line', () => {
    const text = '\u{1F600}\u{1F680}\u{1D11E} a=SECRET b=OTHER'
    const spans = [[6, 12], [15, 20]]
    expect(hits(text, spans)).toEqual(['SECRET', 'OTHER'])
    expect(whole(text, spans)).toBe(text)
  })

  it('merges overlapping and touching hits, in any order, without repeating a character', () => {
    const text = 'abcdefgh'
    expect(segments(text, [[4, 6], [1, 3], [2, 4]])).toEqual([
      { text: 'a', hit: false }, { text: 'bcdef', hit: true }, { text: 'gh', hit: false },
    ])
    expect(whole(text, [[0, 8], [3, 5]])).toBe(text)
  })

  it('drops spans that are not whole numbers inside the text, and clamps the ones that run over', () => {
    const text = 'abc'
    expect(hits(text, [[2, 1], [1.5, 2], [-3, -1], [5, 9], [1]])).toEqual([])
    expect(hits(text, [[-2, 1], [2, 99]])).toEqual(['a', 'c'])
    expect(whole(text, [[-2, 1], [2, 99]])).toBe(text)
  })

  it('gives back the text as one plain piece with no hits, empty text included', () => {
    expect(segments('plain', [])).toEqual([{ text: 'plain', hit: false }])
    expect(segments('', [[0, 1]])).toEqual([{ text: '', hit: false }])
  })
})
