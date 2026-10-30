/* The details pane: what the selected entry is, as a white card floating over
 * the list's right side -- the list keeps its whole width -- or over all of
 * it, when the area is too narrow for two.
 *
 * Three fixed parts and one that scrolls: a head (the kind's tag, the short
 * name the descriptor gives it, the way to the sub-agent tab the entry leads
 * to when it leads to one, and the way out), the tab strip, and the
 * pane the current tab fills with its own scroll position. The card moves by
 * its head and sizes by its corner, the way the desk palette does, and the
 * store holds it inside the area. Closing keeps the selection; the list gets
 * the focus back so the next key goes where the reader was.
 */

import { Pin02Icon } from '@hugeicons/core-free-icons'
import { useEffect, useLayoutEffect, useRef, useSyncExternalStore } from 'react'

import { Icon } from '../../components/Icon'
import { t } from '../../i18n/t'
import { showBar } from '../../state/scrollbars'
import { BlockView } from './BlockView'
import * as details from './detailStore'
import { Overview } from './Overview'
import { kindClass, kindLabel } from './palette'
import * as scopes from './scopes'
import * as list from './store'
import { panelId, tabId, Tabs } from './Tabs'

import type { TrajectoryDetailResult, TrajectoryEntry } from './types'
import type { JSX, PointerEvent as ReactPointerEvent } from 'react'

/* The name under the tag: the tool's from the row's own meta, else a
   skill's, a model's or an agent's when the descriptor's blocks carry it in
   their preview, the span name otherwise. */
export function shortName(value: TrajectoryDetailResult, entry?: TrajectoryEntry | null): string {
  const tool = entry?.meta?.tool
  if (typeof tool === 'string' && tool) return tool
  for (const [blockId, key] of [['skill', 'skill.name'], ['model', 'model'], ['plugin', 'plugin.name'], ['agent', 'subagent.label']] as const) {
    const block = value.blocks.find((b) => b.id === blockId)
    const preview = block?.preview
    if (!Array.isArray(preview)) continue
    const item = preview.find((p) => p !== null && typeof p === 'object' && (p as { key?: unknown }).key === key) as { value?: unknown } | undefined
    if (item && (typeof item.value === 'string' || typeof item.value === 'number')) return String(item.value)
  }
  return value.span_name
}

/* The pane's bar is parked in a fixed layer and placed from the pane's box, so
   a card that moved or changed size asks for it again. */
const followBar = (card: Element | null): void => {
  const el = card?.querySelector<HTMLElement>('.trajectory-pane')
  if (el) showBar(el)
}

/* The top of where the pane opens, from the stylesheet that puts it there: a
   gap under the header. NaN when the sheet does not say. */
const homeTop = (card: Element): number =>
  Number.parseFloat(getComputedStyle(card).getPropertyValue('--wf-stats-h')) + details.CARD_GAP

/* The card's corner, which sizes it from the edges that are free: where the
   pane opens its right edge is pinned, so the hand goes left to widen it, and
   once moved its left edge is, so the hand goes right. The top is pinned
   either way, and a pull takes no more than the room from the pinned edges to
   the area's -- left to the store, the pane would make that room by climbing. */
function Corner({ moved }: { moved: boolean }): JSX.Element {
  const gesture = useRef<AbortController | null>(null)
  useEffect(() => () => gesture.current?.abort(), [])
  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>): void => {
    const el = e.currentTarget
    const card = el.parentElement
    const box = card?.parentElement?.getBoundingClientRect()
    const rect = card?.getBoundingClientRect()
    if (e.button !== 0 || !box || !rect) return
    e.preventDefault()
    e.stopPropagation()
    const start = { x: e.clientX, y: e.clientY }
    const pointerId = e.pointerId
    const down = box.bottom - rect.top - details.CARD_GAP
    const right = box.right - rect.left - details.CARD_GAP
    gesture.current?.abort()
    const controller = new AbortController()
    gesture.current = controller
    controller.signal.addEventListener('abort', () => {
      delete el.dataset.drag
      if (el.hasPointerCapture(pointerId)) el.releasePointerCapture(pointerId)
    })
    el.setPointerCapture(pointerId)
    el.dataset.drag = 'true'
    const move = (ev: PointerEvent): void => {
      if (ev.pointerId !== pointerId) return
      const dx = ev.clientX - start.x
      details.resizeDetails(moved ? Math.min(rect.width + dx, right) : rect.width - dx, Math.min(rect.height + ev.clientY - start.y, down))
    }
    const finish = (ev: PointerEvent): void => {
      if (ev.pointerId === pointerId) controller.abort()
    }
    window.addEventListener('pointermove', move, { signal: controller.signal })
    window.addEventListener('pointerup', finish, { signal: controller.signal })
    window.addEventListener('pointercancel', finish, { signal: controller.signal })
    el.addEventListener('lostpointercapture', finish, { signal: controller.signal })
  }
  return <div className="trajectory-corner" aria-hidden="true" onPointerDown={onPointerDown} />
}

function Pane({ entryId, value }: { entryId: string; value: TrajectoryDetailResult }): JSX.Element {
  const tab = details.tabOf(entryId)
  const box = useRef<HTMLDivElement>(null)
  /* Each tab of each entry keeps its own place; a tab opened for the first
     time starts at the top, which is what an absent entry means. The bar is
     placed here too: this pane's bar stays, so another tab's length cannot be
     left standing over this one. */
  useLayoutEffect(() => {
    const el = box.current
    if (!el) return
    el.scrollTop = details.tabScroll(entryId, tab)
    showBar(el)
  }, [entryId, tab])
  const block = tab === 'overview' ? null : value.blocks.find((b) => b.id === tab)
  /* A tab's body arrives in pages, and each page makes the content longer than
     the bar standing over it was drawn for. */
  const panel = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const el = box.current
    const inner = panel.current
    if (!el || !inner || typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(() => showBar(el))
    ro.observe(inner)
    return () => ro.disconnect()
  }, [entryId, tab])
  return (
    <div
      className="trajectory-pane"
      data-thumb="keep"
      ref={box}
      onScroll={(e) => details.setTabScroll(entryId, tab, e.currentTarget.scrollTop)}
    >
      {tab === 'overview' || !block
        ? <Overview entryId={entryId} value={value} />
        : <div role="tabpanel" id={panelId(tab)} aria-labelledby={tabId(tab)} ref={panel}><BlockView block={block} /></div>}
    </div>
  )
}

export function Details(): JSX.Element | null {
  const s = useSyncExternalStore(details.subscribe, details.get)
  const l = useSyncExternalStore(list.subscribe, list.get)
  const entryId = l.selectedId
  const value = details.descriptor(s)
  const loading = details.isLoading('descriptor', s)
  const fault = details.fault('descriptor', s)
  const narrow = details.narrow(s)
  const entry = entryId !== null ? list.entry(entryId) : null

  /* The descriptor, read when the pane opens on an entry and again when the
     list says the entry moved on. Nothing is asked while an answer is out. */
  const permitted = details.mayRead(s, { what: 'descriptor' })
  const needs = permitted && entryId !== null && (value === null || s.stale) && !loading && !fault
  const identityKey = s.current ? details.descriptorKey(s.current) : null
  useLayoutEffect(() => {
    if (needs) void details.loadDescriptor()
  }, [needs, entryId, s.stale, identityKey])

  /* A drag still under way when the pane closes or turns narrow is let go
     where it stands: nothing is placed by a hand the pane no longer answers.
     Before the bar is placed below, so it is placed from the card let go. */
  const gesture = useRef<AbortController | null>(null)
  useLayoutEffect(() => () => gesture.current?.abort(), [s.open, narrow])
  const card = useRef<HTMLElement | null>(null)
  const place = details.detailsPlace(s)
  const width = details.detailsWidthPx(s)
  const height = details.detailsHeightPx(s)
  /* Every place and size the store gives the card; the hand's own moves, which
     the store only hears at the end, ask in `drag`. */
  useLayoutEffect(() => {
    followBar(card.current)
  }, [place?.x, place?.y, width, height, narrow, s.areaWidth, s.areaHeight])
  /* Set when a press on the head became a drag, and read by the head's capture
     handler: the click that ends the gesture belongs to the drag, not to the
     button under the hand. */
  const dragged = useRef(false)
  /* The head is mostly its name and three buttons, so a press there becomes a
     drag only past DRAG_THRESHOLD, and capture is taken then -- held
     from the press, it would retarget the button's click to the head. The card
     moves by a transform while the hand is down and is placed on release: the
     waterfall and the pane's own views read this store, and a write per pointer
     move would redraw them every frame for a card that only changed place. */
  const drag = (e: ReactPointerEvent<HTMLDivElement>): void => {
    const handle = e.currentTarget
    const el = handle.parentElement
    const box = el?.parentElement?.getBoundingClientRect()
    const rect = el?.getBoundingClientRect()
    if (e.button !== 0 || !el || !box || !rect) return
    e.preventDefault()
    e.stopPropagation()
    dragged.current = false
    const from = { x: rect.left - box.left, y: rect.top - box.top }
    const start = { x: e.clientX, y: e.clientY }
    const pointerId = e.pointerId
    gesture.current?.abort()
    const controller = new AbortController()
    gesture.current = controller
    controller.signal.addEventListener('abort', () => {
      el.style.transform = ''
      delete handle.dataset.dragging
      if (handle.hasPointerCapture(pointerId)) handle.releasePointerCapture(pointerId)
    })
    let at: details.Place | null = null
    const move = (ev: PointerEvent): void => {
      if (ev.pointerId !== pointerId) return
      const dx = ev.clientX - start.x
      const dy = ev.clientY - start.y
      if (at === null) {
        if (Math.abs(dx) + Math.abs(dy) < details.DRAG_THRESHOLD) return
        dragged.current = true
        handle.dataset.dragging = 'true'
        handle.setPointerCapture(pointerId)
      }
      at = details.holdPlace({ x: from.x + dx, y: from.y + dy }, rect.height)
      el.style.transform = `translate(${at.x - from.x}px, ${at.y - from.y}px)`
      followBar(el)
    }
    const finish = (ev: PointerEvent): void => {
      if (ev.pointerId !== pointerId) return
      controller.abort()
      if (at === null) return
      details.dropDetails(at, rect.height, homeTop(el))
      followBar(el)
    }
    window.addEventListener('pointermove', move, { signal: controller.signal })
    window.addEventListener('pointerup', finish, { signal: controller.signal })
    window.addEventListener('pointercancel', finish, { signal: controller.signal })
    handle.addEventListener('lostpointercapture', finish, { signal: controller.signal })
  }

  if (!s.open || entryId === null) return null
  const kind = value?.kind ?? entry?.kind ?? ''
  const closeLabel = t(narrow ? 'gui.trajectory.details.back' : 'gui.trajectory.details.close')
  const pin = details.pinned(s)
  const here = value !== null && pin !== null && pin === details.tabOf(entryId)
  const pinLabel = t(here ? 'gui.trajectory.details.unpin' : 'gui.trajectory.details.pin')
  const target = entry ? scopes.openTarget(entry) : null
  const jump = target === null || entry === null || scopes.landingOf(target) === null
    ? null
    : list.opensOf(entry) !== null
      ? t('gui.trajectory.scope.open')
      : t('gui.trajectory.scope.from', { title: scopes.scopeTitle(scopes.scopeDoor(l, target)) })
  return (
    <aside
      className={narrow ? 'trajectory-details trajectory-details-over' : 'trajectory-details'}
      role="region"
      aria-label={t('gui.trajectory.details.region')}
      ref={card}
      style={narrow ? undefined : { width, height: height ?? undefined, left: place?.x, top: place?.y }}
      data-moved={narrow || place === null ? undefined : ''}
    >
      {narrow ? null : <Corner moved={place !== null} />}
      <div
        className="trajectory-head"
        onPointerDown={narrow ? undefined : drag}
        onClickCapture={(clicked) => {
          if (!dragged.current) return
          dragged.current = false
          clicked.preventDefault()
          clicked.stopPropagation()
        }}
      >
        {kind ? <span className={`trajectory-tag ${kindClass(kind)}`}>{kindLabel(kind)}</span> : null}
        <span className="trajectory-head-name" title={value ? shortName(value, entry) : undefined}>{value ? shortName(value, entry) : (entry?.span_name ?? '')}</span>
        {jump !== null && entry ? (
          <button className="trajectory-scope-jump" title={jump} onClick={() => { if (scopes.openFromEntry(entry)) scopes.focusList() }}>
            {jump}
          </button>
        ) : null}
        {value ? (
          <button className="trajectory-pin" aria-pressed={here} aria-label={pinLabel} title={pinLabel} onClick={() => details.togglePin(entryId)}>
            <Icon icon={Pin02Icon} size={15} />
          </button>
        ) : null}
        <button className="trajectory-close" aria-label={closeLabel} title={closeLabel} onClick={() => details.closeDetails()}>
          {narrow ? t('gui.trajectory.details.back') : '✕'}
        </button>
      </div>
      {value ? <Tabs entryId={entryId} blocks={value.blocks} /> : null}
      {s.unstable ? (
        <p className="trajectory-fault" role="status">
          {t('gui.trajectory.details.unstable')}
          {' '}
          <button className="trajectory-link" onClick={() => { void details.retryDescriptor() }}>{t('gui.trajectory.details.retry')}</button>
        </p>
      ) : null}
      {value
        ? <Pane entryId={entryId} value={value} />
        : fault
          ? (
            <p className="trajectory-fault" role="alert">
              {t('gui.trajectory.details.failed', { detail: fault })}
              {' '}
              <button className="trajectory-link" onClick={() => { void details.retryDescriptor() }}>{t('gui.trajectory.details.retry')}</button>
            </p>
          )
          : s.waitingEpoch !== null
            ? <p className="trajectory-sec-note" role="status">{t('gui.trajectory.details.waiting_list')}</p>
            : (
            <div className="trajectory-skel" aria-busy="true" aria-label={t('gui.trajectory.details.loading')}>
              <div className="trajectory-skel-line" /><div className="trajectory-skel-line" /><div className="trajectory-skel-line trajectory-skel-short" />
            </div>
          )}
    </aside>
  )
}
