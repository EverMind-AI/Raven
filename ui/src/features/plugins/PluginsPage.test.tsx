// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { PlugApp } from './PluginsPage'
import * as store from './store'

import type { Shell } from '../../shell/bridge'
import type { DetailEntry, InstalledRow, MarketItem, PluginsSource } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const toastWriter = vi.hoisted(() => ({ calls: [] as Array<[string, unknown]> }))
vi.mock('../../shell/toast', () => ({
  show: (text: string) => { toastWriter.calls.push(['toast', text]) },
}))

function item(over: Partial<MarketItem> = {}): MarketItem {
  return {
    id: 'websearch',
    name: 'websearch',
    publisher: 'serper.dev',
    summary: 'find things on the live web',
    verified: true,
    tool_preview_count: 2,
    installed: false,
    ...over,
  }
}

function entryOf(it: MarketItem, over: Partial<DetailEntry> = {}): DetailEntry {
  return {
    id: it.id,
    name: it.name,
    version: '1.0.0',
    summary: it.summary,
    description: 'what installing this brings',
    publisher: { name: it.publisher, verified: true },
    contributes: [
      {
        kind: 'mcp',
        connection: { type: 'http', url: 'https://mcp.example.dev' },
        auth: { mode: 'none' },
        tools_preview: ['alpha', 'beta'],
      },
    ],
    ...over,
  }
}

/* The island runs against the same two seams production wires: a fake shell
   on window.RavenShell (T returns its key, so tests assert catalogue keys)
   and a fixture source on window.DS.plugins. */
function install(
  items: MarketItem[],
  rowsArr: InstalledRow[] = [],
  over: Partial<PluginsSource> = {},
) {
  const calls: string[] = []
  const source: PluginsSource = {
    search: async () => ({ items, categories: ['data'] }),
    detail: async (id) => {
      const it = items.find((x) => x.id === id)!
      return { entry: entryOf(it), installed: Boolean(it.installed) }
    },
    install: async (id) => {
      calls.push('install:' + id)
      return { mcp: { name: id, enabled: true, state: 'connected', tool_count: 2 } }
    },
    remove: async (name) => {
      calls.push('remove:' + name)
    },
    toggle: async (name, on) => {
      calls.push(`toggle:${name}:${on}`)
      return null
    },
    togglePy: async (row, on) => {
      row.state = on ? 'on' : 'off'
    },
    auth: async () => null,
    manual: async () => {},
    rows: () => rowsArr,
    /* Recorded: after a connect settles, the shelf has to be re-read or it
       keeps showing the state from before the install. */
    reload: async () => { calls.push('reload') },
    ...over,
  }
  const shellCalls: Array<[string, unknown]> = []
  toastWriter.calls = shellCalls
  const fakeShell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: (id) => shellCalls.push(['showPage', id]),
    useInTask: (key, name) => shellCalls.push(['useInTask', `${key}:${name}`]),
    /* Recorded rather than ignored: the call is this island's contract with
       the legacy caps-page chrome, which owns the tab title, the hero and the
       installed button. It used to be a no-op here, so all five call sites
       could be cut with the suite green. */
    plugRedraw: () => shellCalls.push(['plugRedraw', null]),
  }
  window.RavenShell = fakeShell
  window.DS = { plugins: source }
  document.body.innerHTML =
    '<section class="page" id="capsPage" data-open="true"><div id="capsBody"></div></section>' +
    '<aside class="detail" id="detail" data-open="false"><div id="dTitle"></div><div id="dBody"></div></aside>'
  return { source, calls, shellCalls }
}

async function mount() {
  const view = render(<PlugApp />, { container: document.getElementById('capsBody')! })
  await act(async () => {
    await store.search()
  })
  return view
}

afterEach(() => {
  act(() => {
    store.reset()
    store.drawerClosed()
  })
  cleanup()
  vi.restoreAllMocks()
})

describe('plugins island', () => {
  it('shows every market card the source answers, with the category chips', async () => {
    install([item(), item({ id: 'notion', name: 'Notion', verified: false })])
    await mount()
    expect(await screen.findByText('websearch')).toBeTruthy()
    expect(screen.getByText('Notion')).toBeTruthy()
    expect(screen.getByText('gui.filter.all')).toBeTruthy()
  })

  it('shows the empty note when the search matches nothing', async () => {
    install([])
    await mount()
    expect(await screen.findByText('gui.plug.none_found {"q":""}')).toBeTruthy()
  })

  it('renders a failed search as the market-down note with a retry', async () => {
    install([], [], {
      search: async () => {
        throw new Error('hub unreachable')
      },
    })
    await mount()
    expect(await screen.findByText('gui.plug.market_down')).toBeTruthy()
    expect(screen.getByText('gui.plug.retry')).toBeTruthy()
  })

  it('opens the installed shelf with both groups and comes back to the market', async () => {
    install(
      [item()],
      [
        { id: 'sheets', name: 'sheets', src: 'raven-sheets', ver: '0.9.0', state: 'on' },
        { id: 'mcp:gh', name: 'gh', m: { name: 'gh', enabled: true, state: 'connected', transport: 'http', tool_count: 3 } },
      ],
    )
    await mount()
    act(() => {
      store.toggleView()
    })
    expect(await screen.findByText('gui.plug.grp_builtin')).toBeTruthy()
    expect(screen.getByText('gui.plug.grp_market')).toBeTruthy()
    const back = screen.getByText('← gui.plug.back')
    await act(async () => {
      back.click()
    })
    expect(await screen.findByText('gui.filter.all')).toBeTruthy()
  })

  /* The island sets its own state and then tells the chrome, because the tab
     title, hero and installed button live outside any root it owns. Nothing
     asserted the second half. */
  it('asks the caps chrome to redraw on both view transitions', async () => {
    const { shellCalls } = install([item()], [{ id: 'sheets', name: 'sheets', src: 'raven-sheets', ver: '0.9.0', state: 'on' }])
    await mount()
    const redraws = () => shellCalls.filter((c) => c[0] === 'plugRedraw').length
    const before = redraws()
    act(() => {
      store.toggleView()
    })
    expect(await screen.findByText('gui.plug.grp_builtin')).toBeTruthy()
    expect(redraws()).toBe(before + 1)
    await act(async () => {
      screen.getByText('\u2190 gui.plug.back').click()
    })
    expect(redraws()).toBe(before + 2)
    /* A search redraws twice: once when it goes to loading, once on the
       answer. The chrome shows market state, so a search that only told it at
       the end would leave the old count standing for the length of the call. */
    const beforeSearch = redraws()
    await act(async () => {
      await store.search()
    })
    expect(redraws()).toBe(beforeSearch + 2)
  })

  it('opens the detail drawer from a card and closes with the legacy close path', async () => {
    install([item()])
    await mount()
    await act(async () => {
      ;(await screen.findByText('websearch')).click()
    })
    expect(await screen.findByText('gui.plug.sec_about')).toBeTruthy()
    expect(document.getElementById('detail')!.dataset.open).toBe('true')
    act(() => {
      store.drawerClosed()
    })
    expect(document.getElementById('detail')!.dataset.open).toBe('false')
  })

  it('refuses to connect an api-key plugin with an empty form', async () => {
    const keyed = item({ id: 'keyed', name: 'keyed' })
    const { calls, shellCalls } = install([keyed], [], {
      detail: async () => ({
        entry: entryOf(keyed, {
          contributes: [
            {
              kind: 'mcp',
              connection: { type: 'http', url: 'https://mcp.keyed.dev' },
              auth: { mode: 'apikey', fields: [{ key: 'api_key', label: 'API Key', secret: true }] },
            },
          ],
        }),
        installed: false,
      }),
    })
    await mount()
    await act(async () => {
      ;(await screen.findByText('gui.plug.install')).click()
    })
    expect(await screen.findByText('gui.plug.sec_cred')).toBeTruthy()
    await act(async () => {
      screen.getByText('gui.plug.connect').click()
    })
    expect(shellCalls).toContainEqual(['toast', 'gui.plug.need_key'])
    expect(calls.some((c) => c.startsWith('install:'))).toBe(false)
  })

  it('keeps the shelf standing when a toggle fails handled', async () => {
    install(
      [],
      [{ id: 'mcp:gh', name: 'gh', m: { name: 'gh', enabled: true, state: 'connected', transport: 'http' } }],
      {
        toggle: async () => {
          // What the live source throws after toasting the reason itself.
          throw { handled: true }
        },
      },
    )
    await mount()
    act(() => {
      store.toggleView()
    })
    const swi = (await screen.findAllByRole('switch'))[0]!
    await act(async () => {
      swi.click()
    })
    expect(await screen.findByText('gui.plug.grp_market')).toBeTruthy()
  })

  it('drives a no-auth install through the progress sheet to done', async () => {
    const { calls } = install([item()])
    await mount()
    await act(async () => {
      ;(await screen.findByText('gui.plug.install')).click()
    })
    expect(await screen.findByText('gui.plug.prog_cap')).toBeTruthy()
    expect(await screen.findByText('gui.plug.prog_ok {"n":2}')).toBeTruthy()
    expect(screen.getByText('gui.close')).toBeTruthy()
    /* The install is only half done when the sheet says so: the shelf behind
       it still holds the pre-install answer until the source is re-read, which
       lands a microtask after the progress line the assertions above wait on. */
    await vi.waitFor(() => {
      expect(calls).toContain('reload')
    })
  })
})
