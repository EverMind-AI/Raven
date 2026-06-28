// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// chatStream — typed chat path scaffold (Phase 6, per design.md §D7).
//
// Bridges `RpcClient.subscribe<TurnEvent>('turn.subscribe', ...)` notifications
// onto the existing `turnController` so UI state updates without going through
// the legacy `GatewayClientCompat.gw.on('event', ...)` adapter. The factory
// returns a thin handle with `attach / detach / send / cancel / isTurnActive`
// so it can be unit-tested against a fake RpcClient (no socket required) and
// wired into `useMainApp.ts` as a per-session lifecycle object.
//
// This file does NOT replace the legacy event handler in
// `createGatewayEventHandler.ts` — that path stays alive for the 169 existing
// .tsx consumers per the adapter-retirement plan
// (`docs/RepoMem/persist/memory/cross-language-rpc-adapter-pattern.md` §三).
// Once the Python `turn.*` handlers land and `prompt.submit` is removed, the
// legacy chat-event branch in createGatewayEventHandler becomes dead code
// and is deleted alongside the gateway-compat shim.

import type {
  ErrorEvent,
  MessageCompleteEvent,
  MessageStartEvent,
  TokenDeltaEvent,
  ToolCompleteEvent,
  ToolStartEvent,
  TurnEvent,
  TurnSendParams,
  TurnSendResult,
  TurnSubscribeParams,
} from '../rpc/index.js'

import type { Msg } from '../types.js'

import { turnController } from './turnController.js'
import { patchTurnState } from './turnStore.js'
import { patchUiState } from './uiStore.js'

/**
 * Minimal RpcClient surface the chat path needs. Defining this locally lets
 * tests inject a fake without touching the real socket-backed RpcClient.
 * The shape mirrors the public methods of `src/rpc/client.ts::RpcClient`,
 * including the `subscribe` return shape `{subscription_id, unsubscribe}`.
 */
export interface ChatStreamRpcClient {
  rpc<R = unknown, P = unknown>(method: string, params: P): Promise<R>
  subscribe<E = unknown, P = unknown>(
    method: string,
    params: P,
    handler: (event: E) => void,
    opts?: { unsubscribeMethod?: string },
  ): Promise<{ subscription_id: string; unsubscribe: () => Promise<void> }>
}

export interface ChatStreamOptions {
  rpcClient: ChatStreamRpcClient
  sessionKey: string
  /** Optional sys-message hook for surfacing non-cancellation errors. */
  sys?: (msg: string) => void
  /**
   * Append a finished message to the React history list. Required for
   * `message.complete` to persist the assistant turn's final text + tool
   * trail in the UI — without this the streamed tokens vanish on completion.
   * Mirrors the legacy `createGatewayEventHandler.ts:675` pattern.
   */
  appendMessage?: (msg: Msg) => void
  /**
   * Server-ack watchdog window (ms). Armed when `send` starts; if NO server
   * event of any kind arrives within this window the turn is treated as wedged
   * (events lost / subscription not delivering / turn.send hung) and the input
   * is restored instead of freezing. It measures server-ack liveness only — the
   * first inbound event disarms it — never LLM first-token latency. Defaults to
   * {@link DEFAULT_WATCHDOG_MS}.
   */
  watchdogMs?: number
}

/** Default server-ack watchdog window — see {@link ChatStreamOptions.watchdogMs}. */
export const DEFAULT_WATCHDOG_MS = 10_000

export interface ChatStreamHandle {
  attach: () => Promise<void>
  detach: () => Promise<void>
  send: (content: string) => Promise<TurnSendResult>
  cancel: () => Promise<void>
  isTurnActive: () => boolean
  /**
   * Local hard reset: drop the active turn and restore the prompt WITHOUT a
   * server round-trip. Backs the Ctrl+C escape hatch and the watchdog so a
   * turn that produces no terminal event can never wedge the UI.
   */
  forceReset: () => void
}

interface InternalState {
  attached: boolean
  unsubscribe: (() => Promise<void>) | null
  turnId: string | null
}

const dispatch = (
  state: InternalState,
  event: TurnEvent,
  sys?: (msg: string) => void,
  appendMessage?: (msg: Msg) => void,
): void => {
  switch (event.type) {
    case 'message.start':
      onMessageStart(state, event)
      return
    case 'token.delta':
      onTokenDelta(event)
      return
    case 'thinking.delta':
      // Thinking deltas surface as reasoning in the legacy path; the typed
      // chat path routes through the same controller so /thinking overlays
      // behave consistently across both paths.
      turnController.recordReasoningDelta(event.payload.text)
      return
    case 'tool.start':
      onToolStart(event)
      return
    case 'tool.progress':
      // No-op for v0.1 chat path; createGatewayEventHandler handles previews
      // for the legacy bus and we don't want a parallel preview channel.
      return
    case 'tool.complete':
      onToolComplete(event)
      return
    case 'message.complete':
      onMessageComplete(state, event, appendMessage)
      return
    case 'error':
      onError(state, event, sys)
      return
    case 'cron.delivered': {
      if (sys) {
        const { name, text, fired_at } = event.payload
        const tag = fired_at ? `${name} @ ${fired_at}` : name
        sys(`─── ⏰ ${tag} ───\n${text}\n${'─'.repeat(40)}`)
      }
      return
    }
    default: {
      // Exhaustiveness — if a new TurnEvent variant lands the type-checker
      // will complain here, forcing this file to be updated.
      const exhaustive: never = event
      void exhaustive
    }
  }
}

const onMessageStart = (state: InternalState, ev: MessageStartEvent): void => {
  state.turnId = ev.payload.turn_id
  turnController.startMessage()
  patchUiState({ status: 'running…' })
}

const onTokenDelta = (ev: TokenDeltaEvent): void => {
  turnController.recordMessageDelta({ text: ev.payload.text })
}

const onToolStart = (ev: ToolStartEvent): void => {
  const { tool_call_id, name, arguments: args } = ev.payload
  // Render a short context line from the first scalar argument value so the
  // active-tool list shows useful preview text without leaking the full
  // argument blob into the UI.
  const previewKey = Object.keys(args)[0]
  const previewVal = previewKey !== undefined ? args[previewKey] : undefined
  const context =
    typeof previewVal === 'string'
      ? previewVal
      : previewVal !== undefined
        ? JSON.stringify(previewVal)
        : ''
  turnController.recordToolStart(tool_call_id, name, context)
}

const onToolComplete = (ev: ToolCompleteEvent): void => {
  const { tool_call_id, result_preview, truncated } = ev.payload
  const summary = truncated ? `${result_preview} (truncated)` : result_preview
  turnController.recordToolComplete(tool_call_id, undefined, undefined, summary)
}

const onMessageComplete = (
  state: InternalState,
  ev: MessageCompleteEvent,
  appendMessage?: (msg: Msg) => void,
): void => {
  state.turnId = null
  // The typed message.complete carries `{turn_id, usage}` per CAP-CHAT-1
  // wire shape (B1 fix); the assistant content is reconstructed from the
  // `bufRef` accumulated via token.delta. recordMessageComplete reads bufRef
  // when payload.text is omitted and returns the final message list that
  // the caller must commit into history — without this the streamed tokens
  // appear during the turn but vanish when the turn closes.
  if (ev.payload.usage) {
    patchUiState(s => ({ ...s, usage: { ...s.usage, ...ev.payload.usage } }))
  }
  const { finalMessages, finalText, wasInterrupted } =
    turnController.recordMessageComplete({})
  if (!wasInterrupted && appendMessage) {
    const msgs: Msg[] =
      finalMessages.length > 0 ? finalMessages : [{ role: 'assistant', text: finalText }]
    msgs.forEach(appendMessage)
  }
  patchUiState({ status: 'ready' })
}

const onError = (state: InternalState, ev: ErrorEvent, sys?: (msg: string) => void): void => {
  const { reason, message, code } = ev.payload
  state.turnId = null
  if (reason === 'cancelled_by_client') {
    restoreInputPrompt()
    return
  }
  // Non-cancellation error: surface a sys note, idle the turn, and reset
  // the live anchor so the user can submit again.
  if (sys) {
    sys(`error: ${message} (code=${code})`)
  }
  turnController.recordError()
  patchUiState({ busy: false, status: `error: ${message.slice(0, 80)}` })
  patchTurnState({ activity: [], outcome: '' })
}

const restoreInputPrompt = (): void => {
  // Mirror the visible end-state of turnController.interruptTurn without
  // routing through the legacy `session.interrupt` RPC: drop streaming state,
  // clear pending tools, release `busy`, and settle status. The
  // 'interrupted' status hint is consistent with the legacy interrupt path
  // so users see the same affordance regardless of which chat path is live.
  turnController.idle()
  turnController.clearReasoning()
  turnController.clearStatusTimer()
  patchUiState({ busy: false, status: 'interrupted' })
  patchTurnState({ activity: [], outcome: '' })
  // Reset to 'ready' after the brief cooldown window so the prompt looks
  // settled if the user is just watching.
  setTimeout(() => {
    patchUiState({ status: 'ready' })
  }, 800)
}

export const createChatStream = (opts: ChatStreamOptions): ChatStreamHandle => {
  const state: InternalState = {
    attached: false,
    unsubscribe: null,
    turnId: null,
  }

  const watchdogMs = opts.watchdogMs ?? DEFAULT_WATCHDOG_MS
  let watchdog: ReturnType<typeof setTimeout> | null = null
  // True between the start of `send` and either the turn.send accept resolving
  // OR the first inbound event — i.e. while we are still waiting for the
  // server's acknowledgement. Lets the ack watchdog recover a hung turn.send
  // (RPC never returns), when `turnId` has not been set yet.
  let sendInFlight = false

  const clearWatchdog = (): void => {
    if (watchdog !== null) {
      clearTimeout(watchdog)
      watchdog = null
    }
  }

  const forceReset = (): void => {
    // Local hard escape: drop the turn and restore the prompt WITHOUT waiting
    // for any server event. Backs the watchdog and the Ctrl+C escape hatch so
    // a turn that produces no terminal event can never wedge the UI.
    clearWatchdog()
    sendInFlight = false
    state.turnId = null
    restoreInputPrompt()
  }

  const armAckWatchdog = (): void => {
    clearWatchdog()
    // CONTRACT — server-ack liveness ONLY. This watchdog measures the window
    // [send → first inbound event], where the server emits a pre-LLM
    // `message.start` (an "accepted, working" ack) before any model work. The
    // first inbound event MUST disarm it (see attach), so it never measures LLM
    // first-token latency — which is routinely > 10s and is NOT a fault. It is
    // armed BEFORE `await turn.send` and never re-armed after a clear, so the
    // same-packet accept/message.start race cannot leave it armed on an already
    // started stream (the false positive). If it ever fires, the
    // subscription is delivering nothing or turn.send hung — recover the input.
    watchdog = setTimeout(() => {
      if (!sendInFlight && state.turnId === null) {
        return
      }
      if (opts.sys) {
        opts.sys('turn produced no response — input restored (press Enter to retry)')
      }
      forceReset()
    }, watchdogMs)
  }

  const attach = async (): Promise<void> => {
    if (state.attached) {
      return
    }
    const params: TurnSubscribeParams = { session_key: opts.sessionKey }
    // The result shape `{subscription_id, unsubscribe}` is encoded structurally
    // in the ChatStreamRpcClient return type — we only need to pin the event +
    // params types to keep the dispatch callback narrowed.
    const result = await opts.rpcClient.subscribe<TurnEvent, TurnSubscribeParams>(
      'turn.subscribe',
      params,
      (event) => {
        // Any inbound event is the server ack proving the subscription is live
        // → disarm the ack watchdog. Terminal events additionally reset turn
        // state inside dispatch().
        clearWatchdog()
        dispatch(state, event, opts.sys, opts.appendMessage)
      },
      { unsubscribeMethod: 'turn.unsubscribe' },
    )
    state.unsubscribe = result.unsubscribe
    state.attached = true
  }

  const detach = async (): Promise<void> => {
    if (!state.attached) {
      return
    }
    clearWatchdog()
    sendInFlight = false
    const u = state.unsubscribe
    state.unsubscribe = null
    state.attached = false
    state.turnId = null
    if (u) {
      await u()
    }
  }

  const send = async (content: string): Promise<TurnSendResult> => {
    if (state.turnId || sendInFlight) {
      throw new Error('turn already in progress — wait for message.complete or cancel first')
    }
    const params: TurnSendParams = {
      session_key: opts.sessionKey,
      content,
    }
    // Arm BEFORE the await so the ack watchdog covers a hung turn.send and so
    // the same-packet accept/message.start race always finds it armed (the
    // event's disarm lands on a live timer). It is NOT re-armed below.
    sendInFlight = true
    armAckWatchdog()
    let result: TurnSendResult
    try {
      result = await opts.rpcClient.rpc<TurnSendResult, TurnSendParams>('turn.send', params)
    } catch (err) {
      sendInFlight = false
      clearWatchdog()
      throw err
    }
    sendInFlight = false
    // turn_id is recorded on `message.start` rather than here — the server's
    // accepted turn_id is authoritative, but we cache result.turn_id so
    // `isTurnActive()` returns true between send-accept and message.start. The
    // watchdog stays armed from before the await (no re-arm) until the first
    // inbound event disarms it; a rejected turn disarms it here.
    if (result.accepted) {
      state.turnId = result.turn_id
    } else {
      clearWatchdog()
    }
    return result
  }

  const cancel = async (): Promise<void> => {
    if (!state.turnId) {
      return
    }
    await opts.rpcClient.rpc<{ cancelled: boolean }, { session_key: string }>('turn.cancel', {
      session_key: opts.sessionKey,
    })
    // We do NOT clear state.turnId here — the server is expected to emit an
    // `error(reason=cancelled_by_client)` event that drives the actual
    // UI-state reset via dispatch(). Clearing locally would race with the
    // event delivery and leave the turn-active guard inconsistent.
  }

  const isTurnActive = (): boolean => state.turnId !== null

  return { attach, detach, send, cancel, isTurnActive, forceReset }
}
