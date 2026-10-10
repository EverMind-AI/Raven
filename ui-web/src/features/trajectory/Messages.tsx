/* A message list as the pane shows it: every row from the gateway's outline,
 * folded to one line until it is opened, the rows this call added already
 * open. Nothing here asks the reader to load more: the outline's pages are
 * walked in the background, and a row's body is read when the row is open
 * and in view -- by the cursor the outline gave it, so the first message of
 * a thousand and the last are one request each.
 *
 * Which rows open on their own is the row's delta (`meta.delta`): the new
 * messages of a continued call, every message of a first or an independent
 * call, every message while the delta is still unknown; a list that carries
 * no delta (a stored conversation) starts folded. The two controls at the
 * top open or fold everything; a row the reader opened or folded by hand
 * keeps that choice when the delta is decided later.
 *
 * Only the rows near the pane's viewport are in the DOM: the list measures
 * the rows it draws, estimates the rest, and stands empty height in for
 * them, so a thousand-message prompt costs what a screen of it costs. A row
 * whose page the cache let go folds again until the reader asks for it.
 */

import { useCallback, useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import * as details from './detailStore'
import * as list from './store'

import type { OutlineItem } from './detailStore'
import type { TrajectoryBlockDescriptor } from './types'
import type { JSX, ReactNode } from 'react'

/** A folded row's height and an open row's, until the row is measured. */
export const FOLD_H = 30
export const OPEN_EST = 96
/** The gap the list puts between rows. */
export const ROW_GAP = 6
/** How far beyond the pane's edges rows are still drawn, in pixels. */
export const OVERSCAN_PX = 240
/** The pane's height while it cannot be measured, so the first rows are drawn and read. */
export const DEFAULT_VIEW_H = 600

interface Delta {
  state: 'first' | 'continued' | 'independent' | 'unknown' | null
  newFrom: number | null
}

function deltaOf(entryId: string): Delta {
  const meta = list.entry(entryId)?.meta ?? {}
  const state = meta.delta
  const newFrom = meta.new_from
  return {
    state: state === 'first' || state === 'continued' || state === 'independent' || state === 'unknown' ? state : null,
    newFrom: typeof newFrom === 'number' ? newFrom : null,
  }
}

/** Whether a row opens before the reader says anything. */
export function openByDefault(item: OutlineItem, delta: Delta): boolean {
  if (delta.state === 'continued') return delta.newFrom !== null && item.index >= delta.newFrom
  if (delta.state === 'first' || delta.state === 'independent' || delta.state === 'unknown') return true
  return false
}

const sizeOf = (item: OutlineItem): string => {
  if (item.chars !== null) return t('gui.trajectory.details.msg_chars', { n: item.chars })
  if (item.bytes !== null) return t('gui.trajectory.details.msg_bytes', { n: item.bytes })
  return ''
}

function FoldedRow({ item, onOpen }: { item: OutlineItem; onOpen: () => void }): JSX.Element {
  const preview = item.missing ? t('gui.trajectory.details.msg_missing') : item.preview
  return (
    <button className="trajectory-msg-fold" data-index={item.index} onClick={onOpen} title={item.partial ? t('gui.trajectory.details.msg_partial') : undefined}>
      <span className="trajectory-msg-role">{item.role}</span>
      <span className="trajectory-msg-first">{preview}{item.partial ? '…' : ''}</span>
      <span className="trajectory-msg-size">{sizeOf(item)}</span>
    </button>
  )
}

/* Where the pane's viewport stands over the list: its scroll offset, its
   height, and the list's own offset inside the scrolled content. */
interface View {
  top: number
  height: number
  listTop: number
}

export function MessagesView({ block, entryId, render }: {
  block: TrajectoryBlockDescriptor
  entryId: string
  /** Draws one message's body; the block view lends its renderer so the two never differ. */
  render: (message: unknown) => ReactNode
}): JSX.Element {
  const s = useSyncExternalStore(details.subscribe, details.get)
  const outline = details.outline(s)
  const record = details.block(block.id, s)
  const delta = deltaOf(entryId)
  const [choices, setChoices] = useState<Record<number, boolean>>({})
  const [expandedAll, setExpandedAll] = useState(false)
  const permitted = details.mayRead(s)
  const identityKey = s.current ? details.descriptorKey(s.current) : null

  /* The outline, asked for once the pane may read; the walk goes on by itself, and a walk that
     was cut short (the reader left, the wire dropped) is taken up again on return. */
  useEffect(() => {
    if (permitted && identityKey !== null && (outline === null || (!outline.done && !outline.loading && outline.fault === null))) {
      void details.loadOutline()
    }
  }, [permitted, identityKey, outline])

  /* The reader's choices and the measured heights start over with another entry. */
  const heights = useRef(new Map<number, number>())
  const estimates = useRef<{ open: number; fold: number }>({ open: OPEN_EST, fold: FOLD_H })
  const seen = useRef(entryId)
  useEffect(() => {
    if (seen.current !== entryId) {
      seen.current = entryId
      heights.current = new Map()
      estimates.current = { open: OPEN_EST, fold: FOLD_H }
      setChoices({})
      setExpandedAll(false)
    }
  }, [entryId])

  const isOpen = (item: OutlineItem): boolean => {
    const chosen = choices[item.index]
    if (chosen !== undefined) return chosen
    return expandedAll || openByDefault(item, delta)
  }
  /* A row whose page the window let go: folded again, read only when the reader asks. */
  const evicted = (item: OutlineItem): boolean => record !== null && details.onEvictedPage(record, item.index)
  const drawnOpen = (item: OutlineItem): boolean => isOpen(item) && !evicted(item)

  /* The pane that scrolls this list: its viewport is the window. */
  const box = useRef<HTMLDivElement>(null)
  const [view, setView] = useState<View>({ top: 0, height: 0, listTop: 0 })
  useEffect(() => {
    const el = box.current
    const pane = el?.closest<HTMLElement>('.trajectory-pane') ?? null
    if (!el || !pane) return
    const measure = (): void => {
      /* Layout offsets, which the pane's scrolling does not move, as the list view's anchor reads them. */
      const listTop = el.offsetTop - pane.offsetTop
      setView((v) => (v.top === pane.scrollTop && v.height === pane.clientHeight && v.listTop === listTop ? v : { top: pane.scrollTop, height: pane.clientHeight, listTop }))
    }
    measure()
    pane.addEventListener('scroll', measure, { passive: true })
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    ro?.observe(pane)
    return () => { pane.removeEventListener('scroll', measure); ro?.disconnect() }
  }, [identityKey])

  /* The window: the rows whose estimated extent meets the viewport, with the overscan. */
  const items = outline?.items ?? []
  const rowH = (item: OutlineItem): number =>
    (heights.current.get(item.index) ?? (drawnOpen(item) ? estimates.current.open : estimates.current.fold)) + ROW_GAP
  const viewH = view.height > 0 ? view.height : DEFAULT_VIEW_H
  const from = view.top - view.listTop - OVERSCAN_PX
  const to = view.top - view.listTop + viewH + OVERSCAN_PX
  let y = 0
  let first = -1
  let last = -1
  let topPad = 0
  for (let k = 0; k < items.length; k += 1) {
    const h = rowH(items[k]!)
    if (y + h > from && y < to) {
      if (first < 0) { first = k; topPad = y }
      last = k
    }
    y += h
  }
  const total = y
  const shown = first < 0 ? [] : items.slice(first, last + 1)
  const bottomPad = first < 0 ? 0 : Math.max(0, total - topPad - shown.reduce((n, item) => n + rowH(item), 0))

  /* The rows drawn are measured once they stand -- a skeleton waiting for its body is not a
     height -- and a changed height redraws once. The rows not drawn are estimated from the
     measured ones of their kind, so the list's extent settles after the first screen. The key
     names what is drawn (which rows, folded, open or with a body), so a paint that changed
     nothing of that is not measured again; a row that changed size by itself -- a tree in a
     body opened or folded, the pane's width reflowing it -- is caught by the list's resize. */
  const [, remeasured] = useState(0)
  const drawnKey = shown.map((item) => `${item.index}${drawnOpen(item) ? (record && details.pageHolding(record, item.index) ? 'b' : 'o') : 'f'}`).join(',')
  const measureRows = useCallback((): void => {
    const el = box.current
    if (!el) return
    let changed = false
    const seenOpen: number[] = []
    const seenFold: number[] = []
    for (const node of el.querySelectorAll<HTMLElement>('[data-index]')) {
      const h = node.offsetHeight
      const index = Number(node.dataset.index)
      if (!(h > 0) || !Number.isFinite(index) || node.querySelector('.trajectory-skel-line')) continue
      ;(node.classList.contains('trajectory-msg-fold') ? seenFold : seenOpen).push(h)
      if (Math.abs((heights.current.get(index) ?? 0) - h) > 1) {
        heights.current.set(index, h)
        changed = true
      }
    }
    const median = (xs: number[]): number | null => (xs.length ? [...xs].sort((a, b) => a - b)[Math.floor(xs.length / 2)]! : null)
    const open = median(seenOpen) ?? estimates.current.open
    const fold = median(seenFold) ?? estimates.current.fold
    if (open !== estimates.current.open || fold !== estimates.current.fold) {
      estimates.current = { open, fold }
      changed = true
    }
    if (changed) remeasured((n) => n + 1)
  }, [])
  useLayoutEffect(() => { measureRows() }, [drawnKey, measureRows])
  useEffect(() => {
    const el = box.current
    if (!el || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => { measureRows() })
    ro.observe(el)
    return () => ro.disconnect()
  }, [identityKey, measureRows])

  /* A body is read with the page it sits on: the page is asked for by the
     row that starts it, so twenty open rows of one page ask once. A page the
     gateway cut short to fit one response holds its head but not this row,
     so the row is then asked for by its own cursor. */
  const pageOf = (item: OutlineItem): { cursor: string; index: number } => {
    const start = item.index - (item.index % details.MESSAGES_PAGE)
    const head = items.find((it) => it.index === start)
    if (!head || (record && details.pageHolding(record, head.index) !== null)) return { cursor: item.cursor, index: item.index }
    return { cursor: head.cursor, index: head.index }
  }
  const wanted = useRef(new Set<string>())
  const ask = (item: OutlineItem): void => {
    const page = pageOf(item)
    if (wanted.current.has(page.cursor)) return
    wanted.current.add(page.cursor)
    void details.loadPageAt(block.id, page.cursor, page.index).finally(() => wanted.current.delete(page.cursor))
  }
  /* Open rows in the window read their bodies; nothing beyond the window is asked for,
     and a page that failed waits for the reader's retry rather than being asked again. */
  const pageFault = details.fault({ blockId: block.id, more: true }, s) ?? details.fault({ blockId: block.id }, s)
  const faulted = pageFault !== null
  useEffect(() => {
    if (!permitted || faulted) return
    for (const item of shown) {
      if (drawnOpen(item) && (!record || details.pageHolding(record, item.index) === null)) ask(item)
    }
  })

  const open = (item: OutlineItem): void => {
    setChoices((c) => ({ ...c, [item.index]: true }))
    if (permitted) ask(item)
  }

  const note = delta.state === 'unknown'
    ? t('gui.trajectory.details.delta_unknown')
    : delta.state === 'continued' && delta.newFrom !== null && outline?.total !== null && outline !== null && delta.newFrom >= (outline.total ?? 0)
      ? t('gui.trajectory.details.delta_same')
      : null

  return (
    <div className="trajectory-msgs-view">
      <div className="trajectory-msg-controls">
        <button className="trajectory-link" onClick={() => { setExpandedAll(true); setChoices({}) }}>{t('gui.trajectory.details.expand_all')}</button>
        {/* Back to the default fold: the messages the model had seen folded, the new ones open. */}
        <button className="trajectory-link" onClick={() => { setExpandedAll(false); setChoices({}) }}>{t('gui.trajectory.details.collapse_old')}</button>
        {outline && (outline.loading || (!outline.done && outline.fault === null)) ? (
          <span className="trajectory-msg-progress">{t('gui.trajectory.details.outline_progress', { n: items.length, total: outline.total ?? '?' })}</span>
        ) : null}
        {note ? <span className="trajectory-msg-note">{note}</span> : null}
      </div>
      {outline?.fault ? (
        <p className="trajectory-fault" role="alert">
          {t('gui.trajectory.details.failed', { detail: outline.fault })}
          {' '}
          <button className="trajectory-link" onClick={() => { void details.retryOutline() }}>{t('gui.trajectory.details.retry')}</button>
        </p>
      ) : null}
      {/* A page of bodies that failed: said where the rows wait, with the one way to ask again. */}
      {pageFault !== null ? (
        <p className="trajectory-fault" role="alert">
          {t('gui.trajectory.details.failed', { detail: pageFault })}
          {' '}
          <button className="trajectory-link" onClick={() => { details.clearBlockFaults(block.id) }}>{t('gui.trajectory.details.retry')}</button>
        </p>
      ) : null}
      <div className="trajectory-msgs" ref={box}>
        {topPad > 0 ? <div className="trajectory-msg-space" style={{ height: topPad - ROW_GAP }} aria-hidden="true" /> : null}
        {shown.map((item) => {
          if (!drawnOpen(item)) {
            return <FoldedRow key={item.index} item={item} onOpen={() => open(item)} />
          }
          const body = record ? details.messageAt(record, item.index) : undefined
          return (
            <div key={item.index} className="trajectory-msg-open" data-index={item.index}>
              <button className="trajectory-msg-close" aria-label={t('gui.trajectory.details.collapse_one')} onClick={() => setChoices((c) => ({ ...c, [item.index]: false }))}>
                {'−'}
              </button>
              {body === undefined
                ? <div className="trajectory-skel-line" aria-busy="true" />
                : render(body)}
            </div>
          )
        })}
        {bottomPad > 0 ? <div className="trajectory-msg-space" style={{ height: bottomPad - ROW_GAP }} aria-hidden="true" /> : null}
        {outline === null || (items.length === 0 && outline.loading) ? <div className="trajectory-skel-line" aria-busy="true" /> : null}
        {outline !== null && outline.done && items.length === 0 ? <p className="trajectory-v-none">{t('gui.trajectory.details.empty_list')}</p> : null}
      </div>
    </div>
  )
}
