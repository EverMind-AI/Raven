// @vitest-environment happy-dom
import { afterEach, describe, expect, it } from 'vitest'

import { _resetForTests, forget, read, slot, write } from './sheetDrafts'

afterEach(() => {
  _resetForTests()
})

describe('the sheet drafts', () => {
  it('keys a draft by the conversation and the question', () => {
    expect(slot('a', 'q1')).toBe('a|q1')
    /* Two questions in one conversation cannot share a slot: the second would
       open holding what the reader typed into the first. */
    expect(slot('a', 'q2')).not.toBe(slot('a', 'q1'))
    /* Nor can two conversations, which is the case the rack exists for. */
    expect(slot('b', 'q1')).not.toBe(slot('a', 'q1'))
  })

  /* A request that predates the id field has one slot per conversation, which
     is admissible only because at most one of these sheets is pending there. */
  it('falls back to one slot per conversation without a question id', () => {
    expect(slot('a')).toBe('a|(anon)')
    expect(slot('a', '')).toBe('a|(anon)')
  })

  it('reads an empty draft for a sheet nobody has typed into', () => {
    expect(read(slot('a', 'q1'))).toEqual({})
  })

  it('overwrites a field with what was written last', () => {
    const key = slot('a', 'q1')
    write(key, { steps: [{ text: 'hold on' }] })
    write(key, { steps: [{ text: 'changed' }] })
    expect(read(key)).toEqual({ steps: [{ text: 'changed' }] })
  })

  /* Which question the reader is on and what they filled in are written at
     different moments, so one must not drop the other. */
  it('keeps the fields it was not handed', () => {
    const key = slot('a', 'q1')
    write(key, { steps: [{ picked: ['debug'] }, { skipped: true }] })
    write(key, { reached: 1, step: 1 })
    expect(read(key)).toEqual({
      reached: 1, step: 1, steps: [{ picked: ['debug'] }, { skipped: true }],
    })
  })

  /* An emptied field is a decision, not an absence: an answer the reader
     deleted must come back deleted after a conversation switch, not as the
     text they had removed. */
  it('records an emptied field rather than dropping it', () => {
    const key = slot('a', 'q1')
    write(key, { steps: [{ text: 'draft' }] })
    write(key, { steps: [{ text: '' }] })
    expect(read(key).steps?.[0]?.text).toBe('')
  })

  it('forgets one sheet without touching the others', () => {
    write(slot('a', 'q1'), { steps: [{ text: 'mine' }] })
    write(slot('b', 'q1'), { steps: [{ text: 'theirs' }] })
    forget(slot('a', 'q1'))
    expect(read(slot('a', 'q1'))).toEqual({})
    expect(read(slot('b', 'q1')).steps?.[0]?.text).toBe('theirs')
  })
})
