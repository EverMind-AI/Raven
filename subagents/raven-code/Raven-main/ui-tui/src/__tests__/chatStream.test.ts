// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Defense-in-depth tests for createChatStream. The server-side root
// cause (per-turn cancel tearing down the session subscription) is fixed in
// Python; these guard the client so a turn that produces NO terminal event can
// never wedge the UI again:
//   1. watchdog — a turn with no server event within the window clears
//      busy/turnId and surfaces an error (time-driven recovery).
//   2. forceReset — local hard reset used by the Ctrl+C escape hatch
//      (keypress-driven recovery), no server round-trip required.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { TurnEvent, TurnSendResult } from '../rpc/index.js'
import type { Msg } from '../types.js'

import { createChatStream, type ChatStreamRpcClient } from '../app/chatStream.js'
import { turnController } from '../app/turnController.js'
import { resetTurnState } from '../app/turnStore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'

interface FakeRpc extends ChatStreamRpcClient {
  __pushEvent: (event: TurnEvent) => void
}

const makeFakeRpc = (sendResult?: TurnSendResult): FakeRpc => {
  let handler: ((event: TurnEvent) => void) | null = null
  const fake: FakeRpc = {
    __pushEvent: (event: TurnEvent) => {
      if (handler) {
        handler(event)
      }
    },
    async rpc<R, P>(method: string, _params: P): Promise<R> {
      if (method === 'turn.send') {
        return (sendResult ?? { turn_id: 'turn-1', accepted: true }) as unknown as R
      }
      if (method === 'turn.cancel') {
        return { cancelled: true } as unknown as R
      }
      return {} as R
    },
    async subscribe<E, P>(_method: string, _params: P, h: (event: E) => void) {
      handler = h as unknown as (event: TurnEvent) => void
      return {
        subscription_id: 'sub-1',
        unsubscribe: async () => {
          handler = null
        }
      }
    }
  }
  return fake
}

describe('createChatStream — wedge defenses', () => {
  beforeEach(() => {
    resetTurnState()
    resetUiState()
    turnController.fullReset()
  })
  afterEach(() => {
    vi.useRealTimers()
  })

  it('watchdog clears busy/turnId and surfaces an error when a turn produces no server event', async () => {
    vi.useFakeTimers()
    const fake = makeFakeRpc()
    const sysCalls: string[] = []
    const stream = createChatStream({
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m),
      watchdogMs: 5000
    })
    await stream.attach()

    // Mirror the submit handler marking the UI busy, then send a turn whose
    // events the server never delivers (the wedge condition).
    patchUiState({ busy: true })
    await stream.send('hello')
    expect(stream.isTurnActive()).toBe(true)

    // No events arrive. After the watchdog window the UI must recover.
    vi.advanceTimersByTime(5000)

    expect(stream.isTurnActive()).toBe(false)
    expect(getUiState().busy).toBe(false)
    expect(sysCalls.some(m => /no response/i.test(m))).toBe(true)
  })

  it('does not fire the watchdog when the turn completes normally', async () => {
    vi.useFakeTimers()
    const fake = makeFakeRpc()
    const sysCalls: string[] = []
    const stream = createChatStream({
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m),
      watchdogMs: 5000
    })
    await stream.attach()
    patchUiState({ busy: true })
    await stream.send('hi')

    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
    fake.__pushEvent({ type: 'token.delta', payload: { text: 'a' } })
    fake.__pushEvent({
      type: 'message.complete',
      payload: {
        turn_id: 'turn-1',
        usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 }
      }
    })

    // Long after the window: a completed turn must not trip the watchdog.
    vi.advanceTimersByTime(60000)
    expect(sysCalls.some(m => /no response/i.test(m))).toBe(false)
    expect(getUiState().busy).toBe(false)
  })

  it('forceReset clears turn state locally without any server event', async () => {
    const fake = makeFakeRpc()
    const stream = createChatStream({ rpcClient: fake, sessionKey: 'tui:default' })
    await stream.attach()
    await stream.send('long task')
    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
    patchUiState({ busy: true })
    expect(stream.isTurnActive()).toBe(true)

    stream.forceReset()

    expect(stream.isTurnActive()).toBe(false)
    expect(getUiState().busy).toBe(false)
  })

  it('forceReset clears the armed Ctrl+C escape-hatch flag', async () => {
    const fake = makeFakeRpc()
    const stream = createChatStream({ rpcClient: fake, sessionKey: 'tui:default' })
    await stream.attach()
    await stream.send('long task')
    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
    // Mirror the first Ctrl+C arming the escape hatch (set by useInputHandlers).
    patchUiState({ busy: true, escapeArmed: true })

    stream.forceReset()

    // The hint must revert: a second Ctrl+C should not stay armed once the
    // turn has been reset.
    expect(getUiState().escapeArmed).toBe(false)
    expect(getUiState().busy).toBe(false)
  })

  it('does not false-positive when message.start arrives in the same packet as the turn.send accept (arming race)', async () => {
    vi.useFakeTimers()
    // Reproduce the false positive: under a first-submit render stall the
    // server's accept and its pre-LLM message.start land in one network packet.
    // The subscription callback fires synchronously inside turn.send — BEFORE
    // its accept resolves — so the watchdog must already be armed by then, and
    // must not re-arm afterwards. The model is then silent past the window
    // (slow first token); a healthy turn must not be declared dead.
    let handler: ((event: TurnEvent) => void) | null = null
    const fake: ChatStreamRpcClient = {
      async rpc<R, P>(method: string, _params: P): Promise<R> {
        if (method === 'turn.send') {
          if (handler) {
            handler({ type: 'message.start', payload: { turn_id: 'turn-1' } })
          }
          return { turn_id: 'turn-1', accepted: true } as unknown as R
        }
        return {} as R
      },
      async subscribe<E, P>(_method: string, _params: P, h: (event: E) => void) {
        handler = h as unknown as (event: TurnEvent) => void
        return { subscription_id: 'sub-1', unsubscribe: async () => {} }
      }
    }
    const sysCalls: string[] = []
    const stream = createChatStream({
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m),
      watchdogMs: 5000
    })
    await stream.attach()
    patchUiState({ busy: true })
    await stream.send('hello')

    vi.advanceTimersByTime(60000)

    expect(sysCalls.some(m => /no response/i.test(m))).toBe(false)
    expect(stream.isTurnActive()).toBe(true)
  })

  it('recovers the input when turn.send hangs and never returns (hung RPC)', async () => {
    vi.useFakeTimers()
    // turn.send never resolves: the ack watchdog is armed before the await, so
    // a hung RPC still recovers the input instead of freezing the UI.
    const fake: ChatStreamRpcClient = {
      async rpc<R, P>(method: string, _params: P): Promise<R> {
        if (method === 'turn.send') {
          return new Promise<R>(() => {})
        }
        return {} as R
      },
      async subscribe<E, P>(_method: string, _params: P, _h: (event: E) => void) {
        return { subscription_id: 'sub-1', unsubscribe: async () => {} }
      }
    }
    const sysCalls: string[] = []
    const stream = createChatStream({
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m),
      watchdogMs: 5000
    })
    await stream.attach()
    patchUiState({ busy: true })
    void stream.send('hello')

    await vi.advanceTimersByTimeAsync(5000)

    expect(getUiState().busy).toBe(false)
    expect(sysCalls.some(m => /no response/i.test(m))).toBe(true)
  })

  it('detach during a hung turn.send clears the in-flight guard so the next send is accepted', async () => {
    let resumeSend: (() => void) | null = null
    const fake: ChatStreamRpcClient = {
      async rpc<R, P>(method: string, _params: P): Promise<R> {
        if (method === 'turn.send') {
          if (resumeSend === null) {
            // First send hangs until detach; never resolves on its own.
            return new Promise<R>(() => {})
          }
          return { turn_id: 'turn-2', accepted: true } as unknown as R
        }
        return {} as R
      },
      async subscribe<E, P>(_method: string, _params: P, _h: (event: E) => void) {
        return { subscription_id: 'sub-1', unsubscribe: async () => {} }
      }
    }
    const stream = createChatStream({ rpcClient: fake, sessionKey: 'tui:default' })
    await stream.attach()
    void stream.send('first (hangs)')

    await stream.detach()
    resumeSend = () => {}
    await stream.attach()

    // The hung send left sendInFlight true; detach must have cleared it, else
    // this throws 'turn already in progress'.
    await expect(stream.send('second')).resolves.toMatchObject({ accepted: true })
  })

  it('measures server-ack liveness only: model silence past the window after message.start is not a false positive', async () => {
    vi.useFakeTimers()
    const fake = makeFakeRpc()
    const sysCalls: string[] = []
    const stream = createChatStream({
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m),
      watchdogMs: 5000
    })
    await stream.attach()
    patchUiState({ busy: true })
    await stream.send('slow first token')

    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
    // Pre-LLM ack received; the model now produces no delta for far longer than
    // the window. The watchdog must NOT re-arm into an LLM-TTFT judge.
    vi.advanceTimersByTime(60000)

    expect(sysCalls.some(m => /no response/i.test(m))).toBe(false)
    expect(stream.isTurnActive()).toBe(true)
  })
})

describe('createChatStream — cancel preserves streamed content', () => {
  beforeEach(() => {
    resetTurnState()
    resetUiState()
    turnController.fullReset()
  })

  it('keeps the streamed partial in the transcript on a server cancelled_by_client', async () => {
    const appended: Msg[] = []
    const fake = makeFakeRpc()
    const stream = createChatStream({
      appendMessage: m => appended.push(m),
      rpcClient: fake,
      sessionKey: 'tui:default'
    })
    await stream.attach()
    patchUiState({ busy: true })
    await stream.send('question')

    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
    fake.__pushEvent({ type: 'token.delta', payload: { text: 'Partial answer so far' } })
    fake.__pushEvent({
      type: 'error',
      payload: { code: -32000, message: 'cancelled', reason: 'cancelled_by_client' }
    })

    const assistant = appended.find(m => m.role === 'assistant')
    expect(assistant).toBeDefined()
    expect(assistant!.text).toContain('Partial answer so far')
    expect(assistant!.text).toContain('[interrupted]')
  })

  it('appends the failure detail to the error line when present', async () => {
    const sysCalls: string[] = []
    const fake = makeFakeRpc()
    const stream = createChatStream({
      appendMessage: () => {},
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m)
    })
    await stream.attach()
    patchUiState({ busy: true })
    await stream.send('question')

    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
    fake.__pushEvent({
      type: 'error',
      payload: { code: -32099, message: 'turn_failed', reason: 'internal', detail: "No module named 'orjson'" }
    })

    expect(sysCalls).toContain("error: turn_failed (code=-32099): No module named 'orjson'")
  })

  it('keeps the streamed partial in the transcript on a local forceReset', async () => {
    const appended: Msg[] = []
    const fake = makeFakeRpc()
    const stream = createChatStream({
      appendMessage: m => appended.push(m),
      rpcClient: fake,
      sessionKey: 'tui:default'
    })
    await stream.attach()
    patchUiState({ busy: true })
    await stream.send('question')

    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
    fake.__pushEvent({ type: 'token.delta', payload: { text: 'Half a reply' } })

    stream.forceReset()

    const assistant = appended.find(m => m.role === 'assistant')
    expect(assistant).toBeDefined()
    expect(assistant!.text).toContain('Half a reply')
    expect(assistant!.text).toContain('[interrupted]')
  })

  it('does not emit a redundant interrupted note when a second cancel re-enters after content was already preserved', async () => {
    const appended: Msg[] = []
    const sysCalls: string[] = []
    const fake = makeFakeRpc()
    const stream = createChatStream({
      appendMessage: m => appended.push(m),
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m)
    })
    await stream.attach()
    patchUiState({ busy: true })
    await stream.send('question')

    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
    fake.__pushEvent({ type: 'token.delta', payload: { text: 'A streamed reply' } })

    // First Ctrl+C escape hatch preserves the partial.
    stream.forceReset()
    // The server's cancel error then arrives (turnId was not cleared on cancel),
    // re-entering finalize on now-empty state.
    fake.__pushEvent({
      type: 'error',
      payload: { code: -32000, message: 'cancelled', reason: 'cancelled_by_client' }
    })

    // Content preserved exactly once; no spurious second interrupted sys note.
    expect(appended.filter(m => m.role === 'assistant')).toHaveLength(1)
    expect(sysCalls.filter(m => m === 'interrupted')).toHaveLength(0)
  })

  it('does not append an empty assistant message when nothing was streamed', async () => {
    const appended: Msg[] = []
    const sysCalls: string[] = []
    const fake = makeFakeRpc()
    const stream = createChatStream({
      appendMessage: m => appended.push(m),
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: m => sysCalls.push(m)
    })
    await stream.attach()
    patchUiState({ busy: true })
    await stream.send('question')

    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
    // No token.delta: the turn is cancelled before any content streamed.
    stream.forceReset()

    expect(appended.some(m => m.role === 'assistant')).toBe(false)
    expect(sysCalls).toContain('interrupted')
  })
})
