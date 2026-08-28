// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// One `spawn` call's run, folded from its `subagent.status` frames.
//
// The dag counterpart is `dagRun.ts`, and the split is the same: kept pure and
// free of any store/Ink import so the fold is testable on its own. A spawn is a
// single run rather than a graph, so the state is one record -- but it survives
// the same two ways a graph does: pinned onto the tool row that made it while
// the turn runs, and rebuilt from `spawn_task_id` + `subagent.list` on resume.

import type { SubagentCall, SubagentStatusEvent } from '../rpc/index.js'
import type { Msg } from '../types.js'

/** The `subagent.status` vocabulary, which is also what the panel renders. */
export type SpawnRunStatus = 'cancelled' | 'completed' | 'failed' | 'pending' | 'running'

export interface SpawnRunState {
  /** The manager's short id for the run, stable across its lifecycle. */
  taskId: string
  /** The call this run belongs to, when the host correlates the two. */
  toolCallId?: string
  /** The record id `subagent.context` reads. Known from `running` onward. */
  callId?: string
  /** What the run was asked, in short. */
  label: string
  agent?: string
  /** The addressable handle, when the caller named or minted one. */
  instance?: string
  status: SpawnRunStatus
  /** Wire clock, in ms, as the manager stamped it. */
  startedAt?: number
  endedAt?: number
}

const TERMINAL: ReadonlySet<SpawnRunStatus> = new Set(['cancelled', 'completed', 'failed'])

export const spawnRunSettled = (run: Pick<SpawnRunState, 'status'>): boolean => TERMINAL.has(run.status)

type SpawnStatusPayload = SubagentStatusEvent['payload']

/**
 * Fold one `subagent.status` frame into the run it names.
 *
 * Returns a new state, never a mutation of `prev` -- the store publishes by
 * identity. A frame that would move a terminal run back to a live status is
 * dropped: the terminal frame and a late `running` can arrive out of order,
 * and a run that finished must not start pulsing again (same rule as
 * `liveAgentsStore.upsert`). Fields a later frame does not carry keep what an
 * earlier one reported -- `call_id` rides only from `running` onward, and
 * `started_at`/`ended_at` only the frames that moved the run.
 */
export const foldSpawnStatus = (prev: SpawnRunState | null, payload: SpawnStatusPayload): SpawnRunState => {
  const status = payload.status as SpawnRunStatus

  if (prev === null) {
    return {
      taskId: payload.task_id,
      ...(payload.tool_call_id ? { toolCallId: payload.tool_call_id } : {}),
      ...(payload.call_id ? { callId: payload.call_id } : {}),
      label: payload.label,
      agent: payload.agent,
      ...(payload.instance ? { instance: payload.instance } : {}),
      status,
      ...(payload.started_at !== undefined ? { startedAt: payload.started_at } : {}),
      ...(payload.ended_at !== undefined ? { endedAt: payload.ended_at } : {})
    }
  }

  if (spawnRunSettled(prev) && !TERMINAL.has(status)) {
    return prev
  }

  return {
    ...prev,
    status,
    ...(payload.call_id ? { callId: payload.call_id } : {}),
    ...(payload.instance ? { instance: payload.instance } : {}),
    ...(payload.started_at !== undefined ? { startedAt: payload.started_at } : {}),
    ...(payload.ended_at !== undefined ? { endedAt: payload.ended_at } : {})
  }
}

// `subagent.list` statuses on the left, the event vocabulary on the right. A
// `skipped` row never comes from a spawn (only graph nodes skip), so it is not
// mapped; a row carrying one is dropped by the caller.
const WIRE_STATUS: Record<string, SpawnRunStatus> = {
  cancelled: 'cancelled',
  error: 'failed',
  ok: 'completed',
  queued: 'pending',
  run: 'running'
}

const isoToMs = (iso?: string): number | undefined => {
  if (!iso) {
    return undefined
  }

  const ms = Date.parse(iso)

  return Number.isFinite(ms) ? ms : undefined
}

/**
 * Rebuild a run from its `subagent.list` row, for a resumed transcript.
 *
 * The row is found by its id's task suffix: a spawn record's directory is
 * `<stamp>-<task_id>` (`make_call_id`), and the task id is what the tool's
 * own result text names -- stamped onto the transcript row as `spawn_task_id`
 * by the server, which authors that sentence. `null` when the list names no
 * such record (a pruned history dir), which renders as the plain row it was.
 */
export const spawnRunFromListRow = (
  taskId: string,
  toolCallId: string,
  rows: readonly SubagentCall[]
): SpawnRunState | null => {
  const row = rows.find(item => (item.kind ?? 'spawn') === 'spawn' && item.id.endsWith(`-${taskId}`))
  const status = row ? WIRE_STATUS[row.status] : undefined

  if (!row || !status) {
    return null
  }

  return {
    taskId,
    toolCallId,
    callId: row.id,
    label: row.label,
    ...(row.agent ? { agent: row.agent } : {}),
    ...(row.instance ? { instance: row.instance } : {}),
    status,
    ...(isoToMs(row.started_at) !== undefined ? { startedAt: isoToMs(row.started_at) } : {}),
    ...(isoToMs(row.ended_at) !== undefined ? { endedAt: isoToMs(row.ended_at) } : {})
  }
}

/**
 * The runs pinned onto tool rows in the transcript, one entry per task id.
 *
 * The same two moments populate `tool.spawn` as populate `tool.dag`:
 * `recordSpawnStatus` pins a run as its turn progresses, and
 * `hydrateSpawnRuns` (`domain/messages.ts`) pins one rebuilt after a resume.
 * This is what lets a run still be polled once `turnStore`'s live list has
 * moved past it.
 */
export const spawnRunsFromHistory = (history: readonly Msg[]): SpawnRunState[] => {
  const byTaskId = new Map<string, SpawnRunState>()

  for (const msg of history) {
    for (const episode of msg.episodes ?? []) {
      for (const tool of episode.tools) {
        if (tool.spawn) {
          byTaskId.set(tool.spawn.taskId, tool.spawn)
        }
      }
    }
  }

  return [...byTaskId.values()]
}
