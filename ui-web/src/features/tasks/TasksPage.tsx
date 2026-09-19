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

import { useEffect, useState, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import { copy } from '../../lib/clipboard'
import { formatDuration } from '../../lib/duration'
import * as lang from '../../state/lang'
import { show as showPage } from '../../state/page'
import { ds } from '../../state/sources'
import { Board } from '../dag/Board'
import { DagGraph } from '../dag/DagGraph'
import { layout } from '../dag/graph'
import * as desk from '../desk/store'
import * as workspace from '../workspace/store'
import { hunksForFile } from './diffs'
import * as store from './store'

import type { Dims } from '../dag/graph'
import type { DagNode } from '../dag/types'
import type { WsChange } from '../workspace/types'
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
  /* A running row stuck on a suspended node is not merely "in progress": the
     main agent has to answer resolve_dag_node before it moves again, which
     reads differently from ordinary progress in the list, the pane and
     anywhere else this line is drawn. */
  const awaiting = row.status === 'running' && c.exception ? t('gui.tasks.step_awaiting', { n: c.exception }) : ''
  if (c.total <= 1) return awaiting
  if (row.status === 'running') {
    return [t('gui.tasks.step', { i: c.completed + 1, n: c.total }), awaiting].filter(Boolean).join(' · ')
  }
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

function Row({ row, now, hl, onOpen }: {
  row: TaskRow; now: number; hl: boolean; onOpen: (r: TaskRow) => void
}): JSX.Element {
  const key = store.rowKey(row)
  const dur = taskDuration(row, now)
  const line = [dur != null ? formatDuration(dur) : '', stepLine(row), productText(productsOf(row))]
    .filter(Boolean).join(' · ')
  return (
    <div
      className={'sarow task' + (hl ? ' hl' : '')}
      role="button"
      tabIndex={0}
      onClick={() => onOpen(row)}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen(row) } }}
      onMouseEnter={() => store.hover(key)}
      onMouseLeave={() => store.hover(null)}
    >
      <span className={'dot ' + dotOf(row.status)} />
      <div className="bd">
        <div className="tline">
          <span className="nm" title={row.task_summary || row.id}>{row.task_summary || row.id}</span>
          {row.status === 'failed' ? <ErrorTag /> : null}
          {row.playbook ? <span className="tksrc">{row.playbook}</span> : null}
        </div>
        <div className="st">{line}</div>
      </div>
    </div>
  )
}

function Group({ label, list, now, hover, onOpen }: {
  label: string; list: TaskRow[]; now: number; hover: string | null; onOpen: (r: TaskRow) => void
}): JSX.Element | null {
  if (!list.length) return null
  return (
    <>
      <div className="wsgrp">{label} {list.length}</div>
      {list.map((r) => (
        <Row key={store.rowKey(r)} row={r} now={now} hl={hover === store.rowKey(r)} onOpen={onOpen} />
      ))}
    </>
  )
}

function List(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    if (!store.running(s.rows).length) return
    const id = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(id)
  }, [s.rows])
  if (!s.rows.length) return <div className="wsempty">{t('gui.tasks.none')}</div>
  return (
    <div className="salist tasks">
      <Group label={t('gui.tasks.running')} list={store.running(s.rows)} now={now} hover={s.hover} onOpen={desk.openDeskTask} />
      <Group label={t('gui.tasks.settled')} list={store.settled(s.rows)} now={now} hover={s.hover} onOpen={desk.openDeskTask} />
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
  useEffect(() => { if (!s.loaded) void store.refresh() }, [s.loaded])
  const live = store.running(s.rows)
  if (!live.length) return null
  const shown = live.slice(0, 3)
  const overflow = live.length - 3
  return (
    <div className="runs" role="group" aria-label={t('gui.tasks.running')}>
      {shown.map((r) => {
        const key = store.rowKey(r)
        return (
          <button
            key={key}
            className={'trun' + (s.hover === key ? ' hl' : '')}
            title={r.task_summary || r.id}
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
  )
}

/* ── the board ─────────────────────────────────────────────────────────── */

const COLUMN: Dims = { W: 200, H: 62, GAP_X: 232, GAP_Y: 116, PAD: 16 }

/* The board reuses the dag domain's own card as-is (contributing.md's
   "minimal additions only" -- a domain sheet of dag's own would be a bigger
   change than this board warrants); the tool count and failure count that
   have no room on that card are said in the node panel's header instead
   (`nodeSubtitle`), which already has the room for them. */
function toDagNodes(row: TaskRow): DagNode[] {
  return row.nodes.map((n) => ({
    id: n.node_id, subagent: n.agent, instance: n.instance ?? null, depends_on: n.depends_on,
    status: n.status, started_at: n.started_at ?? null, ended_at: n.ended_at ?? null,
    node_summary: n.node_summary ?? null, prompt_template: n.prompt_template ?? null,
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
      arrowsTaken={!!s.nodes[paneId]}>
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
}

/* Fetched once per (row, node) and shared by both tabs: the order tab's
   "instruction" is the same rendered prompt the context tab's dispatch is,
   and asking for it twice would be asking the gateway the same question
   twice for one screen. Never fetched for a step that has not been
   dispatched -- there is nothing yet to read. */
function useNodeRecord(row: TaskRow, node: TaskNode): RecordLoad {
  const dispatched = node.status !== 'pending' && node.status !== 'skipped'
  const [state, setState] = useState<RecordLoad>({ loading: dispatched, record: null, failed: false })
  useEffect(() => {
    if (!dispatched) { setState({ loading: false, record: null, failed: false }); return }
    setState({ loading: true, record: null, failed: false })
    let alive = true
    const src = store.source()
    if (!src) { setState({ loading: false, record: null, failed: true }); return }
    src.node(row, node)
      .then((r) => { if (alive) setState({ loading: false, record: r, failed: false }) })
      .catch(() => { if (alive) setState({ loading: false, record: null, failed: true }) })
    return () => { alive = false }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [row.kind, row.id, node.node_id, dispatched])
  return state
}

function tokensText(node: TaskNode): string {
  if (node.status === 'pending' || node.status === 'skipped') return ''
  if (node.tokens_in == null && node.tokens_out == null) return t('gui.tasks.tokens_none')
  return t('gui.tasks.tokens_n', { n: (node.tokens_in || 0) + (node.tokens_out || 0) })
}

function nodeSubtitle(node: TaskNode): string {
  const parts = [node.agent + (node.instance ? ' @' + node.instance : ''), nodeStatusWord(node.status)]
  const dur = node.started_at ? formatDuration((node.ended_at ?? Date.now()) - node.started_at) : ''
  if (dur) parts.push(dur)
  const tk = tokensText(node)
  if (tk) parts.push(tk)
  parts.push(node.tool_call_count == null
    ? '—'
    : (node.tool_failure_count
      ? t('gui.tasks.tools_n', { n: node.tool_call_count }) + ' · ' + t('gui.tasks.tools_failed_n', { n: node.tool_failure_count })
      : t('gui.tasks.tools_n', { n: node.tool_call_count })))
  if (node.status === 'completed' && node.has_output === false) parts.push(t('gui.tasks.no_output'))
  return parts.join(' · ')
}

/* Why a step nobody dispatched has nothing to read: skipped names the
   upstream that never gave it a conclusion, when one is findable; pending
   just has not been reached yet. */
function nodeWhyText(node: TaskNode, row: TaskRow): string {
  if (node.status !== 'skipped') return ''
  const bad = node.depends_on
    .map((id) => row.nodes.find((n) => n.node_id === id))
    .find((n): n is TaskNode => !!n && (n.status === 'failed' || n.status === 'skipped'))
  return bad ? t('gui.tasks.skip_why_named', { name: bad.node_summary || bad.node_id }) : t('gui.tasks.skip_why')
}

const UNTRUSTED = /(\[BEGIN UNTRUSTED[^\]]*\][\s\S]*?\[END UNTRUSTED[^\]]*\])/g
const IS_UNTRUSTED = /^\[BEGIN UNTRUSTED/
const CLAMP_LINES = 8

function Dispatch({ text }: { text: string }): JSX.Element {
  const [open, setOpen] = useState(false)
  const long = text.split('\n').length > CLAMP_LINES
  return (
    <div className="tkdisp">
      <div className={'tkdispb' + (long && !open ? ' clamp' : '')}>
        {text.split(UNTRUSTED).map((part, i) => (
          IS_UNTRUSTED.test(part)
            ? <span className="tkuntrusted" key={i}>{part}</span>
            : <span key={i}>{part}</span>
        ))}
      </div>
      {long
        ? (
          <button className="tkmore" onClick={() => setOpen(!open)}>
            {t(open ? 'gui.tasks.rec_less' : 'gui.tasks.rec_more')}
          </button>
        )
        : null}
    </div>
  )
}

function Step({ step }: { step: NodeStep }): JSX.Element {
  const [open, setOpen] = useState(false)
  if (step.kind === 'think') {
    return (
      <div className="tkthink">
        <button className="tkthh" onClick={() => setOpen(!open)}>{t('gui.tasks.rec_thought')}</button>
        {open ? <blockquote>{step.text}</blockquote> : null}
      </div>
    )
  }
  if (step.kind === 'console') return <div className="tkconsole">{t('gui.tasks.ctx_console')}</div>
  return (
    <div className="tktool">
      <button className="tkth" onClick={() => setOpen(!open)}>
        <b className="mono">{step.name}</b>
        <span className="tkta">{step.args}</span>
        {step.result != null ? <span className="tktr">{step.result}</span> : null}
        <span className="tkchev">&rsaquo;</span>
      </button>
      {open ? <pre className="tktd">{step.args + '\n\n' + (step.result || '')}</pre> : null}
    </div>
  )
}

/* The process, folded once it is over and open while it is not: a run of any
   length is mostly steps a reader does not need mid-flight, but the one
   under way is the thing their eye should find. */
function Process({ steps, node }: { steps: NodeStep[]; node: TaskNode }): JSX.Element | null {
  const [open, setOpen] = useState(node.status === 'running')
  if (!steps.length) return null
  const tools = steps.filter((s) => s.kind === 'tool').length
  return (
    <div className={'tkproc' + (open ? ' open' : '')}>
      <button className="tkprock" onClick={() => setOpen(!open)}>
        {t('gui.tasks.rec_process')}
        <span className="tkprocn">{t('gui.tasks.record_tools', { n: tools })}</span>
        <span className="tkchev">{open ? '⌃' : '⌄'}</span>
      </button>
      {open ? <div className="tkprocb">{steps.map((st, i) => <Step step={st} key={i} />)}</div> : null}
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
      <button aria-label={t('gui.send')} disabled={!text.trim() || busy} onClick={send}>{t('gui.send')}</button>
    </div>
  )
}

function ContextTab({ row, node, rec, roster }: {
  row: TaskRow; node: TaskNode; rec: RecordLoad; roster: SubagentRow[]
}): JSX.Element {
  if (node.status === 'pending' || node.status === 'skipped') {
    return <p className="tkempty">{nodeWhyText(node, row) || t('gui.tasks.ctx_none')}</p>
  }
  if (rec.loading) return <p className="tkempty">{t('gui.tasks.loading')}</p>
  if (rec.failed || !rec.record) return <p className="tkempty">{t('gui.tasks.rec_unread')}</p>
  const record = rec.record
  return (
    <div className="tkctx">
      {record.dispatch ? <Dispatch text={record.dispatch} /> : null}
      <Process steps={record.steps} node={node} />
      {record.answer ? <div className="tkans">{record.answer}</div> : null}
      {!record.answer && node.status === 'failed'
        ? <div className="tkerrb">{node.error || t('gui.tasks.why_failed_fallback')}</div>
        : null}
      {!record.answer && node.status === 'interrupted'
        ? <div className="tkerrb">{t('gui.tasks.ctx_interrupted')}</div>
        : null}
      {!record.answer && node.status === 'running'
        ? <div className="tklive"><span className="dot run" />{t('gui.tasks.running')}</div>
        : null}
      {record.outputTruncated ? <div className="tktrunc">{t('gui.tasks.ctx_truncated')}</div> : null}
      <ChatDock node={node} roster={roster} />
    </div>
  )
}

const PLACEHOLDER = /(\{\{[^}]*\}\})/g
const IS_PLACEHOLDER = /^\{\{[^}]*\}\}$/

function Template({ text, label }: { text: string; label: string }): JSX.Element {
  return (
    <div className="tkprompt">
      <div className="tkpk">{label}</div>
      <pre className="tkpv">
        {text.split(PLACEHOLDER).map((part, i) => (
          IS_PLACEHOLDER.test(part)
            ? <mark className="tkph" key={i}>{part}</mark>
            : <span key={i}>{part}</span>
        ))}
      </pre>
    </div>
  )
}

const listOrDash = (v?: string[]): string => (v && v.length ? v.join(', ') : '—')

function inputText(v: unknown): string {
  if (typeof v === 'string') return v
  if (v && typeof v === 'object' && !Array.isArray(v)) {
    const o = v as Record<string, unknown>
    if (typeof o.file === 'string') return `{file: ${o.file}}`
    if (typeof o.node === 'string') return `{node: ${o.node}}`
  }
  return JSON.stringify(v)
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
  const known = !node.agent || roster.some((r) => r.name === node.agent && r.enabled)
  const dispatched = node.status !== 'pending' && node.status !== 'skipped'
  const rendered = dispatched ? rec.record?.dispatch ?? null : null
  const text = rendered ?? node.prompt_template ?? null
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
        <div className="tkfv">{node.skills && node.skills.length ? listOrDash(node.skills) : t('gui.tasks.skills_none')}</div>
      </div>
      <div className="tkfield">
        <div className="tkfk">{t('gui.tasks.mcps')}</div>
        <div className="tkfv">{node.mcps && node.mcps.length ? listOrDash(node.mcps) : t('gui.tasks.mcps_none')}</div>
      </div>
      {inputs.length
        ? (
          <div className="tkfield">
            <div className="tkfk">{t('gui.tasks.inputs')}</div>
            <dl className="tkkv">
              {inputs.map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{inputText(v)}</dd></div>)}
            </dl>
          </div>
        )
        : null}
      {text
        ? <Template text={text} label={rendered ? t('gui.tasks.instruction') : t('gui.tasks.instruction_template')} />
        : (
          <div className="tkfield">
            <div className="tkfk">{t('gui.tasks.instruction')}</div>
            <div className="tkfv">{dispatched ? t('gui.tasks.instruction_pending') : t('gui.tasks.left_blank')}</div>
          </div>
        )}
      {dispatched && !rendered && node.prompt_template ? <p className="tknote">{t('gui.tasks.instruction_note')}</p> : null}
      <div className="tkspecid">
        <span>{row.kind === 'spawn' ? t('gui.tasks.call_label') : t('gui.tasks.run_label')}</span>
        <b>{row.id}</b>
        <button className="tkspeccopy" aria-label={t('gui.tasks.copy')} onClick={() => copy(row.id, t('gui.tasks.copied'))}>
          {t('gui.tasks.copy')}
        </button>
      </div>
    </div>
  )
}

function NodePanel({ row, node, onClose, roster }: {
  row: TaskRow; node: TaskNode; onClose: () => void; roster: SubagentRow[]
}): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  const rec = useNodeRecord(row, node)
  const tab = s.tabPinned ? s.tab : (node.status === 'pending' || node.status === 'skipped' ? 'order' : 'context')
  return (
    <div className="tkcard">
      <div className="tkch">
        <button className="tkback" onClick={onClose} aria-label={t('gui.tasks.back')}>&lsaquo;</button>
        <div className="tktt">
          <b>{node.node_summary || node.node_id}</b>
          <span className="tksub">{nodeSubtitle(node)}</span>
        </div>
        {node.status === 'failed' || node.status === 'exception' ? <ErrorTag /> : null}
      </div>
      <div className="tktabs" role="tablist">
        {(['context', 'order'] as const).map((k) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => store.pickTab(k)}>
            {t(k === 'context' ? 'gui.tasks.tab_context' : 'gui.tasks.tab_order')}
          </button>
        ))}
      </div>
      {tab === 'context'
        ? <ContextTab row={row} node={node} rec={rec} roster={roster} />
        : <OrderTab row={row} node={node} roster={roster} rec={rec} />}
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
      title={t('gui.tasks.stop')}
      onClick={() => { setBusy(true); void store.stop(row).finally(() => setBusy(false)) }}
    >
      {t('gui.tasks.stop')}
    </button>
  )
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
  return (
    <div className={'tkwhy' + (cold ? ' tkcold' : '')}>
      <p>{text}</p>
      {bad
        ? (
          <button className="tkwhyat" onClick={() => onPick(bad.node_id)}>
            {t('gui.tasks.why_at', { name: bad.node_summary || bad.node_id })}
          </button>
        )
        : null}
    </div>
  )
}

function StatusBar({ row, now }: { row: TaskRow; now: number }): JSX.Element {
  const dur = taskDuration(row, now)
  const line = stepLine(row)
  return (
    <div className="tkbar" data-st={row.status}>
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
  const src = store.source()
  const rec = src ? await src.node(row, node).catch(() => null) : null
  const change: WsChange = {
    key: `task:${row.kind}:${row.id}:${node.node_id}:${file.path}`,
    dir: '', name: file.path.split('/').pop() || file.path, kind: 'edit',
    add: file.add, del: file.del, hunks: rec ? hunksForFile(rec.steps, file.path) : [], turn: 0, open: false,
  }
  desk.openDeskDiff(change)
}

function Chips({ row }: { row: TaskRow }): JSX.Element | null {
  const files: Array<{ node: TaskNode; file: TaskFile }> = []
  row.nodes.forEach((n) => n.files.forEach((f) => files.push({ node: n, file: f })))
  if (!files.length) return null
  return (
    <div className="tkchips">
      {files.map(({ node, file }) => {
        const name = file.path.split('/').pop() || file.path
        const key = node.node_id + ':' + file.path
        if (file.op === 'write') {
          return (
            <button className="wchip" key={key} title={file.path} onClick={() => workspace.openPath(file.path)}>
              {name}{file.size != null ? <span className="tkkd">{humanSize(file.size)}</span> : null}
            </button>
          )
        }
        return (
          <button className="wchip" key={key} title={file.path} onClick={() => void openNodeDiff(row, node, file)}>
            {name} <i className="add">+{file.add}</i> <i className="del">&minus;{file.del}</i>
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
    <div className={'tkview' + (full ? ' full' : '')}>
      <div className="tkhead">
        <span className={'dot ' + dotOf(row.status)} />
        <b>{row.task_summary || row.id}</b>
        {row.status === 'failed' ? <ErrorTag /> : null}
      </div>
      <StatusBar row={row} now={now} />
      <WhyBanner row={row} onPick={pick} />
      <Chips row={row} />
      {full
        ? (
          <div className="tkwork">
            <Fork row={row} paneId={paneId} />
            {node
              ? <NodePanel row={row} node={node} onClose={() => pick(null)} roster={roster} />
              : <div className="tkpick">{t('gui.tasks.pick_node')}</div>}
          </div>
        )
        : node
          ? <NodePanel row={row} node={node} onClose={() => pick(null)} roster={roster} />
          : <Fork row={row} paneId={paneId} />}
    </div>
  )
}
