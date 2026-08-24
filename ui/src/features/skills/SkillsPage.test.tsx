// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SkillsApp } from './SkillsPage'
import * as store from './store'

import type { Shell } from '../../shell/bridge'
import type { HubItem, InstalledSkill, SkillsSource } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

function hubItem(over: Partial<HubItem> = {}): HubItem {
  return {
    id: 'sh-review',
    name: 'code-review-checklist',
    description: 'review by the house rules',
    category: 'DEV',
    source: 'skillhub',
    quality_score: 0.9,
    installed: false,
    installed_name: '',
    ...over,
  }
}

/* The island runs against the same two seams production wires: a fake
   shell on window.RavenShell (T returns its key, so tests assert catalogue
   keys, not translations) and a fixture source on window.DS.skills. */
function install(items: HubItem[], over: Partial<SkillsSource> = {}, installed: InstalledSkill[] = []) {
  const calls: string[] = []
  const source: SkillsSource = {
    search: async () => ({ items, total: items.length }),
    detail: async () => ({ description: 'the whole story', category: 'DEV', files: ['SKILL.md'] }),
    install: async () => calls.push('install'),
    remove: async () => calls.push('remove'),
    installed: () => installed,
    ...over,
  }
  const shellCalls: Array<[string, unknown]> = []
  const fakeShell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: (id) => shellCalls.push(['showPage', id]),
    useInTask: (key, name) => shellCalls.push(['useInTask', `${key}:${name}`]),
    reachText: (reach) => `reach:${reach}`,
    closeDetail: () => {
      const el = document.getElementById('detail')
      if (el) el.dataset.open = 'false'
      store.dropDrawer()
    },
  }
  window.RavenShell = fakeShell
  window.DS = { skills: source }
  document.body.innerHTML =
    '<section id="capsPage" data-open="true"><div id="capsBody"></div></section>' +
    '<aside id="detail" data-open="false"><b id="dTitle"></b><div id="dBody"></div></aside>'
  return { source, calls, shellCalls }
}

async function mount() {
  const view = render(<SkillsApp />, { container: document.getElementById('capsBody')! })
  await act(async () => {
    store.ensureSearch()
  })
  return view
}

afterEach(() => {
  act(() => {
    store.wipe()
  })
  cleanup()
  vi.restoreAllMocks()
})

describe('skills island', () => {
  it('shows every hub row the source answers, behind the category chips', async () => {
    install([hubItem(), hubItem({ id: 'sh-sql', name: 'sql-style', quality_score: 0.7 })])
    await mount()
    expect(await screen.findByText('code-review-checklist')).toBeTruthy()
    expect(screen.getByText('sql-style')).toBeTruthy()
    expect(screen.getByText('gui.hubcat.all')).toBeTruthy()
    expect(screen.getAllByText('gui.hub.install').length).toBe(2)
  })

  it('shows the empty note when the filter matches nothing', async () => {
    install([])
    await mount()
    expect(await screen.findByText('gui.hub.empty_filter')).toBeTruthy()
  })

  it('renders a failed search in place, and the retry asks again', async () => {
    const { source } = install([hubItem()], {
      search: async () => {
        throw new Error('boom')
      },
    })
    await mount()
    expect(await screen.findByText('gui.hub.err {"err":"boom"}')).toBeTruthy()
    const searchSpy = vi.spyOn(source, 'search').mockResolvedValue({ items: [hubItem()], total: 1 })
    await act(async () => {
      screen.getByText('gui.plug.retry').click()
    })
    expect(searchSpy).toHaveBeenCalled()
    expect(await screen.findByText('code-review-checklist')).toBeTruthy()
  })

  it('marks the card installed once the source accepts the install', async () => {
    const { calls } = install([hubItem()])
    await mount()
    await act(async () => {
      ;(await screen.findByText('gui.hub.install')).click()
    })
    expect(calls).toContain('install')
    expect(await screen.findByText('gui.hub.installed')).toBeTruthy()
  })

  it('leaves the card installable when the source rejects handled', async () => {
    install([hubItem()], {
      install: async () => {
        // What the live source throws after toasting the reason itself.
        throw { handled: true }
      },
    })
    await mount()
    await act(async () => {
      ;(await screen.findByText('gui.hub.install')).click()
    })
    expect(await screen.findByText('gui.hub.install')).toBeTruthy()
    expect(screen.queryByText('gui.hub.installed')).toBeNull()
    expect(screen.queryByText('gui.hub.working')).toBeNull()
  })

  it('lists what is installed behind the back bar, with a use action', async () => {
    const { shellCalls } = install([], {}, [
      { id: 'sk1', name: 'house-style', one: 'the house voice', src: 'skillhub', hub: true, hubId: 'sh-hs' },
    ])
    await mount()
    await act(async () => {
      store.toggleView()
    })
    expect(await screen.findByText('house-style')).toBeTruthy()
    expect(screen.getByText('← gui.plug.back')).toBeTruthy()
    await act(async () => {
      screen.getByText('gui.hub.use').click()
    })
    expect(shellCalls).toContainEqual(['useInTask', 'gui.hub.use_prompt:house-style'])
  })

  it('shows the empty-installed note when nothing is installed', async () => {
    install([])
    await mount()
    await act(async () => {
      store.toggleView()
    })
    expect(await screen.findByText('gui.hub.empty_installed')).toBeTruthy()
  })

  it('opens a card into the shared drawer and fills it from the detail call', async () => {
    install([hubItem()])
    await mount()
    await act(async () => {
      ;(await screen.findByText('code-review-checklist')).click()
    })
    expect(await screen.findByText('the whole story')).toBeTruthy()
    expect(document.getElementById('detail')!.dataset.open).toBe('true')
    expect(document.getElementById('dBody')!.textContent).toContain('the whole story')
  })

  it('uninstalls from the drawer only after the arm click, then closes it', async () => {
    const { calls } = install([], {}, [
      { id: 'sk1', name: 'house-style', one: 'the house voice', src: 'skillhub', hub: true },
    ])
    await mount()
    await act(async () => {
      store.toggleView()
    })
    await act(async () => {
      ;(await screen.findByText('house-style')).click()
    })
    const rm = await screen.findByText('gui.plug.uninstall')
    await act(async () => {
      rm.click()
    })
    expect(calls).not.toContain('remove')
    await act(async () => {
      screen.getByText('gui.plug.confirm_remove').click()
    })
    expect(calls).toContain('remove')
    expect(document.getElementById('detail')!.dataset.open).toBe('false')
  })
})
