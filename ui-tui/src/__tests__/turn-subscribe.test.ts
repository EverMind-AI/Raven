// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Tests for the typed chat-subscribe path (Phase 6 T6.1).
//
// These tests exercise `createChatStream` — a thin factory that bridges
// `RpcClient.subscribe('turn.subscribe', ...)` events onto the existing
// `turnController` so the chat UI updates without going through the
// legacy `GatewayClientCompat.gw.on('event', ...)` adapter. They rely on a
// fake RpcClient surface (just the two methods the chat path needs:
// `rpc(method, params)` and `subscribe(method, params, handler)`) so we
// don't have to stand up a unix socket per case.

import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createChatStream, type ChatStreamRpcClient } from '../app/chatStream.js'
import { turnController } from '../app/turnController.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'
import { getUiState, patchUiState, resetUiState } from '../app/uiStore.js'
import type {
  TurnEvent,
  TurnSendParams,
  TurnSendResult,
  TurnSubscribeParams,
  TurnSubscribeResult,
} from '../rpc/index.js'

type FakeUnsubscribe = () => Promise<void>

interface FakeRpc extends ChatStreamRpcClient {
  __pushEvent: (event: TurnEvent) => void
  __sendCalls: Array<{ method: string; params: unknown }>
  __cancelCalls: number
  __subParams: TurnSubscribeParams | null
  __unsubscribeCalls: number
}

const makeFakeRpc = (opts: { sendResult?: TurnSendResult; subId?: string } = {}): FakeRpc => {
  const sendCalls: Array<{ method: string; params: unknown }> = []
  let unsubscribeCalls = 0
  let cancelCalls = 0
  let subParams: TurnSubscribeParams | null = null
  let handler: ((event: TurnEvent) => void) | null = null

  const fake: FakeRpc = {
    __sendCalls: sendCalls,
    __cancelCalls: 0,
    __unsubscribeCalls: 0,
    __subParams: null,
    __pushEvent: (event: TurnEvent) => {
      if (handler) handler(event)
    },
    async rpc<R, P>(method: string, params: P): Promise<R> {
      sendCalls.push({ method, params })
      if (method === 'turn.send') {
        const result = opts.sendResult ?? { turn_id: 'turn-1', accepted: true }
        return result as unknown as R
      }
      if (method === 'turn.cancel') {
        cancelCalls += 1
        fake.__cancelCalls = cancelCalls
        return { cancelled: true } as unknown as R
      }
      return {} as R
    },
    async subscribe<E, P, R extends { subscription_id: string } = { subscription_id: string }>(
      method: string,
      params: P,
      h: (event: E) => void,
    ): Promise<{ subscription_id: string; unsubscribe: FakeUnsubscribe }> {
      void method
      subParams = params as unknown as TurnSubscribeParams
      fake.__subParams = subParams
      handler = h as unknown as (event: TurnEvent) => void
      const subscription_id = opts.subId ?? 'sub-1'
      const unsubscribe: FakeUnsubscribe = async () => {
        unsubscribeCalls += 1
        fake.__unsubscribeCalls = unsubscribeCalls
        handler = null
      }
      return { subscription_id, unsubscribe } as unknown as {
        subscription_id: string
        unsubscribe: FakeUnsubscribe
      } & R
    },
  }
  return fake
}

describe('createChatStream', () => {
  beforeEach(() => {
    resetTurnState()
    resetUiState()
    turnController.fullReset()
  })

  it('streams token.delta sequence and closes the turn on message.complete', async () => {
    const fake = makeFakeRpc()
    const stream = createChatStream({ rpcClient: fake, sessionKey: 'tui:default' })
    await stream.attach()

    // Simulate user submit.
    const sendResult = await stream.send('hello')
    expect(sendResult).toEqual({ turn_id: 'turn-1', accepted: true })
    expect(fake.__sendCalls[0]).toEqual({
      method: 'turn.send',
      params: { session_key: 'tui:default', content: 'hello' } satisfies TurnSendParams,
    })
    expect(fake.__subParams).toEqual({ session_key: 'tui:default' } satisfies TurnSubscribeParams)

    // Drive the event stream.
    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
    expect(getUiState().busy).toBe(true)

    for (const text of ['Hel', 'lo, ', 'wor', 'ld', '!']) {
      fake.__pushEvent({ type: 'token.delta', payload: { text } })
    }
    // After 5 deltas the cumulative buffer should be the joined text. The
    // controller stores raw deltas in `bufRef`; we assert via that internal
    // accumulator since the visible-stream patch is timer-scheduled.
    expect(turnController.bufRef).toBe('Hello, world!')

    fake.__pushEvent({
      type: 'message.complete',
      payload: {
        turn_id: 'turn-1',
        usage: {
          prompt_tokens: 1,
          completion_tokens: 5,
          total_tokens: 6,
          cost_usd: 0.0012,
          context_used: 8378,
          context_max: 1048576,
          context_percent: 1,
        },
      },
    })

    // Turn closed: busy flag released, bufRef cleared.
    expect(getUiState().busy).toBe(false)
    expect(turnController.bufRef).toBe('')

    // Usage from message.complete is merged into ui state so the status bar's
    // context gauge and cost reflect the turn (not frozen at the boot baseline).
    const usage = getUiState().usage
    expect(usage.context_used).toBe(8378)
    expect(usage.context_max).toBe(1048576)
    expect(usage.context_percent).toBe(1)
    expect(usage.cost_usd).toBe(0.0012)

    await stream.detach()
    expect(fake.__unsubscribeCalls).toBe(1)
  })

  it('restores the input prompt when the server reports cancelled_by_client', async () => {
    const fake = makeFakeRpc({ subId: 'sub-cancel' })
    const stream = createChatStream({ rpcClient: fake, sessionKey: 'tui:default' })
    await stream.attach()

    await stream.send('long-task')
    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
    fake.__pushEvent({ type: 'token.delta', payload: { text: 'partial...' } })
    expect(getUiState().busy).toBe(true)

    fake.__pushEvent({
      type: 'error',
      payload: { code: -32800, message: 'cancelled by client', reason: 'cancelled_by_client' },
    })

    // Input prompt restored: not busy, status reset to 'ready' (or
    // 'interrupted' cooldown — either is acceptable as long as it's not
    // a stuck 'running').
    const ui = getUiState()
    expect(ui.busy).toBe(false)
    expect(ui.status === 'ready' || ui.status === 'interrupted').toBe(true)
  })

  it('cancel() routes through turn.cancel when a turn is in flight', async () => {
    const fake = makeFakeRpc()
    const stream = createChatStream({ rpcClient: fake, sessionKey: 'tui:default' })
    await stream.attach()
    await stream.send('a long task that the user will interrupt')
    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })

    expect(stream.isTurnActive()).toBe(true)

    await stream.cancel()

    expect(fake.__cancelCalls).toBe(1)
    expect(fake.__sendCalls.some((c) => c.method === 'turn.cancel')).toBe(true)
    const cancelCall = fake.__sendCalls.find((c) => c.method === 'turn.cancel')
    expect(cancelCall?.params).toEqual({ session_key: 'tui:default' })
  })

  it('cancel() is a no-op when there is no active turn', async () => {
    const fake = makeFakeRpc()
    const stream = createChatStream({ rpcClient: fake, sessionKey: 'tui:default' })
    await stream.attach()

    expect(stream.isTurnActive()).toBe(false)
    await stream.cancel()
    expect(fake.__cancelCalls).toBe(0)
  })

  it('records tool.start / tool.complete onto the active turn', async () => {
    const fake = makeFakeRpc()
    const stream = createChatStream({ rpcClient: fake, sessionKey: 'tui:default' })
    await stream.attach()
    await stream.send('use a tool')
    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })
    fake.__pushEvent({
      type: 'tool.start',
      payload: { tool_call_id: 'tc-1', name: 'shell.exec', arguments: { command: 'ls' } },
    })
    // After tool.start the active tool list contains the started tool.
    expect(getTurnState().tools.some((t) => t.name === 'shell.exec')).toBe(true)

    fake.__pushEvent({
      type: 'tool.complete',
      payload: { tool_call_id: 'tc-1', result_preview: 'a b c', truncated: false },
    })
    // After tool.complete the active tool is removed.
    expect(getTurnState().tools.some((t) => t.id === 'tc-1')).toBe(false)
  })

  it('surfaces non-cancellation errors and restores input prompt', async () => {
    const fake = makeFakeRpc()
    const sysCalls: string[] = []
    const stream = createChatStream({
      rpcClient: fake,
      sessionKey: 'tui:default',
      sys: (msg) => sysCalls.push(msg),
    })
    await stream.attach()
    await stream.send('bad request')
    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })

    fake.__pushEvent({
      type: 'error',
      payload: { code: -32008, message: 'model not available' },
    })

    expect(sysCalls.some((m) => m.includes('model not available'))).toBe(true)
    const ui = getUiState()
    expect(ui.busy).toBe(false)
    // Status should reflect an error state — startsWith('error') or
    // 'ready' if the controller has settled. The contract is "not stuck busy".
    expect(ui.status).not.toBe('running…')
  })

  it('blocks send while a turn is already active', async () => {
    const fake = makeFakeRpc()
    const stream = createChatStream({ rpcClient: fake, sessionKey: 'tui:default' })
    await stream.attach()
    await stream.send('first')
    fake.__pushEvent({ type: 'message.start', payload: { turn_id: 'turn-1' } })

    await expect(stream.send('second')).rejects.toThrow(/turn.*in.*progress|active/i)
    // Only the initial turn.send should have hit the wire.
    const sendCount = fake.__sendCalls.filter((c) => c.method === 'turn.send').length
    expect(sendCount).toBe(1)
  })

  it('does not double-subscribe when attach() is called twice', async () => {
    const fake = makeFakeRpc()
    const stream = createChatStream({ rpcClient: fake, sessionKey: 'tui:default' })
    await stream.attach()
    const subscribeSpy = vi.spyOn(fake, 'subscribe')
    await stream.attach()
    expect(subscribeSpy).not.toHaveBeenCalled()
  })
})
