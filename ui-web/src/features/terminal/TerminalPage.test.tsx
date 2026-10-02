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
    taskRef: 'task-1',
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
    mailboxOverview: vi.fn(async () => ({
      data: { bindings: [], messages: [], notifications: [], authority: null },
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

  it('polls and displays mailbox overview with message phases, authority, and compact artifact evidence', async () => {
    rows = [terminal({ identity: { agentName: 'rsi-research-imp', brand: 'claude', bindingGeneration: 1, taskRef: 'task-auth-1' } })]
    ;(source.mailboxOverview as any).mockResolvedValue({
      data: {
        bindings: [{
          binding_id: 'b-1',
          ref: { authority_id: 'auth-1', tenant_id: 't-1', agent_id: 'agent-1', instance_id: 'inst-1', generation: 1 },
          scope: { task_id: 'task-auth-1', workspace_id: 'worktree-1' },
          agent_name: 'rsi-research-imp',
          registry_generation: 1,
          terminal_handle: 'term_claude',
          terminal_incarnation: 'inc-1',
          session_key: null,
          capabilities: ['poll'],
        }],
        authority: {
          task_id: 'task-auth-1',
          workspace_id: 'worktree-1',
          owner_agent_id: 'agent-owner-1',
          assignment_epoch: 3,
          confirmed_handoff_id: null,
          offer_message_id: null,
          accept_message_id: null,
        },
        messages: [
          {
            message_id: 'msg-stored-12345678',
            phase: 'pending',
            result_hash: null,
            terminal_reason: null,
            outcome: null,
            attempt: 0,
            direction: 'incoming',
            envelope: {
              message_id: 'msg-stored-12345678',
              kind: 'task.request',
              sender_identity: { agent_id: 'sender-1' },
              target_identity: { agent_id: 'agent-1' },
              scope: { task_id: 'task-auth-1', workspace_id: 'worktree-1' },
              artifacts: [{ name: 'spec.md', sha256: 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855', size: 100 }],
            },
          },
          {
            message_id: 'msg-claimed-12345678',
            phase: 'in_progress',
            result_hash: null,
            terminal_reason: null,
            outcome: null,
            attempt: 1,
            direction: 'incoming',
          },
          {
            message_id: 'msg-processed-12345678',
            phase: 'completed',
            result_hash: 'res-hash-12345678',
            terminal_reason: null,
            outcome: 'succeeded',
            attempt: 1,
            direction: 'outgoing',
            envelope: {
              message_id: 'msg-processed-12345678',
              kind: 'task.result',
              in_reply_to: 'msg-stored-12345678',
              sender_identity: { agent_id: 'agent-1' },
              target_identity: { agent_id: 'sender-1' },
              scope: { task_id: 'task-auth-1', workspace_id: 'worktree-1' },
            },
          },
          {
            message_id: 'msg-blocked-12345678',
            phase: 'dead_letter',
            result_hash: null,
            terminal_reason: 'blocked',
            outcome: 'blocked',
            attempt: 5,
            direction: 'incoming',
          },
        ],
        notifications: [
          {
            request_id: 'req-input-12345678',
            binding_id: 'b-1',
            message_ids: '["msg-1"]',
            input_hash: 'hash-input-12345678',
            stage: 'input_accepted',
            bytes_written: 42,
          },
          {
            request_id: 'req-turn-12345678',
            binding_id: 'b-1',
            message_ids: '["msg-1"]',
            input_hash: 'hash-turn-12345678',
            stage: 'turn_started',
            turn_id: 'turn-99',
          },
          {
            request_id: 'req-unc-12345678',
            binding_id: 'b-1',
            message_ids: '["msg-1"]',
            input_hash: 'hash-unc-12345678',
            stage: 'uncertain',
            detail: 'host_restarted',
          },
        ],
      },
    })

    await mount()
    fireEvent.click(screen.getByRole('tab', { name: 'rsi-research-imp' }))
    await act(async () => {
      await Promise.resolve()
    })

    expect(source.mailboxOverview).toHaveBeenCalledWith({
      task_id: 'task-auth-1',
      workspace_id: 'worktree-1',
      terminal_handle: 'term_claude',
    })

    expect(screen.getByText('task-auth-1')).toBeTruthy()
    expect(screen.getByText('agent-owner-1')).toBeTruthy()
    expect(screen.getByText(/epoch 3/)).toBeTruthy()

    expect(screen.getByText(/stored: 1/)).toBeTruthy()
    expect(screen.getByText(/claimed: 1/)).toBeTruthy()
    expect(screen.getByText(/processed: 1/)).toBeTruthy()
    expect(screen.getByText('(task incomplete)')).toBeTruthy()
    expect(screen.getByText(/blocked: 1/)).toBeTruthy()

    expect(screen.getByText('spec.md')).toBeTruthy()
    expect(screen.getByText('e3b0c442')).toBeTruthy()
    expect(screen.getByText('reply-to:')).toBeTruthy()
    expect(screen.getByText('result:')).toBeTruthy()

    expect(screen.getByText('input_accepted')).toBeTruthy()
    expect(screen.getByText('turn_started')).toBeTruthy()
    expect(screen.getByText('uncertain')).toBeTruthy()
    expect(screen.getByText('host_restarted')).toBeTruthy()
  })

  it('displays backend handoff status distinctions without assuming every accept is PROPOSED', async () => {
    rows = [terminal()]
    ;(source.mailboxOverview as any).mockResolvedValue({
      data: {
        bindings: [{ binding_id: 'b-1' }],
        messages: [
          {
            message_id: 'msg-accept-raw-12345678',
            phase: 'pending',
            result_hash: null,
            terminal_reason: null,
            outcome: null,
            attempt: 0,
            handoff: { status: 'accept_received' },
            envelope: {
              message_id: 'msg-accept-raw-12345678',
              kind: 'handoff.accept',
            },
          },
          {
            message_id: 'msg-accept-prop-12345678',
            phase: 'pending',
            result_hash: null,
            terminal_reason: null,
            outcome: null,
            attempt: 0,
            handoff: { status: 'PROPOSED' },
            envelope: {
              message_id: 'msg-accept-prop-12345678',
              kind: 'handoff.accept',
            },
          },
        ],
        notifications: [],
        authority: null,
      },
    })

    await mount()
    fireEvent.click(screen.getByRole('tab', { name: 'rsi-research-imp' }))
    await act(async () => {
      await Promise.resolve()
    })

    expect(screen.getByText('handoff: accept_received')).toBeTruthy()
    expect(screen.getByText('handoff: PROPOSED')).toBeTruthy()
  })

  it('hides mailbox section when terminal is unenrolled or has no mailbox data', async () => {
    rows = [terminal()]
    ;(source.mailboxOverview as any).mockResolvedValue({
      data: { bindings: [], messages: [], notifications: [], authority: null },
    })

    await mount()
    fireEvent.click(screen.getByRole('tab', { name: 'rsi-research-imp' }))
    await act(async () => {
      await Promise.resolve()
    })

    expect(document.querySelector('.terminal-mailbox-section')).toBeNull()
    expect(document.querySelector('.terminal-mailbox-notice')).toBeNull()
    expect(screen.getByText('gui.terminal.waiting')).toBeTruthy()
  })

  it.each([
    new Error('receiver_capability_unavailable'),
    { code: -32601, message: 'method_not_found' },
    { code: -32099, message: 'mailbox_error', data: { code: 'receiver_capability_unavailable' } },
  ])('reports honest capability unavailable notice without breaking terminal: %j', async (error) => {
    rows = [terminal()]
    ;(source.mailboxOverview as any).mockRejectedValue(error)

    await mount()
    fireEvent.click(screen.getByRole('tab', { name: 'rsi-research-imp' }))
    await act(async () => {
      await Promise.resolve()
    })

    expect(document.querySelector('.terminal-mailbox-notice')).toBeTruthy()
    expect(screen.getByText('gui.terminal.mailbox_unavailable')).toBeTruthy()
    expect(document.querySelector('.terminal-mailbox-section')).toBeNull()
    expect(screen.getByText('gui.terminal.waiting')).toBeTruthy()
  })

  it('drops stale mailbox responses when task changes or is reconciled', async () => {
    rows = [terminal({ handle: 'term_claude', worktreeId: 'worktree-1', identity: { agentName: 'rsi-research-imp', brand: 'claude', bindingGeneration: 1, taskRef: 'task-1' } })]

    let resolveTask1: ((val: unknown) => void) | null = null
    ;(source.mailboxOverview as any).mockImplementation(({ task_id }: { task_id: string }) => {
      if (task_id === 'task-1') {
        return new Promise((resolve) => {
          resolveTask1 = resolve
        })
      }
      return Promise.resolve({
        data: {
          bindings: [{ binding_id: 'b-task2' }],
          authority: { task_id: 'task-2', workspace_id: 'worktree-1', owner_agent_id: 'agent-2', assignment_epoch: 1 },
          messages: [{
            message_id: 'msg-task2-12345678',
            phase: 'completed',
            result_hash: null,
            terminal_reason: null,
            outcome: 'succeeded',
            attempt: 1,
          }],
          notifications: [],
        },
      })
    })

    await mount('task-1')
    fireEvent.click(screen.getByRole('tab', { name: 'rsi-research-imp' }))

    rows = [terminal({ handle: 'term_claude', worktreeId: 'worktree-1', identity: { agentName: 'rsi-research-imp', brand: 'claude', bindingGeneration: 1, taskRef: 'task-2' } })]
    await act(async () => {
      store.setTask('task-2')
      await Promise.resolve()
    })

    fireEvent.click(screen.getByRole('tab', { name: 'rsi-research-imp' }))
    await act(async () => {
      await Promise.resolve()
    })

    expect(screen.getByText('agent-2')).toBeTruthy()

    await act(async () => {
      resolveTask1?.({
        data: {
          bindings: [{ binding_id: 'b-task1-stale' }],
          authority: { task_id: 'task-1', workspace_id: 'worktree-1', owner_agent_id: 'agent-1-STALE', assignment_epoch: 99 },
          messages: [],
          notifications: [],
        },
      })
      await Promise.resolve()
    })

    expect(screen.queryByText('agent-1-STALE')).toBeNull()
    expect(screen.getByText('agent-2')).toBeTruthy()
  })

  it('drops stale mailbox responses and clears cache when terminal row identity fence changes under the same task', async () => {
    rows = [terminal({
      incarnationId: 'inc-1',
      worktreeId: 'worktree-1',
      identity: { agentName: 'rsi-research-imp', brand: 'claude', bindingGeneration: 1, taskRef: 'task-1' },
    })]
    const overview = (workspace: string, owner: string, epoch: number) => ({ data: {
      bindings: [{ binding_id: owner }],
      authority: { task_id: 'task-1', workspace_id: workspace, owner_agent_id: owner, assignment_epoch: epoch },
      messages: [],
      notifications: [],
    } })
    let resolveOld: (val: unknown) => void = () => { throw new Error('Old request was not started') }
    let resolveNew: (val: unknown) => void = () => { throw new Error('New request was not started') }
    const oldRequest = new Promise((resolve) => { resolveOld = resolve })
    const newRequest = new Promise((resolve) => { resolveNew = resolve })
    ;(source.mailboxOverview as any)
      .mockResolvedValueOnce(overview('worktree-1', 'agent-inc1', 1))
      .mockImplementationOnce(() => oldRequest)
      .mockImplementation(() => newRequest)

    await mount('task-1')
    fireEvent.click(screen.getByRole('tab', { name: 'rsi-research-imp' }))
    await act(async () => { await Promise.resolve() })
    expect(screen.getByText('agent-inc1')).toBeTruthy()
    act(() => { store.selectTab('term_claude') })
    expect(source.mailboxOverview).toHaveBeenCalledTimes(2)

    rows = [terminal({
      incarnationId: 'inc-2',
      worktreeId: 'worktree-2',
      identity: { agentName: 'rsi-research-imp', brand: 'claude', bindingGeneration: 2, taskRef: 'task-1' },
    })]
    await act(async () => { await vi.advanceTimersByTimeAsync(2000) })
    expect(source.mailboxOverview).toHaveBeenCalledTimes(3)
    expect(source.mailboxOverview).toHaveBeenLastCalledWith({
      task_id: 'task-1', workspace_id: 'worktree-2', terminal_handle: 'term_claude',
    })
    expect(screen.queryByText('agent-inc1')).toBeNull()
    await act(async () => {
      resolveNew(overview('worktree-2', 'agent-inc2', 2))
      await Promise.resolve()
    })
    expect(screen.getByText('agent-inc2')).toBeTruthy()
    await act(async () => {
      resolveOld(overview('worktree-1', 'agent-stale-inc1', 1))
      await Promise.resolve()
    })
    expect(screen.queryByText('agent-stale-inc1')).toBeNull()
    expect(screen.getByText('agent-inc2')).toBeTruthy()
  })

  it('displays unresolved items beside PROPOSED handoff status and omits notification input_hash', async () => {
    rows = [terminal()]
    ;(source.mailboxOverview as any).mockResolvedValue({
      data: {
        bindings: [{ binding_id: 'b-1' }],
        messages: [
          {
            message_id: 'msg-proposed-12345678',
            phase: 'pending',
            result_hash: null,
            terminal_reason: null,
            outcome: null,
            attempt: 0,
            handoff: {
              status: 'PROPOSED',
              unresolved_items: ['contract-review', 'coverage-check'],
            },
            envelope: {
              message_id: 'msg-proposed-12345678',
              kind: 'handoff.accept',
            },
          },
        ],
        notifications: [
          {
            request_id: 'req-input-12345678',
            binding_id: 'b-1',
            message_ids: '["msg-1"]',
            input_hash: 'hash-hidden-12345678',
            stage: 'input_accepted',
            bytes_written: 42,
          },
        ],
        authority: null,
      },
    })

    await mount()
    fireEvent.click(screen.getByRole('tab', { name: 'rsi-research-imp' }))
    await act(async () => {
      await Promise.resolve()
    })

    expect(screen.getByText('handoff: PROPOSED')).toBeTruthy()
    expect(screen.getByText('unresolved: contract-review, coverage-check')).toBeTruthy()
    expect(screen.queryByText(/hash:/)).toBeNull()
    expect(screen.queryByText('hash-hidden-12345678')).toBeNull()
  })
})
