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

const buildCtx = (rpc: ReturnType<typeof vi.fn>, sys = vi.fn()) =>
  ({
    gateway: { rpc },
    // Mirrors createSlashHandler: drops a null result and a stale flight.
    guarded:
      <T>(fn: (r: T) => void) =>
      (r: null | T) => {
        if (r) {
          fn(r)
        }
      },
    stale: () => false,
    transcript: { page: vi.fn(), sys },
    ui: { sid: 'sid-1' }
  }) as unknown as SlashRunCtx

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

describe('/dag registration', () => {
  it('resolves locally so it never reaches the CLI slash worker', () => {
    // The command reads live turn state; dispatched to the CLI subprocess it
    // would have no graph to refresh at all.
    expect(findSlashCommand('dag')).toBeTruthy()
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

  it('says so when the turn has no DAG run', () => {
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
