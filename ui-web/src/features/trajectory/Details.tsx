/* The details pane: what the selected entry is, as a column beside the list
 * -- or over it, when the area is too narrow for two.
 *
 * Three fixed parts and one that scrolls: a head (the kind's tag, the short
 * name the descriptor gives it, and the way out), the tab strip, and the
 * pane the current tab fills with its own scroll position. The grip on its
 * left edge resizes it, by pointer or by arrow key, within the bounds the
 * store clamps to. Closing keeps the selection; the list gets the focus back
 * so the next key goes where the reader was.
 */

import { useLayoutEffect, useRef, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import { BlockView } from './BlockView'
import * as details from './details'
import { Overview } from './Overview'
import { kindClass, kindLabel } from './palette'
import * as list from './store'
import { panelId, tabId, Tabs } from './Tabs'

import type { TrajectoryDetailResult } from './types'
import type { JSX, KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react'

/* The name under the tag: a tool's, a skill's or a model's when the
   descriptor's own blocks carry it in their preview, the span name otherwise. */
export function shortName(value: TrajectoryDetailResult): string {
  for (const [blockId, key] of [['tool', 'name'], ['skill', 'skill.name'], ['model', 'model'], ['plugin', 'plugin.name'], ['agent', 'subagent.label']] as const) {
    const block = value.blocks.find((b) => b.id === blockId)
    const preview = block?.preview
    if (!Array.isArray(preview)) continue
    const item = preview.find((p) => p !== null && typeof p === 'object' && (p as { key?: unknown }).key === key) as { value?: unknown } | undefined
    if (item && (typeof item.value === 'string' || typeof item.value === 'number')) return String(item.value)
  }
  return value.span_name
}

/* The seam between list and pane. Dragging it left widens the pane; so does
   the left arrow, in steps. The store clamps what is asked for. */
function Grip(): JSX.Element {
  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>): void => {
    e.preventDefault()
    const el = e.currentTarget
    const startX = e.clientX
    const start = details.detailsWidthPx()
    el.setPointerCapture(e.pointerId)
    el.dataset.drag = 'true'
    const move = (ev: PointerEvent): void => { details.setWidth(start - (ev.clientX - startX)) }
    const up = (): void => {
      el.removeEventListener('pointermove', move)
      el.removeEventListener('pointerup', up)
      el.removeEventListener('pointercancel', up)
      delete el.dataset.drag
    }
    el.addEventListener('pointermove', move)
    el.addEventListener('pointerup', up)
    el.addEventListener('pointercancel', up)
  }
  const onKeyDown = (e: ReactKeyboardEvent<HTMLDivElement>): void => {
    if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return
    e.preventDefault()
    const at = details.detailsWidthPx()
    details.setWidth(e.key === 'ArrowLeft' ? at + details.RESIZE_STEP : at - details.RESIZE_STEP)
  }
  return (
    <div
      className="trajectory-grip"
      role="separator"
      aria-orientation="vertical"
      aria-label={t('gui.trajectory.details.resize')}
      tabIndex={0}
      onPointerDown={onPointerDown}
      onKeyDown={onKeyDown}
    />
  )
}

export { Grip }

function Pane({ entryId, value }: { entryId: string; value: TrajectoryDetailResult }): JSX.Element {
  const tab = details.tabOf(entryId)
  const box = useRef<HTMLDivElement>(null)
  /* Each tab of each entry keeps its own place; a tab opened for the first
     time starts at the top, which is what an absent entry means. */
  useLayoutEffect(() => {
    const el = box.current
    if (el) el.scrollTop = details.tabScroll(entryId, tab)
  }, [entryId, tab])
  const block = tab === 'overview' ? null : value.blocks.find((b) => b.id === tab)
  return (
    <div
      className="trajectory-pane"
      ref={box}
      onScroll={(e) => details.setTabScroll(entryId, tab, e.currentTarget.scrollTop)}
    >
      {tab === 'overview' || !block
        ? <Overview entryId={entryId} value={value} />
        : <div role="tabpanel" id={panelId(tab)} aria-labelledby={tabId(tab)}><BlockView block={block} /></div>}
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
  const needs = s.open && entryId !== null && (value === null || s.stale) && !loading && !fault
  const identityKey = s.current ? details.descriptorKey(s.current) : null
  useLayoutEffect(() => {
    if (needs) void details.loadDescriptor()
  }, [needs, entryId, s.stale, identityKey])

  if (!s.open || entryId === null) return null
  const kind = value?.kind ?? entry?.kind ?? ''
  const closeLabel = t(narrow ? 'gui.trajectory.details.back' : 'gui.trajectory.details.close')
  return (
    <aside
      className={narrow ? 'trajectory-details trajectory-details-over' : 'trajectory-details'}
      role="region"
      aria-label={t('gui.trajectory.details.region')}
      style={narrow ? undefined : { width: details.detailsWidthPx(s) }}
    >
      <div className="trajectory-head">
        {kind ? <span className={`trajectory-tag ${kindClass(kind)}`}>{kindLabel(kind)}</span> : null}
        <span className="trajectory-head-name" title={value ? shortName(value) : undefined}>{value ? shortName(value) : (entry?.span_name ?? '')}</span>
        <button className="trajectory-close" aria-label={closeLabel} title={closeLabel} onClick={() => details.closeDetails()}>
          {narrow ? t('gui.trajectory.details.back') : '✕'}
        </button>
      </div>
      {value ? <Tabs entryId={entryId} blocks={value.blocks} /> : null}
      {s.unstable ? <p className="trajectory-fault" role="status">{t('gui.trajectory.details.unstable')}</p> : null}
      {value
        ? <Pane entryId={entryId} value={value} />
        : fault
          ? (
            <p className="trajectory-fault" role="alert">
              {t('gui.trajectory.details.failed', { detail: fault })}
              {' '}
              <button className="trajectory-link" onClick={() => { void details.loadDescriptor({ fresh: true }) }}>{t('gui.trajectory.details.retry')}</button>
            </p>
          )
          : (
            <div className="trajectory-skel" aria-busy="true" aria-label={t('gui.trajectory.details.loading')}>
              <div className="trajectory-skel-line" /><div className="trajectory-skel-line" /><div className="trajectory-skel-line trajectory-skel-short" />
            </div>
          )}
    </aside>
  )
}
