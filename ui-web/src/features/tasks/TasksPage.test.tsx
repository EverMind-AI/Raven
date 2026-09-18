// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { resetTranslator, setTranslator } from '../../i18n/t'
import { resetSources, setSources, sources } from '../../state/sources'
import * as desk from '../desk/store'
import * as workspace from '../workspace/store'
import * as store from './store'
import { TaskPane, TaskRuns, TasksApp, taskLine } from './TasksPage'

import type { WsChange } from '../workspace/types'
import type { TaskRow } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/* Driven through the seam production wires -- `state/sources.ts` -- which is
   what the island recipe asks of a feature's tests, and the only way to prove
   the renderer reads its rows through the seam rather than from an import. */
let rows: TaskRow[] = []

const task = (over: Partial<TaskRow> = {}): TaskRow => ({
  id: 't1', name: 'Cross-check quotes', source: 'dag', agent: 'Raven-Research',
  state: 'run', duration: '2m47s', nodes: [], ...over,
})

beforeEach(() => {
  rows = []
  store.reset()
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  setSources({ tasks: { list: async () => rows } })
})

afterEach(() => {
  cleanup()
  resetSources()
  resetTranslator()
})

const draw = async (): Promise<void> => {
  render(<TasksApp />)
  await act(async () => { await store.refresh() })
}

describe('the tasks list', () => {
  it('splits the rows into the two groups, each with its count', async () => {
    rows = [
      task({ id: 'a', state: 'run' }),
      task({ id: 'b', state: 'run' }),
      task({ id: 'c', state: 'done' }),
    ]
    await draw()

    expect([...document.querySelectorAll('.wsgrp')].map((g) => g.textContent))
      .toEqual(['gui.tasks.running 2', 'gui.tasks.settled 1'])
  })

  /* A failure is an ending. Grouping it with the live work would put a red dot
     where the reader is looking for progress. */
  it('files a failed run under finished, and tags it beside the name', async () => {
    rows = [task({ id: 'a', state: 'run' }), task({ id: 'b', state: 'fail' })]
    await draw()

    expect(store.running(store.rows())).toHaveLength(1)
    expect(store.settled(store.rows())).toHaveLength(1)
    const tags = [...document.querySelectorAll('.sarow.task .tkerr')]
    expect(tags).toHaveLength(1)
    expect(tags[0]!.textContent).toBe('error')
  })

  it('names the playbook a run came from, and the mechanism when there is no name', async () => {
    rows = [
      task({ id: 'a', source: 'playbook', sourceName: 'Playbook 02' }),
      task({ id: 'b', source: 'dag' }),
      task({ id: 'c', source: 'spawn' }),
    ]
    await draw()

    expect([...document.querySelectorAll('.tsrc')].map((c) => c.textContent))
      .toEqual(['Playbook 02', 'gui.tasks.src_dag', 'gui.tasks.src_spawn'])
  })

  /* The dot says the state. A second line that says it again in words spends
     its one line repeating what the reader already took in. */
  it('writes duration and products, and never the state word', () => {
    expect(taskLine(task({ state: 'run', duration: '2m47s' }))).toBe('2m47s')
    expect(taskLine(task({ state: 'done', duration: '4m12s', artifacts: [{ name: 'a.pptx' }] })))
      .toBe('4m12s · gui.tasks.artifacts_1 {"n":1}')
    expect(taskLine(task({ state: 'fail', duration: '38s' }))).toBe('38s')
  })

  it('says the panel is empty rather than drawing an empty list', async () => {
    await draw()

    expect(screen.getByText('gui.tasks.none')).toBeTruthy()
    expect(document.querySelector('.sarow.task')).toBeNull()
  })

  /* A source that is absent and one that answers empty are the same state while
     there is no `tasks.*` method: neither may throw, which `ds()` would. */
  it('reads an absent source as an empty list', async () => {
    delete sources.tasks
    await draw()

    expect(store.rows()).toEqual([])
    expect(screen.getByText('gui.tasks.none')).toBeTruthy()
  })

  /* The list is the floating palette and the detail is the docked pane, so a
     row opens a pane rather than replacing the list it was clicked in. */
  it('opens a row as a desk pane', async () => {
    rows = [task({ id: 'a' })]
    await draw()

    await act(async () => { (document.querySelector('.sarow.task') as HTMLElement).click() })

    const panes = desk.get().panes
    expect(panes).toHaveLength(1)
    expect(panes[0]!.id).toBe('task:a')
    expect(panes[0]!.kind).toBe('task')
    /* And the list is still the list. */
    expect(document.querySelector('.sarow.task')).not.toBeNull()
  })
})

describe('a task pane', () => {
  const withNodes = task({
    id: 'a',
    nodes: [
      {
        id: 'n1', title: 'Read the spot price', agent: 'Raven-Research', status: 'completed',
        from: [], duration: '48s', skills: ['gold-brief'], mcps: ['playwright'],
        record: { dispatch: 'go', steps: [{ kind: 'tool', tool: 'exec', arg: 'a', result: 'b' }], answer: 'done' },
      },
      {
        id: 'n2', title: 'Reconcile', agent: 'Raven-Research', status: 'running', from: ['n1'],
        prompt: 'Reconcile {{ n1.output }} for {{ inputs.week }}.',
        record: { dispatch: 'go', steps: [] },
      },
      { id: 'n3', title: 'Write it up', agent: 'Raven', status: 'pending', from: ['n2'], prompt: 'Write {{ n2.output }}.' },
    ],
  })

  /* Closes first when a card is already up: in the docked pane the card fills
     the board's place, so reaching another step means closing this one. The
     full-screen fork keeps the board beside the card and needs no such step. */
  const pick = async (i: number): Promise<void> => {
    const shut = document.querySelector('.tkx') as HTMLElement | null
    if (shut) await act(async () => { shut.click() })
    await act(async () => { fireEvent.click(document.querySelectorAll('.daggraph .nd')[i] as Element) })
  }

  /* One drawing of the steps, at both sizes. A list beside a graph would be a
     second thing to keep true about the same run. */
  it('lands on the board, one box per step, and says who runs each', () => {
    render(<TaskPane task={withNodes} />)

    expect(document.querySelector('.gstage')).not.toBeNull()
    expect([...document.querySelectorAll('.daggraph .nd .id')].map((b) => b.textContent))
      .toEqual(['Read the spot price', 'Reconcile', 'Write it up'])
    expect([...document.querySelectorAll('.daggraph .nd .ag')].map((b) => b.textContent))
      .toEqual(['Raven-Research', 'Raven-Research', 'Raven'])
    /* The one step in flight is the one the eye should find. */
    expect(document.querySelectorAll('.daggraph .nd[data-st="running"]')).toHaveLength(1)
    expect(document.querySelector('.tkcard')).toBeNull()
  })

  /* The card is headed by what the step is for, not by the key it is filed
     under. The id is a field in the work order, where a reader looking up a
     dependency or a placeholder goes for it. */
  it('heads a node card with its summary, and keeps the id as a field', async () => {
    render(<TaskPane task={withNodes} />)
    await pick(0)

    expect(document.querySelector('.tkch h4')?.textContent).toBe('Read the spot price')

    await act(async () => { (document.querySelectorAll('.tktabs button')[1] as HTMLElement).click() })
    expect([...document.querySelectorAll('.tkkv dt')].map((d) => d.textContent))
      .toEqual(['node_id', 'subagent', 'skills', 'mcps'])
    expect(document.querySelectorAll('.tkkv dd')[0]?.textContent).toBe('n1')
  })

  /* Two open tasks are two panes, and what one describes is its own. A single
     selected-node field moved both: with ids that differ the other pane's card
     vanished, and with ids two tasks share -- which is what these two have --
     it opened a card nobody asked it for. */
  it('keeps each open pane on the step its own reader picked', async () => {
    const other = task({ id: 'b', name: 'Draft the note', nodes: withNodes.nodes })
    render(<><TaskPane task={withNodes} /><TaskPane task={other} /></>)
    const panes = (): Element[] => [...document.querySelectorAll('.tkview')]

    await act(async () => {
      fireEvent.click(panes()[1]!.querySelectorAll('.daggraph .nd')[0] as Element)
    })

    expect(panes()[1]!.querySelector('.tkch h4')?.textContent).toBe('Read the spot price')
    expect(panes()[0]!.querySelector('.tkcard')).toBeNull()
    expect(panes()[0]!.querySelector('.gstage')).not.toBeNull()

    /* And the other way: the first pane picks a different step, and neither
       pane is moved off what it was showing. */
    await act(async () => {
      fireEvent.click(panes()[0]!.querySelectorAll('.daggraph .nd')[1] as Element)
    })

    expect(panes()[0]!.querySelector('.tkch h4')?.textContent).toBe('Reconcile')
    expect(panes()[1]!.querySelector('.tkch h4')?.textContent).toBe('Read the spot price')
  })

  it('gives the board back when the card is closed', async () => {
    render(<TaskPane task={withNodes} />)
    await pick(0)
    expect(document.querySelector('.gstage')).toBeNull()

    await act(async () => { (document.querySelector('.tkx') as HTMLElement).click() })
    expect(document.querySelector('.gstage')).not.toBeNull()
  })

  /* Flow is the usual question -- except on a step that has not run, where the
     only thing there is to read is what it was asked. */
  it('opens on the flow, and on the order for a step that has not run', async () => {
    render(<TaskPane task={withNodes} />)

    await pick(0)
    expect(document.querySelector('.tktabs button[aria-selected="true"]')?.textContent)
      .toBe('gui.tasks.tab_flow')

    await pick(2)
    expect(document.querySelector('.tktabs button[aria-selected="true"]')?.textContent)
      .toBe('gui.tasks.tab_order')
  })

  /* A reader comparing the same thing across steps has already answered the
     question the per-node default answers, and re-deciding for them would undo
     the choice they just made. */
  it('keeps the tab the reader chose as they move between nodes', async () => {
    render(<TaskPane task={withNodes} />)
    await pick(0)
    await act(async () => { (document.querySelectorAll('.tktabs button')[1] as HTMLElement).click() })

    await pick(1)

    expect(document.querySelector('.tktabs button[aria-selected="true"]')?.textContent)
      .toBe('gui.tasks.tab_order')
    /* Including on a node whose own default would have been the other one. */
    await pick(2)
    expect(document.querySelector('.tktabs button[aria-selected="true"]')?.textContent)
      .toBe('gui.tasks.tab_order')
  })

  /* A spawn has no skills or mcps parameter at all, so both rows read as a dash
     rather than as an empty list -- the honest answer to "which skills" when the
     tool never took any. */
  it('writes a dash for a field the source has no parameter for', async () => {
    render(<TaskPane task={withNodes} />)
    await pick(1)
    await act(async () => { (document.querySelectorAll('.tktabs button')[1] as HTMLElement).click() })

    expect([...document.querySelectorAll('.tkkv dd')].map((d) => d.textContent))
      .toEqual(['n2', 'Raven-Research', '—', '—'])
  })

  /* The whole question a reader brings to a template is which parts are filled
     in from elsewhere, so those are the parts that are marked. */
  it('highlights every placeholder in the prompt and nothing else', async () => {
    render(<TaskPane task={withNodes} />)
    await pick(1)
    await act(async () => { (document.querySelectorAll('.tktabs button')[1] as HTMLElement).click() })

    expect([...document.querySelectorAll('.tkph')].map((m) => m.textContent))
      .toEqual(['{{ n1.output }}', '{{ inputs.week }}'])
    expect(document.querySelector('.tkpv')?.textContent).toContain('Reconcile')
  })

  it('says why a run stopped, above the steps', () => {
    render(<TaskPane task={task({ failure: 'The vendor answered 429.', state: 'fail' })} />)

    expect(document.querySelector('.tkfail')?.textContent).toBe('The vendor answered 429.')
    expect(document.querySelector('.tkhead .tkerr')).not.toBeNull()
  })

  /* The dot carries the state here too, and `confirm` is absent for a spawn
     rather than false: the tool has no such parameter, and a printed false
     would answer a question nobody asked. */
  it('writes duration and approval, and never the state word', () => {
    render(<TaskPane task={task({ confirm: false, duration: '2m47s' })} />)
    expect(document.querySelector('.tkline')?.textContent)
      .toBe('2m47s · gui.tasks.confirm {"v":"false"}')

    cleanup()
    render(<TaskPane task={task({ duration: '2m47s' })} />)
    expect(document.querySelector('.tkline')?.textContent).toBe('2m47s')
  })
})

describe("a node's execution flow", () => {
  const recorded = task({
    id: 'a',
    nodes: [
      {
        id: 'n1', title: 'Reconcile', agent: 'Raven-Research', status: 'completed', from: [],
        duration: '29s', prompt: 'p',
        record: {
          dispatch: 'Spot quotes:\n[BEGIN UNTRUSTED subagent #spot]\n4,312.5\n[END UNTRUSTED subagent #spot]\n\nGo.',
          steps: [
            { kind: 'think', text: 'two dates, align first' },
            { kind: 'tool', tool: 'exec', arg: 'python align.py', result: 'exit 0' },
          ],
          answer: 'Aligned.',
        },
      },
    ],
  })

  const openFirst = async (row: TaskRow): Promise<void> => {
    render(<TaskPane task={row} />)
    await act(async () => { fireEvent.click(document.querySelector('.daggraph .nd') as Element) })
  }

  /* The fence an upstream answer arrived in stays on screen. This tab is the
     one place a reader sees one agent's words rendered for another's prompt,
     and the boundary is why showing them is safe. */
  it('keeps the untrusted fence visible around an upstream answer', async () => {
    await openFirst(recorded)

    const fenced = [...document.querySelectorAll('.tkuntrusted')].map((n) => n.textContent)
    expect(fenced).toHaveLength(1)
    expect(fenced[0]).toContain('[BEGIN UNTRUSTED subagent #spot]')
    expect(fenced[0]).toContain('[END UNTRUSTED subagent #spot]')
  })

  /* The record can be carried on with: a step's executor is a stateful
     instance that outlives the step, so the flow tab ends in a field addressed
     to it by name. */
  it("ends the flow with a field addressed to the step's own sub-agent", async () => {
    await openFirst(recorded)

    const box = document.querySelector('.tkchat input') as HTMLInputElement
    expect(box).not.toBeNull()
    expect(box.placeholder).toBe('gui.tasks.chat_on {"agent":"Raven-Research"}')
    /* Nothing to send yet, so the arrow is not offering to. */
    expect((document.querySelector('.tkgo') as HTMLButtonElement).disabled).toBe(true)
  })

  /* The work order is what the step was asked, which is settled; the field
     belongs to the flow, which is the conversation it carries on. */
  it('keeps the field off the work order', async () => {
    await openFirst(recorded)
    await act(async () => { (document.querySelectorAll('.tktabs button')[1] as HTMLElement).click() })

    expect(document.querySelector('.tkchat')).toBeNull()
  })

  it('folds the process, names its cost, and opens it in the order it happened', async () => {
    await openFirst(recorded)
    expect(document.querySelector('.tkprocb')).toBeNull()
    expect(document.querySelector('.tkprocn')?.textContent)
      .toBe('gui.tasks.record_tools {"n":1} · 29s')

    await act(async () => { (document.querySelector('.tkprock') as HTMLElement).click() })

    expect([...document.querySelectorAll('.tkprocb > div')].map((d) => d.className))
      .toEqual(['tkthink', 'tktool'])
    expect(document.querySelector('.tkans')?.textContent).toBe('Aligned.')
  })

  /* A run that stopped has no answer, and what goes in the answer's place is
     the sub-agent's own account of stopping -- not an empty panel. */
  it('puts the stop note where the answer would be', async () => {
    const stopped = task({
      id: 'b',
      nodes: [{
        id: 'n1', title: 'Fetch', agent: 'Raven-Research', status: 'failed', from: [],
        record: { dispatch: 'go', steps: [{ kind: 'tool', tool: 'web_fetch', arg: 'u', result: 'HTTP 429' }], stopped: 'Stopped without a quote.' },
      }],
    })
    await openFirst(stopped)

    expect(document.querySelector('.tkstop')?.textContent).toBe('Stopped without a quote.')
    expect(document.querySelector('.tkans')).toBeNull()
    /* And the card says so beside the node's name. */
    expect(document.querySelector('.tkch .tkerr')).not.toBeNull()
  })

  /* A step nobody has started has no flow, and saying "not started" is a
     different answer from showing an empty one. */
  it('says a step has not started rather than drawing an empty flow', async () => {
    const pending = task({
      id: 'c',
      nodes: [{ id: 'n1', title: 'Later', agent: 'Raven', status: 'pending', from: [] }],
    })
    await openFirst(pending)
    await act(async () => { (document.querySelectorAll('.tktabs button')[0] as HTMLElement).click() })

    expect(document.querySelector('.tkempty')?.textContent).toBe('gui.tasks.rec_pending')
  })

  /* And a step that ran is not a step that never started. The panel holds no
     record for it, which is a fact about the panel; "not started yet" would be
     a claim about the run. */
  it('says a record has not been read rather than calling the step unstarted', async () => {
    const unread = task({
      id: 'd',
      nodes: [{ id: 'n1', title: 'Pull the futures side', agent: 'Raven-Research', status: 'completed', from: [], duration: '1m12s' }],
    })
    await openFirst(unread)

    expect(document.querySelector('.tkempty')?.textContent).toBe('gui.tasks.rec_unread')
  })
})

/* Full screen is where the fork lives: the same board the playbook page reads a
   stored graph in, around the same graph the dag island draws. */
describe('a full-screen task', () => {
  const forked = task({
    id: 'f',
    nodes: [
      { id: 'n1', title: 'Read the spot price', agent: 'Raven-Research', status: 'completed', from: [], duration: '48s' },
      { id: 'n2', title: 'Read the futures', agent: 'Raven-Research', status: 'completed', from: [], duration: '51s' },
      { id: 'n3', title: 'Reconcile', agent: 'Raven', status: 'running', from: ['n1', 'n2'] },
    ],
  })

  /* happy-dom reports every element as zero-width, so the viewport size has to
     be supplied; without it the board has nothing to frame the graph against. */
  function withPort(w: number, h: number): () => void {
    const own = {
      w: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth'),
      h: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight'),
    }
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => w })
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => h })
    return () => {
      if (own.w) Object.defineProperty(HTMLElement.prototype, 'clientWidth', own.w)
      if (own.h) Object.defineProperty(HTMLElement.prototype, 'clientHeight', own.h)
    }
  }

  const draw = (row: TaskRow = forked): void => {
    render(<TaskPane task={row} full />)
  }

  it('draws the graph on the shared board, with the node beside it', () => {
    const restore = withPort(900, 500)
    try {
      draw()

      expect(document.querySelector('.gstage')).not.toBeNull()
      expect(document.querySelector('.gboard .daggraph')).not.toBeNull()
      /* The same board the docked pane draws, with the room the fork wants and
         the node card beside it. */
      expect(document.querySelectorAll('.daggraph .nd')).toHaveLength(3)
    } finally {
      restore()
    }
  })

  /* The board opens framed on the whole graph and the readout is the control
     that puts it back, both of which are the board's own behaviour -- asserted
     here only to prove the task view hands it a real content size rather than a
     zero one, which frames every graph at 1:1. */
  it('opens framed on the whole graph', () => {
    const restore = withPort(400, 500)
    try {
      draw()

      const pct = document.querySelector('.gzpct')?.textContent || ''
      expect(Number(pct.replace('%', ''))).toBeLessThan(100)
    } finally {
      restore()
    }
  })

  it('picks a step from the graph, and keeps the graph beside the card', () => {
    const restore = withPort(900, 500)
    try {
      draw()
      expect(document.querySelector('.tkpick')?.textContent).toBe('gui.tasks.pick_node')

      act(() => { fireEvent.click(document.querySelectorAll('.daggraph .nd')[2] as Element) })

      expect(document.querySelector('.tkch h4')?.textContent).toBe('Reconcile')
      /* The board the step was picked out of is still on screen, which is the
         one thing full screen has that the docked pane does not: there the card
         takes the board's place. */
      expect(document.querySelector('.gstage')).not.toBeNull()
      expect(document.querySelector('.daggraph .nd[data-sel]')).not.toBeNull()
      expect(document.querySelector('.tkpick')).toBeNull()
    } finally {
      restore()
    }
  })

  /* The same pick in the docked pane covers the board, because the pane is not
     wide enough for both. One store, two layouts. */
  it('covers the board with the card in the docked pane', async () => {
    draw()
    cleanup()
    render(<TaskPane task={forked} />)
    await act(async () => { fireEvent.click(document.querySelectorAll('.daggraph .nd')[0] as Element) })

    expect(document.querySelector('.tkcard')).not.toBeNull()
    expect(document.querySelector('.gstage')).toBeNull()
  })
})

/* The chips are the door to what a task left behind: the design sends a product
   chip to the preview and a changed file's chip to the diff, and a reader who
   has just seen that a task produced something should not have to go and find
   it in another tab. */
describe('the header chips', () => {
  const opened: string[] = []

  const change = (over: Partial<WsChange> = {}): WsChange => ({
    key: '/w/fetch_gold.py', dir: '/w/', name: 'fetch_gold.py', kind: 'edit',
    add: 5, del: 1, hunks: [], turn: 1, open: false, ...over,
  })

  beforeEach(() => {
    opened.length = 0
    desk._resetForTests()
    workspace.reset()
    setSources({
      tasks: { list: async () => rows },
      /* A source that cannot browse, which is the case the workspace opener
         exists to keep honest. */
      workspace: {
        shortPath: (p: string) => p,
        hostPlatform: () => 'mac',
        canBrowse: false,
        openPath: (p: string) => opened.push(p),
      },
    })
  })

  const chips = (): HTMLElement[] => [...document.querySelectorAll('.tkchips .wchip')] as HTMLElement[]

  it('sends a product chip through the workspace opener, not straight at the desk', () => {
    render(<TaskPane task={task({ artifacts: [{ name: 'gold-market-v1.pptx' }] })} />)

    act(() => { chips()[0]!.click() })

    /* A source that cannot browse answers with its own note. Opening a desk
       pane here would put a viewer on screen with nothing to show in it. */
    expect(opened).toEqual(['gold-market-v1.pptx'])
    expect(desk.get().panes).toHaveLength(0)
  })

  it('opens a changed file as the diff the session recorded', () => {
    workspace.shared().changes = [change()]
    render(<TaskPane task={task({ diffs: [{ file: 'fetch_gold.py', add: 5, del: 1, key: '/w/fetch_gold.py' }] })} />)

    act(() => { chips()[0]!.click() })

    const panes = desk.get().panes
    expect(panes).toHaveLength(1)
    expect(panes[0]!.kind).toBe('diff')
    expect(panes[0]!.id).toBe('diff:/w/fetch_gold.py:1')
  })

  /* The name is what a task row is likeliest to carry, and it is what the
     change list draws, so it resolves too. */
  it('finds the change by the name when the task names no path', () => {
    workspace.shared().changes = [change()]
    render(<TaskPane task={task({ diffs: [{ file: 'fetch_gold.py', add: 5, del: 1 }] })} />)

    act(() => { chips()[0]!.click() })

    expect(desk.get().panes[0]!.kind).toBe('diff')
  })

  /* A chip that does nothing is worse than one that shows the file without its
     history: the hunks live in the session's change list, and a task read back
     into a desk whose list has been replayed has no claim on them. */
  it('falls back to the file when the session has no such change', () => {
    render(<TaskPane task={task({ diffs: [{ file: 'fetch_gold.py', add: 5, del: 1 }] })} />)

    act(() => { chips()[0]!.click() })

    expect(opened).toEqual(['fetch_gold.py'])
    expect(desk.get().panes).toHaveLength(0)
  })

  it('draws no chip strip for a task that left nothing behind', () => {
    render(<TaskPane task={task()} />)

    expect(document.querySelector('.tkchips')).toBeNull()
  })
})

/* The strip above the composer: what is running, while you type. A reader who
   never opens the panel still sees the work in flight and can reach it. */
describe('the running strip', () => {
  /* Its own desk: a chip opens a pane, and the pane one test opened is still
     standing when the next one counts them. */
  beforeEach(() => { desk._resetForTests() })

  const draw = async (): Promise<void> => {
    render(<TaskRuns />)
    await act(async () => { await store.refresh() })
  }

  /* happy-dom measures everything as zero, so a rail with somewhere to scroll
     has to be told its size. Returns the undo.
     Deleted rather than restored when there was nothing to restore: these live
     on Element in this DOM, so the lines below ADD an own property to
     HTMLElement, and putting back an undefined descriptor would leave the fake
     measurements in place for every test after this one. */
  const sized = (client: number, scroll: number): (() => void) => {
    const own = {
      clientWidth: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth'),
      scrollWidth: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollWidth'),
    }
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => client })
    Object.defineProperty(HTMLElement.prototype, 'scrollWidth', { configurable: true, get: () => scroll })
    return () => {
      for (const [k, d] of Object.entries(own)) {
        if (d) Object.defineProperty(HTMLElement.prototype, k, d)
        else delete (HTMLElement.prototype as unknown as Record<string, unknown>)[k]
      }
    }
  }

  it('carries one chip per running task, with the step it is on', async () => {
    rows = [
      task({ id: 'a', name: 'Cross-check quotes', state: 'run', step: [3, 5] }),
      task({ id: 'b', name: 'Weekly report', state: 'run', step: [4, 7] }),
      task({ id: 'c', name: 'Build the deck', state: 'done' }),
    ]
    await draw()

    const chips = [...document.querySelectorAll('.trun')]
    expect(chips.map((c) => c.querySelector('.nm')?.textContent))
      .toEqual(['Cross-check quotes', 'Weekly report'])
    /* Which step of how many, not how long: the list's second line answers the
       other one, and a chip is too narrow for both. */
    expect(chips.map((c) => c.querySelector('.st')?.textContent))
      .toEqual(['gui.tasks.step {"i":3,"n":5}', 'gui.tasks.step {"i":4,"n":7}'])
  })

  /* The strip is the dock's, and the dock is not remounted when the reader
     opens another conversation -- so nothing takes it down and its own effect
     has already stopped asking. Left as it was it would show the conversation
     they left, and never request the one they opened. The switch clears the
     panel through state/ws.ts's reset (registered in src/app/install.ts). */
  it("drops one conversation's runs and asks for the next one's", async () => {
    rows = [task({ id: 'a', name: 'Cross-check quotes', state: 'run' })]
    await draw()
    expect([...document.querySelectorAll('.trun .nm')].map((c) => c.textContent))
      .toEqual(['Cross-check quotes'])

    rows = [task({ id: 'b', name: 'Draft the note', state: 'run' })]
    await act(async () => { store.reset() })

    expect([...document.querySelectorAll('.trun .nm')].map((c) => c.textContent))
      .toEqual(['Draft the note'])
  })

  /* An empty strip is a gap above the box the reader types in. Saying "no
     tasks" in words is the panel's job. */
  it('draws nothing at all when nothing is running', async () => {
    rows = [task({ id: 'c', state: 'done' })]
    await draw()

    expect(document.querySelector('.trun')).toBeNull()
  })

  /* The strip is on screen from the first paint and the panel behind it may
     never be opened, so it is usually what asks for the rows. */
  it('reads the rows itself rather than waiting for the panel', async () => {
    rows = [task({ id: 'a', state: 'run', step: [1, 2] })]
    render(<TaskRuns />)
    await act(async () => {})

    expect(store.get().loaded).toBe(true)
    expect(document.querySelectorAll('.trun')).toHaveLength(1)
  })

  it('opens the task as a pane, the same door the list row is', async () => {
    rows = [task({ id: 'a', state: 'run', step: [1, 2] })]
    await draw()

    await act(async () => { (document.querySelector('.trun') as HTMLElement).click() })

    const panes = desk.get().panes
    expect(panes).toHaveLength(1)
    expect(panes[0]!.id).toBe('task:a')
  })

  /* One line, whatever the count: wrapping is what a list does, and six running
     tasks wrapped take three rows of the conversation with them. */
  it('keeps the chips on one scrolling line, and fades only the end with more beyond it', async () => {
    const own = sized(200, 600)
    try {
      rows = [task({ id: 'a', state: 'run' }), task({ id: 'b', state: 'run' })]
      await draw()

      const strip = document.querySelector('.runs') as HTMLElement
      const rail = document.querySelector('.runrail') as HTMLElement
      expect(rail).not.toBeNull()
      expect(strip.hasAttribute('data-l')).toBe(false)
      expect(strip.hasAttribute('data-r')).toBe(true)

      rail.scrollLeft = 400
      await act(async () => { fireEvent.scroll(rail) })

      expect(strip.hasAttribute('data-l')).toBe(true)
      expect(strip.hasAttribute('data-r')).toBe(false)
    } finally {
      own()
    }
  })

  /* The bar is hidden, so without these a plain mouse cannot move the rail at
     all: no horizontal wheel, no bar to drag. */
  it('drives the rail with a plain wheel, and hands the flick back at the end', async () => {
    const own = sized(200, 600)
    try {
      rows = [task({ id: 'a', state: 'run' }), task({ id: 'b', state: 'run' })]
      await draw()
      const rail = document.querySelector('.runrail') as HTMLElement

      const mid = new WheelEvent('wheel', { deltaY: 120, bubbles: true, cancelable: true })
      act(() => { rail.dispatchEvent(mid) })
      expect(rail.scrollLeft).toBe(120)
      expect(mid.defaultPrevented).toBe(true)

      /* At the end it belongs to the page again: a strip that swallows the
         flick there stops the conversation scrolling because the pointer
         happens to be over three chips. */
      rail.scrollLeft = 400
      const past = new WheelEvent('wheel', { deltaY: 120, bubbles: true, cancelable: true })
      act(() => { rail.dispatchEvent(past) })
      expect(rail.scrollLeft).toBe(400)
      expect(past.defaultPrevented).toBe(false)
    } finally {
      own()
    }
  })

  it('scrolls on a drag, and a drag is not a click on the chip under it', async () => {
    const own = sized(200, 600)
    try {
      rows = [task({ id: 'a', state: 'run' }), task({ id: 'b', state: 'run' })]
      await draw()
      const rail = document.querySelector('.runrail') as HTMLElement
      rail.setPointerCapture = () => {}
      const chip = document.querySelector('.trun') as HTMLElement

      fireEvent.pointerDown(chip, { pointerId: 1, pointerType: 'mouse', button: 0, clientX: 300 })
      fireEvent.pointerMove(rail, { pointerId: 1, pointerType: 'mouse', clientX: 240 })
      fireEvent.pointerUp(rail, { pointerId: 1, pointerType: 'mouse', clientX: 240 })
      fireEvent.click(chip)

      expect(rail.scrollLeft).toBe(60)
      expect(desk.get().panes).toHaveLength(0)

      /* A press that stayed still is still a click on the chip. */
      fireEvent.pointerDown(chip, { pointerId: 2, pointerType: 'mouse', button: 0, clientX: 240 })
      fireEvent.pointerUp(chip, { pointerId: 2, pointerType: 'mouse', clientX: 240 })
      await act(async () => { fireEvent.click(chip) })

      expect(desk.get().panes).toHaveLength(1)
    } finally {
      own()
    }
  })

  /* Nothing fades when everything fits, which is the usual case: a ramp over an
     end that is already the end dims a chip for no reason. */
  it('fades neither end when the chips fit', async () => {
    rows = [task({ id: 'a', state: 'run' })]
    await draw()

    const strip = document.querySelector('.runs') as HTMLElement
    expect(strip.hasAttribute('data-l')).toBe(false)
    expect(strip.hasAttribute('data-r')).toBe(false)
  })

  /* Pointed at from either side: the strip and the list light each other up,
     and neither owns the pointer. */
  it('lights the same task in the list, and takes the light from it', async () => {
    rows = [task({ id: 'a', state: 'run', step: [1, 2] }), task({ id: 'b', state: 'run', step: [1, 2] })]
    await draw()

    fireEvent.mouseOver(document.querySelectorAll('.trun')[1] as Element)
    expect(store.get().hover).toBe('b')

    act(() => { store.hover('a') })
    expect([...document.querySelectorAll('.trun')].map((c) => c.classList.contains('hl')))
      .toEqual([true, false])
  })
})
