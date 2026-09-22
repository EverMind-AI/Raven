// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import * as settingsDialog from '../../state/settings'
import { resetSources, setSources } from '../../state/sources'
import { install, modelSource, mount, settle, snap, source as settingsSource } from '../../test/settingsHarness'
import { SettingsApp } from './SettingsApp'
import * as store from './store'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

vi.mock('../../state/toast', () => ({ show: () => {}, subscribe: () => () => {}, get: () => [] }))


beforeEach(() => {
  setSources({ settings: settingsSource, model: modelSource })
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  vi.restoreAllMocks()
  resetSources()
})

describe('settings root', () => {
  it('goes up before the values are in, and says so until they land', async () => {
    /* The catalogue behind `model.options` is seconds of work on a home with
       many providers, and the dialog used to await the whole load before
       lifting the veil -- a click with nothing on screen for that long. The
       veil is what the person is waiting for, so it goes first. */
    let land: (() => void) | null = null
    const data = snap()
    install(data, { load: () => new Promise((resolve) => { land = () => resolve(data) }) })
    settingsDialog.settingsTab.id = 'general'
    render(createElement(SettingsApp), { container: document.getElementById('spanels')! })

    const opening = store.open()
    await settle()
    expect(settingsDialog.open).toHaveBeenCalled()
    expect(document.querySelector('.settings-soonbox')!.textContent).toBe('gui.settings.loading')

    land!()
    await act(async () => { await opening })
    expect(document.querySelector('.settings-soonbox')).toBeNull()
    expect(document.querySelectorAll('.settings-row').length).toBeGreaterThan(0)
  })

  it('portals every nav entry into the shell and titles the header with the open section', async () => {
    install()
    await mount('general')
    const nav = document.querySelectorAll('#snavList .settings-nitem')
    expect([...nav].map((b) => b.textContent)).toEqual([
      'gui.settings.nav.general', 'gui.settings.nav.usage', 'gui.settings.nav.provider',
      'gui.settings.nav.model', 'gui.settings.nav.skills', 'gui.settings.nav.tools',
      'gui.settings.nav.plugins', 'gui.settings.nav.channels', 'gui.settings.nav.cron',
      'gui.settings.nav.memory', 'gui.settings.nav.archive', 'gui.settings.nav.about',
    ])
    expect(nav[0]!.getAttribute('aria-current')).toBe('true')
    expect(document.getElementById('setTitle')!.textContent).toBe('gui.settings.nav.general')
    expect(document.querySelector('.settings-panel')!.getAttribute('data-section')).toBe('general')
  })

  it('a nav pick switches the section, the title and the current mark', async () => {
    install()
    await mount('general')
    await act(async () => { fireEvent.click(screen.getByText('gui.settings.nav.tools')) })
    expect(document.getElementById('setTitle')!.textContent).toBe('gui.settings.nav.tools')
    expect(document.querySelector('.settings-panel')!.getAttribute('data-section')).toBe('tools')
    expect(document.querySelectorAll('#snavList [aria-current="true"]')[0]!.textContent).toBe('gui.settings.nav.tools')
  })

  it('every section renders from the fixture snapshot without a load of its own failing', async () => {
    install()
    await mount('general')
    for (const id of ['usage', 'provider', 'model', 'skills', 'tools', 'plugins', 'archive', 'about'] as const) {
      await act(async () => { store.setTab(id) })
      /* A card or a flat labelled block: the provider pane is the prototype's
         own flat sections, and every other section is still cards. */
      expect(
        document.querySelector(`.settings-panel[data-section="${id}"] .settings-card, .settings-panel[data-section="${id}"] .settings-sec`),
        id,
      ).toBeTruthy()
    }
  })

  it('opens on the section the shared slot names, which is how the chrome jumps to models', async () => {
    install()
    await mount('model')
    expect(document.getElementById('setTitle')!.textContent).toBe('gui.settings.nav.model')
    expect(screen.getByText('gui.settings.roles.title_card')).toBeTruthy()
  })
})
