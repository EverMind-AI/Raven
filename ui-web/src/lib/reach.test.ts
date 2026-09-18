// @vitest-environment happy-dom
/* Exact catalogue coverage for the fixed execution-reach vocabulary. */

import { afterEach, describe, expect, it, vi } from 'vitest'

import { resetTranslator, setTranslator } from '../i18n/t'
import * as confirmStore from '../state/confirm'
import * as pageStore from '../state/page'
import { hint, text } from './reach'

afterEach(() => {
  resetTranslator()
})

describe('execution reach labels', () => {
  it('translates every reach and its hint through the shared catalogue', () => {
    setTranslator((key) => `translated:${key}`)
    vi.spyOn(pageStore, 'show').mockImplementation(() => {})
    vi.spyOn(confirmStore, 'ask').mockImplementation(() => {})

    expect(text('local')).toBe('translated:gui.reach.local')
    expect(hint('net')).toBe('translated:gui.reach.net_hint')
    expect(text('auth')).toBe('translated:gui.reach.auth')
  })

  it('falls back to local for an unknown reach', () => {
    setTranslator((key) => key)

    expect(text('elsewhere')).toBe('gui.reach.local')
    expect(hint('elsewhere')).toBe('gui.reach.local_hint')
  })
})
