// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// The DAG graph's path through a turn: progress frames off the subscription ->
// turnController -> the live turn store, and the finished graph pinned onto the
// tool row so it survives into the transcript.

import { afterEach, describe, expect, it } from 'vitest'

import type { DagRunSnapshot, DagRunStartedEvent, TurnEvent } from '../rpc/index.js'

import { createChatStream, type ChatStreamRpcClient } from '../app/chatStream.js'
import { turnController } from '../app/turnController.js'
import { getTurnState } from '../app/turnStore.js'
import { patchUiState } from '../app/uiStore.js'

afterEach(() => {
  patchUiState({ transcript: 'legacy' })
  turnController.reset()
})

const CHAIN: DagRunStartedEvent['payload']['nodes'] = [
  { id: 'a', subagent: 'echo', depends_on: [] },
  { id: 'b', subagent: 'echo', depends_on: ['a'] }
]

const runStarted = (runId: string, toolCallId?: string): TurnEvent => ({
  type: 'dag.run_started',
  payload: { run_id: runId, nodes: CHAIN, ...(toolCallId ? { tool_call_id: toolCallId } : {}) }
})

describe('turnController DAG runs', () => {
  it('opens a run on the live turn store', () => {
    turnController.reset()
    turnController.recordDagEvent(runStarted('dag-1'))

    expect(getTurnState().dagRuns.map(run => run.runId)).toEqual(['dag-1'])
    expect(getTurnState().dagRuns[0]!.nodes.map(n => n.status)).toEqual(['pending', 'pending'])
  })

  it('folds later frames into the run they name', () => {
    turnController.reset()
    turnController.recordDagEvent(runStarted('dag-1'))
    turnController.recordDagEvent({
      type: 'dag.node_updated',
      payload: { run_id: 'dag-1', node: 'a', status: 'running' }
    })

    expect(getTurnState().dagRuns[0]!.nodes.find(n => n.id === 'a')?.status).toBe('running')
  })

  it('keeps concurrent runs apart', () => {
    // One turn may issue several DAG calls; keying the store by run id is what
    // stops the second graph from overwriting the first.
    turnController.reset()
    turnController.recordDagEvent(runStarted('dag-1'))
    turnController.recordDagEvent(runStarted('dag-2'))
    turnController.recordDagEvent({
      type: 'dag.node_updated',
      payload: { run_id: 'dag-2', node: 'a', status: 'failed' }
    })

    const runs = getTurnState().dagRuns
    expect(runs.map(run => run.runId)).toEqual(['dag-1', 'dag-2'])
    expect(runs[0]!.nodes.find(n => n.id === 'a')?.status).toBe('pending')
    expect(runs[1]!.nodes.find(n => n.id === 'a')?.status).toBe('failed')
  })

  it('drops a frame for a run it never saw start', () => {
    turnController.reset()
    turnController.recordDagEvent({
      type: 'dag.node_updated',
      payload: { run_id: 'ghost', node: 'a', status: 'running' }
    })

    expect(getTurnState().dagRuns).toEqual([])
  })

  it('clears the previous turn graphs on reset', () => {
    turnController.reset()
    turnController.recordDagEvent(runStarted('dag-1'))
    turnController.reset()

    expect(getTurnState().dagRuns).toEqual([])
  })

  it('pins the graph onto the tool row that produced it', () => {
    // Without this the graph vanishes when the turn closes: the live store is
    // cleared, and the tool result that replaces it is clamped to 200 chars.
    patchUiState({ transcript: 'episodes' })
    turnController.reset()

    turnController.recordEpisodeStart(0)
    turnController.recordToolStart('call-a', 'run_subagent_dag', '2 nodes: a, b')
    turnController.recordDagEvent(runStarted('dag-1', 'call-a'))
    turnController.recordDagEvent({
      type: 'dag.node_updated',
      payload: { run_id: 'dag-1', node: 'a', status: 'completed' }
    })
    turnController.recordToolComplete('call-a', 'run_subagent_dag', undefined, 'DAG dag-1: 1/2 completed', 1)
    turnController.recordEpisodeStart(1)

    const { finalMessages } = turnController.recordMessageComplete({ text: 'done' })
    const tool = finalMessages.find(m => m.kind === 'episodes')!.episodes![0]!.tools[0]!

    expect(tool.dag?.runId).toBe('dag-1')
    expect(tool.dag?.nodes.find(n => n.id === 'a')?.status).toBe('completed')
  })

  it('leaves other tool rows without a graph', () => {
    patchUiState({ transcript: 'episodes' })
    turnController.reset()

    turnController.recordEpisodeStart(0)
    turnController.recordToolStart('call-x', 'read_file', 'a.ts')
    turnController.recordDagEvent(runStarted('dag-1', 'call-a'))
    turnController.recordToolComplete('call-x', 'read_file', undefined, 'contents', 1)
    turnController.recordEpisodeStart(1)

    const { finalMessages } = turnController.recordMessageComplete({ text: 'done' })
    const tool = finalMessages.find(m => m.kind === 'episodes')!.episodes![0]!.tools[0]!

    expect(tool.dag).toBeUndefined()
  })
})

interface FakeRpc extends ChatStreamRpcClient {
  __pushEvent: (event: TurnEvent) => void
}

const makeFakeRpc = (): FakeRpc => {
  let handler: ((event: TurnEvent) => void) | null = null

  return {
    __pushEvent: (event: TurnEvent) => handler?.(event),
    async rpc<R, P>(method: string, _params: P): Promise<R> {
      return (method === 'turn.send' ? { turn_id: 'turn-1', accepted: true } : {}) as R
    },
    async subscribe<E, P>(_method: string, _params: P, h: (event: E) => void) {
      handler = h as unknown as (event: TurnEvent) => void

      return {
        subscription_id: 'sub-1',
        unsubscribe: async () => {
          handler = null
        }
      }
    }
  }
}

describe('chatStream DAG dispatch', () => {
  it('routes DAG progress frames off the subscription into the turn graphs', async () => {
    turnController.reset()
    const fake = makeFakeRpc()
    const stream = createChatStream({ rpcClient: fake, sessionKey: 'tui:default' })
    await stream.attach()

    fake.__pushEvent(runStarted('dag-1'))
    fake.__pushEvent({ type: 'dag.node_updated', payload: { run_id: 'dag-1', node: 'a', status: 'completed' } })
    fake.__pushEvent({
      type: 'dag.run_completed',
      payload: {
        run_id: 'dag-1',
        dir: '/w/mas_dag/dag-1',
        summary: { total: 2, completed: 1, failed: 0, skipped: 1 },
        files: [
          { node: 'a', status: 'completed', output_file: '/w/mas_dag/dag-1/a.out.md' },
          { node: 'b', status: 'skipped' }
        ]
      }
    })

    const [run] = getTurnState().dagRuns
    expect(run?.done).toBe(true)
    expect(run?.dir).toBe('/w/mas_dag/dag-1')
    expect(run?.nodes.map(n => n.status)).toEqual(['completed', 'skipped'])

    await stream.detach()
  })
})

describe('turnController.applyDagSnapshot', () => {
  const snapshot = (over: Partial<DagRunSnapshot> = {}): DagRunSnapshot => ({
    run_id: 'dag-1',
    dir: '/w/mas_dag/dag-1',
    finalized: true,
    files: [
      { node: 'a', status: 'completed', subagent: 'echo', depends_on: [] },
      { node: 'b', status: 'interrupted', subagent: 'echo', depends_on: ['a'] }
    ],
    summary: { total: 2, completed: 1, failed: 0, skipped: 0 },
    ...over
  })

  it('repairs a run left stale by lost frames', () => {
    turnController.reset()
    turnController.recordDagEvent(runStarted('dag-1'))
    turnController.recordDagEvent({
      type: 'dag.node_updated',
      payload: { run_id: 'dag-1', node: 'b', status: 'running' }
    })

    turnController.applyDagSnapshot(snapshot())

    const [run] = getTurnState().dagRuns
    expect(run?.nodes.map(n => n.status)).toEqual(['completed', 'interrupted'])
    expect(run?.done).toBe(true)
  })

  it('adds a run the client never saw start', () => {
    turnController.reset()

    turnController.applyDagSnapshot(snapshot({ run_id: 'dag-9' }))

    expect(getTurnState().dagRuns.map(r => r.runId)).toEqual(['dag-9'])
  })

  it('leaves other runs alone', () => {
    turnController.reset()
    turnController.recordDagEvent(runStarted('dag-1'))
    turnController.recordDagEvent(runStarted('dag-2'))

    turnController.applyDagSnapshot(snapshot({ run_id: 'dag-2' }))

    const runs = getTurnState().dagRuns
    expect(runs.map(r => r.runId)).toEqual(['dag-1', 'dag-2'])
    expect(runs[0]!.done).toBe(false)
    expect(runs[1]!.done).toBe(true)
  })

  it('re-pins the repaired graph onto its tool row', () => {
    // A repair that does not reach the transcript row leaves the stale graph on
    // screen -- which is the whole thing the refresh exists to fix.
    patchUiState({ transcript: 'episodes' })
    turnController.reset()
    turnController.recordEpisodeStart(0)
    turnController.recordToolStart('call-a', 'run_subagent_dag', '2 nodes: a, b')
    turnController.recordDagEvent(runStarted('dag-1', 'call-a'))
    turnController.recordToolComplete('call-a', 'run_subagent_dag', undefined, 'DAG dag-1', 1)

    turnController.applyDagSnapshot(snapshot())
    turnController.recordEpisodeStart(1)

    const { finalMessages } = turnController.recordMessageComplete({ text: 'done' })
    const tool = finalMessages.find(m => m.kind === 'episodes')!.episodes![0]!.tools[0]!

    expect(tool.dag?.nodes.map(n => n.status)).toEqual(['completed', 'interrupted'])
  })
})
