// @vitest-environment happy-dom
/* Exact catalogue coverage for the fixed execution-reach vocabulary. */

import { afterEach, describe, expect, it } from 'vitest'

import { hint, text } from './reach'

afterEach(() => {
  delete window.RavenShell
})

describe('execution reach labels', () => {
  it('translates every reach and its hint through the shared catalogue', () => {
    window.RavenShell = {
      T: (key) => `translated:${key}`,
      confirmAsk: () => {},
      showPage: () => {},
    }

    expect(text('local')).toBe('translated:gui.reach.local')
    expect(hint('net')).toBe('translated:gui.reach.net_hint')
    expect(text('auth')).toBe('translated:gui.reach.auth')
  })

  it('falls back to local for an unknown reach', () => {
    window.RavenShell = {
      T: (key) => key,
      confirmAsk: () => {},
      showPage: () => {},
    }

    expect(text('elsewhere')).toBe('gui.reach.local')
    expect(hint('elsewhere')).toBe('gui.reach.local_hint')
  })
})
