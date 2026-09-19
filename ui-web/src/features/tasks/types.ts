/* What one task of a conversation is, as the panel holds it.
 *
 * A task is every unit of delegated work the session started: a spawned
 * subagent, or a `run_subagent_dag` graph (a playbook run is one of those).
 * The wire shapes are `tasks.list`'s own contract types, re-exported here
 * rather than hand-maintained -- see rpc-schema/openrpc.json's `TaskRow` /
 * `TaskNode`; the design is docs/specs/2026-09-18-desk-tasks-list-design.md.
 */

import type {
  DagNodeUpdatedEvent,
  DagRunCompletedEvent,
  DagRunReplannedEvent,
  DagRunStartedEvent,
  SubagentRow,
  SubagentStatusEvent,
  TaskCounts,
  TaskFile,
  TaskKind,
  TaskNode,
  TaskReplan,
  TaskRow,
  TaskStatus,
  TasksListParams,
  TasksListResult,
} from '../../rpc/generated'

export type {
  SubagentRow, TaskCounts, TaskFile, TaskKind, TaskNode, TaskReplan, TaskRow, TaskStatus,
  TasksListParams, TasksListResult,
}

/* The row's own address: a spawn's id and a dag's run id are unique only
   inside their own kind's namespace, so a pane, a seen-mark or a live update
   keys on the pair rather than on `id` alone. */
export const taskKey = (row: TaskRow): string => `${row.kind}:${row.id}`

/* One step of a node's own conversation, in the order it happened. Built from
   the wire's `TranscriptMessage[]` (source.ts's `stepsOf`) rather than a
   second transcript vocabulary: an assistant entry's `reasoning_content` is a
   thought, its own `text` (when the entry is not the trailing answer) is
   something it said between tool calls, its `tool_calls` are actions matched
   to their `role: 'tool'` result, and a `role: 'console'` entry is the live
   cli lane's synthetic stand-in for a channel that keeps no transcript at
   all. */
export type NodeStep =
  | { kind: 'think'; text: string }
  | { kind: 'say'; text: string }
  | { kind: 'tool'; id: string; name: string; args: string; result: string | null; ok: boolean | null }
  | { kind: 'console'; text: string }

/* One node's whole record, normalised from `dag.node` / `subagent.context`.
   `dispatch` is the rendered prompt (upstream output already inlined behind
   its UNTRUSTED fence); `answer` is the trailing assistant entry, when the run
   ended in one rather than in an error or a mid-run stop. No `error` field: a
   node's failure text is already on `TaskNode.error` (capped at 500 chars by
   `tasks.list` itself), and a second copy here could only ever go stale. */
export interface NodeRecord {
  dispatch: string | null
  steps: NodeStep[]
  answer: string | null
  outputTruncated: boolean
}

/* The seam the renderer reads. Every verb it needs:
   the list itself, one row addressed by (kind, id), the two stop calls folded
   into one by `kind`, a node's own record, and the roster the work-order tab
   checks an assigned agent against. */
export interface TasksSource {
  list(sessionKey: string): Promise<TaskRow[]>
  one(kind: TaskKind, id: string): Promise<TaskRow | null>
  /* `subagent.interrupt` for a dag (by run id), `subagent.cancel_instance` for
     a spawn (by agent + handle) -- one call, dispatched by `row.kind`. */
  stop(row: TaskRow): Promise<boolean>
  node(row: TaskRow, node: TaskNode): Promise<NodeRecord>
  roster(): Promise<SubagentRow[]>
  /* Opens the task the spawn record `nodeId` belongs to, when the list holds
     one; answers whether it did. The seam a sibling domain reaches this
     store through, rather than importing it (CONTRIBUTING 2.2). */
  openByNode?: (nodeId: string) => boolean
  /* The five turn-event consumers `state/session/stages.ts` drives (contract
     §5.1), reached the same way: optional so a suite that assembles the
     session pipeline without a tasks source does not throw. */
  onRunStarted?: (p: DagRunStartedEvent['payload']) => void
  onNodeUpdated?: (p: DagNodeUpdatedEvent['payload']) => void
  onRunCompleted?: (p: DagRunCompletedEvent['payload']) => void
  onRunReplanned?: (p: DagRunReplannedEvent['payload']) => void
  onSubagentStatus?: (p: SubagentStatusEvent['payload']) => void
}
