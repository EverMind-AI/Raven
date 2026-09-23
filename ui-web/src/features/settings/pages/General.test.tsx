// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as notifications from '../../../lib/notifications'
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
  notifications.setEnabled(false)
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
    for (const k of ['language', 'theme', 'notify']) {
      const title = screen.getByText('gui.settings.general.' + k, { selector: '.settings-gen-t' })
      expect(title.nextElementSibling!.textContent).toBe('gui.settings.general.' + k + '_sub')
    }
  })

  it('the notification switch goes to the notifications module and refuses where the browser has none', async () => {
    /* Before the mount: the page reads whether the browser has notifications
       when it draws the row. */
    Object.defineProperty(window, 'Notification', { value: { permission: 'granted', requestPermission: async () => 'granted' }, configurable: true })
    install()
    await mount('general')
    const sw = screen.getByRole('switch', { name: 'gui.settings.general.notify' })
    expect(sw.getAttribute('aria-checked')).toBe('false')
    await act(async () => { fireEvent.click(sw) })
    expect(notifications.enabled()).toBe(true)
    delete (window as { Notification?: unknown }).Notification
    notifications.setEnabled(false)
    store.redraw()
    await act(async () => { fireEvent.click(screen.getByRole('switch', { name: 'gui.settings.general.notify' })) })
    expect(notifications.enabled()).toBe(false)
    expect(toasts.calls).toEqual(['gui.settings.general.notify_denied'])
  })
})
