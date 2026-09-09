// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { useSyncExternalStore } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as mount_ from './mount'
import {
  AgentList,
  AgentRecordConversation,
  InstanceConversation,
  InstanceRowView,
  orderAgentGroups,
  SubagentsApp,
} from './SubagentsPage'
import * as store from './store'
import { _resetForTests as sessionReset, setCurrent } from '../../shell/session'

import type { Shell } from '../../shell/bridge'
import type { JSX } from 'react'
import type { AgentCtx, AgentRow, AgentsSource, DirectTurn, InstanceRow, SubagentRow } from './types'

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
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: () => {},
    wsShows: (tab) => tab === 'agents',
  }
  window.RavenShell = fakeShell
  sessionReset()
  setCurrent('s1')
  source.stagePaint = (box, ctx, opts) => {
    paints.push({ ctx, opts })
    box.appendChild(document.createElement('p'))
  }
  window.DS = { agents: source }
  document.body.innerHTML =
    '<span id="wsAgentRun" hidden></span><div class="ws-body" id="wsBody" data-view="agents"></div>'
}

function rows(items: AgentRow[], over: Partial<AgentsSource> = {}): AgentsSource {
  const source: AgentsSource = { list: async () => items, ...over }
  wire(source)
  return source
}

/* One instance row, with only the fields a caller cares about spelled out. The
   wire type requires `sessionKey` and `kind` on every row, which is the point of
   taking it from the generated contract -- but they are noise in a list test. */
function inst(over: Partial<InstanceRow> & { handle: string }): InstanceRow {
  return { sessionKey: 's1', agent: 'hermes', kind: 'cli', ...over }
}

/* The panel drawing instances rather than runs: `list` still answers, because a
   run detail opened from the conversation reads its header off that list. */
function instances(items: InstanceRow[], over: Partial<AgentsSource> = {}): AgentsSource {
  const source: AgentsSource = { list: async () => [], instances: async () => items, ...over }
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

/* The grouped-by-agent list, which is the desk palette's variant of the same
   component -- the standalone panel draws one flat list of instances and has no
   agent headings for a button to sit beside. */
function Grouped({ onOpen }: { onOpen?: (row: InstanceRow) => void }): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.getState)
  return <AgentList s={s} onOpen={onOpen} compact />
}

async function mountGrouped(onOpen?: (row: InstanceRow) => void) {
  const view = render(<Grouped onOpen={onOpen} />, { container: document.getElementById('wsBody')! })
  store.attached(true)
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
  sessionReset()
  window.RavenIslands = undefined
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('subagents island, the list', () => {
  it('routes an existing instance opener into the floating workspace', () => {
    const openAgent = vi.fn()
    window.RavenIslands = { workspace: { openAgent } }
    const row = inst({ handle: 'resume-me', resumable: true })
    store.openInstance(row)
    /* Second argument is the record this promotion replaces; a row opened
       from the list is replacing nothing. */
    expect(openAgent).toHaveBeenCalledWith(row, null)
    /* And records WHAT it opened. Leaving `open` on the node it was promoted
       from is what made the promotion below fire again on every heartbeat. */
    expect(store.getState().open).toEqual({ kind: 'instance', agent: 'hermes', handle: 'resume-me' })
  })

  it('routes a legacy run detail into a workspace record pane', () => {
    const openAgentRecord = vi.fn()
    window.RavenIslands = { workspace: { openAgentRecord } }
    const row: AgentRow = { id: 'spawn-1', kind: 'spawn', label: 'legacy task' }
    store.openRow(row)
    expect(openAgentRecord).toHaveBeenCalledWith(row)
  })

  it('orders agents and their conversations by the latest instance activity', () => {
    const roster = [
      { name: 'quiet-first' },
      { name: 'recent' },
      { name: 'older' },
      { name: 'quiet-last' },
    ] as SubagentRow[]
    const ordered = orderAgentGroups(roster, [
      inst({ agent: 'older', handle: 'older-new', updatedAtMs: 200 }),
      inst({ agent: 'recent', handle: 'recent-old', updatedAtMs: 100 }),
      inst({ agent: 'recent', handle: 'recent-new', updatedAtMs: 300 }),
    ])
    expect(ordered.map((group) => group.name))
      .toEqual(['recent', 'older', 'quiet-first', 'quiet-last'])
    expect(ordered[0]!.children.map((row) => row.handle))
      .toEqual(['recent-new', 'recent-old'])
  })

  it('renders one row per instance: handle, agent, status word and tags', async () => {
    instances([
      inst({ handle: 'research-a2a-726da8', status: 'completed', resumable: true, runId: 'r1', nodeId: 'research_a2a' }),
    ])
    await mount()
    /* Titled by the node, which is what the conversation calls this work; the
       handle it minted for itself stays reachable on the row. */
    expect(await screen.findByText('research_a2a')).toBeTruthy()
    expect(screen.getByText('hermes')).toBeTruthy()
    expect(screen.getByText('gui.ws.instance_done')).toBeTruthy()
    const row = document.querySelector('.salist .sarow') as HTMLElement
    expect(row.querySelector('.nm')?.getAttribute('title')).toBe('research-a2a-726da8')
    expect(row.getAttribute('role')).toBe('button')
    expect(row.querySelector('.dot.ok')).toBeTruthy()
    /* Both facts about this row, as tags rather than as two headings. */
    expect([...row.querySelectorAll('.gr')].map((g) => g.textContent))
      .toEqual(['gui.ws.instance_resumable', 'gui.ws.instance_of_graph'])
  })

  it('draws a graph fan-out as one row per node, not two', async () => {
    /* What the panel showed before: four hermes nodes came back as eight rows,
       each node once as its `<run>/<node>` status row and once as the handle it
       ran on. The pairing is the server's job now, so the page's contract is
       simply that it draws the rows it is given -- and that a node's row is
       marked as a graph's work rather than filed under a second heading. */
    instances(
      ['research_a2a', 'research_mcp', 'research_acp', 'synthesize'].map((n, i) =>
        inst({ handle: `${n.replace(/_/g, '-')}-${i}`, status: 'completed', resumable: true, runId: 'r1', nodeId: n }),
      ),
    )
    await mount()
    await screen.findByText('research_a2a')
    expect(document.querySelectorAll('.salist .sarow')).toHaveLength(4)
    expect(document.querySelectorAll('.sahdr')).toHaveLength(0)
    expect(screen.getAllByText('gui.ws.instance_of_graph')).toHaveLength(4)
  })

  it('marks running, idle and failed apart from finished', async () => {
    /* The registry's words are not the run marker's. Handed over unmapped,
       `running` and `failed` both fell through to the settled green dot, so a
       working instance and a broken one looked done. */
    instances([
      inst({ handle: 'live', status: 'running' }),
      inst({ handle: 'waiting', status: 'idle' }),
      inst({ handle: 'broke', status: 'failed' }),
      inst({ handle: 'gone', status: 'interrupted' }),
      inst({ handle: 'finished', status: 'completed' }),
    ])
    await mount()
    await screen.findByText('live')
    const live = screen.getByText('live').closest('.sarow') as HTMLElement
    expect(live.querySelector('.wkg.sw')).toBeTruthy()
    expect(live.querySelector('.dot')).toBeNull()
    /* Nothing is written next to the working glyph: it already says it. */
    expect(live.querySelector('.st span')?.className).toBe('who')
    const dot = (h: string): string | undefined =>
      screen.getByText(h).closest('.sarow')?.querySelector('.dot')?.className
    expect(dot('waiting')).toBe('dot que')
    expect(dot('broke')).toBe('dot bad')
    expect(dot('gone')).toBe('dot bad')
    expect(dot('finished')).toBe('dot ok')
    expect(screen.getByText('gui.ws.instance_idle')).toBeTruthy()
    expect(screen.getAllByText('gui.ws.instance_bad')).toHaveLength(2)
  })

  it('shows the empty note, and the absent wording when the surface is gone', async () => {
    instances([])
    await mount()
    expect(await screen.findByText('gui.ws.agents_none')).toBeTruthy()
    cleanup()
    store._resetForTests()
    instances([], { absent: () => true })
    await mount()
    expect(await screen.findByText('gui.ws.agents_absent')).toBeTruthy()
  })

  it('keeps the last list when a refresh merely fails', async () => {
    const source = instances([inst({ handle: 'alive', status: 'running' })])
    await mount()
    expect(await screen.findByText('alive')).toBeTruthy()
    source.instances = async () => {
      throw new Error('socket dropped')
    }
    await act(async () => {
      store.refreshInstances(true)
      await Promise.resolve()
    })
    expect(screen.getByText('alive')).toBeTruthy()
    expect(document.querySelector('.wsempty')).toBeNull()
  })

  it('answers -32601 semantics (an empty answer) by emptying the list', async () => {
    const source = instances([inst({ handle: 'old', status: 'completed' })])
    await mount()
    expect(await screen.findByText('old')).toBeTruthy()
    source.instances = async () => []
    await act(async () => {
      store.refreshInstances(true)
      await Promise.resolve()
    })
    expect(screen.queryByText('old')).toBeNull()
    expect(screen.getByText('gui.ws.agents_none')).toBeTruthy()
  })

  it('removes a row without opening it', async () => {
    const forgot: Array<[string, string]> = []
    instances([inst({ handle: 'stale', status: 'completed' })], {
      instanceForget: async (agent, handle) => {
        forgot.push([agent, handle])
      },
    })
    await mount()
    await screen.findByText('stale')
    await act(async () => {
      ;(document.querySelector('.sarow .mini') as HTMLButtonElement).click()
    })
    expect(forgot).toEqual([['hermes', 'stale']])
    /* Gone from view before the server answers, and the click never reached the
       row underneath it. */
    expect(screen.queryByText('stale')).toBeNull()
    expect(document.querySelector('.satx')).toBeNull()
  })

  /* One roster of four, so a single mount can say which agents get the button
     and which do not. `hermes` is the only one that can hold a direct chat. */
  function startable(over: Partial<AgentsSource> = {}): AgentsSource {
    const roster = [
      { name: 'hermes', enabled: true, stateful: true },
      { name: 'mute', enabled: true, stateful: false },
      { name: 'off', enabled: false, stateful: true },
      { name: 'old', enabled: true },
    ] as SubagentRow[]
    return instances([inst({ handle: 'one' })], { roster: async () => roster, ...over })
  }

  const plusFor = (agent: string): HTMLButtonElement | null =>
    Array.from(document.querySelectorAll<HTMLElement>('.agent-group')).find(
      (g) => g.querySelector('.agent-head b')?.textContent === agent,
    )?.querySelector('.agent-new') ?? null

  it('offers a new instance only for an agent that can hold a direct chat', async () => {
    startable()
    await mountGrouped()
    await screen.findByText('hermes')
    /* Stateless, disabled, and a server too old to say either: the create
       refuses all three, so a button on them would be a refusal on click. */
        expect(plusFor('hermes')).not.toBeNull()
    expect(plusFor('mute')).toBeNull()
    expect(plusFor('off')).toBeNull()
    expect(plusFor('old')).toBeNull()
  })

  it('creates an instance, lists it, and opens it', async () => {
    const asked: Array<[string, string]> = []
    let listed: InstanceRow[] = [inst({ handle: 'one' })]
    const fresh = inst({ handle: 'fresh', status: 'idle' })
    startable({
      instances: async () => listed,
      instanceCreate: async (agent, sessionKey) => {
        asked.push([agent, sessionKey])
        listed = [...listed, fresh]
        return fresh
      },
    })
    await mountGrouped()
    await screen.findByText('hermes')
    await act(async () => {
      plusFor('hermes')!.click()
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(asked).toEqual([['hermes', 's1']])
    /* Both, which is the whole ask: the panel is inside the new instance, and
       the instance is on the list waiting when `back()` leaves it. */
    expect(store.getState().open).toEqual({ kind: 'instance', agent: 'hermes', handle: 'fresh' })
    expect(store.getState().instances.map((r) => r.handle)).toContain('fresh')
    act(() => {
      store.back()
    })
    expect(store.getState().open).toBeNull()
    expect(store.getState().instances.map((r) => r.handle)).toContain('fresh')
  })

  it('does not open one conversation\'s new instance in another', async () => {
    /* The create is a round trip, and the reader can switch conversations inside
       it. `refreshInstances` already guards its own answer this way
       (`const asked = sessionKey()` ... `if (asked !== sessionKey()) return`); the
       create's continuation did not, so the row minted for s1 was opened under
       s2 -- and the panel then showed an instance the new conversation never
       made. */
    let release: ((row: InstanceRow) => void) | null = null
    const fresh = inst({ sessionKey: 's1', handle: 'fresh' })
    startable({
      instanceCreate: () => new Promise<InstanceRow>((resolve) => { release = resolve }),
    })
    await mountGrouped()
    await screen.findByText('hermes')
    await act(async () => {
      plusFor('hermes')!.click()
    })
    /* The switch, as production does it: the store is cleared with the session
       (`wsReset` calls `subagents.reset`) and the new session is set. */
    act(() => {
      store.reset()
      setCurrent('s2')
    })
    await act(async () => {
      release!(fresh)
      await Promise.resolve()
    })
    /* Nothing opened, and nothing was written into the new conversation. */
    expect(store.getState().open).toBeNull()
    expect(store.getState().startFail).toBeNull()
    expect(store.getState().instances.map((r) => r.handle)).not.toContain('fresh')
    /* And the button in the new conversation is usable: a `starting` left set by
       the conversation that has gone would disable it for good. */
    expect(store.getState().starting).toBeNull()
  })

  it('does not let a settled request release a newer one\'s button', async () => {
    /* Two creates overlap across a switch. `starting` names the request the panel
       is waiting on, so only that request may release it -- a finalizer that
       clears unconditionally re-enables the button while the newer RPC is still
       out, and the next press sends a duplicate. */
    const gates: Array<(row: InstanceRow) => void> = []
    startable({
      instanceCreate: () => new Promise<InstanceRow>((resolve) => { gates.push(resolve) }),
    })
    await mountGrouped()
    await screen.findByText('hermes')
    await act(async () => {
      plusFor('hermes')!.click()
    })
    expect(store.getState().starting).toBe('hermes')

    act(() => {
      store.reset()
      setCurrent('s2')
    })
    /* The roster leaves with the session, and the panel asks again for the one it
       arrived at -- so the heading, and the button on it, come back. */
    await act(async () => {
      store.refreshRoster(true)
      await Promise.resolve()
      await Promise.resolve()
    })
    /* The successor, in the new conversation and still in flight. */
    await act(async () => {
      plusFor('hermes')!.click()
    })
    expect(store.getState().starting).toBe('hermes')

    /* Now the one from the conversation the reader left comes back. */
    await act(async () => {
      gates[0]!(inst({ sessionKey: 's1', handle: 'stale' }))
      await Promise.resolve()
    })
    /* Still held: the s2 request has not answered. */
    expect(store.getState().starting).toBe('hermes')

    /* And when the successor does answer, it releases its own. */
    await act(async () => {
      gates[1]!(inst({ sessionKey: 's2', handle: 'fresh' }))
      await Promise.resolve()
    })
    expect(store.getState().starting).toBeNull()
  })

  it('does not carry a refusal into the conversation the reader moved to', async () => {
    /* The other half of the switch: the create can also FAIL after it. A refusal
       written then belongs to a conversation nobody is looking at, and shows up
       under one that never asked for anything. */
    let refuse: ((e: Error) => void) | null = null
    startable({
      instanceCreate: () => new Promise<InstanceRow>((_, reject) => { refuse = reject }),
    })
    await mountGrouped()
    await screen.findByText('hermes')
    await act(async () => {
      plusFor('hermes')!.click()
    })
    act(() => {
      store.reset()
      setCurrent('s2')
    })
    await act(async () => {
      refuse!(new Error('hermes is disabled'))
      await Promise.resolve()
    })
    expect(store.getState().startFail).toBeNull()
    expect(store.getState().starting).toBeNull()
  })

  it('does not blame the creation for a failure after it', async () => {
    /* Everything after the create happens to an instance the server has already
       minted. Reporting a later failure as a start failure would say the run
       could not be created with its row sitting in the list underneath. */
    const fresh = inst({ handle: 'fresh', status: 'idle' })
    startable({
      instances: async () => [inst({ handle: 'one' }), fresh],
      instanceCreate: async () => fresh,
    })
    await mountGrouped(() => { throw new Error('the desk pane blew up') })
    await screen.findByText('hermes')
    await act(async () => {
      plusFor('hermes')!.click()
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(store.getState().startFail).toBeNull()
    expect(store.getState().instances.map((r) => r.handle)).toContain('fresh')
    /* And it is released either way, so the button can be pressed again. */
    expect(store.getState().starting).toBeNull()

    /* The call still RESOLVES, and resolves true: the start happened. Asserted on
       the promise because the button discards it, and an opener that throws with
       nothing catching it leaves an unhandled rejection nobody sees -- which is
       what the guard around the open is for, and what asserting only on the store
       state cannot tell apart. */
    let settled: unknown = 'never'
    await act(async () => {
      settled = await store.startInstance('hermes', () => { throw new Error('again') })
    })
    expect(settled).toBe(true)
  })

  it('reads the reason out of the reply, not the wire code beside it', async () => {
    /* What a refused create actually looks like coming off the socket: the
       `message` is the CODE and the sentence rides in `data.detail`. Reading
       `message` drew `config_validation_error` here -- and `internal_error`
       before the server typed the refusal at all. The existing case above
       throws a plain `Error`, whose message IS the sentence, so it passes
       either way; this is the shape that tells them apart. */
    startable({
      instanceCreate: async () => {
        throw {
          code: -32011,
          message: 'config_validation_error',
          data: { detail: "Cannot create a new instance: 'hermes' is stateless, so each turn would start a fresh conversation with no memory of this one. Spawn it with a task instead." },
        }
      },
    })
    await mountGrouped()
    await screen.findByText('hermes')
    await act(async () => { plusFor('hermes')!.click() })
    await act(async () => { await Promise.resolve() })

    const said = document.querySelector('.agent-newfail')?.textContent || ''
    expect(said).toContain('Spawn it with a task instead')
    expect(said).not.toContain('config_validation_error')
  })

  it('falls back to the message when a rejection carries no detail', async () => {
    /* Not every rejection is typed -- a dropped socket has a message and no
       detail -- and a reader given an empty line learns less than one given
       the transport's own words. */
    startable({ instanceCreate: async () => { throw { message: 'socket closed' } } })
    await mountGrouped()
    await screen.findByText('hermes')
    await act(async () => { plusFor('hermes')!.click() })
    await act(async () => { await Promise.resolve() })

    expect(document.querySelector('.agent-newfail')?.textContent).toContain('socket closed')
  })

  it('says why a creation was refused, on the agent that refused it', async () => {
    startable({ instanceCreate: async () => { throw new Error('hermes is stateless') } })
    await mountGrouped()
    await screen.findByText('hermes')
    await act(async () => {
      plusFor('hermes')!.click()
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(document.querySelector('.agent-newfail')?.textContent).toContain('hermes is stateless')
    expect(store.getState().open).toBeNull()
    /* Released, not stuck: the button has to take a second try. */
    expect(store.getState().starting).toBeNull()
    expect(plusFor('hermes')!.disabled).toBe(false)
  })
})

/* A run detail is no longer reached from this list -- the panel lists instances
   now -- so it is opened the way production opens it: the conversation's own
   delegation line and graph card call into the island (src/main.tsx wires
   `openRow` / `openDagNode` onto the transcript source). */
async function openRun(it: AgentRow): Promise<void> {
  await act(async () => {
    store.openRow(it)
  })
  await act(async () => {
    await Promise.resolve()
  })
}

describe('subagents island, the detail', () => {
  it('opens a spawn row into header plus stage, painted from the record', async () => {
    const ctx: AgentCtx = { status: 'ok', agent: 'openclaw', messages: [] }
    const run: AgentRow = { id: 'a1', label: '🧭 survey', status: 'ok', agent: null, started_at: iso(Date.now() - 9000), ended_at: iso(Date.now() - 2000), tokens: 40 }
    /* An instance as well, because "back" now returns to the instance list. */
    rows([run], { context: async () => ctx, instances: async () => [inst({ handle: 'h1', status: 'completed' })] })
    await mount()
    await openRun(run)
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
    const run: AgentRow = { id: 'a1', label: 'survey', status: 'ok', agent: null, started_at: iso(Date.now() - 9000), ended_at: iso(Date.now() - 2000), tokens: 40 }
    rows([run], { context: async () => ctx })
    const host = document.getElementById('wsBody')!
    /* Through the mount module, because the remount is what is being tested:
       drawWs() detaches, wipes the panel body and draws a fresh root. */
    await act(async () => {
      mount_.draw(host)
    })
    await act(async () => {
      await Promise.resolve()
    })
    await openRun(run)
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
    const node: AgentRow = { kind: 'dag', run_id: 'r1', node: 'merge', agent: 'Writer', label: 'merge', status: 'ok', started_at: iso(Date.now() - 9000), ended_at: iso(Date.now() - 2000) }
    rows([node], { node: async () => ({ messages: [], output_truncated: true }) })
    await mount()
    await openRun(node)
    expect(document.querySelector('.sahd .trow b')?.textContent).toBe('merge')
    expect(document.querySelector('.sahd .trow .who')?.textContent).toBe('Writer')
    expect(paints[0]!.opts).toMatchObject({ key: 'dag:r1:merge', empty: 'gui.dag.node_empty', reset: true })
    expect(document.querySelectorAll('.satx > .wsnote')).toHaveLength(1)
  })

  it('keeps the demo detail an honest empty note when the source has no records', async () => {
    const run: AgentRow = { id: 'a1', label: 'survey', status: 'ok', started_at: iso(Date.now() - 9000), ended_at: iso(Date.now() - 2000) }
    rows([run])
    await mount()
    await openRun(run)
    expect(document.querySelector('.satx .wsempty')?.textContent).toBe('gui.ws.agents_none')
    expect(paints).toHaveLength(0)
  })

  it('shows the read failure in the stage when the record cannot be fetched', async () => {
    const run: AgentRow = { id: 'a1', label: 'survey', status: 'run', started_at: iso(Date.now() - 9000) }
    rows([run], {
      context: async () => {
        throw new Error('record gone')
      },
    })
    await mount()
    await act(async () => {
      store.openRow(run)
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(document.querySelector('.satx .wsempty')?.textContent).toBe('record gone')
  })
})

describe('subagents island, an instance detail', () => {
  /* The answer shape is the whole point of these two: `instance.history` returns
     `{ turns }` where a turn's text is `content`, and the renderer takes
     `messages` where it is `text`. Handed over unadapted, every populated
     instance drew as the empty state -- which is what a reader saw after
     clicking any row in the screenshot that started this. */
  it('paints the conversation, not an empty stage', async () => {
    const row = inst({ handle: 'research-a2a-726da8', status: 'completed', resumable: true })
    instances([row], {
      instanceHistory: async () => ({
        turns: [
          { call_id: 'c1', role: 'user' as const, content: '调研 A2A 协议', at_ms: 1000 },
          {
            call_id: 'c1', role: 'assistant' as const, content: '妥了', at_ms: 2000,
            tool_calls: [{ id: 'tc1', name: 'web_search', arguments: '{}' }],
          },
        ],
      }),
    })
    await mount()
    await act(async () => {
      ;(await screen.findByText('research-a2a-726da8')).closest('.sarow')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
    /* The run details' header chrome, which is the only one with a stylesheet. */
    expect(document.querySelector('.sahd .back')).toBeTruthy()
    expect(document.querySelector('.sahd .trow b')?.textContent).toBe('research-a2a-726da8')
    expect(document.querySelector('.sahd .trow .who')?.textContent).toBe('hermes')
    expect(paints).toHaveLength(1)
    expect(paints[0]!.opts).toMatchObject({ key: 'in:hermes:research-a2a-726da8', reset: true })
    expect(paints[0]!.ctx).toEqual({
      status: 'completed',
      messages: [
        { role: 'user', text: '调研 A2A 协议', timestamp: 1000 },
        {
          role: 'assistant', text: '妥了', timestamp: 2000,
          tool_calls: [{ id: 'tc1', name: 'web_search', arguments: '{}' }],
        },
      ],
    })
    expect(document.querySelector('.satx .wsempty')).toBeNull()
  })

  async function openInstance(row: InstanceRow, over: Partial<AgentsSource> = {}): Promise<void> {
    instances([row], { instanceHistory: async () => ({ turns: [] }), ...over })
    await mount()
    await act(async () => {
      /* By what the row shows: what the instance is for, then the node's name,
         then the handle -- the same order the row itself renders. */
      ;(await screen.findByText(row.title || row.nodeId || row.handle)).closest('.sarow')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
  }

  it('does not date a turn nobody timestamped', async () => {
    /* `at_ms` is 0 for a row nothing stamped -- a step off a transport's own
       transcript, or the question of a turn still running -- and 0 is a valid
       instant, so handing it to the renderer printed "1970-01-01 08:00" under
       the message. */
    const row = inst({ handle: 'chatty', status: 'running', resumable: true })
    instances([row], {
      instanceHistory: async () => ({
        turns: [
          { call_id: 'c1', role: 'user' as const, content: '在吗', at_ms: 0, live: true },
          { call_id: 'c1', role: 'assistant' as const, content: '在', at_ms: 1755900000000 },
        ],
      }),
    })
    await mount()
    await act(async () => {
      ;(await screen.findByText('chatty')).closest('.sarow')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
    const messages = (paints[0]!.ctx as { messages: Array<Record<string, unknown>> }).messages
    expect('timestamp' in messages[0]!).toBe(false)
    /* And one that does have a clock keeps it. */
    expect(messages[1]!.timestamp).toBe(1755900000000)
  })

  it('names its own empty note instead of the list\'s', async () => {
    /* An instance with no turns used to inherit `agents_none` -- "no background
       work yet" -- from the shared renderer, under the row just opened. */
    const row = inst({ handle: 'quiet', status: 'completed', resumable: true })
    instances([row], { instanceHistory: async () => ({ turns: [] }) })
    await mount()
    await act(async () => {
      ;(await screen.findByText('quiet')).closest('.sarow')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(paints[0]!.opts).toMatchObject({ empty: 'gui.ws.instance_empty' })
  })

  it('says the handle once when a node names its instance after itself', async () => {
    /* nodeId and handle are the same string when a graph writes
       `instance: "synthesize"` on node `synthesize`, and the header printed
       both. */
    const same = inst({ handle: 'synthesize', status: 'completed', nodeId: 'synthesize', runId: 'r9' })
    instances([same], { instanceHistory: async () => ({ turns: [] }) })
    await mount()
    await act(async () => {
      ;(await screen.findByText('synthesize')).closest('.sarow')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(document.querySelector('.sahd .trow b')?.textContent).toBe('synthesize')
    expect(document.querySelector('.sahd .trow .sp')).toBeNull()
  })

  it('still shows the handle when a titled row happens to be named after it', async () => {
    /* The pair the test above is about, plus a title. Suppressing on `nodeId`
       rather than on what is drawn hid the handle entirely here -- and this
       header is the only place it appears, so the address a direct turn is sent
       to was on screen nowhere. */
    const same = inst({
      handle: 'synthesize',
      status: 'completed',
      nodeId: 'synthesize',
      runId: 'r9',
      title: 'synthesize today findings',
    })
    instances([same], { instanceHistory: async () => ({ turns: [] }) })
    await mount()
    await act(async () => {
      ;(await screen.findByText('synthesize today findings')).closest('.sarow')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => { await Promise.resolve() })

    expect(document.querySelector('.sahd .trow b')?.textContent).toBe('synthesize today findings')
    expect(document.querySelector('.sahd .trow .sp')?.textContent).toBe('synthesize')
  })

  it('still shows the handle when the node id differs from it', async () => {
    const minted = inst({ handle: 'research-a2a-da81fa', status: 'completed', nodeId: 'research_a2a', runId: 'r9' })
    instances([minted], { instanceHistory: async () => ({ turns: [] }) })
    await mount()
    await act(async () => {
      ;(await screen.findByText('research_a2a')).closest('.sarow')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(document.querySelector('.sahd .trow b')?.textContent).toBe('research_a2a')
    expect(document.querySelector('.sahd .trow .sp')?.textContent).toBe('research-a2a-da81fa')
  })

  it('carries the conversation on, addressed to this instance', async () => {
    const said: Array<[string, string, string]> = []
    await openInstance(inst({ handle: 'chatty', status: 'completed', resumable: true }), {
      instanceSend: async (agent, handle, text) => {
        said.push([agent, handle, text])
      },
    })
    const box = document.querySelector('.sasend textarea') as HTMLTextAreaElement
    expect(box).toBeTruthy()
    box.value = '  再补一句它的传输层  '
    await act(async () => {
      ;(document.querySelector('.sasend .mini') as HTMLButtonElement).click()
    })
    await act(async () => {
      await Promise.resolve()
    })
    /* Trimmed, and addressed to the instance rather than to the conversation. */
    expect(said).toEqual([['hermes', 'chatty', '再补一句它的传输层']])
    /* Emptied because the turn was taken. */
    expect(box.value).toBe('')
  })

  it('hands a staged file to the instance as the note the page composer writes', async () => {
    /* The server forwards a direct chat's files from `media`, which the live
       layer derives from this note; the shipped composer had no way to stage a
       file, so every direct chat reached the sub-agent with none. The tray is
       this instance's own and uploads through the page composer's seam. */
    const said: string[] = []
    const uploaded: string[] = []
    instances([inst({ handle: 'chatty', status: 'completed', resumable: true })], {
      instanceHistory: async () => ({ turns: [] }),
      instanceSend: async (_a, _h, text) => {
        said.push(text)
      },
    })
    ;(window.DS as { composer?: unknown }).composer = {
      upload: async (req: { name: string; content_b64: string }) => {
        uploaded.push(`${req.name}:${req.content_b64}`)
        return { path: `uploads/${req.name}`, size: 3 }
      },
    }
    await mount()
    await act(async () => {
      ;(await screen.findByText('chatty')).closest('.sarow')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
    const picker = document.querySelector('.sasend input[type=file]') as HTMLInputElement
    expect(picker).toBeTruthy()
    const file = new File(['abc'], 'a.txt', { type: 'text/plain' })
    Object.defineProperty(picker, 'files', { value: [file], configurable: true })
    await act(async () => {
      picker.dispatchEvent(new Event('change', { bubbles: true }))
    })
    /* The FileReader lands on its own tick; the chip is drawn uploading, then not. */
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
    expect(uploaded).toEqual(['a.txt:YWJj'])
    expect(document.querySelector('.sasend .att .nm')?.textContent).toBe('a.txt')
    expect(document.querySelector('.sasend .att.up')).toBeNull()

    const box = document.querySelector('.sasend textarea') as HTMLTextAreaElement
    box.value = 'have a look'
    await act(async () => {
      ;(document.querySelector('.sasend .mini') as HTMLButtonElement).click()
    })
    await act(async () => {
      await Promise.resolve()
    })
    /* The fake shell's T returns the key: the note is `gui.att.note` itself. */
    expect(said).toEqual(['have a look\n\ngui.att.note\n- uploads/a.txt'])
    expect(box.value).toBe('')
    expect(document.querySelector('.sasend .att')).toBeNull()
  })

  it('sends a file with nothing typed as the note alone, delimiter kept', async () => {
    /* The send button is live with a staged file and an empty box. The note then
       leads the message after a blank line, which is how the live layer finds it;
       the store's trim used to take that blank line, and the files with it. */
    const said: string[] = []
    instances([inst({ handle: 'chatty', status: 'completed', resumable: true })], {
      instanceHistory: async () => ({ turns: [] }),
      instanceSend: async (_a, _h, text) => {
        said.push(text)
      },
    })
    ;(window.DS as { composer?: unknown }).composer = {
      upload: async (req: { name: string }) => ({ path: `uploads/${req.name}`, size: 3 }),
    }
    await mount()
    await act(async () => {
      ;(await screen.findByText('chatty')).closest('.sarow')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
    const picker = document.querySelector('.sasend input[type=file]') as HTMLInputElement
    Object.defineProperty(picker, 'files', { value: [new File(['abc'], 'a.txt')], configurable: true })
    await act(async () => {
      picker.dispatchEvent(new Event('change', { bubbles: true }))
    })
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20))
    })
    const go = document.querySelector('.sasend .go') as HTMLButtonElement
    expect(go.disabled).toBe(false)
    await act(async () => {
      go.click()
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(said).toEqual(['\n\ngui.att.note\n- uploads/a.txt'])
    expect(document.querySelector('.sasend .att')).toBeNull()
  })

  it('offers no tray where nothing can take the bytes', async () => {
    await openInstance(inst({ handle: 'chatty', status: 'completed', resumable: true }), {
      instanceSend: async () => {},
    })
    expect(document.querySelector('.sasend input[type=file]')).toBeNull()
    expect(document.querySelector('.sasend .instance-attach')).toBeNull()
  })

  it('sends on enter and breaks the line on shift-enter', async () => {
    const said: string[] = []
    await openInstance(inst({ handle: 'chatty', status: 'completed', resumable: true }), {
      instanceSend: async (_a, _h, text) => {
        said.push(text)
      },
    })
    const box = document.querySelector('.sasend textarea') as HTMLTextAreaElement
    box.value = '第一句'
    await act(async () => {
      box.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', shiftKey: true, bubbles: true }))
    })
    expect(said).toEqual([])
    await act(async () => {
      box.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    })
    expect(said).toEqual(['第一句'])
  })

  it('does not send the keystroke that confirms an IME candidate', async () => {
    /* Typing Chinese, the Enter that picks the candidate arrives while the
       composition is still open. Sent then, it went out half-typed; and since the
       conversion changed the text afterwards the box was not emptied, so the
       reader pressed Enter again -- one line typed, one sent and one queued
       behind it. */
    const said: string[] = []
    await openInstance(inst({ handle: 'chatty', status: 'completed', resumable: true }), {
      instanceSend: async (_a, _h, text) => {
        said.push(text)
      },
    })
    const box = document.querySelector('.sasend textarea') as HTMLTextAreaElement
    box.value = '帮我做个ppt'
    await act(async () => {
      box.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true }))
    })
    expect(said).toEqual([])
    /* The older spelling some IMEs still send instead of `isComposing`. */
    await act(async () => {
      box.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', keyCode: 229, bubbles: true }))
    })
    expect(said).toEqual([])
    /* The keystroke that follows, once the composition has closed, sends. */
    await act(async () => {
      box.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    })
    expect(said).toEqual(['帮我做个ppt'])
  })

  it('offers no composer for an instance that cannot continue', async () => {
    /* A stateless agent starts from nothing every turn, so what looked like a
       conversation would be a run of unrelated first turns. */
    await openInstance(inst({ handle: 'one-shot', status: 'completed', resumable: false }), {
      instanceSend: async () => {},
    })
    expect(document.querySelector('.satx')).toBeTruthy()
    expect(document.querySelector('.sasend')).toBeNull()
  })

  it('names who the composer is addressing', async () => {
    await openInstance(
      inst({ handle: 'research-a2a-800453', status: 'completed', resumable: true, runId: 'r1', nodeId: 'research_a2a' }),
      { instanceSend: async () => {} },
    )
    const box = document.querySelector('.sasend textarea') as HTMLTextAreaElement
    expect(box.getAttribute('placeholder')).toBe('gui.ws.instance_say_hint {"name":"research_a2a"}')
  })

  it('addresses the composer by what the instance is for, not by its id', async () => {
    /* A pane headed by the task whose composer says "carry on with
       raven-f5caf2" reads as two different things on one screen. */
    await openInstance(
      inst({ handle: 'raven-f5caf2', status: 'completed', resumable: true, title: '用一句话解释光合作用' }),
      { instanceSend: async () => {} },
    )
    const box = document.querySelector('.sasend textarea') as HTMLTextAreaElement
    expect(box.getAttribute('placeholder')).toBe('gui.ws.instance_say_hint {"name":"用一句话解释光合作用"}')
  })

  it('names the desk pane composer the same way the pane is headed', async () => {
    /* The desk pane is the surface that showed the handle: its composer asked
       for `nodeId || handle` and never looked at the title at all, so a pane
       headed "用一句话解释光合作用" invited the reader to carry on with
       `raven-f5caf2`. */
    const row = inst({ handle: 'raven-f5caf2', status: 'completed', resumable: true, title: '用一句话解释光合作用' })
    instances([row], { instanceHistory: async () => ({ turns: [] }), instanceSend: async () => {} })
    render(<InstanceConversation row={row} />, { container: document.getElementById('wsBody')! })
    await act(async () => { await Promise.resolve() })

    const box = document.querySelector('.sasend textarea') as HTMLTextAreaElement
    expect(box.getAttribute('placeholder')).toBe('gui.ws.instance_say_hint {"name":"用一句话解释光合作用"}')
  })

  /* A direct turn's events arrive on the session's one subscription, tagged with
     their target; the conversation drops them and hands them here. Only the fact
     is used -- the transcript is re-read, never re-rendered from the delta. */
  it('pulls the instance transcript forward when its own turn streams', async () => {
    const row = inst({ handle: 'chatty', status: 'running', resumable: true })
    let turns: DirectTurn[] = [{ call_id: 'c1', role: 'user', content: '在吗', at_ms: 1 }]
    instances([row], {
      instanceHistory: async () => ({ turns }),
      instanceSend: async () => {},
    })
    await mount()
    await act(async () => {
      ;(await screen.findByText('chatty')).closest('.sarow')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
    const drawn = paints.length
    turns = [...turns, { call_id: 'c1', role: 'assistant', content: '在', at_ms: 2 }]
    await act(async () => {
      store.directEvent({ agent: 'hermes', handle: 'chatty' }, 'message.complete')
      await Promise.resolve()
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(paints.length).toBeGreaterThan(drawn)
    expect((paints[paints.length - 1]!.ctx as { messages: unknown[] }).messages).toHaveLength(2)
  })

  it('leaves another instance alone when it is the one streaming', async () => {
    const row = inst({ handle: 'chatty', status: 'completed', resumable: true })
    instances([row], { instanceHistory: async () => ({ turns: [] }), instanceSend: async () => {} })
    await mount()
    await act(async () => {
      ;(await screen.findByText('chatty')).closest('.sarow')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
    const drawn = paints.length
    await act(async () => {
      store.directEvent({ agent: 'hermes', handle: 'someone-else' }, 'message.complete')
      await Promise.resolve()
    })
    expect(paints.length).toBe(drawn)
  })

  /* Two instances answering at once, which is the case a single page-wide poke
     timer got wrong in both directions. `asked` is the transcript read, which is
     the only thing that reaches a stage. */
  async function openAlphaOfTwo(asked: string[]): Promise<void> {
    instances(
      [
        inst({ handle: 'alpha', status: 'running', resumable: true }),
        inst({ handle: 'beta', status: 'running', resumable: true }),
      ],
      {
        instanceHistory: async (_agent, handle) => {
          asked.push(handle)
          return { turns: [] }
        },
      },
    )
    await mount()
    await act(async () => {
      await Promise.resolve()
    })
    await act(async () => {
      screen.getByText('alpha').closest('.sarow')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
    asked.length = 0
    paints.length = 0
  }

  it('repaints whichever instance is open when the coalesced window closes', async () => {
    vi.useFakeTimers()
    const asked: string[] = []
    await openAlphaOfTwo(asked)
    /* A token of alpha's answer arms the half-second window... */
    store.directEvent({ agent: 'hermes', handle: 'alpha' }, 'token.delta')
    /* ...and the reader switches instances before it closes. Half a second is
       long enough to do that in, and the timer used to hold the item that was
       open when the event arrived -- so it painted the instance just left into
       the stage of the one just opened. */
    await act(async () => {
      store.openInstance(inst({ handle: 'beta', status: 'running', resumable: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
    asked.length = 0
    paints.length = 0
    await act(async () => {
      vi.advanceTimersByTime(600)
      await Promise.resolve()
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(asked).toEqual([])
    expect(paints).toEqual([])
    /* Beta's own turn still reaches beta's stage. */
    await act(async () => {
      store.directEvent({ agent: 'hermes', handle: 'beta' }, 'message.complete')
      await Promise.resolve()
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(asked).toEqual(['beta'])
  })

  it('does not let one instance stream swallow another instance repaint', async () => {
    vi.useFakeTimers()
    const asked: string[] = []
    await openAlphaOfTwo(asked)
    /* Beta, which nobody is looking at, streams first and arms the window; with
       one timer for the page, alpha's own token then found a poke pending and
       returned -- the open transcript sat still while it was being written. */
    store.directEvent({ agent: 'hermes', handle: 'beta' }, 'token.delta')
    store.directEvent({ agent: 'hermes', handle: 'alpha' }, 'token.delta')
    await act(async () => {
      vi.advanceTimersByTime(600)
      await Promise.resolve()
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(asked).toEqual(['alpha'])
  })

  it('says why a turn was refused instead of dropping it', async () => {
    await openInstance(inst({ handle: 'busy', status: 'running', resumable: true }), {
      instanceSend: async () => {
        throw new Error('that instance is still answering')
      },
    })
    const box = document.querySelector('.sasend textarea') as HTMLTextAreaElement
    box.value = '在吗'
    await act(async () => {
      ;(document.querySelector('.sasend .mini') as HTMLButtonElement).click()
      await Promise.resolve()
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(document.querySelector('.sasend .why')?.textContent).toBe('that instance is still answering')
    /* And the words are still there to try again with. A refusal is the one
       outcome where the reader wants their draft back, and clearing on submit
       threw it away precisely then. */
    expect(box.value).toBe('在吗')
  })

  /* Alpha open, of two resumable rows, with `instanceSend` under the test's
     control. The refusal wording is true of one instance and a lie about any
     other, so where it is shown is the whole point. */
  async function alphaRefusing(send: AgentsSource['instanceSend']): Promise<void> {
    instances(
      [
        inst({ handle: 'alpha', status: 'running', resumable: true }),
        inst({ handle: 'beta', status: 'completed', resumable: true }),
      ],
      { instanceHistory: async () => ({ turns: [] }), instanceSend: send },
    )
    await mount()
    await act(async () => {
      await Promise.resolve()
    })
    await act(async () => {
      screen.getByText('alpha').closest('.sarow')!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
    const box = document.querySelector('.sasend textarea') as HTMLTextAreaElement
    box.value = '在吗'
    await act(async () => {
      ;(document.querySelector('.sasend .mini') as HTMLButtonElement).click()
      await Promise.resolve()
    })
  }

  const open = async (handle: string, status: string): Promise<void> => {
    await act(async () => {
      store.openInstance(inst({ handle, status, resumable: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
  }

  const why = (): string | null | undefined => document.querySelector('.sasend .why')?.textContent

  it('keeps a refusal on the instance that refused it', async () => {
    await alphaRefusing(async () => {
      throw new Error('that instance is still answering')
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(why()).toBe('that instance is still answering')
    await open('beta', 'completed')
    /* Beta's own composer, which has refused nothing. */
    expect(document.querySelector('.sasend textarea')).toBeTruthy()
    expect(document.querySelector('.sasend .why')).toBeNull()
    /* And alpha's is still there to read on the way back. */
    await open('alpha', 'running')
    expect(why()).toBe('that instance is still answering')
  })

  it('does not show a refusal that arrived after the reader moved on', async () => {
    /* The case clearing on open would not have covered: the send is still in
       flight when the reader switches, so the refusal is recorded while another
       instance is already on screen. */
    let refuse: ((e: Error) => void) | null = null
    await alphaRefusing(
      () =>
        new Promise<void>((_res, rej) => {
          refuse = rej
        }),
    )
    await open('beta', 'completed')
    await act(async () => {
      refuse?.(new Error('that instance is still answering'))
      await Promise.resolve()
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(document.querySelector('.sasend .why')).toBeNull()
    await open('alpha', 'running')
    expect(why()).toBe('that instance is still answering')
  })

  it('leaves a draft typed while the turn was in flight alone', async () => {
    let take: (() => void) | null = null
    await openInstance(inst({ handle: 'slow', status: 'completed', resumable: true }), {
      instanceSend: async () => {
        await new Promise<void>((res) => {
          take = res
        })
      },
    })
    const box = document.querySelector('.sasend textarea') as HTMLTextAreaElement
    box.value = '第一句'
    await act(async () => {
      ;(document.querySelector('.sasend .mini') as HTMLButtonElement).click()
      await Promise.resolve()
    })
    /* Carried on typing before the server answered: what lands is not what was
       submitted, so the send has no business emptying the box. */
    box.value = '第一句,还有第二句'
    await act(async () => {
      take?.()
      await Promise.resolve()
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(box.value).toBe('第一句,还有第二句')
  })

  /* One piece of work, two ways in. The conversation's graph card opens a node
     through `openDagNode`; this panel opens its own row. They used to land on
     different screens -- and only one of them had the composer, which is how a
     reader found the direct chat missing depending on where they clicked. */
  it('opens a graph node as the instance it ran on', async () => {
    const row = inst({ handle: 'research-a2a-800453', status: 'completed', resumable: true, runId: 'r1', nodeId: 'research_a2a' })
    instances([row], { instanceHistory: async () => ({ turns: [] }), instanceSend: async () => {} })
    await mount()
    await screen.findByText('research_a2a')
    await act(async () => {
      store.openDagNode('r1', { id: 'research_a2a', subagent: 'hermes' })
    })
    await act(async () => {
      await Promise.resolve()
    })
    /* The instance detail, composer included -- not the node record view. */
    expect(document.querySelector('.sahd .trow b')?.textContent).toBe('research_a2a')
    expect(document.querySelector('.sahd .trow .sp')?.textContent).toBe('research-a2a-800453')
    expect(document.querySelector('.sasend textarea')).toBeTruthy()
  })

  it('keeps a stateless node on its own record, having no instance to open', async () => {
    instances([], { node: async () => ({ messages: [] }) })
    await mount()
    await act(async () => {
      store.openDagNode('r1', { id: 'shape', subagent: 'oneshot' })
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(document.querySelector('.sahd .trow b')?.textContent).toBe('shape')
    expect(document.querySelector('.sasend')).toBeNull()
  })

  /* The production shape of a stateless node: the run gives it a status record
     keyed `<run>/<node>`, which the list keeps because there is no addressable
     row to fall back on. Its two records live at `(r1, shape)` and under the
     handle `shape` -- so asking `instance.history` for `r1/shape`, which every
     row did, finds neither and draws the node as if it had done nothing. */
  const statelessRow = (): InstanceRow =>
    inst({ handle: 'r1/shape', kind: 'dag-node', agent: 'oneshot', status: 'completed', runId: 'r1', nodeId: 'shape' })

  it('opens a stateless node row on the node record, not an empty instance', async () => {
    const asked: string[] = []
    instances([statelessRow()], {
      instanceHistory: async (_agent, handle) => {
        asked.push(handle)
        return { turns: [] }
      },
      node: async () => ({ messages: [] }),
    })
    await mount()
    await act(async () => {
      ;(await screen.findByText('shape')).closest('.sarow')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(asked).toEqual([])
    expect(paints[0]!.opts).toMatchObject({ key: 'dag:r1:shape', empty: 'gui.dag.node_empty' })
    expect(document.querySelector('.sahd .trow b')?.textContent).toBe('shape')
    expect(document.querySelector('.sahd .trow .who')?.textContent).toBe('oneshot')
    /* And the row is still in the list to come back to. */
    await act(async () => {
      ;(document.querySelector('.sahd .back') as HTMLButtonElement).click()
    })
    expect(document.querySelectorAll('.salist .sarow')).toHaveLength(1)
  })

  it('keeps the graph node on its record even with that row already loaded', async () => {
    /* The same node reached the other way. The row carries the node's two ids,
       so a promotion that looked only at those sent the card's own entry point
       to the empty instance view as soon as this panel had been opened once. */
    const asked: string[] = []
    instances([statelessRow()], {
      instanceHistory: async (_agent, handle) => {
        asked.push(handle)
        return { turns: [] }
      },
      node: async () => ({ messages: [] }),
    })
    await mount()
    await screen.findByText('shape')
    await act(async () => {
      store.openDagNode('r1', { id: 'shape', subagent: 'oneshot' })
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(asked).toEqual([])
    expect(paints[0]!.opts).toMatchObject({ key: 'dag:r1:shape' })
    expect(document.querySelector('.sasend')).toBeNull()
  })

  it('promotes a node opened before its row was known', async () => {
    /* Clicked from the conversation with this panel never yet opened: the node
       view is what there is, and the arriving row has to replace it rather than
       leave the same work under two screens depending on timing. */
    const row = inst({ handle: 'h-1', status: 'completed', resumable: true, runId: 'r1', nodeId: 'shape' })
    let rows: InstanceRow[] = []
    instances([], {
      instances: async () => rows,
      instanceHistory: async () => ({ turns: [] }),
      instanceSend: async () => {},
      node: async () => ({ messages: [] }),
    })
    await mount()
    await act(async () => {
      store.openDagNode('r1', { id: 'shape', subagent: 'hermes' })
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(document.querySelector('.sasend')).toBeNull()
    rows = [row]
    await act(async () => {
      store.refreshInstances(true)
      await Promise.resolve()
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(document.querySelector('.sahd .trow b')?.textContent).toBe('shape')
    expect(document.querySelector('.sasend textarea')).toBeTruthy()
  })

  it('titles a graph node\'s record by what the node did, not by its id', async () => {
    /* Reported against the running page: a node panel headed `create_august_ppt`.
       That is the node's ID -- a slug from the plan -- and the run knows a
       sentence for it, `node_summary`, which is what every other surface shows.

       Two things put the id there. `openDagNode` only ever received `{ id }`, so
       the summary never reached the record row at all; and the pane's own header
       reads `row.node` before `row.label`, so the id would have won even once
       the summary arrived. */
    const openAgentRecord = vi.fn()
    window.RavenIslands = { workspace: { openAgentRecord } }
    instances([], { instances: async () => [], node: async () => ({ messages: [] }) })
    await act(async () => {
      store.openDagNode('r1', {
        id: 'create_august_ppt',
        subagent: 'Raven-PPT',
        summary: 'Build the August deck from the research',
      })
    })
    expect(openAgentRecord).toHaveBeenCalledTimes(1)
    expect(openAgentRecord.mock.calls[0]?.[0]).toMatchObject({
      kind: 'dag',
      run_id: 'r1',
      node: 'create_august_ppt',
      label: 'Build the August deck from the research',
    })

    /* And the id when the run has no sentence for the node -- an older run, or a
       plan that named its steps and described none. A pane with no heading at
       all is worse than one headed by a slug. */
    openAgentRecord.mockClear()
    await act(async () => {
      store.openDagNode('r1', { id: 'create_august_ppt', subagent: 'Raven-PPT' })
    })
    expect(openAgentRecord.mock.calls[0]?.[0]).toMatchObject({ label: 'create_august_ppt' })
  })

  it('promotes that node once, not on every heartbeat after it', async () => {
    const row = inst({ handle: 'h-1', status: 'completed', resumable: true, runId: 'r1', nodeId: 'shape' })
    const openAgent = vi.fn()
    const openAgentRecord = vi.fn()
    window.RavenIslands = { workspace: { openAgent, openAgentRecord } }
    instances([], {
      instances: async () => [row],
      instanceHistory: async () => ({ turns: [] }),
      node: async () => ({ messages: [] }),
    })
    await act(async () => {
      store.openDagNode('r1', { id: 'shape', subagent: 'hermes' })
    })
    for (let beat = 0; beat < 3; beat += 1) {
      await act(async () => {
        store.refreshInstances(true)
        await Promise.resolve()
      })
      await act(async () => {
        await Promise.resolve()
      })
    }
    /* Once. Reopening the pane every couple of seconds is what dropped the
       reader out of fullscreen and stole the pane they were reading. */
    expect(openAgent).toHaveBeenCalledTimes(1)
    expect(openAgentRecord).toHaveBeenCalledTimes(1)
  })

  it('repaints a running instance on the heartbeat, without a remount', async () => {
    let poll: (() => void) | null = null
    let snapshot = 1
    const row = inst({ handle: 'live-1', status: 'running', resumable: true })
    instances([row], {
      watch: (fn) => {
        poll = fn
      },
      instanceHistory: async () =>
        snapshot === 1
          ? { turns: [{ call_id: 'c1', role: 'user' as const, content: 'go', at_ms: 1, live: true }] }
          : {
            turns: [
              { call_id: 'c1', role: 'user' as const, content: 'go', at_ms: 1 },
              { call_id: 'c1', role: 'assistant' as const, content: 'done', at_ms: 2 },
            ],
          },
    })
    await mount()
    await act(async () => {
      store.hook()
      ;(await screen.findByText('live-1')).closest('.sarow')!
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(paints).toHaveLength(1)
    /* A live turn is handed over as the renderer's `run`, which is what makes it
       hold the trailing row back instead of appending a second copy of it. */
    expect((paints[0]!.ctx as { status?: string }).status).toBe('run')
    const stage = document.querySelector('.satx')
    snapshot = 2
    /* Asserted before it is used: `poll?.()` no-ops when the source was never
       asked to watch, and the failure then surfaces three assertions later as a
       missing repaint. This names it where it happens. */
    expect(poll).toBeTypeOf('function')
    await act(async () => {
      poll!()
      await Promise.resolve()
    })
    await act(async () => {
      await Promise.resolve()
    })
    expect(paints.length).toBeGreaterThan(1)
    const last = paints[paints.length - 1]!
    expect(last.opts).toMatchObject({ key: 'in:hermes:live-1', reset: false })
    expect((last.ctx as { messages: unknown[] }).messages).toHaveLength(2)
    /* The same box throughout: a remount would have replaced it, and with it
       everything the transcript had already drawn. */
    expect(document.querySelector('.satx')).toBe(stage)
  })

  it('repaints a floating running instance while the agents view is hidden', async () => {
    let poll: (() => void) | null = null
    let snapshot = 1
    const row = inst({ handle: 'floating-live', status: 'running', resumable: true })
    instances([row], {
      watch: (fn) => { poll = fn },
      instanceHistory: async () => ({
        turns: snapshot === 1
          ? [{ call_id: 'c1', role: 'assistant' as const, content: 'first', at_ms: 1, live: true }]
          : [
            { call_id: 'c1', role: 'assistant' as const, content: 'first', at_ms: 1 },
            { call_id: 'c1', role: 'assistant' as const, content: 'second', at_ms: 2, live: true },
          ],
      }),
    })
    window.RavenShell!.wsShows = () => false

    render(<InstanceConversation row={row} />, {
      container: document.getElementById('wsBody')!,
    })
    await act(async () => { await Promise.resolve() })
    expect(paints).toHaveLength(1)

    snapshot = 2
    /* Asserted before it is used: `poll?.()` no-ops when the source was never
       asked to watch, and the failure then surfaces three assertions later as a
       missing repaint. This names it where it happens. */
    expect(poll).toBeTypeOf('function')
    await act(async () => {
      poll!()
      await Promise.resolve()
    })
    await act(async () => { await Promise.resolve() })

    expect(paints).toHaveLength(2)
    expect((paints[1]!.ctx as { messages: unknown[] }).messages).toHaveLength(2)
  })

  it('renders each workspace record from its own row', async () => {
    const asked: string[] = []
    rows([], {
      context: async (id) => {
        asked.push(id)
        return { messages: [{ role: 'assistant', content: id }] }
      },
    })
    const first: AgentRow = { kind: 'spawn', id: 'first', label: 'First' }
    const second: AgentRow = { kind: 'spawn', id: 'second', label: 'Second' }
    store.openRow(first)
    store.openRow(second)

    render(
      <>
        <AgentRecordConversation row={first} />
        <AgentRecordConversation row={second} />
      </>,
      { container: document.getElementById('wsBody')! },
    )
    await act(async () => { await Promise.resolve() })

    expect(asked).toEqual(['first', 'second'])
    expect(paints.map((paint) => paint.opts?.key)).toEqual(['sp:first', 'sp:second'])
  })

  it('repaints a running workspace record on the heartbeat', async () => {
    let poll: (() => void) | null = null
    let snapshot = 1
    const row: AgentRow = { kind: 'spawn', id: 'live', label: 'Live', status: 'run' }
    rows([row], {
      watch: (fn) => { poll = fn },
      context: async () => ({
        messages: snapshot === 1
          ? [{ role: 'assistant', content: 'first' }]
          : [{ role: 'assistant', content: 'first' }, { role: 'assistant', content: 'second' }],
      }),
    })
    window.RavenShell!.wsShows = () => false

    render(<AgentRecordConversation row={row} />, {
      container: document.getElementById('wsBody')!,
    })
    await act(async () => { await Promise.resolve() })
    expect(paints).toHaveLength(1)

    snapshot = 2
    /* Asserted before it is used: `poll?.()` no-ops when the source was never
       asked to watch, and the failure then surfaces three assertions later as a
       missing repaint. This names it where it happens. */
    expect(poll).toBeTypeOf('function')
    await act(async () => {
      poll!()
      await Promise.resolve()
    })
    await act(async () => { await Promise.resolve() })

    expect(paints).toHaveLength(2)
    expect((paints[1]!.ctx as { messages: unknown[] }).messages).toHaveLength(2)
  })
})

/* The desk's list is what a reader watches while work runs, so what a row says
   is the whole point of it. Both lines come from the server on the row itself
   (`title`, `runTitle`) rather than being joined here out of `subagent.list`:
   four surfaces draw this list, and a join each wrote separately is four
   chances to join differently. */
describe('subagents island, what a desk row is called', () => {
  const draw = (row: InstanceRow): HTMLElement => {
    instances([row], { instanceHistory: async () => ({ turns: [] }) })
    render(<InstanceRowView it={row} compact />, { container: document.getElementById('wsBody')! })
    return document.querySelector('.sarow.inst') as HTMLElement
  }

  it('says what the instance was asked, not what its handle is', () => {
    const el = draw(inst({
      handle: 'daily-ai-digest-36e275-scan-news-8edc65',
      nodeId: 'daily-ai-digest-36e275-scan-news',
      runId: '20260825T051102805861Z-871b6ab4',
      title: 'scan today’s ai news',
      runTitle: 'compile the daily ai digest',
    }))

    expect(el.querySelector('.nm')!.textContent).toBe('scan today’s ai news')
    expect(el.querySelector('.source')!.textContent).toBe('compile the daily ai digest')
  })

  it('names the graph it came out of, never the run id', () => {
    /* The id is a timestamp. It filled this slot before there was a line to put
       there, and a reader could do nothing with it. */
    const el = draw(inst({
      handle: 'h', runId: '20260825T051102805861Z-871b6ab4', title: 'a step', runTitle: 'a graph',
    }))

    expect(el.querySelector('.source')!.textContent).not.toContain('20260825T')
  })

  it('draws no source at all for work that came from no graph', () => {
    /* Absent rather than blank: a spawn has nothing to name there, and an empty
       element still takes its share of the row's width. */
    const el = draw(inst({ handle: 'release-notes-3b81ca', title: 'check the release notes' }))

    expect(el.querySelector('.nm')!.textContent).toBe('check the release notes')
    expect(el.querySelector('.source')).toBeNull()
  })

  it('falls back to the handle when nothing dispatched it', () => {
    /* A hand-made instance, and every row written before the summaries existed.
       The handle is a slug, but it is the only name such a row has. */
    const el = draw(inst({ handle: 'made-by-hand' }))

    expect(el.querySelector('.nm')!.textContent).toBe('made-by-hand')
    expect(el.querySelector('.source')).toBeNull()
  })
})

/* One piece of work reached two ways has to land in one place. A spawned run
   that committed under a handle is an instance -- the same instance its own row
   in this list opens -- and the record view has no composer, so opening it from
   the transcript's card gave the reader nothing to say back with. */
describe('subagents island, what a spawned run opens as', () => {
  const run = (over: Partial<AgentRow> = {}): AgentRow =>
    ({ kind: 'spawn', id: 'call-1', agent: 'hermes', label: 'quick survey', ...over })

  it('opens the instance it committed under, not its record', async () => {
    const row = inst({ handle: 'survey-9ab2c6', resumable: true })
    instances([row], { instanceHistory: async () => ({ turns: [] }) })
    await act(async () => { store.refreshInstances(true); await Promise.resolve() })

    store.openRow(run({ instance: 'survey-9ab2c6' }))

    expect(store.getState().open).toEqual({ kind: 'instance', agent: 'hermes', handle: 'survey-9ab2c6' })
  })

  it('opens the record when the run committed under no handle', async () => {
    /* A stateless agent's spawn has no conversation to continue, so its record
       is not a fallback -- it is the whole of what there is. */
    instances([], { instanceHistory: async () => ({ turns: [] }) })
    await act(async () => { store.refreshInstances(true); await Promise.resolve() })

    store.openRow(run())

    expect(store.getState().open).toEqual({ kind: 'spawn', id: 'call-1' })
  })

  it('opens the record when the handle names no row, and swaps when one arrives', async () => {
    /* The ordinary case from the transcript's card: that path holds the run
       list and has never asked for instances. */
    let rows: InstanceRow[] = []
    instances([], { instances: async () => rows, instanceHistory: async () => ({ turns: [] }) })

    store.openRow(run({ instance: 'survey-9ab2c6' }))
    await act(async () => { await Promise.resolve() })
    expect(store.getState().open).toEqual({ kind: 'spawn', id: 'call-1' })

    rows = [inst({ handle: 'survey-9ab2c6', resumable: true })]
    await act(async () => { store.refreshInstances(true); await Promise.resolve() })
    await act(async () => { await Promise.resolve() })

    expect(store.getState().open).toEqual({ kind: 'instance', agent: 'hermes', handle: 'survey-9ab2c6' })
  })

  it('does not take a handle of the same name under another agent', async () => {
    /* A handle is unique per agent, not globally. */
    instances([inst({ agent: 'openclaw', handle: 'survey-9ab2c6' })],
      { instanceHistory: async () => ({ turns: [] }) })
    await act(async () => { store.refreshInstances(true); await Promise.resolve() })

    store.openRow(run({ instance: 'survey-9ab2c6' }))

    expect(store.getState().open).toEqual({ kind: 'spawn', id: 'call-1' })
  })

  it("does not take another agent's handle when the list arrives later either", async () => {
    /* The same rule on the async path, which is the ordinary one: the card
       opens a record first and the list answers after. It used to be checked
       by a second copy of the predicate that no test reached. */
    let rows: InstanceRow[] = []
    instances([], { instances: async () => rows, instanceHistory: async () => ({ turns: [] }) })

    store.openRow(run({ instance: 'survey-9ab2c6' }))
    await act(async () => { await Promise.resolve() })

    rows = [inst({ agent: 'openclaw', handle: 'survey-9ab2c6', resumable: true })]
    await act(async () => { store.refreshInstances(true); await Promise.resolve() })
    await act(async () => { await Promise.resolve() })

    expect(store.getState().open).toEqual({ kind: 'spawn', id: 'call-1' })
  })

  it('a handle-less spawn stays on its record even after an instance list arrives', async () => {
    /* Every id in this file is a reused literal, so a handle remembered by an
       earlier test outlives its conversation and promotes a run that never
       named one. In production the ids are stamped and unique, which is why
       nobody sees it -- but the trap is inherited by the next test added here. */
    instances([inst({ handle: 'survey-9ab2c6', resumable: true })],
      { instanceHistory: async () => ({ turns: [] }) })

    store.openRow(run())
    expect(store.getState().open).toEqual({ kind: 'spawn', id: 'call-1' })

    await act(async () => { store.refreshInstances(true); await Promise.resolve() })
    await act(async () => { await Promise.resolve() })

    expect(store.getState().open).toEqual({ kind: 'spawn', id: 'call-1' })
  })
})

/* The read in flight is shared with whoever asks for the same list while it is
   still going (shell/resume.ts waits on it to put a window back). Shared state
   has to be dropped when the conversation changes, or the next one waits on an
   answer that was thrown away. */
describe('the list reads', () => {
  it('hand a second caller the answer the first is waiting for', async () => {
    /* Waiting on the SECOND call alone is the whole point: a caller told
       "nothing in flight" carries on before the list is there, and the window it
       was going to reopen is not found. */
    let asks = 0
    instances([], {
      instances: async () => {
        asks += 1
        await Promise.resolve()
        return [inst({ handle: 'h7', resumable: true })]
      },
    })

    store.refreshInstances(true)
    const second = store.refreshInstances(true)
    await second

    expect(asks).toBe(1)
    expect(store.getState().instances.map((row) => row.handle)).toEqual(['h7'])
  })

  it('hand a second caller the run list the first is waiting for', async () => {
    /* The same rule on the other list: a spawn's record is reopened from it. */
    let asks = 0
    rows([], {
      list: async () => {
        asks += 1
        await Promise.resolve()
        return [{ kind: 'spawn', id: 'call-1', label: 'research' }]
      },
    })

    store.refresh(true)
    const second = store.refresh(true)
    await second

    expect(asks).toBe(1)
    expect(store.getState().rows.map((row) => row.id)).toEqual(['call-1'])
  })

  it('are shared only while they are in flight', async () => {
    /* A finished read is not a read in flight. Holding its handle would make
       every later refresh hand back the same settled promise, and the list
       would never be asked for again -- the panel would go stale for the life of
       the conversation. */
    const asked: string[] = []
    instances([], {
      instances: async () => {
        asked.push('instances')
        await Promise.resolve()
        return []
      },
      list: async () => {
        asked.push('list')
        await Promise.resolve()
        return []
      },
    })

    await store.refreshInstances(true)
    await store.refreshInstances(true)
    await store.refresh(true)
    await store.refresh(true)

    expect(asked).toEqual(['instances', 'instances', 'list', 'list'])
  })

  it('drop the instance read when the conversation changes under it', async () => {
    /* The answer to the old conversation's read is discarded by the guard
       inside it, so a next conversation that waited on that same read would
       wait for an answer nobody keeps -- and its own list would stay empty
       until something asked again. */
    const asked: string[] = []
    instances([], {
      instances: async (sessionId: string) => {
        asked.push(sessionId)
        await Promise.resolve()
        return [inst({ sessionKey: sessionId, handle: `for-${sessionId}`, resumable: true })]
      },
    })

    store.refreshInstances(true)
    act(() => { store.reset() })
    setCurrent('s2')
    await store.refreshInstances(true)

    expect(asked).toEqual(['s1', 's2'])
    expect(store.getState().instances.map((row) => row.handle)).toEqual(['for-s2'])
  })

  it('drop the run read when the conversation changes under it', async () => {
    const asked: string[] = []
    rows([], {
      list: async (sessionId: string) => {
        asked.push(sessionId)
        await Promise.resolve()
        return [{ kind: 'spawn', id: `call-${sessionId}`, label: sessionId }]
      },
    })

    store.refresh(true)
    act(() => { store.reset() })
    setCurrent('s2')
    await store.refresh(true)

    expect(asked).toEqual(['s1', 's2'])
    expect(store.getState().rows.map((row) => row.id)).toEqual(['call-s2'])
  })
})

describe('the compact roster', () => {
  /* One group with a conversation under it and one without, which is the shape
     the desk's agents tab draws: a handful of registered agents, most of which
     have run nothing this session. */
  const roster = (): { roster: SubagentRow[]; instances: InstanceRow[] } => ({
    roster: [{ name: 'Raven-Research' }, { name: 'Raven-Code' }] as SubagentRow[],
    instances: [inst({ agent: 'Raven-Research', handle: 'r-1' })],
  })

  const draw = (): void => {
    const { roster: names, instances: live } = roster()
    wire({ list: async () => [] })
    render(
      <AgentList s={{ ...store.getState(), roster: names, instances: live }} compact />,
      { container: document.getElementById('wsBody')! },
    )
  }

  const heads = (): HTMLElement[] => [...document.querySelectorAll<HTMLElement>('.agent-head')]

  /* The names have to start at one x. `hidden` removed the fold from the flow,
     so a group with children indented its own icon and name past every leaf
     row's -- visible in the product as a list on two vertical lines, and
     invisible to any assertion about text or classes. happy-dom lays nothing
     out, so what is pinned here is the cause: the slot is on every head, in the
     same position, and never taken out of the flow. Its glyph is what a leaf
     row withholds (page.css does that with visibility), and check-css keeps
     that rule from going back to display. */
  it('gives every head the fold slot, foldable or not', () => {
    draw()
    const [group, leaf] = heads()
    expect(group?.querySelector('b')?.textContent).toBe('Raven-Research')
    expect(leaf?.querySelector('b')?.textContent).toBe('Raven-Code')
    for (const head of [group, leaf]) {
      const fold = head?.firstElementChild as HTMLElement | null
      expect(fold?.className).toBe('agent-fold')
      expect(fold?.hasAttribute('hidden')).toBe(false)
    }
    expect((group?.firstElementChild as HTMLElement).dataset.empty).toBe('false')
    expect((leaf?.firstElementChild as HTMLElement).dataset.empty).toBe('true')
  })

  /* Which field the brand mark comes off. The roster answers a preset per row
     and a name the user may have changed, and the head reads the preset -- so
     a renamed Claude Code row still wears its own mark, and a row the user
     wrote themselves wears the glyph. The mark's own table is pinned in
     shell/agent-mark.test.tsx; what this adds is that the head passes it the
     preset and not the name. */
  it('marks a head from its preset, not from what the row is called', () => {
    wire({ list: async () => [] })
    render(
      <AgentList
        s={{
          ...store.getState(),
          roster: [
            { name: 'my-claude', preset: 'claude_code' },
            { name: 'claude_code' },
          ] as SubagentRow[],
          instances: [],
        }}
        compact
      />,
      { container: document.getElementById('wsBody')! },
    )
    const [renamed, lookalike] = heads()
    expect(renamed?.querySelector('.agent-mark img')?.getAttribute('src'))
      .toBe('assets/agents/claudecode-color.svg')
    expect(lookalike?.querySelector('.agent-mark img')).toBe(null)
    expect(lookalike?.querySelector('.agent-mark svg')).toBeTruthy()
  })
})

/* Why an unchanged snapshot must not repaint.
 *
 * The renderer holds a running turn's last assistant message provisionally, so
 * re-feeding the same snapshot rebuilds that row's DOM node -- measured in the
 * browser against the real renderer: three paints of one snapshot gave three
 * different nodes for the same sentence. The pane repaints every 2s for as long
 * as the instance reads `run`, so a turn that stops producing output without
 * ending leaves its last line rebuilt every two seconds for good. That is the
 * flicker; these cases are the guard, and the three after the first are the
 * reasons the guard must not be a blanket one.
 */
describe('an instance pane repaints only when something changed', () => {
  const turn = (over: Partial<DirectTurn> = {}): DirectTurn => ({
    call_id: 'c1', role: 'assistant', content: 'let me prepare this project', at_ms: 0, live: true, ...over,
  } as DirectTurn)

  const box = (): HTMLElement => {
    const el = document.createElement('div')
    document.getElementById('wsBody')!.appendChild(el)
    return el
  }

  it('paints once for a running turn that has stopped changing', async () => {
    const turns = [turn()]
    rows([], { instanceHistory: async () => ({ turns }) })
    const el = box()

    await act(async () => { store.paintInstanceDirect(el, 'Raven-Design', 'h1') })
    await act(async () => { store.paintInstanceDirect(el, 'Raven-Design', 'h1') })
    await act(async () => { store.paintInstanceDirect(el, 'Raven-Design', 'h1') })

    expect(paints.length).toBe(1)
  })

  it('holds the same guard on the standalone panel path', async () => {
    /* Two paint paths, one rule: the panel reads the same history and feeds the
       same renderer. A test for only the desk pane would leave the panel free
       to flicker with every mutation still green. */
    const turns = [turn()]
    rows([], { instanceHistory: async () => ({ turns }) })
    const el = box()

    await act(async () => { store.paintInstance(el, 'Raven-Design', 'h1') })
    await act(async () => { store.paintInstance(el, 'Raven-Design', 'h1') })

    expect(paints.length).toBe(1)
  })

  it('repaints over a transient failure, on an identical history', async () => {
    /* The recovery read is the one the guard would have swallowed: a stalled
       running turn recovers by answering the SAME history again, and the box
       holds an error rather than what the print says, so the print has to go
       when the error goes in. Without that the pane sat on a transient failure
       until the transcript or status changed. */
    const turns = [turn()]
    let fail = false
    rows([], { instanceHistory: async () => { if (fail) throw new Error('socket closed'); return { turns } } })
    const el = box()

    await act(async () => { store.paintInstanceDirect(el, 'Raven-Design', 'h1') })
    expect(paints.length).toBe(1)

    fail = true
    await act(async () => { store.paintInstanceDirect(el, 'Raven-Design', 'h1') })
    expect(el.textContent).toContain('socket closed')

    fail = false
    await act(async () => { store.paintInstanceDirect(el, 'Raven-Design', 'h1') })

    /* The paint happening is the whole assertion. Whether the error text is
       still in the box afterwards is the fixture's business -- it appends -- and
       in production `laneIn` starts a fresh lane from an emptied box precisely
       so an error note is not left pinned above the transcript
       (`transcript/mount.tsx`). */
    expect(paints.length).toBe(2)
  })

  it('repaints over a transient failure on the panel path too', async () => {
    const turns = [turn()]
    let fail = false
    rows([], { instanceHistory: async () => { if (fail) throw new Error('socket closed'); return { turns } } })
    const el = box()

    await act(async () => { store.paintInstance(el, 'Raven-Design', 'h1') })
    fail = true
    await act(async () => { store.paintInstance(el, 'Raven-Design', 'h1') })
    fail = false
    await act(async () => { store.paintInstance(el, 'Raven-Design', 'h1') })

    expect(paints.length).toBe(2)
  })

  it('paints again when the answer grows', async () => {
    /* The whole point of re-feeding a running turn: while it is still saying
       things, the row genuinely changes and has to be redrawn. */
    let turns = [turn()]
    rows([], { instanceHistory: async () => ({ turns }) })
    const el = box()
    await act(async () => { store.paintInstanceDirect(el, 'Raven-Design', 'h1') })

    turns = [turn({ content: 'let me prepare this project. Slide one is ready.' })]
    await act(async () => { store.paintInstanceDirect(el, 'Raven-Design', 'h1') })

    expect(paints.length).toBe(2)
  })

  it('paints again when the turn ends, on the same words', async () => {
    /* `status` decides how much of the snapshot is held rather than committed,
       so the same messages under a finished turn are a different picture: the
       held row has to be committed. A print without the status would leave the
       answer provisional for good. */
    let turns = [turn()]
    rows([], { instanceHistory: async () => ({ turns }) })
    const el = box()
    await act(async () => { store.paintInstanceDirect(el, 'Raven-Design', 'h1') })

    turns = [turn({ live: false })]
    await act(async () => { store.paintInstanceDirect(el, 'Raven-Design', 'h1') })

    expect(paints.length).toBe(2)
    expect((paints[1]!.ctx as { status?: string }).status).not.toBe('run')
  })

  it('paints when the box is pointed at another instance saying the same thing', async () => {
    /* Two instances can answer identically, and the print alone would then keep
       one conversation on screen under the other one's name. The reset is
       checked first for exactly that. */
    rows([], { instanceHistory: async () => ({ turns: [turn()] }) })
    const el = box()
    await act(async () => { store.paintInstanceDirect(el, 'Raven-Design', 'h1') })

    await act(async () => { store.paintInstanceDirect(el, 'Raven-Design', 'h2') })

    expect(paints.length).toBe(2)
    expect(paints[1]!.opts?.reset).toBe(true)
  })
})
