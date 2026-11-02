/* The tasks panel's state, outside React.
 *
 * The page drives this panel imperatively -- a tab click draws it, a session
 * switch resets it, the strip above the composer reads the same rows -- so
 * the state lives where those callers can reach it and the component
 * subscribes. Rows are addressed by (kind, id): a spawn's id and a dag's run
 * id share no namespace and can theoretically collide.
 */

import { t } from '../../i18n/t'
import { current as sessionCurrent } from '../../lib/session'
import { sources } from '../../state/sources'
import { makeStore } from '../../state/store'
import { hunksForFile } from './diffs'
import * as live from './live'

import type { WsChange } from '../workspace/types'
import type {
  ProgressStep, TaskBoard, TaskFile, TaskGroup, TaskKind, TaskLinkRef, TaskNode, TaskRow,
  TasksBoardUpdatedPayload, TasksSource,
} from './types'

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
     refetch a node's record the moment a frame moves it, ahead of its next
     beat. `dag.node_updated` marks a node's transitions (its `tool_call_id`
     is the parent turn's `run_subagent_dag` call, not a step of the node's
     own); the steps between transitions reach the panel on the beat. */
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
  /* The host's Task Board for the conversation on screen, read whole and
     replaced whole -- a board is small, and the post-turn update rewrites it in
     one go. `lineage` maps each run that continued a placed run (a replan's
     successor, a later stint round) to the run it continues. */
  board: TaskBoard | null
  lineage: Record<string, string>
  boardLoaded: boolean
  /* A Task Group the Task Board page asked the sidebar to open, in the
     conversation it belongs to: held until that conversation's board is
     drawn, then spent once (BoardList). */
  focus: { sessionKey: string; groupId: string } | null
  /* The group the reader's next message moves on, in the conversation it was
     picked in: drawn above the composer (MoveOnBar) until that message goes. */
  moveOn: MoveOn | null
}

export interface MoveOn { sessionKey: string; groupId: string; title: string; summary: string }

const initial: TasksState = {
  rows: [], loaded: false, nodes: {}, tabByPane: {}, nodeVersions: {}, folds: {},
  board: null, lineage: {}, boardLoaded: false, focus: null, moveOn: null,
}

const store = makeStore<TasksState>(initial)

export const { get, set, subscribe, _resetForTests } = store

const patch = (p: Partial<TasksState>): void => { store.set({ ...store.get(), ...p }) }

export const source = (): TasksSource | null => sources.tasks ?? null

/* A different conversation is a different set of tasks. Carrying them across
   would attribute one conversation's background work to another. */
export function reset(): void {
  /* The focus request and the move-on survive: opening a card switches
     conversation, and the switch is what resets this store, before the board it
     asked for exists. Each names its own conversation. */
  store.set({ ...initial, focus: store.get().focus, moveOn: store.get().moveOn })
  rechecked.clear()
  /* The stamps go with the rows they were about. Left behind they would only
     waste room -- the tick is monotonic, so an entry from the conversation
     just left can never out-rank a read taken in the one arrived at. */
  liveAt.clear()
}

export const rows = (): TaskRow[] => store.get().rows

export function requestFocus(sessionKey: string, groupId: string): void {
  patch({ focus: { sessionKey, groupId } })
}

/** The group waiting to be opened in this conversation's board, if any. */
export function focusFor(sessionKey: string): string | null {
  const { focus } = store.get()
  return focus && focus.sessionKey === sessionKey ? focus.groupId : null
}

export function clearFocus(): void {
  if (store.get().focus) patch({ focus: null })
}

/* ── the task board ─────────────────────────────────────────────────────── */

/* Asked for one conversation, answered into whichever is open now: the same
   guard `refresh` keeps, for the same reason. */
export async function refreshBoard(): Promise<void> {
  const src = source()
  const key = sessionCurrent()
  if (!src?.board || !key) { patch({ board: null, lineage: {}, boardLoaded: true }); return }
  const got = await src.board(key)
  if (key !== sessionCurrent()) return
  patch({ board: got.board, lineage: got.lineage || {}, boardLoaded: true })
}

export function onBoardUpdated(p: TasksBoardUpdatedPayload): void {
  if (p.session_key !== sessionCurrent()) return
  patch({ board: p.board, lineage: p.lineage || {}, boardLoaded: true })
}

/* What Answer puts in the composer for a group that needs the reader: the
   group named, then each question its latest event asked with a slot under
   it, and where the cursor goes -- the end of the first slot. A group whose
   latest event named none gets the heading alone. */
export function answerDraft(group: TaskGroup): { text: string; caret: number } {
  const head = t('gui.tasks.board.answer_head', { title: group.title })
  const questions = group.steps[group.steps.length - 1]?.questions ?? []
  if (!questions.length) return { text: `${head}\n`, caret: head.length + 1 }
  const slot = t('gui.tasks.board.answer_slot')
  const text = [head, ...questions.flatMap((q, i) => [`${i + 1}. ${q}`, slot])].join('\n')
  return { text, caret: head.length + 1 + `1. ${questions[0]}`.length + 1 + slot.length }
}

/* The composer's verb, lent at boot (src/app/install.ts) rather than imported
   from a sibling domain; unlent, Answer does nothing. */
type Answer = (draft: string, caret: number) => void
let answerVerb: Answer | null = null

export function lendAnswer(fn: Answer | null): void {
  answerVerb = fn
}

export function answer(group: TaskGroup): void {
  if (!answerVerb) return
  const draft = answerDraft(group)
  answerVerb(draft.text, draft.caret)
}

/* The composer's focus, lent at boot like Answer's verb; unlent, nothing moves. */
let focusVerb: (() => void) | null = null

export function lendFocus(fn: (() => void) | null): void {
  focusVerb = fn
}

/* Move on: the next message in that conversation is about this group. The
   reader is put in the composer to write it; the server hears which group as
   the message goes (`spendMoveOn`), not before, so a bar put away tells it
   nothing. */
export function moveOn(sessionKey: string, group: Pick<TaskGroup, 'id' | 'title' | 'summary'>): void {
  patch({ moveOn: { sessionKey, groupId: group.id, title: group.title, summary: group.summary } })
  focusVerb?.()
}

export function moveOnFor(sessionKey: string | null): MoveOn | null {
  const { moveOn: held } = store.get()
  return held && held.sessionKey === sessionKey ? held : null
}

export const moveOnPending = (sessionKey: string | null): boolean => moveOnFor(sessionKey) !== null

export function clearMoveOn(): void {
  if (store.get().moveOn) patch({ moveOn: null })
}

/* Lent to the send (src/app/install.ts): the message about the group is going,
   so the server is told first and the bar is put away. A server that cannot be
   told costs the note, never the message. */
export async function spendMoveOn(sessionKey: string | null): Promise<void> {
  const held = moveOnFor(sessionKey)
  if (!held) return
  clearMoveOn()
  try {
    await source()?.moveOn?.(held.sessionKey, held.groupId)
  } catch {
    /* The message still goes; its turn just is not told which group. */
  }
}

const newestFirst = (a: TaskGroup, b: TaskGroup): number => b.created_at - a.created_at

export const openGroups = (board: TaskBoard): TaskGroup[] =>
  board.groups.filter((g) => g.status !== 'completed').sort(newestFirst)

export const completedGroups = (board: TaskBoard): TaskGroup[] =>
  board.groups.filter((g) => g.status === 'completed').sort(newestFirst)

export interface LinkedRow { link: TaskLinkRef; row: TaskRow | null }

/* A step's Tasks, each followed by the runs that continued it. A link whose row
   `tasks.list` no longer holds -- a branched conversation, a removed run -- is
   kept, with no row, so the step still names what it started. */
export function linkedRows(step: ProgressStep, list: TaskRow[], lineage: Record<string, string>): LinkedRow[] {
  const find = (kind: TaskKind, id: string): TaskRow | null => list.find((r) => r.kind === kind && r.id === id) || null
  const out: LinkedRow[] = []
  for (const link of step.tasks) {
    out.push({ link, row: find(link.kind, link.id) })
    if (link.kind !== 'dag') continue
    for (const [child, parent] of Object.entries(lineage)) {
      if (parent === link.id) out.push({ link: { kind: 'dag', id: child, summary: link.summary }, row: find('dag', child) })
    }
  }
  return out
}

/* The rows no step places, directly or as a run continuing a placed one. With
   no board, that is every row. */
export function unplacedRows(list: TaskRow[], board: TaskBoard | null, lineage: Record<string, string>): TaskRow[] {
  if (!board) return list
  const placed = new Set<string>(Object.keys(lineage).map((id) => `dag:${id}`))
  for (const g of board.groups) for (const s of g.steps) for (const t of s.tasks) placed.add(`${t.kind}:${t.id}`)
  return list.filter((r) => !placed.has(rowKey(r)))
}

/* A dag row nothing places may be a stint round or a replan's successor the
   board's lineage was read without: the board is re-read once per such row. */
const rechecked = new Set<string>()

export async function noticeUnplaced(): Promise<void> {
  const s = store.get()
  const key = sessionCurrent()
  if (!s.boardLoaded || !key) return
  const fresh = unplacedRows(s.rows, s.board, s.lineage)
    .filter((r) => r.kind === 'dag' && !rechecked.has(`${key}|${rowKey(r)}`))
  if (!fresh.length) return
  for (const r of fresh) rechecked.add(`${key}|${rowKey(r)}`)
  await refreshBoard()
}

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
  const held = new Map(store.get().rows.map((r) => [rowKey(r), r]))
  if (tick === liveTick) {
    patch({ rows: answered.map((r) => live.carryOutcome(held.get(rowKey(r)), r)), loaded: true })
    return
  }
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
  const merged: TaskRow[] = []
  for (const r of answered) {
    const k = rowKey(r)
    if (!fresher(k)) { merged.push(live.carryOutcome(held.get(k), r)); continue }
    /* Held, or retired by that frame and deliberately not put back. */
    const frame = held.get(k)
    if (frame) merged.push(frame)
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
  const next = at >= 0 ? now.map((r, i) => (i === at ? live.carryOutcome(r, row) : r)) : [row, ...now]
  patch({ rows: next })
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
