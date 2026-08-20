// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { draw } from './foot'

import type { Shell } from './bridge'

interface Facts {
  version?: string | null
  mac?: boolean
}

function install(facts: Facts = {}): void {
  const fake: Shell = {
    T: (key) => key,
    toast: () => {},
    menuAt: () => {},
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: () => {},
    appVersion: () => facts.version ?? null,
    /* The legacy modKey(), verbatim: the glyph on a Mac, the word plus its own
       spacing everywhere else. */
    modKey: () => (facts.mac ? '⌘' : 'Ctrl +'),
    isMac: () => !!facts.mac,
  }
  window.RavenShell = fake
}

const sub = (): string => document.getElementById('meSub')?.textContent ?? ''
const kbd = (): string => document.getElementById('meKbd')?.textContent ?? ''

beforeEach(() => {
  document.body.innerHTML = '<button class="me" id="meBtn"><span class="s" id="meSub"></span><span class="kbd" id="meKbd"></span></button>'
})

afterEach(() => {
  delete window.RavenShell
})

describe('the foot row', () => {
  it('names the running build', () => {
    install({ version: '0.1.7', mac: true })
    draw()
    expect(sub()).toBe('Raven v0.1.7')
  })

  it('says -- until the running install has answered, never a guess', () => {
    install({ version: null, mac: true })
    draw()
    expect(sub()).toBe('Raven --')
  })

  it('spells the shortcut the way the platform does', () => {
    install({ mac: true })
    draw()
    expect(kbd()).toBe('⌘,')
    install({ mac: false })
    draw()
    expect(kbd()).toBe('Ctrl + ,')
  })

  it('redraws over its own last words rather than appending', () => {
    install({ version: '0.1.7', mac: true })
    draw()
    install({ version: '0.1.8', mac: true })
    draw()
    expect(sub()).toBe('Raven v0.1.8')
    expect(kbd()).toBe('⌘,')
  })

  it('draws nothing and throws nothing when the row is not in the page', () => {
    install({ version: '0.1.7', mac: true })
    document.body.innerHTML = ''
    expect(() => draw()).not.toThrow()
  })
})
