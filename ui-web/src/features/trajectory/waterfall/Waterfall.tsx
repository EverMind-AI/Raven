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

import { useCallback, useMemo, useRef, useState, useSyncExternalStore } from 'react'

import { t } from '../../../i18n/t'
import { formatDuration } from '../../../lib/duration'
import * as lang from '../../../state/lang'
import * as details from '../detailStore'
import { entryEnd, entryStart, buildWaterfall } from './model'
import { barOf, createAxis } from './axis'
import { crossLinks } from './links'
import { turnSegments } from './segments'
import { scopeTitle } from '../scopes'
import * as store from '../store'

import type { Waterfall, WaterfallRow, WaterfallTurn } from './model'
import type { TimeAxis, Viewport } from './axis'
import type { JSX } from 'react'

export const ROW_HEIGHT = 28
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

/** The agent key both the collapse set and the DOM use; the host has no scope id. */
export const agentKey = (scope: string | null): string => scope ?? ''

export function displayRows(
  waterfall: Waterfall, collapsedAgents: ReadonlySet<string>, openTurns: ReadonlySet<string>,
): DisplayRow[] {
  const rows: DisplayRow[] = []
  waterfall.agents.forEach((agent, at) => {
    rows.push({ kind: 'agent', key: `agent:${agentKey(agent.scope)}`, agent: at, turn: null, row: null, turnRow: null })
    if (collapsedAgents.has(agentKey(agent.scope))) return
    for (const turn of agent.turns) {
      rows.push({ kind: 'turn', key: `turn:${turn.id}`, agent: at, turn: turn.id, row: null, turnRow: turn })
      if (!openTurns.has(turn.id)) continue
      turn.rows.forEach((row, at) => rows.push({ kind: 'span', key: `span:${row.entry.entry_id}:${at}`, agent: at, turn: turn.id, row, turnRow: turn }))
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

/** Every stretch any agent was doing anything, which is what the axis must not compress. */
function intervalsOf(waterfall: Waterfall, now: number): Array<{ start: number; end: number }> {
  const spans: Array<{ start: number; end: number }> = []
  for (const agent of waterfall.agents) {
    for (const turn of agent.turns) {
      for (const row of turn.rows) spans.push({ start: entryStart(row.entry), end: entryEnd(row.entry, now) })
    }
    for (const row of agent.loose) spans.push({ start: entryStart(row.entry), end: entryEnd(row.entry, now) })
  }
  return spans
}

const percent = (fraction: number): string => `${(fraction * 100).toFixed(4)}%`

function Ticks({ axis, view }: { axis: TimeAxis; view: Viewport }): JSX.Element {
  const span = view.to - view.from
  const step = [1000, 2000, 5000, 10_000, 15_000, 30_000, 60_000, 120_000, 300_000, 600_000, 1_800_000, 3_600_000]
    .find((candidate) => (candidate / span) * 100 >= 0.08) ?? 3_600_000
  const ticks: JSX.Element[] = []
  const first = Math.ceil(axis.unproject(view.from) / step) * step
  for (let time = first; time <= axis.unproject(view.to); time += step) {
    const at = (axis.project(time) - view.from) / span
    if (at < 0 || at > 1) continue
    ticks.push(<b key={time} style={{ left: percent(at) }}>{formatDuration(time - axis.start)}</b>)
  }
  return <div className="trajectory-ruler" aria-hidden="true">{ticks}</div>
}

export function Waterfall(): JSX.Element {
  useSyncExternalStore(store.subscribe, store.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set())
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set())
  const [scrollTop, setScrollTop] = useState(0)
  const [height, setHeight] = useState(0)
  const observer = useRef<ResizeObserver | null>(null)

  const now = Date.now()
  const shown = store.get().shown
  const waterfall = useMemo(() => buildWaterfall(shown, now), [shown, now])
  const axis = useMemo(() => createAxis(intervalsOf(waterfall, now)), [waterfall, now])
  const view: Viewport = { from: 0, to: axis.length }
  const rows = useMemo(() => displayRows(waterfall, collapsed, open), [waterfall, collapsed, open])
  const links = useMemo(() => crossLinks(rows, waterfall), [rows, waterfall])
  const { first, last } = windowOf(scrollTop, height, rows.length)

  const attach = useCallback((el: HTMLDivElement | null): void => {
    observer.current?.disconnect()
    observer.current = null
    if (!el) return
    const measure = (): void => { setHeight(el.clientHeight) }
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

  const pick = (row: WaterfallRow): void => {
    store.selectAcross(row.entry.entry_id, { source: 'click' })
    details.openDetails()
  }

  const painted: JSX.Element[] = []
  for (let at = first; at < last; at++) {
    const row = rows[at]!
    const agent = waterfall.agents[row.agent]!
    const style = { top: at * ROW_HEIGHT, height: ROW_HEIGHT }
    if (row.kind === 'agent') {
      const bar = barOf(axis, view, agent.start, agent.end)
      painted.push(
        <div key={row.key} className="trajectory-wf-row trajectory-wf-agent" data-row="agent" style={style}
          onClick={() => toggleAgent(agentKey(agent.scope))}>
          <span className="trajectory-wf-label" style={{ paddingLeft: 4 }}>
            <span className="trajectory-wf-dot" />
            {agent.scope === null ? t('gui.trajectory.scope.main') : scopeTitle(agent.door)}
          </span>
          <span className="trajectory-wf-track">
            {bar ? <i className="trajectory-wf-agentbar" style={{ left: percent(bar.left), width: percent(bar.width) }} /> : null}
          </span>
        </div>,
      )
      continue
    }
    if (row.kind === 'turn') {
      const turn = row.turnRow!
      const parts = turnSegments(turn, now)
      painted.push(
        <div key={row.key} className="trajectory-wf-row trajectory-wf-turn" data-row="turn" style={style}
          data-from={turn.reportedFrom === null ? undefined : turn.reportedFrom}
          onClick={() => toggleTurn(turn.id)}>
          <span className="trajectory-wf-label" style={{ paddingLeft: 18 }}>
            {t('gui.trajectory.turn.ordinal', { n: turn.ordinal })}
            {turn.reportedFrom === null ? null : <span className="trajectory-wf-from">{t('gui.trajectory.turn.from_report', { name: turn.reportedFrom })}</span>}
          </span>
          <span className="trajectory-wf-track">
            {parts.map((part, index) => {
              const at = barOf(axis, view, part.start, part.end)
              return at ? <i key={index} className={`trajectory-wf-seg trajectory-wf-seg-${part.kind}`} style={{ left: percent(at.left), width: percent(at.width) }} /> : null
            })}
          </span>
        </div>,
      )
      continue
    }
    const entry = row.row!.entry
    const span = row.turn === null ? null : row.turnRow!
    const start = entryStart(entry)
    const end = span === null ? entryEnd(entry, now) : Math.min(entryEnd(entry, now), span.end)
    const bar = barOf(axis, view, start, end)
    painted.push(
      <div key={row.key} className="trajectory-wf-row trajectory-wf-span" data-row="span" data-entry={entry.entry_id}
        style={style} onClick={() => pick(row.row!)}>
        <span className="trajectory-wf-label" style={{ paddingLeft: 34 + row.row!.depth * 12 }}>
          {entry.preview ?? entry.span_name}
        </span>
        <span className="trajectory-wf-track">
          {bar ? <i className={`trajectory-wf-spanbar trajectory-wf-kind-${entry.kind.replace(/\./g, '-')}`} style={{ left: percent(bar.left), width: percent(bar.width) }} /> : null}
        </span>
      </div>,
    )
  }

  return (
    <div className="trajectory-wf">
      <Ticks axis={axis} view={view} />
      <div className="trajectory-wf-scroll" ref={attach} onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
        role="list" aria-label={t('gui.trajectory.agents')}>
        <div className="trajectory-wf-rows" style={{ height: rows.length * ROW_HEIGHT }}>
          <div className="trajectory-wf-links">
            {links.map((link, index) => {
              const bar = barOf(axis, view, link.at, link.at)
              if (bar === null) return null
              const top = Math.min(link.fromRow, link.toRow) * ROW_HEIGHT
              const height = (Math.abs(link.toRow - link.fromRow) + 1) * ROW_HEIGHT
              return <span key={index} className={`trajectory-wf-link trajectory-wf-link-${link.kind}`}
                style={{ left: percent(bar.left), top, height }} />
            })}
          </div>
          {painted}
        </div>
      </div>
    </div>
  )
}
