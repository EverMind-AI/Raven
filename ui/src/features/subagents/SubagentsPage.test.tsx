// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as mount_ from './mount'
import { SubagentsApp } from './SubagentsPage'
import * as store from './store'

import type { Shell } from '../../shell/bridge'
import type { AgentCtx, AgentRow, AgentsSource } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const paints: Array<{ ctx: unknown; opts?: { key?: string; empty?: string; reset?: boolean } }> = []

/* The island runs against the same two seams production wires up: a fake
   shell on window.RavenShell (T returns its key, dur a deterministic stamp)
   and a source on window.DS.agents -- the fixture shape for demo behaviour,
   list/context/node for live behaviour. */
function wire(source: AgentsSource): void {
  paints.length = 0
  const fakeShell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    toast: () => {},
    menuAt: () => {},
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: () => {},
    wsShows: (tab) => tab === 'agents',
    sessionKey: () => 's1',
    dur: (ms) => `${Math.round(ms / 1000)}s`,
    plainTitle: (s) => s.replace(/^\p{Extended_Pictographic}+\s*/u, ''),
    agentStagePaint: (box, ctx, opts) => {
      paints.push({ ctx, opts })
      box.appendChild(document.createElement('p'))
    },
  }
  window.RavenShell = fakeShell
  window.DS = { agents: source }
  document.body.innerHTML =
    '<span id="wsAgentRun" hidden></span><div class="ws-body" id="wsBody" data-view="agents"></div>'
}

function rows(items: AgentRow[], over: Partial<AgentsSource> = {}): AgentsSource {
  const source: AgentsSource = { list: async () => items, ...over }
  wire(source)
  return source
}

async function mount() {
  const view = render(<SubagentsApp />, { container: document.getElementById('wsBody')! })
  store.attached(true)
  /* The list effect asks the source on mount; let the answer land. */
  await act(async () => {
    await Promise.resolve()
  })
  return view
}

const iso = (ms: number): string => new Date(ms).toISOString()

afterEach(() => {
  /* A root the mount module owns is not one testing-library knows to clean
     up, so a test that draws through it has to hand it back. */
  act(() => {
    mount_.detach()
  })
  cleanup()
  store._resetForTests()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('subagents island, the list', () => {
  it('renders one row per run with status word, clock, stamp, cost and owner', async () => {
    const t0 = Date.now() - 65000
    rows([
      {
        id: 'a1', label: '🦉 survey the repo', status: 'ok', agent: 'Researcher',
        started_at: iso(t0), ended_at: iso(t0 + 61000), tokens: 1500,
      },
    ])
    await mount()
    expect(await screen.findByText('survey the repo')).toBeTruthy()
    expect(screen.getByText('gui.ws.agent_ok')).toBeTruthy()
    expect(screen.getByText('61s')).toBeTruthy()
    expect(screen.getByText('1.5k')).toBeTruthy()
    expect(screen.getByText('Researcher')).toBeTruthy()
    const row = document.querySelector('.salist .sarow') as HTMLElement
    expect(row.getAttribute('role')).toBe('button')
    expect(row.querySelector('.dot.ok')).toBeTruthy()
    expect(row.querySelector('.sp')?.hasAttribute('data-t0')).toBe(false)
  })

  it('shows the empty note, and the absent wording when the surface is gone', async () => {
    rows([])
    await mount()
    expect(await screen.findByText('gui.ws.agents_none')).toBeTruthy()
    cleanup()
    store._resetForTests()
    rows([], { absent: () => true })
    await mount()
    expect(await screen.findByText('gui.ws.agents_absent')).toBeTruthy()
  })

  it('keeps the last list when a refresh merely fails', async () => {
    const source = rows([{ id: 'a1', label: 'alive run', status: 'run', started_at: iso(Date.now() - 5000) }])
    await mount()
    expect(await screen.findByText('alive run')).toBeTruthy()
    source.list = async () => {
      throw new Error('socket dropped')
    }
    await act(async () => {
      store.refresh(true)
      await Promise.resolve()
    })
    expect(screen.getByText('alive run')).toBeTruthy()
    expect(document.querySelector('.wsempty')).toBeNull()
  })

  it('answers -32601 semantics (an empty answer) by emptying the list', async () => {
    const source = rows([{ id: 'a1', label: 'old run', status: 'ok', started_at: iso(Date.now() - 9000), ended_at: iso(Date.now() - 4000) }])
    await mount()
    expect(await screen.findByText('old run')).toBeTruthy()
    source.list = async () => []
    await act(async () => {
      store.refresh(true)
      await Promise.resolve()
    })
    expect(screen.queryByText('old run')).toBeNull()
    expect(screen.getByText('gui.ws.agents_none')).toBeTruthy()
  })

  it('ticks a running row once a second through the one store clock', async () => {
    vi.useFakeTimers()
    const t0 = Date.now() - 5000
    rows([{ id: 'a1', label: 'working', status: 'run', started_at: iso(t0) }])
    await mount()
    const sp = document.querySelector('.sarow .sp') as HTMLElement
    expect(sp.textContent).toBe('5s')
    expect(sp.dataset.t0).toBe(String(new Date(iso(t0)).getTime()))
    expect(document.querySelector('.sarow .wkg.sw')).toBeTruthy()
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000)
    })
    expect((document.querySelector('.sarow .sp') as HTMLElement).textContent).toBe('6s')
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    expect((document.querySelector('.sarow .sp') as HTMLElement).textContent).toBe('8s')
  })

  it('lists a graph as its runs -- dead branch included -- and hides the queued', async () => {
    const t0 = Date.now() - 30000
    rows([
      { kind: 'dag', run_id: 'r1', node: 'survey', agent: 'Researcher', label: 'survey', status: 'ok', started_at: iso(t0), ended_at: iso(t0 + 3000) },
      { kind: 'dag', run_id: 'r1', node: 'read_a', agent: 'Coder', label: 'read_a', status: 'error', started_at: iso(t0 + 3000), ended_at: iso(t0 + 9000) },
      { kind: 'dag', run_id: 'r1', node: 'merge', agent: 'Writer', label: 'merge', status: 'skipped' },
      { kind: 'dag', run_id: 'r1', node: 'review', agent: 'Critic', label: 'review', status: 'queued' },
    ])
    await mount()
    const marks = [...document.querySelectorAll('.salist .sarow .dot')].map((d) => d.className)
    expect(marks).toEqual(['dot ok', 'dot bad', 'dot que'])
    expect(screen.queryByText('review')).toBeNull()
    expect(screen.getByText('gui.ws.agent_skip')).toBeTruthy()
    expect(screen.getByText('gui.ws.agent_bad')).toBeTruthy()
    expect(document.querySelectorAll('.salist .sarow .gr')).toHaveLength(3)
    /* The dead branch never ran: no clock, no stamp, just the mark and word. */
    const dead = screen.getByText('merge').closest('.sarow') as HTMLElement
    expect(dead.querySelector('.sp')).toBeNull()
    expect(dead.querySelector('.ed')).toBeNull()
  })
})

describe('subagents island, the detail', () => {
  it('opens a spawn row into header plus stage, painted from the record', async () => {
    const ctx: AgentCtx = { status: 'ok', agent: 'openclaw', messages: [] }
    rows(
      [{ id: 'a1', label: 'survey', status: 'ok', agent: null, started_at: iso(Date.now() - 9000), ended_at: iso(Date.now() - 2000), tokens: 40 }],
      { context: async () => ctx },
    )
    await mount()
    await act(async () => {
      ;(await screen.findByText('survey')).closest('.sarow')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(document.querySelector('.sahd .back')).toBeTruthy()
    expect(document.querySelector('.sahd .trow b')?.textContent).toBe('survey')
    expect(document.querySelector('.sahd .trow .tk')?.textContent).toBe('40')
    /* The answer corrects the owner the listed row understated. */
    expect(document.querySelector('.sahd .trow .who')?.textContent).toBe('openclaw')
    expect(paints).toHaveLength(1)
    expect(paints[0]!.opts).toMatchObject({ key: 'sp:a1', reset: true })
    expect(document.querySelector('.satx p')).toBeTruthy()
    await act(async () => {
      ;(document.querySelector('.sahd .back') as HTMLButtonElement).click()
    })
    expect(document.querySelector('.salist')).toBeTruthy()
  })

  it('starts the stage over when a reopened panel hands it a new box', async () => {
    const ctx: AgentCtx = { status: 'ok', agent: 'openclaw', messages: [] }
    rows(
      [{ id: 'a1', label: 'survey', status: 'ok', agent: null, started_at: iso(Date.now() - 9000), ended_at: iso(Date.now() - 2000), tokens: 40 }],
      { context: async () => ctx },
    )
    const host = document.getElementById('wsBody')!
    /* Through the mount module, because the remount is what is being tested:
       drawWs() detaches, wipes the panel body and draws a fresh root. */
    await act(async () => {
      mount_.draw(host)
    })
    await act(async () => {
      await Promise.resolve()
    })
    await act(async () => {
      ;(await screen.findByText('survey')).closest('.sarow')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(paints).toHaveLength(1)
    expect(paints[0]!.opts).toMatchObject({ key: 'sp:a1', reset: true })
    /* Collapsing and reopening the panel. The open record never changed, so a
       paint that reads itself as a continuation would append the slice it drew
       already -- nothing -- into the box the wipe just emptied. */
    await act(async () => {
      mount_.detach()
      host.innerHTML = ''
      mount_.draw(host)
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(paints).toHaveLength(2)
    expect(paints[1]!.opts).toMatchObject({ key: 'sp:a1', reset: true })
    expect(document.querySelector('.satx p')).toBeTruthy()
    expect(document.querySelector('.satx .wsempty')).toBeNull()
  })

  it('opens a dag row through dag.node and appends the truncation note once', async () => {
    rows(
      [{ kind: 'dag', run_id: 'r1', node: 'merge', agent: 'Writer', label: 'merge', status: 'ok', started_at: iso(Date.now() - 9000), ended_at: iso(Date.now() - 2000) }],
      { node: async () => ({ messages: [], output_truncated: true }) },
    )
    await mount()
    await act(async () => {
      ;(await screen.findByText('merge')).closest('.sarow')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(document.querySelector('.sahd .trow b')?.textContent).toBe('merge')
    expect(document.querySelector('.sahd .trow .who')?.textContent).toBe('Writer')
    expect(paints[0]!.opts).toMatchObject({ key: 'dag:r1:merge', empty: 'gui.dag.node_empty', reset: true })
    expect(document.querySelectorAll('.satx > .wsnote')).toHaveLength(1)
  })

  it('keeps the demo detail an honest empty note when the source has no records', async () => {
    rows([{ id: 'a1', label: 'survey', status: 'ok', started_at: iso(Date.now() - 9000), ended_at: iso(Date.now() - 2000) }])
    await mount()
    await act(async () => {
      ;(await screen.findByText('survey')).closest('.sarow')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    expect(document.querySelector('.satx .wsempty')?.textContent).toBe('gui.ws.agents_none')
    expect(paints).toHaveLength(0)
  })

  it('shows the read failure in the stage when the record cannot be fetched', async () => {
    rows(
      [{ id: 'a1', label: 'survey', status: 'run', started_at: iso(Date.now() - 9000) }],
      {
        context: async () => {
          throw new Error('record gone')
        },
      },
    )
    await mount()
    await act(async () => {
      ;(await screen.findByText('survey')).closest('.sarow')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(document.querySelector('.satx .wsempty')?.textContent).toBe('record gone')
  })
})
