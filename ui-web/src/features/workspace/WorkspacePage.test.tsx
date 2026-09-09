// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { WsApp } from './WorkspacePage'
import * as deliveries from './deliveries'
import * as store from './store'

import type { Shell } from '../../shell/bridge'
import type { WorkspaceSnapshot, WorkspaceSource, WsChange } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const writers = vi.hoisted(() => ({ calls: [] as Array<[string, unknown]> }))
vi.mock('../../shell/toast', () => ({
  show: (text: string) => { writers.calls.push(['toast', text]) },
}))
vi.mock('../../shell/menu', () => ({
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

/* The island runs against the same two seams production wires up: a fake
   shell on window.RavenShell (T returns its key) and a source on
   window.DS.workspace -- the fixture shape for demo behaviour, a list/reveal
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
  const fakeShell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: (id) => shellCalls.push(['showPage', id]),
    wsView: () => view,
    showWorkspace: (tab) => {
      shellCalls.push(['showWorkspace', tab])
      view.tab = tab
      view.picked = true
      store.sync()
    },
    wsPick: (tab) => shellCalls.push(['wsPick', tab]),
  }
  window.RavenShell = fakeShell
  /* The file view renders markdown through the bundle's renderer, which reads
     DS.prose for what counts as an openable path -- the page installs it in
     demo/020-prose.js, so the harness does too. */
  window.DS = { workspace: source, prose: { pathOf: () => null, linkTargetOf: () => null } }
  /* The viewer fetches /file for text kinds; a pending promise keeps the
     spinner up instead of letting happy-dom dial a real socket. */
  vi.stubGlobal('fetch', () => new Promise(() => {}))
  document.body.innerHTML = '<aside id="ws"><div class="ws-body" id="wsBody"></div></aside>'
  return { source, shellCalls, view, ws: store.shared() }
}

async function mount() {
  const view = render(<WsApp />, { container: document.getElementById('wsBody')! })
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
  store._resetAppsForTests()
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
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

  /* HTML is a script carrier the agent wrote, so it keeps the empty sandbox:
     readable, never run. */
  it('keeps the sandbox on an HTML frame', async () => {
    install(emptyWs({
      file: { path: '/repo/report.html', kind: 'html', raw: false, text: null, err: null, size: 9, loading: false },
    }), { canBrowse: true }, { tab: 'file', open: true, picked: true })
    await mount()
    const frame = document.querySelector('.fview iframe') as HTMLIFrameElement
    expect(frame).not.toBeNull()
    expect(frame.getAttribute('sandbox')).toBe('')
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
      /* Delivered, which is what gives the note a URL to save from. */
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
    /* The one download that stays, and this is the case it exists for: the
       viewer cannot render this kind, the host cannot be asked to open it, and
       the path in the note is a path on the gateway's machine. Without the
       link there is no way left to reach the bytes. */
    expect(screen.getByText('gui.ws.save_copy').closest('a')?.getAttribute('href'))
      .toBe('/files/download?token=deck')
  })

  /* And a file that was never delivered has no URL to offer: the viewer reads
     it through the gateway, but nothing serves a copy of an arbitrary path. */
  it('offers no copy to save for a file this session never delivered', async () => {
    install(emptyWs({
      file: {
        path: '/repo/vendor/blob.bin', kind: 'bin',
        raw: false, text: null, err: null, size: 9, loading: false,
      },
    }), {
      canBrowse: true,
      openIn: async () => ({}),
      hostIsLocal: () => false,
    }, { tab: 'file', open: true, picked: true })
    await mount()

    expect(await screen.findByText('gui.ws.file_binary')).toBeTruthy()
    expect(screen.queryByText('gui.ws.save_copy')).toBeNull()
    expect(document.querySelector('.binote a')).toBeNull()
  })

  /* The ordering a session reopen actually produces: `resume()` restores this
     pane while `loadDeliveries()` is still in flight, so the row arrives after
     the note is on screen. Read once, the link never appears; the remote reader
     is stranded by timing rather than by policy. */
  it('offers the copy when the delivery row arrives after the note mounts', async () => {
    install(emptyWs({
      file: {
        path: '/repo/deck.pptx', kind: 'bin',
        raw: false, text: null, err: null, size: 9, loading: false,
      },
    }), {
      canBrowse: true,
      openIn: async () => ({}),
      hostIsLocal: () => false,
    }, { tab: 'file', open: true, picked: true })
    await mount()
    expect(await screen.findByText('gui.ws.file_binary')).toBeTruthy()
    expect(screen.queryByText('gui.ws.save_copy')).toBeNull()

    /* What `deliverables.list` answering looks like from here. */
    await act(async () => {
      deliveries.seed([{ path: '/repo/deck.pptx', name: 'deck.pptx', title: 'Deck',
                         download_path: '/files/download?token=deck', size: 9 }])
    })

    expect(screen.getByText('gui.ws.save_copy').closest('a')?.getAttribute('href'))
      .toBe('/files/download?token=deck')
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
    store._resetAppsForTests()
    expect(store.appFor('/x/a.pptx')).toBeNull()
    expect(store.appFor('/x/a.xlsx')).toBe('Numbers')
    localStorage.setItem('raven.openWith', 'not json at all')
    store._resetAppsForTests()
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
    /* Real prose.ts output, so the heading cap and the emphasis are its own;
       the shell has no md verb to stub, which is the point of the test. */
    expect(prose.querySelector('h2')?.textContent).toBe('Title')
    expect(prose.querySelector('strong')?.textContent).toBe('this')
    expect('md' in (window.RavenShell as object)).toBe(false)
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
})
