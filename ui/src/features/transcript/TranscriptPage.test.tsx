// @vitest-environment happy-dom
import { act } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

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
    tlPush: () => {},
    rawPush: () => {},
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

  it('builds a playbook load a chip per node from the run that started', () => {
    /* A dag call the model makes carries `nodes`, so the chips come from the
       arguments. The load of a `mode: dag` playbook carries `{name, params}`
       and the graph exists only once the engine assembled it -- which is the
       run-started payload. Without reading it the card took the run id and then
       dropped every node update, because setChip only updates chips that are
       already there. */
    act(() => {
      const st = mount.step()
      st.tool('load_playbook', { name: 'topic-briefing', params: { topic: 'crows' } })
      mount.dagFeed('dag.run_started', {
        run_id: 'r1',
        nodes: [
          { id: 'tb-scan', subagent: 'research-raven' },
          { id: 'tb-brief', subagent: 'content-raven' },
        ],
      })
      mount.dagFeed('dag.node_updated', { run_id: 'r1', node: 'tb-scan', status: 'completed' })
    })
    const chips = $$('.nds .nd')
    expect(chips).toHaveLength(2)
    expect(chips.map((c) => c.textContent).join(' ')).toContain('tb-scan')
    /* The label the load produced survives: overwriting it in the dag branch
       left the row unable to say which playbook was loaded. */
    expect($('.wk')?.textContent).toContain('topic-briefing')
  })

  it('rebuilds a playbook load chips from disk when it saw no events', async () => {
    /* The restore path, which is what the receipt's `DAG <run>:` lead exists
       for: a card replayed from history sees no `run_started` at all, so the
       nodes can only come from `dag.get`. Its rows went through the same
       update-only `setChip`, so a load card -- whose arguments never carried a
       graph -- kept the run id and showed no strip. */
    wire({
      dagRows: async () => [
        { node: 'tb-scan', status: 'completed', subagent: 'research-raven' },
        { node: 'tb-brief', status: 'running', subagent: 'content-raven' },
      ],
    })
    await act(async () => {
      const st = mount.step()
      st.tool('load_playbook', { name: 'topic-briefing' })
        .done(true, "DAG r9: started 'topic-briefing' (2 steps); results will be delivered when the run completes.", 5)
      st.seal()
    })
    await act(async () => { await Promise.resolve() })

    const chips = $$('.nds .nd')
    expect(chips).toHaveLength(2)
    expect(chips.map((c) => c.getAttribute('data-st'))).toEqual(['completed', 'running'])
  })
})

