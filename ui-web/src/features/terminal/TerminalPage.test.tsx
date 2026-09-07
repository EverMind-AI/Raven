/** Fixture-backed coverage for the hosted-terminal tab surface. */

// @vitest-environment happy-dom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { TerminalApp } from './TerminalPage'
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
  }
  const fakeShell: Shell = {
    T: (key) => key,
    confirmAsk: (_title, _body, _label, fn) => fn(),
    showPage: () => {},
  }
  window.RavenShell = fakeShell
  window.DS = { terminal: source }
  document.body.innerHTML = '<div class="chat"><div id="terminalHost"></div></div>'
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
  cleanup()
  store._resetForTests()
  window.RavenShell = undefined
  window.DS = undefined
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('hosted terminal tabs', () => {
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
})
