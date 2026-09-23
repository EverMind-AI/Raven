// @vitest-environment happy-dom
import { afterEach, describe, expect, it, vi } from 'vitest'

import { mountPageRoot } from '../test/pageRoot'
import { show } from './toast'

/* The notices render from src/App.tsx into the host each was raised in, so the
   page's own root has to be standing for any of them to appear. */
mountPageRoot()

afterEach(() => {
  vi.useRealTimers()
  document.body.innerHTML = ''
})

describe('the toast writer', () => {
  it('appends the legacy node shape and removes a plain notice after 2.6 seconds', () => {
    vi.useFakeTimers()
    document.body.innerHTML = '<div class="toasts" id="toasts"></div>'
    show('Saved')
    expect(document.getElementById('toasts')!.innerHTML).toBe('<div class="toast"><span class="t">Saved</span></div>')
    vi.advanceTimersByTime(2599)
    expect(document.querySelector('.toast')).not.toBeNull()
    vi.advanceTimersByTime(1)
    expect(document.querySelector('.toast')).toBeNull()
  })

  it('keeps an action notice for 5.2 seconds and closes it after the action', () => {
    vi.useFakeTimers()
    document.body.innerHTML = '<div id="toasts"></div>'
    const calls: string[] = []
    show('Deleted', { label: 'Undo', fn: () => calls.push('undo') })
    expect(document.querySelector('.toast')!.innerHTML).toBe('<span class="t">Deleted</span><button>Undo</button>')
    vi.advanceTimersByTime(5199)
    expect(document.querySelector('.toast')).not.toBeNull()
    ;(document.querySelector('.toast button') as HTMLButtonElement).click()
    expect(calls).toEqual(['undo'])
    expect(document.querySelector('.toast')).toBeNull()
  })

  /* The action runs first and the notice comes down after, which is what lets
     an action raise a notice of its own: the drop is by id, so it cannot take
     the new one with it. */
  it('runs the action while its own notice is still up', () => {
    vi.useFakeTimers()
    document.body.innerHTML = '<div id="toasts"></div>'
    const seen: string[] = []
    show('Deleted', { label: 'Undo', fn: () => seen.push(document.querySelector('.toast .t')?.textContent ?? '') })
    ;(document.querySelector('.toast button') as HTMLButtonElement).click()
    expect(seen).toEqual(['Deleted'])
    expect(document.querySelector('.toast')).toBeNull()
  })

  it('does nothing when the page has no toast host', () => {
    expect(() => show('nowhere')).not.toThrow()
    expect(document.body.children).toHaveLength(0)
  })
})
