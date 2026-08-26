// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Whether anything ever *calls* the trace read, and whether it stops calling.
// The reductions and the store are covered on their own; this is the half that
// decides whether an opened node ever fills in.
//
// Every read here is a node a reader has opened: nothing else is polled, since
// nothing else draws a trace.

import { renderSync } from '@hermes/ink'
import React from 'react'
import { PassThrough } from 'stream'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DagRunNodeStatus, DagRunState } from '../domain/dagRun.js'
import type { DagNodeDetail, DagNodeResult, TranscriptMessage } from '../rpc/index.js'

import { getDagNodeTrace, resetDagNodeTraces } from '../app/dagNodeStore.js'
import { useDagNodePoll } from '../app/useDagNodePoll.js'
import { DAG_NODE_POLL_MS, DAG_TRACE_READ_FAILURE_CAP } from '../config/limits.js'
import { dagNodeKey } from '../lib/dagOpenNodes.js'

const node = (id: string, status: DagRunNodeStatus) => ({ id, subagent: 'codex-acp', dependsOn: [], status })

const run = (...nodes: ReturnType<typeof node>[]): DagRunState => ({
  runId: 'r1',
  done: false,
  nodes
})

// `run_id` and `node` are required on the wire but nothing under test reads
// them back off the response, so a fixed neutral value stands in for both.
const nodeResult = (over: Partial<DagNodeDetail> = {}): DagNodeResult => ({
  node: {
    run_id: 'r1',
    node: 'a',
    output_chars: 0,
    output_truncated: false,
    ...over
  }
})

const answering = (messages: TranscriptMessage[]) => {
  const calls: Record<string, unknown>[] = []
  const rpc = async (_method: string, params?: Record<string, unknown>) => {
    calls.push(params ?? {})

    return nodeResult({ messages })
  }

  return { calls, rpc: rpc as never }
}

const throwing = () => {
  const calls: Record<string, unknown>[] = []
  const rpc = async (_method: string, params?: Record<string, unknown>) => {
    calls.push(params ?? {})

    throw new Error('run dir is gone')
  }

  return { calls, rpc: rpc as never }
}

// The first read still reports the node in flight; every read after reports it
// done, with the same messages -- the shape needed to prove a settle survives
// the store's dedup-on-unchanged-content, not just that a status field exists.
const finishing = (messages: TranscriptMessage[]) => {
  const calls: Record<string, unknown>[] = []
  let count = 0
  const rpc = async (_method: string, params?: Record<string, unknown>) => {
    calls.push(params ?? {})
    count++

    return nodeResult({ messages, status: count === 1 ? 'running' : 'completed' })
  }

  return { calls, rpc: rpc as never }
}

const running = (messages: TranscriptMessage[]) => {
  const calls: Record<string, unknown>[] = []
  const rpc = async (_method: string, params?: Record<string, unknown>) => {
    calls.push(params ?? {})

    return nodeResult({ messages, status: 'running' })
  }

  return { calls, rpc: rpc as never }
}

// `status` comes back `null`, not absent -- the shape `dag.node` sends before
// the run has written its manifest, with messages already flowing from the
// runner's live activity fallback. `DagNodeDetail.status` is typed as merely
// optional rather than nullable, so the literal `null` still needs this one
// narrow cast even though the rest of the response is checked for real.
const nullStatus = (messages: TranscriptMessage[]) => {
  const calls: Record<string, unknown>[] = []
  const rpc = async (_method: string, params?: Record<string, unknown>) => {
    calls.push(params ?? {})

    return nodeResult({ messages, status: null as unknown as DagNodeDetail['status'] })
  }

  return { calls, rpc: rpc as never }
}

const finishedEmpty = () => {
  const calls: Record<string, unknown>[] = []
  const rpc = async (_method: string, params?: Record<string, unknown>) => {
    calls.push(params ?? {})

    return nodeResult({ messages: [], status: 'completed' })
  }

  return { calls, rpc: rpc as never }
}

// Throws on every call except the second, which succeeds -- for proving a
// transient failure does not accumulate toward the read-failure cap.
const flaky = () => {
  const calls: Record<string, unknown>[] = []
  let count = 0
  const rpc = async (_method: string, params?: Record<string, unknown>) => {
    calls.push(params ?? {})
    count++

    if (count === 2) {
      return nodeResult({ messages: [{ role: 'assistant', text: 'still going' }], status: 'running' })
    }

    throw new Error('run dir is gone')
  }

  return { calls, rpc: rpc as never }
}

/** The one node every test below opens; only an open node is polled at all. */
const OPEN_A = new Set([dagNodeKey('r1', 'a')])

// Module-scope so it is the same function reference across every render --
// an inline closure here would change on every render and mask what a test
// keying on `runs`/`openKeys` alone is trying to isolate.
const getSessionKey = () => 's1'

const Probe = ({
  openKeys,
  pinnedRuns,
  rpc,
  runs
}: {
  openKeys: ReadonlySet<string>
  pinnedRuns: readonly DagRunState[]
  rpc: never
  runs: readonly DagRunState[]
}) => {
  useDagNodePoll(rpc, getSessionKey, runs, pinnedRuns, openKeys)

  return null
}

const mount = (
  rpc: never,
  runs: readonly DagRunState[],
  openKeys: ReadonlySet<string> = new Set(),
  pinnedRuns: readonly DagRunState[] = []
) =>
  renderSync(<Probe openKeys={openKeys} pinnedRuns={pinnedRuns} rpc={rpc} runs={runs} />, {
    stdout: new PassThrough() as never
  })

beforeEach(() => {
  resetDagNodeTraces()
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
})

describe('useDagNodePoll', () => {
  it('reads a running node at once, without waiting for a tick', async () => {
    const { calls, rpc } = answering([{ role: 'assistant', text: 'hi' }])

    mount(rpc, [run(node('a', 'running'))], OPEN_A)
    await vi.advanceTimersByTimeAsync(0)

    expect(calls).toHaveLength(1)
    expect(calls[0]).toMatchObject({ node: 'a', run_id: 'r1', session_key: 's1' })
    expect(getDagNodeTrace(dagNodeKey('r1', 'a'))).toEqual({
      failures: 0,
      messages: [{ role: 'assistant', text: 'hi' }],
      settled: false
    })
  })

  it('keeps reading it on the interval', async () => {
    const { calls, rpc } = answering([{ role: 'assistant', text: 'hi' }])

    mount(rpc, [run(node('a', 'running'))], OPEN_A)
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 2 + 10)

    expect(calls.length).toBeGreaterThanOrEqual(3)
  })

  it('keeps polling a running node across a re-render with a fresh runs array', async () => {
    const { calls, rpc } = answering([{ role: 'assistant', text: 'hi' }])
    const app = mount(rpc, [run(node('a', 'running'))], OPEN_A)

    await vi.advanceTimersByTimeAsync(0)
    expect(calls).toHaveLength(1)

    // A newly-constructed array with the same content, not the same reference --
    // the shape a real re-render hands down when the run state was rebuilt from
    // an unrelated event. The same component type, so this is a re-render and
    // not a remount: a remount would trivially restart the poll and prove nothing.
    app.rerender(<Probe openKeys={OPEN_A} pinnedRuns={[]} rpc={rpc} runs={[run(node('a', 'running'))]} />)
    await vi.advanceTimersByTimeAsync(0)

    // No new read here: a re-render that changes no dependency must not
    // restart the interval. Keying the effect on `targets` (a fresh array
    // every render) instead of `signature` would restart it and add one.
    expect(calls).toHaveLength(1)

    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 2 + 10)

    expect(calls).toHaveLength(3)
  })

  it('keeps polling a node whose run left the live list but is still pinned', async () => {
    // The turn that launched this run has already ended -- `idle()` cleared
    // `turnStore`'s live `dagRuns` -- but the run itself is still going and its
    // copy is still pinned onto its tool row. This is the whole reported bug.
    const { calls, rpc } = answering([{ role: 'assistant', text: 'hi' }])

    mount(rpc, [], OPEN_A, [run(node('a', 'running'))])
    await vi.advanceTimersByTimeAsync(0)

    expect(calls).toHaveLength(1)
    expect(calls[0]).toMatchObject({ node: 'a', run_id: 'r1', session_key: 's1' })
  })

  it('prefers the live copy over the pinned one when both name the same run', async () => {
    const liveRunning = answering([{ role: 'assistant', text: 'hi' }])

    mount(liveRunning.rpc, [run(node('a', 'running'))], OPEN_A, [run(node('a', 'completed'))])
    await vi.advanceTimersByTimeAsync(0)
    expect(liveRunning.calls).toHaveLength(1)

    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 2 + 10)
    expect(liveRunning.calls.length).toBeGreaterThanOrEqual(3)

    // The same node, live as completed and pinned as running: read once, the
    // way a settled node is, rather than polled on the pinned copy's word.
    const liveCompleted = answering([{ role: 'assistant', text: 'done' }])

    mount(liveCompleted.rpc, [run(node('a', 'completed'))], OPEN_A, [run(node('a', 'running'))])
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 2 + 10)
    expect(liveCompleted.calls).toHaveLength(1)
  })

  it('reads nothing for a running node nobody has opened', async () => {
    // The live line under a running row is gone, so a closed node has nothing
    // on screen to keep up to date -- and this read is twice a second, per node.
    const { calls, rpc } = answering([{ role: 'assistant', text: 'hi' }])

    mount(rpc, [run(node('a', 'running'))])
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 2 + 10)

    expect(calls).toHaveLength(0)
  })

  it('reads nothing when no node is running and none is open', async () => {
    const { calls, rpc } = answering([])

    mount(rpc, [run(node('a', 'completed'))])
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 2)

    expect(calls).toHaveLength(0)
  })

  it('reads an expanded finished node once and then stops', async () => {
    const { calls, rpc } = answering([{ role: 'assistant', text: 'done' }])

    mount(rpc, [run(node('a', 'completed'))], new Set([dagNodeKey('r1', 'a')]))
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 3 + 10)

    expect(calls).toHaveLength(1)
  })

  it('reads an expanded finished node once even when the read comes back empty', async () => {
    // A node that never ran (skipped/cancelled) has no prompt, transcript,
    // output, or error for `dag.node` to report, so it legitimately answers
    // with an empty `messages` -- the store never gains a trace for it, and
    // that must not be mistaken for "not read yet".
    const { calls, rpc } = answering([])

    mount(rpc, [run(node('a', 'completed'))], new Set([dagNodeKey('r1', 'a')]))
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 3 + 10)

    expect(calls).toHaveLength(1)
  })

  it('reads an expanded finished node once even when the read throws', async () => {
    // A pruned or deleted run dir throws on every attempt -- exactly the case
    // `quiet: true` exists for, so it must still be read once and left alone.
    const { calls, rpc } = throwing()

    mount(rpc, [run(node('a', 'completed'))], new Set([dagNodeKey('r1', 'a')]))
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 3 + 10)

    expect(calls).toHaveLength(1)
  })

  it('ignores an open key that names no node of any live run', async () => {
    const { calls, rpc } = answering([])

    mount(rpc, [run(node('a', 'completed'))], new Set([dagNodeKey('other', 'z')]))
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS)

    expect(calls).toHaveLength(0)
  })

  it('settles a node once the response reports a terminal status, even while it is still shown as running', async () => {
    const { calls, rpc } = finishing([{ role: 'assistant', text: 'hi' }])

    // `runs` never changes across this test: the node is shown as `running`
    // throughout, exactly the case where a displayed-status check would poll
    // forever. Only the response settles it.
    mount(rpc, [run(node('a', 'running'))], OPEN_A)
    await vi.advanceTimersByTimeAsync(0)

    expect(calls).toHaveLength(1)
    expect(getDagNodeTrace(dagNodeKey('r1', 'a'))?.settled).toBe(false)

    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS + 10)

    expect(calls).toHaveLength(2)
    expect(getDagNodeTrace(dagNodeKey('r1', 'a'))?.settled).toBe(true)

    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 3 + 10)

    expect(calls).toHaveLength(2)
  })

  it('does not settle a node whose response reports it is still running', async () => {
    const { calls, rpc } = running([{ role: 'assistant', text: 'hi' }])

    mount(rpc, [run(node('a', 'running'))], OPEN_A)
    await vi.advanceTimersByTimeAsync(0)
    expect(getDagNodeTrace(dagNodeKey('r1', 'a'))?.settled).toBe(false)

    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 2 + 10)

    expect(calls.length).toBeGreaterThanOrEqual(3)
    expect(getDagNodeTrace(dagNodeKey('r1', 'a'))?.settled).toBe(false)
  })

  it('does not settle a node whose response reports a null status', async () => {
    // `null` is what the run reports before it has written its manifest -- the
    // window the poll's very first read lands in right after a launch. It must
    // read the same as `pending`/`running`, not as an unrecognised terminal.
    const { calls, rpc } = nullStatus([{ role: 'assistant', text: 'hi' }])

    mount(rpc, [run(node('a', 'running'))], OPEN_A)
    await vi.advanceTimersByTimeAsync(0)
    expect(getDagNodeTrace(dagNodeKey('r1', 'a'))?.settled).toBe(false)

    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 2 + 10)

    expect(calls.length).toBeGreaterThanOrEqual(3)
    expect(getDagNodeTrace(dagNodeKey('r1', 'a'))?.settled).toBe(false)
  })

  it('stops on unmount', async () => {
    const { calls, rpc } = answering([{ role: 'assistant', text: 'hi' }])
    const app = mount(rpc, [run(node('a', 'running'))], OPEN_A)

    await vi.advanceTimersByTimeAsync(0)
    const seen = calls.length

    app.unmount()
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 3)

    expect(calls).toHaveLength(seen)
  })

  it('survives a read that throws', async () => {
    const rpc = (async () => {
      throw new Error('run dir is gone')
    }) as never

    mount(rpc, [run(node('a', 'running'))], OPEN_A)
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS + 10)

    // Below the cap a throw persists a rising failure count rather than
    // being invisible -- "survives" means it does not crash and does not
    // settle early, not that the store stays untouched.
    expect(getDagNodeTrace(dagNodeKey('r1', 'a'))?.settled).toBe(false)
  })

  it('settles a node whose response is terminal with no messages, instead of polling it forever', async () => {
    // A node that never ran (skipped/cancelled) settles this way too, but with
    // nothing in flight -- messages stays empty and only status carries the news.
    const { calls, rpc } = finishedEmpty()

    mount(rpc, [run(node('a', 'running'))], OPEN_A)
    await vi.advanceTimersByTimeAsync(0)

    expect(calls).toHaveLength(1)
    expect(getDagNodeTrace(dagNodeKey('r1', 'a'))?.settled).toBe(true)

    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 3 + 10)

    expect(calls).toHaveLength(1)
  })

  it('stops reading a pinned running node after enough consecutive read failures', async () => {
    const { calls, rpc } = throwing()

    mount(rpc, [run(node('a', 'running'))], OPEN_A)
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * (DAG_TRACE_READ_FAILURE_CAP - 1) + 10)

    expect(calls).toHaveLength(DAG_TRACE_READ_FAILURE_CAP)
    expect(getDagNodeTrace(dagNodeKey('r1', 'a'))?.settled).toBe(true)

    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 3 + 10)

    expect(calls).toHaveLength(DAG_TRACE_READ_FAILURE_CAP)
  })

  it('does not accumulate a failure streak across a transient success', async () => {
    const { calls, rpc } = flaky()

    mount(rpc, [run(node('a', 'running'))], OPEN_A)

    // Call 1 throws (streak 1); call 2 succeeds and must reset the streak.
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS + 10)
    expect(calls).toHaveLength(2)

    // Calls 3-6 throw again. A streak left at 1 across the call-2 success
    // (not reset) would reach the cap of 5 right here, on call 6; a streak
    // reset by that success is only at 4.
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 4 + 10)
    expect(calls).toHaveLength(6)
    expect(getDagNodeTrace(dagNodeKey('r1', 'a'))?.settled).toBe(false)

    // One more throw (call 7) reaches the cap on a correctly-reset streak.
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS + 10)
    expect(calls).toHaveLength(7)
    expect(getDagNodeTrace(dagNodeKey('r1', 'a'))?.settled).toBe(true)
  })

  it('reads an expanded node once more when it leaves running, so the final answer lands', async () => {
    // The transition a watched node actually makes: it streams while running,
    // then goes terminal. The last read is the one carrying the final answer,
    // and an earlier live snapshot must not be mistaken for having made it.
    const calls: string[] = []
    const rpc = async (_method: string, params?: Record<string, unknown>) => {
      calls.push(params?.node as string)

      return calls.length === 1
        ? nodeResult({ messages: [{ role: 'assistant', text: 'live' }], status: 'running' })
        : nodeResult({ messages: [{ role: 'assistant', text: 'final' }], status: 'completed' })
    }

    const open = new Set([dagNodeKey('r1', 'a')])
    const app = mount(rpc as never, [run(node('a', 'running'))], open)

    await vi.advanceTimersByTimeAsync(0)
    expect(calls).toHaveLength(1)

    app.rerender(<Probe openKeys={open} pinnedRuns={[]} rpc={rpc as never} runs={[run(node('a', 'completed'))]} />)
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * 2 + 10)

    expect(calls.length).toBeGreaterThan(1)
    expect(getDagNodeTrace(dagNodeKey('r1', 'a'))?.messages).toEqual([{ role: 'assistant', text: 'final' }])
    expect(getDagNodeTrace(dagNodeKey('r1', 'a'))?.settled).toBe(true)
  })

  it("keeps a failing node's count across an effect rearm instead of restarting it", async () => {
    const calls: string[] = []
    const rpc = async (_method: string, params?: Record<string, unknown>) => {
      calls.push(params?.node as string)

      if (params?.node === 'b') {
        return nodeResult({ messages: [], status: 'completed' })
      }

      throw new Error('run dir is gone')
    }

    const app = mount(
      rpc as never,
      [run(node('a', 'running'), node('b', 'completed'))],
      new Set([dagNodeKey('r1', 'a'), dagNodeKey('r1', 'b')])
    )

    // Drive 'a' most of the way to the cap: one read short of it.
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS * (DAG_TRACE_READ_FAILURE_CAP - 2) + 10)
    expect(calls.filter(n => n === 'a')).toHaveLength(DAG_TRACE_READ_FAILURE_CAP - 1)
    expect(getDagNodeTrace(dagNodeKey('r1', 'a'))?.settled).not.toBe(true)

    // Closing 'b' drops it from `targets`, changing the effect's dependency
    // signature and forcing it to tear down and rearm -- without touching
    // 'a', which keeps throwing on every read throughout.
    app.rerender(
      <Probe
        openKeys={OPEN_A}
        pinnedRuns={[]}
        rpc={rpc as never}
        runs={[run(node('a', 'running'), node('b', 'completed'))]}
      />
    )

    // Exactly one more read of 'a' reaches the cap. A closure-local streak
    // reset by the rearm would instead need a whole cap's worth again.
    await vi.advanceTimersByTimeAsync(DAG_NODE_POLL_MS + 10)
    expect(getDagNodeTrace(dagNodeKey('r1', 'a'))?.settled).toBe(true)
    expect(calls.filter(n => n === 'a')).toHaveLength(DAG_TRACE_READ_FAILURE_CAP)
  })
})
