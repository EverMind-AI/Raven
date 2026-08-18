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
  TurnSubscribeParams
} from '../rpc/index.js'
import type { Msg } from '../types.js'

import { t } from '../i18n/index.js'
import { argPreview } from '../lib/toolArgs.js'
import {
  appendDirectDelta,
  appendDirectMessage,
  clearRunning,
  clearRunningKey,
  directKey,
  getDirectChat,
  MAIN_VIEW_KEY,
  markRunning,
  viewKeyOf
} from './directChatStore.js'
import { scheduleInstanceRefresh } from './directChatSync.js'
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
    opts?: { unsubscribeMethod?: string }
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
  /**
   * The live turn id per view key (`viewKeyOf`).
   *
   * Not one slot: each instance runs on its own lane server-side, so several
   * turns are in flight at once. One slot made `send` refuse the moment any
   * turn was running -- switching to a second instance and typing threw
   * "turn already in progress" locally and never reached the server, which
   * reads as the instance never answering.
   */
  turns: Map<string, string>
}

/**
 * The instance a turn event belongs to, or null for the main conversation.
 *
 * Read off the event, never off `$directChat.active`: Esc returns to the main
 * agent while a direct turn is still streaming, so "what is on screen" and
 * "what this text belongs to" routinely disagree. Only four variants can carry
 * a tag -- a direct turn emits one reply and no tool or reasoning output.
 */
const targetOf = (event: TurnEvent): null | { agent: string; handle: string } => {
  switch (event.type) {
    case 'message.start':
    case 'token.delta':
    case 'message.complete':
    case 'error':
      return event.payload.target ?? null
    default:
      return null
  }
}

/**
 * A direct-chat turn's events, which never touch the main transcript.
 *
 * The whole point of a direct chat is that these exchanges stay out of the main
 * agent's context, so they accumulate in the instance's own transcript and the
 * main agent learns of them only through the runtime's handoff block.
 */
const dispatchDirect = (
  state: InternalState,
  event: TurnEvent,
  target: { agent: string; handle: string },
  sys?: (msg: string) => void
): void => {
  const key = directKey(target.agent, target.handle)

  switch (event.type) {
    case 'message.start':
      state.turns.set(viewKeyOf(target), event.payload.turn_id)
      markRunning(target)
      patchUiState({ status: `${target.agent}/${target.handle}…` })
      // So the chip picks up its running dot now rather than at turn end. The
      // status this reads is only correct because the turn indexes itself under
      // the instance (SubagentManager._hold_instance_slot); without that it
      // would come back reconciled to 'interrupted'.
      scheduleInstanceRefresh()
      return
    case 'token.delta':
      appendDirectDelta(key, 'assistant', event.payload.text)
      return
    case 'message.complete':
      // No recordMessageComplete: that commits turnController's buffer into the
      // main transcript, and this turn never filled it.
      state.turns.delete(viewKeyOf(target))
      // Not `patchUiState({busy: false})`: this turn is one of several that may
      // be in flight, and the user may be watching a different one. `markRunning`
      // / `clearRunning` own that projection.
      clearRunning(target)
      patchUiState({ status: 'ready' })
      // A direct turn moves its own instance's status too.
      scheduleInstanceRefresh()
      return
    case 'error': {
      state.turns.delete(viewKeyOf(target))
      clearRunning(target)
      const { code, message, reason } = event.payload
      appendDirectMessage(key, {
        role: 'system',
        text: reason === 'cancelled_by_client' ? 'interrupted' : `error: ${message} (code=${code})`
      })
      patchUiState({ status: 'ready' })
      sys?.(`${target.agent}/${target.handle}: ${message}`)
      return
    }
    default:
      return
  }
}

/** Tools whose whole purpose is to put a new sub-agent instance in the session. */
const DISPATCH_TOOLS = new Set(['spawn', 'run_subagent_dag'])


// Render an ISO timestamp for the cron.missed summary block: local HH:MM
// when the reminder was scheduled today, MM-DD HH:MM otherwise - a missed
// notice's whole point is how long ago, and a bare "09:00" after a weekend
// away reads like this morning. Falls back to the raw string when
// unparseable.
const formatScheduledAt = (iso: string): string => {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) {
    return iso
  }
  const hhmm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  const now = new Date()
  const sameDay =
    d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate()
  if (sameDay) {
    return hhmm
  }
  return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${hhmm}`
}

const dispatch = (
  state: InternalState,
  event: TurnEvent,
  sys?: (msg: string) => void,
  appendMessage?: (msg: Msg) => void
): void => {
  const target = targetOf(event)

  if (target !== null) {
    dispatchDirect(state, event, target, sys)
    return
  }

  switch (event.type) {
    case 'message.start':
      onMessageStart(state, event)
      return
    case 'episode.start':
      turnController.recordEpisodeStart(event.payload.index)
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

      // The runtime emits no subagent.* event of any kind, so a dispatch tool
      // starting is the earliest signal that the session is about to have a new
      // instance in it.
      if (DISPATCH_TOOLS.has(event.payload.name)) {
        scheduleInstanceRefresh()
      }

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
      // The backstop: a turn can register an instance without any tool this
      // knows about, and a status only reaches its terminal value at the end.
      scheduleInstanceRefresh()
      return
    case 'error':
      onError(state, event, sys, appendMessage)
      return
    case 'cron.delivered': {
      if (sys) {
        const { name, text, fired_at } = event.payload
        const tag = fired_at ? `${name} @ ${fired_at}` : name
        sys(`─── ⏰ ${tag} ───\n${text}\n${'─'.repeat(40)}`)
      }
      return
    }
    case 'notice': {
      // Runtime prose, not the model's: never merged into the streamed answer.
      // The sentence is picked here so it follows the reader's locale, with
      // the blocking tool's own first line underneath when one was given.
      if (sys) {
        const said = t(`gui.notice.${event.payload.kind}`, event.payload.kind)
        const detail = event.payload.detail
        sys(detail ? `${said}\n${detail}` : said)
      }
      return
    }
    case 'subagent.delivered': {
      // The seam where a delegated run's result re-entered the turn; without
      // it the retelling that follows reads as the model speaking unprompted.
      if (sys) {
        const key = event.payload.status === 'error' ? 'gui.deleg.delivered_err' : 'gui.deleg.delivered'
        sys(`↩ ${event.payload.label} — ${t(key, key)}`)
      }
      return
    }
    case 'dag.run_started':
    case 'dag.node_updated':
    case 'dag.run_completed':
      // run_subagent_dag's fan-out progress. One controller call per frame keeps
      // the fold in one place (see turnController.recordDagEvent).
      turnController.recordDagEvent(event)
      // A DAG dispatch registers instances without emitting a single
      // `subagent.*` event, so these are the only signal the strip gets that a
      // fan-out put new instances in the session.
      scheduleInstanceRefresh()
      return

    case 'cron.missed': {
      if (sys) {
        const { count, items } = event.payload
        const lines = items.map(item => `${item.name} — scheduled ${formatScheduledAt(item.scheduled_at)}: ${item.message}`)
        const noun = count === 1 ? 'reminder' : 'reminders'
        sys(`─── ⏰ missed ${count} ${noun} ───\n${lines.join('\n')}\n${'─'.repeat(40)}`)
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
  state.turns.set(MAIN_VIEW_KEY, ev.payload.turn_id)
  markRunning(null)
  turnController.startMessage()
  patchUiState({ status: 'running…' })
}

const onTokenDelta = (ev: TokenDeltaEvent): void => {
  turnController.recordMessageDelta({ text: ev.payload.text })
}

const onToolStart = (ev: ToolStartEvent): void => {
  const { tool_call_id, name, arguments: args, display } = ev.payload
  // Prefer the tool-authored call label; else preview the "what" of the call
  // (query/question/command), skipping numeric flags and raw JSON. See lib/toolArgs.
  turnController.recordToolStart(tool_call_id, name, display ?? argPreview(args))
}

const onToolComplete = (ev: ToolCompleteEvent): void => {
  const { tool_call_id, result_preview, truncated } = ev.payload
  const summary = truncated ? `${result_preview} (truncated)` : result_preview
  turnController.recordToolComplete(tool_call_id, undefined, undefined, summary)
}

const onMessageComplete = (
  state: InternalState,
  ev: MessageCompleteEvent,
  appendMessage?: (msg: Msg) => void
): void => {
  state.turns.delete(MAIN_VIEW_KEY)
  // The typed message.complete carries `{turn_id, usage}` per CAP-CHAT-1
  // wire shape (B1 fix); the assistant content is reconstructed from the
  // `bufRef` accumulated via token.delta. recordMessageComplete reads bufRef
  // when payload.text is omitted and returns the final message list that
  // the caller must commit into history — without this the streamed tokens
  // appear during the turn but vanish when the turn closes.
  if (ev.payload.usage) {
    patchUiState(s => ({ ...s, usage: { ...s.usage, ...ev.payload.usage } }))
  }
  clearRunning(null)
  const { finalMessages, finalText, wasInterrupted } = turnController.recordMessageComplete({})
  if (!wasInterrupted && appendMessage) {
    const msgs: Msg[] = finalMessages.length > 0 ? finalMessages : [{ role: 'assistant', text: finalText }]
    msgs.forEach(appendMessage)
  }
  patchUiState({ status: 'ready' })
}

const onError = (
  state: InternalState,
  ev: ErrorEvent,
  sys?: (msg: string) => void,
  appendMessage?: (msg: Msg) => void
): void => {
  const { reason, message, code, detail } = ev.payload
  state.turns.delete(MAIN_VIEW_KEY)
  clearRunning(null)
  if (reason === 'cancelled_by_client') {
    restoreInputPrompt(appendMessage, sys)
    return
  }
  // Non-cancellation error: surface a sys note, idle the turn, and reset
  // the live anchor so the user can submit again. Append the real failure
  // detail (e.g. the underlying exception) when present, so a generic
  // `turn_failed` code is not the only thing the user sees.
  if (sys) {
    const extra = detail ? `: ${detail.split('\n')[0].slice(0, 200)}` : ''
    sys(`error: ${message} (code=${code})${extra}`)
  }
  turnController.recordError()
  patchUiState({ status: `error: ${message.slice(0, 80)}` })
  patchTurnState({ activity: [], outcome: '' })
}

const restoreInputPrompt = (appendMessage?: (msg: Msg) => void, sys?: (msg: string) => void): void => {
  // Mirror the visible end-state of turnController.interruptTurn without
  // routing through the legacy `session.interrupt` RPC: preserve the streamed
  // content into the transcript (shared finalize), drop streaming state,
  // release `busy`, and settle status. The 'interrupted' status hint is
  // consistent with the legacy interrupt path so users see the same
  // affordance regardless of which chat path is live.
  turnController.finalizeInterruptedTurn({ appendMessage, sys })
  turnController.clearStatusTimer()
  patchUiState({ status: 'interrupted' })
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
    turns: new Map()
  }

  const watchdogMs = opts.watchdogMs ?? DEFAULT_WATCHDOG_MS
  // Both keyed by view key, for the same reason `state.turns` is: two views can
  // be waiting on an ack at once, and one view's first event must not disarm
  // another view's watchdog.
  const watchdogs = new Map<string, ReturnType<typeof setTimeout>>()
  // Holds a view between the start of `send` and either the turn.send accept
  // resolving OR the first inbound event — i.e. while we are still waiting for
  // the server's acknowledgement. Lets the ack watchdog recover a hung
  // turn.send (RPC never returns), when no turn id has been set yet.
  const sending = new Set<string>()

  const clearWatchdog = (key: string): void => {
    const timer = watchdogs.get(key)
    if (timer !== undefined) {
      clearTimeout(timer)
      watchdogs.delete(key)
    }
  }

  const forceReset = (): void => {
    // Local hard escape: drop the turn and restore the prompt WITHOUT waiting
    // for any server event. Backs the watchdog and the Ctrl+C escape hatch so
    // a turn that produces no terminal event can never wedge the UI.
    //
    // The main agent's turn alone: a sub-agent's is not cancellable (spec D3)
    // and a local escape must not pretend one stopped.
    clearWatchdog(MAIN_VIEW_KEY)
    clearRunning(null)
    sending.delete(MAIN_VIEW_KEY)
    state.turns.delete(MAIN_VIEW_KEY)
    restoreInputPrompt(opts.appendMessage, opts.sys)
  }

  const armAckWatchdog = (key: string): void => {
    clearWatchdog(key)
    // CONTRACT — server-ack liveness ONLY. This watchdog measures the window
    // [send → first inbound event], where the server emits a pre-LLM
    // `message.start` (an "accepted, working" ack) before any model work. The
    // first inbound event MUST disarm it (see attach), so it never measures LLM
    // first-token latency — which is routinely > 10s and is NOT a fault. It is
    // armed BEFORE `await turn.send` and never re-armed after a clear, so the
    // same-packet accept/message.start race cannot leave it armed on an already
    // started stream (the false positive). If it ever fires, the
    // subscription is delivering nothing or turn.send hung — recover the input.
    watchdogs.set(
      key,
      setTimeout(() => {
        watchdogs.delete(key)
        if (!sending.has(key) && !state.turns.has(key)) {
          return
        }
        if (opts.sys) {
          opts.sys('turn produced no response — input restored (press Enter to retry)')
        }
        if (key === MAIN_VIEW_KEY) {
          forceReset()
          return
        }
        // A sub-agent's turn is not cancellable, so there is nothing to unwind
        // beyond this client's own bookkeeping: release the view and say so.
        sending.delete(key)
        state.turns.delete(key)
        clearRunningKey(key)
      }, watchdogMs)
    )
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
      event => {
        // Any inbound event is the server ack proving the subscription is live
        // → disarm that view's ack watchdog. Only that view's: another view may
        // still be waiting for an ack of its own. Terminal events additionally
        // reset turn state inside dispatch().
        clearWatchdog(viewKeyOf(targetOf(event)))
        dispatch(state, event, opts.sys, opts.appendMessage)
      },
      { unsubscribeMethod: 'turn.unsubscribe' }
    )
    state.unsubscribe = result.unsubscribe
    state.attached = true
  }

  const detach = async (): Promise<void> => {
    if (!state.attached) {
      return
    }
    for (const key of [...watchdogs.keys()]) {
      clearWatchdog(key)
    }
    sending.clear()
    const u = state.unsubscribe
    state.unsubscribe = null
    state.attached = false
    state.turns.clear()
    if (u) {
      await u()
    }
  }

  const send = async (content: string): Promise<TurnSendResult> => {
    // Per view: several instances answer at once, and refusing on "any turn is
    // running" is what made switching to a second instance and typing do
    // nothing at all -- the throw never left this process.
    const view = viewKeyOf(getDirectChat().active)
    if (state.turns.has(view) || sending.has(view)) {
      throw new Error('turn already in progress — wait for message.complete or cancel first')
    }
    // Omitted entirely on the main conversation rather than sent as null:
    // TurnSendParams forbids extras but not nulls, so both validate -- and an
    // absent key keeps the wire shape identical to every existing client's.
    const active = getDirectChat().active
    const params: TurnSendParams = {
      session_key: opts.sessionKey,
      content,
      ...(active === null ? {} : { target: active })
    }
    // Arm BEFORE the await so the ack watchdog covers a hung turn.send and so
    // the same-packet accept/message.start race always finds it armed (the
    // event's disarm lands on a live timer). It is NOT re-armed below.
    sending.add(view)
    markRunning(active)
    armAckWatchdog(view)
    let result: TurnSendResult
    try {
      result = await opts.rpcClient.rpc<TurnSendResult, TurnSendParams>('turn.send', params)
    } catch (err) {
      sending.delete(view)
      clearRunning(active)
      clearWatchdog(view)
      throw err
    }
    sending.delete(view)
    // turn_id is recorded on `message.start` rather than here — the server's
    // accepted turn_id is authoritative, but we cache result.turn_id so
    // `isTurnActive()` returns true between send-accept and message.start. The
    // watchdog stays armed from before the await (no re-arm) until the first
    // inbound event disarms it; a rejected turn disarms it here.
    if (result.accepted) {
      state.turns.set(view, result.turn_id)
    } else {
      clearRunning(active)
      clearWatchdog(view)
    }
    return result
  }

  const cancel = async (): Promise<void> => {
    // The main agent's turn. `turn.cancel` names a session and means exactly
    // that; a sub-agent's turn is not cancellable (spec D3), so asking here
    // would be a request the server is right to answer with "nothing to do".
    if (!state.turns.has(MAIN_VIEW_KEY)) {
      return
    }
    await opts.rpcClient.rpc<{ cancelled: boolean }, { session_key: string }>('turn.cancel', {
      session_key: opts.sessionKey
    })
    // We do NOT clear the turn here — the server is expected to emit an
    // `error(reason=cancelled_by_client)` event that drives the actual
    // UI-state reset via dispatch(). Clearing locally would race with the
    // event delivery and leave the turn-active guard inconsistent.
  }

  // Consulted by the Ctrl+C router to decide between cancel and force-reset,
  // so it answers for the turn Ctrl+C can act on -- the main agent's.
  const isTurnActive = (): boolean => state.turns.has(MAIN_VIEW_KEY)

  return { attach, detach, send, cancel, isTurnActive, forceReset }
}
