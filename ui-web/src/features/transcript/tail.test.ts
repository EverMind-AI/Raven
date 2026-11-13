// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { _resetForTests, atConversationEnd, down, isStuck, setStuck } from './tail'

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

describe('the end of a window short of the tail is not the conversation end', () => {
  const box = (scrollHeight: number, scrollTop: number, clientHeight: number): {
    scrollHeight: number; scrollTop: number; clientHeight: number
  } => ({ scrollHeight, scrollTop, clientHeight })

  it('needs both the box at its end and the window at the conversation tail', () => {
    expect(atConversationEnd(box(900, 400, 500), 0)).toBe(true)
    expect(atConversationEnd(box(900, 395, 500), 0)).toBe(false)
    expect(atConversationEnd(box(900, 400, 500), 42)).toBe(false)
    expect(atConversationEnd(box(900, 395, 500), 42)).toBe(false)
  })
})
