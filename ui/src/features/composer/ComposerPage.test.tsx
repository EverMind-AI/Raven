// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { AttTray, QueueList, SlashList, TurnLive } from './ComposerPage'
import * as store from './store'

import type { ComposerSource, SlashCmd } from './types'
import type { Shell } from '../../shell/bridge'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

interface Calls {
  sent: string[]
  halted: number
  notes: Array<[string, string]>
  toasts: string[]
  attImgs: Array<[string, string]>
  draftDropped: number
  stick: boolean
}

let lang = 'zh'

const WORDS: Record<string, Record<string, string>> = {
  zh: {
    'gui.send': '发送', 'gui.stop': '停止', 'gui.q.edit': '编辑', 'gui.q.remove': '删掉',
    'gui.att.uploading': '上传中', 'gui.att.remove': '移除 {name}', 'gui.att.fail': '{name} 上传失败',
    'gui.live.busy': '进行中 {t}', 'gui.pill.bottom': '回到底部',
    'gui.att.note': '我给你的文件：', 'gui.att.pending': '还有文件在上传',
    'gui.att.pending_body': '{n} 个文件还没传完，传完再发。',
  },
  en: {
    'gui.send': 'Send', 'gui.stop': 'Stop', 'gui.q.edit': 'Edit', 'gui.q.remove': 'Remove',
    'gui.att.uploading': 'uploading', 'gui.att.remove': 'remove {name}', 'gui.att.fail': '{name} failed',
    'gui.live.busy': 'running {t}', 'gui.pill.bottom': 'back to the bottom',
    'gui.att.note': 'Files for you:', 'gui.att.pending': 'a file is still uploading',
    'gui.att.pending_body': '{n} still going; send once they land.',
  },
}

const word = (key: string): string => (WORDS[lang] as Record<string, string>)[key] as string

/* The dock's markup, as page.html carries it. The island writes into these
   containers, so the test gives it the real ones rather than a stand-in. */
const DOCK = `
  <div class="chat">
    <div class="scroll" id="scroll"><div class="col" id="stage"></div></div>
    <button class="backpill" id="backpill" hidden></button>
    <div class="dock">
      <div class="sheets" id="sheetRack"></div>
      <div class="dock-in">
        <div class="queued" id="queued"></div>
        <div class="field"><textarea id="ta" rows="1"></textarea></div>
        <div class="under">
          <button class="tool-btn" id="attBtn"></button>
          <span class="meter" id="meter"></span>
          <button class="go" id="go" disabled></button>
        </div>
        <div class="pop slash" id="slashPop" data-open="false" role="listbox">
          <div id="slashList"></div>
        </div>
      </div>
    </div>
  </div>`

function wire(over: Partial<ComposerSource> = {}): { source: ComposerSource; calls: Calls } {
  const calls: Calls = {
    sent: [], halted: 0, notes: [], toasts: [], attImgs: [],
    draftDropped: 0, stick: true,
  }
  const fakeShell: Shell = {
    T: (key, vars) => {
      const raw = (WORDS[lang] as Record<string, string>)[key] ?? key
      return vars ? raw.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m)) : raw
    },
    toast: (text) => { calls.toasts.push(text) },
    menuAt: () => {},
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: () => {},
    dur: (ms) => `${Math.round(ms / 1000)}s`,
    noteRow: (label, detail) => { calls.notes.push([label, detail]) },
    stick: () => calls.stick,
    setStick: (on) => { calls.stick = on },
    draftTouch: () => {},
    draftDrop: () => { calls.draftDropped += 1 },
    draftPark: () => {},
    attImageSet: (p, url) => { calls.attImgs.push([p, url]) },
    slashName: (id) => id.replace(/^gui\./, ''),
    slashHelp: (id) => `help for ${id}`,
    down: () => {},
  }
  const source: ComposerSource = {
    busy: () => false,
    queue: () => [],
    meter: () => '',
    slash: [],
    /* The two actions live on the source now, not the shell. Spread last so a
       test can still override either one. */
    send: (text) => { calls.sent.push(text) },
    stop: () => { calls.halted += 1 },
    ...over,
  }
  window.RavenShell = fakeShell
  window.DS = { composer: source }
  document.body.innerHTML = DOCK
  return { source, calls }
}

/* FileReader lands on a macrotask, not a microtask: staging a file is two
   real turns of the loop before the chip carries its answer. */
const flush = async (): Promise<void> => {
  for (let i = 0; i < 4; i += 1) await new Promise((r) => setTimeout(r, 0))
}

const ta = (): HTMLTextAreaElement => document.getElementById('ta') as HTMLTextAreaElement
const go = (): HTMLButtonElement => document.getElementById('go') as HTMLButtonElement

/* Every root is mounted the way the page mounts it: over the container that is
   already in the markup. */
function mountQueue(): void {
  render(<QueueList />, { container: document.getElementById('queued')! })
}

function mountTray(): HTMLElement {
  const box = document.createElement('div')
  box.className = 'atts'
  box.id = 'atts'
  box.hidden = true
  document.querySelector('.dock-in')!.insertBefore(box, document.querySelector('.field'))
  render(<AttTray />, { container: box })
  return box
}

function mountSlash(): void {
  render(<SlashList />, { container: document.getElementById('slashList')! })
}

function mountLive(afterPaint?: () => void): void {
  const host = document.createElement('div')
  host.dataset.cvl = '1'
  host.style.display = 'contents'
  document.getElementById('stage')!.appendChild(host)
  render(<TurnLive afterPaint={afterPaint} />, { container: host })
}

afterEach(() => {
  cleanup()
  store._resetForTests()
  lang = 'zh'
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('the send button', () => {
  it('is dead with an empty field, live once something is typed', () => {
    wire()
    store.goPaint()
    expect(go().disabled).toBe(true)
    expect(go().getAttribute('aria-label')).toBe('发送')
    ta().value = 'hello'
    store.goPaint()
    expect(go().disabled).toBe(false)
    expect(go().classList.contains('halt')).toBe(false)
  })

  it('stays sendable with an attachment and an empty field', async () => {
    const up = vi.fn(async () => ({ path: 'uploads/a.png', size: 9 }))
    wire({ upload: up })
    mountTray()
    store.goPaint()
    expect(go().disabled).toBe(true)
    await act(async () => {
      store.addFiles([new File(['x'], 'a.png', { type: 'image/png' })])
      await flush()
    })
    expect(ta().value).toBe('')
    expect(go().disabled).toBe(false)
  })

  it('turns into stop while a turn runs, and halts on the click', () => {
    const { calls } = wire({ busy: () => true })
    store.goPaint()
    expect(go().disabled).toBe(false)
    expect(go().classList.contains('halt')).toBe(true)
    expect(go().getAttribute('aria-label')).toBe('停止')
    store.goClick()
    expect(calls.halted).toBe(1)
    expect(calls.sent).toEqual([])
  })

  it('sends the trimmed text, clears the field and drops the parked draft', () => {
    const { calls } = wire()
    ta().value = '  ship it  '
    store.fireSend()
    expect(calls.sent).toEqual(['ship it'])
    expect(ta().value).toBe('')
    expect(calls.draftDropped).toBe(1)
  })

  it('refuses to send nothing at all', () => {
    const { calls } = wire()
    store.fireSend()
    expect(calls.sent).toEqual([])
  })
})

describe('the queue rows', () => {
  it('draws one row per queued message with an edit and a remove button', () => {
    const q = ['first', 'second']
    wire({ queue: () => q })
    mountQueue()
    const rows = document.querySelectorAll('#queued .qrow')
    expect(rows.length).toBe(2)
    expect(rows[0]!.querySelector('.v')!.textContent).toBe('first')
    const btns = rows[0]!.querySelectorAll('button.icb')
    expect(btns.length).toBe(2)
    expect(btns[0]!.getAttribute('data-tip')).toBe('编辑')
    expect(btns[1]!.getAttribute('aria-label')).toBe('删掉')
  })

  it('commits an edit on Enter, writing through to the page queue', () => {
    const q = ['first', 'second']
    wire({ queue: () => q })
    mountQueue()
    act(() => { store.editRow(1) })
    const input = document.querySelector('#queued .qrow input') as HTMLInputElement
    expect(input.value).toBe('second')
    input.value = 'second, revised'
    act(() => { fireEvent.keyDown(input, { key: 'Enter' }) })
    expect(q).toEqual(['first', 'second, revised'])
    expect(document.querySelector('#queued .qrow input')).toBeNull()
  })

  it('lets an IME keep Enter while a composition is open', () => {
    const q = ['first']
    wire({ queue: () => q })
    mountQueue()
    act(() => { store.editRow(0) })
    const input = document.querySelector('#queued .qrow input') as HTMLInputElement
    input.value = 'half typed'
    act(() => { fireEvent.keyDown(input, { key: 'Enter', isComposing: true }) })
    expect(q).toEqual(['first'])
    expect(document.querySelector('#queued .qrow input')).toBeTruthy()
  })

  it('cancels on Escape, and the blur that follows does not commit either', () => {
    const q = ['first']
    wire({ queue: () => q })
    mountQueue()
    act(() => { store.editRow(0) })
    const input = document.querySelector('#queued .qrow input') as HTMLInputElement
    input.value = 'never mind'
    act(() => { fireEvent.keyDown(input, { key: 'Escape' }) })
    act(() => { fireEvent.blur(input) })
    expect(q).toEqual(['first'])
  })

  it('removes a row from the page queue', () => {
    const q = ['first', 'second']
    wire({ queue: () => q })
    mountQueue()
    const rm = document.querySelectorAll('#queued .qrow')[0]!.querySelectorAll('button.icb')[1]!
    act(() => { fireEvent.click(rm) })
    expect(q).toEqual(['second'])
    expect(document.querySelectorAll('#queued .qrow').length).toBe(1)
  })
})

describe('the live turn row', () => {
  it('appears when the turn starts, carries one glyph and one clock, and lands last', () => {
    let busy = false
    wire({ busy: () => busy })
    const stage = document.getElementById('stage')!
    /* The transcript island's lane host is already there; the row belongs
       after it. */
    const lane = document.createElement('div')
    lane.dataset.tsl = '1'
    stage.appendChild(lane)
    const host = document.createElement('div')
    host.dataset.cvl = '1'
    render(<TurnLive afterPaint={() => {
      if (!store.getState().live) { host.remove(); return }
      if (stage.lastElementChild !== host) stage.appendChild(host)
    }} />, { container: host })

    expect(document.querySelector('.turnlive')).toBeNull()
    busy = true
    act(() => { store.drawTurnLive() })
    const row = document.querySelector('.turnlive') as HTMLElement
    expect(row).toBeTruthy()
    expect(row.querySelectorAll('.wkg').length).toBe(1)
    expect(row.querySelectorAll('.wkg i').length).toBe(3)
    expect(row.querySelectorAll('.lb').length).toBe(1)
    expect(row.getAttribute('aria-live')).toBe('off')
    expect(stage.lastElementChild).toBe(host)

    /* Ticking must not re-insert anything: re-inserting a node restarts its
       CSS animation. */
    const glyph = row.querySelector('.wkg')
    act(() => { store.drawTurnLive() })
    expect(document.querySelector('.turnlive')).toBe(row)
    expect(document.querySelector('.wkg')).toBe(glyph)

    busy = false
    act(() => { store.drawTurnLive() })
    expect(document.querySelector('.turnlive')).toBeNull()
    expect(host.isConnected).toBe(false)
  })

  it('keeps counting from where the turn really started across a session round trip', () => {
    vi.useFakeTimers()
    wire({ busy: () => true })
    mountLive()
    act(() => { store.drawTurnLive() })
    const anchor = store.liveAnchor()
    expect(anchor).toBeGreaterThan(0)
    /* What the parked-turn machinery does: read the anchor out, zero it with
       the idle paint, put it back on the way in. */
    store.setLiveAnchor(0)
    store.setLiveAnchor(anchor - 61000)
    act(() => { store.drawTurnLive() })
    expect((document.querySelector('.turnlive .lb') as HTMLElement).textContent).toBe('61s')
  })

  it('paints the meter from the source and shows the pill only away from the tail', () => {
    const { calls } = wire({ busy: () => false, meter: () => '3 calls' })
    mountLive()
    calls.stick = false
    const sc = document.getElementById('scroll')!
    Object.defineProperty(sc, 'scrollHeight', { value: 900, configurable: true })
    Object.defineProperty(sc, 'clientHeight', { value: 300, configurable: true })
    sc.scrollTop = 0
    act(() => { store.drawMeter() })
    expect(document.getElementById('meter')!.textContent).toBe('3 calls')
    const pill = document.getElementById('backpill') as HTMLElement
    expect(pill.hidden).toBe(false)
    expect(pill.dataset.tip).toBe('回到底部')
    calls.stick = true
    act(() => { store.drawMeter() })
    expect(pill.hidden).toBe(true)
  })
})

describe('the attachment tray', () => {
  it('stages an in-flight chip, then the uploaded size, and files the image bytes', async () => {
    let settle: (r: { path: string; size: number }) => void = () => {}
    const { calls } = wire({ upload: () => new Promise((r) => { settle = r }) })
    const box = mountTray()
    await act(async () => {
      store.addFiles([new File(['x'], 'shot.png', { type: 'image/png' })])
      await flush()
    })
    expect(box.hidden).toBe(false)
    /* In flight it is a name and a word, not a thumbnail: the bytes are read
       for display but the chip only becomes the image once the path it will be
       keyed by exists -- same order as the renderer this replaces. */
    const chip = box.querySelector('.att') as HTMLElement
    expect(chip.classList.contains('up')).toBe(true)
    expect(chip.querySelector('.sz')!.textContent).toBe('上传中')
    await act(async () => {
      settle({ path: 'uploads/shot.png', size: 2048 })
      await flush()
    })
    expect((box.querySelector('.att') as HTMLElement).classList.contains('up')).toBe(false)
    expect(box.querySelector('.att img')!.getAttribute('title')).toBe('shot.png · 2 KB')
    expect(calls.attImgs[0]![0]).toBe('uploads/shot.png')
  })

  it('shows a non-image as a name and a size', async () => {
    wire({ upload: async () => ({ path: 'uploads/notes.txt', size: 300 }) })
    const box = mountTray()
    await act(async () => {
      store.addFiles([new File(['x'], 'notes.txt', { type: 'text/plain' })])
      await flush()
    })
    expect(box.querySelector('.att .nm')!.textContent).toBe('notes.txt')
    expect(box.querySelector('.att .sz')!.textContent).toBe('300 B')
    expect((box.querySelector('.att') as HTMLElement).classList.contains('img')).toBe(false)
    expect(box.querySelector('.att .rm')!.getAttribute('aria-label')).toBe('移除 notes.txt')
  })

  it('opens an image chip in the viewer, since the square crops it', async () => {
    const { calls } = wire({ upload: async () => ({ path: 'uploads/p.png', size: 10 }) })
    const box = mountTray()
    await act(async () => {
      store.addFiles([new File(['x'], 'p.png', { type: 'image/png' })])
      await flush()
    })
    const img = box.querySelector('.att.img img') as HTMLImageElement
    expect(img).toBeTruthy()
    act(() => { fireEvent.click(img) })
    /* The lightbox is a module in this bundle now, not a shell verb, so the
       click opens the real overlay rather than recording a call. */
    const shown = document.querySelector('.lightbox img') as HTMLImageElement
    expect(shown).toBeTruthy()
    expect(shown.alt).toBe('p.png')
  })

  it('removes a chip, hides the tray when the last one goes, and re-deadens send', async () => {
    wire({ upload: async () => ({ path: 'uploads/a.txt', size: 4 }) })
    const box = mountTray()
    await act(async () => {
      store.addFiles([new File(['x'], 'a.txt', { type: 'text/plain' })])
      await flush()
    })
    expect(go().disabled).toBe(false)
    act(() => { fireEvent.click(box.querySelector('.att .rm')!) })
    expect(box.querySelectorAll('.att').length).toBe(0)
    expect(box.hidden).toBe(true)
    expect(go().disabled).toBe(true)
  })

  it('drops a chip whose upload failed and says so in the transcript', async () => {
    const { calls } = wire({
      upload: async () => { throw { data: { detail: 'disk full' } } },
    })
    const box = mountTray()
    await act(async () => {
      store.addFiles([new File(['x'], 'big.bin', { type: '' })])
      await flush()
    })
    expect(box.querySelectorAll('.att').length).toBe(0)
    expect(calls.notes).toEqual([['big.bin 上传失败', 'disk full']])
  })

  it('hands the staged paths over and empties the tray when the message leaves', async () => {
    wire({ upload: async () => ({ path: 'uploads/a.txt', size: 4 }) })
    const box = mountTray()
    await act(async () => {
      store.addFiles([new File(['x'], 'a.txt', { type: 'text/plain' })])
      await flush()
    })
    expect(store.attsPending()).toBe(0)
    let paths: string[] = []
    act(() => { paths = store.takeAtts() })
    expect(paths).toEqual(['uploads/a.txt'])
    expect(box.querySelectorAll('.att').length).toBe(0)
  })

  /* The island builds the message now, so both of these are its behaviour
     rather than the page layer's. The first one is a bug fix: the page ran this
     same check after fireSend had already emptied the textarea. */
  it('keeps what you typed when a still-uploading file refuses the send', async () => {
    const { calls } = wire({ upload: () => new Promise(() => {}) })
    mountTray()
    const ta = document.getElementById('ta') as HTMLTextAreaElement
    await act(async () => {
      store.addFiles([new File(['x'], 'slow.bin', { type: '' })])
      await flush()
    })
    ta.value = 'look at this'
    act(() => { store.fireSend() })
    expect(calls.sent).toEqual([])
    expect(calls.notes.length).toBe(1)
    expect(ta.value).toBe('look at this')
  })

  it('folds the staged paths into the message it hands the source', async () => {
    const { calls } = wire({ upload: async () => ({ path: 'uploads/a.txt', size: 4 }) })
    mountTray()
    const ta = document.getElementById('ta') as HTMLTextAreaElement
    await act(async () => {
      store.addFiles([new File(['x'], 'a.txt', { type: 'text/plain' })])
      await flush()
    })
    ta.value = 'have a look'
    act(() => { store.fireSend() })
    expect(calls.sent.length).toBe(1)
    /* Two blank lines between the message and the note, one path per dash row --
       byte for byte what the page layer used to build, because the reader's own
       bubble renders its chips from this text. */
    expect(calls.sent[0]).toBe(`have a look\n\n${word('gui.att.note')}\n- uploads/a.txt`)
  })

  it('leads with the note when a file is handed over with nothing typed', async () => {
    const { calls } = wire({ upload: async () => ({ path: 'uploads/b.txt', size: 4 }) })
    mountTray()
    await act(async () => {
      store.addFiles([new File(['x'], 'b.txt', { type: 'text/plain' })])
      await flush()
    })
    act(() => { store.fireSend() })
    expect(calls.sent[0]).toBe(`\n\n${word('gui.att.note')}\n- uploads/b.txt`)
  })

  it('says so instead of opening a picker the demo canvas has no backend for', () => {
    const { calls } = wire({ pickHint: 'demo: pick a file here' })
    const opened = vi.fn()
    store.pickFiles(opened)
    expect(opened).not.toHaveBeenCalled()
    expect(calls.toasts).toEqual(['demo: pick a file here'])
  })
})

describe('the slash palette', () => {
  const cmds = (): SlashCmd[] => [
    { id: 'gui.compress', fn: vi.fn() },
    { id: 'gui.clear', fn: vi.fn() },
  ]

  it('opens on a bare slash and filters by name', () => {
    const slash = cmds()
    wire({ slash })
    mountSlash()
    act(() => { store.drawSlash('/') })
    expect(document.getElementById('slashPop')!.dataset.open).toBe('true')
    let rows = document.querySelectorAll('#slashList .srow')
    expect(rows.length).toBe(2)
    expect(rows[0]!.querySelector('.cmd')!.textContent).toBe('/compress')
    expect(rows[0]!.querySelector('.d')!.textContent).toBe('help for gui.compress')
    expect(rows[0]!.getAttribute('aria-selected')).toBe('true')

    act(() => { store.drawSlash('/clear') })
    rows = document.querySelectorAll('#slashList .srow')
    expect(rows.length).toBe(1)
    expect(rows[0]!.querySelector('.cmd')!.textContent).toBe('/clear')
  })

  it('closes itself when nothing matches', () => {
    wire({ slash: cmds() })
    mountSlash()
    act(() => { store.drawSlash('/zzz') })
    expect(document.getElementById('slashPop')!.dataset.open).toBe('false')
    expect(document.querySelectorAll('#slashList .srow').length).toBe(0)
  })

  it('walks the rows with the arrows and wraps around', () => {
    wire({ slash: cmds() })
    mountSlash()
    act(() => { store.drawSlash('/') })
    act(() => { store.moveSlash(1) })
    let rows = document.querySelectorAll('#slashList .srow')
    expect(rows[1]!.getAttribute('aria-selected')).toBe('true')
    expect(rows[0]!.getAttribute('aria-selected')).toBe('false')
    act(() => { store.moveSlash(1) })
    rows = document.querySelectorAll('#slashList .srow')
    expect(rows[0]!.getAttribute('aria-selected')).toBe('true')
    act(() => { store.moveSlash(-1) })
    rows = document.querySelectorAll('#slashList .srow')
    expect(rows[1]!.getAttribute('aria-selected')).toBe('true')
  })

  it('runs the selected command on Enter and clears the field', () => {
    const slash = cmds()
    const { calls } = wire({ slash })
    mountSlash()
    ta().value = '/comp'
    act(() => { store.drawSlash('/comp') })
    act(() => { store.fieldKeydown(new KeyboardEvent('keydown', { key: 'Enter' })) })
    expect(slash[0]!.fn).toHaveBeenCalledTimes(1)
    expect(ta().value).toBe('')
    expect(calls.draftDropped).toBe(1)
    expect(document.getElementById('slashPop')!.dataset.open).toBe('false')
    /* Enter belonged to the palette, so no message went out. */
    expect(calls.sent).toEqual([])
  })

  it('closes on Escape without halting the turn behind it', () => {
    wire({ slash: cmds() })
    mountSlash()
    act(() => { store.drawSlash('/') })
    const e = new KeyboardEvent('keydown', { key: 'Escape', cancelable: true, bubbles: true })
    const stopped = vi.spyOn(e, 'stopPropagation')
    act(() => { store.fieldKeydown(e) })
    expect(document.getElementById('slashPop')!.dataset.open).toBe('false')
    expect(stopped).toHaveBeenCalled()
  })

  it('opens on a typed slash and closes once a space lands', () => {
    wire({ slash: cmds() })
    mountSlash()
    ta().value = '/cle'
    act(() => { store.fieldInput() })
    expect(store.slashIsOpen()).toBe(true)
    ta().value = '/cle ar'
    act(() => { store.fieldInput() })
    expect(store.slashIsOpen()).toBe(false)
  })
})

describe('a language flip', () => {
  it('repaints the dock in place: the labels, the palette and the live row', () => {
    let busy = true
    const q = ['queued one']
    wire({ busy: () => busy, queue: () => q, slash: [{ id: 'gui.clear', fn: () => {} }] })
    mountQueue()
    mountSlash()
    mountLive()
    act(() => {
      store.drawQueue()
      store.drawSlash('/')
      store.drawTurnLive()
      store.goPaint()
    })
    expect(document.querySelector('#queued .icb')!.getAttribute('aria-label')).toBe('编辑')
    expect(go().getAttribute('aria-label')).toBe('停止')
    expect(document.querySelector('.turnlive')!.getAttribute('aria-label')).toContain('进行中')

    lang = 'en'
    /* What redrawAll() does: call the same paints again, no reload. */
    act(() => {
      store.drawQueue()
      store.drawSlash('/')
      store.drawTurnLive()
      store.goPaint()
    })
    expect(document.querySelector('#queued .icb')!.getAttribute('aria-label')).toBe('Edit')
    expect(document.querySelector('#slashList .d')!.textContent).toBe('help for gui.clear')
    expect(go().getAttribute('aria-label')).toBe('Stop')
    expect(document.querySelector('.turnlive')!.getAttribute('aria-label')).toContain('running')
    busy = false
    act(() => { store.goPaint() })
    expect(go().getAttribute('aria-label')).toBe('Send')
  })
})
