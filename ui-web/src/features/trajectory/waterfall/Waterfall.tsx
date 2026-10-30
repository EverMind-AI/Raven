/* The page's rows: one per agent, one per turn, and a turn's spans beneath it
 * once it is open.
 *
 * Every row draws its bar against the same axis, so the ruler, an agent's
 * lifetime and a span's extent are one coordinate system. The vertical order
 * is the model's (agent, then turn, then span); this file only decides which
 * of those rows are on screen and how each one is painted.
 *
 * Rows are windowed by scroll offset, the way the old list was: a 500-turn
 * session must mount a screenful, not a session.
 */

import { ArrowDown01Icon, ArrowRight01Icon, BotIcon } from '@hugeicons/core-free-icons'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'

import { AgentMark, agentMarkPath } from '../../../components/AgentMark'
import { Icon } from '../../../components/Icon'
import { t } from '../../../i18n/t'
import { formatDuration } from '../../../lib/duration'
import * as lang from '../../../state/lang'
import { showBar } from '../../../state/scrollbars'
import * as details from '../detailStore'
import { kindLabel } from '../palette'
import { enter as enterScope, scopeTitle } from '../scopes'
import * as store from '../store'
import { AxisIndex, TRAIL_PAD, barOf, clampViewport, presetWindow, zoomViewport } from './axis'
import { clockOf, lengthOf, spanOf, windowText } from './clock'
import { WaterfallIndex } from './incremental'
import { busyRuns, entryEnd, entryStart, isLive, turnKey } from './model'
import { OverviewLanes } from './OverviewLanes'
import { turnBreakdown, turnSegments } from './segments'
import { agentKind, summarize } from './summary'
import { clockTicks, stepFor, tickLabel } from './ticks'

import type { TrajectoryEntry } from '../types'
import type { TimeAxis, Viewport } from './axis'
import type { Waterfall, WaterfallAgent, WaterfallRow, WaterfallTurn } from './model'
import type { Figures } from './summary'
import type { ClockTick } from './ticks'
import type { JSX, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react'

export const ROW_HEIGHT = 30

/** A stretch of the axis, as fractions of the open viewport: what the reader
 *  picked on the ruler, and what the rows are filtered to. */
export interface Pick {
  from: number
  to: number
}

/** The agent group's trunk, under its fold control: 12px of row padding plus
 *  the 18px chevron's centre. The turn's branch sits under the turn's own
 *  chevron, 26px in, so each level's children hang from the control that
 *  folds them -- and neither line crosses a chevron or an icon. */
export const GUIDE_X = 21
export const TURN_GUIDE_X = 35
const OVERSCAN = 8
const DEFAULT_ROWS = 20

/** Which side of the window a folded group of agents lies on. */
export type FoldSide = 'earlier' | 'later'

export interface DisplayRow {
  kind: 'agent' | 'turn' | 'span' | 'fold'
  key: string
  agent: number
  turn: string | null
  row: WaterfallRow | null
  turnRow: WaterfallTurn | null
  /** A fold row's side and the agents it holds, by index. */
  fold?: { side: FoldSide; agents: readonly number[] }
}

export function displayRows(
  waterfall: Waterfall, collapsedAgents: ReadonlySet<string>, openTurns: ReadonlySet<string>,
  outside: ReadonlyMap<number, FoldSide> = new Map(), openFolds: ReadonlySet<FoldSide> = new Set(),
): DisplayRow[] {
  const rows: DisplayRow[] = []
  const agentRows = (at: number): void => {
    const agent = waterfall.agents[at]!
    rows.push({ kind: 'agent', key: `agent:${agent.key}`, agent: at, turn: null, row: null, turnRow: null })
    if (collapsedAgents.has(agent.key)) return
    for (const turn of agent.turns) {
      rows.push({ kind: 'turn', key: `turn:${turnKey(at, turn.id)}`, agent: at, turn: turn.id, row: null, turnRow: turn })
      if (!openTurns.has(turnKey(at, turn.id))) continue
      turn.rows.forEach((row, index) => rows.push({ kind: 'span', key: `span:${row.entry.entry_id}:${index}`, agent: at, turn: turn.id, row, turnRow: turn }))
    }
    for (const row of agent.loose) {
      rows.push({ kind: 'span', key: `loose:${row.entry.entry_id}`, agent: at, turn: null, row, turnRow: null })
    }
  }
  /* The earlier group follows the main agent, the later one closes the list;
     an open group lists its agents beneath its own row. */
  const group = (side: FoldSide): void => {
    const agents = [...outside].filter(([, where]) => where === side).map(([at]) => at).sort((a, b) => a - b)
    if (!agents.length) return
    rows.push({ kind: 'fold', key: `fold:${side}`, agent: -1, turn: null, row: null, turnRow: null, fold: { side, agents } })
    if (openFolds.has(side)) agents.forEach(agentRows)
  }
  const inside = waterfall.agents.map((_, at) => at).filter((at) => !outside.has(at))
  const main = inside.filter((at) => waterfall.agents[at]!.scope === null)
  main.forEach(agentRows)
  group('earlier')
  inside.filter((at) => waterfall.agents[at]!.scope !== null).forEach(agentRows)
  group('later')
  return rows
}

function windowOf(scrollTop: number, height: number, count: number): { first: number; last: number } {
  const visible = height > 0 ? Math.ceil(height / ROW_HEIGHT) : DEFAULT_ROWS
  const first = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN)
  const last = Math.min(count, Math.floor(scrollTop / ROW_HEIGHT) + visible + OVERSCAN)
  return { first, last }
}

const percent = (fraction: number): string => `${(fraction * 100).toFixed(4)}%`

/** A drawn row's id, for the tree's active descendant. */
const rowId = (at: number): string => `trajectory-wf-row-${at}`

/** The runs that reach into a window. Runs are merged, so their starts and
 *  their ends both rise: two binary searches find the stretch, and a scroll
 *  frame never walks an agent's whole history. */
function within<T extends { start: number; end: number }>(runs: readonly T[], from: number, to: number): readonly T[] {
  let low = 0
  let high = runs.length
  while (low < high) {
    const middle = (low + high) >>> 1
    if (runs[middle]!.end < from) low = middle + 1
    else high = middle
  }
  let stop = low
  let top = runs.length
  while (stop < top) {
    const middle = (stop + top) >>> 1
    if (runs[middle]!.start <= to) stop = middle + 1
    else top = middle
  }
  return runs.slice(low, stop)
}

/** Who the agents were, as the header's second line: zero counts are left out. */
function agentSplit(agents: Figures['agents']): string | null {
  const parts = [
    agents.main > 0 ? t('gui.trajectory.stat.main', { n: agents.main }) : null,
    agents.external > 0 ? t('gui.trajectory.stat.external', { n: agents.external }) : null,
    agents.sub === 1 ? t('gui.trajectory.stat.sub_one') : agents.sub > 1 ? t('gui.trajectory.stat.sub_n', { n: agents.sub }) : null,
  ].filter((part): part is string => part !== null)
  return parts.length ? parts.join(' \u00b7 ') : null
}

/** What a worker was asked to do, the row's name: its task when the door
 *  names one, else whatever names the run. */
function taskOf(agent: WaterfallAgent): string {
  const label = agent.door?.meta?.label
  return typeof label === 'string' && label.trim() !== '' ? label : scopeTitle(agent.door)
}

/** The model an agent ran on: the first model call's, as a label. */
function agentModel(agent: WaterfallAgent): string | null {
  for (const turn of agent.turns) {
    for (const row of turn.rows) {
      const model = row.entry.meta?.model
      if (typeof model === 'string' && model !== '') return model
    }
  }
  return null
}

/** The preset whose brand mark a door's `meta.agent` names, if any. ACP runs
 *  spell the agent as the vendor does ('claude-code', 'Codex'), so the word is
 *  normalised once and then held to the marks the page already ships. */
const AGENT_ALIASES: Record<string, string> = {
  claude: 'claude_code', claude_code: 'claude_code', claudecode: 'claude_code',
  codex: 'codex', hermes: 'hermes', hermes_agent: 'hermes', openclaw: 'openclaw',
  opencode: 'opencode', pi: 'pi', qwen: 'qwen_code', qwen_code: 'qwen_code',
  kimi: 'kimi_code', kimi_code: 'kimi_code', grok: 'grok', copilot: 'github_copilot',
  github_copilot: 'github_copilot', qoder: 'qoder', codebuddy: 'codebuddy',
  antigravity: 'mirasim_antigravity', dsh: 'mirasim_dsh', zcode: 'mirasim_zcode',
  mirothinker: 'mirothinker',
}

function presetOf(agent: WaterfallAgent): string | null {
  const meta = agent.door?.meta ?? {}
  const raw = typeof meta.agent === 'string' ? meta.agent : typeof meta.instance === 'string' ? meta.instance : ''
  const word = raw.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_|_$/g, '')
  if (word === '') return null
  const direct = AGENT_ALIASES[word] ?? AGENT_ALIASES[word.split('_')[0] ?? '']
  if (direct !== undefined && agentMarkPath(direct) !== null) return direct
  return null
}

/** An agent of Raven's own, which wears the Raven tile: the in-process row and
 *  the vendored agents under `agents/`. The spawn span carries the displayed
 *  name and nothing about the package behind it, so the name is the signal
 *  here -- the roster reads `builtin`/`vendored` instead, a fact this entry
 *  does not carry. A vendor's agent spells itself after the vendor. */
function ownOf(agent: WaterfallAgent): boolean {
  const meta = agent.door?.meta ?? {}
  const raw = typeof meta.agent === 'string' ? meta.agent : typeof meta.instance === 'string' ? meta.instance : ''
  return /^raven([^a-z0-9]|$)/i.test(raw.trim())
}

/** An agent's mark: Raven's own tile, its vendor's mark, or a plain glyph. */
function agentMark(agent: WaterfallAgent): JSX.Element {
  if (agent.scope === null || ownOf(agent)) return <AgentMark own />
  const preset = presetOf(agent)
  return preset === null ? <span className="trajectory-wf-glyph"><Icon icon={BotIcon} size={15} /></span> : <AgentMark preset={preset} />
}

/** A call row's name: a tool by its own name, a model call by the part of
 *  the call the entry is (one call writes its input, its thinking and its
 *  answer), anything else by what it says. */
function callName(entry: TrajectoryEntry): string {
  const tool = entry.meta?.tool
  if (entry.kind.startsWith('tool.') && typeof tool === 'string' && tool !== '') return tool
  if (entry.kind.startsWith('llm.')) return kindLabel(entry.kind)
  return entry.preview ?? entry.span_name
}

/** What the call worked on, beside its name: the model a model call ran on,
 *  and a tool's argument or result without the tool's name the preview
 *  starts with ("bash npm test", "bash: 3 passed"). */
function callSubject(entry: TrajectoryEntry): string | null {
  if (entry.kind.startsWith('llm.')) return typeof entry.meta?.model === 'string' ? entry.meta.model : null
  const tool = entry.meta?.tool
  if (entry.kind.startsWith('tool.') && typeof tool === 'string' && tool !== '') {
    const said = entry.preview ?? ''
    const rest = said.startsWith(`${tool}: `) ? said.slice(tool.length + 2) : said.startsWith(`${tool} `) ? said.slice(tool.length + 1) : said
    return rest === '' ? null : rest
  }
  return entry.span_name
}

/** One of five fixed classes; the index's kind strings never reach a class name.
 *  A failed operation's bar is the failure's colour, whatever kind it was, and
 *  only a model call wears the model's: anything that is neither a model nor a
 *  tool call is neutral, as the turn's bar paints it (`segments.ts`). */
const spanClass = (entry: TrajectoryEntry): string =>
  entry.failure_entry ? 'trajectory-wf-kind-failure'
    : entry.kind.startsWith('tool.') ? 'trajectory-wf-kind-tool'
      : entry.kind === 'user.input' ? 'trajectory-wf-kind-user'
        : entry.kind.startsWith('llm.') ? 'trajectory-wf-kind-model' : 'trajectory-wf-kind-other'

/* The mark a failed operation carries on its own row, and on the turn that
   holds it -- folded, the turn's bar shows red only for a failure that took
   time, and a failure in an open turn otherwise reads like any other row. The
   index flags one entry per failed span (`failure_entry`), so the mark and the
   header's count both read that, never the span's status, which every entry
   of the span shares. */
function FailMark(): JSX.Element {
  const label = t('gui.trajectory.status.error')
  return <span className="trajectory-wf-fail" role="img" aria-label={label} title={label} />
}

/** Whether the wheel's target stands in a scroller on the island that can
 *  still move the way the wheel points: the overview's lanes, once the reader
 *  has opened past the strip's height. The island's own list is the caller's
 *  to check; everything else here is furniture with nowhere to go. */
function scrollsAt(root: HTMLElement, target: Node | null, deltaY: number): boolean {
  let node = target instanceof Element ? target : null
  for (; node && node !== root; node = node.parentElement) {
    const style = getComputedStyle(node)
    if (style.overflowY !== 'auto' && style.overflowY !== 'scroll') continue
    const max = node.scrollHeight - node.clientHeight
    if (max <= 1) continue
    if (deltaY < 0 ? node.scrollTop > 0 : node.scrollTop < max) return true
  }
  return false
}

/** The hovered turn's tooltip, which its row names as its description. */
const TIP_ID = 'trajectory-wf-tip'

/** The windows the header offers, after the whole session: a length in real time. */
const PRESETS: ReadonlyArray<readonly ['fit' | '3h' | '1h' | '15m', number | null]> = [
  ['fit', null], ['3h', 3 * 3_600_000], ['1h', 3_600_000], ['15m', 15 * 60_000],
]

/* A label closer than this to either end of the ruler would hang past it; its
   tick still draws. */
const EDGE_PX = 24

/** Where a vertical line is drawn: the last pixel stays inside the track, or
 *  the scroll box grows wider than itself and the overlay scrollbars answer
 *  with a horizontal bar nobody can use. */
const lineLeft = (left: number): string => (left >= 1 ? 'calc(100% - 1px)' : percent(left))

function Ticks({ axis, view, pick, onPick, ticks, step, onWidth }: {
  axis: TimeAxis; view: Viewport; pick: Pick | null; onPick: (pick: Pick | null) => void
  ticks: readonly ClockTick[]; step: number; onWidth: (width: number) => void
}): JSX.Element {
  const { from, to } = view
  /* The ruler is the one element that knows how wide the axis really is; the
     rows draw their grid lines from the same count, so the measured width
     goes up rather than each side guessing. */
  const strip = useRef<HTMLDivElement | null>(null)
  const [width, setWidth] = useState(0)
  useEffect(() => {
    const el = strip.current
    if (!el) return
    const measure = (): void => { setWidth(el.clientWidth); onWidth(el.clientWidth) }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [onWidth])
  /* Dragging across the axis picks an interval, the way the network panel's
     timeline does: the axis keeps its own scale -- its left edge is still the
     start of the conversation -- and what changes is the band around the
     picked stretch. Each edge carries a handle, and once there is a band the
     handles are the way to move that edge alone: the other one stays put, and
     the rows filter to whatever the band now covers. Nothing zooms, so the
     scale never shifts under the reader. */
  type Drag =
    | { mode: 'new'; at: number; width: number }
    | { mode: 'edge'; edge: 'lo' | 'hi'; width: number; fixed: number }
  const drag = useRef<Drag | null>(null)
  const fraction = (clientX: number, rect: DOMRect): number =>
    Math.min(1, Math.max(0, (clientX - rect.left) / Math.max(1, rect.width)))
  const down = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if (event.button !== 0) return
    const rect = event.currentTarget.getBoundingClientRect()
    const at = fraction(event.clientX, rect)
    const grip = (event.target as HTMLElement).closest?.('[data-edge]') as HTMLElement | null
    if (grip !== null && pick !== null) {
      const lo = Math.min(pick.from, pick.to)
      const hi = Math.max(pick.from, pick.to)
      const edge = grip.dataset.edge === 'lo' ? 'lo' : 'hi'
      drag.current = { mode: 'edge', edge, width: rect.width, fixed: edge === 'lo' ? hi : lo }
    } else {
      drag.current = { mode: 'new', at, width: rect.width }
      onPick({ from: at, to: at })
    }
    try {
      event.currentTarget.setPointerCapture(event.pointerId)
    } catch {
      /* not capturable */
    }
  }
  const move = (event: ReactPointerEvent<HTMLDivElement>): void => {
    const start = drag.current
    if (start === null) return
    const at = fraction(event.clientX, event.currentTarget.getBoundingClientRect())
    if (start.mode === 'edge') {
      onPick(start.edge === 'lo' ? { from: at, to: start.fixed } : { from: start.fixed, to: at })
      return
    }
    onPick({ from: start.at, to: at })
  }
  const up = (event: ReactPointerEvent<HTMLDivElement>): void => {
    const start = drag.current
    drag.current = null
    if (start === null) return
    const at = fraction(event.clientX, event.currentTarget.getBoundingClientRect())
    if (start.mode === 'edge') {
      onPick(start.edge === 'lo' ? { from: at, to: start.fixed } : { from: start.fixed, to: at })
      return
    }
    /* A click, not a drag, is how the pick is taken back. */
    onPick(Math.abs(at - start.at) * start.width < 4 ? null : { from: start.at, to: at })
  }
  const cancel = (): void => {
    drag.current = null
    onPick(null)
  }
  const span = to - from
  const room = width > 0 ? width : 1200
  const gaps = span > 0 ? axis.gaps : []
  return (
    <div className="trajectory-ruler" aria-hidden="true" ref={strip}
      onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={cancel}>
      {ticks.map((tick) => (
        <span key={`tick-${tick.time}`} className="trajectory-ruler-tick" data-major={tick.major ? '' : undefined}
          style={{ left: lineLeft(tick.left) }} />
      ))}
      {ticks.map((tick) => (
        tick.left * room < EDGE_PX || (1 - tick.left) * room < EDGE_PX ? null : (
          <b key={tick.time} data-major={tick.major ? '' : undefined} style={{ left: percent(tick.left) }}>{tickLabel(tick.time, step)}</b>
        )
      ))}
      {gaps.map((gap, index) => {
        const left = (gap.from - view.from) / span
        const right = (gap.to - view.from) / span
        const from = Math.max(0, left)
        const to = Math.min(1, right)
        /* A collapsed gap is a few pixels wide and a long idle stretch has a
           long word for it; squeezed in, the word sets itself one letter per
           line and reads as noise. Only a gap with room for the word says it. */
        if (to <= from || (to - from) * (width > 0 ? width : 1200) < 64) return null
        return (
          <span key={index} className="trajectory-ruler-idle"
            style={{ left: percent(from), width: percent(to - from) }}>
            {t('gui.trajectory.axis.idle', { duration: formatDuration(gap.end - gap.start) })}
          </span>
        )
      })}
      {pick === null ? null : (() => {
        const lo = Math.min(pick.from, pick.to)
        const hi = Math.max(pick.from, pick.to)
        return (
          <>
            {/* What is outside the interval is dimmed, the interval itself is
                left at the ruler's own ground, and each edge carries a grip. */}
            <span className="trajectory-ruler-dim" style={{ left: 0, width: percent(lo) }} />
            <span className="trajectory-ruler-dim" style={{ left: percent(hi), width: percent(1 - hi) }} />
            <span className="trajectory-ruler-pick" style={{ left: percent(lo), width: percent(hi - lo) }} />
            {/* A handle is a pill straddling its edge, half on either side of
                it -- at the axis's own ends too, where the ruler lets it out
                rather than cutting it in half. */}
            <span className="trajectory-ruler-grip" data-edge="lo" style={{ left: percent(lo) }} />
            <span className="trajectory-ruler-grip" data-edge="hi" style={{ left: percent(hi) }} />
          </>
        )
      })()}
    </div>
  )
}

export function Waterfall(): JSX.Element {
  useSyncExternalStore(store.subscribe, store.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  /* What the reader folded or opened, by agent key; an agent they have not
     touched takes the default below. */
  const [agentFold, setAgentFold] = useState<ReadonlyMap<string, boolean>>(new Map())
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set())
  const [scrollTop, setScrollTop] = useState(0)
  const [rulerWidth, setRulerWidth] = useState(0)
  const [height, setHeight] = useState(0)
  /* The reader's window and picked stretch, kept in real time and owned by
     the conversation they were made on. A live session's axis grows with
     every step that lands past its end, which moves every position on it; in
     milliseconds the two mean the same stretch on every axis of the
     conversation, so only a change of conversation, or the reader, drops them. */
  const [zoom, setZoom] = useState<{ session: string | null; start: number; end: number } | null>(null)
  const [picked, setPicked] = useState<{ a: number; b: number } | null>(null)
  const sessionKey = store.get().sessionKey
  const sessionRef = useRef(sessionKey)
  sessionRef.current = sessionKey
  const observer = useRef<ResizeObserver | null>(null)
  const box = useRef<HTMLDivElement | null>(null)
  const island = useRef<HTMLDivElement | null>(null)

  /* Closing the pane hands the focus back to the list, as it did when the rows
     were a list: the reader's next key press must land where they left off. */
  const pane = useSyncExternalStore(details.subscribe, details.get)
  const wasOpen = useRef(false)
  useLayoutEffect(() => {
    const closed = wasOpen.current && !pane.open
    wasOpen.current = pane.open
    /* A tab switch also closes the old pane; that keeps the focus on the strip. */
    if (!closed || details.get().swapped) return
    box.current?.focus()
  }, [pane.open])


  /* The wheel zooms about the pointer and Shift+wheel pans; a native listener,
     because React's own is passive and could not keep the page from scrolling. */
  const viewRef = useRef<Viewport>({ from: 0, to: 0 })
  const fullRef = useRef(0)
  const axisRef = useRef<TimeAxis | null>(null)
  useEffect(() => {
    const el = island.current
    const scroller = box.current
    if (!el || !scroller) return
    const onWheel = (event: WheelEvent): void => {
      /* A plain wheel is the list's to scroll. Only a deliberate gesture --
         pinch / ctrl+wheel to zoom, shift+wheel to pan -- is taken, or the page
         would have no vertical scroll at all. */
      const zooming = event.ctrlKey || event.metaKey
      const panning = event.shiftKey && !zooming
      if (!zooming && !panning) {
        /* Over the list the browser scrolls it. Another scroller standing on
           the island -- the overview's lanes once the reader has opened them
           all -- takes the wheel while it can still move this way; everything
           else (the header, the ruler, a strip with nowhere to go) forwards it
           to the list, so the page scrolls wherever the pointer happens to be. */
        if (scroller.contains(event.target as Node)) return
        if (scrollsAt(el, event.target as Node | null, event.deltaY)) return
        event.preventDefault()
        scroller.scrollTop += event.deltaY
        return
      }
      event.preventDefault()
      /* The axis is drawn over the track, not the whole row: the labels take
         the gutter on its left. So the pointer is read against the ruler, the
         one element that spans exactly the axis; read against the whole box,
         a pointer on the track's left edge would anchor a third of the way in,
         and a pan would move by the row's width rather than the axis's. */
      const ruler = el.querySelector<HTMLElement>('.trajectory-ruler')
      const rect = (ruler ?? scroller).getBoundingClientRect()
      const at = rect.width > 0 ? (event.clientX - rect.left) / rect.width : 0.5
      const current = viewRef.current
      const span = current.to - current.from
      /* Keep the ref current as we go: a trackpad delivers several wheel events
         per frame, and each must build on the one before it. The window is
         kept in real time (above); a zoom or a pan drops the picked stretch. */
      const keep = (view: Viewport): void => {
        const axisNow = axisRef.current
        viewRef.current = view
        if (axisNow === null) return
        setZoom({ session: sessionRef.current, start: axisNow.unproject(view.from), end: axisNow.unproject(view.to) })
        setPicked(null)
      }
      if (panning) {
        const shift = (span * event.deltaY) / Math.max(1, rect.width)
        keep(clampViewport({ from: current.from + shift, to: current.to + shift }, fullRef.current))
        return
      }
      const anchor = current.from + span * Math.min(1, Math.max(0, at))
      const factor = Math.max(0.5, Math.min(2, Math.exp(-event.deltaY * 0.0015)))
      keep(zoomViewport(current, factor, anchor, fullRef.current))
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)

  }, [])

  /* The clock the bars are cut at. It ticks only while something is still
     running: a fresh `Date.now()` every render would rebuild the whole model
     on every scroll frame, which is what a 500-turn session cannot afford. */
  const shown = store.get().shown
  const [now, setNow] = useState(() => Date.now())
  const live = useMemo(() => shown.some((entry) => isLive(entry, now)), [shown, now])
  useEffect(() => {
    if (!live) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [live])
  /* Both structures grow with the pages the walk lands: `grownFrom` is the
     store's word that the list was appended to rather than rewritten, and the
     indexes keep what they already built. A rewrite (the live feed, a session
     change) starts them over. */
  const grownFrom = store.get().grownFrom
  const tree = useRef<{ index: WaterfallIndex; built: number; now: number } | null>(null)
  const waterfall = useMemo(() => {
    const prev = tree.current
    if (prev !== null && grownFrom === prev.built && shown.length >= prev.built) {
      if (prev.now !== now) {
        prev.index.setNow(now)
        prev.now = now
      }
      if (shown.length > prev.built) prev.index.add(shown.slice(prev.built))
      prev.built = shown.length
      return prev.index.waterfall()
    }
    const index = new WaterfallIndex(now)
    index.add(shown)
    tree.current = { index, built: shown.length, now }
    return index.waterfall()
  }, [shown, grownFrom, now])

  /* Agents start folded when there are several -- the session reads as one
     row per agent -- and a lone agent starts open. */
  const foldedByDefault = waterfall.agents.length > 1
  const collapsed = useMemo(
    () => new Set(waterfall.agents.filter((agent) => !(agentFold.get(agent.key) ?? !foldedByDefault)).map((agent) => agent.key)),
    [waterfall, agentFold, foldedByDefault],
  )

  const axes = useRef<{ index: AxisIndex; built: number; now: number } | null>(null)
  const axis = useMemo(() => {
    const prev = axes.current
    /* A running row's end moves with the clock, so a tick rebuilds the axis
       rather than appending to it. */
    if (prev !== null && grownFrom === prev.built && shown.length >= prev.built && (!live || prev.now === now)) {
      if (shown.length > prev.built) {
        prev.index.add(shown.slice(prev.built).map((entry) => ({ start: entryStart(entry), end: entryEnd(entry, now) })))
      }
      prev.built = shown.length
      prev.now = now
      return prev.index.axis()
    }
    const index = new AxisIndex({ pad: TRAIL_PAD })
    index.add(shown.map((entry) => ({ start: entryStart(entry), end: entryEnd(entry, now) })))
    axes.current = { index, built: shown.length, now }
    return index.axis()
  }, [shown, grownFrom, now, live])
  /* The window, projected onto this axis: a different conversation's window
     means nothing here, and is not used. */
  const view: Viewport = (() => {
    if (zoom === null || zoom.session !== sessionKey) return { from: 0, to: axis.length }
    const from = axis.project(zoom.start)
    const to = axis.project(zoom.end)
    return to > from ? clampViewport({ from, to }, axis.length) : { from: 0, to: axis.length }
  })()
  viewRef.current = view
  /* The window in real time, and whether the reader has narrowed it. */
  const windowFrom = axis.unproject(view.from)
  const windowTo = axis.unproject(view.to)
  const zoomedView = view.from > 0 || view.to < axis.length
  fullRef.current = axis.length
  axisRef.current = axis
  useEffect(() => {
    setZoom(null)
    setPicked(null)
  }, [sessionKey])
  /* The picked stretch is owned here: the axis draws it, and the rows below
     are filtered to it. The ruler speaks in fractions of the view it is drawn
     over, so the stretch is handed to it that way and taken back in real time;
     Escape drops it too. */
  const viewSpan = view.to - view.from
  const pickOnView: Pick | null = picked === null || !(viewSpan > 0)
    ? null
    : { from: (axis.project(picked.a) - view.from) / viewSpan, to: (axis.project(picked.b) - view.from) / viewSpan }
  const pickFromView = (next: Pick | null): void => {
    setPicked(next === null || !(viewSpan > 0)
      ? null
      : { a: axis.unproject(view.from + viewSpan * next.from), b: axis.unproject(view.from + viewSpan * next.to) })
  }
  const held = picked !== null
  useEffect(() => {
    if (!held) return
    const onKey = (event: KeyboardEvent): void => { if (event.key === 'Escape') setPicked(null) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [held])
  const range = useMemo(
    () => (picked === null ? null : { start: Math.min(picked.a, picked.b), end: Math.max(picked.a, picked.b) }),
    [picked],
  )
  /* A filtered list is a new list: it starts at its own beginning, not at
     whatever scroll offset the full one happened to be at. */
  useEffect(() => {
    if (range === null) return
    const el = box.current
    if (el) el.scrollTop = 0
  }, [range])
  /* What the header and the badges say, from every row of the session. The
     list re-renders on every scroll event, and each of these walks the whole
     session, so they are worked out when the rows or the clock change, never
     per frame. */
  const summary = useMemo(() => {
    /* A scope id is `trace:span`; the badge must say who the worker is. */
    const nameOfScope = new Map<string, string>()
    for (const agent of waterfall.agents) {
      if (agent.scope === null) continue
      for (const scope of agent.scopes) if (scope !== null) nameOfScope.set(scope, scopeTitle(agent.door))
    }
    /* The slowest single span wears the badge, as the reference does. */
    let slowestId: string | null = null
    let slowestMs = 0
    for (const agent of waterfall.agents) {
      for (const turn of agent.turns) {
        for (const row of turn.rows) {
          const ms = entryEnd(row.entry, now) - entryStart(row.entry)
          if (ms > slowestMs) { slowestMs = ms; slowestId = row.entry.entry_id }
        }
      }
    }
    /* An agent's runs of work, merged: its row draws one capsule per run, and
       its busy time is their total, never its lifetime. Its failures are
       marked on the row at the instant each one happened. */
    const runs = new Map<string, Array<{ start: number; end: number }>>()
    const busy = new Map<string, number>()
    const failures = new Map<string, number[]>()
    for (const agent of waterfall.agents) {
      const agentRuns = busyRuns(agent, now)
      runs.set(agent.key, agentRuns)
      busy.set(agent.key, agentRuns.reduce((sum, run) => sum + run.end - run.start, 0))
      const failed = [...agent.turns.flatMap((turn) => turn.rows), ...agent.loose].filter((row) => row.entry.failure_entry)
      failures.set(agent.key, failed.map((row) => entryStart(row.entry)))
    }
    /* The turn that sent each worker: the turn, on whichever agent, that holds
       the worker's door. */
    const turnOfEntry = new Map<string, number>()
    for (const agent of waterfall.agents) {
      for (const turn of agent.turns) for (const row of turn.rows) turnOfEntry.set(row.entry.entry_id, turn.ordinal)
    }
    const sentFrom = new Map<string, number>()
    const kinds = new Map<string, ReturnType<typeof agentKind>>()
    for (const agent of waterfall.agents) {
      const ordinal = agent.door === null ? undefined : turnOfEntry.get(agent.door.entry_id)
      if (ordinal !== undefined) sentFrom.set(agent.key, ordinal)
      kinds.set(agent.key, agentKind(agent))
    }
    return { nameOfScope, slowestId, runs, busy, failures, sentFrom, kinds, figures: summarize(waterfall, shown, now) }
  }, [shown, waterfall, now])
  const { nameOfScope, slowestId, runs, busy, failures, sentFrom, kinds, figures } = summary

  /* Once the reader narrows the window, a worker whose work lies wholly before
     it, or wholly after it, steps into one row for each side; the main agent
     always keeps its own. */
  const [foldOpen, setFoldOpen] = useState<ReadonlySet<FoldSide>>(new Set())
  /* The turn row under the pointer, whose breakdown the tooltip shows. */
  const [hoverTurn, setHoverTurn] = useState<{ key: string; at: number } | null>(null)
  const outside = useMemo(() => {
    /* The whole session's window holds every agent, so nothing folds until
       the reader narrows it. */
    const side = new Map<number, FoldSide>()
    waterfall.agents.forEach((agent, index) => {
      const agentRuns = runs.get(agent.key) ?? []
      const first = agentRuns[0]
      const last = agentRuns.at(-1)
      if (agent.scope === null || first === undefined || last === undefined) return
      if (last.end < windowFrom) side.set(index, 'earlier')
      else if (first.start > windowTo) side.set(index, 'later')
    })
    return side
  }, [waterfall, runs, windowFrom, windowTo])
  const toggleFold = (side: FoldSide): void => {
    setFoldOpen((previous) => {
      const next = new Set(previous)
      if (next.has(side)) next.delete(side)
      else next.add(side)
      return next
    })
  }
  const rows = useMemo(() => {
    const drawn = displayRows(waterfall, collapsed, open, outside, foldOpen)
    if (range === null) return drawn
    /* Only what was running inside the picked stretch stays: an agent, a turn
       and a row are in it when their own stretch of time meets it. */
    const overlaps = (start: number, end: number): boolean => end >= range.start && start <= range.end
    return drawn.filter((row) => {
      if (row.kind === 'fold') return true
      if (row.kind === 'agent') {
        const agent = waterfall.agents[row.agent]
        return agent !== undefined && overlaps(agent.start, agent.end)
      }
      if (row.kind === 'turn') return row.turnRow !== null && overlaps(row.turnRow.start, row.turnRow.end)
      const entry = row.row?.entry
      return entry !== undefined && overlaps(entryStart(entry), entryEnd(entry, now))
    })
  }, [waterfall, collapsed, open, outside, foldOpen, range, now])
  const { first, last } = windowOf(scrollTop, height, rows.length)

  /* The rows' bar stays, so it is placed again whenever the list it stands
     for changes length: it mounts empty (the store's pages are still on the
     wire), folds and opens turns, and a resize moves the box under it. */
  useEffect(() => {
    const el = box.current
    if (el) showBar(el)
  }, [rows.length, height])

  /* What the reveal effect has already handled: one (selection, door) pair.
     Without this the effect would re-open a turn the reader just folded, since
     everything it depends on changes on that click. */
  const revealed = useRef<{ id: string | null; tick: number; phase: 'none' | 'opening' | 'done' }>({ id: null, tick: -1, phase: 'none' })

  /* A selection made elsewhere -- a link in the pane, a dispatching row's door --
     opens the turn that holds the row and brings the row into view, so a jump
     lands somewhere the reader can see. */
  const selectedId = store.get().selectedId
  const selections = store.get().selections
  /* Where the selection is drawn. The tree names it as its active descendant
     only while it is mounted: an id the page does not hold is no descendant. */
  const selectedAt = useMemo(
    () => (selectedId === null ? -1 : rows.findIndex((row) => row.kind === 'span' && row.row?.entry.entry_id === selectedId)),
    [rows, selectedId],
  )
  useEffect(() => {
    if (selectedId === null) return
    const same = revealed.current.id === selectedId && revealed.current.tick === selections
    if (same && revealed.current.phase === 'done') return
    /* Claimed before anything is written. A branch that opens a turn re-renders;
       acting on that render again is how a page gets into an update loop (React
       #185, which unmounts the island), so once the state has been asked for,
       the effect only ever scrolls -- never asks again. */
    const phase = same ? revealed.current.phase : 'none'
    revealed.current = { id: selectedId, tick: selections, phase: 'opening' }
    /* The row's agent, and its turn -- none for a row outside every turn, which
       hangs from its agent directly and needs only the agent open. */
    let owner: { agent: number; turn: string | null } | null = null
    for (const [index, agent] of waterfall.agents.entries()) {
      for (const turn of agent.turns) {
        if (turn.rows.some((row) => row.entry.entry_id === selectedId)) owner = { agent: index, turn: turn.id }
      }
      if (owner === null && agent.loose.some((row) => row.entry.entry_id === selectedId)) owner = { agent: index, turn: null }
    }
    if (owner === null) return
    const group = waterfall.agents[owner.agent]!.key
    const key = owner.turn === null ? null : turnKey(owner.agent, owner.turn)
    /* Everything that hides the row is opened in the one pass: an agent folded
       by default and the turn inside it are both closed, and the next pass
       only scrolls. */
    const agentShut = collapsed.has(group)
    const turnShut = key !== null && !open.has(key)
    const side = outside.get(owner.agent)
    const foldShut = side !== undefined && !foldOpen.has(side)
    if (phase === 'none' && (agentShut || turnShut || foldShut)) {
      if (agentShut) setAgentFold((previous) => new Map(previous).set(group, true))
      if (turnShut) setOpen((previous) => new Set(previous).add(key))
      if (foldShut) setFoldOpen((previous) => new Set(previous).add(side))
      return
    }
    const at = selectedAt
    const scroller = box.current
    if (at < 0 || scroller === null) return
    const top = at * ROW_HEIGHT
    if (top < scroller.scrollTop || top + ROW_HEIGHT > scroller.scrollTop + scroller.clientHeight) scroller.scrollTop = top
    revealed.current = { id: selectedId, tick: selections, phase: 'done' }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, selections, rows, open, collapsed, foldOpen])

  const attach = useCallback((el: HTMLDivElement | null): void => {
    box.current = el
    observer.current?.disconnect()
    observer.current = null
    if (!el) return
    /* The rows' bar stays: shown before the first scroll, not after it, and
       placed again whenever the box moves under it -- the details pane
       opening narrows this column without changing its height. */
    showBar(el)
    const measure = (): void => { setHeight(el.clientHeight); showBar(el) }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    observer.current = ro
  }, [])

  const toggleTurn = (id: string): void => {
    setOpen((previous) => {
      const next = new Set(previous)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const toggleAgent = (key: string): void => {
    setAgentFold((previous) => new Map(previous).set(key, collapsed.has(key)))
  }

  /* Every entry in the order the page draws them, for the arrow keys. */
  const order = useMemo(
    () => waterfall.agents.flatMap((agent) => [
      ...agent.turns.flatMap((turn) => turn.rows.map((row) => row.entry.entry_id)),
      ...agent.loose.map((row) => row.entry.entry_id),
    ]),
    [waterfall],
  )

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>): void => {
    /* Enter pressed on a control inside a row -- a fold button, a door's
       arrow -- is that control's: it must not also act on the row that holds
       the selection. The arrows have no meaning on a button and stay the tree's. */
    if (event.key === 'Enter' && event.target !== event.currentTarget) return
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp' || event.key === 'Home' || event.key === 'End') {
      event.preventDefault()
      const selected = store.get().selectedId
      const at = selected === null ? -1 : order.indexOf(selected)
      const next = event.key === 'Home' ? 0
        : event.key === 'End' ? order.length - 1
          : event.key === 'ArrowDown' ? Math.min(order.length - 1, at + 1) : Math.max(0, at - 1)
      const id = order[next]
      if (id !== undefined) store.selectAcross(id, { source: 'keyboard' })
      return
    }
    if (event.key !== 'Enter') return
    const selected = store.get().selectedId
    if (selected === null) return
    const entry = store.get().entries[store.get().index[selected]!]
    if (entry === undefined) return
    event.preventDefault()
    if (entry.opens_subagent_scope !== null && entry.opens_subagent_scope !== undefined) {
      locate(entry)
      return
    }
    for (const [index, agent] of waterfall.agents.entries()) {
      for (const turn of agent.turns) {
        if (turn.rows.some((row) => row.entry.entry_id === selected)) {
          toggleTurn(turnKey(index, turn.id))
          return
        }
      }
    }
  }

  /* The door's arrow brings the run it opened into view -- that run, not the
     first run of the agent it folds into -- landing where the pane's own jump
     does (scopes.ts). */
  const locate = (entry: TrajectoryEntry): void => {
    const scope = entry.opens_subagent_scope
    if (scope !== null && scope !== undefined) enterScope(scope)
  }

  const pick = (row: WaterfallRow): void => {
    store.selectAcross(row.entry.entry_id, { source: 'click' })
    details.openDetails()
  }

  const errors = figures.errors
  const jumpToFirstError = (): void => {
    if (figures.firstError === null) return
    store.selectAcross(figures.firstError, { source: 'click' })
    details.openDetails()
  }
  const stats: Array<[string, string, string | null]> = [
    [t('gui.trajectory.stat.duration'), formatDuration(waterfall.agents.length ? figures.end - figures.start : 0),
      waterfall.agents.length ? t('gui.trajectory.stat.span', spanOf(figures.start, figures.end)) : null],
    [t('gui.trajectory.stat.agents'), String(waterfall.agents.length), agentSplit(figures.agents)],
    [t('gui.trajectory.stat.turns'), String(figures.turns),
      figures.medianTurn === null ? null : t('gui.trajectory.stat.median', { duration: formatDuration(figures.medianTurn) })],
    [t('gui.trajectory.stat.llm'), String(figures.llm),
      figures.modelShare === null ? null : t('gui.trajectory.stat.model_share', { percent: Math.round(figures.modelShare * 100) })],
    [t('gui.trajectory.stat.tool'), String(figures.tool),
      figures.topTools.length
        ? figures.topTools.map(({ name, share }) => t('gui.trajectory.stat.tool_share', { name, percent: Math.round(share * 100) })).join(' \u00b7 ')
        : null],
    [t('gui.trajectory.stat.errors'), String(errors), null],
  ]

  /* The ruler and the rows' grid read the same marks: one per round clock
     time the ruler's width has room to label. */
  const room = rulerWidth > 0 ? rulerWidth : 1200
  const step = stepFor(view, room)
  const marks = useMemo(() => clockTicks(axis, { from: view.from, to: view.to }, room), [axis, view.from, view.to, room])
  /* The window the axis shows, named in the head: what ran inside it, from
     its first instant to its last, and how long a window it is once the
     reader has narrowed it. The element carries the window itself. */
  const shownFrom = Math.min(Math.max(windowFrom, figures.start), figures.end)
  const shownTo = Math.min(Math.max(windowTo, shownFrom), figures.end)
  const named = windowText(shownFrom, shownTo, step < 60_000 ? 'second' : 'minute')
  /* A preset is pressed while the window is that long, however it got there;
     a window the wheel made matches none. */
  const activePreset = !zoomedView
    ? 'fit'
    : PRESETS.find(([, length]) => length !== null && Math.abs(windowTo - windowFrom - length) <= length * 0.01)?.[0] ?? null
  const presetLabels = {
    fit: t('gui.trajectory.zoom.fit'), '3h': t('gui.trajectory.zoom.3h'),
    '1h': t('gui.trajectory.zoom.1h'), '15m': t('gui.trajectory.zoom.15m'),
  }
  const choosePreset = (length: number | null): void => {
    const next = length === null ? null : presetWindow(axis, view, length)
    setZoom(next === null ? null : { session: sessionKey, start: next.start, end: next.end })
    setPicked(null)
  }
  const painted: JSX.Element[] = []
  for (let at = first; at < last; at++) {
    const row = rows[at]!
    const style = { top: at * ROW_HEIGHT, height: ROW_HEIGHT }
    if (row.kind === 'fold') {
      const { side, agents: held } = row.fold!
      const members = held.map((index) => waterfall.agents[index]!)
      const edges = members.flatMap((member) => runs.get(member.key) ?? [])
      const time = clockOf(side === 'earlier' ? Math.max(...edges.map((run) => run.end)) : Math.min(...edges.map((run) => run.start)))
      const one = members.length === 1
      const label = side === 'earlier'
        ? one ? t('gui.trajectory.fold.earlier_one', { time }) : t('gui.trajectory.fold.earlier_n', { n: members.length, time })
        : one ? t('gui.trajectory.fold.later_one', { time }) : t('gui.trajectory.fold.later_n', { n: members.length, time })
      const foldOpened = foldOpen.has(side)
      painted.push(
        <div key={row.key} id={rowId(at)} className="trajectory-wf-row trajectory-wf-fold" data-row="fold" data-side={side} style={style}
          role="treeitem" aria-level={1} aria-expanded={foldOpened} onClick={() => toggleFold(side)}>
          <span className="trajectory-wf-label" style={{ paddingLeft: 12 }}>
            <button type="button" className="trajectory-wf-chev" aria-expanded={foldOpened} aria-label={label}>
              <Icon icon={foldOpened ? ArrowDown01Icon : ArrowRight01Icon} size={14} />
            </button>
            <span className="trajectory-wf-stack" aria-hidden="true">
              {members.slice(0, 4).map((member) => <span key={member.key} className="trajectory-wf-tile">{agentMark(member)}</span>)}
            </span>
            <span className="trajectory-wf-name">{label}</span>
          </span>
          <span className="trajectory-wf-track">
            <i className="trajectory-wf-edge" data-side={side === 'earlier' ? 'start' : 'end'} />
          </span>
        </div>,
      )
      continue
    }
    const agent = waterfall.agents[row.agent]!
    if (row.kind === 'agent') {
      const agentRuns = runs.get(agent.key) ?? []
      const shownRuns = within(agentRuns, windowFrom, windowTo)
      const first = agentRuns[0]
      const last = agentRuns.at(-1)
      const life = first === undefined || last === undefined ? null : barOf(axis, view, first.start, last.end)
      /* Nothing of this agent inside the window: say when it works next, or
         when it last did, at the edge that work lies past. */
      const next = shownRuns.length === 0 ? agentRuns.find((run) => run.start > windowTo) : undefined
      /* Runs are merged and in order: with none inside the window and none
         after it, every one lies before it and the last is the latest. */
      const previous = shownRuns.length === 0 && next === undefined ? last : undefined
      const agentOpen = !collapsed.has(agent.key)
      const agentName = agent.scope === null ? t('gui.trajectory.scope.main') : taskOf(agent)
      /* An open worker's second line: who ran it, the turn that sent it, and
         how many turns it took. */
      const runner = typeof agent.door?.meta?.agent === 'string' && agent.door.meta.agent.trim() !== '' ? agent.door.meta.agent : null
      const sent = sentFrom.get(agent.key)
      const secondLine = [
        runner,
        sent === undefined ? null : t('gui.trajectory.agent.from_turn', { n: sent }),
        agent.turns.length === 1 ? t('gui.trajectory.agent.turns_one') : t('gui.trajectory.agent.turns_n', { n: agent.turns.length }),
      ].filter((part): part is string => part !== null).join(' \u00b7 ')
      painted.push(
        <div key={row.key} id={rowId(at)} className="trajectory-wf-row trajectory-wf-agent" data-row="agent" style={style}
          data-main={agent.scope === null ? '' : undefined}
          role="treeitem" aria-level={1} aria-expanded={agentOpen}
          onClick={() => toggleAgent(agent.key)}>
          <span className="trajectory-wf-label" style={{ paddingLeft: 12 }}>
            {/* The fold control is a button, so the keyboard reaches it; its
                click is the row's, which does the folding. */}
            <button type="button" className="trajectory-wf-chev" aria-expanded={agentOpen} aria-label={agentName}>
              <Icon icon={agentOpen ? ArrowDown01Icon : ArrowRight01Icon} size={14} />
            </button>
            <span className="trajectory-wf-tile" data-tone={row.agent % 4}>{agentMark(agent)}</span>
            {agent.scope !== null && agentOpen ? (
              <span className="trajectory-wf-two">
                <span className="trajectory-wf-name" title={scopeTitle(agent.door)}>{agentName}</span>
                <span className="trajectory-wf-agentsub">{secondLine}</span>
              </span>
            ) : (
              <span className="trajectory-wf-name" title={agent.scope === null ? undefined : scopeTitle(agent.door)}>{agentName}</span>
            )}
            {agent.scope === null ? <span className="trajectory-wf-sub">{agentModel(agent)}</span> : null}
            {kinds.get(agent.key) === 'sub' ? <span className="trajectory-wf-tag">{t('gui.trajectory.tag.sub')}</span> : null}
            <span className="trajectory-wf-busy">{formatDuration(busy.get(agent.key) ?? 0)}</span>
          </span>
          <span className="trajectory-wf-track">
            {life ? <i className="trajectory-wf-life" style={{ left: percent(life.left), width: percent(life.width) }} /> : null}
            {shownRuns.map((run) => {
              const cap = barOf(axis, view, run.start, run.end)
              return cap ? <i key={run.start} className="trajectory-wf-cap" style={{ left: percent(cap.left), width: percent(cap.width) }} /> : null
            })}
            {(failures.get(agent.key) ?? []).filter((time) => time >= windowFrom && time <= windowTo).map((time) => (
              <i key={`fail-${time}`} className="trajectory-wf-diamond" style={{ left: percent((axis.project(time) - view.from) / viewSpan) }} />
            ))}
            {first !== undefined && first.start < windowFrom ? <i className="trajectory-wf-edge" data-side="start" /> : null}
            {last !== undefined && last.end > windowTo ? <i className="trajectory-wf-edge" data-side="end" /> : null}
            {next === undefined ? null : <span className="trajectory-wf-next" data-side="end">{t('gui.trajectory.next', { time: clockOf(next.start) })}</span>}
            {previous === undefined ? null : <span className="trajectory-wf-next" data-side="start">{t('gui.trajectory.last', { time: clockOf(previous.end) })}</span>}
          </span>
        </div>,
      )
      continue
    }
    if (row.kind === 'turn') {
      const turn = row.turnRow!
      const parts = turnSegments(turn, now)
      const openTurn = open.has(turnKey(row.agent, turn.id))
      const turnName = t('gui.trajectory.turn.ordinal', { n: turn.ordinal })
      painted.push(
        <div key={row.key} id={rowId(at)} className="trajectory-wf-row trajectory-wf-turn" data-row="turn" style={style}
          role="treeitem" aria-level={2} aria-expanded={openTurn}
          data-open={openTurn ? '' : undefined} data-failed={turn.failure ? '' : undefined}
          data-from={turn.reportedFrom === null ? undefined : turn.reportedFrom}
          aria-describedby={hoverTurn?.key === row.key ? TIP_ID : undefined}
          onPointerEnter={() => setHoverTurn({ key: row.key, at })}
          onPointerLeave={() => setHoverTurn((current) => (current?.key === row.key ? null : current))}
          onClick={() => toggleTurn(turnKey(row.agent, turn.id))}>
          <span className="trajectory-wf-guide" style={{ left: GUIDE_X }} />
          <span className="trajectory-wf-label" style={{ paddingLeft: 26 }}>
            <button type="button" className="trajectory-wf-chev" aria-expanded={openTurn} aria-label={turnName}>
              <Icon icon={openTurn ? ArrowDown01Icon : ArrowRight01Icon} size={14} />
            </button>
            <span className="trajectory-wf-name">{turnName}</span>
            {turn.failure ? <FailMark /> : null}
            {turn.preview === null ? null : <span className="trajectory-wf-sub">{turn.preview}</span>}
            {turn.reportedFrom === null ? null : <span className="trajectory-wf-from">{t('gui.trajectory.turn.from_report', { name: nameOfScope.get(turn.reportedFrom) ?? turn.reportedFrom })}</span>}
            <span className="trajectory-wf-busy">{formatDuration(turn.end - turn.start)}</span>
          </span>
          <span className="trajectory-wf-track">
            {parts.map((part, index) => {
              const slice = barOf(axis, view, part.start, part.end)
              return slice ? <i key={index} className={`trajectory-wf-seg trajectory-wf-seg-${part.kind}`} style={{ left: percent(slice.left), width: percent(slice.width) }} /> : null
            })}
          </span>
        </div>,
      )
      continue
    }
    const entry = row.row!.entry
    const bar = barOf(axis, view, entryStart(entry), entryEnd(entry, now))
    painted.push(
      <div key={row.key} id={rowId(at)} className="trajectory-wf-row trajectory-wf-span" data-row="span" data-entry={entry.entry_id}
        role="treeitem" aria-level={row.turn === null ? 2 : 3} aria-selected={entry.entry_id === selectedId}
        data-failed={entry.failure_entry ? '' : undefined} style={style} onClick={() => pick(row.row!)}>
        <span className="trajectory-wf-guide" style={{ left: GUIDE_X }} />
        <span className="trajectory-wf-guide" style={{ left: TURN_GUIDE_X }} />
        <span className="trajectory-wf-label" style={{ paddingLeft: 44 }}>
          <span className={`trajectory-wf-key ${spanClass(entry)}`} aria-hidden="true" />
          <span className="trajectory-wf-name">{callName(entry)}</span>
          {entry.failure_entry ? <FailMark /> : null}
          {callSubject(entry) === null ? null : <span className="trajectory-wf-sub">{callSubject(entry)}</span>}
          {entry.entry_id === slowestId ? <span className="trajectory-wf-slow">{t('gui.trajectory.slowest')}</span> : null}
          {entry.opens_subagent_scope === null || entry.opens_subagent_scope === undefined ? null : (
            <button className="trajectory-scope-opens" aria-label={t('gui.trajectory.scope.opens')}
              title={t('gui.trajectory.scope.opens')}
              onClick={(event) => { event.stopPropagation(); locate(entry) }}>{'\u2197'}</button>
          )}
          <span className="trajectory-wf-busy">{formatDuration(entryEnd(entry, now) - entryStart(entry))}</span>
        </span>
        <span className="trajectory-wf-track">
          {bar ? <i className={`trajectory-wf-spanbar ${spanClass(entry)}`} style={{ left: percent(bar.left), width: percent(bar.width) }} /> : null}
        </span>
      </div>,
    )
  }

  const foldAll = (): void => {
    setAgentFold(new Map(waterfall.agents.map((agent) => [agent.key, false])))
    setOpen(new Set())
  }

  /* The hovered turn's breakdown, just under its row and from where its bar
     starts: what its time went on, each share of the turn's length. */
  const tip = (() => {
    /* Read by the index it was hovered at, never by a walk over the rows; a
       row that has moved since (a fold above it) no longer matches. */
    const at = hoverTurn === null || rows[hoverTurn.at]?.key !== hoverTurn.key ? -1 : hoverTurn.at
    const turn = at < 0 ? null : rows[at]!.turnRow
    if (turn === null) return null
    const parts = turnBreakdown(turn, now)
    const share = (ms: number): number => (parts.total > 0 ? Math.round((ms / parts.total) * 100) : 0)
    const calls = (n: number): string => (n === 1 ? t('gui.trajectory.tip.calls_one') : t('gui.trajectory.tip.calls_n', { n }))
    const precision = parts.total < 120_000 ? 'second' : 'minute'
    const left = Math.min(0.72, Math.max(0, barOf(axis, view, turn.start, turn.end)?.left ?? 0))
    return (
      <div id={TIP_ID} className="trajectory-wf-tip" role="tooltip"
        style={{ top: (at + 1) * ROW_HEIGHT + 4, left: `calc(var(--wf-gutter) + (100% - var(--wf-gutter)) * ${left.toFixed(4)})` }}>
        <div data-part="head">
          {t('gui.trajectory.tip.head', {
            turn: t('gui.trajectory.turn.ordinal', { n: turn.ordinal }),
            from: clockOf(turn.start, precision), to: clockOf(turn.end, precision), length: formatDuration(parts.total),
          })}
        </div>
        <div data-part="model"><i className="trajectory-wf-key trajectory-wf-kind-model" />
          {t('gui.trajectory.tip.model', { calls: calls(parts.model.calls), time: formatDuration(parts.model.ms), percent: share(parts.model.ms) })}</div>
        <div data-part="tool"><i className="trajectory-wf-key trajectory-wf-kind-tool" />
          {t('gui.trajectory.tip.tool', { calls: calls(parts.tool.calls), time: formatDuration(parts.tool.ms), percent: share(parts.tool.ms) })}</div>
        {parts.subagent > 0 ? (
          <div data-part="subagent"><i className="trajectory-wf-key" />
            {t('gui.trajectory.tip.subagent', { time: formatDuration(parts.subagent), percent: share(parts.subagent) })}</div>
        ) : null}
        {parts.failure > 0 ? (
          <div data-part="failure"><i className="trajectory-wf-key trajectory-wf-kind-failure" />
            {t('gui.trajectory.tip.failure', { time: formatDuration(parts.failure), percent: share(parts.failure) })}</div>
        ) : null}
        <div data-part="waiting"><i className="trajectory-wf-key" />
          {t('gui.trajectory.tip.waiting', { time: formatDuration(parts.waiting), percent: share(parts.waiting) })}</div>
      </div>
    )
  })()

  return (
    <div className="trajectory-wf" ref={island}>
      <div className="trajectory-wf-stats">
        {stats.map(([label, value, sub]) => {
          const errorCell = label === t('gui.trajectory.stat.errors')
          return (
            <div key={label} className="trajectory-wf-stat">
              <span className="trajectory-wf-statk">{label}</span>
              <span className="trajectory-wf-statv" data-bad={errorCell && errors > 0 ? '' : undefined}>{value}</span>
              {sub === null ? null : <span className="trajectory-wf-statsub">{sub}</span>}
              {errorCell && figures.firstError !== null ? (
                <button type="button" className="trajectory-wf-statsub trajectory-wf-jump" onClick={jumpToFirstError}>
                  {t('gui.trajectory.stat.jump')}
                </button>
              ) : null}
            </div>
          )
        })}
        <div className="trajectory-wf-tools">
          <div className="trajectory-wf-zoom" role="group" aria-label={t('gui.trajectory.zoom.label')}>
            {PRESETS.map(([name, length]) => (
              <button key={name} type="button" aria-pressed={activePreset === name} onClick={() => choosePreset(length)}>
                {presetLabels[name]}
              </button>
            ))}
          </div>
          <button type="button" className="trajectory-wf-tool trajectory-wf-foldall" onClick={foldAll}>
            {t('gui.trajectory.fold_all')}
          </button>
        </div>
      </div>
      <OverviewLanes waterfall={waterfall} axis={axis} viewport={view} />
      <div className="trajectory-wf-head">
        <div className="trajectory-wf-headlabel">
          {waterfall.agents.length === 0 ? null : (
            <div className="trajectory-wf-window" data-start={windowFrom} data-end={windowTo}>
              <b>{named.day}</b>
              <span>
                {zoomedView
                  ? t('gui.trajectory.window.zoomed', { from: named.from, to: named.to, length: lengthOf(windowTo - windowFrom) })
                  : t('gui.trajectory.window.whole', { from: named.from, to: named.to })}
              </span>
            </div>
          )}
          <span className="trajectory-wf-busyhead">{t('gui.trajectory.busy')}</span>
        </div>
        <Ticks axis={axis} view={view} pick={pickOnView} onPick={pickFromView} ticks={marks} step={step} onWidth={setRulerWidth} />
      </div>
      {/* A tree, as the old list was a listbox: the box holds the focus, the
          arrows move the selection, and the selected row is its active
          descendant, so a screen reader follows the keys. */}
      <div className="trajectory-list trajectory-wf-scroll" data-thumb="keep" tabIndex={0} ref={attach}
        onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
        onKeyDown={onKeyDown}
        onDoubleClick={() => { setZoom(null); setPicked(null) }}
        role="tree" aria-label={t('gui.trajectory.agents')}
        aria-activedescendant={selectedAt >= first && selectedAt < last ? rowId(selectedAt) : undefined}>
        <div className="trajectory-wf-rows" role="none" style={{ height: rows.length * ROW_HEIGHT }}>
          <div className="trajectory-wf-links" aria-hidden="true">
            {marks.map((tick) => (
              <span key={`grid-${tick.time}`} className="trajectory-wf-grid" data-major={tick.major ? '' : undefined}
                style={{ left: lineLeft(tick.left) }} />
            ))}
          </div>
          {painted}
          {tip}
        </div>
      </div>
    </div>
  )
}

/** A byte count in MiB, to one decimal, the way the status line words a scan. */
export const mib = (bytes: number): string => (bytes / (1024 * 1024)).toFixed(1)
