/* A message list as the pane shows it: every row from the gateway's outline,
 * folded to one line until it is opened, the rows this call added already
 * open. Nothing here asks the reader to load more: the outline's pages are
 * walked in the background, and a row's body is read when the row opens --
 * by the cursor the outline gave it, so the first message of a thousand and
 * the last are one request each.
 *
 * Which rows open on their own is the row's delta (`meta.delta`): the new
 * messages of a continued call, every message of a first or an independent
 * call, every message while the delta is still unknown; a list that carries
 * no delta (a stored conversation) starts folded. The two controls at the
 * top open or fold everything; a row the reader opened or folded by hand
 * keeps that choice when the delta is decided later.
 */

import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import * as details from './detailStore'
import * as list from './store'

import type { OutlineItem } from './detailStore'
import type { TrajectoryBlockDescriptor } from './types'
import type { JSX, ReactNode } from 'react'

/** How many open rows load their bodies at once; the rest load as they come into view. */
const EAGER_ROWS = 6

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

  /* The outline, asked for once the pane may read; the walk goes on by itself. */
  useEffect(() => {
    if (permitted && identityKey !== null && (outline === null || (outline.nextCursor !== null && !outline.loading && outline.fault === null))) {
      void details.loadOutline()
    }
  }, [permitted, identityKey, outline])

  /* The reader's choices start over with another entry. */
  const seen = useRef(entryId)
  useEffect(() => {
    if (seen.current !== entryId) { seen.current = entryId; setChoices({}); setExpandedAll(false) }
  }, [entryId])

  const isOpen = (item: OutlineItem): boolean => {
    const chosen = choices[item.index]
    if (chosen !== undefined) return chosen
    return expandedAll || openByDefault(item, delta)
  }

  const items = outline?.items ?? []
  const open = items.filter(isOpen)
  const missingBodies = open.filter((item) => !record || details.pageHolding(record, item.index) === null)
  /* A body is read with the page it sits on: the page is asked for by the
     row that starts it, so twenty open rows of one page ask once. */
  const pageOf = (item: OutlineItem): { cursor: string; index: number } => {
    const start = item.index - (item.index % details.MESSAGES_PAGE)
    const first = items.find((it) => it.index === start)
    return first ? { cursor: first.cursor, index: first.index } : { cursor: item.cursor, index: item.index }
  }
  /* A page the window let go of is not asked for again on its own account:
     the rows it held come back when the reader scrolls to them. */
  const letGo = (index: number): boolean => {
    if (!record || record.pages.length < details.PAGE_WINDOW) return false
    const first = details.firstHeldOffset(record)
    return first !== null && index < first
  }
  /* Open rows read their bodies: the first few at once, the rest as they come into view. */
  const wanted = useRef(new Set<string>())
  useEffect(() => {
    if (!permitted) return
    for (const item of missingBodies.filter((it) => !letGo(it.index)).slice(0, EAGER_ROWS)) {
      const page = pageOf(item)
      if (wanted.current.has(page.cursor)) continue
      wanted.current.add(page.cursor)
      void details.loadPageAt(block.id, page.cursor, page.index).finally(() => wanted.current.delete(page.cursor))
    }
  })

  const onSight = (item: OutlineItem) => (el: HTMLDivElement | null): void => {
    const page = pageOf(item)
    if (!el || typeof IntersectionObserver === 'undefined') {
      if (el && permitted && !letGo(item.index)) void details.loadPageAt(block.id, page.cursor, page.index)
      return
    }
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) {
        io.disconnect()
        void details.loadPageAt(block.id, page.cursor, page.index)
      }
    })
    io.observe(el)
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
        {outline && (outline.loading || outline.nextCursor !== null) ? (
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
      <div className="trajectory-msgs">
        {items.map((item) => {
          if (!isOpen(item)) {
            return <FoldedRow key={item.index} item={item} onOpen={() => setChoices((c) => ({ ...c, [item.index]: true }))} />
          }
          const body = record ? details.messageAt(record, item.index) : undefined
          return (
            <div key={item.index} className="trajectory-msg-open" data-index={item.index} ref={body === undefined ? onSight(item) : undefined}>
              <button className="trajectory-msg-close" aria-label={t('gui.trajectory.details.collapse_one')} onClick={() => setChoices((c) => ({ ...c, [item.index]: false }))}>
                {'−'}
              </button>
              {body === undefined
                ? <div className="trajectory-skel-line" aria-busy="true" />
                : render(body)}
            </div>
          )
        })}
        {outline === null || (items.length === 0 && outline.loading) ? <div className="trajectory-skel-line" aria-busy="true" /> : null}
        {outline !== null && items.length === 0 && !outline.loading && !outline.fault ? <p className="trajectory-v-none">{t('gui.trajectory.details.empty_list')}</p> : null}
      </div>
    </div>
  )
}
