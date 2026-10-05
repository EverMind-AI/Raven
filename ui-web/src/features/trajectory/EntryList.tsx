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

import { useCallback, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import * as details from './details'
import { isKnownKind, kindClass, kindLabel } from './palette'
import * as store from './store'

import type { TrajectoryEntry } from './types'
import type { JSX, KeyboardEvent as ReactKeyboardEvent, UIEvent } from 'react'

export const ROW_HEIGHT = 32
/** Rows drawn beyond either edge of the viewport, so a flick has rows to show. */
export const OVERSCAN = 8
/** Within this many pixels of the bottom the reader counts as following the tail. */
export const FOLLOW_SLACK = 48
/** Below this width the turn and kind columns narrow. */
export const NARROW_WIDTH = 480
/** What a box that has not been measured yet is assumed to show. */
const DEFAULT_ROWS = 20

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

function Row({ entry, at, selected, total }: {
  entry: TrajectoryEntry; at: number; selected: boolean; total: number
}): JSX.Element {
  const known = isKnownKind(entry.kind)
  /* A sub-agent's turn carries a mark after its number; the main line does not. */
  const turnClass = entry.origin === 'main' ? 'trajectory-turn' : 'trajectory-turn trajectory-turn-sub'
  return (
    <div
      className={selected ? 'trajectory-row trajectory-row-on' : 'trajectory-row'}
      id={`trajectory-row-${at}`}
      role="option"
      aria-selected={selected}
      aria-setsize={total}
      aria-posinset={at + 1}
      data-entry={entry.entry_id}
      style={{ top: at * ROW_HEIGHT }}
      onClick={() => { store.select(entry.entry_id, { source: 'click' }); details.openDetails() }}
    >
      <span className={turnClass}>
        {entry.turn_start && entry.turn_number !== null && entry.turn_number !== undefined ? entry.turn_number : ''}
      </span>
      <span className="trajectory-fail">
        {entry.failure_entry ? <span className="trajectory-fail-dot" role="img" aria-label={t('gui.trajectory.failed')} /> : null}
      </span>
      <span className="trajectory-kind">
        <span className={`trajectory-tag ${kindClass(entry.kind)}`} title={known ? undefined : entry.span_name}>
          {kindLabel(entry.kind)}
        </span>
      </span>
      {entry.preview === null || entry.preview === undefined
        ? <span className="trajectory-text trajectory-text-none">{t('gui.trajectory.no_preview')}</span>
        : <span className="trajectory-text" title={entry.preview}>{entry.preview}</span>}
    </div>
  )
}

export function EntryList(): JSX.Element {
  const state = useSyncExternalStore(store.subscribe, store.get)
  const { entries, selectedId, follow, anchor, listing, indexState } = state
  const box = useRef<HTMLDivElement | null>(null)
  const observer = useRef<ResizeObserver | null>(null)
  const [scrollTop, setScrollTop] = useState(0)
  const [size, setSize] = useState({ width: 0, height: 0 })
  const count = entries.length
  const paneOpen = useSyncExternalStore(details.subscribe, details.get).open
  const wasOpen = useRef(paneOpen)

  /* The pane closing hands the focus back to the list, on the row that was
     selected, brought into view first when the window has scrolled past it. */
  useLayoutEffect(() => {
    const closed = wasOpen.current && !paneOpen
    wasOpen.current = paneOpen
    if (!closed) return
    const el = box.current
    if (!el) return
    if (selectedId !== null) {
      const at = state.index[selectedId]
      if (at !== undefined) {
        const top = at * ROW_HEIGHT
        if (top < el.scrollTop || top + ROW_HEIGHT > el.scrollTop + el.clientHeight) el.scrollTop = top
      }
    }
    el.focus()
  }, [paneOpen, selectedId, state.index])

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
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    observer.current = ro
  }, [])

  /* After the rows change: follow the tail, or put the anchored row back
     where it was. Layout-time, so the reader never sees the frame in between. */
  useLayoutEffect(() => {
    const el = box.current
    if (!el) return
    if (follow) {
      el.scrollTop = el.scrollHeight
      return
    }
    if (anchor) {
      const at = state.index[anchor.id]
      if (at !== undefined) el.scrollTop = at * ROW_HEIGHT + anchor.offset
    }
  }, [entries, follow, anchor, state.index])

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
    const at = selectedId !== null ? state.index[selectedId] : undefined
    let next: number | null = null
    if (e.key === 'ArrowDown') next = at === undefined ? 0 : Math.min(count - 1, at + 1)
    else if (e.key === 'ArrowUp') next = at === undefined ? count - 1 : Math.max(0, at - 1)
    else if (e.key === 'Home') next = 0
    else if (e.key === 'End') next = count - 1
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
        {scanning
          ? t('gui.trajectory.scanning', { done: mib(indexState.scanned_bytes), total: mib(indexState.total_bytes) })
          : t('gui.trajectory.empty')}
      </div>
    )
  }

  const { first, last } = windowOf(scrollTop, size.height, count)
  const rows: JSX.Element[] = []
  for (let at = first; at < last; at += 1) {
    const entry = entries[at]
    if (entry) rows.push(<Row key={entry.entry_id} entry={entry} at={at} selected={entry.entry_id === selectedId} total={count} />)
  }
  const activeAt = selectedId !== null ? state.index[selectedId] : undefined
  return (
    <div
      className="trajectory-list"
      ref={attach}
      role="listbox"
      tabIndex={0}
      aria-label={t('gui.trajectory.list_label')}
      aria-activedescendant={activeAt !== undefined ? `trajectory-row-${activeAt}` : undefined}
      data-narrow={size.width > 0 && size.width < NARROW_WIDTH ? '' : undefined}
      onScroll={onScroll}
      onKeyDown={onKeyDown}
    >
      <div className="trajectory-rows" style={{ height: count * ROW_HEIGHT }}>{rows}</div>
    </div>
  )
}

/** Bytes as a mebibyte figure with one decimal, for the scan line. */
export const mib = (bytes: number): string => (bytes / (1024 * 1024)).toFixed(1)
