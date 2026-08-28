// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { MemoryApp } from './MemoryPage'
import * as store from './store'

import type { Shell } from '../../shell/bridge'
import type { MemItem, MemStats, MemorySource } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

function item(over: Partial<MemItem> = {}): MemItem {
  return {
    id: 'm1',
    kind: 'episode',
    subject: 'shipped the island',
    summary: 'what the session did',
    body: 'the long form of the episode',
    timestamp: '2026-08-19T08:00:00Z',
    session_id: 's1',
    ...over,
  }
}

/* The island runs against the same two seams production wires: a fake
   shell on window.RavenShell (T returns its key, so tests assert catalogue
   keys, not translations) and a fixture source on window.DS.memory. */
function install(over: Partial<MemorySource> = {}, stats: MemStats | null = null) {
  const calls: string[] = []
  const source: MemorySource = {
    stats: async () => stats,
    list: async () => ({ items: [item()], total: 1 }),
    remove: async () => {
      calls.push('remove')
    },
    ...over,
  }
  const shellCalls: Array<[string, unknown]> = []
  const fakeShell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: (id) => shellCalls.push(['showPage', id]),
    closeDetail: () => {
      const d = document.getElementById('detail')
      if (d) d.dataset.open = 'false'
    },
  }
  window.RavenShell = fakeShell
  window.DS = { memory: source }
  document.body.innerHTML =
    '<section id="memPage"><div id="memBody"></div></section>' +
    '<aside id="detail" data-open="false"><b id="dTitle">—</b><div id="dBody"></div></aside>'
  return { source, calls, shellCalls }
}

async function mount() {
  const view = render(<MemoryApp />, { container: document.getElementById('memBody')! })
  await act(async () => {
    store.open()
  })
  return view
}

afterEach(() => {
  act(() => {
    store.detailDismissed()
    store.setKind('episode')
  })
  cleanup()
  vi.restoreAllMocks()
})

describe('memory island', () => {
  it('shows the rows and the stat band the source answers', async () => {
    install(
      { list: async () => ({ items: [item(), item({ id: 'm2', subject: 'fixed the flake' })], total: 2 }) },
      { episodes: 12, profiles: 1, agent_cases: 3, agent_skills: 4 },
    )
    await mount()
    expect(await screen.findByText('shipped the island')).toBeTruthy()
    expect(screen.getByText('fixed the flake')).toBeTruthy()
    expect(screen.getByText('gui.mem.hero')).toBeTruthy()
    expect(await screen.findByText('12')).toBeTruthy()
    expect(screen.getByText('gui.mem.n_total {"n":2}')).toBeTruthy()
  })

  it('shows the empty note when the kind has nothing', async () => {
    install({ list: async () => ({ items: [], total: 0 }) })
    await mount()
    expect(await screen.findByText('gui.mem.empty')).toBeTruthy()
  })

  it('renders the demo down note, without the stat band', async () => {
    install({
      list: async () => {
        // What the fixture source answers: the demo has no memory engine.
        throw { down: true }
      },
    })
    await mount()
    expect(await screen.findByText('gui.mem.down')).toBeTruthy()
    expect(screen.queryByText('gui.mem.tab_episode')).toBeNull()
  })

  it('renders a live failure inline with its detail and recovers on retry', async () => {
    let failed = false
    install({
      list: async () => {
        if (!failed) {
          failed = true
          throw new Error('engine offline')
        }
        return { items: [item()], total: 1 }
      },
    })
    await mount()
    expect(await screen.findByText('gui.mem.down · engine offline')).toBeTruthy()
    await act(async () => {
      screen.getByText('gui.plug.retry').click()
    })
    expect(await screen.findByText('shipped the island')).toBeTruthy()
  })

  it('opens the detail drawer from a row', async () => {
    install()
    await mount()
    await act(async () => {
      ;(await screen.findByText('shipped the island')).click()
    })
    expect(document.getElementById('detail')!.dataset.open).toBe('true')
    expect(screen.getByText('gui.mem.sec_detail')).toBeTruthy()
    expect(screen.getByText('the long form of the episode')).toBeTruthy()
  })

  it('repaints its own item after another page borrowed the drawer', async () => {
    install()
    await mount()
    await act(async () => {
      ;(await screen.findByText('shipped the island')).click()
    })
    expect(screen.getByText('the long form of the episode')).toBeTruthy()
    /* What the skills opener does when it takes over the shared drawer:
       wipes #dBody wholesale and re-sets the already-true open flag. */
    const dBody = document.getElementById('dBody')!
    await act(async () => {
      dBody.innerHTML = '<div>SKILL DETAIL</div>'
      document.getElementById('detail')!.dataset.open = 'true'
    })
    await act(async () => {
      ;(await screen.findByText('shipped the island')).click()
    })
    expect(dBody.textContent).toContain('the long form of the episode')
    expect(dBody.textContent).not.toContain('SKILL DETAIL')
  })

  it('keeps the drawer open when a delete fails handled', async () => {
    install({
      remove: async () => {
        // What the live source throws after toasting the reason itself.
        throw { handled: true }
      },
    })
    await mount()
    await act(async () => {
      ;(await screen.findByText('shipped the island')).click()
    })
    const del = screen.getByText('gui.mem.delete')
    await act(async () => {
      del.click()
    })
    await act(async () => {
      screen.getByText('gui.mem.confirm_del').click()
    })
    expect(document.getElementById('detail')!.dataset.open).toBe('true')
    expect(screen.getByText('gui.mem.sec_detail')).toBeTruthy()
  })

  it('closes the drawer and reloads after a successful delete', async () => {
    const { source, calls } = install()
    const listSpy = vi.spyOn(source, 'list')
    await mount()
    await act(async () => {
      ;(await screen.findByText('shipped the island')).click()
    })
    await act(async () => {
      screen.getByText('gui.mem.delete').click()
    })
    await act(async () => {
      screen.getByText('gui.mem.confirm_del').click()
    })
    expect(calls).toContain('remove')
    expect(document.getElementById('detail')!.dataset.open).toBe('false')
    expect(listSpy.mock.calls.length).toBeGreaterThan(1)
  })

  it('switches kind through a stat and reloads with it', async () => {
    const asked: string[] = []
    install({
      list: async (req) => {
        asked.push(req.kind)
        return { items: [], total: 0 }
      },
    })
    await mount()
    await act(async () => {
      screen.getByText('gui.mem.tab_case').click()
    })
    expect(asked).toContain('agent_case')
  })
})
