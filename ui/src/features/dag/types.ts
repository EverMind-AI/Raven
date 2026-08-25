/* What a `run_subagent_dag` call is orchestrating, as the page holds it.
 *
 * The graph arrives whole on `dag.run_started`, before any node runs, so the
 * shape is fixed for the life of a run and only status, times and the summary
 * move afterwards. That is the fact the whole panel is built on: the layout is
 * computed once and every later event is an update in place.
 */

export type NodeStatus = 'pending' | 'running' | 'completed' | 'failed' | 'skipped' | 'interrupted'

/* Where one of a node's inputs came from. A bare string is a literal the caller
   wrote inline; the other two name something to read. Kept as a union rather
   than flattened to a display string because the three read differently and a
   reader has to be able to tell "the word 'draft'" from "whatever node `draft`
   produced". */
export type NodeInput = string | { file: string } | { node: string } | Record<string, unknown>

export interface DagNode {
  id: string
  subagent: string
  instance?: string | null
  depends_on: string[]
  status: NodeStatus | string
  started_at: number | null
  ended_at: number | null
  /* One line on what this step is for, written by whoever composed the graph.
     Not a shortening of the template: the template is the instruction, this is
     the intent, and the node id is neither -- a playbook namespaces it, so the
     ids across one graph share their first twenty characters. Absent on a run
     that predates the field, where the id stands in. */
  node_summary?: string | null
  /* What the node was asked to do, before rendering. The rendered prompt is a
     different fact and lives behind `dag.node` -- this is the request, that is
     what happened. Absent on a run whose source could not supply it. */
  prompt_template?: string | null
  /* The other half of the request: a template's `{{ inputs.k }}` does not say
     where k came from. */
  inputs?: Record<string, NodeInput> | null
}

/* What `dag.run_completed` reports. `total` is the server's count and can
   differ from the node list when a run was interrupted, which is why the
   summary line prefers it and falls back to the order's length. */
export interface DagSummary {
  completed?: number
  failed?: number
  skipped?: number
  total?: number
}

export interface DagRun {
  run_id: string
  session: string
  /* The server's own order, kept beside the map: it decides which node sits
     above which inside a column, and a Map's insertion order is not something
     to lean on once nodes are replaced. */
  order: string[]
  nodes: Map<string, DagNode>
  summary: DagSummary | null
  done: boolean
  folded: boolean
  dir?: string | null
  /* The line the graph was dispatched with, which is what the sheet is titled
     by. Absent for a run started before the field existed, where the sheet
     falls back to naming itself. */
  task_summary?: string | null
}

/* Where one node's box goes, in the graph's own coordinates. */
export interface NodeAt {
  x: number
  y: number
}

export interface DagLayout {
  at: Map<string, NodeAt>
  width: number
  height: number
}
