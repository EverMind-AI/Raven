// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { current, toggle } from './theme'

import type { Shell } from './bridge'

/* The shell half of the seam, as thin as the theme needs it: one call that
   remembers the pick. `picked` is what the legacy look store would have
   received in CFG.theme. */
function install(withStore = true): { picked: string[] } {
  const picked: string[] = []
  const fake: Shell = {
    T: (key) => key,
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: () => {},
  }
  if (withStore) fake.themeSet = (next) => picked.push(next)
  window.RavenShell = fake
  return { picked }
}

/* happy-dom answers every media query with matches:false, so the OS
   preference has to be stated per test. */
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
  delete document.documentElement.dataset.theme
  prefersDark(false)
})

afterEach(() => {
  delete window.RavenShell
})

describe('the theme', () => {
  it('reads the attribute the stylesheet reads', () => {
    document.documentElement.dataset.theme = 'dark'
    expect(current()).toBe('dark')
    document.documentElement.dataset.theme = 'light'
    expect(current()).toBe('light')
  })

  it('falls back to the OS preference while nothing is set', () => {
    expect(current()).toBe('light')
    prefersDark(true)
    expect(current()).toBe('dark')
  })

  it('cycles between the two, from whichever the OS was showing', () => {
    prefersDark(true)
    install()
    expect(toggle()).toBe('light')
    expect(document.documentElement.dataset.theme).toBe('light')
    expect(toggle()).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
    expect(toggle()).toBe('light')
  })

  it('hands every pick to the look store, which is what persists it', () => {
    const { picked } = install()
    toggle()
    toggle()
    expect(picked).toEqual(['dark', 'light'])
  })

  it('still flips when the shell has no look store to tell', () => {
    install(false)
    expect(toggle()).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
  })
})
