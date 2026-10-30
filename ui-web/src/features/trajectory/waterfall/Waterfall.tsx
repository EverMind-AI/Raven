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

import { ArrowDown01Icon, ArrowRight01Icon, BotIcon, Message01Icon, SparklesIcon, UserGroupIcon, Wrench01Icon } from '@hugeicons/core-free-icons'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'

import { AgentMark, agentMarkPath } from '../../../components/AgentMark'
import { Icon } from '../../../components/Icon'
import { t } from '../../../i18n/t'
import { formatDuration } from '../../../lib/duration'
import * as lang from '../../../state/lang'
import { showBar } from '../../../state/scrollbars'
import * as details from '../detailStore'
import { enter as enterScope, scopeTitle } from '../scopes'
import * as store from '../store'
import { AxisIndex, TRAIL_PAD, barOf, clampViewport, formatTick, tickMarks, zoomViewport } from './axis'
import { WaterfallIndex } from './incremental'
import { entryEnd, entryStart, isLive, turnKey } from './model'
import { OverviewLanes } from './OverviewLanes'
import { turnSegments } from './segments'

import type { TrajectoryEntry } from '../types'
import type { TimeAxis, Viewport } from './axis'
import type { Waterfall, WaterfallAgent, WaterfallRow, WaterfallTurn } from './model'
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

export interface DisplayRow {
  kind: 'agent' | 'turn' | 'span'
  key: string
  agent: number
  turn: string | null
  row: WaterfallRow | null
  turnRow: WaterfallTurn | null
}

export function displayRows(
  waterfall: Waterfall, collapsedAgents: ReadonlySet<string>, openTurns: ReadonlySet<string>,
): DisplayRow[] {
  const rows: DisplayRow[] = []
  waterfall.agents.forEach((agent, at) => {
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
  })
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

/* The duration stands just past the bar it belongs to; a bar that runs into
   the track's end would push it out, so it stops where the label still fits.
   A row whose bar is off the visible stretch keeps the label at the edge. */
const TIME_RESERVE = 104
const timeStyle = (bar: { left: number; width: number } | null): { left?: string; right?: number } =>
  bar === null
    ? { right: 12 }
    : { left: `min(calc(${percent(bar.left + bar.width)} + 8px), calc(100% - ${TIME_RESERVE}px))` }

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

/** The line icon a row wears: the reference draws a real icon in its box, not a
 *  text glyph, so it fills the box instead of sitting small inside it. */
function kindIcon(kind: string): typeof SparklesIcon {
  if (kind.startsWith('llm.')) return SparklesIcon
  if (kind.startsWith('tool.')) return Wrench01Icon
  if (kind === 'user.input') return Message01Icon
  if (kind.startsWith('subagent')) return UserGroupIcon
  return BotIcon
}

/** One of four fixed classes; the index's kind strings never reach a class name.
 *  A failed operation's bar is the failure's colour, whatever kind it was. */
const spanClass = (entry: TrajectoryEntry): string =>
  entry.failure_entry ? 'trajectory-wf-kind-failure'
    : entry.kind.startsWith('tool.') ? 'trajectory-wf-kind-tool'
      : entry.kind === 'user.input' ? 'trajectory-wf-kind-user' : 'trajectory-wf-kind-model'

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

function Ticks({ axis, view, pick, onPick, count, onWidth }: {
  axis: TimeAxis; view: Viewport; pick: Pick | null; onPick: (pick: Pick | null) => void
  count: number; onWidth: (width: number) => void
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
  const ticks = useMemo(() => tickMarks(axis, { from, to }, count), [axis, from, to, count])
  const step = ticks.length > 1 ? ticks[1]!.time - ticks[0]!.time : 1
  const span = to - from
  const gaps = span > 0 ? axis.gaps : []
  return (
    <div className="trajectory-ruler" aria-hidden="true" ref={strip}
      onPointerDown={down} onPointerMove={move} onPointerUp={up} onPointerCancel={cancel}>
      {ticks.map((tick, index) => (
        /* The line under each label, drawn through the ruler the way the
           network timeline does; the last one keeps its pixel inside the
           ruler's own edge. */
        <span key={`tick-${tick.left}`} className="trajectory-ruler-tick"
          style={{ left: index === ticks.length - 1 ? 'calc(100% - 1px)' : percent(tick.left) }} />
      ))}
      {ticks.map((tick, index) => {
        /* The axis's left edge is the start of the session, and its label
           would hang off the ruler anyway: the number is not written there.
           A zoomed view starts elsewhere, and that one is written flush with
           the edge instead. */
        if (index === 0) return tick.time <= 0 ? null : <b key={tick.left} data-first="" style={{ left: 0 }}>{formatTick(tick.time, step)}</b>
        return <b key={tick.left} style={{ left: percent(tick.left) }}>{formatTick(tick.time, step)}</b>
      })}
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
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set())
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set())
  const [scrollTop, setScrollTop] = useState(0)
  const [rulerWidth, setRulerWidth] = useState(0)
  const [height, setHeight] = useState(0)
  /* The reader's window and picked stretch, both in real time and both owned
     by the conversation they were made on. A live session's axis grows with
     every step that lands past its end, and a window kept in the axis's own
     positions (or as fractions of the view) went stale with each one -- so it
     was dropped, every few seconds, while the session the reader was watching
     ran. In milliseconds they mean the same stretch on every axis of the same
     conversation, and only a change of conversation, or the reader, drops them. */
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
      const rect = scroller.getBoundingClientRect()
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
  const live = shown.some((entry) => isLive(entry, now))
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
  const rows = useMemo(() => {
    const drawn = displayRows(waterfall, collapsed, open)
    if (range === null) return drawn
    /* Only what was running inside the picked stretch stays: an agent, a turn
       and a row are in it when their own stretch of time meets it. */
    const overlaps = (start: number, end: number): boolean => end >= range.start && start <= range.end
    return drawn.filter((row) => {
      if (row.kind === 'agent') {
        const agent = waterfall.agents[row.agent]
        return agent !== undefined && overlaps(agent.start, agent.end)
      }
      if (row.kind === 'turn') return row.turnRow !== null && overlaps(row.turnRow.start, row.turnRow.end)
      const entry = row.row?.entry
      return entry !== undefined && overlaps(entryStart(entry), entryEnd(entry, now))
    })
  }, [waterfall, collapsed, open, range, now])
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
    let owner: { agent: number; turn: string } | null = null
    for (const [index, agent] of waterfall.agents.entries()) {
      for (const turn of agent.turns) {
        if (turn.rows.some((row) => row.entry.entry_id === selectedId)) owner = { agent: index, turn: turn.id }
      }
    }
    if (owner === null) return
    const group = waterfall.agents[owner.agent]!.key
    if (phase === 'none' && collapsed.has(group)) {
      setCollapsed((previous) => {
        const next = new Set(previous)
        next.delete(group)
        return next
      })
      return
    }
    const key = turnKey(owner.agent, owner.turn)
    if (phase === 'none' && !open.has(key)) {
      setOpen((previous) => new Set(previous).add(key))
      return
    }
    const at = rows.findIndex((row) => row.kind === 'span' && row.row?.entry.entry_id === selectedId)
    const scroller = box.current
    if (at < 0 || scroller === null) return
    const top = at * ROW_HEIGHT
    if (top < scroller.scrollTop || top + ROW_HEIGHT > scroller.scrollTop + scroller.clientHeight) scroller.scrollTop = top
    revealed.current = { id: selectedId, tick: selections, phase: 'done' }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, selections, rows, open, collapsed])

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
    setCollapsed((previous) => {
      const next = new Set(previous)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
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
    /* A key pressed on a control inside a row -- a fold button, a door's
       arrow -- is that control's: Enter there must not also act on the row
       that holds the selection. */
    if (event.target !== event.currentTarget) return
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

  /* A scope id is `trace:span`; the badge must say who the worker is. */
  const nameOfScope = new Map<string, string>()
  for (const agent of waterfall.agents) {
    if (agent.scope === null) continue
    for (const scope of agent.scopes) if (scope !== null) nameOfScope.set(scope, scopeTitle(agent.door))
  }

  /* The header's numbers, from the rows the page draws. */
  const countOf = (prefix: string): number => shown.filter((entry) => entry.kind.startsWith(prefix)).length
  const errors = shown.filter((entry) => entry.failure_entry).length
  const turnCount = waterfall.agents.reduce((total, agent) => total + agent.turns.length, 0)
  const sessionStart = waterfall.agents.length ? Math.min(...waterfall.agents.map((agent) => agent.start)) : 0
  const sessionEnd = waterfall.agents.length ? Math.max(...waterfall.agents.map((agent) => agent.end)) : 0
  const stats: Array<[string, string]> = [
    [t('gui.trajectory.stat.duration'), formatDuration(sessionEnd - sessionStart)],
    [t('gui.trajectory.stat.agents'), String(waterfall.agents.length)],
    [t('gui.trajectory.stat.turns'), String(turnCount)],
    [t('gui.trajectory.stat.llm'), String(countOf('llm.'))],
    [t('gui.trajectory.stat.tool'), String(countOf('tool.'))],
    [t('gui.trajectory.stat.errors'), String(errors)],
  ]
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

  const painted: JSX.Element[] = []
  for (let at = first; at < last; at++) {
    const row = rows[at]!
    const agent = waterfall.agents[row.agent]!
    const style = { top: at * ROW_HEIGHT, height: ROW_HEIGHT }
    if (row.kind === 'agent') {
      const bar = barOf(axis, view, agent.start, agent.end)
      const agentOpen = !collapsed.has(agent.key)
      const agentName = agent.scope === null ? t('gui.trajectory.scope.main') : scopeTitle(agent.door)
      painted.push(
        <div key={row.key} id={rowId(at)} className="trajectory-wf-row trajectory-wf-agent" data-row="agent" style={style}
          role="treeitem" aria-level={1} aria-expanded={agentOpen}
          onClick={() => toggleAgent(agent.key)}>
          <span className="trajectory-wf-label" style={{ paddingLeft: 12 }}>
            {/* The fold control is a button, so the keyboard reaches it; its
                click is the row's, which does the folding. */}
            <button type="button" className="trajectory-wf-chev" aria-expanded={agentOpen} aria-label={agentName}>
              <Icon icon={agentOpen ? ArrowDown01Icon : ArrowRight01Icon} size={14} />
            </button>
            <span className="trajectory-wf-tile" data-tone={row.agent % 4}>
              {agent.scope === null || ownOf(agent)
                ? <AgentMark own />
                : presetOf(agent) === null ? <span className="trajectory-wf-glyph"><Icon icon={BotIcon} size={15} /></span> : <AgentMark preset={presetOf(agent)} />}
            </span>
            <span className="trajectory-wf-name">{agentName}</span>
            <span className="trajectory-wf-sub">{agentModel(agent)}</span>
          </span>
          <span className="trajectory-wf-track">
            {bar ? <i className="trajectory-wf-agentbar" style={{ left: percent(bar.left), width: percent(bar.width) }} /> : null}
            <span className="trajectory-wf-time" style={timeStyle(bar)}>{formatDuration(agent.end - agent.start)}</span>
          </span>
        </div>,
      )
      continue
    }
    if (row.kind === 'turn') {
      const turn = row.turnRow!
      const parts = turnSegments(turn, now)
      const turnBar = barOf(axis, view, turn.start, turn.end)
      const openTurn = open.has(turnKey(row.agent, turn.id))
      const turnName = t('gui.trajectory.turn.ordinal', { n: turn.ordinal })
      painted.push(
        <div key={row.key} id={rowId(at)} className="trajectory-wf-row trajectory-wf-turn" data-row="turn" style={style}
          role="treeitem" aria-level={2} aria-expanded={openTurn}
          data-open={openTurn ? '' : undefined} data-failed={turn.failure ? '' : undefined}
          data-from={turn.reportedFrom === null ? undefined : turn.reportedFrom}
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
          </span>
          <span className="trajectory-wf-track">
            {parts.map((part, index) => {
              const slice = barOf(axis, view, part.start, part.end)
              return slice ? <i key={index} className={`trajectory-wf-seg trajectory-wf-seg-${part.kind}`} style={{ left: percent(slice.left), width: percent(slice.width) }} /> : null
            })}
            <span className="trajectory-wf-time" style={timeStyle(turnBar)}>{formatDuration(turn.end - turn.start)}</span>
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
        <span className="trajectory-wf-label" style={{ paddingLeft: 44 + row.row!.depth * 20 }}>
          <span className="trajectory-wf-ico"><Icon icon={kindIcon(entry.kind)} size={15} /></span>
          <span className="trajectory-wf-name">{entry.preview ?? entry.span_name}</span>
          {entry.failure_entry ? <FailMark /> : null}
          <span className="trajectory-wf-sub">{entry.span_name}</span>
          {entry.entry_id === slowestId ? <span className="trajectory-wf-slow">{t('gui.trajectory.slowest')}</span> : null}
          {entry.opens_subagent_scope === null || entry.opens_subagent_scope === undefined ? null : (
            <button className="trajectory-scope-opens" aria-label={t('gui.trajectory.scope.opens')}
              title={t('gui.trajectory.scope.opens')}
              onClick={(event) => { event.stopPropagation(); locate(entry) }}>{'\u2197'}</button>
          )}
        </span>
        <span className="trajectory-wf-track">
          {bar ? <i className={`trajectory-wf-spanbar ${spanClass(entry)}`} style={{ left: percent(bar.left), width: percent(bar.width) }} /> : null}
          <span className="trajectory-wf-time" style={timeStyle(bar)}>{formatDuration(entryEnd(entry, now) - entryStart(entry))}</span>
        </span>
      </div>,
    )
  }

  /* One label per ~150px keeps them from colliding when the details pane
     narrows the column. The rows draw a line at every label and one between,
     so the grid keeps a 1:2 rhythm with the ruler at any width or zoom. */
  const tickCount = Math.max(3, Math.min(8, Math.floor((rulerWidth > 0 ? rulerWidth : 1200) / 150)))

  return (
    <div className="trajectory-wf" ref={island}>
      <div className="trajectory-wf-stats">
        {stats.map(([label, value]) => (
          <div key={label} className="trajectory-wf-stat">
            <span className="trajectory-wf-statk">{label}</span>
            <span className="trajectory-wf-statv" data-bad={label === t('gui.trajectory.stat.errors') && errors > 0 ? '' : undefined}>{value}</span>
          </div>
        ))}
      </div>
      <OverviewLanes waterfall={waterfall} axis={axis} viewport={view} />
      <div className="trajectory-wf-head">
        <Ticks axis={axis} view={view} pick={pickOnView} onPick={pickFromView} count={tickCount} onWidth={setRulerWidth} />
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
            {tickMarks(axis, view, tickCount * 2).map((tick, index, marks) => (
              /* The last line keeps its pixel inside the track, the way the
                 ruler's does: a 1px box on the right edge makes the scroll
                 box wider than itself, and the overlay scrollbars answer that
                 with a horizontal bar nobody can use. */
              <span key={`grid-${tick.left}`} className="trajectory-wf-grid"
                style={{ left: index === marks.length - 1 ? 'calc(100% - 1px)' : percent(tick.left) }} />
            ))}
          </div>
          {painted}
        </div>
      </div>
    </div>
  )
}

/** Rupees of MiB, as the status line words a scan. */
export const mib = (bytes: number): string => (bytes / (1024 * 1024)).toFixed(1)
