/* The workspace panel's tasks view: every unit of delegated work this
 * conversation started, whichever way it was started -- a spawned subagent,
 * or a `run_subagent_dag` graph (a playbook run is one of those).
 *
 * The list is the floating palette and opening a row docks the wide pane;
 * that split is the desk's, so this renders one or the other and lets the
 * desk size itself. The steps are drawn once, as a graph on the board the
 * reader pans and zooms, docked or full screen alike -- there is no second,
 * flatter rendering of the same steps to fall out of step with the graph.
 */

import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { Glyph, SendGlyph } from '../../components/Ico'
import { t } from '../../i18n/t'
import { copy } from '../../lib/clipboard'
import { formatDuration } from '../../lib/duration'
import * as lang from '../../state/lang'
import { show as showPage } from '../../state/page'
import { ds } from '../../state/sources'
import { show as toast } from '../../state/toast'
import { Board } from '../dag/Board'
import { DagGraph } from '../dag/DagGraph'
import { layout } from '../dag/graph'
import * as desk from '../desk/store'
import * as workspace from '../workspace/store'
import { BoardCard } from './BoardCard'
import { Answer, StepList } from './NodeRecord'
import * as store from './store'

import type { Dims } from '../dag/graph'
import type { DagNode } from '../dag/types'
import type { NodeRecord, NodeStep, SubagentRow, TaskFile, TaskNode, TaskRow } from './types'
import type { JSX } from 'react'
import './styles.css'

/* The pane id the desk files this row's window under (features/desk/store.ts's
   `openDeskTask`) -- also this domain's own key for which node each pane is
   showing, so the two never drift apart. */
export const paneIdOf = (row: TaskRow): string => `task:${row.kind}:${row.id}`

/* Run / done / failed, the only three the page's shared `.dot` vocabulary
   carries a colour for. The status word beside it -- which the pane and the
   node panel both print -- is what carries the fifth and sixth states. */
const dotOf = (status: TaskRow['status'] | TaskNode['status']): string =>
  (status === 'running' ? 'run' : status === 'completed' ? 'ok' : 'bad')

/* The prototype's own three-state dot: moss while running, clay only for a
   failure or an interruption, faint grey for everything else settled
   (completed and cancelled alike). Carried as a `data-st` attribute rather
   than a new class, so the page-wide `.dot` rule every other domain reads is
   left alone -- only the task surfaces below add a colour under this
   attribute. */
const tdotState = (status: TaskRow['status']): 'run' | 'ok' | 'error' =>
  (status === 'running' ? 'run' : (status === 'failed' || status === 'interrupted') ? 'error' : 'ok')

function taskStatusWord(status: TaskRow['status']): string {
  switch (status) {
    case 'running': return t('gui.tasks.running')
    case 'completed': return t('gui.tasks.st_completed')
    case 'failed': return t('gui.tasks.st_failed')
    case 'interrupted': return t('gui.tasks.st_interrupted')
    case 'cancelled': return t('gui.tasks.st_cancelled')
  }
}

function nodeStatusWord(status: TaskNode['status']): string {
  switch (status) {
    case 'pending': return t('gui.tasks.node_st_pending')
    case 'running': return t('gui.tasks.node_st_running')
    case 'completed': return t('gui.tasks.node_st_completed')
    case 'failed': return t('gui.tasks.node_st_failed')
    case 'skipped': return t('gui.tasks.node_st_skipped')
    case 'cancelled': return t('gui.tasks.node_st_cancelled')
    case 'interrupted': return t('gui.tasks.node_st_interrupted')
    case 'exception': return t('gui.tasks.node_st_exception')
  }
}

/* How long, in ms: the running case is measured against `now`, which the
   list and the pane both re-read on a one-second clock; an interrupted row
   with no end anywhere shows nothing rather than a number that keeps
   growing. */
function taskDuration(row: TaskRow, now: number): number | null {
  if (!row.started_at) return null
  const end = row.ended_at ?? (row.status === 'running' ? now : null)
  return end == null ? null : Math.max(end - row.started_at, 0)
}

/* The list's and the pane's second line: which step, or how it ended. Every
   branch reads `counts`, never the node array itself -- a graph the server
   cascaded skips through counts them, which the old three-state guess did
   not. */
function stepLine(row: TaskRow): string {
  const c = row.counts
  if (c.total <= 1) return ''
  if (row.status === 'running') return t('gui.tasks.step', { i: c.completed + 1, n: c.total })
  if (row.status === 'completed') return t('gui.tasks.step_all_done', { n: c.total })
  if (row.status === 'interrupted') return t('gui.tasks.step_stopped', { d: c.completed, k: c.completed + 1 })
  const parts: string[] = []
  if (c.completed) parts.push(t('gui.tasks.step_done', { n: c.completed }))
  if (c.failed) parts.push(t('gui.tasks.step_failed', { n: c.failed }))
  if (c.skipped) parts.push(t('gui.tasks.step_skipped', { n: c.skipped }))
  return parts.join(' · ')
}

const productsOf = (row: TaskRow): number => row.nodes.reduce((n, node) => n + node.files.length, 0)

const productText = (n: number): string => (n ? t(n === 1 ? 'gui.tasks.artifacts_1' : 'gui.tasks.artifacts_n', { n }) : '')

function humanSize(bytes: number): string {
  if (!bytes) return ''
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1 }
  return `${unit ? value.toFixed(value < 10 ? 1 : 0) : Math.round(value)} ${units[unit]}`
}

/* Not translated, and not a word: the same token the runtime uses for the
   same condition, so a reader who sees it here and in a log is looking at
   one thing. The colour carries it; the text is the label on the colour. */
const ErrorTag = (): JSX.Element => <span className="tkerr">error</span>

/* ── the list ─────────────────────────────────────────────────────────── */

function Row({ row, now, hl, open, onOpen }: {
  row: TaskRow; now: number; hl: boolean; open: boolean; onOpen: (r: TaskRow) => void
}): JSX.Element {
  const key = store.rowKey(row)
  const dur = taskDuration(row, now)
  /* The step fragment only while the task is running -- a settled row's
     ending is already said by the group it sits in and by the products
     count; repeating the full "all steps done" sentence on every finished
     row is not what the prototype's list draws. */
  const step = row.status === 'running' ? stepLine(row) : ''
  const line = [dur != null ? formatDuration(dur) : '', step, productText(productsOf(row))]
    .filter(Boolean).join(' · ')
  return (
    <button
      type="button"
      className={'sarow task' + (hl ? ' hl' : '')}
      data-st={tdotState(row.status)}
      aria-current={open || undefined}
      onClick={() => onOpen(row)}
      onMouseEnter={() => store.hover(key)}
      onMouseLeave={() => store.hover(null)}
    >
      <span className={'dot ' + dotOf(row.status)} />
      <div className="bd">
        <div className="tline">
          <span className="nm">{row.task_summary || row.id}</span>
          {row.playbook ? <span className="tksrc">{row.playbook}</span> : null}
        </div>
        <s className="st">{line}</s>
      </div>
      {row.status === 'failed' || row.status === 'interrupted' ? <ErrorTag /> : null}
    </button>
  )
}

function Group({ label, list, now, hover, openIds, onOpen }: {
  label: string; list: TaskRow[]; now: number; hover: string | null; openIds: Set<string>
  onOpen: (r: TaskRow) => void
}): JSX.Element | null {
  if (!list.length) return null
  return (
    <>
      <div className="wsgrp">{label}</div>
      {list.map((r) => (
        <Row
          key={store.rowKey(r)} row={r} now={now} hl={hover === store.rowKey(r)}
          open={openIds.has(paneIdOf(r))} onOpen={onOpen}
        />
      ))}
    </>
  )
}

function List(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  const deskState = useSyncExternalStore(desk.subscribe, desk.get)
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!store.running(s.rows).length) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [s.rows])
  const openIds = new Set(deskState.panes.filter((p) => p.kind === 'task').map((p) => p.id))
  return (
    <div className="salist tasks">
      <Group
        label={t('gui.tasks.running')} list={store.running(s.rows)} now={now} hover={s.hover}
        openIds={openIds} onOpen={desk.openDeskTask}
      />
      <Group
        label={t('gui.tasks.settled')} list={store.settled(s.rows)} now={now} hover={s.hover}
        openIds={openIds} onOpen={desk.openDeskTask}
      />
    </div>
  )
}

export function TasksApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  /* The language the page resolved, so a pick repaints this island: every
     word below is a t(key) read at render time. */
  useSyncExternalStore(lang.subscribe, lang.get)
  useEffect(() => { if (!s.loaded) void store.refresh() }, [s.loaded])
  return <List />
}

/* ── the strip above the composer ─────────────────────────────────────── */

/* Running rows only, name only, at most three -- a status a reader watching
   something run wants at a glance, not a second reading of the pane's own
   step count. */
export function TaskRuns(): JSX.Element | null {
  const s = useSyncExternalStore(store.subscribe, store.get)
  const deskState = useSyncExternalStore(desk.subscribe, desk.get)
  useEffect(() => { if (!s.loaded) void store.refresh() }, [s.loaded])
  const live = store.running(s.rows)
  if (!live.length) return null
  const shown = live.slice(0, 3)
  const overflow = live.length - 3
  const openIds = new Set(deskState.panes.filter((p) => p.kind === 'task').map((p) => p.id))
  return (
    <div className="runs">
      <div className="tkrail">
        {shown.map((r) => {
          const key = store.rowKey(r)
          return (
            <button
              key={key}
              className={'trun' + (s.hover === key ? ' hl' : '')}
              title={r.task_summary || r.id}
              data-st={tdotState(r.status)}
              aria-current={openIds.has(paneIdOf(r)) || undefined}
              onClick={() => desk.openDeskTask(r)}
              onMouseEnter={() => store.hover(key)}
              onMouseLeave={() => store.hover(null)}
            >
              <span className={'dot ' + dotOf(r.status)} />
              <span className="nm">{r.task_summary || r.id}</span>
            </button>
          )
        })}
        {overflow > 0 ? (
          <button className="trun" data-more="true" onClick={() => desk.openDeskTab('tasks')}>
            {t('gui.tasks.overflow_more', { n: overflow })}
          </button>
        ) : null}
      </div>
    </div>
  )
}

/* ── the board ─────────────────────────────────────────────────────────── */

/* The prototype's own `RT_DIMS` (proto.js:3386): taller than the shared dag
   domain's column dims, since the board's own card (`BoardCard`) carries a
   two-line title and an agent line the shared SVG card never had to fit. */
const COLUMN: Dims = { W: 196, H: 78, GAP_X: 210, GAP_Y: 122, PAD: 12 }

function toDagNodes(row: TaskRow): DagNode[] {
  return row.nodes.map((n) => ({
    id: n.node_id, subagent: n.agent, instance: n.instance ?? null, depends_on: n.depends_on,
    status: n.status, started_at: n.started_at ?? null, ended_at: n.ended_at ?? null,
    node_summary: n.node_summary ?? null, prompt_template: n.prompt_template ?? null,
    tool_call_count: n.tool_call_count ?? null,
  }))
}

interface Lane { key: string; label: string; x: number; y: number; w: number; h: number }

/* Nodes sharing one (agent, instance) share one stateful conversation --
   a fact the arrows cannot say, since they only say "after". */
function laneRects(nodes: TaskNode[], at: Map<string, { x: number; y: number }>, dims: Dims): Lane[] {
  const groups = new Map<string, TaskNode[]>()
  nodes.forEach((n) => {
    if (!n.instance) return
    const key = `${n.agent}@${n.instance}`
    const list = groups.get(key)
    if (list) list.push(n)
    else groups.set(key, [n])
  })
  const out: Lane[] = []
  groups.forEach((members, key) => {
    if (members.length < 2) return
    const pts = members.map((m) => at.get(m.node_id)).filter((p): p is { x: number; y: number } => !!p)
    if (pts.length < 2) return
    const xs = pts.map((p) => p.x)
    const ys = pts.map((p) => p.y)
    const x0 = Math.min(...xs) - 11
    const y0 = Math.min(...ys) - 11
    const x1 = Math.max(...xs) + dims.W + 11
    const y1 = Math.max(...ys) + dims.H + 11
    out.push({ key, label: members[0]!.instance || '', x: x0, y: y0, w: x1 - x0, h: y1 - y0 })
  })
  return out
}

function Fork({ row, paneId }: { row: TaskRow; paneId: string }): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  const nodes = toDagNodes(row)
  const { at, width, height } = layout(nodes, COLUMN, 'down')
  const lanes = laneRects(row.nodes, at, COLUMN)
  return (
    <Board width={width} height={height} fitKey={paneId} label={t('gui.tasks.canvas')} owns=".nd"
      arrowsTaken={!!s.nodes[paneId]} onBlank={() => store.pickNode(paneId, null)}>
      <div className="tkfork" style={{ position: 'relative', width, height }}>
        {lanes.map((l) => (
          <div key={l.key} className="tklane" style={{ left: l.x, top: l.y, width: l.w, height: l.h }}>
            <b>{t('gui.tasks.lane', { instance: l.label })}</b>
          </div>
        ))}
        <DagGraph
          dims={COLUMN}
          nodes={nodes}
          now={Date.now()}
          surface="sheet"
          flow="down"
          selectedId={s.nodes[paneId] ?? null}
          onPick={(n) => store.pickNode(paneId, n.id)}
          renderNode={(n, now) => <BoardCard node={n} now={now} />}
        />
      </div>
    </Board>
  )
}

/* ── the node panel ────────────────────────────────────────────────────── */

interface RecordLoad {
  loading: boolean
  record: NodeRecord | null
  failed: boolean
  /** Asks for the record again -- the reader's own way past a failed fetch;
      no key in the effect's own deps array names "try again" on its own. */
  retry: () => void
}

/* The beat a running spawn's record is re-read on: the one the transcript's
   own spawn card reads `subagent.context` on (TranscriptPage.tsx), so the
   two views of one run move together. */
const SPAWN_READ_BEAT_MS = 1000

/* Fetched once per (row, node) and shared by both tabs: the order tab's
   "instruction" is the same rendered prompt the context tab's dispatch is,
   and asking for it twice would be asking the gateway the same question
   twice for one screen. Never fetched for a step that has not been
   dispatched -- there is nothing yet to read.

   Refetched on every live event that names this node (`store.nodeVersion`)
   and on every status transition, on top of the (row, node) identity.
   `dag.node_updated` fires once per tool call while a dag node runs, so a
   dag node opened mid-run keeps reading its own steps and its answer as they
   arrive rather than freezing at the first read. A spawn has no per-step
   event -- `subagent.status` moves on pending, running and the terminal word
   only -- so while one runs its record is re-read on a beat instead, skipping
   a beat while a read is still out; the status key then covers the terminal
   frame, so the answer lands without the reader closing and reopening the
   node. A stale record is kept on screen through a refetch rather than
   cleared back to `null` -- the reader is watching a node run, not watching
   it flicker blank once a second. */
function useNodeRecord(row: TaskRow, node: TaskNode): RecordLoad {
  const dispatched = node.status !== 'pending' && node.status !== 'skipped'
  const version = useSyncExternalStore(store.subscribe, () => store.nodeVersion(row.kind, row.id, node.node_id))
  const [nonce, setNonce] = useState(0)
  const [state, setState] = useState<{ loading: boolean; record: NodeRecord | null; failed: boolean }>(
    { loading: dispatched, record: null, failed: false },
  )
  const reading = useRef(false)
  useEffect(() => {
    if (!dispatched) { setState({ loading: false, record: null, failed: false }); return }
    setState((prev) => ({ loading: true, record: prev.record, failed: false }))
    let alive = true
    const src = store.source()
    if (!src) { setState((prev) => ({ loading: false, record: prev.record, failed: true })); return }
    reading.current = true
    /* The row too, while the node runs: its usage and tool counts grow on the
       server as the lane reports them (`tasks.list` reads the live activity),
       and no frame carries them -- so the subtitle's token total moves with
       the record. Once the node settles, the terminal frame's own reconcile
       brings the final copy. */
    if (node.status === 'running') void store.reconcile(row.kind, row.id)
    src.node(row, node)
      .then((r) => { if (alive) setState({ loading: false, record: r, failed: false }) })
      .catch(() => { if (alive) setState((prev) => ({ loading: false, record: prev.record, failed: true })) })
      .finally(() => { if (alive) reading.current = false })
    return () => { alive = false; reading.current = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row.kind, row.id, node.node_id, node.status, version, nonce])
  /* No source, no beat: with nothing to read from, a beat would only re-run
     the effect into its failed branch once a second. */
  const beating = row.kind === 'spawn' && node.status === 'running' && !!store.source()
  useEffect(() => {
    if (!beating) return
    const beat = setInterval(() => { if (!reading.current) setNonce((n) => n + 1) }, SPAWN_READ_BEAT_MS)
    return () => clearInterval(beat)
  }, [beating])
  return { ...state, retry: () => setNonce((n) => n + 1) }
}

/* Grouped by thousands, the way the prototype's own `fmtN` reads a token
   count -- "4,321 tokens" rather than "4321 tokens". */
const fmtN = (n: number): string => n.toLocaleString('en-US')

/* The rest of the subtitle, after the agent (which the caller sets apart in
   its own `<b>`): status word, duration, tokens -- each pushed only when the
   fact is there to push. A lane that never reported usage is a fact this
   line says nothing about, not a line that says "not reported"; the tool
   count is the board card's, and the calls themselves are the process
   fold's, so neither is this line's. */
function nodeSubtitleRest(node: TaskNode): string[] {
  const parts = [nodeStatusWord(node.status)]
  const dur = node.started_at ? formatDuration((node.ended_at ?? Date.now()) - node.started_at) : ''
  if (dur) parts.push(dur)
  if (node.tokens_in != null || node.tokens_out != null) {
    parts.push(t('gui.tasks.tokens_n', { n: fmtN((node.tokens_in || 0) + (node.tokens_out || 0)) }))
  }
  if (node.status === 'completed' && node.has_output === false) parts.push(t('gui.tasks.no_output'))
  return parts
}

/* The node panel head's subtitle line: the agent in its own `<b>`, the rest
   of the sentence plain after it. The agent alone, without its handle: a
   spawn's handle is a minted id (`TaskRow.handle`), and the work-order tab
   already names the instance for the reader who wants it. */
function NodeSubtitle({ node }: { node: TaskNode }): JSX.Element {
  const rest = nodeSubtitleRest(node)
  return <span className="tksub"><b>{node.agent}</b>{rest.length ? ' · ' + rest.join(' · ') : ''}</span>
}

/* Why a step nobody dispatched has nothing to read: skipped names the
   upstream that never gave it a conclusion, when one is findable; an
   interrupted step that never got as far as a record says the gateway went
   away first; pending just has not been reached yet. */
function nodeWhyText(node: TaskNode, row: TaskRow): string {
  if (node.status === 'interrupted') return t('gui.tasks.ctx_why_interrupted')
  if (node.status !== 'skipped') return ''
  const bad = node.depends_on
    .map((id) => row.nodes.find((n) => n.node_id === id))
    .find((n): n is TaskNode => !!n && (n.status === 'failed' || n.status === 'skipped'))
  return bad ? t('gui.tasks.skip_why_named', { name: bad.node_summary || bad.node_id }) : t('gui.tasks.skip_why')
}

/* HH:MM, zero-padded, in the reader's own timezone -- the timestamp a
   dispatch or an answer wears in its footer. */
function hhmm(ms: number): string {
  const d = new Date(ms)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/* The marker tokens alone, not the whole BEGIN..END span: the injected body
   between them stays ordinary prose, and a truncated prompt carrying only a
   BEGIN marker (its matching END never arrived) still gets that one line
   marked rather than none of it. */
const UNTRUSTED_MARKER = /(\[(?:BEGIN|END) UNTRUSTED[^\]]*\])/g
const IS_UNTRUSTED_MARKER = /^\[(?:BEGIN|END) UNTRUSTED/
/* A character count, not a line count -- the prototype folds on length so a
   long one-line prompt still clamps and a short eight-line one does not. */
const CLAMP_CHARS = 260

function Dispatch({ text, at, nodeKey }: { text: string; at: number | null; nodeKey: string }): JSX.Element {
  const touched = useSyncExternalStore(store.subscribe, () => store.foldOf(nodeKey, 'wide'))
  const open = touched ?? false
  const long = text.length >= CLAMP_CHARS
  return (
    <div className="tkdisp">
      <div className={'tkdispb' + (long && !open ? ' tkclamp' : '')}>
        {text.split(UNTRUSTED_MARKER).map((part, i) => (
          IS_UNTRUSTED_MARKER.test(part)
            ? <span className="tkuntrusted" key={i}>{part}</span>
            : <span key={i}>{part}</span>
        ))}
      </div>
      {long
        ? (
          <button className="tkmore" aria-expanded={open} onClick={() => store.setFold(nodeKey, 'wide', !open)}>
            {t(open ? 'gui.tasks.rec_less' : 'gui.tasks.rec_more')}
          </button>
        )
        : null}
      <div className="tkansfoot">
        <button
          className="tkfootcopy" aria-label={t('gui.tasks.copy')}
          onClick={() => copy(text, t('gui.tasks.copied_dispatch'))}
        >
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
            <rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
          </svg>
        </button>
        {at != null ? <span className="tkturnmeta">{hhmm(at)}</span> : null}
      </div>
    </div>
  )
}

/* The process, folded once it is over and open while it is not: a run of any
   length is mostly steps a reader does not need mid-flight, but the one
   under way is the thing their eye should find. Each step's own rendering --
   the thought, what it said, the calls it led to and their detail cards --
   is NodeRecord.tsx's `StepList` (proto.js's `stepView` / `callRow` /
   `plainDtl` / `delegDtl`). */
function Process({ steps, node, nodeKey }: { steps: NodeStep[]; node: TaskNode; nodeKey: string }): JSX.Element | null {
  /* `undefined` (never touched) recomputes the default every render rather
     than freezing it at mount: open while the node is the only thing moving,
     closed the moment there is a conclusion to read instead -- a node
     watched from running to completed folds itself, and one the reader
     already opened or closed stays that way regardless of status. */
  const touched = useSyncExternalStore(store.subscribe, () => store.foldOf(nodeKey, 'proc'))
  if (!steps.length) return null
  const open = touched ?? node.status === 'running'
  const dur = node.ended_at && node.started_at ? formatDuration(node.ended_at - node.started_at) : ''
  return (
    <div className="tkproc">
      <button
        className="tkprock" aria-expanded={open} aria-label={t('gui.tasks.rec_process_aria')}
        onClick={() => store.setFold(nodeKey, 'proc', !open)}
      >
        <span>{t(node.status === 'running' ? 'gui.tasks.rec_process' : 'gui.tasks.st_completed')}</span>
        {dur ? <span className="tkprocn">{dur}</span> : null}
        <Glyph d="M9.5 6.5 15 12l-5.5 5.5" cls="tkchev" />
      </button>
      {open ? <div className="tkprocb"><StepList steps={steps} running={node.status === 'running'} nodeKey={nodeKey} /></div> : null}
    </div>
  )
}

/* "Continue with X" -- only when the agent keeps a session and this node has
   one. #488 left the running conversation's own second input box out on
   purpose; this is the primitive without committing to where a reply would
   be read. */
function ChatDock({ node, roster }: { node: TaskNode; roster: SubagentRow[] }): JSX.Element | null {
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const agentRow = roster.find((r) => r.name === node.agent)
  if (!agentRow?.stateful || !node.instance) return null
  const instance = node.instance
  const send = (): void => {
    const value = text.trim()
    if (!value || busy) return
    setBusy(true)
    Promise.resolve(ds('subagents').instanceSend?.(node.agent, instance, value))
      .then(() => setText(''))
      .finally(() => setBusy(false))
  }
  return (
    <div className="tkdock">
      <input
        type="text" value={text} onChange={(e) => setText(e.target.value)}
        placeholder={t('gui.tasks.continue_with', { name: node.agent })}
        onKeyDown={(e) => { if (e.key === 'Enter' && !e.nativeEvent.isComposing) send() }}
      />
      <button className="go" aria-label={t('gui.send')} disabled={!text.trim() || busy} onClick={send}>
        <SendGlyph />
      </button>
    </div>
  )
}

function ContextTab({ row, node, rec }: {
  row: TaskRow; node: TaskNode; rec: RecordLoad
}): JSX.Element | null {
  if (node.status === 'pending' || node.status === 'skipped') {
    return <p className="tkempty">{nodeWhyText(node, row) || t('gui.tasks.ctx_none')}</p>
  }
  /* Something on screen for the whole of the fetch, and on a failed one too:
     the prototype's own transcript is local data with no fetch to wait on or
     fail, so it never has either case to say -- but it also never shows
     nothing, which a silently blank card leaves the reader unable to tell
     apart from a node that truly has no record. A record already on screen
     from an earlier read (a live event refetching it) stays up rather than
     being cleared back to either state. */
  if (!rec.record) {
    if (rec.loading) return <p className="tkempty">{t('gui.tasks.ctx_loading')}</p>
    if (rec.failed) {
      return (
        <div className="tkerrb">
          {t('gui.tasks.ctx_load_failed')}
          <button type="button" className="tkretry" onClick={rec.retry}>{t('gui.retry')}</button>
        </div>
      )
    }
    return <p className="tkempty">{nodeWhyText(node, row) || t('gui.tasks.ctx_none')}</p>
  }
  const record = rec.record
  /* Keyed on the absence of a dispatched prompt, like the prototype's own
     `!n.prompt` gate -- a node whose record has not been read yet reads the
     same "nothing here" line as one the run never reached. */
  if (!record.dispatch) {
    return <p className="tkempty">{nodeWhyText(node, row) || t('gui.tasks.ctx_none')}</p>
  }
  const nodeKey = store.rowKey(row) + ':' + node.node_id
  return (
    <div className="tkctx">
      <Dispatch text={record.dispatch} at={node.started_at ?? null} nodeKey={nodeKey} />
      <Process steps={record.steps} node={node} nodeKey={nodeKey} />
      {record.answer ? <Answer text={record.answer} at={node.ended_at ?? null} running={node.status === 'running'} /> : null}
      {!record.answer && node.status === 'failed'
        ? (
          <div className="tkerrb">
            {/* This node's own error, or -- when it failed without leaving
               one of its own -- the run-level reason a sibling node already
               carries, rather than the bare fallback sentence. */}
            {node.error || row.nodes.find((n) => n.error)?.error || t('gui.tasks.ctx_failed')}
          </div>
        )
        : null}
      {!record.answer && node.status === 'interrupted'
        ? <div className="tkerrb">{t('gui.tasks.ctx_interrupted')}</div>
        : null}
      {record.outputTruncated ? <div className="tktrunc">{t('gui.tasks.ctx_truncated')}</div> : null}
    </div>
  )
}

/* Two placeholder shapes, each marked its own way: `{{...}}` is a template
   slot filled in at dispatch, `${...}` is one already resolved (the
   prototype's `now` / `run` classes) -- so a reader can tell "waiting on
   upstream" from "resolved" at a glance rather than reading both the same. */
const PLACEHOLDER = /(\{\{[^}]*\}\}|\$\{[^}]*\})/g
const IS_TEMPLATE_PLACEHOLDER = /^\{\{[^}]*\}\}$/

function Template({ text, label }: { text: string; label: string }): JSX.Element {
  return (
    <div className="tkfield">
      <div className="tkfk">{label}</div>
      <div className="tkfv">
        <pre className="tkpv">
          {text.split(PLACEHOLDER).map((part, i) => (
            IS_TEMPLATE_PLACEHOLDER.test(part)
              ? <mark className="tkph" key={i}>{part}</mark>
              : (part.startsWith('${') ? <mark className="tkphnow" key={i}>{part}</mark> : <span key={i}>{part}</span>)
          ))}
        </pre>
      </div>
    </div>
  )
}

/* Each skill or MCP as its own tag, not a comma-joined sentence -- the
   prototype's `specTags`. */
function Tags({ items }: { items: string[] }): JSX.Element {
  return <>{items.map((s) => <span className="tktag" key={s}>{s}</span>)}</>
}

/* The kind of value beside the value itself, the way the prototype's
   `inputValue` reads an `{file: ...}` / `{node: ...}` shape -- two chips
   rather than a brace-syntax string in the reader's face. Not translated:
   `file` and `node` name a shape in the data, the way the error tag names a
   condition, not a sentence. */
function InputValue({ v }: { v: unknown }): JSX.Element {
  if (v && typeof v === 'object' && !Array.isArray(v)) {
    const o = v as Record<string, unknown>
    if (typeof o.file === 'string') return <><span className="tkikind">file</span><span className="tkival">{o.file}</span></>
    if (typeof o.node === 'string') return <><span className="tkikind">node</span><span className="tkival">{o.node}</span></>
  }
  return <span className="tkival">{typeof v === 'string' ? v : JSON.stringify(v)}</span>
}

/* A `depends_on` entry (or an `inputs` value shaped `{node: id}`) that names
   no node of this task refers to a node of a different run: node ids are
   unique inside one session, not across it, so a later graph can depend on an
   earlier one's completed node. The board draws only this task's own nodes
   and already leaves such an id undrawn; this is the work order's own line
   for it. */
function externalDeps(node: TaskNode, row: TaskRow): string[] {
  const known = new Set(row.nodes.map((n) => n.node_id))
  const ids = new Set<string>()
  node.depends_on.forEach((id) => { if (!known.has(id)) ids.add(id) })
  Object.values(node.inputs || {}).forEach((v) => {
    const id = v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>).node : null
    if (typeof id === 'string' && !known.has(id)) ids.add(id)
  })
  return [...ids]
}

function OrderTab({ row, node, roster, rec }: {
  row: TaskRow; node: TaskNode; roster: SubagentRow[]; rec: RecordLoad
}): JSX.Element {
  /* A registered-but-disabled agent is still on this machine -- naming it
     missing here would be wrong in that case, not just imprecise. */
  const known = !node.agent || roster.some((r) => r.name === node.agent)
  const dispatched = node.status !== 'pending' && node.status !== 'skipped'
  const rendered = dispatched ? rec.record?.dispatch ?? null : null
  /* A dispatched node's record still in flight (and never yet read) is not
     "waiting on upstream to fill in" -- it already went out. Showing the raw
     template plus that note during the fetch would state something false
     about a run that has already happened, so this shows a neutral "reading
     it" placeholder instead until the rendered prompt lands or the fetch
     gives up. */
  const pendingFetch = dispatched && rec.loading && rendered == null
  const text = rendered ?? (pendingFetch ? null : node.prompt_template ?? null)
  const inputs = node.inputs ? Object.entries(node.inputs) : []
  const external = externalDeps(node, row)
  return (
    <div className="tkorder">
      <div className="tkfield">
        <div className="tkfk">{t('gui.tasks.assigned_to')}</div>
        <div className="tkfv">
          {!node.agent
            ? t('gui.tasks.left_blank')
            : (
              <>
                <span className={'tkagent' + (known ? '' : ' tkmiss')}>{node.agent}</span>
                {!known
                  ? <button className="tkfix" onClick={() => showPage('extAgentsPage')}>{t('gui.tasks.agent_missing')}</button>
                  : null}
                {node.instance ? <span className="tkhd">{t('gui.tasks.instance_note', { instance: node.instance })}</span> : null}
              </>
            )}
        </div>
      </div>
      {external.map((id) => (
        <p className="tknote" key={id}>
          {t('gui.tasks.depends_on_pre')} <b className="mono">{id}</b> {t('gui.tasks.depends_on_post')}
        </p>
      ))}
      <div className="tkfield">
        <div className="tkfk">{t('gui.tasks.skills')}</div>
        <div className="tkfv">
          {node.skills && node.skills.length ? <Tags items={node.skills} /> : t('gui.tasks.skills_none')}
        </div>
      </div>
      <div className="tkfield">
        <div className="tkfk">{t('gui.tasks.mcps')}</div>
        <div className="tkfv">
          {node.mcps && node.mcps.length ? <Tags items={node.mcps} /> : t('gui.tasks.mcps_none')}
        </div>
      </div>
      {inputs.length
        ? (
          <div className="tkfield">
            <div className="tkfk">{t('gui.tasks.inputs')}</div>
            <dl className="tkkv">
              {inputs.map(([k, v]) => <div key={k}><dt>{k}</dt><dd><InputValue v={v} /></dd></div>)}
            </dl>
          </div>
        )
        : null}
      {text
        ? <Template text={text} label={rendered ? t('gui.tasks.instruction') : t('gui.tasks.instruction_template')} />
        : (
          <div className="tkfield">
            <div className="tkfk">{t('gui.tasks.instruction')}</div>
            <div className="tkfv">
              {pendingFetch ? t('gui.tasks.instruction_loading') : t('gui.tasks.instruction_pending')}
            </div>
          </div>
        )}
      {!rendered && !pendingFetch && node.prompt_template ? <p className="tknote">{t('gui.tasks.instruction_note')}</p> : null}
      <div className="tkspecid">
        <span>{row.kind === 'spawn' ? t('gui.tasks.call_label') : t('gui.tasks.run_label')}</span>
        <b>{row.id}</b>
        <button className="tkspeccopy" aria-label={t('gui.tasks.copy')} onClick={() => copy(row.id, t('gui.tasks.copied'))}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
            <rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
          </svg>
        </button>
      </div>
    </div>
  )
}

function NodePanel({ row, node, paneId, onClose, roster }: {
  row: TaskRow; node: TaskNode; paneId: string; onClose: () => void; roster: SubagentRow[]
}): JSX.Element {
  useSyncExternalStore(store.subscribe, store.get)
  const rec = useNodeRecord(row, node)
  const pinned = store.tabOf(paneId)
  /* A skipped step opens on the context tab too -- it has no order to
     dispatch, but it does have a reason it never ran, and that reason is
     what the context tab reads first (`nodeWhyText`). Only a step still
     ahead of the run (`pending`) opens on the work order by default. */
  const tab = pinned ?? (node.status === 'pending' ? 'order' : 'context')
  return (
    <div className="tkcard">
      <div className="tkch">
        <button className="tkback" onClick={onClose} aria-label={t('gui.tasks.back')} title={t('gui.tasks.back')}>
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
            <path d="M15 5l-7 7 7 7" />
          </svg>
        </button>
        <div className="tktt">
          <b>{node.node_summary || node.node_id}</b>
          <NodeSubtitle node={node} />
        </div>
        <div className="tktabs" role="tablist">
          {(['context', 'order'] as const).map((k) => (
            <button key={k} role="tab" aria-selected={tab === k} onClick={() => store.pickTab(paneId, k)}>
              {t(k === 'context' ? 'gui.tasks.tab_context' : 'gui.tasks.tab_order')}
            </button>
          ))}
        </div>
      </div>
      {/* The one scrolling child: the header above stays put and, on the
         context tab, the reply dock below stays pinned at the bottom --
         neither rides off the top or the end of a long record. */}
      <div className="tkbody">
        {tab === 'context'
          ? <ContextTab row={row} node={node} rec={rec} />
          : <OrderTab row={row} node={node} roster={roster} rec={rec} />}
      </div>
      {tab === 'context' && node.status !== 'pending' && node.status !== 'skipped'
        ? <ChatDock node={node} roster={roster} />
        : null}
    </div>
  )
}

/* ── the pane ──────────────────────────────────────────────────────────── */

function StopButton({ row }: { row: TaskRow }): JSX.Element {
  const [busy, setBusy] = useState(false)
  return (
    <button
      className="tkbaract"
      disabled={busy}
      title={t('gui.tasks.stop_title')}
      onClick={() => {
        /* Said the moment the request goes out, not once the reconciled row
           lands: the button greying out is not itself an answer to "did that
           work", and the gap between the click and the next status word is
           exactly where the prototype's own toast fires. */
        toast(t('gui.tasks.stop_requested'))
        setBusy(true)
        void store.stop(row).finally(() => setBusy(false))
      }}
    >
      <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M7.5 7.5h9v9h-9z" /></svg>
      {t('gui.tasks.stop')}
    </button>
  )
}

/* "stuck at: " + the name in its own `<b>`, the way the prototype's `.at`
   span sets the node name apart from the rest of the sentence. The name
   sits at the end of the sentence in both languages, so the catalogue entry
   is the prefix alone -- no interpolation to split back apart. */
function AtLine({ name }: { name: string }): JSX.Element {
  return <span className="tkwhyat">{t('gui.tasks.why_at')}<b>{name}</b></span>
}

function WhyBanner({ row, onPick }: { row: TaskRow; onPick: (id: string) => void }): JSX.Element | null {
  const replan = row.replan
  /* A replan that started reads as cancelled rather than failed (contract
     rule 0), and its banner names the successor instead of a bad node --
     there is nothing here that failed, just a run this one handed off from. */
  if (replan?.started) {
    const successor = store.byKey('dag', replan.run_id)
    return (
      <div className="tkwhy">
        <p>{t('gui.tasks.replan_superseded')}</p>
        {successor
          ? (
            <button className="tkwhyat" onClick={() => desk.openDeskTask(successor)}>
              {t('gui.tasks.replan_at', { id: replan.run_id })}
            </button>
          )
          : <p className="tknote">{replan.run_id}</p>}
      </div>
    )
  }
  const cold = row.status === 'interrupted'
  if (row.status !== 'failed' && !cold) return null
  const bad = row.nodes.find((n) => n.status === 'failed' || n.status === 'interrupted')
  /* A replan that never started (the row still reads failed) explains itself
     through `replan.error`, not through a node's own error -- the successor
     is what did not come up, and no node here is the reason why. */
  const text = replan
    ? (replan.error || t('gui.tasks.why_failed_fallback'))
    : (cold ? t('gui.tasks.why_interrupted') : (bad?.error || t('gui.tasks.why_failed_fallback')))
  /* The whole banner is the control, not just the name inside it -- the
     prototype makes the entire `.taskwhy` clickable rather than carving out
     one span of it. */
  return (
    <div
      className={'tkwhy' + (cold ? ' tkcold' : '')}
      role={bad ? 'button' : undefined}
      tabIndex={bad ? 0 : undefined}
      style={bad ? { cursor: 'pointer' } : undefined}
      onClick={bad ? () => onPick(bad.node_id) : undefined}
      onKeyDown={bad ? (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onPick(bad.node_id) } } : undefined}
    >
      <p>{text}</p>
      {bad ? <AtLine name={bad.node_summary || bad.node_id} /> : null}
    </div>
  )
}

function StatusBar({ row, now }: { row: TaskRow; now: number }): JSX.Element {
  const dur = taskDuration(row, now)
  const line = stepLine(row)
  return (
    <div className="tkbar" data-st={tdotState(row.status)}>
      <span className={'dot ' + dotOf(row.status)} />
      <span className="st">{taskStatusWord(row.status)}</span>
      {dur != null ? <span>{'· ' + formatDuration(dur)}</span> : null}
      {line ? <span>{'· ' + line}</span> : null}
      {row.status === 'running' ? <StopButton row={row} /> : null}
    </div>
  )
}

/* The pane's own resolution of a node's write / edit file into something to
   open. A diff's actual patch body is not on the wire: it is read from this
   node's own tool calls, every one that touched the path, in order
   (`diffs.ts`), which is what the folded chip's counts add up. */
async function openNodeDiff(row: TaskRow, node: TaskNode, file: TaskFile): Promise<void> {
  desk.openDeskDiff(await store.fileDiffChange(row, node, file))
}

const DocGlyph = (): JSX.Element => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
    <path d="M7 3.5h7L18.5 8v12.5h-11z" />
    <path d="M13.5 3.5V8H18.5" />
  </svg>
)
const DiffGlyph = (): JSX.Element => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
    <path d="M7 4h10a3 3 0 0 1 3 3v10a3 3 0 0 1-3 3H7a3 3 0 0 1-3-3V7a3 3 0 0 1 3-3z" />
    <path d="M8 12h8" /><path d="M12 8v8" />
  </svg>
)

/* Written files first, then changes -- every node's writes before any node's
   edits, regardless of which node produced which. The prototype walks the
   two lists separately (`taskFiles` then `taskDiffs`) rather than
   interleaving them in node order. */
function Chips({ row }: { row: TaskRow }): JSX.Element | null {
  const all: Array<{ node: TaskNode; file: TaskFile }> = []
  row.nodes.forEach((n) => n.files.forEach((f) => all.push({ node: n, file: f })))
  if (!all.length) return null
  const writes = all.filter(({ file }) => file.op === 'write')
  const edits = all.filter(({ file }) => file.op !== 'write')
  return (
    <div className="tkchips">
      {writes.map(({ node, file }) => {
        const name = file.path.split('/').pop() || file.path
        return (
          <button className="wchip" key={node.node_id + ':' + file.path} onClick={() => workspace.openPath(file.path)}>
            <DocGlyph />
            {name}{file.size != null ? <span className="tkkd">{humanSize(file.size)}</span> : null}
          </button>
        )
      })}
      {edits.map(({ node, file }) => {
        const name = file.path.split('/').pop() || file.path
        return (
          <button className="wchip" key={node.node_id + ':' + file.path} onClick={() => void openNodeDiff(row, node, file)}>
            <DiffGlyph />
            {name}
            <span className="tkdstat">
              <b className="add">+{file.add}</b> <b className="del">&minus;{file.del}</b>
            </span>
          </button>
        )
      })}
    </div>
  )
}

export function TaskPane({ task, full = false }: { task: TaskRow; full?: boolean }): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  /* The store's own row for this pane, not the snapshot the desk opened it
     with (`pane.row`, taken once when the pane opened): a live event moves
     the store forward while a held snapshot does not, which left a pane open
     on a stopped run still showing "running" and its stop button. Falls back
     to the snapshot only while the store holds no row for this (kind, id) yet. */
  const row = s.rows.find((r) => r.kind === task.kind && r.id === task.id) || task
  const [now, setNow] = useState(Date.now())
  const [roster, setRoster] = useState<SubagentRow[]>([])
  useEffect(() => {
    if (row.status !== 'running') return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [row.status])
  useEffect(() => { void store.source()?.roster().then(setRoster).catch(() => {}) }, [])
  const paneId = paneIdOf(row)
  const mine = s.nodes[paneId] ?? null
  const picked = mine && row.nodes.some((n) => n.node_id === mine) ? mine : null
  const node = row.nodes.find((n) => n.node_id === picked) || null
  const pick = (id: string | null): void => store.pickNode(paneId, id)
  return (
    <div className={'tkview' + (full ? ' full' : '')} data-detail={!!node}>
      <StatusBar row={row} now={now} />
      <WhyBanner row={row} onPick={pick} />
      <Chips row={row} />
      {/* The board stays mounted either way, and CSS -- not this ternary --
          decides whether it or the node panel is what shows: the prototype
          hides its own board with a display rule rather than tearing it
          down, so the reader's pan and zoom are still there when a docked
          pane's picked node closes. Unmounting `Fork` here snapped the graph
          back to its opening frame on every pick/back round trip. */}
      {full
        ? (
          <div className="tkwork">
            <Fork row={row} paneId={paneId} />
            {node ? <NodePanel row={row} node={node} paneId={paneId} onClose={() => pick(null)} roster={roster} /> : null}
          </div>
        )
        : (
          <>
            <Fork row={row} paneId={paneId} />
            {node ? <NodePanel row={row} node={node} paneId={paneId} onClose={() => pick(null)} roster={roster} /> : null}
          </>
        )}
    </div>
  )
}
