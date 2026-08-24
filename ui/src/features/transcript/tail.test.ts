// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { _resetForTests, down, isStuck, setStuck } from './tail'

describe('transcript tail ownership', () => {
  beforeEach(() => {
    _resetForTests()
    document.body.innerHTML = '<div id="scroll"></div>'
  })

  it('follows new output only while the reader is stuck to the tail', () => {
    const scroll = document.getElementById('scroll')!
    Object.defineProperty(scroll, 'scrollHeight', { value: 900, configurable: true })
    const scrollTo = vi.fn()
    scroll.scrollTo = scrollTo

    down()
    expect(scrollTo).toHaveBeenCalledWith({ top: 900, behavior: 'instant' })

    setStuck(false)
    expect(isStuck()).toBe(false)
    down()
    expect(scrollTo).toHaveBeenCalledTimes(1)
  })
})
