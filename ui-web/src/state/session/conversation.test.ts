// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { _resetFreshForTests, isFresh, onFresh, pitch, unpitch } from './conversation'

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
