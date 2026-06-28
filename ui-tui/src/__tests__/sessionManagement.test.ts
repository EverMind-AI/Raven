// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.

import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatStreamRpcClient } from '../app/chatStream.js'
import { createSlashHandler } from '../app/createSlashHandler.js'
import { findSlashCommand } from '../app/slash/registry.js'
import { getOverlayState, resetOverlayState } from '../app/overlayStore.js'
import { patchUiState, resetUiState } from '../app/uiStore.js'
import { buildChatStreamHandle } from '../app/useMainApp.js'

// ── helpers ───────────────────────────────────────────────────────────────────

const buildComposer = () => ({
  enqueue: vi.fn(),
  hasSelection: false,
  paste: vi.fn(),
  queueRef: { current: [] as string[] },
  selection: { copySelection: vi.fn(async () => '') },
  setInput: vi.fn()
})

const buildGateway = () => ({
  gw: {
    getLogTail: vi.fn(() => ''),
    request: vi.fn(() => Promise.resolve({}))
  },
  rpc: vi.fn(() => Promise.resolve({}))
})

const buildLocal = () => ({
  catalog: null,
  getHistoryItems: vi.fn(() => []),
  getLastUserMsg: vi.fn(() => ''),
  maybeWarn: vi.fn(),
  setCatalog: vi.fn()
})

const buildSession = () => ({
  closeSession: vi.fn(() => Promise.resolve(null)),
  deleteSessionWithFallback: vi.fn(() => Promise.resolve(true)),
  die: vi.fn(),
  guardBusySessionSwitch: vi.fn(() => false),
  newSession: vi.fn(),
  resetVisibleHistory: vi.fn(),
  resumeById: vi.fn(),
  setSessionStartedAt: vi.fn()
})

const buildTranscript = () => ({
  page: vi.fn(),
  panel: vi.fn(),
  send: vi.fn(),
  setHistoryItems: vi.fn(),
  sys: vi.fn(),
  trimLastExchange: vi.fn((items: unknown[]) => items)
})

const buildVoice = () => ({
  setVoiceEnabled: vi.fn(),
  setVoiceRecordKey: vi.fn()
})

const buildCtx = (overrides: Partial<ReturnType<typeof buildCtxFull>> = {}) => buildCtxFull(overrides)

const buildCtxFull = (overrides: Partial<{
  slashFlightRef: { current: number }
  composer: ReturnType<typeof buildComposer>
  gateway: ReturnType<typeof buildGateway>
  local: ReturnType<typeof buildLocal>
  session: ReturnType<typeof buildSession>
  transcript: ReturnType<typeof buildTranscript>
  voice: ReturnType<typeof buildVoice>
}> = {}) => ({
  slashFlightRef: overrides.slashFlightRef ?? { current: 0 },
  composer: { ...buildComposer(), ...overrides.composer },
  gateway: { ...buildGateway(), ...overrides.gateway },
  local: { ...buildLocal(), ...overrides.local },
  session: { ...buildSession(), ...overrides.session },
  transcript: { ...buildTranscript(), ...overrides.transcript },
  voice: { ...buildVoice(), ...overrides.voice }
})

// ── /sessions command parsing ──────────────────────────────────────────────────

describe('/sessions slash command', () => {
  beforeEach(() => {
    resetOverlayState()
    resetUiState()
  })

  it('registers a single /sessions command and no legacy /session', () => {
    expect(findSlashCommand('sessions')).toBeDefined()
    expect(findSlashCommand('session')).toBeUndefined()
  })

  it('opens picker for bare /sessions', () => {
    const ctx = buildCtx()
    createSlashHandler(ctx)('/sessions')
    expect(getOverlayState().picker).toBe(true)
    expect(ctx.session.guardBusySessionSwitch).toHaveBeenCalled()
  })

  it('opens picker for /sessions list', () => {
    const ctx = buildCtx()
    createSlashHandler(ctx)('/sessions list')
    expect(getOverlayState().picker).toBe(true)
  })

  it('creates a new session for /sessions new', () => {
    const ctx = buildCtx()
    createSlashHandler(ctx)('/sessions new')
    expect(ctx.session.newSession).toHaveBeenCalledWith('New session started', undefined)
  })

  it('passes title to newSession for /sessions new <title>', () => {
    const ctx = buildCtx()
    createSlashHandler(ctx)('/sessions new sprint planning')
    expect(ctx.session.newSession).toHaveBeenCalledWith('New session started', 'sprint planning')
  })

  it('opens picker for /sessions resume with no id', () => {
    const ctx = buildCtx()
    createSlashHandler(ctx)('/sessions resume')
    expect(getOverlayState().picker).toBe(true)
  })

  it('calls resumeById for /sessions resume <id>', () => {
    const ctx = buildCtx()
    createSlashHandler(ctx)('/sessions resume tui:abc123')
    expect(ctx.session.resumeById).toHaveBeenCalledWith('tui:abc123')
    expect(getOverlayState().picker).toBe(false)
  })

  it('calls deleteSessionWithFallback for /sessions delete current using active sid', () => {
    patchUiState({ sid: 'tui:active-session' })
    const ctx = buildCtx()
    createSlashHandler(ctx)('/sessions delete current')
    expect(ctx.session.deleteSessionWithFallback).toHaveBeenCalledWith('tui:active-session')
  })

  it('calls deleteSessionWithFallback for /sessions delete <id>', () => {
    patchUiState({ sid: 'tui:active' })
    const ctx = buildCtx()
    createSlashHandler(ctx)('/sessions delete tui:other')
    expect(ctx.session.deleteSessionWithFallback).toHaveBeenCalledWith('tui:other')
  })

  it('treats bare /sessions delete (no arg) as deleting the active session', () => {
    patchUiState({ sid: 'tui:active-session' })
    const ctx = buildCtx()
    createSlashHandler(ctx)('/sessions delete')
    expect(ctx.session.deleteSessionWithFallback).toHaveBeenCalledWith('tui:active-session')
  })

  it('normalizes a bare id to the full tui:<chat_id> session key', () => {
    patchUiState({ sid: 'tui:active' })
    const ctx = buildCtx()
    createSlashHandler(ctx)('/sessions delete 20260612_061956_b9e391')
    expect(ctx.session.deleteSessionWithFallback).toHaveBeenCalledWith('tui:20260612_061956_b9e391')
  })

  it('treats a bare id matching the active chat_id as an active delete (guarded)', () => {
    patchUiState({ sid: 'tui:abc123' })
    const guard = vi.fn(() => true)
    const ctx = buildCtx({ session: { ...buildSession(), guardBusySessionSwitch: guard } })

    createSlashHandler(ctx)('/sessions delete abc123')

    expect(guard).toHaveBeenCalled()
    expect(ctx.session.deleteSessionWithFallback).not.toHaveBeenCalled()
  })

  it('reports error for /sessions delete with no active session and no id', () => {
    const ctx = buildCtx()
    createSlashHandler(ctx)('/sessions delete')
    expect(ctx.session.deleteSessionWithFallback).not.toHaveBeenCalled()
    expect(ctx.transcript.sys).toHaveBeenCalledWith('no active session to delete')
  })

  it('shows usage for unknown /sessions subcommand', () => {
    const ctx = buildCtx()
    createSlashHandler(ctx)('/sessions frobnicate')
    expect(ctx.transcript.sys).toHaveBeenCalledWith(
      'usage: /sessions [list|new [title]|resume [id]|delete [id|current]]'
    )
  })

  it('guards busy session switch for /sessions list', () => {
    const ctx = buildCtx({
      session: { ...buildSession(), guardBusySessionSwitch: vi.fn(() => true) }
    })
    createSlashHandler(ctx)('/sessions list')
    expect(getOverlayState().picker).toBe(false)
  })

  it('guards busy session switch for /sessions new', () => {
    const ctx = buildCtx({
      session: { ...buildSession(), guardBusySessionSwitch: vi.fn(() => true) }
    })
    createSlashHandler(ctx)('/sessions new')
    expect(ctx.session.newSession).not.toHaveBeenCalled()
  })

  it('guards busy session switch for /sessions resume', () => {
    const ctx = buildCtx({
      session: { ...buildSession(), guardBusySessionSwitch: vi.fn(() => true) }
    })
    createSlashHandler(ctx)('/sessions resume tui:abc')
    expect(ctx.session.resumeById).not.toHaveBeenCalled()
  })

  it('guards busy session switch for /sessions delete current (active target)', () => {
    patchUiState({ sid: 'tui:active' })
    const guard = vi.fn(() => true)
    const ctx = buildCtx({ session: { ...buildSession(), guardBusySessionSwitch: guard } })

    createSlashHandler(ctx)('/sessions delete current')

    expect(guard).toHaveBeenCalled()
    expect(ctx.session.deleteSessionWithFallback).not.toHaveBeenCalled()
  })

  it('guards busy delete when the explicit id equals the active session', () => {
    patchUiState({ sid: 'tui:active' })
    const guard = vi.fn(() => true)
    const ctx = buildCtx({ session: { ...buildSession(), guardBusySessionSwitch: guard } })

    createSlashHandler(ctx)('/sessions delete tui:active')

    expect(guard).toHaveBeenCalled()
    expect(ctx.session.deleteSessionWithFallback).not.toHaveBeenCalled()
  })

  it('does not guard non-active /sessions delete even while busy', () => {
    patchUiState({ sid: 'tui:active' })
    const guard = vi.fn(() => true)
    const ctx = buildCtx({ session: { ...buildSession(), guardBusySessionSwitch: guard } })

    createSlashHandler(ctx)('/sessions delete tui:other')

    expect(guard).not.toHaveBeenCalled()
    expect(ctx.session.deleteSessionWithFallback).toHaveBeenCalledWith('tui:other')
  })
})

// ── /sessions delete fallback surfaces errors ──────────────────────────────────

describe('/sessions delete error propagation', () => {
  beforeEach(() => {
    resetOverlayState()
    resetUiState()
  })

  it('surfaces deleteSessionWithFallback errors to transcript.sys', async () => {
    patchUiState({ sid: 'tui:active' })
    const deleteSessionWithFallback = vi.fn(() => Promise.reject(new Error('rpc failed')))
    const ctx = buildCtx({ session: { ...buildSession(), deleteSessionWithFallback } })

    createSlashHandler(ctx)('/sessions delete current')

    await vi.waitFor(() => {
      expect(ctx.transcript.sys).toHaveBeenCalledWith('error: rpc failed')
    })
  })

  it('reports success after a non-active /sessions delete resolves', async () => {
    patchUiState({ sid: 'tui:active' })
    const ctx = buildCtx()

    createSlashHandler(ctx)('/sessions delete tui:other')

    await vi.waitFor(() => {
      expect(ctx.transcript.sys).toHaveBeenCalledWith('deleted session: tui:other')
    })
  })

  it('reports "no such session" when the delete resolves false (typo id)', async () => {
    patchUiState({ sid: 'tui:active' })
    const deleteSessionWithFallback = vi.fn(() => Promise.resolve(false))
    const ctx = buildCtx({ session: { ...buildSession(), deleteSessionWithFallback } })

    createSlashHandler(ctx)('/sessions delete tui:typo')

    await vi.waitFor(() => {
      expect(ctx.transcript.sys).toHaveBeenCalledWith('no such session: tui:typo')
    })
    expect(ctx.transcript.sys).not.toHaveBeenCalledWith(expect.stringContaining('deleted session'))
  })

  it('stays silent on active-delete success (the session switch announces itself)', async () => {
    patchUiState({ sid: 'tui:active' })
    const ctx = buildCtx()

    createSlashHandler(ctx)('/sessions delete current')

    await vi.waitFor(() => {
      expect(ctx.session.deleteSessionWithFallback).toHaveBeenCalledWith('tui:active')
    })
    expect(ctx.transcript.sys).not.toHaveBeenCalledWith(expect.stringContaining('deleted session'))
  })
})

// ── /clear is an alias of /new (both mint a fresh session) ────────────────────

describe('/clear and /new', () => {
  beforeEach(() => {
    resetOverlayState()
    resetUiState()
  })

  it('/clear is an alias of /new: mints via newSession, never calls session.clear', () => {
    patchUiState({ sid: 'tui:active' })
    const rpc = vi.fn(() => Promise.resolve({}))
    const ctx = buildCtx({ gateway: { ...buildGateway(), rpc } })

    createSlashHandler(ctx)('/clear')
    getOverlayState().confirm?.onConfirm()

    expect(ctx.session.newSession).toHaveBeenCalledWith('New session started', undefined)
    expect(rpc).not.toHaveBeenCalled()
  })

  it('/clear passes a requested title through to newSession (alias of /new)', () => {
    const ctx = buildCtx()

    createSlashHandler(ctx)('/clear sprint planning')
    getOverlayState().confirm?.onConfirm()

    expect(ctx.session.newSession).toHaveBeenCalledWith('New session started', 'sprint planning')
  })

  it('/new mints a fresh session and does not call session.clear', () => {
    patchUiState({ sid: 'tui:active' })
    const rpc = vi.fn(() => Promise.resolve({}))
    const ctx = buildCtx({ gateway: { ...buildGateway(), rpc } })

    createSlashHandler(ctx)('/new')
    getOverlayState().confirm?.onConfirm()

    expect(ctx.session.newSession).toHaveBeenCalledWith('New session started', undefined)
    expect(rpc).not.toHaveBeenCalled()
  })

  it('/new passes a requested title through to newSession', () => {
    const ctx = buildCtx()

    createSlashHandler(ctx)('/new sprint planning')
    getOverlayState().confirm?.onConfirm()

    expect(ctx.session.newSession).toHaveBeenCalledWith('New session started', 'sprint planning')
  })
})

// ── sid reconciliation: turn path uses minted sid ─────────────────────────────

const makeFakeRpcClient = () => {
  const subscribeParams: unknown[] = []
  const rpcCalls: { method: string; params: unknown }[] = []

  const fake: ChatStreamRpcClient & {
    rpcCalls: typeof rpcCalls
    subscribeParams: typeof subscribeParams
  } = {
    rpcCalls,
    subscribeParams,
    async rpc<R, P>(method: string, params: P): Promise<R> {
      rpcCalls.push({ method, params })

      return { accepted: true, turn_id: 't-1' } as unknown as R
    },
    async subscribe<E, P>(_method: string, params: P, _handler: (event: E) => void) {
      subscribeParams.push(params)

      return { subscription_id: 'sub-1', unsubscribe: async () => {} }
    }
  }

  return fake
}

const noop = () => {}

describe('sid reconciliation (chat-stream seam)', () => {
  beforeEach(() => {
    resetUiState()
  })

  it('keys the chat stream to the minted sid, not a hardcoded default', async () => {
    const fake = makeFakeRpcClient()
    const handle = buildChatStreamHandle(fake, 'tui:minted-123', noop, noop)

    expect(handle).not.toBeNull()
    await handle!.attach()

    expect(fake.subscribeParams).toEqual([{ session_key: 'tui:minted-123' }])
    await handle!.detach()
  })

  it('returns no handle without a sid or rpc client', () => {
    expect(buildChatStreamHandle(makeFakeRpcClient(), null, noop, noop)).toBeNull()
    expect(buildChatStreamHandle(undefined, 'tui:minted-123', noop, noop)).toBeNull()
  })

  it('re-keys the subscription when the sid changes (session switch)', async () => {
    const fake = makeFakeRpcClient()

    const first = buildChatStreamHandle(fake, 'tui:one', noop, noop)
    await first!.attach()
    await first!.detach()

    const second = buildChatStreamHandle(fake, 'tui:two', noop, noop)
    await second!.attach()

    expect(fake.subscribeParams).toEqual([{ session_key: 'tui:one' }, { session_key: 'tui:two' }])
    await second!.detach()
  })
})

// ── /export command ─────────────────────────────────────────────────────────

describe('/export slash command', () => {
  beforeEach(() => {
    resetOverlayState()
    resetUiState()
  })

  const ctxWithRpc = (resp: unknown) =>
    buildCtx({ gateway: { ...buildGateway(), rpc: vi.fn(() => Promise.resolve(resp)) } })

  it('exports the current session when given no arg', async () => {
    patchUiState({ sid: 'tui:active' })
    const ctx = ctxWithRpc({ exported: true, path: '/ws/exports/tui_active.md' })

    createSlashHandler(ctx)('/export')

    expect(ctx.gateway.rpc).toHaveBeenCalledWith('session.export', { session_id: 'tui:active' })
    await vi.waitFor(() => {
      expect(ctx.transcript.sys).toHaveBeenCalledWith('✓ exported to /ws/exports/tui_active.md')
    })
  })

  it('passes a bare id through unchanged for cross-channel resolution', () => {
    patchUiState({ sid: 'tui:active' })
    const ctx = ctxWithRpc({ exported: true, path: '/ws/exports/x.md' })

    createSlashHandler(ctx)('/export 20990101_000000_abcdef')

    expect(ctx.gateway.rpc).toHaveBeenCalledWith('session.export', {
      session_id: '20990101_000000_abcdef'
    })
  })

  it('reports not-found when the id resolves to nothing', async () => {
    patchUiState({ sid: 'tui:active' })
    const ctx = ctxWithRpc({ exported: false, path: null, reason: 'not_found' })

    createSlashHandler(ctx)('/export nope000')

    await vi.waitFor(() => {
      expect(ctx.transcript.sys).toHaveBeenCalledWith('no such session: nope000')
    })
  })

  it('lists candidates on an ambiguous id', async () => {
    patchUiState({ sid: 'tui:active' })
    const ctx = ctxWithRpc({
      exported: false,
      path: null,
      reason: 'ambiguous',
      candidates: ['cli:20990101_000000_dddddd', 'tui:20990101_000000_dddddd']
    })

    createSlashHandler(ctx)('/export 20990101_000000_dddddd')

    await vi.waitFor(() => {
      expect(ctx.transcript.sys).toHaveBeenCalledWith(
        'ambiguous session id — candidates: cli:20990101_000000_dddddd, tui:20990101_000000_dddddd'
      )
    })
  })

  it('reports when there is no active session and no id', () => {
    const ctx = buildCtx()
    createSlashHandler(ctx)('/export')
    expect(ctx.transcript.sys).toHaveBeenCalledWith('no active session to export')
    expect(ctx.gateway.rpc).not.toHaveBeenCalled()
  })
})
