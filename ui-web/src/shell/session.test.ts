/* Behavioral checks for the page-scoped session pointer. */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { _resetForTests, current, onChange, setCurrent } from './session'

afterEach(_resetForTests)

describe('the page session pointer', () => {
  it('starts as a draft and moves through conversation ids', () => {
    expect(current()).toBeNull()
    setCurrent('a')
    expect(current()).toBe('a')
    setCurrent(null)
    expect(current()).toBeNull()
  })

  it('notifies once per real change and supports unsubscribe', () => {
    const listener = vi.fn()
    const off = onChange(listener)
    setCurrent('a')
    setCurrent('a')
    setCurrent('b')
    off()
    setCurrent(null)
    expect(listener.mock.calls).toEqual([['a'], ['b']])
  })
})
