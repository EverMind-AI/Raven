// @vitest-environment happy-dom
/* The copy button's tick: the card can go away while the tick is still owed. */
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { CopyCmd } from './Parts'

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('CopyCmd', () => {
  it('drops its tick when the card goes away', async () => {
    vi.useFakeTimers()
    const cleared: number[] = []
    const realClear = window.clearTimeout.bind(window)
    vi.spyOn(window, 'clearTimeout').mockImplementation((id?: number) => {
      if (id !== undefined) cleared.push(id)
      realClear(id)
    })

    const { container, unmount } = render(createElement(CopyCmd, { command: 'raven agents list' }))
    await act(async () => { fireEvent.click(container.querySelector('.skillsTools-cmdcopy')!) })

    /* The card goes away with the tick still owed: the modal it sat in was
       closed a moment after the copy. The tick is dropped rather than left
       to fire on a component that is no longer there. */
    unmount()
    expect(cleared.length).toBeGreaterThan(0)

    await act(async () => { vi.advanceTimersByTime(2000) })
  })
})
