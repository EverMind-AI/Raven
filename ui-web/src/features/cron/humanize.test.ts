/* What a schedule reads as. The words are the message catalogue's; what this
   pins is that the punctuation holding two of them together is the message catalogue's
   too --
   a list joined with the mark of one language reads as that language whatever
   the words around it say. */

import { afterEach, describe, expect, it } from 'vitest'

import { setCode } from '../../i18n/t'
import { cronExprHuman } from './humanize'

/* scripts/check_source_language.py's CJK_RUN, which is also what
   scripts/gates/first-frame-literals.test.mjs counts. Spelled in escapes: the
   first range opens on the ideographic space, which is whitespace to a reader
   of this file and to the lint rule that reads it. */
const CJK = /[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uac00-\ud7af\uf900-\ufaff\uff00-\uffef]/

afterEach(() => { setCode('en') })

describe('a schedule in words', () => {
  it('holds a list of weekdays together with nothing from the other language', () => {
    setCode('en')

    const said = cronExprHuman('0 9 * * 1,3,5')

    expect(said).toBe('Mon, Wed, Fri 09:00')
    expect(said).not.toMatch(CJK)
  })

  it('holds a list of times together with nothing from the other language', () => {
    setCode('en')

    const said = cronExprHuman('30 9,17 * * *')

    expect(said).toBe('Every day 09:30, 17:30')
    expect(said).not.toMatch(CJK)
  })

  /* The other half of the same rule: the mark is the language's, so the reader
     who is in that language keeps the one their language writes. */
  it('keeps the ideographic comma for a reader who is in that language', () => {
    setCode('zh')

    expect(cronExprHuman('0 9 * * 1,3,5')).toMatch(CJK)
  })
})
