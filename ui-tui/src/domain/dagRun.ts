// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// One `run_subagent_dag` call's graph, folded from its progress events.
//
// Kept pure and free of any store/Ink import so the state machine is testable
// on its own: the events arrive out of a socket in real time and a wrong fold
// shows up as a graph that quietly stops updating, which a render test cannot
// distinguish from a graph that is simply idle.

import type {
  DagNodeStatus,
  DagNodeUpdatedEvent,
  DagRunCompletedEvent,
  DagRunSnapshot,
  DagRunStartedEvent
} from '../rpc/index.js'

/** Any of the three progress events a DAG run emits. */
export type DagEvent = DagNodeUpdatedEvent | DagRunCompletedEvent | DagRunStartedEvent

/** The wire vocabulary plus `interrupted`, which the client infers rather than
 * receives: the runner only ever reports the four terminal states, so a node
 * still called `running` when the run closed lost its terminal frame. */
export type DagRunNodeStatus = DagNodeStatus | 'interrupted'

export interface DagRunNode {
  id: string
  subagent: string
  dependsOn: string[]
  /** Shared stateful handle; nodes naming the same one ran sequentially. */
  instance?: string
  status: DagRunNodeStatus
  outputFile?: string
  error?: string
}

export interface DagRunSummary {
  total?: number
  completed?: number
  failed?: number
  skipped?: number
}

export interface DagRunState {
  runId: string
  /** The call this run belongs to, when the host correlates the two. */
  toolCallId?: string
  nodes: DagRunNode[]
  done: boolean
  /** Where the run wrote its per-node outputs; known once it completes. */
  dir?: string
  summary?: DagRunSummary
}

const fromStart = (payload: DagRunStartedEvent['payload']): DagRunState => ({
  runId: payload.run_id,
  ...(payload.tool_call_id ? { toolCallId: payload.tool_call_id } : {}),
  done: false,
  nodes: payload.nodes.map(node => ({
    id: node.id,
    subagent: node.subagent,
    dependsOn: [...node.depends_on],
    ...(node.instance ? { instance: node.instance } : {}),
    status: 'pending' as const
  }))
})

const withNodeStatus = (run: DagRunState, id: string, status: DagRunNodeStatus): DagRunState => ({
  ...run,
  nodes: run.nodes.map(node => (node.id === id ? { ...node, status } : node))
})

const fromCompletion = (run: DagRunState, payload: DagRunCompletedEvent['payload']): DagRunState => {
  const byNode = new Map(payload.files.map(file => [file.node, file]))

  return {
    ...run,
    done: true,
    dir: payload.dir,
    summary: { ...payload.summary },
    nodes: run.nodes.map(node => {
      const file = byNode.get(node.id)

      // The manifest is authoritative for a node it names; one it does not name
      // keeps whatever the last update said, except that `running` can no longer
      // be true of a run that has closed.
      return {
        ...node,
        status: file ? file.status : node.status === 'running' ? 'interrupted' : node.status,
        ...(file?.output_file ? { outputFile: file.output_file } : {}),
        ...(file?.error ? { error: file.error } : {})
      }
    })
  }
}

/**
 * Fold one progress event into the run it belongs to.
 *
 * Returns a new state, never a mutation of `prev` — the store publishes by
 * identity, so folding in place would not re-render.
 *
 * `null` in means no run is open yet: only `dag.run_started` can open one, so an
 * update that arrives first (a subscription attached mid-run) is dropped rather
 * than used to invent a node, which would draw a graph that never gains its
 * edges. An event naming a different run leaves `prev` untouched.
 */
export const foldDagEvent = (prev: DagRunState | null, event: DagEvent): DagRunState | null => {
  if (event.type === 'dag.run_started') {
    return fromStart(event.payload)
  }

  if (prev === null || prev.runId !== event.payload.run_id) {
    return prev
  }

  if (event.type === 'dag.run_completed') {
    return fromCompletion(prev, event.payload)
  }

  return prev.nodes.some(node => node.id === event.payload.node)
    ? withNodeStatus(prev, event.payload.node, event.payload.status)
    : prev
}

/**
 * Replace a run's state with a snapshot read back off disk (`dag.get`).
 *
 * The live frames are not replayed anywhere, so a run whose gateway died
 * mid-flight leaves its nodes pinned to whatever the client last heard —
 * typically `running`, forever. The snapshot is the durable record and wins
 * outright; it is only ever fetched when the local state is known to be stale.
 *
 * `prev` contributes exactly one thing the snapshot cannot know: the tool call
 * the graph was pinned to. Without carrying that over, a repaired graph is
 * orphaned from the transcript row that draws it.
 */
export const foldDagSnapshot = (prev: DagRunState | null, snapshot: DagRunSnapshot): DagRunState => ({
  runId: snapshot.run_id,
  ...(prev?.toolCallId ? { toolCallId: prev.toolCallId } : {}),
  done: snapshot.finalized,
  dir: snapshot.dir,
  summary: { ...snapshot.summary },
  nodes: snapshot.files.map(file => ({
    id: file.node,
    subagent: file.subagent ?? '',
    dependsOn: [...(file.depends_on ?? [])],
    ...(file.instance ? { instance: file.instance } : {}),
    status: file.status,
    ...(file.output_file ? { outputFile: file.output_file } : {}),
    ...(file.error ? { error: file.error } : {})
  }))
})
