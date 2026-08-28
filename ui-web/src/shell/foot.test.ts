// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { draw } from './foot'

interface Facts {
  version?: string | null
  mac?: boolean
}

const originalPlatform = Object.getOwnPropertyDescriptor(navigator, 'platform')

function install(facts: Facts = {}): void {
  window.DS = { settings: { version: () => facts.version ?? null } }
  Object.defineProperty(navigator, 'platform', {
    configurable: true,
    value: facts.mac ? 'MacIntel' : 'Linux x86_64',
  })
}

const sub = (): string => document.getElementById('meSub')?.textContent ?? ''
const kbd = (): string => document.getElementById('meKbd')?.textContent ?? ''

beforeEach(() => {
  document.body.innerHTML = '<button class="me" id="meBtn"><span class="s" id="meSub"></span><span class="kbd" id="meKbd"></span></button>'
})

afterEach(() => {
  delete window.DS
  if (originalPlatform) Object.defineProperty(navigator, 'platform', originalPlatform)
  else Reflect.deleteProperty(navigator, 'platform')
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
