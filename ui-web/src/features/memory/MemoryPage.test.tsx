// @vitest-environment happy-dom
import { act, cleanup, render, screen, within } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { setTranslator } from '../../i18n/t'
import * as confirmStore from '../../state/confirm'
import { resetSources, setSources } from '../../state/sources'
import { domSnapshot } from '../../test/domSnapshot'
import { MemoryApp } from './MemoryPage'
import * as store from './store';

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

/* The island runs against the same two seams production wires: a stand-in
   translator on setTranslator (it returns its key, so tests assert catalogue
   keys, not translations) and a fixture source on sources.memory. */
function install(over: Partial<MemorySource> = {}, stats: MemStats | null = null) {
  const source: MemorySource = {
    stats: async () => stats,
    list: async () => ({ items: [item()], total: 1 }),
    ...over,
  }
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  vi.spyOn(confirmStore, 'ask').mockImplementation((_t, _b, _l, fn) => fn())
  setSources({ memory: source })
  document.body.innerHTML = '<div id="memoryBody"></div>'
  return { source }
}

/* The section as the dialog hosts it: the island in its own box, and the two
   reads arriving at the section costs (features/memory/store.ts's `enter`,
   which state/settings.ts spends -- here it is called by hand, because the
   dialog is not what this file is about). */
async function mount() {
  const view = render(<MemoryApp />, { container: document.getElementById('memoryBody')! })
  await act(async () => {
    store.setKind('episode')
    await Promise.all([store.load(), store.refreshStats()])
  })
  return view
}

/* Scoped to the list: the picked memory carries the same subject in its own
   header, so an unscoped query answers two elements once a row is open. */
const row = (text: string): HTMLElement =>
  within(document.querySelector('.two-pane-side') as HTMLElement).getByText(text)

afterEach(() => {
  cleanup()
  act(() => { store._resetForTests() })
  vi.restoreAllMocks()
  resetSources()
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

  it('says why the page is empty instead of showing an empty stat band', async () => {
    /* No memory plugin, or one the config does not name: not a failure, and
       four zeros read as "your memories are gone" rather than "they are not
       kept here". */
    const note = 'Long-term memory runs on mem0, and this page reads EverOS only.'
    install({ list: async () => ({ items: [], total: 0, note }) })
    await mount()
    expect(await screen.findByText(note)).toBeTruthy()
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

  /* Beside the list rather than in the shared drawer: inside the settings
     dialog a drawer is a layer over a layer, and the list it covered is what a
     reader comparing two memories needs to keep. */
  it('shows the picked memory beside the list, and drops it on a kind switch', async () => {
    install()
    await mount()
    await act(async () => {
      row('shipped the island').click()
    })
    expect(screen.getByText('gui.mem.sec_detail')).toBeTruthy()
    expect(screen.getByText('the long form of the episode')).toBeTruthy()
    /* The pick belongs to the kind that was listed, so switching kind has to
       drop it -- otherwise the reader is left reading a row from a tab they
       have left. */
    await act(async () => {
      screen.getByText('gui.mem.tab_case').click()
    })
    expect(screen.queryByText('gui.mem.sec_detail')).toBeNull()
  })

  it('says to pick one until a row is picked', async () => {
    install()
    await mount()
    expect(await screen.findByText('gui.mem.pick')).toBeTruthy()
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

  it('keeps its rendered shape', async () => {
    install(
      { list: async () => ({ items: [item(), item({ id: 'm2', subject: 'fixed the flake' })], total: 2 }) },
      { episodes: 12, profiles: 1, agent_cases: 3, agent_skills: 4 },
    )
    await mount()
    await screen.findByText('shipped the island')
    expect(domSnapshot(document.getElementById('memoryBody')!)).toMatchSnapshot()
  })
})
