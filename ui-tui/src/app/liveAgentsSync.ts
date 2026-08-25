// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Pulling the session's delegated runs into `$liveAgents`.
//
// Events keep the store current while they flow; this reconcile covers the
// boundaries they cannot: cold start, a session switch, a reconnect that
// missed a terminal frame. Same bind-once pattern as `directChatSync`, and
// separate from the store so the store stays free of RPC.

import type { SubagentListResult } from '../rpc/generated.js'

import { reconcileFromList } from './liveAgentsStore.js'

/** Structurally `GatewayRpc`, restated so a test can pass a plain function. */
type Rpc = <T extends object>(
  method: string,
  params?: Record<string, unknown>,
  opts?: { quiet?: boolean }
) => Promise<null | T>

/**
 * Reconcile the store against the session's on-disk call list.
 *
 * A failed read changes nothing: a quiet rpc answers `null` for "failed" and
 * for "empty" alike, and an empty answer is only trustworthy when it really
 * came back — `reconcileFromList` is what disowns rows, so it must only run
 * on a real snapshot.
 */
export const fetchLiveAgents = async (rpc: Rpc, sessionKey: null | string): Promise<void> => {
  if (!sessionKey) {
    return
  }

  try {
    const r = await rpc<SubagentListResult>('subagent.list', { session_id: sessionKey }, { quiet: true })

    if (!r?.items) {
      return
    }

    reconcileFromList(r.items)
  } catch {
    // Best-effort: this backs a status band.
  }
}

let boundRpc: null | Rpc = null
let boundSid: (() => null | string) | null = null
let refreshTimer: null | ReturnType<typeof setTimeout> = null
let bindSeq = 0

/** Bind the handle refreshes go through. Returns its own unbind (see `bindInstanceRefresh`). */
export const bindLiveAgentsRefresh = (rpc: Rpc, getSid: () => null | string) => {
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
export const resetLiveAgentsRefresh = () => {
  boundRpc = null
  boundSid = null

  if (refreshTimer !== null) {
    clearTimeout(refreshTimer)
    refreshTimer = null
  }
}

/** Coalesced re-read, for the same reason `scheduleInstanceRefresh` coalesces. */
export const scheduleLiveAgentsRefresh = (): void => {
  if (refreshTimer !== null || boundRpc === null) {
    return
  }

  refreshTimer = setTimeout(() => {
    refreshTimer = null

    if (boundRpc !== null) {
      void fetchLiveAgents(boundRpc, boundSid?.() ?? null)
    }
  }, 300)
}
