// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import * as look from './look'
import { current, toggle } from './theme'

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
  prefersDark(false)
  look.load()
})

afterEach(() => {
  localStorage.clear()
  delete document.documentElement.dataset.theme
})

describe('the theme', () => {
  it('reads the attribute the stylesheet reads', () => {
    document.documentElement.dataset.theme = 'dark'
    expect(current()).toBe('dark')
    document.documentElement.dataset.theme = 'light'
    expect(current()).toBe('light')
  })

  it('falls back to the OS preference while nothing is set', () => {
    delete document.documentElement.dataset.theme
    expect(current()).toBe('light')
    prefersDark(true)
    expect(current()).toBe('dark')
  })

  it('cycles between the two, from whichever the OS was showing', () => {
    prefersDark(true)
    delete document.documentElement.dataset.theme
    expect(toggle()).toBe('light')
    expect(toggle()).toBe('dark')
    expect(toggle()).toBe('light')
  })

  it('hands every pick to the appearance owner for persistence', () => {
    toggle()
    expect(look.get().theme).toBe('dark')
    expect(JSON.parse(localStorage.getItem('raven.gui.look') || '{}').theme).toBe('dark')
  })
})
