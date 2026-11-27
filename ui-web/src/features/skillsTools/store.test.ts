// @vitest-environment happy-dom
/* State and lifecycle tests for the Skills & Tools domain store. */
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as detail from '../../state/detail'
import * as page from '../../state/page'
import { PAGES } from '../../state/pages'
import { resetSources, setSources } from '../../state/sources'
import { pkgList, snap } from '../../test/settingsHarness'
import * as store from './store'

import type { SettingsSnapshot, SkillsToolsSource } from './types'

const PAGE_SHELL =
  '<div class="app"></div>'
  + PAGES.map((p) => `<section class="page" id="${p.id}" data-open="false"><div class="work"><div class="wrap" id="${p.bodyId}"></div></div></section>`).join('')
  + '<aside class="detail" id="detail" data-open="false"><div class="dpanel"><header><b id="dTitle"></b></header><div class="body" id="dBody"></div></div></aside>'

const sourceOf = (over: Partial<SkillsToolsSource> = {}): SkillsToolsSource => ({
  load: async () => snap(),
  set: async () => snap(),
  everosSet: async () => snap(),
  addModel: async () => snap(),
  inspectSkill: async (name) => ({ name, description: 'desc', path: `/skills/${name}`, body: '# Hi', files: ['SKILL.md'], always: false, install: null }),
  openSkillFile: async () => {},
  uninstallSkill: async () => snap(),
  blockSkill: async () => snap(),
  serverDetail: async () => ({ known: true, fields: [], tools: [] }),
  toggleServer: async () => snap(),
  retryServer: async () => snap(),
  revokeServer: async () => snap(),
  configureServer: async () => snap(),
  authServer: async () => snap(),
  agentPlugins: async () => pkgList(),
  setPackageMcp: async () => pkgList(),
  ...over,
})

afterEach(() => {
  store._resetForTests()
  detail._resetForTests()
  page._resetForTests()
  resetSources()
  document.body.innerHTML = ''
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('skillsTools store', () => {
  it('opens the module page, loads snapshot, and switches tabs', async () => {
    document.body.innerHTML = PAGE_SHELL
    setSources({ skillsTools: sourceOf() })

    await store.open('tools')
    expect(page.get()).toBe('skillsToolsPage')
    expect(store.get().tab).toBe('tools')
    expect(store.get().loaded).toBe(true)

    store.setTab('mcp')
    expect(store.get().tab).toBe('mcp')
  })

  it('leaves the page standing when the config has not moved', async () => {
    const first = snap()
    setSources({ skillsTools: sourceOf({ load: async () => first }) })
    await store.refresh()
    const epoch0 = store.get().epoch

    /* The same config read again: the page does not remount, because a
       remount would drop what the reader had typed into a field. */
    await store.refresh()
    expect(store.get().epoch).toBe(epoch0)

    const next: SettingsSnapshot = snap({ model: 'claude-sonnet-4-5' })
    setSources({ skillsTools: sourceOf({ load: async () => next }) })
    await store.refresh()
    expect(store.get().epoch).toBe(epoch0 + 1)
    expect(store.get().snap.model).toBe('claude-sonnet-4-5')
  })

  it('coalesces refreshSoon bursts only while the Skills & Tools page is open', async () => {
    vi.useFakeTimers()
    document.body.innerHTML = PAGE_SHELL
    let loads = 0
    setSources({
      skillsTools: sourceOf({
        load: async () => {
          loads += 1
          return snap({ model: `m-${loads}` })
        },
      }),
    })

    store.set({ loaded: true })
    store.refreshSoon()
    await vi.advanceTimersByTimeAsync(store.REFRESH_SOON_MS + 50)
    expect(loads).toBe(0)

    page.show('skillsToolsPage')
    store.refreshSoon()
    store.refreshSoon()
    store.refreshSoon()
    await vi.advanceTimersByTimeAsync(store.REFRESH_SOON_MS + 50)
    expect(loads).toBe(1)
    expect(store.get().snap.model).toBe('m-1')
  })

  it('opens a centered detail modal and clears target after fade when closed', async () => {
    vi.useFakeTimers()
    document.body.innerHTML = PAGE_SHELL
    setSources({ skillsTools: sourceOf() })

    store.openTool('web_search')
    expect(detail.get().owner).toBe('skillsTools')
    expect(detail.get().open).toBe(true)
    expect(store.get().modal).toEqual({ kind: 'tool', id: 'web_search' })

    detail.close()
    expect(detail.get().open).toBe(false)
    expect(store.get().modal).toEqual({ kind: 'tool', id: 'web_search' })

    await vi.advanceTimersByTimeAsync(300)
    expect(store.get().modal).toBeNull()
  })

  it('loads skill detail when opening a skill modal', async () => {
    document.body.innerHTML = PAGE_SHELL
    setSources({ skillsTools: sourceOf() })

    await store.openSkill('git-flow')
    expect(detail.get().owner).toBe('skillsTools')
    expect(detail.get().fill).toBe(true)
    expect(store.get().skillDetail?.name).toBe('git-flow')
  })

  it('opens an MCP modal in the settled detail fill while its body loads', async () => {
    document.body.innerHTML = PAGE_SHELL
    setSources({ skillsTools: sourceOf() })

    await store.openMcp('github')

    expect(detail.get().owner).toBe('skillsTools')
    expect(detail.get().fill).toBe(true)
    expect(store.get().mcpDetail).not.toBeNull()
  })

  it('coalesces package loads and ignores one superseded by a package write', async () => {
    document.body.innerHTML = PAGE_SHELL
    let finish!: (value: ReturnType<typeof pkgList>) => void
    const agentPlugins = vi.fn(() => new Promise<ReturnType<typeof pkgList>>((resolve) => { finish = resolve }))
    setSources({ skillsTools: sourceOf({ agentPlugins }) })

    const first = store.pkgsLoad()
    const second = store.pkgsLoad()
    expect(agentPlugins).toHaveBeenCalledTimes(1)

    const fresh = pkgList({ packages: [] })
    store.syncPkgs(fresh)
    finish(pkgList())
    await Promise.all([first, second])

    expect(store.get().pkgs).toBe(fresh)
  })
})
