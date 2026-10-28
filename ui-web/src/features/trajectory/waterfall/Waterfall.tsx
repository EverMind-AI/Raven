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

import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'

import { AgentMark, agentMarkPath } from '../../../components/AgentMark'
import { t } from '../../../i18n/t'
import { formatDuration } from '../../../lib/duration'
import * as lang from '../../../state/lang'
import * as details from '../detailStore'
import { openFromEntry, scopeTitle } from '../scopes'
import * as store from '../store'
import { barOf, clampViewport, createAxis, tickMarks, zoomViewport } from './axis'
import { crossLinks } from './links'
import { agentKey, buildWaterfall, entryEnd, entryStart, isLive, turnKey } from './model'
import { OverviewLanes } from './OverviewLanes'
import { turnSegments } from './segments'

import type { TimeAxis, Viewport } from './axis'
import type { Waterfall, WaterfallAgent, WaterfallRow, WaterfallTurn } from './model'
import type { JSX, KeyboardEvent as ReactKeyboardEvent } from 'react'

export const ROW_HEIGHT = 48
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
    rows.push({ kind: 'agent', key: `agent:${agentKey(agent.scope)}`, agent: at, turn: null, row: null, turnRow: null })
    if (collapsedAgents.has(agentKey(agent.scope))) return
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

/** A glyph per family, the way the reference labels its tools. */
function icoOf(kind: string): string {
  if (kind.startsWith('llm.')) return '\u2726'
  if (kind.startsWith('tool.')) return '\u2699'
  if (kind === 'user.input') return '\u25a4'
  if (kind.startsWith('subagent')) return '\u27d0'
  return '\u00b7'
}

/** One of three fixed classes; the index's kind strings never reach a class name. */
const spanClass = (kind: string): string =>
  kind.startsWith('tool.') ? 'trajectory-wf-kind-tool'
    : kind === 'user.input' ? 'trajectory-wf-kind-user' : 'trajectory-wf-kind-model'

function Ticks({ axis, view }: { axis: TimeAxis; view: Viewport }): JSX.Element {
  const { from, to } = view
  const ticks = useMemo(() => tickMarks(axis, { from, to }), [axis, from, to])
  const span = to - from
  const gaps = span > 0 ? axis.gaps : []
  return (
    <div className="trajectory-ruler" aria-hidden="true">
      {ticks.map((tick) => (
        <b key={tick.left} style={{ left: percent(tick.left) }}>{formatDuration(tick.time)}</b>
      ))}
      {gaps.map((gap, index) => {
        const left = (gap.from - view.from) / span
        const right = (gap.to - view.from) / span
        const from = Math.max(0, left)
        const to = Math.min(1, right)
        if (to <= from) return null
        return (
          <span key={index} className="trajectory-ruler-idle"
            style={{ left: percent(from), width: percent(to - from) }}>
            {t('gui.trajectory.axis.idle', { duration: formatDuration(gap.end - gap.start) })}
          </span>
        )
      })}
    </div>
  )
}

export function Waterfall(): JSX.Element {
  useSyncExternalStore(store.subscribe, store.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  const [collapsed, setCollapsed] = useState<ReadonlySet<string>>(new Set())
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set())
  const [scrollTop, setScrollTop] = useState(0)
  const [height, setHeight] = useState(0)
  const [zoom, setZoom] = useState<{ axis: number; view: Viewport } | null>(null)
  const observer = useRef<ResizeObserver | null>(null)
  const box = useRef<HTMLDivElement | null>(null)

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
  useEffect(() => {
    const el = box.current
    if (!el) return
    const onWheel = (event: WheelEvent): void => {
      event.preventDefault()
      const rect = el.getBoundingClientRect()
      const at = rect.width > 0 ? (event.clientX - rect.left) / rect.width : 0.5
      const current = viewRef.current
      const span = current.to - current.from
      /* Keep the ref current as we go: a trackpad delivers several wheel events
         per frame, and each must build on the one before it. */
      if (event.shiftKey) {
        const shift = (span * event.deltaY) / Math.max(1, rect.width)
        const view = clampViewport({ from: current.from + shift, to: current.to + shift }, fullRef.current)
        viewRef.current = view
        setZoom({ axis: fullRef.current, view })
        return
      }
      const anchor = current.from + span * Math.min(1, Math.max(0, at))
      const factor = Math.max(0.5, Math.min(2, Math.exp(-event.deltaY * 0.0015)))
      const view = zoomViewport(current, factor, anchor, fullRef.current)
      viewRef.current = view
      setZoom({ axis: fullRef.current, view })
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
     
  }, [])

  /* The clock the bars are cut at. It ticks only while something is still
     running: a fresh `Date.now()` every render would rebuild the whole model
     on every scroll frame, which is what a 500-turn session cannot afford. */
  const shown = store.get().shown
  const prefs = store.get().prefs
  const [now, setNow] = useState(() => Date.now())
  const live = shown.some((entry) => isLive(entry, now))
  useEffect(() => {
    if (!live) return
    const timer = setInterval(() => setNow(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [live])
  const waterfall = useMemo(() => buildWaterfall(shown, now), [shown, now])
  const axis = useMemo(() => createAxis(intervalsOf(waterfall, now)), [waterfall, now])
  /* A window belongs to the axis it was cut from; a different conversation has
     a different length, and the old window means nothing on it. */
  const view: Viewport = zoom !== null && zoom.axis === axis.length && zoom.view.to > zoom.view.from
    ? zoom.view
    : { from: 0, to: axis.length }
  viewRef.current = view
  fullRef.current = axis.length
  /* A different conversation has a different axis; the old window means nothing. */
   
  useEffect(() => { setZoom(null) }, [axis.length])
  const rows = useMemo(() => displayRows(waterfall, collapsed, open), [waterfall, collapsed, open])
  const links = useMemo(() => crossLinks(rows, waterfall), [rows, waterfall])
  const { first, last } = windowOf(scrollTop, height, rows.length)

  /* A selection made elsewhere -- a link in the pane, a tab's locate button --
     opens the turn that holds the row and brings the row into view, so a jump
     lands somewhere the reader can see. */
  const selectedId = store.get().selectedId
  const selections = store.get().selections
  useEffect(() => {
    if (selectedId === null) return
    let owner: { agent: number; turn: string } | null = null
    for (const [index, agent] of waterfall.agents.entries()) {
      for (const turn of agent.turns) {
        if (turn.rows.some((row) => row.entry.entry_id === selectedId)) owner = { agent: index, turn: turn.id }
      }
    }
    if (owner === null) return
    const group = agentKey(waterfall.agents[owner.agent]!.scope)
    if (collapsed.has(group)) {
      setCollapsed((previous) => {
        const next = new Set(previous)
        next.delete(group)
        return next
      })
      return
    }
    const key = turnKey(owner.agent, owner.turn)
    if (!open.has(key)) {
      setOpen((previous) => new Set(previous).add(key))
      return
    }
    const at = rows.findIndex((row) => row.kind === 'span' && row.row?.entry.entry_id === selectedId)
    const scroller = box.current
    if (at < 0 || scroller === null) return
    const top = at * ROW_HEIGHT
    if (top < scroller.scrollTop || top + ROW_HEIGHT > scroller.scrollTop + scroller.clientHeight) scroller.scrollTop = top
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId, selections, rows, open, collapsed])

  const attach = useCallback((el: HTMLDivElement | null): void => {
    box.current = el
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

  /* Every entry in the order the page draws them, for the arrow keys. */
  const order = useMemo(
    () => waterfall.agents.flatMap((agent) => [
      ...agent.turns.flatMap((turn) => turn.rows.map((row) => row.entry.entry_id)),
      ...agent.loose.map((row) => row.entry.entry_id),
    ]),
    [waterfall],
  )

  const onKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>): void => {
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
      openFromEntry(entry)
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

  const pick = (row: WaterfallRow): void => {
    store.selectAcross(row.entry.entry_id, { source: 'click' })
    details.openDetails()
  }

  /* A scope id is `trace:span`; the badge must say who the worker is. */
  const nameOfScope = new Map<string, string>()
  for (const agent of waterfall.agents) {
    if (agent.scope !== null) nameOfScope.set(agent.scope, scopeTitle(agent.door))
  }

  /* The header's numbers, from the rows the page draws. */
  const countOf = (prefix: string): number => shown.filter((entry) => entry.kind.startsWith(prefix)).length
  const errors = shown.filter((entry) => entry.failure_entry || entry.operation_status === 'error').length
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
  const nowLine = sessionEnd > sessionStart ? barOf(axis, view, sessionEnd, sessionEnd) : null

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
          <span className="trajectory-wf-label" style={{ paddingLeft: 12, ['--indent' as string]: '12px' }}>
            <span className="trajectory-wf-chev">{collapsed.has(agentKey(agent.scope)) ? '\u25b8' : '\u25be'}</span>
            <span className="trajectory-wf-tile" data-tone={row.agent % 4}>
              {agent.scope === null
                ? <AgentMark own />
                : presetOf(agent) === null ? <span className="trajectory-wf-glyph">{'\u2726'}</span> : <AgentMark preset={presetOf(agent)} />}
            </span>
            <span className="trajectory-wf-name">{agent.scope === null ? t('gui.trajectory.scope.main') : scopeTitle(agent.door)}</span>
            <span className="trajectory-wf-sub">{agentModel(agent)}</span>
          </span>
          <span className="trajectory-wf-track">
            {bar ? <i className="trajectory-wf-agentbar" style={{ left: percent(bar.left), width: percent(bar.width) }} /> : null}
            {bar ? <span className="trajectory-wf-cap" style={{ left: `calc(${percent(bar.left + bar.width)} + 8px)` }}>{formatDuration(agent.end - agent.start)}</span> : null}
          </span>
        </div>,
      )
      continue
    }
    if (row.kind === 'turn') {
      const turn = row.turnRow!
      const parts = turnSegments(turn, now)
      const openTurn = open.has(turnKey(row.agent, turn.id))
      painted.push(
        <div key={row.key} className="trajectory-wf-row trajectory-wf-turn" data-row="turn" style={style}
          data-open={openTurn ? '' : undefined}
          data-from={turn.reportedFrom === null ? undefined : turn.reportedFrom}
          onClick={() => toggleTurn(turnKey(row.agent, turn.id))}>
          <span className="trajectory-wf-label" style={{ paddingLeft: 26, ['--indent' as string]: '26px' }}>
            <span className="trajectory-wf-chev">{openTurn ? '\u25be' : '\u25b8'}</span>
            <span className="trajectory-wf-name">{t('gui.trajectory.turn.ordinal', { n: turn.ordinal })}</span>
            {turn.preview === null ? null : <span className="trajectory-wf-sub">{turn.preview}</span>}
            {turn.reportedFrom === null ? null : <span className="trajectory-wf-from">{t('gui.trajectory.turn.from_report', { name: nameOfScope.get(turn.reportedFrom) ?? turn.reportedFrom })}</span>}
          </span>
          <span className="trajectory-wf-track">
            {parts.map((part, index) => {
              const slice = barOf(axis, view, part.start, part.end)
              return slice ? <i key={index} className={`trajectory-wf-seg trajectory-wf-seg-${part.kind}`} style={{ left: percent(slice.left), width: percent(slice.width) }} /> : null
            })}
            {(() => {
              const whole = barOf(axis, view, turn.start, turn.end)
              return whole
                ? <span className="trajectory-wf-cap" style={{ left: `calc(${percent(whole.left + whole.width)} + 8px)` }}>{formatDuration(turn.end - turn.start)}</span>
                : null
            })()}
          </span>
        </div>,
      )
      continue
    }
    const entry = row.row!.entry
    const bar = barOf(axis, view, entryStart(entry), entryEnd(entry, now))
    painted.push(
      <div key={row.key} className="trajectory-wf-row trajectory-wf-span" data-row="span" data-entry={entry.entry_id}
        style={style} onClick={() => pick(row.row!)}>
        <span className="trajectory-wf-label" style={{ paddingLeft: 44 + row.row!.depth * 20, ['--indent' as string]: `${44 + row.row!.depth * 20}px` }}>
          <span className="trajectory-wf-ico">{icoOf(entry.kind)}</span>
          <span className="trajectory-wf-name">{entry.preview ?? entry.span_name}</span>
          <span className="trajectory-wf-sub">{entry.span_name}</span>
          {entry.entry_id === slowestId ? <span className="trajectory-wf-slow">{t('gui.trajectory.slowest')}</span> : null}
        </span>
        <span className="trajectory-wf-track">
          {bar ? <i className={`trajectory-wf-spanbar ${spanClass(entry.kind)}`} style={{ left: percent(bar.left), width: percent(bar.width) }} /> : null}
          {bar ? <span className="trajectory-wf-cap" style={{ left: `calc(${percent(bar.left + bar.width)} + 8px)` }}>{formatDuration(entryEnd(entry, now) - entryStart(entry))}</span> : null}
          {entry.opens_subagent_scope === null || entry.opens_subagent_scope === undefined ? null : (
            <button className="trajectory-scope-opens" aria-label={t('gui.trajectory.scope.opens')}
              title={t('gui.trajectory.scope.opens')}
              onClick={(event) => { event.stopPropagation(); openFromEntry(entry) }}>{'\u2197'}</button>
          )}
        </span>
      </div>,
    )
  }

  return (
    <div className="trajectory-wf">
      <div className="trajectory-wf-stats">
        {stats.map(([label, value]) => (
          <div key={label} className="trajectory-wf-stat">
            <span className="trajectory-wf-statk">{label}</span>
            <span className="trajectory-wf-statv" data-bad={label === t('gui.trajectory.stat.errors') && errors > 0 ? '' : undefined}>{value}</span>
          </div>
        ))}
        <div className="trajectory-wf-tools">
          <button className="trajectory-wf-tool" data-testid="show-hidden" aria-pressed={prefs.showHidden}
            title={t('gui.trajectory.bar.show_hidden', { n: store.get().hiddenCount })}
            onClick={() => store.setPrefs({ showHidden: !prefs.showHidden })}>
            {t('gui.trajectory.bar.show_hidden', { n: store.get().hiddenCount })}
          </button>
          <button className="trajectory-wf-tool" aria-label={t('gui.trajectory.bar.reset')} disabled={zoom === null}
            onClick={() => setZoom(null)}>{'\u2922'}</button>
        </div>
      </div>
      <OverviewLanes waterfall={waterfall} axis={axis} viewport={view} />
      <div className="trajectory-wf-head">
        <span className="trajectory-wf-spanhead">{t('gui.trajectory.span')}</span>
        <Ticks axis={axis} view={view} />
      </div>
      <div className="trajectory-list trajectory-wf-scroll" tabIndex={-1} ref={attach}
        onScroll={(e) => setScrollTop(e.currentTarget.scrollTop)}
        onKeyDown={onKeyDown}
        onDoubleClick={() => setZoom(null)}
        role="list" aria-label={t('gui.trajectory.agents')}>
        <div className="trajectory-wf-rows" style={{ height: rows.length * ROW_HEIGHT }}>
          <div className="trajectory-wf-links">
            {tickMarks(axis, view).map((tick) => (
              <span key={`grid-${tick.left}`} className="trajectory-wf-grid" style={{ left: percent(tick.left) }} />
            ))}
            {nowLine === null ? null : (
              <span className="trajectory-wf-now" style={{ left: percent(nowLine.left) }} />
            )}
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

/** Rupees of MiB, as the status line words a scan. */
export const mib = (bytes: number): string => (bytes / (1024 * 1024)).toFixed(1)
