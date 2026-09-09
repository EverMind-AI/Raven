// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Keeping a watched spawn run's transcript up to date while it works.
//
// The spawn counterpart of `useDagNodePoll`, sharing its trace store: no event
// carries a run's steps, and `subagent.context` serves the live activity index
// mid-flight and the record on disk afterwards -- so each response is a
// complete snapshot and a reader can poll it holding no state at all.
//
// `spawn` returns as soon as the run is scheduled, so a run routinely outlives
// the turn that started it -- `idle()` keeps a working run on the live list but
// a settled one only survives pinned onto its tool row. Runs therefore come
// from two places: `turnStore`'s live list and the pinned copies in the
// transcript, merged by task id, the live one winning.

import { useEffect, useMemo } from 'react'

import type { SpawnRunState } from '../domain/spawnRun.js'
import type { SubagentContextResult } from '../rpc/index.js'

import { DAG_NODE_POLL_MS } from '../config/limits.js'
import { spawnRunSettled } from '../domain/spawnRun.js'
import { spawnTraceKey } from '../lib/spawnOpen.js'
import { getDagNodeTrace, markDagNodeReadFailure, setDagNodeTrace, settleDagNodeTrace } from './dagNodeStore.js'

/** Structurally `GatewayRpc`, restated so a test can pass a plain function. */
type Rpc = <T extends object>(
  method: string,
  params?: Record<string, unknown>,
  opts?: { quiet?: boolean }
) => Promise<null | T>

// What `SpawnRecord.finish` writes. A positive test on purpose, like the dag
// poll's: `status` is whatever meta.json last said, and reading anything
// unrecognized as "finished" would stop the poll before the run had spoken.
const TERMINAL_STATUSES: ReadonlySet<string> = new Set(['aborted', 'cancelled', 'completed', 'failed'])

/** The live runs and their pinned copies, one entry per task id, live winning. */
const mergeSpawnRuns = (live: readonly SpawnRunState[], pinned: readonly SpawnRunState[]): SpawnRunState[] => {
  const byTaskId = new Map<string, SpawnRunState>()

  for (const run of pinned) {
    byTaskId.set(run.taskId, run)
  }

  for (const run of live) {
    byTaskId.set(run.taskId, run)
  }

  return [...byTaskId.values()]
}

export const useSpawnTracePoll = (
  rpc: Rpc,
  getSessionKey: () => null | string,
  runs: readonly SpawnRunState[],
  pinnedRuns: readonly SpawnRunState[],
  openKeys: ReadonlySet<string>
): void => {
  const targets = useMemo(() => {
    const out: { callId: string; running: boolean }[] = []

    for (const run of mergeSpawnRuns(runs, pinnedRuns)) {
      // A pending run has not opened its record yet: there is nothing to read.
      if (!run.callId) {
        continue
      }

      if (getDagNodeTrace(spawnTraceKey(run.callId))?.settled) {
        continue
      }

      // Only what is open on screen. A running panel is open by default (see
      // `spawnTraceOpen`), so the live view polls without a click; a panel the
      // reader folded is a read twice a second for something nothing draws.
      if (!openKeys.has(run.taskId)) {
        continue
      }

      out.push({ callId: run.callId, running: !spawnRunSettled(run) })
    }

    return out
  }, [openKeys, pinnedRuns, runs])

  // A stable dependency, exactly as in `useDagNodePoll`: `targets` is a fresh
  // array every render, and keying the effect on it would clear and rebuild the
  // interval before it ever ticked. `running` rearms it when a run stops.
  const signature = targets.map(t => `${t.callId}:${t.running ? 1 : 0}`).join(',')

  useEffect(() => {
    if (targets.length === 0) {
      return
    }

    let live = true
    const attempted = new Set<string>()

    const read = async ({ callId }: { callId: string }) => {
      const key = spawnTraceKey(callId)
      const sid = getSessionKey()

      if (!sid) {
        return
      }

      attempted.add(key)

      try {
        // `quiet` is required rather than cosmetic: a pruned record answers
        // with an error, and without this it would print into the transcript.
        const result = await rpc<SubagentContextResult>(
          'subagent.context',
          { id: callId, session_id: sid },
          { quiet: true }
        )

        if (!live) {
          return
        }

        const status = result?.status
        const settled = typeof status === 'string' && TERMINAL_STATUSES.has(status)
        const messages = result?.messages

        if (messages && messages.length > 0) {
          setDagNodeTrace(key, messages, settled)
        } else if (status != null) {
          // Empty messages with a status is a legitimate answer (a record whose
          // prompt could not be read back); a response with neither carries no
          // news either way.
          settleDagNodeTrace(key, settled)
        }
      } catch {
        if (!live) {
          return
        }

        markDagNodeReadFailure(key)
      }
    }

    const tick = () => {
      for (const target of targets) {
        const key = spawnTraceKey(target.callId)

        // Same bounds as the dag poll: a settled run is never read again, and a
        // run not displayed as running gets one read per arming of this effect
        // -- going terminal rearms it, so the read carrying the final answer
        // still happens. A run still displayed as running has no such bound;
        // `markDagNodeReadFailure` counts the streak and settles it at the cap.
        if (getDagNodeTrace(key)?.settled) {
          continue
        }

        if (!target.running && attempted.has(key)) {
          continue
        }

        void read(target)
      }
    }

    // At once as well as on the interval: a run that starts between ticks would
    // otherwise show nothing for half a second.
    tick()

    const id = setInterval(tick, DAG_NODE_POLL_MS)

    return () => {
      live = false
      clearInterval(id)
    }
    // `signature` stands in for `targets`; see above.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [getSessionKey, rpc, signature])
}
