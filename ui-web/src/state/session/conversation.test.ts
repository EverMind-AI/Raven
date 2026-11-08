// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { _resetFreshForTests, isFresh, onFresh, pitch, readOnly, unpitch } from './conversation'

beforeEach(() => {
  _resetFreshForTests()
  document.body.innerHTML = '<div class="chat"></div>'
})

afterEach(() => {
  document.body.innerHTML = ''
})

describe('the empty-state flag', () => {
  it('is written as the attribute and as the value by the same two verbs', () => {
    const chat = document.querySelector('.chat') as HTMLElement
    expect(isFresh()).toBe(true)
    unpitch()
    expect(isFresh()).toBe(false)
    expect(chat.dataset.fresh).toBeUndefined()
    pitch()
    expect(isFresh()).toBe(true)
    expect(chat.dataset.fresh).toBe('1')
  })

  it('tells a subscriber about each flip, synchronously', () => {
    const seen: boolean[] = []
    const off = onFresh(() => { seen.push(isFresh()) })
    unpitch()
    pitch()
    off()
    unpitch()
    expect(seen).toEqual([false, true])
  })
})

describe('the read-only flag', () => {
  it('lays the column out read-only and back, apart from the empty state', () => {
    const chat = document.querySelector('.chat') as HTMLElement
    readOnly(true)
    expect(chat.dataset.readonly).toBe('')
    expect(isFresh()).toBe(true)
    unpitch()
    expect(chat.dataset.readonly).toBe('')
    readOnly(false)
    expect(chat.dataset.readonly).toBeUndefined()
    expect(chat.dataset.fresh).toBeUndefined()
  })

  it('does nothing on a page without the column', () => {
    document.body.innerHTML = ''
    expect(() => readOnly(true)).not.toThrow()
  })
})
