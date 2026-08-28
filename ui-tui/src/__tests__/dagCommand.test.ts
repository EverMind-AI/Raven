// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// `/dag` -- the user-initiated half of the DAG surface. The live events cover a
// run that behaves; this covers the two things they cannot: repairing a graph
// whose frames were lost, and reading a node's prompt/output, which no event
// ever carries.

import { afterEach, describe, expect, it, vi } from 'vitest'

import type { SlashRunCtx } from '../app/slash/types.js'
import type { DagRunSnapshot } from '../rpc/index.js'
import type { Msg } from '../types.js'

import { dagCommands } from '../app/slash/commands/dag.js'
import { findSlashCommand } from '../app/slash/registry.js'
import { turnController } from '../app/turnController.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'

afterEach(() => {
  resetTurnState()
  turnController.reset()
})

const SNAPSHOT: DagRunSnapshot = {
  run_id: 'dag-1',
  dir: '/w/mas_dag/dag-1',
  finalized: true,
  files: [
    { node: 'a', status: 'completed', subagent: 'echo', depends_on: [] },
    { node: 'b', status: 'interrupted', subagent: 'echo', depends_on: ['a'] }
  ],
  summary: { total: 2, completed: 1, failed: 0, skipped: 0 }
}

// What a later re-read of the same run reports once node 'b' gets retried and
// finishes -- distinct from SNAPSHOT so a test can tell a real second refresh
// apart from one that quietly reused the first response.
const SNAPSHOT_REPEAT: DagRunSnapshot = {
  ...SNAPSHOT,
  files: [
    { node: 'a', status: 'completed', subagent: 'echo', depends_on: [] },
    { node: 'b', status: 'completed', subagent: 'echo', depends_on: ['a'] }
  ],
  summary: { total: 2, completed: 2, failed: 0, skipped: 0 }
}

const buildCtx = (rpc: ReturnType<typeof vi.fn>, sys = vi.fn(), history: Msg[] = []) => {
  // A mutable stand-in for the real setHistoryItems/getHistoryItems pair: the
  // history-sourced refresh path publishes through a functional updater, so a
  // mock that just returns the original array would let a broken publish (or
  // none at all) pass unnoticed.
  let current = history

  return {
    gateway: { rpc },
    // Mirrors createSlashHandler: drops a null result and a stale flight.
    guarded:
      <T>(fn: (r: T) => void) =>
      (r: null | T) => {
        if (r) {
          fn(r)
        }
      },
    local: { getHistoryItems: () => current },
    stale: () => false,
    transcript: {
      page: vi.fn(),
      setHistoryItems: (next: Msg[] | ((prev: Msg[]) => Msg[])) => {
        current = typeof next === 'function' ? next(current) : next
      },
      sys
    },
    ui: { sid: 'sid-1' }
  } as unknown as SlashRunCtx
}

const dagCmd = dagCommands[0]!

const openRun = (runId = 'dag-1') => {
  turnController.recordDagEvent({
    type: 'dag.run_started',
    payload: {
      run_id: runId,
      nodes: [
        { id: 'a', subagent: 'echo', depends_on: [] },
        { id: 'b', subagent: 'echo', depends_on: ['a'] }
      ]
    }
  })
}

// `resetSession` on resume empties `turnState.dagRuns` -- it is live-turn
// state -- but `hydrateDagRuns` has already pinned the run it read back onto
// the tool call sitting in the transcript. This is that transcript, standing
// in for what a resumed session actually has to offer `/dag`.
const resumedHistory = (runId = 'dag-1'): Msg[] => [
  {
    episodes: [
      {
        index: 0,
        tools: [
          {
            dag: {
              done: false,
              nodes: [
                { dependsOn: [], id: 'a', status: 'pending', subagent: 'echo' },
                { dependsOn: ['a'], id: 'b', status: 'pending', subagent: 'echo' }
              ],
              runId
            },
            id: 't0',
            name: 'run_subagent_dag',
            ok: true,
            summary: 'dag'
          }
        ]
      }
    ],
    kind: 'episodes',
    role: 'assistant',
    text: ''
  }
]

describe('/dag registration', () => {
  it('resolves locally so it never reaches the CLI slash worker', () => {
    // The command reads live turn state; dispatched to the CLI subprocess it
    // would have no graph to refresh at all.
    expect(findSlashCommand('dag')).toBeTruthy()
  })
})

describe('/dag after a resume', () => {
  it('refreshes a run the turn store lost but the transcript still pins', async () => {
    // No `openRun()`: the turn store starts empty, exactly like right after
    // `resetSession()`. Only the transcript history carries the run.
    const rpc = vi.fn(() => Promise.resolve({ run: SNAPSHOT }))
    turnController.reset()

    dagCmd.run('', buildCtx(rpc, vi.fn(), resumedHistory()), 'dag')

    await vi.waitFor(() => expect(rpc).toHaveBeenCalledWith('dag.get', { run_id: 'dag-1', session_key: 'sid-1' }))
    await vi.waitFor(() => {
      expect(getTurnState().dagRuns[0]!.nodes.map(n => n.status)).toEqual(['completed', 'interrupted'])
    })
  })

  it('resolves a node by id off the same fallback, the other repro named in the bug', () => {
    const rpc = vi.fn(() =>
      Promise.resolve({
        node: { run_id: 'dag-1', node: 'b', output_chars: 0, output_truncated: false }
      })
    )
    turnController.reset()

    dagCmd.run('b', buildCtx(rpc, vi.fn(), resumedHistory()), 'dag')

    return vi.waitFor(() =>
      expect(rpc).toHaveBeenCalledWith('dag.node', expect.objectContaining({ node: 'b', run_id: 'dag-1' }))
    )
  })

  it('publishes the refreshed graph onto the history row a resumed transcript renders', async () => {
    // applyDagSnapshot alone (the live-turn path) cannot make this pass: a
    // resumed session has no live episode for it to reach, so only a genuine
    // setHistoryItems publish lands the refreshed statuses where this reads
    // them from.
    const rpc = vi.fn(() => Promise.resolve({ run: SNAPSHOT }))
    turnController.reset()
    const before = resumedHistory()

    const ctx = buildCtx(rpc, vi.fn(), before)

    dagCmd.run('', ctx, 'dag')

    await vi.waitFor(() => {
      const dag = ctx.local.getHistoryItems()[0]!.episodes![0]!.tools[0]!.dag
      expect(dag?.nodes.map(n => n.status)).toEqual(['completed', 'interrupted'])
    })

    // A silent in-place mutation of `before` would satisfy the assertion above
    // too; these two catch that, since MessageLine/EpisodeMessage are memoized
    // on the msg prop's identity and only a fresh reference re-renders them.
    const after = ctx.local.getHistoryItems()
    expect(after).not.toBe(before)
    expect(after[0]).not.toBe(before[0])
    expect(before[0]!.episodes![0]!.tools[0]!.dag!.nodes.map(n => n.status)).toEqual(['pending', 'pending'])
  })

  it('keeps refreshing the transcript on a second /dag call, not just the first', async () => {
    // The first call's applyDagSnapshot has no live episode to pin to (see
    // above) but does append the run into turnState.dagRuns as a side effect.
    // A second call must not read that append as proof a live turn is running
    // now -- that used to make it skip the transcript publish entirely and
    // the graph would go stale after exactly one refresh.
    const rpc = vi.fn().mockResolvedValueOnce({ run: SNAPSHOT }).mockResolvedValueOnce({ run: SNAPSHOT_REPEAT })
    turnController.reset()
    const ctx = buildCtx(rpc, vi.fn(), resumedHistory())

    dagCmd.run('', ctx, 'dag')

    await vi.waitFor(() => {
      const dag = ctx.local.getHistoryItems()[0]!.episodes![0]!.tools[0]!.dag
      expect(dag?.nodes.map(n => n.status)).toEqual(['completed', 'interrupted'])
    })

    dagCmd.run('', ctx, 'dag')

    await vi.waitFor(() => {
      const dag = ctx.local.getHistoryItems()[0]!.episodes![0]!.tools[0]!.dag
      expect(dag?.nodes.map(n => n.status)).toEqual(['completed', 'completed'])
    })
  })

  it('still offers a run whose first fetch failed, on the next /dag call', async () => {
    // dagRuns only ever grows by what applyDagSnapshot successfully appended,
    // so dag-2's failed first fetch must not make it disappear from the set a
    // second /dag call offers to refresh.
    const tworunHistory: Msg[] = [
      {
        episodes: [
          {
            index: 0,
            tools: [
              {
                dag: { done: false, nodes: [{ dependsOn: [], id: 'a', status: 'pending', subagent: 'echo' }], runId: 'dag-1' },
                id: 't0',
                name: 'run_subagent_dag',
                ok: true,
                summary: 'dag'
              },
              {
                dag: { done: false, nodes: [{ dependsOn: [], id: 'x', status: 'pending', subagent: 'echo' }], runId: 'dag-2' },
                id: 't1',
                name: 'run_subagent_dag',
                ok: true,
                summary: 'dag'
              }
            ]
          }
        ],
        kind: 'episodes',
        role: 'assistant',
        text: ''
      }
    ]

    const rpc = vi.fn((_method: string, params: { run_id: string }) =>
      params.run_id === 'dag-1' ? Promise.resolve({ run: SNAPSHOT }) : Promise.reject(new Error('no readable DAG run'))
    )
    turnController.reset()
    const ctx = buildCtx(rpc, vi.fn(), tworunHistory)

    dagCmd.run('', ctx, 'dag')

    await vi.waitFor(() => expect(rpc).toHaveBeenCalledWith('dag.get', { run_id: 'dag-2', session_key: 'sid-1' }))
    await vi.waitFor(() => {
      const dag = ctx.local.getHistoryItems()[0]!.episodes![0]!.tools[0]!.dag
      expect(dag?.nodes.map(n => n.status)).toEqual(['completed', 'interrupted'])
    })

    rpc.mockClear()
    dagCmd.run('', ctx, 'dag')

    await vi.waitFor(() => expect(rpc).toHaveBeenCalledWith('dag.get', { run_id: 'dag-2', session_key: 'sid-1' }))
  })
})

describe('/dag (no argument)', () => {
  it('refreshes every run of the turn off disk', async () => {
    const rpc = vi.fn(() => Promise.resolve({ run: SNAPSHOT }))
    turnController.reset()
    openRun()

    dagCmd.run('', buildCtx(rpc), 'dag')

    await vi.waitFor(() => expect(rpc).toHaveBeenCalledWith('dag.get', { run_id: 'dag-1', session_key: 'sid-1' }))
    await vi.waitFor(() => {
      expect(getTurnState().dagRuns[0]!.nodes.map(n => n.status)).toEqual(['completed', 'interrupted'])
    })
  })

  it('reports the repaired tally', async () => {
    const sys = vi.fn()
    const rpc = vi.fn(() => Promise.resolve({ run: SNAPSHOT }))
    turnController.reset()
    openRun()

    dagCmd.run('', buildCtx(rpc, sys), 'dag')

    await vi.waitFor(() => {
      expect(sys.mock.calls.flat().join('\n')).toContain('dag-1')
    })
  })

  it('lists every node id, which is the only keyboard route to one', () => {
    // `/dag <node>` takes an id and nothing else, and the tool row's own label
    // elides past the third id ("N nodes: a, b, c (+K more)"). Expanding a row
    // shows its id but needs a mouse, so without this the ids past the third
    // are reachable from nowhere.
    const sys = vi.fn()
    const rpc = vi.fn(() => Promise.resolve({ run: SNAPSHOT }))
    turnController.reset()
    openRun()

    dagCmd.run('', buildCtx(rpc, sys), 'dag')

    return vi.waitFor(() => {
      const out = sys.mock.calls.flat().join('\n')
      expect(out).toContain('a')
      expect(out).toContain('b')
      expect(out).toMatch(/nodes:/i)
    })
  })

  it('says so when the session has no DAG run', () => {
    const sys = vi.fn()
    const rpc = vi.fn(() => Promise.resolve({}))
    turnController.reset()

    dagCmd.run('', buildCtx(rpc, sys), 'dag')

    expect(rpc).not.toHaveBeenCalled()
    expect(sys.mock.calls.flat().join('\n')).toMatch(/no DAG run/i)
  })

  it('leaves the graph as it was when the refresh fails', async () => {
    // A run dir that was cleaned up must not blank the graph the user can see.
    const rpc = vi.fn(() => Promise.reject(new Error('no readable DAG run')))
    const sys = vi.fn()
    turnController.reset()
    openRun()

    dagCmd.run('', buildCtx(rpc, sys), 'dag')

    await vi.waitFor(() => expect(sys).toHaveBeenCalled())
    expect(getTurnState().dagRuns[0]!.nodes.map(n => n.status)).toEqual(['pending', 'pending'])
  })
})

describe('/dag <node>', () => {
  it('pages in the node prompt and output', async () => {
    const page = vi.fn()
    const rpc = vi.fn(() =>
      Promise.resolve({
        node: {
          run_id: 'dag-1',
          node: 'b',
          prompt: 'rendered prompt text',
          output: 'the node output',
          output_chars: 15,
          output_truncated: false
        }
      })
    )
    turnController.reset()
    openRun()
    const ctx = buildCtx(rpc)
    ;(ctx.transcript as unknown as { page: unknown }).page = page

    dagCmd.run('b', ctx, 'dag')

    await vi.waitFor(() =>
      expect(rpc).toHaveBeenCalledWith('dag.node', expect.objectContaining({ node: 'b', run_id: 'dag-1' }))
    )
    await vi.waitFor(() => expect(page.mock.calls.flat().join('\n')).toContain('rendered prompt text'))
  })

  it('rejects a node the graph does not contain without calling the server', () => {
    const sys = vi.fn()
    const rpc = vi.fn(() => Promise.resolve({}))
    turnController.reset()
    openRun()

    dagCmd.run('ghost', buildCtx(rpc, sys), 'dag')

    expect(rpc).not.toHaveBeenCalled()
    expect(sys.mock.calls.flat().join('\n')).toContain('ghost')
  })
})

describe('/dag <ordinal>', () => {
  it('takes the short number the picture shows', () => {
    const rpc = vi.fn(() =>
      Promise.resolve({
        node: { run_id: 'dag-1', node: 'b', output_chars: 0, output_truncated: false }
      })
    )
    turnController.reset()
    openRun()

    dagCmd.run('2', buildCtx(rpc), 'dag')

    return vi.waitFor(() =>
      expect(rpc).toHaveBeenCalledWith('dag.node', expect.objectContaining({ node: 'b', run_id: 'dag-1' }))
    )
  })

  it('rejects an ordinal past the end of the run', () => {
    const sys = vi.fn()
    const rpc = vi.fn(() => Promise.resolve({}))
    turnController.reset()
    openRun()

    dagCmd.run('9', buildCtx(rpc, sys), 'dag')

    expect(rpc).not.toHaveBeenCalled()
    expect(sys.mock.calls.flat().join('\n')).toContain('9')
  })

  it('reads an ordinal against the latest run when a turn opened several', () => {
    // An ordinal is an index, so it only means something against one graph. The
    // latest is the one whose panel is on screen, and picking any other would
    // silently fetch a node the reader was not pointing at.
    const rpc = vi.fn(() =>
      Promise.resolve({ node: { run_id: 'dag-2', node: 'y', output_chars: 0, output_truncated: false } })
    )
    turnController.reset()
    openRun()
    turnController.recordDagEvent({
      type: 'dag.run_started',
      payload: {
        run_id: 'dag-2',
        nodes: [
          { id: 'x', subagent: 'echo', depends_on: [] },
          { id: 'y', subagent: 'echo', depends_on: ['x'] }
        ]
      }
    })

    dagCmd.run('2', buildCtx(rpc), 'dag')

    return vi.waitFor(() =>
      expect(rpc).toHaveBeenCalledWith('dag.node', expect.objectContaining({ node: 'y', run_id: 'dag-2' }))
    )
  })

  it('numbers the ids in the refresh listing, so the mapping is printed', () => {
    const sys = vi.fn()
    const rpc = vi.fn(() => Promise.resolve({ run: SNAPSHOT }))
    turnController.reset()
    openRun()

    dagCmd.run('', buildCtx(rpc, sys), 'dag')

    return vi.waitFor(() => expect(sys.mock.calls.flat().join('\n')).toContain('1 a, 2 b'))
  })

  it('lets a node whose id is a number still resolve as an id', () => {
    // The ordinal is tried only after an id lookup misses, so a numeric id keeps
    // meaning itself and no existing usage changes meaning under this feature.
    // The node called `2` sits at ordinal 3 here, which is what tells the two
    // readings apart: ordinal-first would fetch `middle`.
    const rpc = vi.fn(() =>
      Promise.resolve({ node: { run_id: 'dag-1', node: '2', output_chars: 0, output_truncated: false } })
    )
    turnController.reset()
    turnController.recordDagEvent({
      type: 'dag.run_started',
      payload: {
        run_id: 'dag-1',
        nodes: [
          { id: 'first', subagent: 'echo', depends_on: [] },
          { id: 'middle', subagent: 'echo', depends_on: [] },
          { id: '2', subagent: 'echo', depends_on: [] }
        ]
      }
    })

    dagCmd.run('2', buildCtx(rpc), 'dag')

    return vi.waitFor(() => expect(rpc).toHaveBeenCalledWith('dag.node', expect.objectContaining({ node: '2' })))
  })
})
