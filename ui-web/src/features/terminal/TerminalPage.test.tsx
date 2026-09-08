/** Fixture-backed coverage for the hosted-terminal tab surface. */

// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const xtermHarness = vi.hoisted(() => ({
  instances: [] as Array<{
    cols: number
    rows: number
    writes: Uint8Array[]
    disposed: boolean
    emitData(data: string): void
  }>,
  fits: 0,
}))

vi.mock('@xterm/xterm', () => ({
  Terminal: class {
    cols = 100
    rows = 30
    writes: Uint8Array[] = []
    disposed = false
    private dataListener: ((data: string) => void) | null = null

    constructor() {
      xtermHarness.instances.push(this)
    }

    loadAddon() {}
    open() {}
    onData(listener: (data: string) => void) {
      this.dataListener = listener
      return { dispose: () => (this.dataListener = null) }
    }
    emitData(data: string) {
      this.dataListener?.(data)
    }
    write(data: Uint8Array, callback: () => void) {
      this.writes.push(data)
      callback()
    }
    dispose() {
      this.disposed = true
    }
  },
}))

vi.mock('@xterm/addon-fit', () => ({
  FitAddon: class {
    fit() {
      xtermHarness.fits += 1
    }
  },
}))

import { TerminalApp } from './TerminalPage'
import * as terminalIsland from './mount'
import * as store from './store'

import type { Shell } from '../../shell/bridge'
import type { TerminalRow, TerminalSource } from './types'

;(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true

const terminal = (over: Partial<TerminalRow> = {}): TerminalRow => ({
  handle: 'term_claude',
  incarnationId: 'incarnation-1',
  ptyId: 'worktree-1@@1234abcd',
  tabId: 'tab-1',
  leafId: 'leaf-1',
  paneKey: 'tab-1:leaf-1',
  worktreeId: 'worktree-1',
  worktreePath: '/workspace/raven',
  executionHostId: 'local',
  title: 'rsi-research-imp',
  status: 'working',
  liveness: 'live',
  connected: true,
  writable: true,
  orphaned: false,
  visible: true,
  owner: 'rsi-research-imp',
  lastOutputAt: null,
  identity: {
    agentName: 'rsi-research-imp',
    brand: 'claude',
    bindingGeneration: 1,
  },
  ...over,
})

let rows: TerminalRow[] = []
let source: TerminalSource

function wire(): void {
  rows = []
  source = {
    list: vi.fn(async () => ({
      terminals: rows,
      truncated: false,
      hostScope: { hostIds: ['local'], omittedHostIds: [] },
      topologyRevisions: {},
    })),
    input: vi.fn(async () => ({})),
    resize: vi.fn(async () => ({})),
    subscribe: vi.fn(async ({ handle, enabled = true }) => ({
      subscription: { handle, enabled, seq: 0, ackBytes: 65536, subscription_id: `sub-${handle}` },
    })),
    onOutput: null,
    onEvent: null,
  }
  const fakeShell: Shell = {
    T: (key) => key,
    confirmAsk: (_title, _body, _label, fn) => fn(),
    showPage: () => {},
  }
  window.RavenShell = fakeShell
  window.DS = { terminal: source }
  document.body.innerHTML = '<div class="chat"><div id="terminalHost"></div></div>'
  Object.defineProperty(HTMLElement.prototype, 'offsetParent', {
    configurable: true,
    get: () => document.body,
  })
  xtermHarness.instances.length = 0
  xtermHarness.fits = 0
}

async function mount(task = 'task-1') {
  const view = render(<TerminalApp />, { container: document.getElementById('terminalHost')! })
  await act(async () => {
    store.setTask(task)
    await Promise.resolve()
  })
  return view
}

beforeEach(() => {
  vi.useFakeTimers()
  wire()
})

afterEach(() => {
  act(() => terminalIsland.detach())
  cleanup()
  store._resetForTests()
  window.RavenShell = undefined
  window.DS = undefined
  vi.useRealTimers()
  vi.restoreAllMocks()
  Reflect.deleteProperty(HTMLElement.prototype, 'offsetParent')
})

describe('hosted terminal tabs', () => {
  it('waits for the shell bridge before mounting the terminal island', async () => {
    const publishedShell = window.RavenShell
    const frames: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => {
      frames.push(callback)
      return frames.length
    })
    vi.stubGlobal('cancelAnimationFrame', vi.fn())
    window.RavenShell = undefined

    terminalIsland.mount(document.getElementById('terminalHost')!)
    expect(document.querySelector('.terminal-surface')).toBeNull()

    act(() => frames.shift()?.(0))
    expect(document.querySelector('.terminal-surface')).toBeNull()
    expect(frames).toHaveLength(1)

    window.RavenShell = publishedShell
    await act(async () => {
      frames.shift()?.(1)
      await Promise.resolve()
    })

    expect(screen.getByRole('tab', { name: 'gui.terminal.transcript' })).toBeTruthy()
  })

  it('starts with only the Raven transcript tab', async () => {
    await mount()

    expect(screen.getAllByRole('tab')).toHaveLength(1)
    expect(screen.getByRole('tab', { name: 'gui.terminal.transcript' }).getAttribute('aria-selected')).toBe('true')
    expect(document.querySelector('.terminal-identity')).toBeNull()
    expect(document.querySelector<HTMLElement>('.chat')?.dataset.terminalActive).toBe('false')
  })

  it('polls every two seconds and adds a canonical read-only terminal tab', async () => {
    await mount()
    expect(source.list).toHaveBeenCalledWith('task-1')

    rows = [terminal()]
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })

    const tab = screen.getByRole('tab', { name: 'rsi-research-imp' })
    expect(screen.getAllByRole('tab')).toHaveLength(2)
    fireEvent.click(tab)

    expect(screen.getByText('rsi-research-imp', { selector: '.terminal-name' })).toBeTruthy()
    expect(screen.getByText('claude')).toBeTruthy()
    expect(screen.getByText('term_claude')).toBeTruthy()
    expect(screen.getByText('incarnation-1')).toBeTruthy()
    expect(document.querySelector('.terminal-identity input')).toBeNull()
    expect(document.querySelector('.terminal-identity [contenteditable]')).toBeNull()
    expect(document.querySelector<HTMLElement>('.chat')?.dataset.terminalActive).toBe('true')
  })

  it('drops stale tabs when the current task is reconciled after reconnect', async () => {
    rows = [terminal()]
    await mount()
    expect(screen.getByRole('tab', { name: 'rsi-research-imp' })).toBeTruthy()

    rows = []
    await act(async () => {
      terminalIsland.reconcileTask('task-1')
      await Promise.resolve()
    })

    expect(source.list).toHaveBeenCalledTimes(2)
    expect(screen.getAllByRole('tab')).toHaveLength(1)
    expect(screen.getByRole('tab', { name: 'gui.terminal.transcript' }).getAttribute('aria-selected')).toBe('true')
  })

  it('keeps terminal content across tab switches and falls back when it closes', async () => {
    rows = [terminal()]
    await mount()

    fireEvent.click(screen.getByRole('tab', { name: 'rsi-research-imp' }))
    expect(screen.getByText('gui.terminal.waiting')).toBeTruthy()
    fireEvent.click(screen.getByRole('tab', { name: 'gui.terminal.transcript' }))
    fireEvent.click(screen.getByRole('tab', { name: 'rsi-research-imp' }))
    expect(screen.getByText('gui.terminal.waiting')).toBeTruthy()

    rows = []
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
    })
    expect(screen.getAllByRole('tab')).toHaveLength(1)
    expect(screen.getByRole('tab', { name: 'gui.terminal.transcript' }).getAttribute('aria-selected')).toBe('true')
    expect(document.querySelector<HTMLElement>('.chat')?.dataset.terminalActive).toBe('false')
  })

  it('bridges input, output, resize, acknowledgement and disposal', async () => {
    rows = [terminal()]
    await mount()
    fireEvent.click(screen.getByRole('tab', { name: 'rsi-research-imp' }))

    const xterm = xtermHarness.instances[0]!
    expect(source.subscribe).toHaveBeenCalledWith({ handle: 'term_claude' })
    expect(source.resize).toHaveBeenCalledWith({ handle: 'term_claude', cols: 100, rows: 30 })

    xterm.emitData('confirm target\r')
    expect(source.input).toHaveBeenCalledWith({ handle: 'term_claude', data: 'confirm target\r' })

    const replay = new TextEncoder().encode('buffered output')
    act(() => source.onOutput?.({ handle: 'term_claude', seq: 65536, replay: true, data: replay }))
    expect(xterm.writes[0]).toEqual(replay)
    expect(source.subscribe).not.toHaveBeenCalledWith({ handle: 'term_claude', ack: 65536 })

    const live = new TextEncoder().encode('research output')
    act(() => source.onOutput?.({ handle: 'term_claude', seq: 131072, data: live }))
    expect(xterm.writes[1]).toEqual(live)
    expect(source.subscribe).toHaveBeenCalledWith({ handle: 'term_claude', ack: 131072 })

    rows = []
    await act(async () => {
      await vi.advanceTimersByTimeAsync(2000)
      await Promise.resolve()
    })
    expect(xterm.disposed).toBe(true)
    expect(source.subscribe).toHaveBeenCalledWith({ handle: 'term_claude', enabled: false })
  })

  it('shows delivery and matched acknowledgement without an inbox state', async () => {
    rows = [terminal()]
    await mount()
    fireEvent.click(screen.getByRole('tab', { name: 'rsi-research-imp' }))

    act(() => source.onEvent?.({
      type: 'a2a.send',
      payload: { handle: 'term_claude', state: 'accepted', nonce: 'a2a-123456abcdef' },
    }))
    const delivered = document.querySelector<HTMLElement>('[data-state="delivered"]')!
    const acknowledged = document.querySelector<HTMLElement>('[data-state="acknowledged"]')!
    expect(delivered.classList.contains('active')).toBe(true)
    expect(acknowledged.classList.contains('active')).toBe(false)
    expect(document.querySelector('.terminal-delivery')?.textContent).not.toContain('inbox')

    act(() => source.onEvent?.({
      type: 'a2a.ack.matched',
      payload: { handle: 'term_claude', nonce: 'a2a-reply', ack_for: 'a2a-other', from: 'peer', to: 'raven' },
    }))
    expect(acknowledged.classList.contains('active')).toBe(false)

    act(() => source.onEvent?.({
      type: 'a2a.ack.matched',
      payload: {
        handle: 'term_claude', nonce: null, ack_for: 'a2a-123456abcdef', from: 'peer', to: 'raven',
      },
    }))
    expect(acknowledged.classList.contains('active')).toBe(true)
  })
})
