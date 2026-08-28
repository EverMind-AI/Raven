// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { PlaybooksApp } from './PlaybooksPage'
import * as store from './store'

import type { PlaybookDetail, PlaybookNode, PlaybookRow, PlaybooksSource } from './types'
import type { Shell } from '../../shell/bridge'

function node(over: Partial<PlaybookNode> & { id: string }): PlaybookNode {
  return {
    subagent: 'Raven',
    node_summary: 'do the thing',
    prompt_template: 'do it',
    depends_on: [],
    instance: '',
    inputs: {},
    ...over
  }
}

function row(over: Partial<PlaybookRow> & { name: string }): PlaybookRow {
  return {
    description: 'what it is for',
    task_summary: 'what running it dispatches',
    mode: 'dag',
    confirm: true,
    origin: 'user',
    disabled: false,
    nodes: [
      { id: 'a', depends_on: [] },
      { id: 'b', depends_on: ['a'] }
    ],
    error: '',
    ...over
  }
}

function detail(over: Partial<PlaybookDetail> & { name: string }): PlaybookDetail {
  return {
    description: 'what it is for',
    task_summary: 'what running it dispatches',
    version: 1,
    mode: 'dag',
    confirm: true,
    origin: 'user',
    disabled: false,
    path: '~/.raven/playbooks/x/playbook.md',
    keywords: ['kw'],
    params: {},
    nodes: [node({ id: 'a' }), node({ id: 'b', depends_on: ['a'] })],
    prompts: '',
    ...over
  }
}

/* The island runs against the two seams production wires: a fake shell on
   window.RavenShell (T answers its own key, so assertions name catalogue keys
   rather than translations) and a fixture source on window.DS.playbooks. */
const pages: (string | null)[] = []

function install(over: Partial<PlaybooksSource> = {}): void {
  window.RavenShell = {
    T: (key: string, vars?: Record<string, unknown>) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    confirmAsk: (_t: unknown, _b: unknown, _l: unknown, fn: () => void) => fn(),
    showPage: (id: string | null) => pages.push(id),
    closeDetail: () => {}
  } as unknown as Shell
  window.DS = {
    ...(window.DS || {}),
    playbooks: {
      list: async () => [row({ name: 'issue-triage' })],
      get: async (name: string) => detail({ name }),
      ...over
    }
  }
}

async function mount() {
  const view = render(<PlaybooksApp />)
  await act(async () => {
    await store.load()
  })
  return view
}

afterEach(() => {
  cleanup()
  store._resetForTests()
  pages.length = 0
  delete (window as { DS?: unknown }).DS
})

describe('the playbook library', () => {
  it('puts the name, the description and the drawing on a card, and nothing else', async () => {
    install()
    await mount()
    expect(screen.getByText('issue-triage')).toBeTruthy()
    expect(screen.getByText('what it is for')).toBeTruthy()
    /* The drawing is the statement about shape: a step and stage count under it
       said in words what the reader is already looking at. */
    expect(document.querySelector('.pbcard .pbrib')).not.toBeNull()
    expect(document.querySelector('.pbcard .pbcap')).toBeNull()
    /* And `taskSummary` is not on the wall at all, because nothing on the wall
       is decided by it. */
    expect(screen.queryByText('what running it dispatches')).toBeNull()
  })

  it('keeps the caption only where there is no drawing to read', async () => {
    /* A prompt-mode playbook stores no graph, so its dashed boxes stand for a
       shape composed per run -- unexplained without the line. */
    install({ list: async () => [row({ name: 'competitor-scan', mode: 'prompt', nodes: [] })] })
    await mount()
    expect(document.querySelector('.pbcard .pbcap')?.textContent).toBe('gui.pb.shape_live')
  })

  it('counts the matches while a query is on, not the library', async () => {
    install({
      list: async () => [
        row({ name: 'issue-triage' }),
        row({ name: 'release-notes', description: 'what changed in a version' })
      ]
    })
    await mount()
    expect(screen.getByText('gui.pb.count {"n":2}')).toBeTruthy()
    fireEvent.change(document.querySelector('.cfind input') as Element, { target: { value: 'changed' } })
    /* The total nobody asked for is the less useful of the two answers. */
    expect(screen.getByText('gui.pb.matched {"n":1}')).toBeTruthy()
    expect(screen.queryByText('gui.pb.count {"n":2}')).toBeNull()
  })

  it('says why an unreadable file cannot be opened, and does not open it', async () => {
    install({ list: async () => [row({ name: 'repo-audit', error: 'nodes.2.subagent: unknown agent', nodes: [] })] })
    await mount()
    expect(screen.getByText(/gui\.pb\.unreadable/)).toBeTruthy()
    fireEvent.click(screen.getByText('repo-audit'))
    await act(async () => {})
    /* Still the library: a broken row has nothing to show behind it. */
    expect(screen.getByText('repo-audit')).toBeTruthy()
    expect(document.querySelector('.pbcanvas')).toBeNull()
  })

  it('hands the library back when the detail read fails, and says why', async () => {
    /* The file can go away between the listing and the click. Without this the
       reader is left on the reading placeholder, which has no way back. */
    const host = document.createElement('div')
    host.id = 'toasts'
    document.body.appendChild(host)
    install({
      get: async () => {
        throw new Error('playbook.md: no such file')
      }
    })
    await mount()
    fireEvent.click(screen.getByText('issue-triage'))
    await act(async () => {})
    expect(screen.queryByText('gui.pb.reading')).toBeNull()
    expect(store.getState().openName).toBeNull()
    expect(host.textContent).toContain('playbook.md: no such file')
    /* And the library is what is showing, so another card is one click away. */
    expect(screen.getByText('issue-triage')).toBeTruthy()
    host.remove()
  })

  it('does not stay stuck after the page is closed and opened again', async () => {
    install({
      get: async () => {
        throw new Error('gone')
      }
    })
    await mount()
    fireEvent.click(screen.getByText('issue-triage'))
    await act(async () => {})
    store.closePage()
    await act(async () => {
      await store.load()
    })
    expect(screen.queryByText('gui.pb.reading')).toBeNull()
    expect(screen.getByText('issue-triage')).toBeTruthy()
  })

  it('re-reads a detail the user edited between two visits to the library', async () => {
    /* The backend builds a store per call so a file edit is visible; a detail
       cached from the previous listing would hide it for the whole session. */
    let summary = 'the first version'
    install({ get: async (name: string) => detail({ name, task_summary: summary }) })
    await mount()
    fireEvent.click(screen.getByText('issue-triage'))
    await act(async () => {})
    expect(screen.getByText('what it is for')).toBeTruthy()
    store.back()
    summary = 'the second version'
    await act(async () => {
      await store.load()
    })
    fireEvent.click(screen.getByText('issue-triage'))
    await act(async () => {})
    expect(store.getState().detail?.task_summary).toBe('the second version')
  })

  it('re-reads the playbook still on screen when the page is opened again', async () => {
    /* Closing the module keeps the reader's place, so the detail on screen is
       as stale as any cached one once the file has been edited under it. */
    let summary = 'the first version'
    let reads = 0
    install({
      get: async (name: string) => {
        reads += 1
        return detail({ name, task_summary: summary })
      }
    })
    await mount()
    fireEvent.click(screen.getByText('issue-triage'))
    await act(async () => {})
    expect(store.getState().detail?.task_summary).toBe('the first version')
    store.closePage()
    summary = 'the second version'
    store.openPage()
    await act(async () => {
      await new Promise(r => setTimeout(r, 0))
    })
    expect(store.getState().openName).toBe('issue-triage')
    expect(store.getState().detail?.task_summary).toBe('the second version')
    expect(reads).toBe(2)
  })

  it('keeps the reader tab and step across a refresh, and drops a step the edit removed', async () => {
    let nodes = [node({ id: 'a' }), node({ id: 'b', depends_on: ['a'] })]
    install({ get: async (name: string) => detail({ name, nodes }) })
    await mount()
    fireEvent.click(screen.getByText('issue-triage'))
    await act(async () => {})
    store.pick('b')
    store.showTab('contract')
    store.closePage()
    store.openPage()
    await act(async () => {
      await new Promise(r => setTimeout(r, 0))
    })
    expect(store.getState().tab).toBe('contract')
    expect(store.getState().pickedNode).toBe('b')

    /* And when the edit took that step away, fall back rather than point at a
       node the graph no longer has. */
    nodes = [node({ id: 'a' })]
    store.closePage()
    store.openPage()
    await act(async () => {
      await new Promise(r => setTimeout(r, 0))
    })
    expect(store.getState().tab).toBe('contract')
    expect(store.getState().pickedNode).toBe('a')
  })

  it('opens the graph with a step already picked', async () => {
    install()
    await mount()
    fireEvent.click(screen.getByText('issue-triage'))
    await act(async () => {})
    expect(document.querySelectorAll('.pbnode')).toHaveLength(2)
    /* The first start node, so the panel beside the canvas is already pointing
       somewhere rather than asking the reader to aim first. */
    expect(document.querySelector('.pbnode[data-picked] .l1')?.textContent).toBe('a')
    /* A box carries the step's id and its executor. What the step says is prose
       of unknown length, which in a fixed box could only be a clipped fragment,
       so it is the panel's to show. */
    expect(document.querySelector('.pbnode[data-picked] .ag')?.textContent).toBe('Raven')
    expect(document.querySelector('.pbpanel .pbphead p')?.textContent).toBe('do the thing')
    expect([...document.querySelectorAll('.pbpanel .pbkv dt')].map(d => d.textContent)).not.toContain('id')
  })

  it('shows the clicked step, and tells the three states of skills apart', async () => {
    install({
      get: async (name: string) =>
        detail({
          name,
          nodes: [
            node({ id: 'a', skills: ['web-research'] }),
            node({ id: 'b', depends_on: ['a'], skills: [] }),
            node({ id: 'c', depends_on: ['b'] })
          ]
        })
    })
    await mount()
    fireEvent.click(screen.getByText('issue-triage'))
    await act(async () => {})

    const panel = (): string => document.querySelector('.pbpanel')?.textContent || ''
    expect(panel()).toContain('web-research')

    const nodes = document.querySelectorAll('.pbnode')
    fireEvent.click(nodes[1] as Element)
    /* Written as an empty list is a real instruction, and not the same as never
       written -- a panel that collapsed them would call a deliberate line
       missing. */
    expect(panel()).toContain('gui.pb.empty_list')
    fireEvent.click(nodes[2] as Element)
    expect(panel()).toContain('gui.pb.unwritten')
  })

  it('leaves dependsOn and instance to the drawing, and clicks a step to move', async () => {
    install({
      get: async (name: string) =>
        detail({
          name,
          nodes: [
            node({ id: 'a', instance: 'writer', node_summary: 'draft it' }),
            node({ id: 'b', depends_on: ['a'], instance: 'writer', node_summary: 'check it' })
          ]
        })
    })
    await mount()
    fireEvent.click(screen.getByText('issue-triage'))
    await act(async () => {})
    const panel = (): string => document.querySelector('.pbpanel')?.textContent || ''

    /* The arrows already say what waits for what, and a shared session is drawn
       as the lane around its members with the handle on every box inside it.
       Repeating either in the panel is a worse copy of a picture the reader is
       already looking at. */
    expect(panel()).not.toContain('dependsOn')
    expect(panel()).not.toContain('instance')
    expect(document.querySelectorAll('.pblane')).toHaveLength(1)
    expect(document.querySelector('.pbnode .hd')?.textContent).toBe('@writer')

    /* Which makes the drawing the way to move between steps. */
    expect(document.querySelector('.pbpanel .pbphead b')?.textContent).toBe('a')
    fireEvent.click(document.querySelectorAll('.pbnode')[1] as Element)
    expect(document.querySelector('.pbpanel .pbphead b')?.textContent).toBe('b')
  })

  it('names which form an input takes, since a path and a node id read alike', async () => {
    install({
      get: async (name: string) =>
        detail({
          name,
          nodes: [
            node({ id: 'a' }),
            node({
              id: 'b',
              depends_on: ['a'],
              inputs: { notes: { file: 'docs/NOTES.md' }, upstream: { node: 'a' }, mode: 'strict' }
            })
          ]
        })
    })
    await mount()
    fireEvent.click(screen.getByText('issue-triage'))
    await act(async () => {})
    fireEvent.click(document.querySelectorAll('.pbnode')[1] as Element)
    const rows = [...document.querySelectorAll('.pbin > div')].map(r => [
      r.querySelector('dt')?.textContent,
      r.querySelector('.kind')?.textContent ?? null,
      r.querySelector('.val')?.textContent
    ])
    expect(rows).toEqual([
      ['notes', 'file', 'docs/NOTES.md'],
      ['upstream', 'node', 'a'],
      /* A literal is a literal and gets no form word, because there is no other
         form it could be confused with. */
      ['mode', null, 'strict']
    ])
  })

  it('draws no graph for a playbook that stores none', async () => {
    install({
      get: async (name: string) => detail({ name, mode: 'prompt', nodes: [], prompts: 'compose three layers' })
    })
    await mount()
    fireEvent.click(screen.getByText('issue-triage'))
    await act(async () => {})
    expect(document.querySelector('.pbcanvas')).toBeNull()
    expect(screen.getByText('compose three layers')).toBeTruthy()
    /* The guidance takes the whole width: a panel beside it, saying the graph is
       composed per run, was a note about the absence of a panel. */
    expect(document.querySelector('.pbwork.solo')).not.toBeNull()
    expect(document.querySelector('.pbpanel')).toBeNull()
    /* And the tab does not name a picture that is not there. */
    expect(document.querySelector('.pbtab')?.textContent).toBe('gui.pb.tab_assembly')
  })

  it('shows a step the author left blank as blank, not as missing', async () => {
    install({
      get: async (name: string) =>
        detail({ name, nodes: [node({ id: 'angle', subagent: '', node_summary: '', prompt_template: '' })] })
    })
    await mount()
    fireEvent.click(screen.getByText('issue-triage'))
    await act(async () => {})
    const box = document.querySelector('.pbnode')
    expect(box?.classList.contains('blank')).toBe(true)
    expect(box?.querySelector('.l1')?.textContent).toBe('angle')
    /* A summary the author left for the caller to fill is simply absent from the
       panel -- no heading of its own, and no line saying it is missing. */
    expect(document.querySelector('.pbpanel .pbphead p')).toBeNull()
    expect(document.querySelector('.pbpanel')?.textContent).toContain('gui.pb.blank_prompt')
  })

  it('searches name and description together', async () => {
    install({
      list: async () => [
        row({ name: 'issue-triage' }),
        row({ name: 'release-notes', description: 'what changed in a version' })
      ]
    })
    await mount()
    fireEvent.change(document.querySelector('.cfind input') as Element, { target: { value: 'changed' } })
    expect(screen.queryByText('issue-triage')).toBeNull()
    expect(screen.getByText('release-notes')).toBeTruthy()
  })

  /* happy-dom reports every element as zero-width, so the viewport size has to
     be supplied; without it the board has nothing to frame the graph against. */
  function withPort(w: number, h: number): () => void {
    const own = {
      w: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientWidth'),
      h: Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'clientHeight')
    }
    Object.defineProperty(HTMLElement.prototype, 'clientWidth', { configurable: true, get: () => w })
    Object.defineProperty(HTMLElement.prototype, 'clientHeight', { configurable: true, get: () => h })
    return () => {
      if (own.w) Object.defineProperty(HTMLElement.prototype, 'clientWidth', own.w)
      if (own.h) Object.defineProperty(HTMLElement.prototype, 'clientHeight', own.h)
    }
  }

  /* happy-dom's WheelEvent drops ctrlKey, so a zoom gesture has to be built by
     hand -- fireEvent.wheel({ctrlKey: true}) silently arrives without it, and a
     test written that way passes while asserting nothing. */
  function wheel(el: Element, deltaY: number, ctrl: boolean): WheelEvent {
    const ev = new WheelEvent('wheel', { deltaY, bubbles: true, cancelable: true })
    Object.defineProperty(ev, 'ctrlKey', { value: ctrl })
    /* A raw dispatch is outside fireEvent's act(), so React never flushes the
       state it set and the assertion reads the pre-gesture zoom. */
    act(() => {
      el.dispatchEvent(ev)
    })
    return ev
  }

  const zoomOf = (): number => {
    const m = /scale\(([\d.]+)\)/.exec((document.querySelector('.pbview') as HTMLElement).style.transform)
    return m ? Number(m[1]) : NaN
  }

  async function openGraph(nodes: PlaybookNode[]): Promise<void> {
    install({ get: async (name: string) => detail({ name, nodes }) })
    await mount()
    fireEvent.click(screen.getByText('issue-triage'))
    await act(async () => {})
  }

  const chain = (n: number): PlaybookNode[] =>
    Array.from({ length: n }, (_, i) =>
      node({ id: 's' + i, depends_on: i ? ['s' + (i - 1)] : [], node_summary: 'step ' + i })
    )

  it('opens with the whole graph in view', async () => {
    const restore = withPort(700, 400)
    try {
      await openGraph(chain(6))
      /* Six steps do not fit at life size, so the opening view is the fit: a box
         is an id and an agent name, which stay readable far enough out that the
         whole graph can be the first thing a reader sees. */
      const z = zoomOf()
      expect(z).toBeGreaterThan(0.3)
      expect(z).toBeLessThan(1)
      /* ROOMY: five gaps of GAP_X, one box of W, and PAD on both sides. */
      const width = 5 * 256 + 200 + 32
      expect(z).toBeCloseTo(700 / width, 2)
    } finally {
      restore()
    }
  })

  it('opens a graph too big even for the zoom floor at its start, not centred', async () => {
    const restore = withPort(300, 400)
    try {
      /* Twenty steps cannot fit at the floor. Centring what does not fit cuts
         off the first step and the last one at once, which reads as damage. */
      await openGraph(chain(20))
      expect(zoomOf()).toBe(0.3)
      const pan = /translate\((-?[\d.]+)px,/.exec((document.querySelector('.pbview') as HTMLElement).style.transform)
      expect(Number(pan?.[1])).toBe(14)
    } finally {
      restore()
    }
  })

  it('leaves a small graph at life size', async () => {
    const restore = withPort(1400, 600)
    try {
      await openGraph(chain(2))
      expect(zoomOf()).toBe(1)
      expect(document.querySelector('.pbview')?.classList.contains('lean')).toBe(false)
    } finally {
      restore()
    }
  })

  it('zooms from the control, and stops at the limits', async () => {
    const restore = withPort(1400, 600)
    try {
      await openGraph(chain(2))
      const inBtn = screen.getByLabelText('gui.pb.zoom_in')
      const outBtn = screen.getByLabelText('gui.pb.zoom_out')
      fireEvent.click(inBtn)
      expect(zoomOf()).toBeCloseTo(1.15, 5)
      /* The readout is the control that puts the whole graph back. */
      expect(document.querySelector('.pbzpct')?.textContent).toBe('115%')
      for (let i = 0; i < 12; i++) fireEvent.click(inBtn)
      expect(zoomOf()).toBe(2)
      for (let i = 0; i < 30; i++) fireEvent.click(outBtn)
      expect(zoomOf()).toBeCloseTo(0.3, 5)
      fireEvent.click(document.querySelector('.pbzpct') as Element)
      expect(zoomOf()).toBe(1)
    } finally {
      restore()
    }
  })

  it('keeps the browser out of a ctrl+wheel, and tells a trackpad from a mouse', async () => {
    const restore = withPort(1400, 600)
    try {
      await openGraph(chain(2))
      const stage = document.querySelector('.pbstage') as HTMLElement
      const fit = (): void => void fireEvent.click(document.querySelector('.pbzpct') as Element)

      /* ctrl+wheel is the browser's own page-zoom gesture. It has to be
         cancelled, or the whole page grows while this canvas zooms the other
         way -- and React's own onWheel prop cannot cancel it, because React
         registers wheel passively and preventDefault is a no-op there. */
      const gesture = wheel(stage, -4, true)
      expect(gesture.defaultPrevented).toBe(true)
      /* A plain wheel is the page's to scroll, and must not be cancelled. */
      expect(wheel(stage, -400, false).defaultPrevented).toBe(false)

      /* One trackpad event is a small fraction of a zoom, so a flick glides. */
      fit()
      wheel(stage, -4, true)
      const nudge = zoomOf()
      expect(nudge).toBeGreaterThan(1.02)
      expect(nudge).toBeLessThan(1.06)

      /* A flick is a burst: several events land before React re-renders, so each
         has to compose on the last. Reading the zoom from the render's closure
         makes ten of them land as one, which is a canvas that ignores the
         gesture until it stops. */
      fit()
      act(() => {
        for (let i = 0; i < 10; i++) {
          const ev = new WheelEvent('wheel', { deltaY: -4, bubbles: true, cancelable: true })
          Object.defineProperty(ev, 'ctrlKey', { value: true })
          stage.dispatchEvent(ev)
        }
      })
      expect(zoomOf()).toBeGreaterThan(1 + (nudge - 1) * 8)

      /* A mouse notch is one press, not a distance: it gets exactly the step the
         button gives, so the two controls do not disagree about what a notch is
         worth -- and a runaway delta cannot outrun it. */
      fit()
      wheel(stage, -120, true)
      expect(zoomOf()).toBeCloseTo(1.15, 5)
      fit()
      wheel(stage, -4000, true)
      expect(zoomOf()).toBeCloseTo(1.15, 5)
    } finally {
      restore()
    }
  })

  it('pans on a drag, and a drag is not a click on the step underneath', async () => {
    const restore = withPort(1400, 600)
    try {
      await openGraph(chain(3))
      const stage = document.querySelector('.pbstage') as HTMLElement
      stage.setPointerCapture = () => {}
      const panOf = (): [number, number] => {
        const m = /translate\((-?[\d.]+)px, (-?[\d.]+)px\)/.exec(
          (document.querySelector('.pbview') as HTMLElement).style.transform
        )
        return m ? [Number(m[1]), Number(m[2])] : [NaN, NaN]
      }
      const before = panOf()
      fireEvent.pointerDown(stage, { pointerId: 1, clientX: 100, clientY: 100 })
      fireEvent.pointerMove(stage, { pointerId: 1, clientX: 160, clientY: 130 })
      fireEvent.pointerUp(stage, { pointerId: 1, clientX: 160, clientY: 130 })
      /* The pan is a delta on wherever the reader already was, not a jump to the
         pointer -- a canvas that snapped its content to the cursor would throw
         away the view they had. */
      expect(panOf()).toEqual([before[0] + 60, before[1] + 30])
      /* Zoom is untouched by a pan: the two gestures must not bleed. */
      expect(zoomOf()).toBe(1)
      /* A press that starts on a step is that button's press, not the canvas's:
         capturing the pointer here would take the pointerup away from the node
         and the reader's click on a step would go nowhere. */
      const held = panOf()
      const step = document.querySelectorAll('.pbnode')[2] as Element
      fireEvent.pointerDown(step, { pointerId: 2, clientX: 300, clientY: 200 })
      fireEvent.pointerMove(stage, { pointerId: 2, clientX: 420, clientY: 260 })
      expect(panOf()).toEqual(held)
      fireEvent.pointerUp(stage, { pointerId: 2, clientX: 420, clientY: 260 })
      /* And it still picks that step. */
      fireEvent.click(step)
      expect(document.querySelector('.pbnode[data-picked] .l1')?.textContent).toBe('s2')

      /* A press on the canvas that barely moved is a click on the canvas, not a
         pan: a one-pixel tremor must not shift the view under the reader. */
      const still = panOf()
      fireEvent.pointerDown(stage, { pointerId: 3, clientX: 500, clientY: 300 })
      fireEvent.pointerMove(stage, { pointerId: 3, clientX: 501, clientY: 301 })
      expect(panOf()).toEqual(still)
      fireEvent.pointerUp(stage, { pointerId: 3, clientX: 501, clientY: 301 })
    } finally {
      restore()
    }
  })

  it('says nothing about the metric it drew with or the scale it chose', async () => {
    const restore = withPort(700, 400)
    try {
      await openGraph(chain(6))
      const text = (document.querySelector('.pbboard') as HTMLElement).textContent || ''
      /* The board carries a zoom control, not a sentence explaining itself. */
      expect(text).not.toMatch(/gui\.pb\.(compact|fit)/)
      expect(document.querySelector('.pbpanel')?.textContent).not.toMatch(/gui\.pb\.blank_summary/)
    } finally {
      restore()
    }
  })

  it('does not call a failed read an empty library', async () => {
    install({
      list: async () => {
        throw new Error('not connected')
      }
    })
    await mount()
    expect(screen.getByText('not connected')).toBeTruthy()
    /* Both at once would read as "the read failed, and by the way you have no
       playbooks" -- and only one of those is something this page knows. */
    expect(screen.queryByText('gui.pb.none')).toBeNull()
    expect(screen.queryByText(/gui\.pb\.count/)).toBeNull()
  })

  it('opens through the shell page registry, so the rail lights up', () => {
    install()
    store.openPage()
    expect(pages).toEqual(['pbPage'])
    store.closePage()
    expect(pages).toEqual(['pbPage', null])
  })
})

describe('the graph-level fields', () => {
  async function open(over: Partial<PlaybookDetail> = {}): Promise<void> {
    install({ get: async (name: string) => detail({ name, ...over }) })
    await mount()
    fireEvent.click(screen.getByText('issue-triage'))
    await act(async () => {})
  }
  const tab = (label: string): HTMLElement =>
    [...document.querySelectorAll('.pbtab')].find(b => b.textContent === label) as HTMLElement

  it('opens on the graph and keeps the contract one click away', async () => {
    await open()
    expect(tab('gui.pb.tab_graph').getAttribute('aria-selected')).toBe('true')
    expect(document.querySelector('.pbwork.gone')).toBeNull()
    expect(document.querySelector('.pbsec')).toBeNull()

    fireEvent.click(tab('gui.pb.tab_contract'))
    expect(tab('gui.pb.tab_contract').getAttribute('aria-selected')).toBe('true')
    expect(document.querySelectorAll('.pbsec').length).toBe(2)
    /* Hidden, not unmounted: the board still holds whatever the reader panned
       and zoomed it to, and coming back must not reset that. */
    expect(document.querySelector('.pbwork.gone')).not.toBeNull()
    expect(document.querySelectorAll('.pbnode')).toHaveLength(2)
  })

  it('puts another playbook back on its own graph', async () => {
    install({
      list: async () => [row({ name: 'issue-triage' }), row({ name: 'release-notes' })],
      get: async (name: string) => detail({ name })
    })
    await mount()
    fireEvent.click(screen.getByText('issue-triage'))
    await act(async () => {})
    fireEvent.click(tab('gui.pb.tab_contract'))
    fireEvent.click(document.querySelector('.pbcrumb button') as Element)
    fireEvent.click(screen.getByText('release-notes'))
    await act(async () => {})
    /* A tab the reader chose for one playbook is not a choice about the next. */
    expect(tab('gui.pb.tab_graph').getAttribute('aria-selected')).toBe('true')

    /* And opening carries that on its own, not only because going back did it:
       one of the two resets alone would leave the other path unguarded. */
    fireEvent.click(tab('gui.pb.tab_contract'))
    await act(async () => {
      await store.open('issue-triage')
    })
    expect(tab('gui.pb.tab_graph').getAttribute('aria-selected')).toBe('true')
  })

  it('reports the spec version the file declares', async () => {
    await open({ version: 3 })
    expect(screen.getByText('v3')).toBeTruthy()
  })

  it('writes out both states of confirm', async () => {
    const confirmCell = (): string | undefined =>
      [...document.querySelectorAll('.pbmeta div')]
        .find(d => d.querySelector('.pblab')?.textContent === 'gui.pb.f_confirm')
        ?.querySelector('.pbval')?.textContent
    await open({ confirm: true })
    expect(confirmCell()).toBe('true')

    cleanup()
    store._resetForTests()
    /* Rendering nothing for `false` -- which is what a lone mark for the true
       case does -- leaves a reader unable to tell "does not ask" from "the page
       did not say". */
    await open({ confirm: false })
    expect(confirmCell()).toBe('false')
  })

  it('tells a missing default apart from one that is the empty string', async () => {
    await open({
      params: {
        from_ref: { type: 'string', required: true, description: 'where to start' },
        style: { type: 'path', required: false, default: '', description: 'house style file' },
        depth: {
          type: 'enum',
          required: false,
          default: 'standard',
          enum: ['quick', 'standard'],
          description: 'how far'
        }
      }
    })
    fireEvent.click(tab('gui.pb.tab_contract'))
    const rows = [...document.querySelectorAll('.pbtbl tbody tr')]
    expect(rows).toHaveLength(3)
    /* No default at all versus a default that IS the empty string: two facts,
       and one dash for both would lose the second. */
    expect(rows[0]!.querySelector('.gap')).not.toBeNull()
    expect(rows[1]!.querySelector('.df')?.textContent).toBe('""')
    expect(rows[2]!.querySelector('.df')?.textContent).toBe('standard')
    /* required rides on the name, so the column is not six rows of "no". */
    expect(rows[0]!.querySelector('.nm .req')?.textContent).toBe('gui.pb.required')
    expect(rows[1]!.querySelector('.req')).toBeNull()
    expect(rows[2]!.querySelector('.en')?.textContent).toBe('quick \u00b7 standard')
  })

  it('says a playbook takes no inputs rather than showing an empty table', async () => {
    await open({ params: {} })
    fireEvent.click(tab('gui.pb.tab_contract'))
    expect(screen.getByText('gui.pb.no_params')).toBeTruthy()
    expect(document.querySelector('.pbtbl')).toBeNull()
  })

  it('carries description under the title and nowhere else', async () => {
    await open()
    expect(document.querySelector('.pbsum')?.textContent).toBe('what it is for')
    fireEvent.click(tab('gui.pb.tab_contract'))
    /* Two sections, not three: description is the line above, and repeating it
       under a heading of its own said the same thing twice. */
    expect([...document.querySelectorAll('.pbsec h2')].map(h => h.textContent)).toEqual([
      'gui.pb.sec_keywords',
      'gui.pb.sec_params'
    ])
    expect(document.body.textContent?.split('what it is for').length).toBe(2)
  })
})
