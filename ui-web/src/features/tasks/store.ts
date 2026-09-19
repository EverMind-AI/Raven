/* The tasks panel's state, outside React.
 *
 * The page drives this panel imperatively -- a tab click draws it, a session
 * switch resets it, the composer strip and the desk light each other up --
 * so the state lives where those callers can reach it and the component
 * subscribes. Rows are addressed by (kind, id): a spawn's id and a dag's run
 * id share no namespace and can theoretically collide.
 */

import { current as sessionCurrent } from '../../lib/session'
import { sources } from '../../state/sources'
import { makeStore } from '../../state/store'
import { hunksForFile } from './diffs'
import * as live from './live'

import type { WsChange } from '../workspace/types'
import type { TaskFile, TaskKind, TaskNode, TaskRow, TasksSource } from './types'

export interface TasksState {
  rows: TaskRow[]
  loaded: boolean
  /* Which node each task's pane is showing, keyed by the pane's own id
     (`task:<kind>:<id>`). Per pane, not one field: the desk opens a pane per
     task, so a single selected-node field would move every open pane when
     the reader picked in one of them. */
  nodes: Record<string, string>
  /* Pointed at from either side: the composer strip and the list light each
     other up, and neither owns the pointer. */
  hover: string | null
  /* Which tab each pane is pinned to, once its reader has picked one --
     keyed by the pane's own id so two open task windows do not move each
     other's tab. A pane absent here has not been pinned: the card decides
     per node until it is (context for one that has run, the work order for
     one that has not). */
  tabByPane: Record<string, 'context' | 'order'>
  /* Bumped once per live event that names one node, keyed by that node's own
     (kind, id, node_id) -- what `TasksPage.tsx`'s `useNodeRecord` reads to
     refetch a running node's record. `dag.node_updated` fires once per tool
     call while a node runs (its payload carries `tool_call_id`), not only on
     a status transition, so a value that only changed on transitions would
     miss every step in between. */
  nodeVersions: Record<string, number>
  /* Every fold inside a node's own record -- the dispatch's "show all", the
     process fold, a thought, a step's calls, one call's own card -- keyed by
     the node (never the pane), so switching tabs and back, or switching to a
     different node and back, neither resets a fold nor carries one node's
     open folds onto another's (contract's own module state does the same:
     DESK.wide / DESK.proc / DESK.think / DESK.wk / DESK.call, all keyed by
     node id). A fold absent here has not been touched: the caller's own
     default still applies until the reader picks one. */
  folds: Record<string, Record<string, boolean>>
}

const initial: TasksState = {
  rows: [], loaded: false, nodes: {}, hover: null, tabByPane: {}, nodeVersions: {}, folds: {},
}

const store = makeStore<TasksState>(initial)

export const { get, set, subscribe, _resetForTests } = store

const patch = (p: Partial<TasksState>): void => { store.set({ ...store.get(), ...p }) }

export const source = (): TasksSource | null => sources.tasks ?? null

/* A different conversation is a different set of tasks. Carrying them across
   would attribute one conversation's background work to another. */
export function reset(): void {
  store.set(initial)
}

export const rows = (): TaskRow[] => store.get().rows

export const rowKey = (row: TaskRow): string => `${row.kind}:${row.id}`

export const byKey = (kind: TaskKind, id: string): TaskRow | null =>
  store.get().rows.find((r) => r.kind === kind && r.id === id) || null

export async function refresh(): Promise<void> {
  const src = source()
  const key = sessionCurrent()
  /* No source or no open conversation reads as no tasks, not as an error: the
     strip above the composer is in the first frame, before the wiring runs,
     and a draft has no session for a task to be filed under. */
  if (!src || !key) { patch({ rows: [], loaded: true }); return }
  const got = await src.list(key)
  /* Asked for one conversation, answered into whichever is open now: the
     reader can switch sessions while this is in flight. The same guard the
     agents lists, the deliveries shelf and the desk replay use. */
  if (key !== sessionCurrent()) return
  patch({ rows: Array.isArray(got) ? got : [], loaded: true })
}

/* The two groups the panel shows. A row is finished when it is not running --
   failure is an ending, and grouping it with the live work would put a red
   dot where the reader looks for progress. */
export const running = (list: TaskRow[] = store.get().rows): TaskRow[] => list.filter((r) => r.status === 'running')
export const settled = (list: TaskRow[] = store.get().rows): TaskRow[] => list.filter((r) => r.status !== 'running')

/** Which node that task's pane is describing, once the reader has picked one. */
export const nodeOf = (paneId: string): string | null => store.get().nodes[paneId] ?? null

/** Picks in one pane, leaving what every other pane is describing where it is. */
export function pickNode(paneId: string, id: string | null): void {
  const nodes = { ...store.get().nodes }
  if (id) nodes[paneId] = id
  else delete nodes[paneId]
  patch({ nodes })
}

/** Which tab a pane is pinned to, or null while it is still following the
    node's own default. */
export const tabOf = (paneId: string): 'context' | 'order' | null => store.get().tabByPane[paneId] ?? null

/* Only the reader pins a tab, per pane; the card decides per node until
   they do (context for one that has run, the work order for one that has
   not). */
export function pickTab(paneId: string, tab: 'context' | 'order'): void {
  patch({ tabByPane: { ...store.get().tabByPane, [paneId]: tab } })
}

export const hover = (id: string | null): void => patch({ hover: id })

const nodeKeyOf = (kind: TaskKind, id: string, nodeId: string): string => `${kind}:${id}:${nodeId}`

/** How many live events have named this node so far -- what `useNodeRecord`
    keys its refetch on, so a running node's steps and answer keep arriving
    without the reader closing and reopening it. */
export const nodeVersion = (kind: TaskKind, id: string, nodeId: string): number =>
  store.get().nodeVersions[nodeKeyOf(kind, id, nodeId)] ?? 0

function bumpNodeVersion(kind: TaskKind, id: string, nodeId: string): void {
  const key = nodeKeyOf(kind, id, nodeId)
  patch({ nodeVersions: { ...store.get().nodeVersions, [key]: (store.get().nodeVersions[key] ?? 0) + 1 } })
}

/** Whether a fold inside a node's own record is open -- `undefined` when the
    reader has not touched it yet, so the caller's own default still applies. */
export const foldOf = (nodeKey: string, fold: string): boolean | undefined => store.get().folds[nodeKey]?.[fold]

/** Records that the reader touched one fold, keyed by the node it belongs to. */
export function setFold(nodeKey: string, fold: string, open: boolean): void {
  patch({ folds: { ...store.get().folds, [nodeKey]: { ...store.get().folds[nodeKey], [fold]: open } } })
}

/* The server's own read for one row, folded back over whatever a live event
   already guessed. Used after every terminal live event and after a stop: a
   frame carries no tokens, no files and no final error text, and a stop's own
   answer is a bare `found` flag. */
async function reconcile(kind: TaskKind, id: string): Promise<void> {
  const src = source()
  if (!src) return
  const key = sessionCurrent()
  const row = await src.one(kind, id).catch(() => null)
  /* Same guard as refresh: a row read for the conversation the reader has
     since left does not belong in the one they are looking at now, and a
     stop reconciled after the switch must not re-insert it either. */
  if (!row || key !== sessionCurrent()) return
  const now = store.get().rows
  const at = now.findIndex((r) => r.kind === kind && r.id === id)
  patch({ rows: at >= 0 ? now.map((r, i) => (i === at ? row : r)) : [row, ...now] })
}

function apply(next: live.LiveResult): void {
  patch({ rows: next.rows })
  if (next.refetch) void reconcile(next.refetch.kind, next.refetch.id)
}

/* One consumer per live event the contract names (§5.1), called from
   state/session/stages.ts beside the transcript's own calls for the same
   frames. Each wraps its reducer in `live.ts` -- pure there, applied here. */
export function onRunStarted(p: live.RunStartedPayload): void {
  patch({ rows: live.applyRunStarted(store.get().rows, p) })
}
export function onNodeUpdated(p: live.NodeUpdatedPayload): void {
  patch({ rows: live.applyNodeUpdated(store.get().rows, p) })
  bumpNodeVersion('dag', p.run_id, p.node)
}
export function onRunCompleted(p: live.RunCompletedPayload): void {
  apply(live.applyRunCompleted(store.get().rows, p))
}
export function onRunReplanned(p: live.RunReplannedPayload): void {
  apply(live.applyRunReplanned(store.get().rows, p))
}
export function onSubagentStatus(p: live.SubagentStatusPayload): void {
  apply(live.applySubagentStatus(store.get().rows, p))
}

/* A write/edit chip's diff pane, read from the node's own tool calls
   (diffs.ts) since the wire carries only the counts (`add` / `del` / `size`),
   never a patch body. Shared by every door that opens one of a task's
   files as a diff -- the pane's own file chips and the desk's diff tab --
   so the same click reads the same patch wherever it is made. */
export async function fileDiffChange(row: TaskRow, node: TaskNode, file: TaskFile): Promise<WsChange> {
  const src = source()
  const rec = src ? await src.node(row, node).catch(() => null) : null
  return {
    key: `task:${row.kind}:${row.id}:${node.node_id}:${file.path}`,
    dir: file.path.includes('/') ? file.path.slice(0, file.path.lastIndexOf('/') + 1) : '',
    name: file.path.split('/').pop() || file.path, kind: 'edit',
    add: file.add, del: file.del, hunks: rec ? hunksForFile(rec.steps, file.path) : [], turn: 0, open: false,
  }
}

/** Stop a running task -- `subagent.interrupt` for a dag, `subagent.cancel_instance`
    for a spawn, dispatched by `row.kind` -- then reconcile it. */
export async function stop(row: TaskRow): Promise<void> {
  const src = source()
  if (!src) return
  await src.stop(row)
  await reconcile(row.kind, row.id)
}
