/* The overview tab: the entry's type and status, the notes the index
 * attached, and one section per block -- its title as a button that opens
 * the block's own tab, then the preview the descriptor carried or the reason
 * there is none, then "see all" when the preview is a cut.
 *
 * The sections are the descriptor's `blocks` in its order, the same array
 * the tab strip reads, so the two cannot drift. Status is the owning
 * operation's, named in words beside its colour; an input entry whose call
 * failed says so here without being drawn as a failure of its own.
 */

import { t } from '../../i18n/t'
import { availabilityKey, evidenceKey, noteKey, previewSummary, reasonKey, statusKey } from './blocks'
import { PreviewView } from './BlockView'
import * as details from './details'
import { kindClass, kindLabel } from './palette'
import { blockTitle, panelId, tabId } from './Tabs'

import type { TrajectoryBlockDescriptor, TrajectoryDetailResult } from './types'
import type { JSX } from 'react'

function Section({ entryId, block }: { entryId: string; block: TrajectoryBlockDescriptor }): JSX.Element {
  const summary = previewSummary(block)
  const open = (): void => { details.setTab(entryId, block.id) }
  const reasonText = block.availability !== 'available'
    ? (() => { const key = (block.reason && reasonKey(block.reason)) || availabilityKey(block.availability); return key ? t(key) : (block.reason ?? block.availability) })()
    : null
  return (
    <section className="trajectory-sec" aria-labelledby={`trajectory-h-${block.id}`}>
      <h3 className="trajectory-h-row">
        <button className="trajectory-h" id={`trajectory-h-${block.id}`} onClick={open}>{blockTitle(block)}</button>
        {block.related_operation ? <span className="trajectory-h-from">{t('gui.trajectory.details.from_operation', { op: block.related_operation })}</span> : null}
      </h3>
      {reasonText !== null
        ? <p className="trajectory-sec-note">{reasonText}</p>
        : <div className="trajectory-sec-body"><PreviewView block={block} /></div>}
      {summary.clipped ? (
        <button className="trajectory-link trajectory-see-all" onClick={open}>
          {summary.total !== null ? t('gui.trajectory.details.see_all_n', { n: summary.total }) : t('gui.trajectory.details.see_all')}
        </button>
      ) : null}
    </section>
  )
}

export function Overview({ entryId, value }: { entryId: string; value: TrajectoryDetailResult }): JSX.Element {
  const status = statusKey(value.operation_status)
  const evidence = value.status_evidence.map((code) => { const key = evidenceKey(code); return key ? t(key) : code })
  return (
    <div className="trajectory-overview" role="tabpanel" id={panelId('overview')} aria-labelledby={tabId('overview')}>
      <dl className="trajectory-info">
        <div className="trajectory-info-row">
          <dt>{t('gui.trajectory.details.type_label')}</dt>
          <dd><span className={`trajectory-tag ${kindClass(value.kind)}`}>{kindLabel(value.kind)}</span>{value.kind !== kindLabel(value.kind) ? <span className="trajectory-info-sub">{value.span_name}</span> : null}</dd>
        </div>
        <div className="trajectory-info-row">
          <dt>{t('gui.trajectory.details.status_label')}</dt>
          <dd>
            <span className={`trajectory-status-${value.operation_status}`}>{status ? t(status) : value.operation_status}</span>
            {evidence.length ? <span className="trajectory-info-sub">{evidence.join(', ')}</span> : null}
          </dd>
        </div>
      </dl>
      {value.notes.length ? (
        <ul className="trajectory-notes">
          {value.notes.map((code) => { const key = noteKey(code); return <li key={code}>{key ? t(key) : code}</li> })}
        </ul>
      ) : null}
      {value.blocks.map((block) => <Section key={block.id} entryId={entryId} block={block} />)}
    </div>
  )
}
