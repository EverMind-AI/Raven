import { afterEach, describe, expect, it, vi } from 'vitest'

import { _resetForTests, fire, onVisibleAgain } from './visibility'

afterEach(() => {
  _resetForTests()
})

describe('the visibility slot', () => {
  it('spends nothing until filled, then the one function it was given', () => {
    expect(() => fire()).not.toThrow()
    const fn = vi.fn()
    onVisibleAgain(fn)
    fire()
    fire()
    expect(fn).toHaveBeenCalledTimes(2)
  })

  it('holds one function: a later fill replaces the earlier one', () => {
    const first = vi.fn()
    const second = vi.fn()
    onVisibleAgain(first)
    onVisibleAgain(second)
    fire()
    expect(first).not.toHaveBeenCalled()
    expect(second).toHaveBeenCalledTimes(1)
  })
})
