// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as look from '../../../state/look'
import { resetSources, setSources } from '../../../state/sources'
import { install, modelSource, mount, source as settingsSource } from '../../../test/settingsHarness'
import * as store from '../store'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const toasts = vi.hoisted(() => ({ calls: [] as string[] }))
vi.mock('../../../state/toast', () => ({ show: (t: string) => { toasts.calls.push(t) }, subscribe: () => () => {}, get: () => [] }))


beforeEach(() => {
  setSources({ settings: settingsSource, model: modelSource })
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  vi.restoreAllMocks()
  resetSources()
  localStorage.clear()
  look.load()
  toasts.calls = []
})

describe('general page', () => {
  it('the language pick goes to the source, which owns what a flip means', async () => {
    const { calls } = install()
    await mount('general')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.general.lang_en')) })
    expect(calls).toEqual([['setLang', 'en']])
  })

  it('the theme pick writes the look store and the document attribute, not the config', async () => {
    const { calls } = install()
    await mount('general')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.general.theme_dark')) })
    expect(look.get().theme).toBe('dark')
    expect(document.documentElement.dataset.theme).toBe('dark')
    const cards = screen.getAllByRole('radio')
    expect(cards.map((c) => c.textContent)).toEqual([
      'gui.settings.general.theme_system', 'gui.settings.general.theme_light', 'gui.settings.general.theme_dark',
    ])
    expect(cards.map((c) => c.getAttribute('aria-checked'))).toEqual(['false', 'false', 'true'])
    expect(calls).toEqual([])
  })

  it('says what each setting is for under its name', async () => {
    install()
    await mount('general')
    for (const k of ['language', 'theme']) {
      const title = screen.getByText('gui.settings.general.' + k, { selector: '.settings-gen-t' })
      expect(title.nextElementSibling!.textContent).toBe('gui.settings.general.' + k + '_sub')
    }
  })

  it('offers no desktop-notification switch', async () => {
    /* The row is gone from the page, not from the tree: src/lib/notifications
       and its writer in state/session/runtime.ts are untouched, and this is
       what says the reader is no longer offered the setting. */
    Object.defineProperty(window, 'Notification', { value: { permission: 'granted', requestPermission: async () => 'granted' }, configurable: true })
    install()
    await mount('general')
    expect(screen.queryByRole('switch')).toBeNull()
    expect(screen.queryByText('gui.settings.general.notify')).toBeNull()
  })
})
