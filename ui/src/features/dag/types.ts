/* What a `run_subagent_dag` call is orchestrating, as the page holds it.
 *
 * The graph arrives whole on `dag.run_started`, before any node runs, so the
 * shape is fixed for the life of a run and only status, times and the summary
 * move afterwards. That is the fact the whole panel is built on: the layout is
 * computed once and every later event is an update in place.
 */

export type NodeStatus = 'pending' | 'running' | 'completed' | 'failed' | 'skipped' | 'interrupted'

export interface DagNode {
  id: string
  subagent: string
  instance?: string | null
  depends_on: string[]
  status: NodeStatus | string
  started_at: number | null
  ended_at: number | null
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
