/* What one task of a conversation is, as the panel holds it.
 *
 * A task is every unit of delegated work the session started, whatever started
 * it: a spawned subagent, a `run_subagent_dag` graph, or a playbook run. The
 * three differ in how they were dispatched and in nothing the reader of this
 * list cares about, which is why they share one row shape and say their
 * `source` rather than living in three lists.
 *
 * There is no `tasks.*` RPC yet. These shapes are the contract the panel is
 * written against, so the live source can be installed over the fixture one
 * without the renderer changing.
 */

import type { DagNode } from '../dag/types'

/* Running, finished, or finished badly. Deliberately coarser than
   `NodeStatus`: a node's `skipped` and `interrupted` are facts about one step,
   while a row answers only which of the two groups it belongs in and which
   colour its dot is. */
export type TaskState = 'run' | 'done' | 'fail'

export type TaskSource = 'spawn' | 'dag' | 'playbook'

export interface TaskNode {
  id: string
  /* `node_summary` for a graph or playbook node, `task_summary` for a spawn's
     single node. Falls back to the id, which is what `nodeLabel` in the dag
     island already does. */
  title?: string
  /* `subagent`. A roster name, not a role: Raven for the built-in one, and the
     ACP externals by their own names. */
  agent: string
  status: string
  /* `depends_on`. Named for the reader rather than mirroring the wire, and
     converted at the dag boundary. */
  from: string[]
  duration?: string
  /* The whole task text sent to the sub-agent, placeholders unexpanded. The
     detail card's largest field, and the only place the reader can see what a
     step was actually asked. */
  prompt?: string
  /* `skills` and `mcps`. A spawn has neither -- the tool takes no such
     parameters -- so its rows read as absent rather than as empty. */
  skills?: string[]
  mcps?: string[]
  /* How much of a record the executor kept. A `cli` external agent records
     none, which the card has to say differently from "it has not run". */
  records?: number
  toolCalls?: number
  record?: NodeRecord
}

/* One step of what a sub-agent did, in the order it did it. Two kinds because
   a reader treats them differently: thinking is prose to skim, a tool call is
   an action with an argument and a result to check. */
export type RecordStep =
  | { kind: 'think'; text: string }
  | { kind: 'tool'; tool: string; arg: string; result: string }

/* What one node's conversation holds. The same shape `dag.node` and
   `subagent.context` answer with, narrowed to what this card draws.
   No input field anywhere in it: the record is read, not continued -- the main
   agent is what dispatches, and a second place to type would be a second
   conversation nobody is listening to. */
export interface NodeRecord {
  /* The rendered prompt, upstream output included. Whatever came back from
     another agent arrives inside its UNTRUSTED fence and stays there: this is
     the one place the page shows one agent's words to another's reader. */
  dispatch: string
  steps: RecordStep[]
  answer?: string
  /* Where the answer would be, when the run stopped instead of answering. */
  stopped?: string
  /* A `cli` external agent reports no thinking and no tool calls at all. That
     is a fact about the executor, not about the run, and the card has to say
     so rather than draw the empty middle of a run that did happen. */
  unrecorded?: boolean
}

/* One file a task produced, and one it changed. The header chips name them and
   are the door to them: a product opens the preview, a changed file opens its
   diff. Both panes are the desk's, not this panel's -- the chip supplies the
   identity and the desk supplies the view.

   Hence the second field on each. What a chip prints is a name, which is for
   the reader; what a pane opens on is a path, which is for the desk, and the
   two are the same string only in the easy case. Absent, the name is used --
   for a fixture and for a run whose source has nothing better to say. */
export interface TaskArtifact {
  name: string
  path?: string
}

export interface TaskDiff {
  file: string
  add: number
  del: number
  /* The path the session's change list files this change under. The hunks live
     there rather than here: what changed in a file is a fact about the session,
     recorded by the tool calls that did it, and a task carrying its own copy
     would be a second answer to the same question. */
  key?: string
}

export interface TaskRow {
  id: string
  name: string
  source: TaskSource
  /* What to print on the source chip when the source alone does not say it.
     A playbook run names its playbook; a spawn and a graph do not. */
  sourceName?: string
  agent: string
  state: TaskState
  /* Which step of how many, for a running row. The list's second line shows a
     duration instead, but the composer chip is too narrow for one and this is
     the fact it wants. */
  step?: [number, number]
  duration?: string
  /* Whether the whole graph was approved before any of it ran. `run dag`
     defaults false and a playbook defaults true; a spawn has no such field, so
     absent means "do not say", not "false". */
  confirm?: boolean
  artifacts?: TaskArtifact[]
  diffs?: TaskDiff[]
  /* Why a failed task failed, in one line. The header says it on a red band;
     the node that failed says its own half. */
  failure?: string
  nodes: TaskNode[]
}


/* The seam the renderer reads. `list` is all this increment needs: a static
   panel has nothing to watch and nothing to open on the server. */
export interface TasksSource {
  list: () => Promise<TaskRow[]>
}

/* A task's steps as the dag island draws them.
 *
 * The two shapes carry the same graph and disagree about names, so the
 * conversion lives here rather than in the renderer: `DagGraph` is shared with
 * the composer sheet and the transcript card, and it should not learn a second
 * vocabulary to be reusable. Times are null because a static task has no
 * clock -- `took` then prints nothing, which is the honest answer. */
export const toDagNodes = (task: TaskRow): DagNode[] =>
  task.nodes.map((n) => ({
    id: n.id,
    subagent: n.agent,
    depends_on: n.from,
    status: n.status,
    started_at: null,
    ended_at: null,
    node_summary: n.title || null,
    prompt_template: n.prompt || null,
  }))
