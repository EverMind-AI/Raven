// @vitest-environment happy-dom
// Verifies persisted appearance settings and their document-level effects.
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { get, load, set } from './look'

const nativeCalls: unknown[] = []

type TestWindow = Window & {
  webkit?: { messageHandlers?: { raven?: { postMessage(value: unknown): void } } }
}

function prefersDark(dark: boolean): void {
  window.matchMedia = ((q: string) =>
    ({
      matches: dark && q.includes('dark'),
      media: q,
      addEventListener: () => {},
      removeEventListener: () => {},
    }) as unknown as MediaQueryList) as typeof window.matchMedia
}

beforeEach(() => {
  localStorage.clear()
  nativeCalls.length = 0
  prefersDark(false)
  ;(window as TestWindow).webkit = {
    messageHandlers: { raven: { postMessage: (value) => nativeCalls.push(value) } },
  }
  load()
})

afterEach(() => {
  localStorage.clear()
  delete (window as TestWindow).webkit
  delete document.documentElement.dataset.theme
  delete document.documentElement.dataset.motion
  delete document.documentElement.dataset.font
})

describe('the appearance owner', () => {
  it('starts light so the native splash and first frame agree', () => {
    expect(get()).toEqual({ theme: 'light', codeFont: 'system', motion: 'on' })
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(nativeCalls.at(-1)).toEqual({ type: 'theme', value: 'light' })
  })

  it('loads the stored preference and reports the effective system theme', () => {
    prefersDark(true)
    localStorage.setItem('raven.gui.look', JSON.stringify({ theme: 'system', codeFont: 'jet', motion: 'off' }))
    load()

    expect(get()).toEqual({ theme: 'system', codeFont: 'jet', motion: 'off' })
    expect(document.documentElement.dataset.theme).toBeUndefined()
    expect(document.documentElement.dataset.font).toBe('jet')
    expect(document.documentElement.dataset.motion).toBe('off')
    expect(nativeCalls.at(-1)).toEqual({ type: 'theme', value: 'dark' })
  })

  it('applies and persists every appearance field together', () => {
    set({ theme: 'dark', codeFont: 'sf', motion: 'off' })

    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(document.documentElement.dataset.font).toBe('sf')
    expect(document.documentElement.dataset.motion).toBe('off')
    expect(JSON.parse(localStorage.getItem('raven.gui.look') || '{}')).toEqual(get())
  })

  it('falls back to the full default after malformed storage', () => {
    set({ theme: 'dark', codeFont: 'sf', motion: 'off' })
    localStorage.setItem('raven.gui.look', '{broken')
    load()
    expect(get()).toEqual({ theme: 'light', codeFont: 'system', motion: 'on' })
  })
})
