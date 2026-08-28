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

  it('keeps a still-running graph across the end of its turn', () => {
    // `run_subagent_dag` returns as soon as the run is scheduled, so the reply
    // commits while nodes are still working and every remaining frame arrives
    // after idle(). Clearing the list there dropped those frames on the floor
    // -- foldDagEvent has nothing to fold against -- and the pinned copy read
    // "0 done" for the rest of the session.
    turnController.reset()
    turnController.recordDagEvent(runStarted('dag-1', 'call-a'))
    turnController.idle()

    expect(getTurnState().dagRuns.map(run => run.runId)).toEqual(['dag-1'])

    turnController.recordDagEvent({
      type: 'dag.node_updated',
      payload: { run_id: 'dag-1', node: 'a', status: 'completed' }
    })

    expect(getTurnState().dagRuns[0]!.nodes.find(n => n.id === 'a')?.status).toBe('completed')
  })

  it('releases a graph that finished before the turn ended', () => {
    turnController.reset()
    turnController.recordDagEvent(runStarted('dag-1', 'call-a'))
    turnController.recordDagEvent({
      type: 'dag.run_completed',
      payload: {
        run_id: 'dag-1',
        dir: '/tmp/dag-1',
        summary: { total: 2, completed: 2 },
        files: [
          { node: 'a', status: 'completed' },
          { node: 'b', status: 'completed' }
        ]
      }
    })
    turnController.idle()

    expect(getTurnState().dagRuns).toEqual([])
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
  it("carries the call's own prompts onto the graph the run_started frame builds", async () => {
    // The read-back that matters: no dag.* frame carries a prompt template, so
    // the panel can only name a node if tool.start's arguments were kept and
    // matched to the run by tool_call_id. Asserting the extraction alone would
    // pass with the two halves never wired together.
    turnController.reset()
    const fake = makeFakeRpc()
    const stream = createChatStream({ rpcClient: fake, sessionKey: 'tui:default' })
    await stream.attach()

    fake.__pushEvent({
      type: 'tool.start',
      payload: {
        tool_call_id: 'call-a',
        name: 'run_subagent_dag',
        arguments: {
          nodes: [
            { id: 'a', agent: 'echo', prompt_template: 'read the file' },
            { id: 'b', agent: 'echo', prompt_template: 'summarise {{ a.output }}' }
          ]
        }
      }
    } as TurnEvent)
    fake.__pushEvent(runStarted('dag-1', 'call-a'))

    expect(getTurnState().dagRuns[0]!.nodes.map(n => n.promptTemplate)).toEqual([
      'read the file',
      'summarise {{ a.output }}'
    ])

    await stream.detach()
  })

  it('bounds the stored prompt sets, so a rejected graph cannot pin them for the session', async () => {
    // A graph rejected by validation returns before any dag.* frame, so its
    // entry is never consumed. Eviction is keyed on nothing but insertion order
    // -- deleting on the call's completion instead would race the background
    // path, where the tool returns before the runner has emitted run_started.
    turnController.reset()
    const fake = makeFakeRpc()
    const stream = createChatStream({ rpcClient: fake, sessionKey: 'tui:default' })
    await stream.attach()

    const call = (id: string) =>
      fake.__pushEvent({
        type: 'tool.start',
        payload: {
          tool_call_id: id,
          name: 'run_subagent_dag',
          arguments: { nodes: [{ id: 'a', agent: 'echo', prompt_template: `prompt for ${id}` }] }
        }
      } as TurnEvent)

    // The first call is rejected (no dag.* frame ever follows it), then enough
    // later calls arrive to push it out.
    call('call-rejected')
    for (let i = 0; i < 8; i++) {
      call(`call-${i}`)
    }

    fake.__pushEvent(runStarted('dag-old', 'call-rejected'))
    expect(getTurnState().dagRuns.find(r => r.runId === 'dag-old')?.nodes[0]!.promptTemplate).toBeUndefined()

    fake.__pushEvent(runStarted('dag-new', 'call-7'))
    expect(getTurnState().dagRuns.find(r => r.runId === 'dag-new')?.nodes[0]!.promptTemplate).toBe('prompt for call-7')

    await stream.detach()
  })

  it('still names the nodes when run_started beats tool.start to the client', async () => {
    // The two travel on different channels -- dag.* on the tool's own progress
    // sink, tool.start through the delivery hub -- so their order at the client
    // is not guaranteed, and the margin measured on a real run was 2ms. Reading
    // the prompts only while folding run_started makes that margin decide
    // whether every row of the graph can be expanded, and nothing backfills
    // them afterwards, so a lost race is permanent for the run.
    turnController.reset()
    const fake = makeFakeRpc()
    const stream = createChatStream({ rpcClient: fake, sessionKey: 'tui:default' })
    await stream.attach()

    fake.__pushEvent(runStarted('dag-1', 'call-a'))
    fake.__pushEvent({
      type: 'tool.start',
      payload: {
        tool_call_id: 'call-a',
        name: 'run_subagent_dag',
        arguments: {
          nodes: [
            { id: 'a', agent: 'echo', prompt_template: 'read the file' },
            { id: 'b', agent: 'echo', prompt_template: 'summarise {{ a.output }}' }
          ]
        }
      }
    } as TurnEvent)

    expect(getTurnState().dagRuns[0]!.nodes.map(n => n.promptTemplate)).toEqual([
      'read the file',
      'summarise {{ a.output }}'
    ])

    await stream.detach()
  })

  it('leaves the graph unnamed when the frame names no tool call', async () => {
    // Without a tool_call_id there is nothing to match the stored prompts to,
    // so the rows fall back to their node ids rather than borrowing another
    // call's prompts.
    turnController.reset()
    const fake = makeFakeRpc()
    const stream = createChatStream({ rpcClient: fake, sessionKey: 'tui:default' })
    await stream.attach()

    fake.__pushEvent({
      type: 'tool.start',
      payload: {
        tool_call_id: 'call-a',
        name: 'run_subagent_dag',
        arguments: { nodes: [{ id: 'a', agent: 'echo', prompt_template: 'read the file' }] }
      }
    } as TurnEvent)
    fake.__pushEvent(runStarted('dag-1'))

    expect(getTurnState().dagRuns[0]!.nodes.every(n => n.promptTemplate === undefined)).toBe(true)

    await stream.detach()
  })

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
