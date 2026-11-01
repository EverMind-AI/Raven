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

import { ArrowDown01Icon, ArrowRight01Icon, BotIcon, PlayIcon, StopIcon } from '@hugeicons/core-free-icons'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'

import { AgentMark, agentMarkPath } from '../../../components/AgentMark'
import { Icon } from '../../../components/Icon'
import { t } from '../../../i18n/t'
import { formatDuration } from '../../../lib/duration'
import * as lang from '../../../state/lang'
import { showBar } from '../../../state/scrollbars'
import * as details from '../detailStore'
import { kindLabel, toolLabel } from '../palette'
import { enter as enterScope, scopeTitle } from '../scopes'
import * as store from '../store'
import { ActivityStrip } from './ActivityStrip'
import { AxisIndex, TRAIL_PAD, barOf, clampViewport, presetWindow, zoomViewport } from './axis'
import { clockOf, lengthOf, spanOf, windowText } from './clock'
import { WaterfallIndex } from './incremental'
import { buildWaterfall, busyRuns, entryEnd, entryStart, isLive, turnKey } from './model'
import { CATCH_UP_RATE, REPLAY_MS, advance, entriesAt, startReplay } from './replay'
import { turnBreakdown, turnSegments } from './segments'
import { summarize } from './summary'
import { clockTicks, stepFor, tickLabel } from './ticks'

import type { TrajectoryEntry } from '../types'
import type { TimeAxis, Viewport } from './axis'
import type { Waterfall, WaterfallAgent, WaterfallRow, WaterfallTurn } from './model'
import type { Replay } from './replay'
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
      rows.push({ kind: 'turn', key: `turn:${turnKey(agent.key, turn.id)}`, agent: at, turn: turn.id, row: null, turnRow: turn })
      if (!openTurns.has(turnKey(agent.key, turn.id))) continue
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
    agents.sub === 1 ? t('gui.trajectory.stat.sub_one') : agents.sub > 1 ? t('gui.trajectory.stat.sub_n', { n: agents.sub }) : null,
  ].filter((part): part is string => part !== null)
  return parts.length ? parts.join(' \u00b7 ') : null
}

/** The header's six figures, each with the line that says what it is made of. */
function statsOf(figures: Figures, agents: number): Array<[string, string, string | null]> {
  return [
    [t('gui.trajectory.stat.duration'), formatDuration(agents ? figures.end - figures.start : 0),
      agents ? t('gui.trajectory.stat.span', spanOf(figures.start, figures.end)) : null],
    [t('gui.trajectory.stat.agents'), String(agents), agentSplit(figures.agents)],
    [t('gui.trajectory.stat.turns'), String(figures.turns),
      figures.medianTurn === null ? null : t('gui.trajectory.stat.median', { duration: formatDuration(figures.medianTurn) })],
    [t('gui.trajectory.stat.llm'), String(figures.llm),
      figures.modelShare === null ? null : t('gui.trajectory.stat.model_share', { percent: Math.round(figures.modelShare * 100) })],
    [t('gui.trajectory.stat.tool'), String(figures.tool),
      figures.topTools.length
        ? figures.topTools.map(({ name, share }) => t('gui.trajectory.stat.tool_share', { name: toolLabel(name), percent: Math.round(share * 100) })).join(' \u00b7 ')
        : null],
    [t('gui.trajectory.stat.errors'), String(figures.errors), null],
  ]
}

/** A figure drawn in the room another takes. While a replay runs, each figure in the header
 *  sits over a hidden copy of the whole session's, which sets its cell's width: a count gaining
 *  a digit moves none of its neighbours, and a line that is longer mid-way than at the end is
 *  cut short rather than widening its cell. */
function Held({ text, held }: { text: string | null; held: string | null }): JSX.Element {
  if (held === null) return <>{text}</>
  return (
    <span className="trajectory-wf-held">
      <span>{text}</span>
      <span aria-hidden="true">{held}</span>
    </span>
  )
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
  if (entry.kind.startsWith('tool.') && typeof tool === 'string' && tool !== '') return toolLabel(tool)
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

/** The hovered turn's tooltip, which its row names as its description. */
const TIP_ID = 'trajectory-wf-tip'

/** The windows the header offers, after the whole session: a length in real time. */
const PRESETS: ReadonlyArray<readonly ['fit' | '3h' | '1h' | '15m', number | null]> = [
  ['fit', null], ['3h', 3 * 3_600_000], ['1h', 3_600_000], ['15m', 15 * 60_000],
]

/* A label closer than this to either end of the ruler would hang past it; its
   tick still draws. */
const EDGE_PX = 24

/** How far a press on the ruler travels before it is a drag rather than a click. */
const CLICK_PX = 4

/** Where a vertical line is drawn: the last pixel stays inside the track, or
 *  the scroll box grows wider than itself and the overlay scrollbars answer
 *  with a horizontal bar nobody can use. */
const lineLeft = (left: number): string => (left >= 1 ? 'calc(100% - 1px)' : percent(left))

function Ticks({ axis, view, pick, onPick, ticks, step, onWidth, playhead }: {
  axis: TimeAxis; view: Viewport; pick: Pick | null; onPick: (pick: Pick | null) => void
  ticks: readonly ClockTick[]; step: number; onWidth: (width: number) => void
  /** A running replay's playhead, as a fraction of the view; null when none is on screen. */
  playhead: number | null
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
  /* A new drag picks nothing until it has travelled CLICK_PX: a pick, even a
     zero-width one, filters the rows and sends the list back to its top, which
     a click -- one that only takes a pick back, or none at all -- must not do. */
  type Drag =
    | { mode: 'new'; at: number; width: number; picking: boolean }
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
      drag.current = { mode: 'new', at, width: rect.width, picking: false }
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
    if (!start.picking && Math.abs(at - start.at) * start.width < CLICK_PX) return
    start.picking = true
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
    onPick(Math.abs(at - start.at) * start.width < CLICK_PX ? null : { from: start.at, to: at })
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
      {playhead === null ? null : <span className="trajectory-ruler-playhead" style={{ left: lineLeft(playhead) }} />}
    </div>
  )
}

/** What a running replay's button reads beside Stop: the time the first pass has left, in
 *  whole seconds -- a rate would belie a folded axis, whose idle hours pass in a moment --
 *  and then the rate the catch-up plays at. */
function replayReadout(replay: Replay): string {
  if (replay.pressed === null) return `${CATCH_UP_RATE}\u00d7`
  const left = Math.max(0, Math.ceil((replay.duration - replay.played) / 1000))
  return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`
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
     conversation, so only a change of conversation, or the reader, drops them.
     The pick is the store's, so the page's one Escape table can take it back;
     the window is this component's. */
  const [zoom, setZoom] = useState<{ session: string | null; start: number; end: number } | null>(null)
  const picked = store.get().picked
  const sessionKey = store.get().sessionKey
  const sessionRef = useRef(sessionKey)
  sessionRef.current = sessionKey
  const observer = useRef<ResizeObserver | null>(null)
  const box = useRef<HTMLDivElement | null>(null)
  const island = useRef<HTMLDivElement | null>(null)

  /* Closing the pane hands the focus back to the list, as it did when the rows
     were a list: the reader's next key press must land where they left off. */
  const paneOpen = useSyncExternalStore(details.subscribe, () => details.get().open)
  const wasOpen = useRef(false)
  useLayoutEffect(() => {
    const closed = wasOpen.current && !paneOpen
    wasOpen.current = paneOpen
    /* A tab switch also closes the old pane; that keeps the focus on the strip. */
    if (!closed || details.get().swapped) return
    box.current?.focus()
  }, [paneOpen])


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
        /* Over the list the browser scrolls it; everywhere else on the island
           -- the header, the strip, the ruler -- the wheel is forwarded to the
           list, so the page scrolls wherever the pointer happens to be. */
        if (scroller.contains(event.target as Node)) return
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
        store.setPicked(null)
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

  /* The clock the bars are cut at, unless a replay cuts them at its playhead
     (`clock` below). It ticks only while something is still running: a fresh
     `Date.now()` every render would rebuild the whole model on every scroll
     frame, which is what a 500-turn session cannot afford. */
  const entries = store.get().entries
  const [now, setNow] = useState(() => Date.now())
  const live = useMemo(() => entries.some((entry) => isLive(entry, now)), [entries, now])
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
  const whole = useMemo(() => {
    const prev = tree.current
    if (prev !== null && grownFrom === prev.built && entries.length >= prev.built) {
      if (prev.now !== now) {
        prev.index.setNow(now)
        prev.now = now
      }
      if (entries.length > prev.built) prev.index.add(entries.slice(prev.built))
      prev.built = entries.length
      return prev.index.waterfall()
    }
    const index = new WaterfallIndex(now)
    index.add(entries)
    tree.current = { index, built: entries.length, now }
    return index.waterfall()
  }, [entries, grownFrom, now])

  /* A replay, while one runs (`replay.ts`): the rows, the figures and the
     strip are drawn as the session stood at the playhead, on the axis of the
     whole session. Its tree is built whole every frame -- the steps running
     at the playhead are cut somewhere new each time, which an index that
     only appends cannot follow. */
  const [replay, setReplay] = useState<Replay | null>(null)
  const replaying = replay !== null
  const replayRef = useRef<Replay | null>(null)
  const replayAt = replay?.at ?? null
  const clock = replayAt ?? now
  const shown = useMemo(() => (replayAt === null ? entries : entriesAt(entries, replayAt, now)), [entries, replayAt, now])
  const replayed = useMemo(() => (replayAt === null ? null : buildWaterfall(shown, replayAt)), [shown, replayAt])
  const waterfall = replayed ?? whole

  /* Agents start folded when there are several -- the session reads as one
     row per agent -- and a lone agent starts open. Counted over the whole
     session, so a replay does not fold the main agent shut under the reader
     the moment its first worker appears. */
  const foldedByDefault = whole.agents.length > 1
  const collapsed = useMemo(
    () => new Set(waterfall.agents.filter((agent) => !(agentFold.get(agent.key) ?? !foldedByDefault)).map((agent) => agent.key)),
    [waterfall, agentFold, foldedByDefault],
  )

  const axes = useRef<{ index: AxisIndex; built: number; now: number } | null>(null)
  const axis = useMemo(() => {
    const prev = axes.current
    /* A running row's end moves with the clock, so a tick rebuilds the axis
       rather than appending to it. */
    if (prev !== null && grownFrom === prev.built && entries.length >= prev.built && (!live || prev.now === now)) {
      if (entries.length > prev.built) {
        prev.index.add(entries.slice(prev.built).map((entry) => ({ start: entryStart(entry), end: entryEnd(entry, now) })))
      }
      prev.built = entries.length
      prev.now = now
      return prev.index.axis()
    }
    const index = new AxisIndex({ pad: TRAIL_PAD })
    index.add(entries.map((entry) => ({ start: entryStart(entry), end: entryEnd(entry, now) })))
    axes.current = { index, built: entries.length, now }
    return index.axis()
  }, [entries, grownFrom, now, live])
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
  }, [sessionKey])

  /* The playhead moves once an animation frame, by the wall clock. A frame
     reads the axis, the rows and the clock of the last render together, so
     it never pairs one render's axis with another's rows. A replay belongs
     to the conversation it was started on, and to the reader watching it:
     the island stays mounted behind the chat, and going back to the chat
     lets the replay go rather than leave it rebuilding rows out of sight. */
  const onScreen = store.get().view === 'trajectory'
  const scene = useRef({ axis, entries, now })
  scene.current = { axis, entries, now }
  useEffect(() => {
    if (!replaying) return
    let last = performance.now()
    let frame = requestAnimationFrame(function step(time: number): void {
      const current = replayRef.current
      if (current === null) return
      const next = advance(current, scene.current.axis, time - last, scene.current.entries, scene.current.now)
      last = time
      replayRef.current = next
      setReplay(next)
      if (next !== null) frame = requestAnimationFrame(step)
    })
    return () => cancelAnimationFrame(frame)
  }, [replaying])
  useEffect(() => {
    replayRef.current = null
    setReplay(null)
  }, [sessionKey, onScreen])
  /* The picked stretch is the store's: the axis draws it, and the rows below
     are filtered to it. The ruler speaks in fractions of the view it is drawn
     over, so the stretch is handed to it that way and taken back in real time;
     the Escape table takes it back (`detailStore`). */
  const viewSpan = view.to - view.from
  const pickOnView: Pick | null = picked === null || !(viewSpan > 0)
    ? null
    : { from: (axis.project(picked.a) - view.from) / viewSpan, to: (axis.project(picked.b) - view.from) / viewSpan }
  const pickFromView = (next: Pick | null): void => {
    store.setPicked(next === null || !(viewSpan > 0)
      ? null
      : { a: axis.unproject(view.from + viewSpan * next.from), b: axis.unproject(view.from + viewSpan * next.to) })
  }
  /* Where a running replay has got to, as a fraction of the view, while that is on screen. */
  const playheadAt = replayAt === null || !(viewSpan > 0) ? null : (axis.project(replayAt) - view.from) / viewSpan
  const playhead = playheadAt !== null && playheadAt >= 0 && playheadAt <= 1 ? playheadAt : null
  const range = useMemo(
    () => (picked === null ? null : { start: Math.min(picked.a, picked.b), end: Math.max(picked.a, picked.b) }),
    [picked],
  )
  /* Whether the picked stretch keeps this entry: the same overlap the rows are
     filtered by, so a key can never land on a row the reader cannot see. */
  const inPick = useCallback((entryId: string): boolean => {
    if (range === null) return true
    const s = store.get()
    const at = s.index[entryId]
    const entry = at === undefined ? undefined : s.entries[at]
    if (entry === undefined) return false
    return entryEnd(entry, now) >= range.start && entryStart(entry) <= range.end
  }, [range, now])
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
          const ms = entryEnd(row.entry, clock) - entryStart(row.entry)
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
      const agentRuns = busyRuns(agent, clock)
      runs.set(agent.key, agentRuns)
      busy.set(agent.key, agentRuns.reduce((sum, run) => sum + run.end - run.start, 0))
      const failed = [...agent.turns.flatMap((turn) => turn.rows), ...agent.loose].filter((row) => row.entry.failure_entry)
      failures.set(agent.key, failed.map((row) => entryStart(row.entry)))
    }
    /* The turn that sent each worker: the turn, on whichever agent, that holds
       the worker's door. */
    const turnOfEntry = new Map<string, number>()
    const agentOfEntry = new Map<string, number>()
    waterfall.agents.forEach((agent, index) => {
      for (const turn of agent.turns) {
        for (const row of turn.rows) {
          turnOfEntry.set(row.entry.entry_id, turn.ordinal)
          agentOfEntry.set(row.entry.entry_id, index)
        }
      }
      for (const row of agent.loose) agentOfEntry.set(row.entry.entry_id, index)
    })
    const sentFrom = new Map<string, number>()
    /* Where each worker was sent from: the agent holding its door, and when. */
    const dispatch = new Map<string, { holder: number; at: number }>()
    for (const agent of waterfall.agents) {
      const ordinal = agent.door === null ? undefined : turnOfEntry.get(agent.door.entry_id)
      if (ordinal !== undefined) sentFrom.set(agent.key, ordinal)
      const holder = agent.door === null ? undefined : agentOfEntry.get(agent.door.entry_id)
      if (agent.door !== null && holder !== undefined) dispatch.set(agent.key, { holder, at: entryStart(agent.door) })
    }
    return { nameOfScope, slowestId, runs, busy, failures, sentFrom, dispatch, figures: summarize(waterfall, shown, clock) }
  }, [shown, waterfall, clock])
  const { nameOfScope, slowestId, runs, busy, failures, sentFrom, dispatch, figures } = summary

  /* Once the reader narrows the window, a worker whose work lies wholly before
     it, or wholly after it, steps into one row for each side; the main agent
     always keeps its own. */
  const [foldOpen, setFoldOpen] = useState<ReadonlySet<FoldSide>>(new Set())
  /* A conversation's folds are its own: the island stays mounted across a
     session change, so a fold chosen in the one left behind must not hide the
     next one's rows -- a lone agent starts open, whatever was folded before
     it. The list starts at its top for the same reason. */
  useEffect(() => {
    setAgentFold(new Map())
    setOpen(new Set())
    setFoldOpen(new Set())
    setScrollTop(0)
    const el = box.current
    if (el) el.scrollTop = 0
  }, [sessionKey])
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
      return entry !== undefined && overlaps(entryStart(entry), entryEnd(entry, clock))
    })
  }, [waterfall, collapsed, open, outside, foldOpen, range, clock])
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
    /* A selection made elsewhere -- a link in the pane -- lands on the row it
       names: a pick that hides that row is dropped rather than left hiding the
       reader's destination. The pick can only be made with a pointer, so a
       reader who picked it again has not changed the selection. */
    if (phase === 'none' && !inPick(selectedId)) {
      store.setPicked(null)
      return
    }
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
    const key = owner.turn === null ? null : turnKey(group, owner.turn)
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

  /* Every entry the reader can see, in the order the page draws them, for the
     arrow keys: a picked stretch keeps its own, so the key moves through what
     is on screen rather than into rows the filter hides. Read off the drawn
     rows with everything open, because the folds outside the window reorder
     the agents: one wholly before it is drawn after the main agent, above a
     worker that started earlier. */
  const order = useMemo(() => {
    const allTurns = new Set(waterfall.agents.flatMap((agent) => agent.turns.map((turn) => turnKey(agent.key, turn.id))))
    return displayRows(waterfall, new Set(), allTurns, outside, new Set<FoldSide>(['earlier', 'later']))
      .flatMap((row) => (row.kind === 'span' && row.row !== null ? [row.row.entry.entry_id] : []))
      .filter(inPick)
  }, [waterfall, outside, inPick])

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
    for (const agent of waterfall.agents) {
      for (const turn of agent.turns) {
        if (turn.rows.some((row) => row.entry.entry_id === selected)) {
          toggleTurn(turnKey(agent.key, turn.id))
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
  const stats = statsOf(figures, waterfall.agents.length)
  /* While a replay runs, the whole session's figures: each cell keeps the room they take. */
  const wholeFigures = useMemo(() => (replaying ? summarize(whole, entries, now) : null), [replaying, whole, entries, now])
  const held = wholeFigures === null ? null : statsOf(wholeFigures, whole.agents.length)

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
    store.setPicked(null)
  }
  /* A replay is of the whole session: a narrowed window and a picked stretch
     are let go, and the list goes back to its top. Pressed again it stops,
     and the session is drawn as it is now. */
  const toggleReplay = (): void => {
    if (replayRef.current !== null) {
      replayRef.current = null
      setReplay(null)
      return
    }
    const next = startReplay(axis, entries, now, Date.now())
    if (next === null) return
    setZoom(null)
    store.setPicked(null)
    if (box.current) box.current.scrollTop = 0
    setScrollTop(0)
    replayRef.current = next
    setReplay(next)
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
      const parts = turnSegments(turn, clock)
      const openTurn = open.has(turnKey(agent.key, turn.id))
      const turnName = t('gui.trajectory.turn.ordinal', { n: turn.ordinal })
      painted.push(
        <div key={row.key} id={rowId(at)} className="trajectory-wf-row trajectory-wf-turn" data-row="turn" style={style}
          role="treeitem" aria-level={2} aria-expanded={openTurn}
          data-open={openTurn ? '' : undefined} data-failed={turn.failure ? '' : undefined}
          data-from={turn.reportedFrom === null ? undefined : turn.reportedFrom}
          aria-describedby={hoverTurn?.key === row.key ? TIP_ID : undefined}
          onPointerEnter={() => setHoverTurn({ key: row.key, at })}
          onPointerLeave={() => setHoverTurn((current) => (current?.key === row.key ? null : current))}
          onClick={() => toggleTurn(turnKey(agent.key, turn.id))}>
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
    const bar = barOf(axis, view, entryStart(entry), entryEnd(entry, clock))
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
          <span className="trajectory-wf-busy">{formatDuration(entryEnd(entry, clock) - entryStart(entry))}</span>
        </span>
        <span className="trajectory-wf-track">
          {bar ? <i className={`trajectory-wf-spanbar ${spanClass(entry)}`} style={{ left: percent(bar.left), width: percent(bar.width) }} /> : null}
        </span>
      </div>,
    )
  }

  /* A window taken on the activity strip, in real time; one that covers the
     whole axis is the whole session again. */
  const takeWindow = useCallback((next: { start: number; end: number }): void => {
    const whole = next.start <= axis.start && next.end >= axis.unproject(axis.length)
    setZoom(whole || !(next.end > next.start) ? null : { session: sessionKey, start: next.start, end: next.end })
    store.setPicked(null)
  }, [axis, sessionKey])
  const foldAll = (): void => {
    setAgentFold(new Map(waterfall.agents.map((agent) => [agent.key, false])))
    setOpen(new Set())
  }

  /* A line from the instant an open worker was sent, on its dispatcher's row,
     to where the worker's work begins: the one handoff the rows draw between
     agents rather than mark on one. Worked out when the rows or the window
     change, never per scroll frame. */
  const links = useMemo(() => {
    const span = view.to - view.from
    const rowOfAgent = new Map<number, number>()
    rows.forEach((row, index) => { if (row.kind === 'agent') rowOfAgent.set(row.agent, index) })
    const drawn: Array<{ key: string; main: boolean; x1: number; x2: number; y1: number; y2: number }> = []
    for (const [index, at] of rowOfAgent) {
      const agent = waterfall.agents[index]!
      const sent = dispatch.get(agent.key)
      const start = (runs.get(agent.key) ?? [])[0]
      const from = sent === undefined ? undefined : rowOfAgent.get(sent.holder)
      if (agent.scope === null || collapsed.has(agent.key) || sent === undefined || start === undefined || from === undefined) continue
      if (!(span > 0) || sent.at < windowFrom || sent.at > windowTo) continue
      drawn.push({
        key: agent.key,
        main: waterfall.agents[sent.holder]!.scope === null,
        x1: (axis.project(sent.at) - view.from) / span,
        x2: Math.min(1, Math.max(0, (axis.project(start.start) - view.from) / span)),
        y1: from * ROW_HEIGHT + ROW_HEIGHT / 2,
        y2: at * ROW_HEIGHT + ROW_HEIGHT / 2,
      })
    }
    return drawn
  }, [rows, waterfall, dispatch, runs, collapsed, axis, view.from, view.to, windowFrom, windowTo])

  /* The hovered turn's breakdown, just under its row and from where its bar
     starts: what its time went on, each share of the turn's length. */
  const tip = (() => {
    /* Read by the index it was hovered at, never by a walk over the rows; a
       row that has moved since (a fold above it) no longer matches. */
    const at = hoverTurn === null || rows[hoverTurn.at]?.key !== hoverTurn.key ? -1 : hoverTurn.at
    const turn = at < 0 ? null : rows[at]!.turnRow
    if (turn === null) return null
    const parts = turnBreakdown(turn, clock)
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
        {stats.map(([label, value, sub], at) => {
          const errorCell = label === t('gui.trajectory.stat.errors')
          const keep = held?.[at] ?? null
          const keepSub = keep?.[2] ?? null
          return (
            <div key={label} className="trajectory-wf-stat">
              <span className="trajectory-wf-statk">{label}</span>
              <span className="trajectory-wf-statv" data-bad={errorCell && errors > 0 ? '' : undefined}>
                <Held text={value} held={keep?.[1] ?? null} />
              </span>
              {sub === null && keepSub === null ? null : <span className="trajectory-wf-statsub"><Held text={sub} held={keepSub} /></span>}
              {errorCell && figures.firstError !== null ? (
                <button type="button" className="trajectory-wf-statsub trajectory-wf-jump" onClick={jumpToFirstError}>
                  {t('gui.trajectory.stat.jump')}
                </button>
              ) : errorCell && wholeFigures !== null && wholeFigures.firstError !== null ? (
                <span className="trajectory-wf-statsub trajectory-wf-jumphold" aria-hidden="true">{t('gui.trajectory.stat.jump')}</span>
              ) : null}
            </div>
          )
        })}
        <div className="trajectory-wf-tools">
          <button type="button" className="trajectory-wf-tool trajectory-wf-replay" data-playing={replay === null ? undefined : ''}
            disabled={replay === null && !(axis.end > axis.start)} onClick={toggleReplay}
            title={replay === null ? t('gui.trajectory.replay.hint', { seconds: REPLAY_MS / 1000 })
              : replay.pressed === null ? t('gui.trajectory.replay.catching_up', { rate: replayReadout(replay) }) : undefined}>
            <Icon icon={replay === null ? PlayIcon : StopIcon} size={13} />
            {replay === null ? t('gui.trajectory.replay.start') : t('gui.trajectory.replay.stop')}
            {replay === null ? null : <span className="trajectory-wf-readout">{replayReadout(replay)}</span>}
          </button>
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
      {waterfall.agents.length === 0 ? null : (
        <ActivityStrip axis={axis} entries={shown} start={windowFrom} end={windowTo} zoomed={zoomedView} onWindow={takeWindow} />
      )}
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
        <Ticks axis={axis} view={view} pick={pickOnView} onPick={pickFromView} ticks={marks} step={step} onWidth={setRulerWidth}
          playhead={playhead} />
      </div>
      {/* A tree, as the old list was a listbox: the box holds the focus, the
          arrows move the selection, and the selected row is its active
          descendant, so a screen reader follows the keys. */}
      <div className="trajectory-list trajectory-wf-scroll" data-thumb="keep" tabIndex={0} ref={attach}
        onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
        onKeyDown={onKeyDown}
        onDoubleClick={() => { setZoom(null); store.setPicked(null) }}
        role="tree" aria-label={t('gui.trajectory.agents')}
        aria-activedescendant={selectedAt >= first && selectedAt < last ? rowId(selectedAt) : undefined}>
        <div className="trajectory-wf-rows" role="none" style={{ height: rows.length * ROW_HEIGHT }}>
          <div className="trajectory-wf-links" aria-hidden="true">
            {marks.map((tick) => (
              <span key={`grid-${tick.time}`} className="trajectory-wf-grid" data-major={tick.major ? '' : undefined}
                style={{ left: lineLeft(tick.left) }} />
            ))}
            {playhead === null ? null : <span className="trajectory-wf-playhead" style={{ left: lineLeft(playhead) }} />}
          </div>
          {links.length === 0 ? null : (
            <svg className="trajectory-wf-dispatch" aria-hidden="true" preserveAspectRatio="none"
              viewBox={`0 0 1000 ${rows.length * ROW_HEIGHT}`} style={{ height: rows.length * ROW_HEIGHT }}>
              {links.map((link) => {
                const middle = (link.y1 + link.y2) / 2
                const x1 = (link.x1 * 1000).toFixed(2)
                const x2 = (link.x2 * 1000).toFixed(2)
                return (
                  <path key={link.key} data-main={link.main ? '' : undefined} vectorEffect="non-scaling-stroke"
                    d={`M ${x1} ${link.y1} C ${x1} ${middle}, ${x2} ${middle}, ${x2} ${link.y2}`} />
                )
              })}
            </svg>
          )}
          {links.map((link) => (
            <i key={`dot-${link.key}`} className="trajectory-wf-dispatch-dot" data-main={link.main ? '' : undefined} aria-hidden="true"
              style={{ top: link.y1, left: `calc(var(--wf-gutter) + (100% - var(--wf-gutter)) * ${link.x1.toFixed(4)})` }} />
          ))}
          {painted}
          {tip}
        </div>
      </div>
    </div>
  )
}

/** A byte count in MiB, to one decimal, the way the status line words a scan. */
export const mib = (bytes: number): string => (bytes / (1024 * 1024)).toFixed(1)
