// @vitest-environment happy-dom
import { act, cleanup, fireEvent, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { resetSources, setSources } from '../../state/sources'
import { install, mount, source as settingsSource } from '../../test/settingsHarness'
import * as store from './store'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

vi.mock('../../state/toast', () => ({ show: () => {}, subscribe: () => () => {}, get: () => [] }))


beforeEach(() => {
  setSources({ settings: settingsSource })
})

afterEach(() => {
  cleanup()
  store._resetForTests()
  vi.restoreAllMocks()
  resetSources()
})

describe('settings root', () => {
  it('portals eight nav entries into the shell and titles the header with the open section', async () => {
    install()
    await mount('general')
    const nav = document.querySelectorAll('#snavList .settings-nitem')
    expect([...nav].map((b) => b.textContent)).toEqual([
      'gui.settings.nav.general', 'gui.settings.nav.usage', 'gui.settings.nav.model', 'gui.settings.nav.skills',
      'gui.settings.nav.tools', 'gui.settings.nav.plugins', 'gui.settings.nav.archive', 'gui.settings.nav.about',
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
    for (const id of ['usage', 'model', 'skills', 'tools', 'plugins', 'archive', 'about'] as const) {
      await act(async () => { store.setTab(id) })
      expect(document.querySelector(`.settings-panel[data-section="${id}"] .settings-card`), id).toBeTruthy()
    }
  })

  it('opens on the section the shared slot names, which is how the chrome jumps to models', async () => {
    install()
    await mount('model')
    expect(document.getElementById('setTitle')!.textContent).toBe('gui.settings.nav.model')
    expect(screen.getByText('gui.settings.roles.title_card')).toBeTruthy()
  })
})
