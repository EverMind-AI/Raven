// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Keeping a watched DAG node's transcript up to date while it works.
//
// The counterpart of `useDirectStepPoll`, and a read rather than a stream for
// the same reason: no event carries a node's steps. `dag.node` falls back to the
// activity the runner is publishing when nothing has reached disk yet, and the
// collector republishes its whole transcript on every frame -- so each response
// is a complete snapshot and a reader can poll it holding no state at all.
//
// `run_subagent_dag` returns as soon as it launches the run, so a node
// routinely keeps running after the turn that started it has ended -- `idle()`
// clears the live `dagRuns` right at that boundary. So runs come from two
// places: `turnStore`'s live list, and the copies pinned onto their tool rows
// in the transcript, which outlive it. They are merged by `runId`, the live
// one winning where both name one.

import { useEffect, useMemo } from 'react'

import type { DagRunState } from '../domain/dagRun.js'
import type { DagNodeResult } from '../rpc/index.js'

import { DAG_NODE_POLL_MS, DAG_TRACE_OUTPUT_CHARS } from '../config/limits.js'
import { dagNodeKey } from '../lib/dagOpenNodes.js'
import { getDagNodeTrace, markDagNodeReadFailure, setDagNodeTrace, settleDagNodeTrace } from './dagNodeStore.js'

/** Structurally `GatewayRpc`, restated so a test can pass a plain function. */
type Rpc = <T extends object>(
  method: string,
  params?: Record<string, unknown>,
  opts?: { quiet?: boolean }
) => Promise<null | T>

const RUNNING: DagRunState['nodes'][number]['status'] = 'running'

// The four the runner records as final. A positive test on purpose: `status`
// is null until the run writes its manifest, and the poll's first read of a
// node lands inside that window -- a negative test would read "not running"
// as "finished" and stop the poll before the node had said anything.
const TERMINAL_STATUSES: ReadonlySet<string> = new Set(['cancelled', 'completed', 'failed', 'skipped'])

/** The live runs and their pinned copies, one entry per `runId`, live winning. */
const mergeDagRuns = (live: readonly DagRunState[], pinned: readonly DagRunState[]): DagRunState[] => {
  const byRunId = new Map<string, DagRunState>()

  for (const run of pinned) {
    byRunId.set(run.runId, run)
  }

  for (const run of live) {
    byRunId.set(run.runId, run)
  }

  return [...byRunId.values()]
}

export const useDagNodePoll = (
  rpc: Rpc,
  getSessionKey: () => null | string,
  runs: readonly DagRunState[],
  pinnedRuns: readonly DagRunState[],
  openKeys: ReadonlySet<string>
): void => {
  const targets = useMemo(() => {
    const out: { nodeId: string; running: boolean; runId: string }[] = []

    for (const run of mergeDagRuns(runs, pinnedRuns)) {
      for (const node of run.nodes) {
        const key = dagNodeKey(run.runId, node.id)

        if (getDagNodeTrace(key)?.settled) {
          continue
        }

        // Only what a reader has opened. A running node used to be polled
        // whether or not anyone was looking, to feed the live line under its
        // row; that line is gone, so polling a closed node is a `dag.node` read
        // twice a second for something nothing draws.
        if (!openKeys.has(key)) {
          continue
        }

        out.push({ nodeId: node.id, running: node.status === RUNNING, runId: run.runId })
      }
    }

    return out
  }, [openKeys, pinnedRuns, runs])

  // A stable dependency: `targets` is a fresh array every render, and keying the
  // effect on it would clear and rebuild the interval before it ever ticked.
  // `running` is part of it so the interval rearms when a node stops.
  const signature = targets.map(t => `${t.runId}/${t.nodeId}:${t.running ? 1 : 0}`).join(',')

  useEffect(() => {
    if (targets.length === 0) {
      return
    }

    let live = true
    const attempted = new Set<string>()

    const read = async ({ nodeId, runId }: { nodeId: string; runId: string }) => {
      const key = dagNodeKey(runId, nodeId)

      attempted.add(key)

      try {
        // `quiet` is required rather than cosmetic: a pruned run dir answers
        // with an error, and without this it would print into the transcript --
        // which is the surface this is trying to keep readable.
        const result = await rpc<DagNodeResult>(
          'dag.node',
          {
            max_output_chars: DAG_TRACE_OUTPUT_CHARS,
            node: nodeId,
            run_id: runId,
            session_key: getSessionKey() ?? undefined
          },
          { quiet: true }
        )

        if (!live) {
          return
        }

        const status = result?.node?.status
        // A displayed status can go stale once its turn ends -- a pinned run's
        // copy is never revised -- so only the response itself can end the poll.
        const settled = typeof status === 'string' && TERMINAL_STATUSES.has(status)
        const messages = result?.node?.messages

        if (messages && messages.length > 0) {
          setDagNodeTrace(key, messages, settled)
        } else if (status !== undefined) {
          // Empty messages is a legitimate answer -- a node that never ran has
          // no prompt, transcript, output, or error to give -- and must not be
          // read as "not read yet". A response with no status at all (nothing
          // has reached disk, and no activity is registered either) is not
          // settled and is not this branch: it carries no news either way.
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
        const key = dagNodeKey(target.runId, target.nodeId)
        const held = getDagNodeTrace(key)

        // A settled node is never read again: the server already said its
        // status can never change. For a target not displayed as running,
        // `attempted` bounds it to one read per arming of this effect --
        // marked before the read starts and kept whether the read produced a
        // trace, came back empty, or threw -- so a node with nothing to give
        // (never ran) or an unreadable run dir (pruned) is not re-read
        // forever just because it never earned a settled status.
        // Deliberately not "and we hold no trace": a node that streamed while
        // running holds one, and leaving `running` is exactly when its last
        // read -- the one carrying the final answer -- is still owed. Going
        // terminal rearms this effect, so `attempted` is empty for that
        // generation and the read happens once, after which the response's
        // own terminal status settles the node for good.
        // A target still displayed as running has no such bound: a pinned
        // `running` node behind a pruned run dir throws on every read, so
        // `markDagNodeReadFailure` counts the streak in the store (surviving
        // this effect's own rearming) and settles the node once that count
        // reaches `DAG_TRACE_READ_FAILURE_CAP`, the same way a terminal status
        // would.
        if (held?.settled) {
          continue
        }

        if (!target.running && attempted.has(key)) {
          continue
        }

        void read(target)
      }
    }

    // At once as well as on the interval: a node that starts running between
    // ticks would otherwise show nothing for half a second.
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
