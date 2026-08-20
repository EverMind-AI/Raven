// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Pulling the session's sub-agent instances into `$directChat`.
//
// Separate from the store so the store stays free of RPC, and separate from the
// event handler so the session lifecycle can call it too: the strip has to be
// right at startup and after a session switch, not only after the first
// sub-agent event of the session.

import type { SubagentsInstanceHistoryResult, SubagentsInstancesResult } from '../rpc/generated.js'
import type { Msg } from '../types.js'
import type { DirectTargetRef } from './directChatStore.js'

import { directKey, getDirectTranscript, patchDirectChat, setDirectTranscript } from './directChatStore.js'

/** Structurally `GatewayRpc`, restated so a test can pass a plain function. */
type Rpc = <T extends object>(
  method: string,
  params?: Record<string, unknown>,
  opts?: { quiet?: boolean }
) => Promise<null | T>

/**
 * Replace the strip with what the runtime reports for this session.
 *
 * Failure is swallowed: this backs a status band, and an error line the user
 * cannot act on is worse. An absent session key clears the strip rather than
 * leaving the previous session's rows on it.
 *
 * A failed read leaves the strip as it was, rather than emptying it. A quiet
 * rpc answers `null` for "the call failed" and for "there is nothing", and
 * treating those alike made every chip vanish on one dropped refresh -- the
 * instances were still there, and the next refresh brought them all back,
 * which is exactly what makes it read as a display glitch rather than a
 * failure.
 */
export const fetchInstances = async (rpc: Rpc, sessionKey: null | string): Promise<void> => {
  if (!sessionKey) {
    patchDirectChat({ instances: [], pendingHandoffCount: 0 })
    return
  }

  try {
    const r = await rpc<SubagentsInstancesResult>('subagents.instances', { session_key: sessionKey }, { quiet: true })

    if (!r?.instances) {
      return
    }

    patchDirectChat({ instances: r.instances, pendingHandoffCount: r.pending_handoff_count ?? 0 })
  } catch {
    // Best-effort.
  }
}

/**
 * Load one instance's past turns, once.
 *
 * The records on disk are the only memory of a direct chat that survives a TUI
 * restart -- they are deliberately absent from the session transcript. Skipped
 * when the transcript already holds something, so a reload cannot drop a turn
 * that streamed in since.
 */
export const fetchDirectHistory = async (rpc: Rpc, sessionKey: null | string, target: DirectTargetRef) => {
  const key = directKey(target.agent, target.handle)

  if (!sessionKey || getDirectTranscript(key).length > 0) {
    return
  }

  try {
    const r = await rpc<SubagentsInstanceHistoryResult>(
      'subagents.instance.history',
      { agent: target.agent, handle: target.handle, session_key: sessionKey },
      { quiet: true }
    )

    if (r?.turns === undefined || getDirectTranscript(key).length > 0) {
      return
    }

    setDirectTranscript(
      key,
      r.turns.map((t): Msg => ({ role: t.role, text: t.content }))
    )
  } catch {
    // Best-effort: an unreadable record must not block entering the chat.
  }
}

// Bound once by the component that owns the RPC handle. Both event paths need
// to trigger a refresh -- `subagent.*` arrives on the legacy gateway bus and
// `dag.*` only on the typed chat stream -- and neither of them holds a gateway
// rpc of its own. Binding here is what keeps a third caller from having to
// plumb one through.
let boundRpc: null | Rpc = null
let boundSid: (() => null | string) | null = null
let refreshTimer: null | ReturnType<typeof setTimeout> = null

/**
 * Bind the handle both event paths refresh through. Returns its own unbind.
 *
 * The unbind clears only while this binding is still the current one: React
 * runs an effect's cleanup *after* its replacement has run, so an
 * unconditional clear there would drop every refresh until the next re-bind.
 */
let bindSeq = 0

export const bindInstanceRefresh = (rpc: Rpc, getSid: () => null | string) => {
  // A sequence rather than an identity check on `rpc`: a re-bind can legitimately
  // pass the same function object, and comparing it would let the stale cleanup
  // clear a live binding.
  const mine = ++bindSeq
  boundRpc = rpc
  boundSid = getSid

  return () => {
    if (bindSeq === mine) {
      boundRpc = null
      boundSid = null
    }
  }
}

/** Test seam: drop the binding whoever holds it. */
export const resetInstanceRefresh = () => {
  boundRpc = null
  boundSid = null

  if (refreshTimer !== null) {
    clearTimeout(refreshTimer)
    refreshTimer = null
  }
}

/**
 * Re-read the strip after something moved the registry.
 *
 * Coalesced, because a fan-out fires many events at once; event-driven rather
 * than polled, because the registry is a JSON file with no change notification.
 */
export const scheduleInstanceRefresh = (): void => {
  if (refreshTimer !== null || boundRpc === null) {
    return
  }

  refreshTimer = setTimeout(() => {
    refreshTimer = null

    if (boundRpc !== null) {
      void fetchInstances(boundRpc, boundSid?.() ?? null)
    }
  }, 250)
}
