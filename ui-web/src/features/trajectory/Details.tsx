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
import { useEffect, useId, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'

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
import type { JSX, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react'

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
   gap under the stats, the activity strip and the ruler. `--wf-card-top` is the
   sheet's own formula; a browser may hand the calc() back as written, so the
   three bands are summed when it cannot be read as one number. Absent bands
   leave the old top under the stats and the gap, and a sheet that says nothing
   gives NaN. */
const homeTop = (card: Element): number => {
  const style = getComputedStyle(card)
  const top = Number.parseFloat(style.getPropertyValue('--wf-card-top'))
  if (Number.isFinite(top)) return top
  const px = (name: string): number => Number.parseFloat(style.getPropertyValue(name))
  const act = px('--wf-act-h')
  const ruler = px('--wf-ruler-h')
  return px('--wf-stats-h') + details.CARD_GAP + (Number.isFinite(act) ? act : 0) + (Number.isFinite(ruler) ? ruler : 0)
}

/* The card's corner, which sizes it from the edges that are free: where the
   pane opens its right edge is pinned, so the hand goes left to widen it, and
   once moved its left edge is, so the hand goes right. The top is pinned
   either way, and a pull takes no more than the room from the pinned edges to
   the area's -- left to the store, the pane would make that room by climbing. */
/* The card's size in words: its width, and its height once the reader set one -- until then it
   fills the room under the header, which the stylesheet sizes. */
const sizeText = (width: number, height: number | null): string =>
  height === null ? t('gui.trajectory.details.size_full', { width }) : t('gui.trajectory.details.size', { width, height })

/* The head's own controls: a press on one belongs to that control, not to the
   drag, so the drag must neither start there nor cancel the default -- canceling
   a pointerdown suppresses the mousedown that would focus the button. */
const CONTROL = 'button, a, input, select, textarea, [role="button"]'
const controlOf = (target: EventTarget | null): HTMLElement | null =>
  target instanceof Element ? target.closest<HTMLElement>(CONTROL) : null

function Corner({ moved, width, height }: { moved: boolean; width: number; height: number | null }): JSX.Element {
  const gesture = useRef<AbortController | null>(null)
  const sizeId = useId()
  const keysId = useId()
  /* What a keyboard step left the card at, read out once per step; the hand's pulls are not, or
     every pointer move would be spoken. */
  const [spoken, setSpoken] = useState('')
  useEffect(() => () => gesture.current?.abort(), [])
  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>): void => {
    const el = e.currentTarget
    const card = el.parentElement
    const box = card?.parentElement?.getBoundingClientRect()
    const rect = card?.getBoundingClientRect()
    if (e.button !== 0 || !box || !rect) return
    /* The handle takes the focus itself, since it cancels the default that
       would give it one, so the arrows land on it after a press. */
    el.focus()
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
      const dy = ev.clientY - start.y
      /* A pull with no vertical travel changes only the width: an auto height
         stays auto, or the card would stop filling the room it sits in. */
      const height = details.detailsHeightPx() === null && dy === 0 ? null : Math.min(rect.height + dy, down)
      details.resizeDetails(moved ? Math.min(rect.width + dx, right) : rect.width - dx, height)
    }
    const finish = (ev: PointerEvent): void => {
      if (ev.pointerId === pointerId) controller.abort()
    }
    window.addEventListener('pointermove', move, { signal: controller.signal })
    window.addEventListener('pointerup', finish, { signal: controller.signal })
    window.addEventListener('pointercancel', finish, { signal: controller.signal })
    el.addEventListener('lostpointercapture', finish, { signal: controller.signal })
  }
  /* The arrows move the free edges a step at a time, under the same rule as
     the hand: the left edge where the pane opens, the right one once moved,
     and the bottom, never past the room to the area's edge. */
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>): void => {
    const card = e.currentTarget.parentElement
    const box = card?.parentElement?.getBoundingClientRect()
    const rect = card?.getBoundingClientRect()
    if (!box || !rect) return
    const step = details.RESIZE_STEP
    const wider = moved ? 'ArrowRight' : 'ArrowLeft'
    const narrower = moved ? 'ArrowLeft' : 'ArrowRight'
    const dw = e.key === wider ? step : e.key === narrower ? -step : 0
    const dh = e.key === 'ArrowDown' ? step : e.key === 'ArrowUp' ? -step : 0
    if (dw === 0 && dh === 0) return
    e.preventDefault()
    e.stopPropagation()
    /* The size the store gives the card, not the box: a press can follow the
       last one before the card has been laid out again. */
    const nextWidth = details.detailsWidthPx() + dw
    const right = box.right - rect.left - details.CARD_GAP
    const down = box.bottom - rect.top - details.CARD_GAP
    const current = details.detailsHeightPx()
    /* An arrow that only moves the width leaves an auto height auto, as the
       hand's own width-only pull does. */
    const nextHeight = current === null && dh === 0 ? null : Math.min((current ?? rect.height) + dh, down)
    details.resizeDetails(moved ? Math.min(nextWidth, right) : nextWidth, nextHeight)
    setSpoken(sizeText(details.detailsWidthPx(), details.detailsHeightPx()))
  }
  /* A button, not a separator: a focusable separator is a range along one axis, and this handle
     sets two. Its description says both sizes and which keys move them. */
  return (
    <div className="trajectory-corner" role="button" aria-label={t('gui.trajectory.details.resize')}
      aria-describedby={`${sizeId} ${keysId}`} tabIndex={0} onPointerDown={onPointerDown} onKeyDown={onKeyDown}>
      <span id={sizeId} hidden>{sizeText(width, height)}</span>
      <span id={keysId} hidden>{t('gui.trajectory.details.resize_keys')}</span>
      <span className="trajectory-sr" aria-live="polite">{spoken}</span>
    </div>
  )
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
    if (e.button !== 0) return
    /* A new press starts a new gesture: a drag that ended without a click must
       not swallow the next button's click. */
    dragged.current = false
    /* A press on one of the head's buttons is the button's: no drag starts, no
       default is canceled, and the control takes the focus -- explicitly, so the
       pane holds it in every environment, not only where a mousedown follows. */
    const control = controlOf(e.target)
    if (control !== null && handle.contains(control)) {
      control.focus()
      return
    }
    const el = handle.parentElement
    const box = el?.parentElement?.getBoundingClientRect()
    const rect = el?.getBoundingClientRect()
    if (!el || !box || !rect) return
    /* The head's background becomes the drag hand, so the pane itself takes the
       focus: Escape pressed after the press must find the pane, not the list. */
    el.focus()
    e.preventDefault()
    e.stopPropagation()
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
  /* The pane is focusable by script, not by Tab: a press on the head's
     background gives it the focus, which is what Escape's layer asks for. */
  return (
    <aside
      className={narrow ? 'trajectory-details trajectory-details-over' : 'trajectory-details'}
      role="region"
      aria-label={t('gui.trajectory.details.region')}
      ref={card}
      tabIndex={-1}
      style={narrow ? undefined : { width, height: height ?? undefined, left: place?.x, top: place?.y }}
      data-moved={narrow || place === null ? undefined : ''}
    >
      {narrow ? null : <Corner moved={place !== null} width={width} height={height} />}
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
