// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import catalogue from '../../../../i18n/messages.json'
import { setTranslator } from '../../i18n/t'
import * as confirmStore from '../../state/confirm'
import * as pageStore from '../../state/page'
import { resetSources, setSources } from '../../state/sources'
import { domSnapshot } from '../../test/domSnapshot'
import { installWsPane } from '../../test/wsPaneHarness'
import * as deliveries from './deliveries'
import * as store from './store'
import { WorkspaceApp } from './WorkspacePage';

import type { WorkspaceSnapshot, WorkspaceSource, WsChange } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const writers = vi.hoisted(() => ({ calls: [] as Array<[string, unknown]> }))
vi.mock('../../state/toast', () => ({
  show: (text: string) => { writers.calls.push(['toast', text]) },
}))
vi.mock('../../state/menu', () => ({
  show: (_x: number, _y: number, items: unknown) => { writers.calls.push(['menuAt', items]) },
}))

function change(over: Partial<WsChange> = {}): WsChange {
  return {
    key: '/repo/src/app.py',
    dir: 'src/',
    name: 'app.py',
    kind: 'edit',
    add: 2,
    del: 1,
    hunks: [{ rows: [['ctx', 'a'], ['del', 'b'], ['add', 'c'], ['add', 'd']], add: 2, del: 1 }],
    turn: 1,
    open: false,
    seen: false,
    ...over,
  }
}

function emptyWs(over: Partial<WorkspaceSnapshot> = {}): WorkspaceSnapshot {
  return { changes: [], urls: [], file: null, turn: 1, unseen: 0, deliveries: [], ...over }
}

/* The island runs against the same two seams production wires up: a stand-in
   translator on setTranslator (it returns its key) and a source on
   sources.workspace -- the fixture shape for offline behaviour, a list/reveal
   shape for live behaviour. */
function install(ws: WorkspaceSnapshot, over: Partial<WorkspaceSource> = {}, view = { tab: 'diff', open: true, picked: true }) {
  store.restore(ws)
  const shellCalls: Array<[string, unknown]> = []
  writers.calls = shellCalls
  const source: WorkspaceSource = {
    shortPath: (p) => String(p).replace(/^\/repo\//, ''),
    hostPlatform: () => 'mac',
    openPath: (p) => shellCalls.push(['openPath', p]),
    ...over,
  }
  setTranslator((key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key))
  /* The panel's own viewer, which is what this file is about: with a desk
     opener wired the store hands every open to the floating window instead
     (features/workspace/store.ts's showFile). */
  store.setDeskOpener(null)
  vi.spyOn(confirmStore, 'ask').mockImplementation((_t, _b, _l, fn) => fn())
  vi.spyOn(pageStore, 'show').mockImplementation((id) => shellCalls.push(['showPage', id]))
  installWsPane({
    view: () => view,
    show: (tab) => {
      shellCalls.push(['showWorkspace', tab])
      view.tab = tab
      view.picked = true
      store.sync()
    },
    pick: (tab) => { shellCalls.push(['wsPick', tab]) },
  })
  /* The file view renders markdown through the bundle's renderer, which reads
     sources.prose for what counts as an openable path -- the page installs it
     from features/workspace/source.ts, so the harness does too. */
  setSources({ workspace: source, prose: { pathOf: () => null, linkTargetOf: () => null } })
  /* The viewer fetches /file for text kinds; a pending promise keeps the
     spinner up instead of letting happy-dom dial a real socket. */
  vi.stubGlobal('fetch', () => new Promise(() => {}))
  document.body.innerHTML = '<aside id="ws"><div class="ws-body" id="wsBody"></div></aside>'
  return { source, shellCalls, view, ws: store.shared() }
}

async function mount() {
  const view = render(<WorkspaceApp />, { container: document.getElementById('wsBody')! })
  await act(async () => {
    store.sync()
  })
  return view
}

afterEach(() => {
  act(() => {
    store.reset()
  })
  localStorage.clear()
  store._resetForTests()
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
  resetSources()
})

describe('workspace island', () => {
  it('owns the stable record and round-trips a parked snapshot', () => {
    const first = emptyWs({ turn: 3, urls: [{ url: 'https://one', kind: 'fetch', at: 'now' }] })
    store.restore(first)
    const stable = store.shared()
    const parked = store.snapshot()

    expect(store.advanceTurn()).toBe(4)
    store.restore(emptyWs({ turn: 9 }))
    store.restore(parked)

    expect(store.shared()).toBe(stable)
    expect(store.currentTurn()).toBe(3)
    expect(store.urls()).toEqual(first.urls)
    expect(Object.keys(parked).sort()).toEqual(['changes', 'deliveries', 'file', 'turn', 'unseen', 'urls'])
  })

  it('shows the launcher while nothing happened and no view was picked', async () => {
    const { shellCalls } = install(emptyWs(), {}, { tab: 'diff', open: true, picked: false })
    await mount()
    expect(await screen.findByText('gui.ws.changes')).toBeTruthy()
    expect(screen.getByText('gui.ws.browser')).toBeTruthy()
    /* Browsing the working directory is not one of the things this offers. */
    expect(screen.queryByText('gui.ws.files')).toBeNull()
    await act(async () => {
      screen.getByText('gui.ws.browser').closest('button')!.click()
    })
    expect(shellCalls).toContainEqual(['wsPick', 'browser'])
  })

  it('renders the changes grouped by turn, with counts', async () => {
    install(emptyWs({
      changes: [
        change({ turn: 2, open: false }),
        change({ key: '/repo/README.md', dir: '', name: 'README.md', kind: 'write', turn: 1, add: 5, del: 0 }),
      ],
      turn: 2,
    }))
    await mount()
    expect(await screen.findByText('gui.ws.turn_now')).toBeTruthy()
    expect(screen.getByText('gui.ws.turn_earlier')).toBeTruthy()
    expect(screen.getByText('app.py')).toBeTruthy()
    expect(screen.getByText('README.md')).toBeTruthy()
    expect(screen.getByText('+2')).toBeTruthy()
    expect(screen.getByText('−1')).toBeTruthy()
  })

  /* The chip takes its word by building the key out of the kind
     (`gui.ws.chip.` + c.kind), and the catalogue gate reads literal keys only
     -- so a kind with no pair of words of its own renders its own key at the
     reader, untranslated and in both languages, with nothing red. Written as a
     record of the union so a fifth kind fails to compile until it is listed. */
  it('carries a catalogue word and tooltip for every kind a change row can be', () => {
    const kinds: Record<WsChange['kind'], true> = { add: true, write: true, edit: true, delete: true }
    const ui = (catalogue as { ui: Record<string, unknown> }).ui
    const missing = Object.keys(kinds)
      .flatMap((kind) => [`gui.ws.chip.${kind}`, `gui.ws.chip.${kind}_t`])
      .filter((key) => !(key in ui))
    expect(missing).toEqual([])
  })

  it('shows the empty note once a view was picked but nothing changed', async () => {
    install(emptyWs({ urls: [{ url: 'https://x', kind: 'fetch', at: 'now' }] }))
    await mount()
    expect(await screen.findByText('gui.ws.no_changes')).toBeTruthy()
  })

  it('unfolds a change from its header and folds it back', async () => {
    const c = change({ open: false })
    install(emptyWs({ changes: [c] }))
    await mount()
    const hd = (await screen.findByText('app.py')).closest('.chghd') as HTMLElement
    expect(hd.getAttribute('aria-expanded')).toBe('false')
    await act(async () => {
      hd.click()
    })
    expect(c.open).toBe(true)
    expect(document.querySelector('.diff')).toBeTruthy()
    expect(screen.getByText('b')).toBeTruthy()
    await act(async () => {
      hd.click()
    })
    expect(document.querySelector('.diff')).toBeNull()
  })

  it('routes a row open through the fixture toast when the source cannot browse', async () => {
    const { shellCalls } = install(emptyWs({ changes: [change()] }))
    await mount()
    const more = document.querySelector('.chgm') as HTMLButtonElement
    await act(async () => {
      more.click()
    })
    const menu = shellCalls.find(([k]) => k === 'menuAt')
    expect(menu).toBeTruthy()
    const items = menu![1] as Array<{ label: string; fn: () => void }>
    await act(async () => {
      items[0]!.fn()
    })
    expect(shellCalls).toContainEqual(['openPath', '/repo/src/app.py'])
  })

  it('opens the real viewer for a change when the source can browse', async () => {
    const state = install(emptyWs({ changes: [change()] }), {
      canBrowse: true,
      reveal: async () => ({}),
    })
    await mount()
    await act(async () => {
      store.openPath('/repo/src/app.py')
    })
    expect(state.ws.file?.path).toBe('/repo/src/app.py')
    expect(state.shellCalls).toContainEqual(['showWorkspace', 'file'])
  })

  it('opens a delivered file in the real viewer', async () => {
    const state = install(emptyWs(), {
      canBrowse: true,
    })
    await mount()
    await act(async () => {
      store.openDelivery('/repo/deck.pptx')
    })
    expect(state.ws.file).toMatchObject({ path: '/repo/deck.pptx' })
    expect(state.shellCalls).toContainEqual(['showWorkspace', 'file'])
  })

  /* Chromium refuses its PDF viewer inside any frame that carries the sandbox
     attribute -- the file is fetched and then blocked by the client, and the
     pane stays a grey box -- so the PDF frame carries none. The response's own
     CSP sandbox is what keeps the document off the page's origin. */
  it('frames a PDF without the sandbox attribute', async () => {
    install(emptyWs({
      file: { path: '/repo/deck.pdf', kind: 'pdf', raw: false, text: null, err: null, size: 9, loading: false },
    }), { canBrowse: true }, { tab: 'file', open: true, picked: true })
    await mount()
    const frame = document.querySelector('.fview iframe') as HTMLIFrameElement
    expect(frame).not.toBeNull()
    expect(frame.hasAttribute('sandbox')).toBe(false)
    expect(frame.getAttribute('src')).toContain('/file?path=%2Frepo%2Fdeck.pdf')
  })

  /* An HTML file renders as the page it is: scripts granted on both halves --
     the frame's attribute and the route's header, asked for with run=1 --
     never same-origin, and no note above the frame about what was withheld. */
  it('renders an HTML file with its scripts, isolated, and says nothing above it', async () => {
    install(emptyWs({
      file: { path: '/repo/game.html', kind: 'html', raw: false, text: null, err: null, size: 9, loading: false },
    }), { canBrowse: true }, { tab: 'file', open: true, picked: true })
    await mount()
    const frame = document.querySelector('.fview iframe') as HTMLIFrameElement
    expect(frame).not.toBeNull()
    expect(frame.getAttribute('sandbox')).toBe('allow-scripts')
    expect(frame.getAttribute('src') || '').toContain('run=1')
    expect(document.querySelector('.fview')?.textContent || '').toBe('')
    expect(document.querySelectorAll('.fview button')).toHaveLength(0)
  })

  /* The source flag belongs to the rendered and source pair, and a PDF is not
     offered the pair: read as text it is its bytes as lines, fetched whole. So
     a PDF stays framed however the flag came to be set. */
  it('frames a PDF even when the file asks for its source', async () => {
    install(emptyWs({
      file: { path: '/repo/paper.pdf', kind: 'pdf', raw: true, text: null, err: null, size: 9, loading: false },
    }), { canBrowse: true }, { tab: 'file', open: true, picked: true })
    const asked = vi.fn(() => new Promise(() => {}))
    vi.stubGlobal('fetch', asked)
    await mount()
    const frame = document.querySelector('.fview iframe')
    expect(frame).not.toBeNull()
    expect(frame!.getAttribute('src')).toContain('/file?path=%2Frepo%2Fpaper.pdf')
    expect(document.querySelector('.fview .code')).toBeNull()
    expect(asked).not.toHaveBeenCalled()
  })

  /* A deck is its own kind: the page cannot draw one, but the gateway can
     render it as a PDF, and that is what the viewer frames. */
  it('classifies a deck as its own kind and asks the file route for its PDF', () => {
    expect(store.fileKind('/repo/out/deck.pptx')).toBe('pptx')
    expect(store.fileKind('/repo/out/DECK.PPTX')).toBe('pptx')
    expect(store.fileKind('/repo/out/deck.ppt')).toBe('bin')
    expect(store.makeFile('/repo/out/deck.pptx').kind).toBe('pptx')
    expect(store.renderURL('/repo/out/deck.pptx')).toBe('/file?path=%2Frepo%2Fout%2Fdeck.pptx&render=pdf')
  })

  const deckFile = { path: '/repo/deck.pptx', kind: 'pptx', raw: false, text: null, err: null, size: 9, loading: false }

  it('says it is rendering while the gateway converts a deck', async () => {
    install(emptyWs({ file: { ...deckFile } }), { canBrowse: true }, { tab: 'file', open: true, picked: true })
    await mount()
    expect(await screen.findByText('gui.ws.file_rendering')).toBeTruthy()
    expect(document.querySelector('.fview iframe')).toBeNull()
    expect(screen.queryByText('gui.ws.file_binary')).toBeNull()
  })

  /* The render is asked for first and framed second; once the answer is good
     the frame gets the same URL, without the sandbox attribute a PDF frame
     must not carry, and the note about rendering stays up until it loads. */
  it('draws a deck as pictures of its pages once the gateway has them', async () => {
    install(emptyWs({ file: { ...deckFile } }), { canBrowse: true }, { tab: 'file', open: true, picked: true })
    const cancel = vi.fn()
    const asked: Array<[string, RequestInit | undefined]> = []
    vi.stubGlobal('fetch', (url: string, init?: RequestInit) => {
      asked.push([url, init])
      return Promise.resolve({
        ok: true, status: 200, statusText: 'OK', body: { cancel },
        headers: { get: (h: string) => (h === 'X-Raven-Pdf-Pages' ? '3' : null) },
      })
    })
    await mount()
    await act(async () => { await Promise.resolve() })
    /* One round trip, for the first page: it proves the rendering can be made
       and says how many pages there are to ask for. */
    expect(asked.map((a) => a[0])).toEqual(['/file?path=%2Frepo%2Fdeck.pptx&render=page&p=1'])
    expect(cancel).toHaveBeenCalled()
    /* Not framed. Safari does not draw a framed PDF served under the sandbox
       policy these files carry, and that policy is what keeps an agent's
       document away from the page's cookie and socket, so the pictures are
       what changed rather than the header. */
    expect(document.querySelector('.fview iframe')).toBeNull()
    const pages = [...document.querySelectorAll('img.workspace-page')]
    expect(pages.map((i) => i.getAttribute('src'))).toEqual([
      '/file?path=%2Frepo%2Fdeck.pptx&render=page&p=1',
      '/file?path=%2Frepo%2Fdeck.pptx&render=page&p=2',
      '/file?path=%2Frepo%2Fdeck.pptx&render=page&p=3',
    ])
    /* The first is already fetched; the rest arrive as the reader reaches them,
       and each one is a render on the gateway the first time it is asked for. */
    expect(pages.map((i) => i.getAttribute('loading'))).toEqual(['eager', 'lazy', 'lazy'])
    expect(document.querySelector('.workspace-pages')!.closest('.fview')!
      .classList.contains('workspace-fill')).toBe(true)
    expect(screen.queryByText('gui.ws.file_rendering')).toBeNull()
  })

  it('says it is rendering until the gateway answers, and quotes a refusal', async () => {
    install(emptyWs({ file: { ...deckFile } }), { canBrowse: true }, { tab: 'file', open: true, picked: true })
    vi.stubGlobal('fetch', () => Promise.resolve({
      ok: false, status: 503, statusText: 'Service Unavailable',
      text: () => Promise.resolve('LibreOffice is not installed on the gateway host'),
    }))
    await mount()
    await act(async () => { await Promise.resolve() })
    await act(async () => { await Promise.resolve() })
    expect(document.querySelectorAll('img.workspace-page')).toHaveLength(0)
    expect(screen.queryByText(/LibreOffice is not installed/)).not.toBeNull()
  })

  it('redraws a deck delivered again under the same path', async () => {
    install(emptyWs({ file: { ...deckFile } }), { canBrowse: true }, { tab: 'file', open: true, picked: true })
    vi.stubGlobal('fetch', () => Promise.resolve({
      ok: true, status: 200, statusText: 'OK', body: { cancel: vi.fn() },
      headers: { get: () => '1' },
    }))
    const delivered = (token: string, size: number, at: number): unknown => ({ raven_delivery: {
      files: [{ path: '/repo/deck.pptx', name: 'deck.pptx', title: 'Deck', download_path: `/files/download?token=${token}`, size }],
      delivered_at: at,
    } })
    deliveries.record(deliveries.SESSION, 1, delivered('one', 9, 1000))
    await mount()
    await act(async () => { await Promise.resolve() })
    expect(document.querySelector('img.workspace-page')!.getAttribute('src')).toContain('&v=1000')
    await act(async () => {
      deliveries.record(deliveries.SESSION, 2, delivered('two', 11, 2000))
      await Promise.resolve()
    })
    await act(async () => { await Promise.resolve() })
    expect(document.querySelector('img.workspace-page')!.getAttribute('src')).toContain('&v=2000')
  })

  it('gives a delivered deck the same bar as any other file', async () => {
    install(emptyWs({ file: { ...deckFile } }), { canBrowse: true }, { tab: 'file', open: true, picked: true })
    deliveries.seed([{ path: '/repo/deck.pptx', name: 'deck.pptx', title: 'Deck',
                       download_path: '/files/download?token=deck', size: 9 }])
    await mount()
    expect(screen.getByRole('button', { name: /gui\.ws\.reveal_/ })).toBeTruthy()
    expect(document.querySelector('.fbar .pane-head-seg')).toBeNull()
  })

  it('keeps the deck bar as it is even when the gateway is this desktop', async () => {
    install(emptyWs({ file: { ...deckFile } }), {
      canBrowse: true,
      hostIsLocal: () => true,
      openIn: async () => ({}),
    }, { tab: 'file', open: true, picked: true })
    await mount()
    expect(document.querySelector('.fbar .pane-head-seg')).toBeNull()
    expect(screen.getByRole('button', { name: /gui\.ws\.reveal_/ })).toBeTruthy()
    expect(screen.queryByText('gui.ws.open')).toBeNull()
    expect(screen.queryByLabelText('gui.ws.open_with_pick')).toBeNull()
  })

  /* A viewer shows a file; it does not hand out copies of it. No kind carries
     a download, delivered or not -- the file stays where the agent wrote it,
     one reveal away. */
  it.each(['/repo/out/shot.png', '/repo/out/paper.pdf', '/repo/report.html', '/repo/notes.md', '/repo/deck.pptx', '/repo/blob.bin'])(
    'offers no download on %s', async (path) => {
      install(emptyWs({ file: store.makeFile(path) }), { canBrowse: true }, { tab: 'file', open: true, picked: true })
      deliveries.seed([{ path, name: path.split('/').pop()!, title: 'T', download_path: '/files/download?token=x', size: 9 }])
      await mount()
      expect(document.querySelector('.fwrap a[download]')).toBeNull()
      expect(document.querySelector('.fwrap [href*="/files/download"]')).toBeNull()
      expect(screen.queryByLabelText('gui.ws.download')).toBeNull()
      expect(screen.queryByText('gui.ws.save_copy')).toBeNull()
    })

  /* Where the bar offers a new tab, that button joins the reveal in one group,
     so the squares touch. happy-dom applies no stylesheet, so this pins the
     markup the domain's rule selects and the browser shows the spacing. */
  const GROUPED: Array<[string, string[]]> = [
    ['/repo/out/paper.pdf', ['gui.ws.file_newtab', 'gui.ws.reveal_finder']],
    ['/repo/report.html', ['gui.ws.file_newtab', 'gui.ws.reveal_finder']],
    ['/repo/out/shot.png', ['gui.ws.reveal_finder']],
  ]
  it.each(GROUPED)('keeps the file actions on %s in one group', async (path, labels) => {
    install(emptyWs({ file: store.makeFile(path) }), { canBrowse: true }, { tab: 'file', open: true, picked: true })
    await mount()
    const group = screen.getByLabelText('gui.ws.reveal_finder').parentElement!
    expect(group.classList.contains('workspace-file-acts')).toBe(true)
    expect([...group.children].map((el) => el.getAttribute('aria-label'))).toEqual(labels)
  })

  it('says why a reveal was refused, not the wire code', async () => {
    const refused = Object.assign(new Error('config_validation_error'), {
      data: { detail: "reveal failed: [Errno 2] No such file or directory: 'xdg-open'" },
    })
    const state = install(emptyWs({ file: { ...deckFile } }), { canBrowse: true, reveal: async () => { throw refused } },
      { tab: 'file', open: true, picked: true })
    await mount()
    await act(async () => {
      screen.getByRole('button', { name: /gui\.ws\.reveal_/ }).click()
      await Promise.resolve()
    })
    await act(async () => { await Promise.resolve() })
    const toasts = state.shellCalls.filter((c) => c[0] === 'toast').map((c) => String(c[1]))
    expect(toasts).toEqual(["reveal failed: [Errno 2] No such file or directory: 'xdg-open'"])
  })

  /* A frame cannot say why its document did not come, so the answer to the
     first request is what the viewer quotes, and the deck falls back to the
     note it used to get. */
  it('falls back to the note with the gateway\'s words when a deck does not render', async () => {
    install(emptyWs({ file: { ...deckFile } }), {
      canBrowse: true,
      hostIsLocal: () => true,
      openIn: async () => ({}),
    }, { tab: 'file', open: true, picked: true })
    vi.stubGlobal('fetch', () => Promise.resolve({
      ok: false, status: 504, statusText: 'Gateway Timeout',
      text: async () => 'LibreOffice took longer than 180s to render the deck and was stopped',
    }))
    await mount()
    expect(await screen.findByText(/LibreOffice took longer than 180s/)).toBeTruthy()
    expect(screen.getByText(/gui.ws.render_failed/)).toBeTruthy()
    expect(screen.getByText('gui.ws.file_binary')).toBeTruthy()
    expect(screen.getByText('gui.ws.open_with_host {"k":"PPTX"}')).toBeTruthy()
    expect(document.querySelector('.fview iframe')).toBeNull()
    expect(screen.queryByText('gui.ws.file_rendering')).toBeNull()
  })

  /* A kind the page cannot render: the note offers the host's own application
     for it. Both actions run where the GATEWAY runs, which is why the offer is
     conditional -- see the withheld case below. */
  it('offers the host application for a file it cannot render', async () => {
    const opened: Array<[string, string | undefined]> = []
    const state = install(emptyWs({
      file: { path: '/repo/deck.pptx', kind: 'bin', raw: false, text: null, err: null, size: 9, loading: false },
    }), {
      canBrowse: true,
      openIn: async (p: string, app?: string) => { opened.push([p, app]); return {} },
      hostIsLocal: () => true,
    }, { tab: 'file', open: true, picked: true })
    await mount()
    /* Nothing chosen yet, so the button names the kind and the host decides. */
    const go = await screen.findByText('gui.ws.open_with_host {"k":"PPTX"}')
    await act(async () => { (go.closest('button') as HTMLButtonElement).click() })
    expect(opened).toEqual([['/repo/deck.pptx', undefined]])
    expect(state.shellCalls.filter((c) => c[0] === 'toast')).toEqual([])
  })

  it('remembers the application per kind, not per file', async () => {
    const opened: Array<[string, string | undefined]> = []
    const state = install(emptyWs({
      file: { path: '/repo/deck.pptx', kind: 'bin', raw: false, text: null, err: null, size: 9, loading: false },
    }), {
      canBrowse: true,
      openIn: async (p: string, app?: string) => { opened.push([p, app]); return {} },
      hostIsLocal: () => true,
    }, { tab: 'file', open: true, picked: true })
    await mount()
    const pick = await screen.findByText('gui.ws.open_with_pick')
    await act(async () => {
      ;(pick.closest('button') as HTMLButtonElement)
        .dispatchEvent(new PointerEvent('pointerup', { bubbles: true }))
    })
    const menu = state.shellCalls.find((c) => c[0] === 'menuAt')?.[1] as Array<{ label: string; fn: () => void }>
    expect(menu.map((x) => (typeof x === 'string' ? x : x.label))).toContain('Keynote')
    await act(async () => { menu.find((x) => x.label === 'Keynote')!.fn() })
    expect(opened).toEqual([['/repo/deck.pptx', 'Keynote']])
    /* The choice was for pptx, so ANOTHER pptx inherits it. */
    expect(store.appFor('/elsewhere/other.pptx')).toBe('Keynote')
    expect(store.appFor('/elsewhere/sheet.xlsx')).toBeNull()
  })

  it('lets the reader hand the kind back to the host default', async () => {
    const opened: Array<[string, string | undefined]> = []
    store.setAppFor('/x/a.pptx', 'Keynote')
    const state = install(emptyWs({
      file: { path: '/repo/deck.pptx', kind: 'bin', raw: false, text: null, err: null, size: 9, loading: false },
    }), {
      canBrowse: true,
      openIn: async (p: string, app?: string) => { opened.push([p, app]); return {} },
      hostIsLocal: () => true,
    }, { tab: 'file', open: true, picked: true })
    await mount()
    /* A chosen application names itself on the button -- and IS what the
       button sends, rather than the button meaning "host default" whatever it
       says. */
    const go = await screen.findByText('gui.ws.open_with_app {"a":"Keynote"}')
    await act(async () => { (go.closest('button') as HTMLButtonElement).click() })
    expect(opened).toEqual([['/repo/deck.pptx', 'Keynote']])
    opened.length = 0
    const pick = screen.getByText('gui.ws.open_with_pick')
    await act(async () => {
      ;(pick.closest('button') as HTMLButtonElement)
        .dispatchEvent(new PointerEvent('pointerup', { bubbles: true }))
    })
    const menu = state.shellCalls.find((c) => c[0] === 'menuAt')?.[1] as Array<{ label: string; fn: () => void }>
    /* The current one is marked, so the reader can see what they are changing. */
    expect(menu.some((x) => typeof x !== 'string' && x.label.includes('gui.ws.open_with_now'))).toBe(true)
    await act(async () => {
      menu.find((x) => typeof x !== 'string' && x.label === 'gui.ws.open_with_default')!.fn()
    })
    expect(opened).toEqual([['/repo/deck.pptx', undefined]])
    expect(store.appFor('/x/a.pptx')).toBeNull()
  })

  /* `open` runs on the gateway's host. On a remote serve that is not the
     reader's screen, so the offer is withheld rather than launching a program
     somebody else would have to close. */
  it('withholds the offer when the gateway is not this desktop', async () => {
    install(emptyWs({
      file: {
        path: '/repo/deck.pptx', kind: 'bin',
        raw: false, text: null, err: null, size: 9, loading: false,
      },
      /* Delivered: even so, the note hands out no copy to save. */
      deliveries: [{
        path: '/repo/deck.pptx', name: 'deck.pptx', title: 'Deck', description: '',
        ext: 'pptx', mediaType: '', size: 9, turn: 1, missing: false,
        downloadPath: '/files/download?token=deck',
      }],
    }), {
      canBrowse: true,
      openIn: async () => ({}),
      hostIsLocal: () => false,
    }, { tab: 'file', open: true, picked: true })
    await mount()
    expect(await screen.findByText('gui.ws.file_binary')).toBeTruthy()
    expect(screen.queryByText('gui.ws.open_with_pick')).toBeNull()
    /* And copying the path, which needs no host at all, stays. */
    expect(screen.getByText('gui.ws.copy_path_do')).toBeTruthy()
    expect(document.querySelector('.binote a')).toBeNull()
  })

  it('withholds the offer when the source cannot open at all', async () => {
    install(emptyWs({
      file: { path: '/repo/deck.pptx', kind: 'bin', raw: false, text: null, err: null, size: 9, loading: false },
    }), {
      canBrowse: true,
      hostIsLocal: () => true,
    }, { tab: 'file', open: true, picked: true })
    await mount()
    expect(await screen.findByText('gui.ws.file_binary')).toBeTruthy()
    expect(screen.queryByText('gui.ws.open_with_pick')).toBeNull()
  })

  it('says why nothing opened rather than failing silently', async () => {
    const state = install(emptyWs({
      file: { path: '/repo/deck.pptx', kind: 'bin', raw: false, text: null, err: null, size: 9, loading: false },
    }), {
      canBrowse: true,
      openIn: async () => { throw new Error('open failed: no such application') },
      hostIsLocal: () => true,
    }, { tab: 'file', open: true, picked: true })
    await mount()
    const go = await screen.findByText('gui.ws.open_with_host {"k":"PPTX"}')
    await act(async () => { (go.closest('button') as HTMLButtonElement).click() })
    await act(async () => { await Promise.resolve() })
    expect(state.shellCalls).toContainEqual(['toast', 'open failed: no such application'])
  })

  /* A value read back out of storage is input too: a page that wrote something
     else, or somebody editing the key by hand, must not reach a command line. */
  it('refuses a stored application name that is not one', async () => {
    localStorage.setItem('raven.openWith', JSON.stringify({ pptx: '/bin/sh', xlsx: 'Numbers' }))
    store._resetForTests()
    expect(store.appFor('/x/a.pptx')).toBeNull()
    expect(store.appFor('/x/a.xlsx')).toBe('Numbers')
    localStorage.setItem('raven.openWith', 'not json at all')
    store._resetForTests()
    expect(store.appFor('/x/a.xlsx')).toBeNull()
  })

  it('says so rather than drawing a viewer when the source cannot read files', async () => {
    install(emptyWs(), {}, { tab: 'file', open: true, picked: true })
    await mount()
    expect(await screen.findByText('gui.ws.file_unreadable')).toBeTruthy()
  })

  it('renders a loaded text file as numbered lines and reveals on request', async () => {
    const reveal = vi.fn(async () => ({}))
    const state = install(emptyWs({
      file: { path: '/repo/src/app.py', kind: 'code', raw: false, text: 'x = 1\ny = 2', err: null, size: null, loading: false, seq: 1 },
    }), {
      canBrowse: true,
      reveal,
    }, { tab: 'file', open: true, picked: true })
    await mount()
    expect(await screen.findByText('x = 1')).toBeTruthy()
    expect(screen.getByText('y = 2')).toBeTruthy()
    expect(document.querySelector('.fbar .nm b')?.textContent).toBe('app.py')
    const buttons = [...document.querySelectorAll('.fbar .ghost-ic')]
    await act(async () => {
      ;(buttons[buttons.length - 1] as HTMLButtonElement).click()
    })
    expect(reveal).toHaveBeenCalledWith('/repo/src/app.py')
    expect(state.ws.file?.err).toBeNull()
  })

  it('renders a markdown file through the bundle renderer, not a shell verb', async () => {
    install(emptyWs({
      file: {
        path: '/repo/docs/README.md', kind: 'md', raw: false,
        text: '# Title\n\nsee `src/app.py` and **this**', err: null, size: null, loading: false, seq: 3,
      },
    }), {
      canBrowse: true,
    }, { tab: 'file', open: true, picked: true })
    await mount()
    const prose = document.querySelector('.prose')!
    /* Real prose.ts output, so the heading cap and the emphasis are its own:
       nothing between the panel and the renderer can stub the markdown, which
       is the point of the test. */
    expect(prose.querySelector('h2')?.textContent).toBe('Title')
    expect(prose.querySelector('strong')?.textContent).toBe('this')
  })

  /* The pair switches between two ways of reading one file, so it is offered
     only where both exist: text that also renders. A picture and a deck draw
     the same thing in either position, and a PDF has no source to read, so
     none of them gets the pair -- nor does a kind with nothing to render. */
  const PAIRED: Array<[string, boolean]> = [
    ['/repo/notes.md', true],
    ['/repo/logo.svg', true],
    ['/repo/report.html', true],
    ['/repo/rows.csv', true],
    ['/repo/rows.tsv', true],
    ['/repo/data.json', true],
    ['/repo/out/shot.png', false],
    ['/repo/out/paper.pdf', false],
    ['/repo/out/deck.pptx', false],
    ['/repo/src/app.py', false],
    ['/repo/fix.diff', false],
    ['/repo/out/bundle.zip', false],
  ]
  it.each(PAIRED)('offers the rendered and source pair on %s: %s', async (path, paired) => {
    install(emptyWs({ file: store.makeFile(path) }), { canBrowse: true }, { tab: 'file', open: true, picked: true })
    await mount()
    const pair = [...document.querySelectorAll('.fbar .pane-head-seg button')].map((b) => b.textContent)
    expect(pair).toEqual(paired ? ['gui.ws.file_rendered', 'gui.ws.file_source'] : [])
  })

  it('flips a markdown file between its prose and its numbered source', async () => {
    install(emptyWs({
      file: {
        path: '/repo/docs/README.md', kind: 'md', raw: false,
        text: '# Title\n\nbody', err: null, size: null, loading: false, seq: 4,
      },
    }), { canBrowse: true }, { tab: 'file', open: true, picked: true })
    await mount()
    const rendered = screen.getByRole('tab', { name: 'gui.ws.file_rendered' })
    const source = screen.getByRole('tab', { name: 'gui.ws.file_source' })
    expect(document.querySelector('.fview .prose h2')?.textContent).toBe('Title')

    await act(async () => { source.click() })
    expect(source.getAttribute('aria-selected')).toBe('true')
    expect(document.querySelector('.fview .prose')).toBeNull()
    expect(screen.getByText('# Title').closest('.code')).not.toBeNull()

    await act(async () => { rendered.click() })
    expect(rendered.getAttribute('aria-selected')).toBe('true')
    expect(document.querySelector('.fview .prose h2')?.textContent).toBe('Title')
  })

  it('shows the viewer error when the file read failed', async () => {
    install(emptyWs({
      file: { path: '/repo/a.py', kind: 'code', raw: false, text: null, err: 'gone for good', size: null, loading: false, seq: 2 },
    }), {
      canBrowse: true,
    }, { tab: 'file', open: true, picked: true })
    await mount()
    expect(await screen.findByText('gone for good')).toBeTruthy()
  })

  it('keeps its rendered shape', async () => {
    install(emptyWs({
      changes: [
        change({ turn: 2, open: false }),
        change({ key: '/repo/README.md', dir: '', name: 'README.md', kind: 'write', turn: 1, add: 5, del: 0 }),
      ],
      turn: 2,
    }))
    await mount()
    await screen.findByText('app.py')
    expect(domSnapshot(document.getElementById('wsBody')!)).toMatchSnapshot()
  })
})
