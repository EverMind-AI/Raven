// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Keeping the instance on screen up to date while it works.
//
// Its own hook rather than two effects inside `useMainApp` so that it can be
// mounted on its own in a test. This is the half of the live view that no unit
// test could reach while it lived there -- the reader and the folding were
// covered, and whether anything ever *called* them was not.

import { useEffect, useRef } from 'react'

import type { DirectTargetRef } from './directChatStore.js'

import { DIRECT_STEP_POLL_MS } from '../config/limits.js'
import { directKey } from './directChatStore.js'
import { fetchDirectHistory } from './directChatSync.js'

/** Structurally `GatewayRpc`, restated so a test can pass a plain function. */
type Rpc = <T extends object>(
  method: string,
  params?: Record<string, unknown>,
  opts?: { quiet?: boolean }
) => Promise<null | T>

/**
 * Re-read the instance on screen while it has a turn in flight.
 *
 * The steps and the text of a running turn live only in the activity the runtime
 * is collecting -- nothing of them reaches disk until the turn lands -- and the
 * event stream tags an instance on the four events of a *direct* turn, so a
 * `spawn` or a DAG node reaches this view through no other route.
 *
 * Polled rather than pushed on purpose: the snapshot is folded by the same code
 * that folds the settled record, which is what keeps a turn from re-rendering
 * into a different shape the moment it finishes.
 *
 * Only the view on screen is read. A turn the user is not looking at is caught
 * by the settled read when it ends.
 */
export const useDirectStepPoll = (
  rpc: Rpc,
  getSessionKey: () => null | string,
  target: DirectTargetRef | null,
  working: boolean
): void => {
  const viewKey = target === null ? '' : directKey(target.agent, target.handle)
  // `target` is the dependency rather than its key: the store hands out the same
  // object until the view actually changes, so this arms the interval once per
  // view. Keying on a value rebuilt each render would clear and recreate the
  // interval every time and it would never reach a tick.
  //
  // Per view, so switching between two instances cannot make one's transition
  // look like the other's.
  const wasWorking = useRef<Map<string, boolean>>(new Map())

  useEffect(() => {
    if (!working || target === null) {
      return
    }

    // At once as well as on the interval: a view switched into mid-turn would
    // otherwise show nothing until the first tick.
    void fetchDirectHistory(rpc, getSessionKey(), target, 'live')

    const id = setInterval(() => {
      void fetchDirectHistory(rpc, getSessionKey(), target, 'live')
    }, DIRECT_STEP_POLL_MS)

    return () => clearInterval(id)
  }, [getSessionKey, rpc, target, working])

  useEffect(() => {
    const was = wasWorking.current.get(viewKey) ?? false
    wasWorking.current.set(viewKey, working)

    // One read after the work stops. The poll is gone by then, and for a spawn
    // or a DAG node there is no `message.complete` addressed to this instance
    // either -- so without this the last thing on screen stays the final live
    // snapshot, whose rows the record has since replaced.
    if (target !== null && was && !working) {
      void fetchDirectHistory(rpc, getSessionKey(), target, 'settled')
    }
  }, [getSessionKey, rpc, target, viewKey, working])
}
