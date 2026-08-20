// @vitest-environment happy-dom
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { WsApp } from './WorkspacePage'
import * as store from './store'

import type { Shell } from '../../shell/bridge'
import type { WorkspaceSource, WsChange, WsShared } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

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

function emptyWs(over: Partial<WsShared> = {}): WsShared {
  return { changes: [], urls: [], file: null, turn: 1, unseen: 0, ...over }
}

/* The island runs against the same two seams production wires up: a fake
   shell on window.RavenShell (T returns its key) and a source on
   window.DS.workspace -- the fixture shape for demo behaviour, a list/reveal
   shape for live behaviour. */
function install(ws: WsShared, over: Partial<WorkspaceSource> = {}, view = { tab: 'diff', open: true, picked: true }) {
  const shellCalls: Array<[string, unknown]> = []
  const source: WorkspaceSource = {
    shortPath: (p) => String(p).replace(/^\/repo\//, ''),
    openPath: (p) => shellCalls.push(['openPath', p]),
    ...over,
  }
  const fakeShell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    toast: (text) => shellCalls.push(['toast', text]),
    menuAt: (_x, _y, items) => shellCalls.push(['menuAt', items]),
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: (id) => shellCalls.push(['showPage', id]),
    copyToClip: (text, done) => shellCalls.push(['copyToClip', `${text} -> ${done}`]),
    hostPlatform: () => 'mac',
    wsView: () => view,
    wsState: () => ws,
    showWorkspace: (tab) => {
      shellCalls.push(['showWorkspace', tab])
      view.tab = tab
      view.picked = true
      store.sync()
    },
    wsPick: (tab) => shellCalls.push(['wsPick', tab]),
    drawWsAgents: () => {},
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
  return { source, shellCalls, view, ws }
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
  /* reset() is the session-switch reset, and the tree's width and folded
     state deliberately survive that -- so a test that drove them has to hand
     them back itself, or it sets the starting conditions of everything
     declared after it. */
  store.FT.hide = false
  store.FT.w = 208
  cleanup()
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('workspace island', () => {
  it('shows the launcher while nothing happened and no view was picked', async () => {
    const { shellCalls } = install(emptyWs(), {}, { tab: 'diff', open: true, picked: false })
    await mount()
    expect(await screen.findByText('gui.ws.changes')).toBeTruthy()
    expect(screen.getByText('gui.ws.files')).toBeTruthy()
    expect(screen.getByText('gui.ws.browser')).toBeTruthy()
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
      list: async () => ({ root: '/repo', entries: [] }),
      reveal: async () => ({}),
    })
    await mount()
    await act(async () => {
      store.openPath('/repo/src/app.py')
    })
    expect(state.ws.file?.path).toBe('/repo/src/app.py')
    expect(state.shellCalls).toContainEqual(['showWorkspace', 'file'])
  })

  it('keeps the demo file tab an empty note without a browsable source', async () => {
    install(emptyWs(), {}, { tab: 'file', open: true, picked: true })
    await mount()
    expect(await screen.findByText('gui.ws.dir_empty')).toBeTruthy()
  })

  it('draws the tree from the source, folders first, and opens a file', async () => {
    const state = install(emptyWs(), {
      canBrowse: true,
      list: async (dir: string) => ({
        root: '/repo',
        entries: dir === ''
          ? [{ name: 'zeta.py', size: 10 }, { name: 'docs', dir: true }]
          : [],
      }),
      reveal: async () => ({}),
    }, { tab: 'file', open: true, picked: true })
    await mount()
    await act(async () => {
      await Promise.resolve()
    })
    const rows = [...document.querySelectorAll('.ftrow .nm')].map((n) => n.textContent)
    expect(rows).toEqual(['docs', 'zeta.py'])
    await act(async () => {
      ;(screen.getByText('zeta.py').closest('button') as HTMLButtonElement).click()
    })
    expect(state.ws.file?.path).toBe('/repo/zeta.py')
  })

  it('hides the tree when the grip is shoved past its floor and released', async () => {
    install(emptyWs(), {
      canBrowse: true,
      list: async () => ({ root: '/repo', entries: [{ name: 'zeta.py', size: 10 }] }),
    }, { tab: 'file', open: true, picked: true })
    await mount()
    await act(async () => {
      await Promise.resolve()
    })
    const wrap = document.querySelector('.fwrap') as HTMLElement
    const grip = wrap.querySelector('button.grip') as HTMLButtonElement
    grip.setPointerCapture = () => {}
    grip.releasePointerCapture = () => {}
    const at = (type: string, x: number): PointerEvent =>
      new PointerEvent(type, { clientX: x, bubbles: true, pointerId: 1 })
    expect(wrap.dataset.tree).toBe('on')
    await act(async () => {
      grip.dispatchEvent(at('pointerdown', 300))
      /* Down to the floor, then a good shove past it: the release reads how
         far the pointer actually got, not where it started. */
      grip.dispatchEvent(at('pointermove', 120))
      grip.dispatchEvent(at('pointermove', 200))
      grip.dispatchEvent(at('pointermove', 100))
      grip.dispatchEvent(at('pointerup', 100))
    })
    expect(wrap.dataset.tree).toBe('off')
  })

  it('shows the read failure in the tree when the source refuses', async () => {
    install(emptyWs(), {
      canBrowse: true,
      list: async () => {
        throw new Error('denied')
      },
    }, { tab: 'file', open: true, picked: true })
    await mount()
    await act(async () => {
      await Promise.resolve()
    })
    expect(await screen.findByText('gui.ws.read_fail {"err":"denied"}')).toBeTruthy()
  })

  it('renders a loaded text file as numbered lines and reveals on request', async () => {
    const reveal = vi.fn(async () => ({}))
    const state = install(emptyWs({
      file: { path: '/repo/src/app.py', kind: 'code', raw: false, text: 'x = 1\ny = 2', err: null, size: null, loading: false, seq: 1 },
    }), {
      canBrowse: true,
      list: async () => ({ root: '/repo', entries: [] }),
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
      list: async () => ({ root: '/repo', entries: [] }),
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
      list: async () => ({ root: '/repo', entries: [] }),
    }, { tab: 'file', open: true, picked: true })
    await mount()
    expect(await screen.findByText('gone for good')).toBeTruthy()
  })
})
