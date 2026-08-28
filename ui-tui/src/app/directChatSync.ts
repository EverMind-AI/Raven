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

import type { DirectTurn, SubagentsInstanceHistoryResult, SubagentsInstancesResult } from '../rpc/generated.js'
import type { Msg } from '../types.js'
import type { DirectTargetRef } from './directChatStore.js'

import { foldDirectTurns } from '../domain/directEpisodes.js'
import { messageHeightKey } from '../lib/virtualHeights.js'
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
 * Why one read of an instance's conversation is happening.
 *
 * `enter`   -- switching into the chat. Skipped when the transcript already
 *              holds something, so it cannot drop a turn that streamed in since.
 * `settled` -- a turn just ended. Replaces everything: the record now holds the
 *              turn whose steps were only a snapshot a moment ago.
 * `live`    -- a turn is running. Splices its steps in without touching the
 *              prompt the user typed or the reply streaming in beside them.
 */
export type HistoryRead = 'enter' | 'live' | 'settled'

/**
 * The rows of the turn still running, and the rows already written down.
 *
 * The runtime marks the first kind, because only it knows: they come from the
 * activity it is collecting rather than from the record. A reader cannot tell
 * them apart by shape -- that is the same shape either way, which is the point.
 */
const splitLive = (turns: DirectTurn[]) => ({
  settled: turns.filter(t => !t.live),
  live: turns.filter(t => t.live)
})

/**
 * Replace the running turn with the snapshot the read carries.
 *
 * The read carries the whole turn -- its prompt, its steps and the answer so far
 * -- so there is nothing of it left for the client to hold on to and the rows
 * are simply rebuilt. Deltas that land between two reads append to the trailing
 * message, which is the same message the next read replaces, so the text stays
 * smooth and self-correcting rather than being owned by one side.
 *
 * Rebuilt rather than spliced onto the tail: a spawn or a DAG node is a turn of
 * this instance that the client never sent, so there is no row of its own to
 * anchor on. Anchoring on the last thing the user said put such a turn's steps
 * where the *previous* turn's answer was.
 *
 * With nothing to add it does nothing, rather than treating the absence as "the
 * turn ended". A transport with no per-step visibility reports no live rows for
 * the whole of every turn, so replacing the view here would delete the prompt
 * and the reply it is streaming. Ending a turn is the settled read's job.
 */
const applyLive = (key: string, settled: DirectTurn[], live: DirectTurn[]) => {
  if (live.length === 0) {
    return
  }

  replaceRows(key, [...foldDirectTurns(settled), ...foldDirectTurns(live)])
}

/**
 * Put the rows on screen, keeping the object identity of the ones that did not
 * change.
 *
 * A row's identity in the transcript *is* its object identity: the renderer
 * mints a key per object, so a fresh object for an unchanged row unmounts and
 * remounts it -- discarding the component state that holds what the reader
 * expanded. Rebuilding every row every few hundred milliseconds therefore
 * collapsed an open tool call on each poll, and re-mounted the whole view with
 * it.
 *
 * `messageHeightKey` is the comparison because it is already this codebase's
 * answer to "would this row render the same", used by the height cache for the
 * same reason.
 */
const replaceRows = (key: string, next: Msg[]) => {
  const prev = getDirectTranscript(key)

  setDirectTranscript(
    key,
    next.map((row, index) => {
      const before = prev[index]

      return before !== undefined && messageHeightKey(before) === messageHeightKey(row) ? before : row
    })
  )
}

/**
 * Read one instance's conversation and put it on screen.
 *
 * The records on disk are the only memory of a direct chat that survives a TUI
 * restart -- they are deliberately absent from the session transcript.
 */
export const fetchDirectHistory = async (
  rpc: Rpc,
  sessionKey: null | string,
  target: DirectTargetRef,
  read: HistoryRead = 'enter'
) => {
  const key = directKey(target.agent, target.handle)

  if (!sessionKey || (read === 'enter' && getDirectTranscript(key).length > 0)) {
    return
  }

  try {
    const r = await rpc<SubagentsInstanceHistoryResult>(
      'subagents.instance.history',
      { agent: target.agent, handle: target.handle, session_key: sessionKey },
      { quiet: true }
    )

    if (r?.turns === undefined || (read === 'enter' && getDirectTranscript(key).length > 0)) {
      return
    }

    // A settled read that finds no record cannot correct anything: the rows on
    // screen came from somewhere, and an empty file is not a better account of
    // the conversation than they are.
    if (read === 'settled' && r.turns.length === 0 && getDirectTranscript(key).length > 0) {
      return
    }

    const { live, settled } = splitLive(r.turns)

    // Folded into episodes rather than mapped row-for-row, so a direct chat
    // renders through the transcript's own renderer -- same folds, same detail
    // blocks, same verbs as a Raven turn. One folder for both reads, so what is
    // on screen while a turn runs and what replaces it when the turn lands
    // cannot drift apart.
    if (read === 'live') {
      applyLive(key, settled, live)
      return
    }

    replaceRows(key, foldDirectTurns(settled))
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

/**
 * Re-read one instance's conversation now that its turn has ended.
 *
 * Through the bound handle for the reason `bindInstanceRefresh` exists: the
 * event path that knows a turn ended holds no rpc of its own.
 *
 * Without this the steps of the turn the user just watched were on disk and
 * never shown: the only read was on entering the view, and it declines to
 * replace a transcript that already holds something -- which a turn that just
 * ran always does.
 */
export const settleDirectHistory = (target: DirectTargetRef): void => {
  if (boundRpc === null) {
    return
  }

  void fetchDirectHistory(boundRpc, boundSid?.() ?? null, target, 'settled')
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
