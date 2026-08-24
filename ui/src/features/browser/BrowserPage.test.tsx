// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { BrowserApp } from './BrowserPage'
import * as store from './store'

import type { Shell } from '../../shell/bridge'
import type { BrowserSource, ChromiumSource, LinksSource, UrlRow } from './types'

/* React refuses act() outside a test runner it recognizes unless told. */
;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const shellCalls: Array<[string, unknown]> = []

/* The island runs against the same two seams production wires up: a fake
   shell on window.RavenShell (T returns its key, so tests assert catalogue
   keys, not translations) and a source on window.DS.browser. */
function wire(source: BrowserSource, lang = 'en'): void {
  shellCalls.length = 0
  document.documentElement.lang = lang === 'zh' ? 'zh-CN' : 'en'
  Object.defineProperty(navigator, 'clipboard', {
    configurable: true,
    value: { writeText: (text: string) => { shellCalls.push(['copy', text]); return Promise.resolve() } },
  })
  const fakeShell: Shell = {
    T: (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key),
    toast: () => {},
    menuAt: () => {},
    confirmAsk: (_t, _b, _l, fn) => fn(),
    showPage: () => {},
    showWorkspace: (tab) => shellCalls.push(['showWorkspace', tab]),
    wsShows: () => true,
  }
  window.RavenShell = fakeShell
  window.DS = { browser: source }
  document.body.innerHTML = '<div class="ws-body" id="wsBody"></div>'
}

function links(urls: UrlRow[]): { source: LinksSource; opened: string[] } {
  const opened: string[] = []
  const source: LinksSource = {
    embedded: false,
    urls: () => urls,
    openUrl: (u) => opened.push(u),
  }
  wire(source)
  return { source, opened }
}

function chromium(over: Partial<ChromiumSource> = {}): { source: ChromiumSource; calls: Array<[string, unknown]> } {
  const calls: Array<[string, unknown]> = []
  const source: ChromiumSource = {
    embedded: true,
    urls: () => [],
    frame: async () => ({ started: false }),
    open: async (p) => {
      calls.push(['open', p])
      return {}
    },
    watch: async (p) => {
      calls.push(['watch', p])
      return { watching: !!p.on }
    },
    mode: async () => ({}),
    close: async () => {
      calls.push(['close', null])
    },
    tabs: async (p) => {
      calls.push(['tabs', p])
      return { started: true, tabs: [] }
    },
    input: async (p) => {
      calls.push(['input', p])
    },
    onFrame: null,
    ...over,
  }
  wire(source)
  return { source, calls }
}

function mount() {
  const box = document.getElementById('wsBody')!
  store.setHost(box)
  return render(<BrowserApp />, { container: box })
}

afterEach(() => {
  cleanup()
  store._resetForTests()
  vi.restoreAllMocks()
})

describe('browser island, links shape (the fixture source)', () => {
  it('shows one row per fetched url, kind and stamp included', () => {
    links([
      { url: 'https://api.example.com/docs', kind: 'fetch', at: 'just now' },
      { url: 'rate limits', kind: 'search', at: 'earlier' },
    ])
    mount()
    expect(screen.getByText('api.example.com/docs')).toBeTruthy()
    expect(screen.getByText('gui.ws.web_fetched · just now')).toBeTruthy()
    expect(screen.getByText('gui.ws.web_searched · earlier')).toBeTruthy()
  })

  it('shows the empty note when the agent has fetched nothing', () => {
    links([])
    mount()
    expect(screen.getByText('gui.ws.no_web')).toBeTruthy()
  })

  it('opens a row through the source, and offers open/copy on right-click', () => {
    const { opened } = links([{ url: 'https://a.example/x', kind: 'fetch', at: 'now' }])
    mount()
    const row = screen.getByTitle('https://a.example/x') as HTMLButtonElement & { _ctx?: () => Array<{ fn: () => void }> }
    act(() => row.click())
    expect(opened).toEqual(['https://a.example/x'])
    const items = row._ctx!()
    expect(items).toHaveLength(2)
    items[1]!.fn()
    expect(shellCalls).toContainEqual(['copy', 'https://a.example/x'])
  })
})

describe('browser island, embedded shape (the rpc source)', () => {
  it('says the surface is absent on -32601 and stops asking', async () => {
    chromium({
      frame: async () => {
        throw { code: -32601 }
      },
    })
    mount()
    expect(await screen.findByText('gui.br.absent_h')).toBeTruthy()
  })

  it('shows the install note when the server has no chromium', async () => {
    chromium({ frame: async () => ({ available: false, reason: 'no chromium' }) })
    mount()
    expect(await screen.findByText('gui.br.unavail')).toBeTruthy()
    expect(screen.getByText('uv sync --extra browser && uv run playwright install chromium')).toBeTruthy()
    expect(await screen.findByText('no chromium')).toBeTruthy()
  })

  it('idles on the fetched links and opens one through browser.open', async () => {
    const { calls } = chromium({ urls: () => [{ url: 'https://raven.dev/spec', kind: 'fetch', at: 'now' }] })
    mount()
    expect(await screen.findByText('gui.br.idle')).toBeTruthy()
    const row = screen.getByTitle('https://raven.dev/spec')
    await act(async () => row.click())
    expect(calls).toContainEqual(['open', { url: 'https://raven.dev/spec' }])
  })

  it('draws the stage, the tab strip and a watch lease once a page runs', async () => {
    chromium({
      frame: async () => ({ started: true, url: 'https://example.com/a', title: 'Example' }),
      tabs: async () => ({
        started: true,
        tabs: [{ index: 0, url: 'https://example.com/a', title: 'Example', active: true }],
      }),
    })
    mount()
    expect(await screen.findByText('Example')).toBeTruthy()
    const box = document.getElementById('wsBody')!
    expect(box.querySelector('.bstage canvas.shot')).toBeTruthy()
    expect(box.querySelector('.btabs .btab[aria-current="true"]')).toBeTruthy()
    expect(box.dataset.view).toBe('web')
    expect(box.querySelector('.bbar input.url')).toBeTruthy()
  })

  it('routes the address bar: url-shaped input navigates, prose searches', async () => {
    const { calls } = chromium({ frame: async () => ({ started: true, url: 'https://example.com' }) })
    mount()
    const box = document.getElementById('wsBody')!
    await screen.findByText('gui.br.waiting')
    const bar = box.querySelector('.bbar input.url') as HTMLInputElement
    bar.value = 'example.org/docs'
    await act(async () => {
      fireEvent.keyDown(bar, { key: 'Enter' })
    })
    expect(calls).toContainEqual(['open', { url: 'example.org/docs' }])
    bar.value = 'rate limits'
    await act(async () => {
      fireEvent.keyDown(bar, { key: 'Enter' })
    })
    expect(calls).toContainEqual(['open', { url: 'https://duckduckgo.com/?q=rate%20limits' }])
    document.documentElement.lang = 'zh-CN'
    bar.value = '天气'
    await act(async () => {
      fireEvent.keyDown(bar, { key: 'Enter' })
    })
    expect(calls).toContainEqual(['open', { url: 'https://www.baidu.com/s?wd=%E5%A4%A9%E6%B0%94' }])
  })

  it('clears the address bar in the same tick the page closes', async () => {
    chromium({ frame: async () => ({ started: true, url: 'https://example.com/a' }) })
    mount()
    const box = document.getElementById('wsBody')!
    await screen.findByText('gui.br.waiting')
    const bar = box.querySelector('.bbar input.url') as HTMLInputElement
    /* What a running panel has already done to this node. It matters for what
       is being tested: an imperative write marks the input dirty, and from then
       on React's defaultValue no longer reaches it -- which is why the bar can
       hold an address the state has already dropped. */
    bar.value = 'https://example.com/a'
    await act(async () => {
      await store.closeBrowser()
    })
    /* The bar survives the swap to the idle view at the same position, so
       nothing rebuilds it: without a sync it would still show the address of
       the page that is no longer open until the next poll landed. */
    expect(bar.value).toBe('')
  })

  it('leaves a half-typed address alone when a frame lands mid-edit', async () => {
    chromium({ frame: async () => ({ started: true, url: 'https://example.com/a' }) })
    mount()
    const box = document.getElementById('wsBody')!
    await screen.findByText('gui.br.waiting')
    const bar = box.querySelector('.bbar input.url') as HTMLInputElement
    bar.focus()
    bar.value = 'half typed'
    await act(async () => {
      store.onFrame({ url: 'https://example.com/b', loading: false }, null)
    })
    expect(bar.value).toBe('half typed')
    bar.blur()
    await act(async () => {
      store.onFrame({ url: 'https://example.com/c', loading: false }, null)
    })
    expect(bar.value).toBe('https://example.com/c')
  })

  it('turns a failed navigation into the error page, and retry re-asks', async () => {
    const { calls } = chromium({
      frame: async () => ({ started: true, url: 'https://example.com' }),
      open: async (p) => {
        calls.push(['open', p])
        throw { message: 'boom' }
      },
    })
    mount()
    await screen.findByText('gui.br.waiting')
    await act(async () => {
      await store.open('https://dead.example')
    })
    expect(await screen.findByText('boom')).toBeTruthy()
    const asked = calls.filter(([k]) => k === 'open').length
    await act(async () => {
      screen.getByText('gui.plug.retry').click()
    })
    expect(calls.filter(([k]) => k === 'open').length).toBe(asked + 1)
  })
})
