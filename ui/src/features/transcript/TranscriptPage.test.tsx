// @vitest-environment happy-dom
import { act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { CARD as dagCARD } from '../dag/graph'
import * as mount from './mount'
import * as store from './store'

import type { Shell } from '../../shell/bridge'
import type { ProseTarget } from '../../shell/prose'
import type { TranscriptSource } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

/* The island runs against the same two seams production wires: a fake shell
   on window.RavenShell (T returns its key, prefixed by the current language
   so a flip is observable) and a source on window.DS.transcript. */
let lang = 'en'

/* The renders are counted on the REAL renderer, not a stub: the island imports
   it directly now, and a stub would also stop the output being the prose the
   segments actually show. The counter is hoisted because vi.mock is. */
const seen = vi.hoisted(() => ({ md: 0 }))
vi.mock('../../shell/prose', async (importOriginal) => {
  const real = await importOriginal<typeof import('../../shell/prose')>()
  return { ...real, md: (src: string) => { seen.md += 1; return real.md(src) } }
})

function wire(over: Partial<TranscriptSource> = {}): void {
  lang = 'en'
  seen.md = 0
  const fakeShell: Shell = {
    T: (key, vars) => `${lang}:${key}` + (vars ? ` ${JSON.stringify(vars)}` : ''),
    toast: () => {},
    menuAt: () => {},
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: () => {},
    dur: (ms) => `${Math.round(ms / 1000)}s`,
    down: () => {},
    attNotes: () => ['[attachments]'],
    attImage: () => undefined,
    copyToClip: () => {},
    hunkFromEdit: (o, n) => ({ rows: [['del', o], ['add', n]], add: 1, del: 1 }),
    hunkFromWrite: (c) => ({ rows: [['add', c]], add: 1, del: 0 }),
    hunkFromUnified: () => ({ rows: [], add: 0, del: 0 }),
  }
  window.RavenShell = fakeShell
  const source: TranscriptSource = {
    clean: (t) => String(t == null ? '' : t).trim(),
    okOf: (_n, p) => !/^\s*(error|traceback|failed)\b/i.test(p),
    ...over,
  }
  window.DS = {
    transcript: source,
    workspace: { shortPath: (p: string) => p },
    /* The renderer reads this for what counts as an openable path. */
    prose: { pathOf: () => null, linkTargetOf: () => null },
  }
  document.body.innerHTML = '<div id="scroll"><div class="col" id="stage"></div></div>'
}

const $ = <T extends Element = HTMLElement>(sel: string): T | null => document.querySelector<T>(sel)
const $$ = (sel: string): Element[] => [...document.querySelectorAll(sel)]

beforeEach(() => {
  store._resetForTests()
  wire()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

const iso = (ms: number): string => new Date(ms).toISOString()

describe('transcript island, history', () => {
  it('reads a stored turn as segments: ask, folded step with thought and call, answer', () => {
    const t0 = Date.now() - 60000
    act(() => {
      mount.history([
        { role: 'user', text: 'check the login timeout', timestamp: iso(t0) },
        {
          role: 'assistant', reasoning_content: 'read the log first', reasoning_ms: 2000, text: '',
          tool_calls: [{ id: 'c1', name: 'read_file', arguments: '{"path":"/tmp/a.log"}' }],
        },
        { role: 'tool', tool_call_id: 'c1', name: 'read_file', text: 'line1\nline2' },
        { role: 'assistant', text: 'the config was wrong', timestamp: iso(t0 + 3000) },
      ])
    })
    const ask = $('.ask')
    expect(ask?.querySelector('.b')?.textContent).toBe('check the login timeout')
    expect(ask?.querySelector('.ansfoot .turnmeta')?.textContent).toBeTruthy()
    /* Everything that led to the answer folded behind one line with the
       question-to-answer span on it. */
    const fold = $('.tfold')
    expect(fold).toBeTruthy()
    expect(fold?.querySelector('.tfh .lb')?.textContent).toBe('en:gui.fold.done')
    expect(fold?.querySelector('.tfh .tm')?.textContent).toBe('3s')
    const step = fold?.querySelector('.tfb .step')
    expect(step).toBeTruthy()
    expect((step?.querySelector('.think') as HTMLElement).hidden).toBe(false)
    expect(step?.querySelector('.think .tm')?.textContent).toBe('2s')
    expect(step?.querySelector('.cot')?.textContent).toBe('read the log first')
    /* One call, so no summary line: the row stands on its own. */
    expect((step?.querySelector('.wk > .wrow') as HTMLElement).hidden).toBe(true)
    const row = step?.querySelector('.wkin .wrow')
    expect(row?.querySelector('.vb')?.textContent).toBe('en:gui.act.v.read_file')
    expect($('.answer .prose')?.textContent).toBe('the config was wrong')
  })

  it('keeps a redirection whole: an ACP shell title stays one row, one command', () => {
    act(() => {
      mount.history([
        { role: 'user', text: 'run the suite', timestamp: iso(Date.now() - 5000) },
        {
          role: 'assistant', text: '',
          tool_calls: [{ id: 'c1', name: 'terminal: pytest -q 2>&1 | tee out.log', arguments: '{}' }],
        },
        { role: 'tool', tool_call_id: 'c1', name: 'terminal: pytest -q 2>&1 | tee out.log', text: 'ok' },
        { role: 'assistant', text: 'done', timestamp: iso(Date.now()) },
      ])
    })
    const rows = $$('.wkin .wrow')
    expect(rows).toHaveLength(1)
    expect(rows[0]?.querySelector('.vb')?.textContent).toBe('en:gui.act.v.terminal')
    /* The command, redirection included, titles the detail card whole. */
    expect($('.wkin .dtl .dhd .nm')?.textContent).toBe('pytest -q 2>&1 | tee out.log')
  })

  /* The whole bug: the runtime's closing marker carries text for the MODEL to
     read on the next turn, and counting it as model prose meant the turn's
     real last words were no longer the last -- so they were drawn as
     narration and the fold closed over them, under a note promising the
     output above was kept. */
  it("keeps a stopped turn's half-written answer where the reader watched it land", () => {
    const t0 = Date.now() - 20000
    act(() => {
      mount.history([
        { role: 'user', text: 'compare the three', timestamp: iso(t0) },
        { role: 'assistant', text: 'I got as far as the first two:', timestamp: iso(t0 + 4000) },
        {
          role: 'assistant', text: '(turn cancelled by the user)',
          turn_ended: { status: 'cancelled' }, timestamp: iso(t0 + 5000),
        },
      ])
    })
    expect($('.answer .prose')?.textContent).toBe('I got as far as the first two:')
    /* Not behind the fold, and the marker's own text is never drawn as prose. */
    expect($('.tfold')).toBeNull()
    expect(document.body.textContent).not.toContain('cancelled by the user')
    const notes = $$('.tnote')
    expect(notes).toHaveLength(1)
    expect(notes[0]?.textContent).toContain('en:gui.halted')
  })

  it('keeps the answer when a runtime notice is what follows it', () => {
    const t0 = Date.now() - 9000
    act(() => {
      mount.history([
        { role: 'user', text: 'save that', timestamp: iso(t0) },
        { role: 'assistant', text: 'saved it under notes/', timestamp: iso(t0 + 2000) },
        /* A notice entry carries its prose as `text` because the model reads
           it on the next turn -- that is what made it look like model prose
           and pushed the answer above it into the fold. */
        {
          role: 'assistant', text: 'I stopped short of that one.',
          notice: { kind: 'action_blocked', detail: 'needs approval' }, timestamp: iso(t0 + 3000),
        },
      ])
    })
    expect($('.answer .prose')?.textContent).toBe('saved it under notes/')
    expect($('.tfold')).toBeNull()
    expect(document.body.textContent).not.toContain('I stopped short of that one.')
    expect($$('.tnote')).toHaveLength(1)
  })

  /* "The output above is kept" over a bare question is a promise about
     nothing, and the reader reads it as the output having been lost. */
  it('promises nothing was kept when a stop came before any output', () => {
    const t0 = Date.now() - 3000
    act(() => {
      mount.history([
        { role: 'user', text: 'never mind', timestamp: iso(t0) },
        {
          role: 'assistant', text: '(turn cancelled by the user)',
          turn_ended: { status: 'cancelled' }, timestamp: iso(t0 + 500),
        },
      ])
    })
    expect($('.answer')).toBeNull()
    /* The whole label, not a substring of it: 'gui.halted_bare' contains
       'gui.halted', so a containment check passes for the wrong key too. */
    expect($$('.tnote')[0]?.querySelector('.tx')?.textContent).toBe('en:gui.halted_bare')
  })

  /* A step boundary opens on every episode, and a notice clears the open step
     outright, so the prose of a turn stopped later is in an EARLIER step. The
     first version promoted only the open step, so live hid that prose while a
     reload of the same turn showed it -- the two halves disagreeing again, one
     case over. */
  it('promotes the last prose of the turn, not only the open step\'s', () => {
    act(() => { mount.ask('check the log') })
    let first!: ReturnType<typeof mount.step>
    let second!: ReturnType<typeof mount.step>
    let third!: ReturnType<typeof mount.step>
    act(() => {
      first = mount.step()
      first.setSay('let me check the log')
      first.tool('read_file', { path: '/tmp/a.log' }, null).done(true, 'line1', 12)
      first.seal()
      /* A second episode that also said something, then a third with nothing:
         the LAST prose of the turn is the answer, not the first. */
      second = mount.step()
      second.setSay('the pool is the problem')
      second.tool('read_file', { path: '/tmp/b.log' }, null).done(true, 'line2', 9)
      second.seal()
      third = mount.step()
    })
    act(() => { mount.finishTurn(third, [first, second, third], '4s') })
    expect($('.answer .prose')?.textContent).toBe('the pool is the problem')
    /* And the earlier prose stays where it was said, inside the fold. */
    expect($('.tfold')?.textContent).toContain('let me check the log')
    /* And it lands AFTER the work it introduced, as a finished turn does. */
    const order = [...document.querySelectorAll('.ask, .tfold, .answer')].map((n) => n.className.split(' ')[0])
    expect(order).toEqual(['ask', 'tfold', 'answer'])
    expect(mount.turnKept()).toBe(true)
  })

  /* The replayed half of the same shape. A stopped turn's last prose usually
     shares its message with the tool calls it introduced, and their results
     come after it in the payload -- so emitting the answer on sight put it
     ABOVE the work, the reverse of the live order. */
  it('replays an answer that shares its message with tool calls in live order', () => {
    const t0 = Date.now() - 20000
    act(() => {
      mount.history([
        { role: 'user', text: 'check the log', timestamp: iso(t0) },
        {
          role: 'assistant', text: 'let me check the log', timestamp: iso(t0 + 2000),
          tool_calls: [{ id: 'c1', name: 'read_file', arguments: '{"path":"/tmp/a.log"}' }],
        },
        { role: 'tool', tool_call_id: 'c1', name: 'read_file', text: 'line1' },
        {
          role: 'assistant', text: '(turn cancelled by the user)',
          turn_ended: { status: 'cancelled' }, timestamp: iso(t0 + 5000),
        },
      ])
    })
    expect($('.answer .prose')?.textContent).toBe('let me check the log')
    const order = [...document.querySelectorAll('.ask, .tfold, .answer, .tnote')]
      .map((n) => n.className.split(' ')[0])
    expect(order).toEqual(['ask', 'tfold', 'answer', 'tnote'])
    /* The work is inside the fold, not lost with it. */
    expect($('.tfold .tfb .wkin .wrow .vb')?.textContent).toBe('en:gui.act.v.read_file')
  })

  /* What the live stop path asks before it picks its label. */
  it('reports whether the turn put anything on the stage', () => {
    act(() => { mount.ask('do the thing') })
    expect(mount.turnKept()).toBe(false)
    let st!: ReturnType<typeof mount.step>
    act(() => { st = mount.step(); st.setSay('starting on it') })
    act(() => { mount.finishTurn(st, [st], '4s') })
    expect(mount.turnKept()).toBe(true)
    /* A note is the runtime talking, not the turn's output: it must not flip
       the answer back to true for the NEXT stop. */
    act(() => { mount.ask('and again') })
    act(() => { mount.note('stopped', '', { quiet: true }) })
    expect(mount.turnKept()).toBe(false)
  })

  it('draws a stored notice and a died turn as quiet and loud note rows', () => {
    act(() => {
      mount.history([
        { role: 'user', text: 'hello', timestamp: iso(Date.now() - 9000) },
        { role: 'assistant', notice: { kind: 'memory_flush', detail: 'deposited' }, timestamp: iso(Date.now() - 3000) },
      ])
      mount.note('send failed', 'socket closed', { quiet: false })
    })
    const notes = $$('.tnote')
    expect(notes).toHaveLength(2)
    expect(notes[0]?.classList.contains('bad')).toBe(false)
    expect(notes[0]?.textContent).toContain('en:gui.notice.memory_flush')
    expect(notes[1]?.classList.contains('bad')).toBe(true)
    expect((notes[1] as HTMLElement).title).toBe('send failed · socket closed')
  })
})

describe('transcript island, streaming', () => {
  it('appends 100 chunks with one paint: no render per token, earlier segments untouched', () => {
    const rafQ: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafQ.push(cb); return rafQ.length })
    vi.stubGlobal('cancelAnimationFrame', () => {})
    act(() => {
      mount.ask('stream me a story')
    })
    const askEl = $('.ask')
    let st!: ReturnType<typeof mount.step>
    act(() => { st = mount.step() })
    seen.md = 0
    act(() => {
      for (let i = 0; i < 100; i += 1) st.sayDelta(`word${i} `)
    })
    /* The store batched everything behind ONE frame callback and nothing
       rendered yet -- not the prose, not the list. */
    expect(rafQ).toHaveLength(1)
    expect(seen.md).toBe(0)
    expect($('.say')?.textContent).toBe('')
    act(() => { rafQ.forEach((cb) => cb(0)) })
    /* One flush, one render of the streaming leaf; the ask bubble above it
       was not remounted or re-rendered. */
    expect(seen.md).toBe(1)
    expect($('.say')?.textContent).toContain('word0 ')
    /* No trailing space: the real renderer trims the line it lays out, which a
       stub returning the raw source did not. */
    expect($('.say')?.textContent).toContain('word99')
    expect($('.ask')).toBe(askEl)
  })

  it('promotes the streamed prose into the answer block when the turn lands', () => {
    const rafQ: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => { rafQ.push(cb); return rafQ.length })
    vi.stubGlobal('cancelAnimationFrame', () => {})
    let st!: ReturnType<typeof mount.step>
    act(() => {
      mount.ask('question')
      st = mount.step()
      st.sayDelta('the whole answer')
    })
    act(() => { mount.finishTurn(st, [st], '4s') })
    /* The prose-only step gave way to the answer block where it stood. */
    expect($('.answer .prose')?.textContent).toBe('the whole answer')
    expect($$('.step')).toHaveLength(0)
    expect($('.answer .ansfoot .turnmeta')?.textContent).toBeTruthy()
  })
})

/* The three delegation verbs are optional, and the offline demo installs none
   of them -- it used to install null-guarded stand-ins in the page layer, which
   put the "what happens with no host behind this" decision in the wrong place.
   These pin the island's own answers, which are now the whole of that. */
describe('transcript island, the delegation verbs', () => {
  /* A dag call plus a second call, because the work summary row only exists for
     a stretch of more than one -- without it the card never opens and every
     assertion about its contents is inert. */
  function dagCard(): HTMLElement {
    act(() => {
      const st = mount.step()
      st.tool('run_subagent_dag', { nodes: [{ id: 'alpha' }, { id: 'beta' }] })
        .done(true, 'DAG run-7: 2 nodes done', 30)
      st.tool('run', { cmd: 'ls' }).done(true, 'ok', 5)
      st.seal()
    })
    act(() => { ($('.wk > .wrow.sum') as HTMLElement).click() })
    const row = $$('.wkin .wrow')[0] as HTMLElement
    act(() => { row.click() })
    return row.nextElementSibling as HTMLElement
  }

  const nodeStates = (card: HTMLElement): string[] =>
    [...card.querySelectorAll<HTMLElement>('.gnd')].map((n) => n.dataset.st!)

  const pickNode = (card: HTMLElement, i: number): void => {
    card.querySelectorAll('.gnd')[i]!.dispatchEvent(new MouseEvent('click', { bubbles: true }))
  }

  it('sends a spawn row to the agents panel when nothing else will take it', () => {
    const went: string[] = []
    wire()
    window.RavenShell!.showWorkspace = (tab) => went.push(tab)
    store.openSpawn('researcher', 'read the docs')
    expect(went).toEqual(['agents'])
  })

  /* A restored card recovers its node states through dagRows. The falsifiable
     half is the contrast: with a reader the nodes take the reported states,
     without one they stay as seeded. */
  it('hydrates a restored dag card from dagRows', async () => {
    wire({ dagRows: () => Promise.resolve([{ node: 'alpha', status: 'completed' }, { node: 'beta', status: 'failed' }]) })
    const card = dagCard()
    await act(async () => { await Promise.resolve() })
    expect(nodeStates(card)).toEqual(['completed', 'failed'])
  })

  it('leaves the nodes as seeded when no reader is installed', async () => {
    wire()
    const card = dagCard()
    await act(async () => { await Promise.resolve() })
    expect(nodeStates(card)).toEqual(['pending', 'pending'])
  })

  /* The node panel's own control, not the node: clicking a node selects it, so
     that the run's transcript is a separate intention from reading the request. */
  it('opens a node through the source, with the run id it recovered', () => {
    const opened: Array<[string, string]> = []
    wire({ openDagNode: (runId, nodeId) => opened.push([runId, nodeId]) })
    const card = dagCard()
    /* Dispatched rather than `.click()`: a node box is an SVG <g>, and
       SVGElement has no click() in jsdom. */
    act(() => { pickNode(card, 0) })
    act(() => { card.querySelector<HTMLElement>('.npanel .go')!.click() })
    expect(opened).toEqual([['run-7', 'alpha']])
  })

  /* Called directly, not through the chip: React swallows an exception thrown
     inside an event handler, so a click can never witness this. There is no
     matching case for an ABSENT opener -- optional chaining makes that
     unfalsifiable, and the case above already proves the call happens when a
     verb is there, which is the only observable difference. */
  it('survives a node opener that throws', () => {
    wire({ openDagNode: () => { throw new Error('no such run') } })
    expect(() => store.openDagNode('run-7', 'alpha')).not.toThrow()
  })
})

describe('transcript island, tool episodes', () => {
  function twoCalls(secondFails = false): void {
    act(() => {
      const st = mount.step()
      st.tool('read_file', { path: '/tmp/a.txt' }).done(true, 'aaa', 5)
      st.tool('read_file', { path: '/tmp/b.txt' }).done(!secondFails, secondFails ? 'error: nope' : 'bbb', 5)
      st.seal()
    })
  }

  it('folds a stretch behind one summary line and unfolds on click', () => {
    twoCalls()
    const sum = $('.wk > .wrow.sum') as HTMLElement
    expect(sum.hidden).toBe(false)
    expect(sum.querySelector('.ar')?.textContent).toBe('en:gui.act.n.read_file {"n":2}')
    const wkin = $('.wkin') as HTMLElement
    expect(wkin.hidden).toBe(true)
    act(() => { sum.click() })
    expect(($('.wkin') as HTMLElement).hidden).toBe(false)
    expect(sum.classList.contains('open')).toBe(true)
    act(() => { sum.click() })
    expect(($('.wkin') as HTMLElement).hidden).toBe(true)
  })

  it('opens a settled row into its detail card and closes it again', () => {
    twoCalls()
    act(() => { ($('.wk > .wrow.sum') as HTMLElement).click() })
    const row = $$('.wkin .wrow')[0] as HTMLElement
    expect(row.classList.contains('tog')).toBe(true)
    const dtl = row.nextElementSibling as HTMLElement
    expect(dtl.classList.contains('dtl')).toBe(true)
    expect(dtl.hidden).toBe(true)
    act(() => { row.click() })
    expect((row.nextElementSibling as HTMLElement).hidden).toBe(false)
    expect(row.classList.contains('open')).toBe(true)
    /* The card is titled with the target and carries the output. */
    expect(dtl.querySelector('.dhd .nm')?.textContent).toBe('/tmp/a.txt')
    expect(dtl.querySelector('.bd pre')?.textContent).toBe('aaa')
    act(() => { row.click() })
    expect((row.nextElementSibling as HTMLElement).hidden).toBe(true)
  })

  /* The chip's click is the island's own, and has to be: React's
     stopPropagation -- which the chip needs so the row underneath does not
     toggle -- stops the native event too, so shell/chips.ts never sees it.
     Without these two cases, dropping the openChip call would leave
     click-to-open dead with the whole suite still green. */
  describe('a path chip in tool output', () => {
    const wireProse = (open: (at: ProseTarget) => void): void => {
      window.DS = { ...window.DS, prose: { pathOf: () => null, linkTargetOf: () => null, open } }
    }

    function chip(): HTMLElement {
      act(() => {
        const st = mount.step()
        st.tool('run', { cmd: 'pytest' }).done(true, 'wrote /tmp/out.log just now', 5)
        st.tool('run', { cmd: 'ls' }).done(true, 'ok', 5)
        st.seal()
      })
      act(() => { ($('.wk > .wrow.sum') as HTMLElement).click() })
      const row = $$('.wkin .wrow')[0] as HTMLElement
      act(() => { row.click() })
      return (row.nextElementSibling as HTMLElement).querySelector<HTMLElement>('.bd pre .pth')!
    }

    it('opens the path it carries, through the prose seam', () => {
      const opened: Array<{ p: string; dir: boolean }> = []
      wireProse((at) => opened.push(at))
      const el = chip()
      expect(el.dataset.p).toBe('/tmp/out.log')
      act(() => { el.click() })
      expect(opened).toEqual([{ p: '/tmp/out.log', dir: false }])
    })

    it('never reaches a document-level listener, so the island must open it', () => {
      wireProse(() => {})
      /* Armed AFTER the chip is on screen: getting there takes two clicks of
         its own, and those do reach the document. */
      const el = chip()
      let atDocument = 0
      const spy = (): void => { atDocument += 1 }
      document.addEventListener('click', spy)
      act(() => { el.click() })
      document.removeEventListener('click', spy)
      expect(atDocument).toBe(0)
    })
  })

  it('shows a failure on the row, on the summary chip, and opens the fold unasked', () => {
    twoCalls(true)
    const sum = $('.wk > .wrow.sum') as HTMLElement
    /* A failure is the one thing worth opening unasked. */
    expect(($('.wkin') as HTMLElement).hidden).toBe(false)
    expect(sum.querySelector('.chip.bad')?.textContent).toBe('en:gui.n_failed {"n":1}')
    const bad = $$('.wkin .wrow')[1] as HTMLElement
    expect(bad.classList.contains('bad')).toBe(true)
    expect(bad.querySelector('.err')?.textContent).toBe('error: nope')
    expect(bad.querySelector('svg path')?.getAttribute('d')).toContain('M12 4.5')
  })

  it('merges consecutive silent steps into one stretch under one summary', () => {
    const handles: Array<ReturnType<typeof mount.step>> = []
    act(() => {
      for (let i = 0; i < 2; i += 1) {
        const st = mount.step()
        st.tool('exec', { command: `cmd${i}` }).done(true, 'out', 3)
        st.seal()
        handles.push(st)
      }
      mount.foldRuns(handles)
    })
    expect($$('.step')).toHaveLength(1)
    expect($$('.wkin .wrow')).toHaveLength(2)
    expect($('.wk > .wrow.sum .ar')?.textContent).toBe('en:gui.act.n.exec {"n":2}')
  })
})

describe('transcript island, language', () => {
  it('repaints every catalogue word in place on a flip', () => {
    twoTurns()
    expect($$('.wkin .wrow')[0]?.querySelector('.vb')?.textContent).toBe('en:gui.act.v.grep')
    expect($('.tfh .lb')?.textContent).toBe('en:gui.fold.done')
    lang = 'zh'
    act(() => { mount.redraw() })
    expect($$('.wkin .wrow')[0]?.querySelector('.vb')?.textContent).toBe('zh:gui.act.v.grep')
    expect($('.tfh .lb')?.textContent).toBe('zh:gui.fold.done')
  })

  function twoTurns(): void {
    act(() => {
      mount.ask('question')
      const st = mount.step()
      st.tool('grep', { pattern: 'x' }).done(true, 'hit', 2)
      st.tool('grep', { pattern: 'y' }).done(true, 'hit', 2)
      st.seal()
      mount.collapse('2s')
    })
  }
})

describe('transcript island, the agent stage', () => {
  it('paints a running record with the glyph, holds back a streaming answer, appends on poll', () => {
    const box = document.createElement('div')
    document.body.appendChild(box)
    const t0 = Date.now() - 9000
    act(() => {
      mount.agentStage(box, {
        status: 'run',
        messages: [
          { role: 'user', text: 'survey the repo', timestamp: iso(t0) },
          { role: 'assistant', text: 'half an ans' },
        ],
      }, { key: 'sp:a1', reset: true })
    })
    expect(box.querySelector('.ask .b')?.textContent).toBe('survey the repo')
    /* The streaming answer is held back; the glyph carries the motion. */
    expect(box.querySelector('.answer')).toBeNull()
    expect(box.querySelector('.sarun .wkg')).toBeTruthy()
    act(() => {
      mount.agentStage(box, {
        status: 'ok',
        messages: [
          { role: 'user', text: 'survey the repo', timestamp: iso(t0) },
          { role: 'assistant', text: 'the whole answer', timestamp: iso(t0 + 5000) },
        ],
      }, { key: 'sp:a1' })
    })
    expect(box.querySelector('.answer .prose')?.textContent).toBe('the whole answer')
    expect(box.querySelector('.sarun')).toBeNull()
  })

  it('starts a fresh lane in a box the subagents island already wrote into', () => {
    const box = document.createElement('div')
    document.body.appendChild(box)
    /* What failStage leaves behind when the first context fetch rejects: the
       box wiped and an error note in it, with the lane host gone. */
    box.innerHTML = '<div class="wsempty">rpc timeout</div>'
    act(() => {
      mount.agentStage(box, { status: 'run', messages: [{ role: 'user', text: 'retry ok' }] },
        { key: 'sp:a1', reset: true })
    })
    expect(box.querySelector('.ask .b')?.textContent).toBe('retry ok')
    expect(box.textContent).not.toContain('rpc timeout')
    expect($$('.wsempty').length).toBe(0)
  })

  it('words its own empty state', () => {
    const box = document.createElement('div')
    document.body.appendChild(box)
    act(() => {
      mount.agentStage(box, { status: 'ok', messages: [] }, { key: 'dag:r:n', empty: 'nothing ran', reset: true })
    })
    expect(box.querySelector('.wsempty')?.textContent).toBe('nothing ran')
  })
})

describe('transcript island, lane lifetime', () => {
  /* One answer's prose is one md() call per repaint, which is how a lane
     nobody can see any more announces itself. */
  const stage = (): HTMLElement => document.getElementById('stage')!
  const turn = (text: string): void => {
    act(() => {
      mount.ask('q')
      mount.answer(text)
    })
  }

  it('releases a lane whose host the shell wiped, and stops repainting it', () => {
    turn('first')
    const gone = stage().querySelector('[data-tsl]')!
    stage().innerHTML = ''
    turn('second')
    expect(gone.isConnected).toBe(false)
    seen.md = 0
    act(() => { mount.redraw() })
    expect(seen.md).toBe(1)
    expect(stage().textContent).toContain('second')
  })

  it('keeps a parked host: detached is not the same as thrown away', () => {
    turn('streaming')
    const parked = stage().querySelector('[data-tsl]')! as HTMLElement
    /* What live/060-parked.js does on a mid-turn session switch: the stage's
       children are held in a detached array, then wiped off the page. */
    ;(window.DS!.transcript as TranscriptSource).parked = (node) => node === parked
    stage().innerHTML = ''
    turn('the other session')
    seen.md = 0
    act(() => { mount.redraw() })
    expect(seen.md).toBe(2)
    /* And it comes back with its text, not as an empty shell. */
    act(() => { stage().appendChild(parked) })
    expect(stage().textContent).toContain('streaming')
  })
})

describe('transcript island, delegated calls', () => {

  it('names the agent a spawn ran on, under either argument spelling', () => {
    /* These arguments are the model's own, recorded with the call, so a
       conversation opened from history hands us both spellings for as long as
       those transcripts exist. Reading only the new one dropped the agent out
       of every delegated row: the label fell back to "raven" whichever agent
       had actually run. */
    act(() => {
      const st = mount.step()
      st.tool('spawn', { task: 'dig', subagent: 'research-raven' }).done(true, 'started', 5)
      st.tool('spawn', { task: 'dig', agent: 'code-raven', instance: 'refactor' }).done(true, 'started', 5)
      st.seal()
    })
    const rows = $$('.wk .wrow').map((el) => el.textContent || '')
    expect(rows.join(' | ')).toContain('research-raven')
    expect(rows.join(' | ')).toContain('code-raven @refactor')
    expect(rows.join(' | ')).not.toContain('gui.deleg.self')
  })

  /* Open the dag card, wherever the step put it: a sealed step of one call folds
     behind a summary row and an unsealed one does not, and this is about the card
     rather than about the fold. Found through its own detail block, whose row is
     the toggle in front of it. */
  const openDagCard = (): HTMLElement => {
    const sum = $('.wk > .wrow.sum') as HTMLElement | null
    if (sum) act(() => { sum.click() })
    const dtl = $('.dtl.dagc') as HTMLElement
    act(() => { (dtl.previousElementSibling as HTMLElement).click() })
    return dtl
  }

  it('draws a playbook load the same graph, from the run that started', () => {
    /* A dag call the model makes carries `nodes`, so the graph comes from the
       arguments. The load of a `mode: dag` playbook carries `{name, params}` and
       the graph exists only once the engine assembled it -- which is the
       run-started payload. Same card either way: which source the nodes came from
       is not something the reader should be able to see. */
    act(() => {
      const st = mount.step()
      st.tool('load_playbook', { name: 'topic-briefing', params: { topic: 'crows' } })
      mount.dagFeed('dag.run_started', {
        run_id: 'r1',
        nodes: [
          { id: 'tb-scan', subagent: 'scout', depends_on: [] },
          { id: 'tb-brief', subagent: 'writer', depends_on: ['tb-scan'] },
        ],
      })
      mount.dagFeed('dag.node_updated', { run_id: 'r1', node: 'tb-scan', status: 'completed' })
    })
    const card = openDagCard()
    const nodes = [...card.querySelectorAll<HTMLElement>('.gnd')]
    expect(nodes).toHaveLength(2)
    expect(nodes.map((n) => n.dataset.st)).toEqual(['completed', 'pending'])
    /* The dependency the event carried is drawn as an edge, which is the whole
       difference between a graph and a list. */
    expect(card.querySelectorAll('.edge')).toHaveLength(1)
    /* The label the load produced survives: overwriting it in the dag branch
       left the row unable to say which playbook was loaded. */
    expect($('.wk')?.textContent).toContain('topic-briefing')
  })

  it('draws the graph at the card dims rather than the sheet ones', () => {
    /* The card sits in a 744px reading column and the sheet has the chat's whole
       width; drawing at the sheet's geometry would run the graph past the card's
       edge, and nothing about the picture would look wrong enough to notice. */
    act(() => {
      const st = mount.step()
      st.tool('run_subagent_dag', {
        nodes: [{ id: 'scan', subagent: 'scout' }, { id: 'brief', subagent: 'writer', depends_on: ['scan'] }],
      })
    })
    const card = openDagCard()
    const svg = card.querySelector('.canvas svg') as SVGElement
    expect(svg.getAttribute('width')).toBe(String(dagCARD.PAD * 2 + dagCARD.GAP_X + dagCARD.W))
  })

  it('marks the node whose detail is open, including the failure it opened unasked', () => {
    /* A failure is the one thing worth opening unasked -- the same rule the
       step's own fold follows. Derived rather than stored, because the node states
       arrive after the card was built; so the graph has to be told which node the
       panel is showing rather than reading the stored selection, which is empty. */
    act(() => {
      const st = mount.step()
      st.tool('run_subagent_dag', {
        nodes: [{ id: 'scan', subagent: 'scout' }, { id: 'brief', subagent: 'writer', depends_on: ['scan'] }],
      })
      mount.dagFeed('dag.run_started', { run_id: 'r2', nodes: [{ id: 'scan' }, { id: 'brief' }] })
      mount.dagFeed('dag.node_updated', { run_id: 'r2', node: 'scan', status: 'completed' })
      mount.dagFeed('dag.node_updated', { run_id: 'r2', node: 'brief', status: 'failed' })
    })
    const card = openDagCard()
    expect(card.querySelector('.npanel .nm')!.textContent).toBe('brief')
    const marked = [...card.querySelectorAll<HTMLElement>('.gnd')].filter((g) => g.dataset.sel === '1')
    expect(marked).toHaveLength(1)
    expect(marked[0]!.querySelector('.id')!.textContent).toBe('brief')
  })

  it('keeps the failure reason on a run the nodes cannot explain', () => {
    /* `run_subagent_dag` can raise mid-run (a backend write failing, say) after
       some nodes have already completed. `okOf` calls a dag result bad only when
       its first word is error-shaped, so a bad state IS that case -- a run whose
       nodes merely failed returns a summary and reads as ok. Withholding the
       receipt whenever the graph had produced a tally therefore left exactly this
       card showing a node count and nothing about why it stopped. */
    act(() => {
      const st = mount.step()
      const h = st.tool('run_subagent_dag', {
        nodes: [{ id: 'scan', subagent: 'scout' }, { id: 'brief', subagent: 'writer', depends_on: ['scan'] }],
      })
      mount.dagFeed('dag.run_started', { run_id: 'r4', nodes: [{ id: 'scan' }, { id: 'brief' }] })
      mount.dagFeed('dag.node_updated', { run_id: 'r4', node: 'scan', status: 'completed' })
      h.done(false, 'Error running DAG r4: backend write failed: disk full', 40)
      st.seal()
    })
    const state = [...openDagCard().querySelectorAll('.dgr > .v')][1] as HTMLElement
    expect(state.textContent).toContain('disk full')
    /* The tally stays beside it: "1 done" is not the same fact as the cause, and
       it is the only word on what did get through before the run stopped. */
    expect(state.textContent).toContain('gui.deleg.dag_done')
  })

  it('closes the panel it opened unasked, and leaves it closed', () => {
    /* The unasked open is stored rather than derived. Derived, `null` meant both
       "nobody picked one" and "the reader closed it": the first click on the
       failed node was a no-op and the second reopened it, so the panel could
       never be shut -- on the one run where a reader most wants the graph back
       unobstructed. */
    act(() => {
      const st = mount.step()
      st.tool('run_subagent_dag', {
        nodes: [{ id: 'scan', subagent: 'scout' }, { id: 'brief', subagent: 'writer', depends_on: ['scan'] }],
      })
      mount.dagFeed('dag.run_started', { run_id: 'r5', nodes: [{ id: 'scan' }, { id: 'brief' }] })
      mount.dagFeed('dag.node_updated', { run_id: 'r5', node: 'brief', status: 'failed' })
    })
    const card = openDagCard()
    const at = (i: number): Element => card.querySelectorAll('.gnd')[i] as Element
    expect(card.querySelector('.npanel .nm')!.textContent).toBe('brief')

    act(() => { at(1).dispatchEvent(new MouseEvent('click', { bubbles: true })) })
    expect(card.querySelector('.npanel')).toBeNull()

    /* And a later failure does not reopen what the reader shut -- the unasked
       open happens once, like the step's own fold. */
    act(() => { mount.dagFeed('dag.node_updated', { run_id: 'r5', node: 'scan', status: 'failed' }) })
    expect(card.querySelector('.npanel')).toBeNull()

    /* Still a working toggle, so the reader can bring it back. */
    act(() => { at(0).dispatchEvent(new MouseEvent('click', { bubbles: true })) })
    expect(card.querySelector('.npanel .nm')!.textContent).toBe('scan')
  })

  it('gives a playbook load the same node detail a model-made call gets', async () => {
    /* The point of the read: a playbook's arguments never carried the graph, so
       without `dag.get` its card can show who ran and in what order and nothing
       about what any step was asked -- while the identical card for a model-made
       call shows all of it from its own arguments. */
    wire({
      dagRows: async () => [
        { node: 'tb-scan', status: 'completed', subagent: 'scout', depends_on: [] },
        {
          node: 'tb-brief',
          status: 'running',
          subagent: 'writer',
          depends_on: ['tb-scan'],
          prompt_template: 'write up {{ tb-scan.output }} in the house voice',
          inputs: { voice: { file: 'docs/voice.md' } },
        },
      ],
    })
    await act(async () => {
      const st = mount.step()
      st.tool('load_playbook', { name: 'topic-briefing' })
        .done(true, "DAG r9: started 'topic-briefing' (2 steps); results will be delivered when the run completes.", 5)
      st.seal()
    })
    const card = openDagCard()
    await act(async () => { await Promise.resolve(); await Promise.resolve() })
    const nodes = [...card.querySelectorAll<HTMLElement>('.gnd')]
    expect(nodes.map((n) => n.dataset.st)).toEqual(['completed', 'running'])
    act(() => { nodes[1]!.dispatchEvent(new MouseEvent('click', { bubbles: true })) })
    const panel = card.querySelector('.npanel') as HTMLElement
    expect(panel.querySelector('.dep')!.textContent).toBe('tb-scan')
    expect(panel.querySelector('.ins')!.textContent).toContain('docs/voice.md')
    expect(panel.querySelector('.tpl')!.textContent).toContain('in the house voice')
    /* The placeholder is marked up rather than left as text: it is the part that
       says where this step's material comes from. */
    expect(panel.querySelector('.tpl .ph')!.textContent).toBe('{{ tb-scan.output }}')
  })
})

