// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import * as mount_ from './mount'
import { SubagentsApp } from './SubagentsPage'
import * as store from './store'
import { _resetForTests as sessionReset, setCurrent } from '../../shell/session'

import type { Shell } from '../../shell/bridge'
import type { AgentCtx, AgentRow, AgentsSource, DirectTurn, InstanceRow } from './types'

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
    dur: (ms) => `${Math.round(ms / 1000)}s`,
    plainTitle: (s) => s.replace(/^\p{Extended_Pictographic}+\s*/u, ''),
    agentStagePaint: (box, ctx, opts) => {
      paints.push({ ctx, opts })
      box.appendChild(document.createElement('p'))
    },
  }
  window.RavenShell = fakeShell
  sessionReset()
  setCurrent('s1')
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
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('subagents island, the list', () => {
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
    const run: AgentRow = { id: 'a1', label: 'survey', status: 'ok', agent: null, started_at: iso(Date.now() - 9000), ended_at: iso(Date.now() - 2000), tokens: 40 }
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
      /* By what the row shows, which is the node's name where it has one. */
      ;(await screen.findByText(row.nodeId || row.handle)).closest('.sarow')!
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
    await act(async () => {
      poll?.()
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
})
