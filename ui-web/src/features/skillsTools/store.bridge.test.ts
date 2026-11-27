// @vitest-environment happy-dom
/* The bridge between the settings dialog and the Skills & Tools page.
 *
 * Both read the same config through features/settings/source.ts, so a change
 * made in one shows in the other. The page re-reads rather than taking the
 * dialog's copy: the two hold snapshots taken at different moments, and the
 * older one must not overwrite the newer. */
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as page from '../../state/page'
import { PAGES } from '../../state/pages'
import { resetSources, setSources } from '../../state/sources'
import { snap } from '../../test/settingsHarness'
import * as settingsStore from '../settings/store'
import * as store from './store'

import type { SettingsSnapshot, SkillsToolsSource } from './types'

const PAGE_SHELL =
  '<div class="app"></div>'
  + PAGES.map((p) => `<section class="page" id="${p.id}" data-open="false"><div class="work"><div class="wrap" id="${p.bodyId}"></div></div></section>`).join('')
  + '<aside class="detail" id="detail" data-open="false"><div class="dpanel"><header><b id="dTitle"></b></header><div class="body" id="dBody"></div></div></aside>'

function sourceOf(load: () => Promise<SettingsSnapshot>): SkillsToolsSource {
  return {
    load,
    set: async () => snap(),
    everosSet: async () => snap(),
    addModel: async () => snap(),
    inspectSkill: async () => { throw new Error('no') },
    openSkillFile: async () => {},
    uninstallSkill: async () => snap(),
    blockSkill: async () => snap(),
    serverDetail: async () => { throw new Error('no') },
    toggleServer: async () => snap(),
    retryServer: async () => snap(),
    revokeServer: async () => snap(),
    configureServer: async () => snap(),
    authServer: async () => snap(),
    agentPlugins: async () => ({ packages: [], problems: [] }),
    setPackageMcp: async () => ({ packages: [], problems: [] }),
  }
}

afterEach(() => {
  store._resetForTests()
  settingsStore._resetForTests()
  page._resetForTests()
  resetSources()
  document.body.innerHTML = ''
  vi.useRealTimers()
})

describe('the settings bridge', () => {
  it('re-reads the config instead of taking the dialog snapshot', async () => {
    vi.useFakeTimers()
    let loads = 0
    setSources({
      skillsTools: sourceOf(async () => {
        loads += 1
        return snap({ model: `m-${loads}` })
      }),
    })

    document.body.innerHTML = PAGE_SHELL
    store.set({ loaded: true })
    page.show('skillsToolsPage')

    /* What install.ts spends when the settings store notifies. */
    store.refreshSoon()
    await vi.advanceTimersByTimeAsync(store.REFRESH_SOON_MS + 50)

    expect(loads).toBe(1)
    expect(store.get().snap.model).toBe('m-1')
  })

  it('keeps the page on the config as it stands now', async () => {
    vi.useFakeTimers()
    let loads = 0
    setSources({
      skillsTools: sourceOf(async () => {
        loads += 1
        return snap({ model: `m-${loads}` })
      }),
    })

    document.body.innerHTML = PAGE_SHELL
    store.set({ loaded: true })
    page.show('skillsToolsPage')
    await store.refresh()
    expect(store.get().snap.model).toBe('m-1')

    /* The next read is the config as it stands, so the page follows it. */
    store.refreshSoon()
    await vi.advanceTimersByTimeAsync(store.REFRESH_SOON_MS + 50)
    expect(loads).toBe(2)
    expect(store.get().snap.model).toBe('m-2')
  })

  it('carries a settings-side write onto the page', async () => {
    vi.useFakeTimers()
    let serverModel = 'before'
    setSources({
      skillsTools: sourceOf(async () => snap({ model: serverModel })),
    })

    document.body.innerHTML = PAGE_SHELL
    await store.open('skills')
    expect(store.get().snap.model).toBe('before')

    /* The reader changes something in the settings dialog: the config moves
       and the settings store takes the new snapshot. */
    serverModel = 'after'
    settingsStore.set({ snap: snap({ model: 'after' }) })

    /* install.ts spends the notification. */
    store.refreshSoon()
    await vi.advanceTimersByTimeAsync(store.REFRESH_SOON_MS + 50)

    expect(store.get().snap.model).toBe('after')
  })

  it('does not read while the page is closed', async () => {
    vi.useFakeTimers()
    let loads = 0
    setSources({
      skillsTools: sourceOf(async () => {
        loads += 1
        return snap()
      }),
    })

    store.set({ loaded: true })
    store.refreshSoon()
    await vi.advanceTimersByTimeAsync(store.REFRESH_SOON_MS + 50)
    expect(loads).toBe(0)
  })
})
