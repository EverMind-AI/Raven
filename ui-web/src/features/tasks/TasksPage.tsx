/* The workspace panel's tasks view: every unit of delegated work this
 * conversation started, whichever way it was started.
 *
 * Replaces the subagents view, which answered a narrower question -- it listed
 * spawned runs, so a graph and a playbook, which are the two the reader is most
 * likely to be waiting on, appeared nowhere. The list is the floating panel and
 * opening a row docks the wide one; that split is the panel's, so this renders
 * one or the other and lets the chrome size itself.
 *
 * The steps are drawn once, as a graph on a board the reader pans and zooms.
 * The design asks for the same dotted board in the docked pane and full screen,
 * with full screen enlarging it rather than redrawing it -- so there is no
 * second, flatter rendering of the same steps for the narrow pane to fall back
 * to, and nothing that can disagree with the graph about what ran.
 */

import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import { Board } from '../dag/Board'
import { DagGraph } from '../dag/DagGraph'
import { layout } from '../dag/graph'
import { openDeskDiff, openDeskTask } from '../desk/store'
import * as workspace from '../workspace/store'
import * as store from './store'
import { toDagNodes } from './types'

import type { Dims } from '../dag/graph'
import type { WsChange } from '../workspace/types'
import type { NodeRecord, RecordStep, TaskArtifact, TaskDiff, TaskNode, TaskRow } from './types'
import type { JSX } from 'react'
import './styles.css'

const SOURCE_KEY: Record<string, string> = {
  spawn: 'gui.tasks.src_spawn',
  dag: 'gui.tasks.src_dag',
  playbook: 'gui.tasks.src_playbook',
}

/* What the chip says. A playbook run names its playbook, because "playbook" on
   six rows of a session that ran six of them is not an answer. */
export const sourceTag = (r: TaskRow): string => r.sourceName || t(SOURCE_KEY[r.source] || 'gui.tasks.src_spawn')

/* Not translated, and not a word: the same token the runtime uses for the same
   condition, so a reader who sees it here and in a log is looking at one thing.
   The colour is what carries it; the text is the label on the colour. */
const ErrorTag = (): JSX.Element => <span className="tkerr">error</span>

/* The second line, and the whole of it: how long, and how many products. The
   state is the dot's to say -- a row that repeats it in words spends its one
   line saying twice what the reader already read once. */
export function taskLine(r: TaskRow): string {
  const parts: string[] = []
  if (r.duration) parts.push(r.duration)
  const n = r.artifacts?.length ?? 0
  if (n) parts.push(t(n === 1 ? 'gui.tasks.artifacts_1' : 'gui.tasks.artifacts_n', { n }))
  return parts.join(' · ')
}

function Row({ r, open }: { r: TaskRow; open: (r: TaskRow) => void }): JSX.Element {
  const s = store.get()
  return (
    <div
      className={'sarow task' + (s.hover === r.id ? ' hl' : '')}
      role="button"
      tabIndex={0}
      onClick={() => open(r)}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(r) } }}
      onMouseEnter={() => store.hover(r.id)}
      onMouseLeave={() => store.hover(null)}
    >
      <span className={'dot ' + store.dot(r.state)} />
      <div className="bd">
        <div className="tline">
          <span className="nm" title={r.name}>{r.name}</span>
          {r.state === 'fail' ? <ErrorTag /> : null}
          <span className="tsrc">{sourceTag(r)}</span>
        </div>
        <div className="st">{taskLine(r)}</div>
      </div>
    </div>
  )
}

function Group({ label, list, open }: { label: string; list: TaskRow[]; open: (r: TaskRow) => void }): JSX.Element | null {
  if (!list.length) return null
  return (
    <>
      <div className="wsgrp">{label} {list.length}</div>
      {list.map((r) => <Row key={r.id} r={r} open={open} />)}
    </>
  )
}

function List(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  if (!s.rows.length) return <div className="wsempty">{t('gui.tasks.none')}</div>
  return (
    <div className="salist tasks">
      <Group label={t('gui.tasks.running')} list={store.running(s.rows)} open={openDeskTask} />
      <Group label={t('gui.tasks.settled')} list={store.settled(s.rows)} open={openDeskTask} />
    </div>
  )
}

/* ── the work order ────────────────────────────────────────────────────
   What was handed to the sub-agent, as it was written. Read-only, and the
   only tab a pending node can show: there is no flow to read until it runs. */

/* The four placeholder forms a prompt template may carry, and the only four
   the runtime expands. Highlighted rather than merely shown, because the whole
   question a reader brings to a template is which parts of it are filled in
   from somewhere else and where that somewhere is. */
const PLACEHOLDER = /(\{\{[^}]*\}\})/g
/* A separate, non-global copy for the test: a /g regex carries `lastIndex`
   between calls, so re-using the splitter to ask "is this piece a placeholder"
   answers about wherever the previous call stopped. */
const IS_PLACEHOLDER = /^\{\{[^}]*\}\}$/

function Template({ text }: { text: string }): JSX.Element {
  return (
    <div className="tkprompt">
      <div className="tkpk">promptTemplate</div>
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

/* A row that is absent and one that is empty read the same here, and that is
   deliberate: a spawn has no skills parameter at all, so the honest answer to
   "which skills" is the same dash either way. */
const listOrDash = (v?: string[]): string => (v && v.length ? v.join(', ') : '—')

function OrderTab({ node }: { node: TaskNode }): JSX.Element {
  return (
    <>
      <dl className="tkkv">
        {/* The id is a field here rather than the heading: it is what a
            dependency and a placeholder name, which makes it a value the
            reader looks up, not the title they arrived by. */}
        <dt>node_id</dt><dd>{node.id}</dd>
        <dt>subagent</dt><dd>{node.agent || '—'}</dd>
        <dt>skills</dt><dd>{listOrDash(node.skills)}</dd>
        <dt>mcps</dt><dd>{listOrDash(node.mcps)}</dd>
      </dl>
      {node.prompt ? <Template text={node.prompt} /> : null}
    </>
  )
}

/* ── the execution flow ────────────────────────────────────────────────
   What actually happened, appended as it happens and whole once it ends.
   Same drawing as the sub-agent context view, minus its input box: the record
   is read, not continued -- the main agent is what dispatches, and a second
   place to type would be a conversation nobody is listening to. */

/* The runtime wraps an upstream node's output in an UNTRUSTED marker before it
   reaches the next agent's prompt, and the fence stays visible here: this tab
   is the one place a reader sees one agent's words rendered for another, and
   the boundary is the whole reason it is safe to show. Plain text and light
   markdown only -- no links resolved, no HTML parsed. */
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

function Step({ step }: { step: RecordStep }): JSX.Element {
  const [open, setOpen] = useState(false)
  if (step.kind === 'think') {
    return (
      <div className="tkthink">
        <button className="tkthh" onClick={() => setOpen(!open)}>{t('gui.tasks.rec_thought')}</button>
        {open ? <blockquote>{step.text}</blockquote> : null}
      </div>
    )
  }
  return (
    <div className="tktool">
      <button className="tkth" onClick={() => setOpen(!open)}>
        <b className="mono">{step.tool}</b>
        <span className="tkta">{step.arg}</span>
        <span className="tktr">{step.result}</span>
        <span className="tkchev">&rsaquo;</span>
      </button>
      {open ? <pre className="tktd">{step.arg + '\n\n' + step.result}</pre> : null}
    </div>
  )
}

/* The process, folded by default. A run of any length is mostly steps a reader
   does not need, and the two things they came for -- what was asked and what
   came back -- sit above and below it. */
function Process({ rec, took }: { rec: NodeRecord; took?: string }): JSX.Element | null {
  const [open, setOpen] = useState(false)
  if (rec.unrecorded) return <p className="tkempty">{t('gui.tasks.rec_unrecorded')}</p>
  if (!rec.steps.length) return null
  const tools = rec.steps.filter((x) => x.kind === 'tool').length
  return (
    <div className={'tkproc' + (open ? ' open' : '')}>
      <button className="tkprock" onClick={() => setOpen(!open)}>
        {t('gui.tasks.rec_process')}
        <span className="tkprocn">
          {t('gui.tasks.record_tools', { n: tools })}{took ? ' · ' + took : ''}
        </span>
        <span className="tkchev">{open ? '⌃' : '⌄'}</span>
      </button>
      {open ? <div className="tkprocb">{rec.steps.map((st, i) => <Step step={st} key={i} />)}</div> : null}
    </div>
  )
}

function FlowTab({ node }: { node: TaskNode }): JSX.Element {
  const rec = node.record
  /* A step nobody has started has no flow to show, and saying so is a different
     answer from an empty one -- and a different answer again from a step that
     ran and whose record this panel has not read. Decided by the status rather
     than by the record's absence: a run with no record in hand is not a run
     that never started, and the full-screen fork makes it one click to ask. */
  if (!rec) {
    return (
      <p className="tkempty">
        {t(node.status === 'pending' ? 'gui.tasks.rec_pending' : 'gui.tasks.rec_unread')}
      </p>
    )
  }
  return (
    <>
      <Dispatch text={rec.dispatch} />
      <Process rec={rec} took={node.duration} />
      {rec.answer ? <div className="tkans">{rec.answer}</div> : null}
      {!rec.answer && rec.stopped ? <div className="tkstop">{rec.stopped}</div> : null}
      {!rec.answer && !rec.stopped && !rec.unrecorded
        ? (
          <div className="tklive">
            <span className="dot run" />
            {t('gui.tasks.state_run')}
          </div>
        )
        : null}
    </>
  )
}

/* ── one node, described ───────────────────────────────────────────────
   Fills the node area rather than sitting beside it in the docked pane: it is
   not wide enough for both, and closing the card brings the column back. */
function NodeCard({ node, onClose }: { node: TaskNode; onClose: () => void }): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  /* Flow first, because what happened is the usual question -- except on a step
     that has not run, where the only thing to read is what it was asked. A
     reader who has chosen a tab keeps it as they move between nodes: they are
     comparing the same thing across steps, and re-deciding for them each time
     would undo the choice they just made. */
  const tab = s.tabPinned ? s.tab : (node.status === 'pending' ? 'order' : 'flow')
  return (
    <div className="tkcard">
      <div className="tkch">
        <h4>{node.title || node.id}</h4>
        {node.status === 'failed' || node.status === 'exception' ? <ErrorTag /> : null}
        <button className="tkx" onClick={onClose} aria-label={t('gui.close')}>&times;</button>
      </div>
      <div className="tktabs" role="tablist">
        {(['flow', 'order'] as const).map((k) => (
          <button key={k} role="tab" aria-selected={tab === k} onClick={() => store.pickTab(k)}>
            {t(k === 'flow' ? 'gui.tasks.tab_flow' : 'gui.tasks.tab_order')}
          </button>
        ))}
      </div>
      {tab === 'flow' ? <FlowTab node={node} /> : <OrderTab node={node} />}
    </div>
  )
}

/* The steps, as the one picture of them there is.
   Not a list beside a graph: the design draws the same dotted board at both
   sizes and full screen only enlarges it, so a second drawing of the same
   steps would be a second thing to keep true. The picture is the dag island's
   graph, which already layers by longest upstream path, fits labels, marks
   status and draws the edges; the viewport around it is the board the playbook
   page reads a stored graph in. It runs downward rather than sideways, which is
   the axis a pane docked beside a conversation has room on -- the composer's
   sheet, which is a wide strip, keeps the sideways one. */
/* Wider and shorter than the sheet's box, and spaced for a graph that runs
   downward: the width is the docked pane's to spare, the height is what a
   reader scrolling a column of steps pays for each one, and the gap between
   layers is what a curve needs to read as a curve rather than as a kink. */
const COLUMN: Dims = { W: 216, H: 46, GAP_X: 240, GAP_Y: 106, PAD: 16 }

function Fork({ task }: { task: TaskRow }): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  const nodes = toDagNodes(task)
  const size = layout(nodes, COLUMN, 'down')
  return (
    <Board
      width={size.width}
      height={size.height}
      fitKey={task.id}
      label={t('gui.tasks.canvas')}
      owns=".nd"
      arrowsTaken={!!s.node}
    >
      <DagGraph
        dims={COLUMN}
        nodes={nodes}
        now={Date.now()}
        surface="sheet"
        flow="down"
        selectedId={s.node}
        onPick={(n) => store.pickNode(n.id)}
      />
    </Board>
  )
}

/* What the task left behind, and the door to each of them: the design sends a
   product chip to the preview and a changed file's chip to the diff, so a
   reader who has just seen that a task produced something does not have to go
   and find it in another tab. Both panes are the desk's own, opened the way
   every other caller opens them.

   A changed file resolves through the session's change list, which is where the
   hunks are. When it is not there -- a task read back into a desk whose change
   list has since been replayed, say -- the chip opens the file instead: the
   file is what the chip names, and a control that does nothing is worse than
   one that shows the thing without its history. */
const changeOf = (d: TaskDiff): WsChange | null => {
  const list = workspace.changes()
  return list.find((c) => c.key === (d.key || d.file)) || list.find((c) => c.name === d.file) || null
}

const openTaskDiff = (d: TaskDiff): void => {
  const change = changeOf(d)
  if (change) openDeskDiff(change)
  else workspace.openPath(d.key || d.file)
}

/* Through the workspace's opener rather than straight at the desk, which is the
   door the shelf's row and the transcript's delivery card already use: a source
   that cannot read files answers with its own note, instead of a viewer opening
   on nothing it can show. */
const openTaskArtifact = (a: TaskArtifact): void => workspace.openDelivery(a.path || a.name)

function Chips({ task }: { task: TaskRow }): JSX.Element | null {
  const arts = task.artifacts || []
  const diffs = task.diffs || []
  if (!arts.length && !diffs.length) return null
  return (
    <div className="tkchips">
      {arts.map((a) => (
        <button className="wchip" key={a.name} title={a.path || a.name} onClick={() => openTaskArtifact(a)}>
          {a.name}
        </button>
      ))}
      {diffs.map((d) => (
        <button className="wchip" key={d.file} title={d.key || d.file} onClick={() => openTaskDiff(d)}>
          {d.file} <i className="add">+{d.add}</i> <i className="del">&minus;{d.del}</i>
        </button>
      ))}
    </div>
  )
}

/* How long, and whether the whole graph was approved first. The state is the
   dot's to say. The approval is absent for a spawn rather than false: the tool
   has no such parameter, and a printed `false` would answer a question nobody
   asked of it. */
function headLine(task: TaskRow): string {
  const parts: string[] = []
  if (task.duration) parts.push(task.duration)
  if (task.confirm !== undefined) parts.push(t('gui.tasks.confirm', { v: String(task.confirm) }))
  return parts.join(' · ')
}

export function TaskPane({ task, full = false }: { task: TaskRow; full?: boolean }): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  /* Per pane, not per store: two open tasks are two panes, and a single
     selected-node field would move both when the reader picked in one. Nothing
     is selected until the reader picks, so the pane opens on the steps. */
  const picked = s.node && task.nodes.some((n) => n.id === s.node) ? s.node : null
  const node = task.nodes.find((n) => n.id === picked) || null
  return (
    <div className={'tkview' + (full ? ' full' : '')}>
      {/* No source tag and no description: the backend carries one title field
          per task and no separate description, so a head that showed either
          would be showing something invented here. */}
      <div className="tkhead">
        <span className={'dot ' + store.dot(task.state)} />
        <b>{task.name}</b>
        {task.state === 'fail' ? <ErrorTag /> : null}
      </div>
      <div className="tkline">{headLine(task)}</div>
      <Chips task={task} />
      {task.failure ? <div className="tkfail">{task.failure}</div> : null}
      {/* One board, two rooms. Full screen keeps the graph and the node side by
          side: picking a step there must not hide the fork it was picked out
          of. The docked pane is not wide enough for both, so the card fills the
          board's place and closing it brings the board back. */}
      {full
        ? (
          <div className="tkwork">
            <Fork task={task} />
            {node
              ? <NodeCard node={node} onClose={() => store.pickNode(null)} />
              : <div className="tkpick">{t('gui.tasks.pick_node')}</div>}
          </div>
        )
        : node
          ? <NodeCard node={node} onClose={() => store.pickNode(null)} />
          : <Fork task={task} />}
    </div>
  )
}

/* ── what is running while you type ────────────────────────────────────
   The same rows as the list's first group, on a strip above the composer, so a
   reader who never opens the panel still knows what is under way and can reach
   it in one click. Above the card and outside it: this is not part of their
   draft.

   A chip says which step of how many rather than how long, which is the other
   way round from the list. Both are true; the chip is too narrow for one and a
   fraction is the fact a reader watching something run is waiting on.

   One line, always. Six running tasks wrapped into three rows of chips took a
   third of the conversation with them, and the strip is a status line rather
   than a list -- so it scrolls sideways instead, and fades at whichever end has
   more beyond it.

   Nothing when nothing is running -- an empty strip is a gap above the box the
   reader types in, and the panel is where "no tasks" gets said in words. */

/* A pointer that moved less than this between down and up was a click on the
   chip under it, not a drag of the rail. */
const DRAG_SLOP = 4

/* Everything the rail needs to be reachable: which ends have more past them,
   and the two gestures that get there.

   The bar is hidden -- it is half a chip of furniture under a 30px strip -- so
   without these a plain mouse has no way to move the rail at all: no horizontal
   wheel, no bar to drag, and a trackpad's sideways flick is the only gesture
   that works. So the vertical wheel drives it while the pointer is over it, and
   the rail can be dragged by the space between its chips.

   Touch is left to the browser (`touch-action: pan-x`): it already pans this
   with momentum, and a second hand on `scrollLeft` would move it twice as far
   as the finger did. */
interface Rail {
  ref: React.RefObject<HTMLDivElement | null>
  ends: { l: boolean; r: boolean }
  grip: {
    onPointerDown: (e: React.PointerEvent<HTMLDivElement>) => void
    onPointerMove: (e: React.PointerEvent<HTMLDivElement>) => void
    onPointerUp: (e: React.PointerEvent<HTMLDivElement>) => void
  }
  /* Whether the press that just ended moved the rail, asked once and forgotten.
     The chip asks before it opens: a drag that happens to end on a chip is a
     drag, not a click on it. Asked by the chip rather than swallowed by the rail
     in the capture phase, because a chip that decides for itself works the same
     wherever the click comes from. */
  dragged: () => boolean
}

function useRail(count: number): Rail {
  const rail = useRef<HTMLDivElement>(null)
  const [ends, setEnds] = useState({ l: false, r: false })
  const drag = useRef<{ id: number; x: number; from: number; moved: boolean } | null>(null)
  /* Whether the press that just ended was a drag. Kept apart from `drag` because
     the click it has to swallow arrives after the pointerup that clears it --
     and cleared on the next press, since a drag that ends on the rail's own
     background is followed by no click at all. */
  const dragged = useRef(false)

  /* Which ends have something past them, so only those fade. A fade on an end
     that is already the end of the chips dims a chip for no reason, and the
     two-chip case -- the usual one -- is exactly that. */
  useEffect(() => {
    const el = rail.current
    if (!el) return
    const read = (): void => {
      const l = el.scrollLeft > 1
      const r = el.scrollLeft + el.clientWidth < el.scrollWidth - 1
      setEnds((prev) => (prev.l === l && prev.r === r ? prev : { l, r }))
    }
    read()
    el.addEventListener('scroll', read, { passive: true })
    window.addEventListener('resize', read)
    /* The dock is narrowed by the workspace panel opening as well as by the
       window, and neither of those is a scroll or a change of rows. */
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(read)
    if (ro) ro.observe(el)
    return () => {
      el.removeEventListener('scroll', read)
      window.removeEventListener('resize', read)
      if (ro) ro.disconnect()
    }
  }, [count])

  /* Attached by hand, non-passive, because React registers `wheel` passively
     and `preventDefault` is a no-op in a passive listener. */
  useEffect(() => {
    const el = rail.current
    if (!el) return
    const onWheel = (e: WheelEvent): void => {
      /* A trackpad's sideways flick and a page zoom are already what they mean.
         Only the vertical wheel is being borrowed. */
      if (e.ctrlKey || e.metaKey || Math.abs(e.deltaX) > Math.abs(e.deltaY) || !e.deltaY) return
      const max = el.scrollWidth - el.clientWidth
      if (max <= 0) return
      /* At either end the flick belongs to the page again. Swallowing it there
         is what makes a strip feel like a trap: the conversation stops scrolling
         because the pointer happens to be over three chips.
         Within a pixel of the end IS the end: these three are fractional on a
         scaled display, and `>= max` is a comparison that never comes true --
         the strip kept eating the flick while sitting against its own edge. */
      if (e.deltaY < 0 ? el.scrollLeft <= 1 : el.scrollLeft >= max - 1) return
      e.preventDefault()
      el.scrollLeft = Math.max(0, Math.min(max, el.scrollLeft + e.deltaY))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [count])

  const onPointerDown = (e: React.PointerEvent<HTMLDivElement>): void => {
    const el = rail.current
    dragged.current = false
    if (!el || e.pointerType === 'touch' || (e.pointerType === 'mouse' && e.button !== 0)) return
    drag.current = { id: e.pointerId, x: e.clientX, from: el.scrollLeft, moved: false }
  }

  const onPointerMove = (e: React.PointerEvent<HTMLDivElement>): void => {
    const el = rail.current
    const d = drag.current
    if (!el || !d || d.id !== e.pointerId) return
    const dx = e.clientX - d.x
    if (!d.moved) {
      if (Math.abs(dx) < DRAG_SLOP) return
      d.moved = true
      /* Captured once it IS a drag, not on the press: capturing at the press
         takes the pointerup away from the chip under it, and every click on a
         chip would go nowhere.
         Guarded because this throws on a pointer the browser no longer has --
         a release the page missed, a synthetic id -- and an exception thrown
         here abandons the gesture with the rail stuck under the reader's hand.
         Without the capture the drag still works; it just stops at the edge of
         the strip instead of following the pointer past it. */
      try {
        e.currentTarget.setPointerCapture(e.pointerId)
      } catch {
        /* see above */
      }
    }
    el.scrollLeft = d.from - dx
  }

  const onPointerUp = (e: React.PointerEvent<HTMLDivElement>): void => {
    const d = drag.current
    if (!d || d.id !== e.pointerId) return
    dragged.current = d.moved
    drag.current = null
  }

  return {
    ref: rail,
    ends,
    grip: { onPointerDown, onPointerMove, onPointerUp },
    dragged: () => {
      const was = dragged.current
      dragged.current = false
      return was
    },
  }
}

export function TaskRuns(): JSX.Element | null {
  const s = useSyncExternalStore(store.subscribe, store.get)
  /* The strip is on screen from the first paint, and is usually the first
     thing to want the rows: the panel behind it may never be opened. */
  useEffect(() => { if (!s.loaded) void store.refresh() }, [s.loaded])
  const live = store.running(s.rows)
  const rail = useRail(live.length)
  if (!live.length) return null
  return (
    <div className="runs" role="group" aria-label={t('gui.tasks.running')}
      data-l={rail.ends.l || undefined} data-r={rail.ends.r || undefined}>
      <div className="runrail" ref={rail.ref} {...rail.grip}>
        {live.map((r) => (
          <button
            key={r.id}
            className={'trun' + (s.hover === r.id ? ' hl' : '')}
            title={r.name}
            onClick={() => { if (!rail.dragged()) openDeskTask(r) }}
            /* Pointed at from either side: the strip and the list light each
               other up, and neither owns the pointer. */
            onMouseEnter={() => store.hover(r.id)}
            onMouseLeave={() => store.hover(null)}
          >
            <span className={'dot ' + store.dot(r.state)} />
            <span className="nm">{r.name}</span>
            {r.step ? <span className="st">{t('gui.tasks.step', { i: r.step[0], n: r.step[1] })}</span> : null}
          </button>
        ))}
      </div>
    </div>
  )
}

export function TasksApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.get)
  /* The language the page resolved, so a pick repaints this island: every word
     below is a t(key) read at render time (state/lang/store.ts). */
  useSyncExternalStore(lang.subscribe, lang.get)
  useEffect(() => { if (!s.loaded) void store.refresh() }, [s.loaded])
  return <List />
}
