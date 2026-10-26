/* The list of trajectory entries: fixed 32px rows, only the ones in view.
 *
 * A conversation's trajectory runs to tens of thousands of entries and the
 * list is redrawn on every feed batch, so the rows are virtual: the scroller
 * holds one box as tall as every row would be, and only the rows inside the
 * viewport plus eight either side exist. A row is positioned by its index,
 * which the fixed height makes arithmetic rather than measurement.
 *
 * Two things the list owns and the store only records: whether the reader is
 * following the tail (within 48px of the bottom), in which case a batch that
 * adds rows pulls the viewport down with it; and, when they are not, which
 * entry sits at the top of the viewport and how far into it, so the same row
 * stays under the eye when the feed inserts above it. Selection is not the
 * list's: every door -- a click, an arrow key -- goes through the store's one
 * `select`, and the list only draws what it says.
 */

import { ArrowUpRight01Icon } from '@hugeicons/core-free-icons'
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore } from 'react'

import { Icon } from '../../components/Icon'
import { t } from '../../i18n/t'
import { formatDuration } from '../../lib/duration'
import * as lang from '../../state/lang'
import { statusKey } from './blocks'
import * as details from './detailStore'
import { DBL_MS } from './geometry'
import { isKnownKind, kindClass, kindLabel, kindLabels } from './palette'
import * as scopes from './scopes'
import * as store from './store'

import type { TrajectoryEntry } from './types'
import type { CSSProperties, JSX, KeyboardEvent as ReactKeyboardEvent, UIEvent } from 'react'

export const ROW_HEIGHT = 32
/** Rows drawn beyond either edge of the viewport, so a flick has rows to show. */
export const OVERSCAN = 8
/** Within this many pixels of the bottom the reader counts as following the tail. */
export const FOLLOW_SLACK = 48
/** Below this width the turn and kind columns narrow. */
export const NARROW_WIDTH = 480
/** What a box that has not been measured yet is assumed to show. */
const DEFAULT_ROWS = 20
/** The integrity codes that mean the row's record is not there to read. */
const MISSING_CODES = ['artifact_missing', 'artifact_unreadable', 'blob_missing']
/** The least a sub-agent's door row leaves its preview, however narrow the list. */
export const DOOR_PREVIEW_MIN = 72
/** The column widths before any label was measured: the turn number, the mark, the kind tag. */
const FALLBACK_COLS = { turn: 72, kind: 176 }

/* The grid's first three columns, measured from the words they must hold:
   three digits and the sub-agent mark for the turn, the widest kind label in
   the current language plus the tag's padding for the kind. Measured on a
   canvas so no row has to be drawn twice; where there is no canvas (the test
   DOM) the fallback widths stand. */
export function measureColumns(labels: string[], tagFont: string, turnFont: string): { turn: number; kind: number } {
  const ctx = typeof document !== 'undefined' ? document.createElement('canvas').getContext?.('2d') : null
  if (!ctx) return FALLBACK_COLS
  ctx.font = tagFont
  const widest = labels.reduce((w, label) => Math.max(w, ctx.measureText(label).width), 0)
  ctx.font = turnFont
  const digits = ctx.measureText('000').width
  return { turn: Math.ceil(digits + 12), kind: Math.ceil(widest + 16 + 4) }
}

const fontOf = (selector: string, fallback: string): string => {
  const el = typeof document !== 'undefined' ? document.querySelector<HTMLElement>(selector) : null
  const font = el ? getComputedStyle(el).font : ''
  return font || fallback
}

const isMissing = (entry: TrajectoryEntry): boolean => entry.integrity.some((code) => MISSING_CODES.includes(code))

interface Window {
  first: number
  last: number
}

/* The rows a viewport of `height` at `scrollTop` shows, widened by the
   overscan and clamped to the list. Exported for the test that pins the upper
   bound on how many rows exist at once. */
export function windowOf(scrollTop: number, height: number, count: number): Window {
  const rows = height > 0 ? Math.ceil(height / ROW_HEIGHT) : DEFAULT_ROWS
  const first = Math.max(0, Math.floor(scrollTop / ROW_HEIGHT) - OVERSCAN)
  const last = Math.min(count, Math.floor(scrollTop / ROW_HEIGHT) + rows + OVERSCAN)
  return { first, last }
}

/* A row that opens a sub-agent's tab says how the run stands and for how
   long, and that it can be opened. */
function Door({ entry }: { entry: TrajectoryEntry }): JSX.Element {
  const key = statusKey(entry.operation_status)
  const hint = t('gui.trajectory.scope.opens')
  return (
    <span className="trajectory-scope-door">
      <span className={`trajectory-scope-door-state trajectory-status-${entry.operation_status}`}>{key ? t(key) : entry.operation_status}</span>
      {typeof entry.duration_ms === 'number' ? <span className="trajectory-scope-door-dur">{formatDuration(entry.duration_ms)}</span> : null}
      <span className="trajectory-scope-opens" role="img" aria-label={hint} title={hint}><Icon icon={ArrowUpRight01Icon} size={12} /></span>
    </span>
  )
}

function Row({ entry, at, selected, total, revealed, turnTotal, inScope, onPick, onOpen }: {
  entry: TrajectoryEntry; at: number; selected: boolean; total: number; revealed: boolean; turnTotal: number | null; inScope: boolean
  onPick: (entry: TrajectoryEntry) => void; onOpen: (entry: TrajectoryEntry) => void
}): JSX.Element {
  const known = isKnownKind(entry.kind)
  /* A turn a sub-agent's report started carries a mark after its number that
     says so; any other dispatched turn carries the bare mark, the main line none. */
  const reported = entry.turn_start && store.reportedFromOf(entry) !== null
  const turnClass = entry.origin === 'main' || reported ? 'trajectory-turn' : 'trajectory-turn trajectory-turn-sub'
  /* A sub-agent tab's rows all belong to the turn that dispatched it: no number to tell them apart by. */
  const turnShown = !inScope && entry.turn_start && entry.turn_number !== null && entry.turn_number !== undefined
  const turnTitle = turnShown && turnTotal !== null
    ? t('gui.trajectory.turn_total', { n: entry.turn_number as number, dur: formatDuration(turnTotal) })
    : undefined
  const missing = isMissing(entry)
  const door = store.opensOf(entry) !== null
  const classes = ['trajectory-row']
  if (selected) classes.push('trajectory-row-on')
  if (revealed) classes.push('trajectory-row-revealed')
  if (door) classes.push('trajectory-row-door')
  return (
    <div
      className={classes.join(' ')}
      id={`trajectory-row-${at}`}
      role="option"
      aria-selected={selected}
      aria-setsize={total}
      aria-posinset={at + 1}
      data-entry={entry.entry_id}
      data-revealed={revealed ? '' : undefined}
      title={revealed ? t('gui.trajectory.revealed_row') : undefined}
      style={{ top: at * ROW_HEIGHT }}
      onClick={() => onPick(entry)}
      onDoubleClick={() => onOpen(entry)}
    >
      <span className={turnClass} title={turnTitle}>
        {turnShown ? entry.turn_number : ''}
        {reported ? (
          <span className="trajectory-scope-from" role="img" aria-label={t('gui.trajectory.scope.reported')} title={t('gui.trajectory.scope.reported')}>
            {'\u2022'}
          </span>
        ) : null}
      </span>
      <span className="trajectory-fail">
        {entry.failure_entry ? <span className="trajectory-fail-dot" role="img" aria-label={t('gui.trajectory.failed')} /> : null}
      </span>
      <span className="trajectory-kind">
        <span className={`trajectory-tag ${kindClass(entry.kind)}`} title={known ? undefined : entry.span_name}>
          {kindLabel(entry.kind)}
        </span>
      </span>
      {missing
        ? <span className="trajectory-text trajectory-text-missing" title={entry.integrity.join(', ')}>{t('gui.trajectory.missing_record')}</span>
        : entry.preview === null || entry.preview === ''
          ? <span className="trajectory-text trajectory-text-none">{t('gui.trajectory.no_preview')}</span>
          : <span className="trajectory-text" title={entry.preview}>{entry.preview}</span>}
      {door ? <Door entry={entry} /> : null}
    </div>
  )
}

/* Opens the tab a row leads to. The list keeps the focus when the tab has
   rows to show; a tab with none puts its empty state where the list was, and
   the focus goes to the tab instead. */
function openFromList(entry: TrajectoryEntry): void {
  if (scopes.openFromEntry(entry) && !store.get().visible.length) scopes.focusTab()
}

export function EntryList(): JSX.Element {
  const state = useSyncExternalStore(store.subscribe, store.get)
  const { selectedId, follow, anchor, listing, indexState } = state
  /* The rows the list draws are the visible ones; the store keeps the whole
     set beside them for the feed, the span links and the turn totals. */
  const entries = state.visible
  const index = state.visibleIndex
  const box = useRef<HTMLDivElement | null>(null)
  const observer = useRef<ResizeObserver | null>(null)
  const [scrollTop, setScrollTop] = useState(0)
  const [size, setSize] = useState({ width: 0, height: 0 })
  const count = entries.length
  const paneOpen = useSyncExternalStore(details.subscribe, details.get).open
  /* A click on a row that opens a tab, while the pane is closed, waits a
     double click's time before it selects: the pane it would open takes the
     list's right side, where the second click of a double click would then
     land. A double click in the wait opens the tab instead; any other row,
     or any row while the pane is up, selects at once. The wait gives way to
     whatever the reader does next -- another selection by any door, another
     tab, another conversation, the other view -- the moment it happens, so a
     click from before can never land over it; the feed alone does not count. */
  const waiting = useRef<{ timer: ReturnType<typeof setTimeout>; unwatch: () => void } | null>(null)
  const settle = useCallback((): void => {
    const w = waiting.current
    waiting.current = null
    if (!w) return
    clearTimeout(w.timer)
    w.unwatch()
  }, [])
  useEffect(() => settle, [settle])
  const onPick = useCallback((entry: TrajectoryEntry): void => {
    settle()
    const pick = (): void => { store.select(entry.entry_id, { source: 'click' }); details.openDetails() }
    if (scopes.openTarget(entry) === null || details.get().open) { pick(); return }
    const was = store.get()
    const unwatch = store.subscribe(() => {
      const s = store.get()
      if (s.selections !== was.selections || s.scope !== was.scope || s.sessionKey !== was.sessionKey || s.view !== was.view) settle()
    })
    const timer = setTimeout(() => {
      settle()
      if (entry.entry_id in store.get().visibleIndex) pick()
    }, DBL_MS)
    waiting.current = { timer, unwatch }
  }, [settle])
  const onOpen = useCallback((entry: TrajectoryEntry): void => {
    settle()
    openFromList(entry)
  }, [settle])
  const wasOpen = useRef(paneOpen)
  /* The column widths follow the words of the current language: the labels
     are the subscription's snapshot, joined so the value compares by content. */
  const labels = useSyncExternalStore(lang.subscribe, () => kindLabels().join('\n'))
  /* A row that opens a sub-agent's tab has a fifth column for how the run
     stands, and its kind column gives way down to its own tag's width when the
     list is too narrow for both: the kind column is sized to the widest tag in
     the language, and that room is the only room there is to give. The preview
     keeps a floor, or the kind column would take that room first. */
  const cols = useMemo(() => {
    const measured = measureColumns(labels.split('\n'), fontOf('.trajectory-tag', '11px system-ui'), fontOf('.trajectory-turn', '11px monospace'))
    return {
      all: `${measured.turn}px 14px ${measured.kind}px minmax(0, 1fr)`,
      door: `${measured.turn}px 14px minmax(min-content, ${measured.kind}px) minmax(${DOOR_PREVIEW_MIN}px, 1fr) auto`,
    }
  }, [labels])

  /* The selected row, brought into view when the window has scrolled past it. */
  const reveal = useCallback((): void => {
    const el = box.current
    if (!el || selectedId === null) return
    const at = index[selectedId]
    if (at === undefined) return
    const top = at * ROW_HEIGHT
    if (top < el.scrollTop || top + ROW_HEIGHT > el.scrollTop + el.clientHeight) el.scrollTop = top
  }, [selectedId, index])

  /* The pane closing hands the focus back to the list, on the row that was
     selected, brought into view first. */
  useLayoutEffect(() => {
    const closed = wasOpen.current && !paneOpen
    wasOpen.current = paneOpen
    if (!closed || details.get().swapped) return
    reveal()
    box.current?.focus()
  }, [paneOpen, reveal])

  /* A selection made elsewhere -- on the duration bar, or a link in the
     details -- brings its row into view too; a click here already has it. The
     follow flag is the reader's and is left alone. */
  const { selectedBy, selections } = state
  /* Such a selection can show a hidden row, which changes the rows in the
     same commit; neither the tail-following nor the anchor below may take
     the view away from it in that commit. The scroll handler then settles
     the follow flag and the anchor, the way it does after a reader's scroll. */
  const revealing = useRef(false)
  /* Once per selection: `reveal` changes with every batch of rows, and a
     selection answered again on each would keep the tail from being followed. */
  const answered = useRef<number | null>(null)
  useLayoutEffect(() => {
    if (answered.current === selections) return
    answered.current = selections
    if (selectedBy !== 'bar' && selectedBy !== 'link') return
    reveal()
    revealing.current = true
  }, [selections, selectedBy, reveal])

  /* The viewport's size, measured the moment the scroller exists and again
     whenever the browser says it changed. Bound to the node rather than to
     the component's mount: the island is mounted at boot and shows its empty
     state first, so the scroller comes and goes with the data, and a measure
     taken once at mount would never see it. Where there is no ResizeObserver
     (happy-dom, the embedded pane) the one measure at attach is what there is. */
  const attach = useCallback((el: HTMLDivElement | null): void => {
    observer.current?.disconnect()
    observer.current = null
    box.current = el
    if (!el) return
    const measure = (): void => { setSize({ width: el.clientWidth, height: el.clientHeight }) }
    measure()
    /* A scroller that comes back starts where the browser put it, which no
       scroll event reports: a short list swapped in under a deep scroll
       position would otherwise draw an empty window. */
    setScrollTop(el.scrollTop)
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    observer.current = ro
  }, [])

  /* After the rows change: follow the tail, or put the anchored row back
     where it was. Layout-time, so the reader never sees the frame in between. */
  useLayoutEffect(() => {
    const el = box.current
    if (!el || revealing.current) return
    if (follow) {
      el.scrollTop = el.scrollHeight
      setScrollTop(el.scrollTop)
      return
    }
    if (anchor) {
      /* The anchored row, or -- when a switch hid it -- the first visible row after it. */
      let at = index[anchor.id]
      const whole = state.index[anchor.id]
      if (at === undefined && whole !== undefined) {
        const next = state.entries.slice(whole).find((e) => e.entry_id in index)
        at = next ? index[next.entry_id] : undefined
      }
      if (at !== undefined) {
        el.scrollTop = at * ROW_HEIGHT + anchor.offset
        setScrollTop(el.scrollTop)
      } else if (whole !== undefined && !state.listing) {
        /* The row and everything after it are hidden: the place it marked is the
           list's end. An anchor the rows do not hold yet waits for its page. */
        store.setPlace(true, null)
      }
    }
  }, [entries, follow, anchor, index, state.index, state.entries, state.listing])

  /* Last in every commit: a selection's reveal holds the tail only in its own. */
  useLayoutEffect(() => { revealing.current = false })

  const onScroll = (e: UIEvent<HTMLDivElement>): void => {
    const el = e.currentTarget
    setScrollTop(el.scrollTop)
    const nearTail = el.scrollHeight - el.scrollTop - el.clientHeight <= FOLLOW_SLACK
    if (nearTail) {
      store.setPlace(true, null)
      return
    }
    const top = Math.floor(el.scrollTop / ROW_HEIGHT)
    const entry = entries[top]
    store.setPlace(false, entry ? { id: entry.entry_id, offset: el.scrollTop - top * ROW_HEIGHT } : null)
  }

  /* Up and down move one row, Home and End to either end; each goes through
     the store's one door and then brings the row into view. */
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>): void => {
    if (!count) return
    const at = selectedId !== null ? index[selectedId] : undefined
    let next: number | null = null
    if (e.key === 'ArrowDown') next = at === undefined ? 0 : Math.min(count - 1, at + 1)
    else if (e.key === 'ArrowUp') next = at === undefined ? count - 1 : Math.max(0, at - 1)
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = count - 1
    else if (e.key === 'Enter') {
      const target = selectedId !== null ? store.entry(selectedId) : null
      if (!target || scopes.openTarget(target) === null) return
      e.preventDefault()
      openFromList(target)
      return
    }
    if (next === null) return
    e.preventDefault()
    const target = entries[next]
    if (!target) return
    store.select(target.entry_id, { source: 'keyboard' })
    const el = box.current
    if (!el) return
    const top = next * ROW_HEIGHT
    if (top < el.scrollTop) el.scrollTop = top
    else if (top + ROW_HEIGHT > el.scrollTop + el.clientHeight) el.scrollTop = top + ROW_HEIGHT - el.clientHeight
  }

  if (!count && !listing) {
    const scanning = indexState !== null && indexState.phase !== 'ready'
    return (
      <div className="trajectory-empty" role="status">
        {state.scope !== null
          ? t(emptyScopeKey(scopes.scopeState(state, state.scope)))
          : scanning
            ? t('gui.trajectory.scanning', { done: mib(indexState.scanned_bytes), total: mib(indexState.total_bytes) })
            : t('gui.trajectory.empty')}
      </div>
    )
  }

  const { first, last } = windowOf(scrollTop, size.height, count)
  const rows: JSX.Element[] = []
  for (let at = first; at < last; at += 1) {
    const entry = entries[at]
    if (!entry) continue
    const turnTotal = entry.turn_span_id ? (state.turnTotals[entry.turn_span_id] ?? null) : null
    rows.push(
      <Row
        key={entry.entry_id}
        entry={entry}
        at={at}
        selected={entry.entry_id === selectedId}
        total={count}
        revealed={store.hiddenOf(entry) !== null && !state.prefs.showHidden}
        turnTotal={turnTotal}
        inScope={state.scope !== null}
        onPick={onPick}
        onOpen={onOpen}
      />,
    )
  }
  const activeAt = selectedId !== null ? index[selectedId] : undefined
  return (
    <div
      className="trajectory-list"
      ref={attach}
      role="listbox"
      tabIndex={0}
      aria-label={t('gui.trajectory.list_label')}
      aria-activedescendant={activeAt !== undefined ? `trajectory-row-${activeAt}` : undefined}
      data-narrow={size.width > 0 && size.width < NARROW_WIDTH ? '' : undefined}
      style={{ '--trajectory-cols': cols.all, '--trajectory-cols-door': cols.door } as CSSProperties}
      onScroll={onScroll}
      onKeyDown={onKeyDown}
    >
      <div className="trajectory-rows" style={{ height: count * ROW_HEIGHT }}>{rows}</div>
    </div>
  )
}

/** What an empty sub-agent tab says: the run's records are gone, it has not recorded a step yet, or it recorded none. */
const emptyScopeKey = (state: scopes.ScopeState): string =>
  state === 'gone' ? 'gui.trajectory.scope.gone' : state === 'running' ? 'gui.trajectory.scope.empty_running' : 'gui.trajectory.scope.empty_done'

/** Bytes as a mebibyte figure with one decimal, for the scan line. */
export const mib = (bytes: number): string => (bytes / (1024 * 1024)).toFixed(1)
