/* The tasks panel's state, outside React.
 *
 * The page drives this panel imperatively -- a tab click draws it, a session
 * switch resets it, the strip above the composer reads the same rows -- so
 * the state lives where those callers can reach it and the component
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
  rows: [], loaded: false, nodes: {}, tabByPane: {}, nodeVersions: {}, folds: {},
}

const store = makeStore<TasksState>(initial)

export const { get, set, subscribe, _resetForTests } = store

const patch = (p: Partial<TasksState>): void => { store.set({ ...store.get(), ...p }) }

export const source = (): TasksSource | null => sources.tasks ?? null

/* A different conversation is a different set of tasks. Carrying them across
   would attribute one conversation's background work to another. */
export function reset(): void {
  store.set(initial)
  /* The stamps go with the rows they were about. Left behind they would only
     waste room -- the tick is monotonic, so an entry from the conversation
     just left can never out-rank a read taken in the one arrived at. */
  liveAt.clear()
}

export const rows = (): TaskRow[] => store.get().rows

export const rowKey = (row: TaskRow): string => `${row.kind}:${row.id}`

export const byKey = (kind: TaskKind, id: string): TaskRow | null =>
  store.get().rows.find((r) => r.kind === kind && r.id === id) || null

/* Bumped by every live frame this store applies, and stamped per row so the
   one read that replaces the whole list can tell which rows it is allowed to
   speak for.
 *
 * The read is the problem this exists for. `tasks.list` answers from the state
 * the server held when it was asked, so a frame that lands during the round
 * trip describes a row the answer cannot: a run dispatched in that window is
 * missing from it entirely, and a node the frame moved is stale in it. Neither
 * is recoverable afterwards -- `dag.run_started` fires once per run, and
 * `applyNodeUpdated` schedules a reconcile only on a node's own terminal frame,
 * so whatever the answer writes stands until the next frame or the next read.
 *
 * A key stamped later than the tick a read captured is a row that read must
 * leave alone. A key stamped with no row behind it is one a frame RETIRED (a
 * spawn cancelled before it ran), and the answer must not put it back. */
let liveTick = 0
const liveAt = new Map<string, number>()

/* Every live frame lands through here rather than `patch` directly, so no
   consumer can add a sixth and forget the stamp. Which rows moved is read off
   object identity rather than asked of the reducers: each one rebuilds only
   the row it touches and passes the rest through by reference (live.ts), so
   the diff is exact and the reducers stay pure. */
function liveRows(next: TaskRow[]): void {
  liveTick += 1
  const was = new Map(store.get().rows.map((r) => [rowKey(r), r]))
  for (const r of next) {
    const k = rowKey(r)
    if (was.get(k) !== r) liveAt.set(k, liveTick)
    was.delete(k)
  }
  for (const k of was.keys()) liveAt.set(k, liveTick)
  patch({ rows: next })
}

export async function refresh(): Promise<void> {
  const src = source()
  const key = sessionCurrent()
  /* No source or no open conversation reads as no tasks, not as an error: the
     strip above the composer is in the first frame, before the wiring runs,
     and a draft has no session for a task to be filed under. */
  if (!src || !key) { patch({ rows: [], loaded: true }); return }
  const tick = liveTick
  const got = await src.list(key)
  /* Asked for one conversation, answered into whichever is open now: the
     reader can switch sessions while this is in flight. The same guard the
     agents lists, the deliveries shelf and the desk replay use. */
  if (key !== sessionCurrent()) return
  const answered = Array.isArray(got) ? got : []
  if (tick === liveTick) { patch({ rows: answered, loaded: true }); return }
  /* A frame landed while the read was out, so the answer is no longer the
     whole truth -- but only about the rows those frames named. Row by row:
     the answer speaks for everything it has not been overtaken on, and a
     frame speaks for what it moved after this read was asked.
     Preferring the frame there rather than the answer is the safe way round.
     The answer is the richer copy -- it alone carries the tokens, the output
     files and the final error text -- but it may also be describing a node
     the reader has already watched finish, and nothing would correct that:
     a mid-run `dag.node_updated` schedules no reconcile. The other way costs nothing
     for long, because every terminal frame DOES schedule one (`apply`'s
     refetch), so the richer copy lands a moment later of its own accord. */
  const fresher = (k: string): boolean => (liveAt.get(k) ?? 0) > tick
  const held = new Map(store.get().rows.map((r) => [rowKey(r), r]))
  const merged: TaskRow[] = []
  for (const r of answered) {
    const k = rowKey(r)
    if (!fresher(k)) { merged.push(r); continue }
    /* Held, or retired by that frame and deliberately not put back. */
    const live = held.get(k)
    if (live) merged.push(live)
  }
  /* And the rows the answer has not caught up to at all, which is the case
     this whole branch was written for. Only the ones a frame actually put
     there: a row the answer dropped that no frame touched is a row that is
     gone, and the plain path above would have dropped it too. */
  const named = new Set(answered.map(rowKey))
  const missed = store.get().rows.filter((r) => !named.has(rowKey(r)) && fresher(rowKey(r)))
  patch({ rows: [...missed, ...merged], loaded: true })
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
   already guessed. Used after every terminal live event, after a stop, and on
   each read of a running node's record (`TasksPage.tsx`'s `useNodeRecord`):
   a frame carries no tokens, no files and no final error text, a stop's own
   answer is a bare `found` flag, and a running node's usage and counts grow
   on the server with no frame to carry them. */
export async function reconcile(kind: TaskKind, id: string): Promise<void> {
  const src = source()
  if (!src) return
  const key = sessionCurrent()
  const tick = liveTick
  const row = await src.one(kind, id).catch(() => null)
  /* Same guard as refresh: a row read for the conversation the reader has
     since left does not belong in the one they are looking at now, and a
     stop reconciled after the switch must not re-insert it either. */
  if (!row || key !== sessionCurrent()) return
  /* And the same rule as refresh for a frame that landed while the read was
     out: the answer is then the older copy of this row, and the frame's own
     reconcile brings the newer one a moment later. Without this a read
     started on the beat could put a settled row back to running. */
  if ((liveAt.get(rowKey(row)) ?? 0) > tick) return
  const now = store.get().rows
  const at = now.findIndex((r) => r.kind === kind && r.id === id)
  patch({ rows: at >= 0 ? now.map((r, i) => (i === at ? row : r)) : [row, ...now] })
}

function apply(next: live.LiveResult): void {
  liveRows(next.rows)
  if (next.refetch) void reconcile(next.refetch.kind, next.refetch.id)
}

/* One consumer per live event the contract names (§5.1), called from
   state/session/stages.ts beside the transcript's own calls for the same
   frames. Each wraps its reducer in `live.ts` -- pure there, applied here. */
export function onRunStarted(p: live.RunStartedPayload): void {
  liveRows(live.applyRunStarted(store.get().rows, p))
}
export function onNodeUpdated(p: live.NodeUpdatedPayload): void {
  apply(live.applyNodeUpdated(store.get().rows, p))
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

/** The id one of a task's files is opened under, so the desk's diff tab can
    count the same item the pane's own chip opens. */
export const taskChangeKey = (row: TaskRow, node: TaskNode, file: TaskFile): string =>
  `task:${row.kind}:${row.id}:${node.node_id}:${file.path}`

/* A task file's row is keyed by the node that touched it, not by a path on
   disk -- two nodes of one run can write the same file -- so a door that would
   read a change's key as a path has to ask first. */
export const isTaskChangeKey = (key: string): boolean => key.startsWith('task:')

/* The lane already folded every touch of this path into one verdict (add,
   write, edit or delete), so the row draws what it was told. Deriving it from
   the counts instead -- a write with no deleted lines read as a creation --
   drew `+` on every append to a file that was already there. */
export const taskChangeKind = (file: TaskFile): WsChange['kind'] => file.op

/* A file chip's diff pane, read from the node's own tool calls
   (diffs.ts) since the wire carries only the counts (`add` / `del` / `size`),
   never a patch body. Shared by every door that opens one of a task's
   files as a diff -- the pane's own file chips and the desk's diff tab --
   so the same click reads the same patch wherever it is made. */
export async function fileDiffChange(row: TaskRow, node: TaskNode, file: TaskFile): Promise<WsChange> {
  const src = source()
  const rec = src ? await src.node(row, node).catch(() => null) : null
  return {
    key: taskChangeKey(row, node, file),
    dir: file.path.includes('/') ? file.path.slice(0, file.path.lastIndexOf('/') + 1) : '',
    name: file.path.split('/').pop() || file.path, kind: taskChangeKind(file),
    add: file.add, del: file.del,
    hunks: rec ? hunksForFile(rec.steps, file.path, file.op) : [], turn: 0, open: false,
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
