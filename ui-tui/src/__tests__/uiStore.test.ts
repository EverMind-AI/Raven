// SPDX-License-Identifier: MIT
// Portions Copyright (c) 2025 Nous Research (hermes-agent, MIT).
// Modifications Copyright (c) 2026 EverMind.
// See NOTICES.md and LICENSES/MIT-hermes-agent.txt.

import { afterEach, describe, expect, it, vi } from 'vitest'

// uiStore's initial theme comes from DEFAULT_THEME, which theme.js computes at
// module-load from the environment. Sterilize the same vars theme.test.ts does
// and import fresh, so an ambient RAVEN_TUI_THEME in the developer's shell
// cannot flip the base palette under these assertions.
const RELEVANT_ENV = [
  'RAVEN_TUI_LIGHT',
  'RAVEN_TUI_THEME',
  'RAVEN_TUI_BACKGROUND',
  'COLORFGBG',
  'COLORTERM',
  'TERM_PROGRAM'
] as const

async function importStoreWithCleanEnv() {
  for (const key of RELEVANT_ENV) {
    vi.stubEnv(key, '')
  }

  vi.resetModules()

  return { store: await import('../app/uiStore.js'), theme: await import('../theme.js') }
}

afterEach(() => {
  vi.unstubAllEnvs()
  vi.resetModules()
})

describe('applyTerminalBackground', () => {
  it('re-themes when the measured ground moves but the scheme holds', async () => {
    const { store, theme } = await importStoreWithCleanEnv()

    expect(store.getUiState().theme.color.userBg).toBe(theme.DARK_THEME.color.userBg)

    // A warm dark ground: still dark, so the old `changed` flag alone would
    // have left the cool default surfaces on screen.
    const res = store.applyTerminalBackground('rgb:16/15/0f')

    expect(res).toEqual({ changed: false, scheme: 'dark', surfacesChanged: true })
    expect(store.getUiState().theme.color.userBg).toBe(theme.deriveSurfaces('dark', '#16150f').userBg)
    expect(store.getUiState().theme.color.detailBg).toBe(theme.deriveSurfaces('dark', '#16150f').detailBg)
  })

  it('re-themes when the scheme flips', async () => {
    const { store, theme } = await importStoreWithCleanEnv()

    store.applyTerminalBackground('rgb:f5f5/efef/e3e3')

    const { color } = store.getUiState().theme

    expect(color.primary).toBe(theme.LIGHT_THEME.color.primary)
    expect(color.userBg).toBe(theme.deriveSurfaces('light', '#f5efe3').userBg)
  })

  it('leaves the theme alone when the same ground is re-reported', async () => {
    const { store } = await importStoreWithCleanEnv()

    store.applyTerminalBackground('#16150f')
    const settled = store.getUiState().theme

    expect(store.applyTerminalBackground('#16150f')).toEqual({
      changed: false,
      scheme: 'dark',
      surfacesChanged: false
    })
    expect(store.getUiState().theme).toBe(settled)
  })

  it('leaves the theme alone on an unparseable reply', async () => {
    const { store } = await importStoreWithCleanEnv()

    const before = store.getUiState().theme

    expect(store.applyTerminalBackground('not-a-color')).toBeNull()
    expect(store.getUiState().theme).toBe(before)
  })

  it('keeps the surfaces a skin names across a ground change', async () => {
    const { store, theme } = await importStoreWithCleanEnv()

    store.applySkinTheme({ colors: { ui_detail_bg: '#123456', ui_user_bg: '#abcdef' } })
    store.applyTerminalBackground('rgb:f5f5/efef/e3e3')

    const { color } = store.getUiState().theme

    expect(color.userBg).toBe('#abcdef')
    expect(color.detailBg).toBe('#123456')
    // A skin that says nothing about a surface still follows the ground.
    store.applySkinTheme({ colors: { ui_user_bg: '#abcdef' } })
    expect(store.getUiState().theme.color.detailBg).toBe(theme.deriveSurfaces('light', '#f5efe3').detailBg)
  })
})
