/* The upload: what will ship, the risk to accept when the report still needs
   review, and the upload itself to its receipt. A report that landed but was
   not delivered says where it is kept and how to send it later from the
   command line, so closing the wizard never loses it. */

import { t } from '../../i18n/t'
import { size } from './ExportPage'
import * as store from './store'

import type { ExportState } from './store'
import type { JSX } from 'react'

export function UploadPage({ s }: { s: ExportState }): JSX.Element {
  const status = s.report?.status
  const summary = status?.summary
  const phase = status?.phase
  const id = status?.bug_report_id ?? ''
  return (
    <div className="export-page">
      {phase === 'finishing' || !summary ? (
        <p className="export-state"><span className="export-spin" aria-hidden="true" />{t('gui.export.finishing')}</p>
      ) : (
        <dl className="export-summary">
          <dt>{t('gui.export.summary_id')}</dt>
          <dd className="export-mono">{id}</dd>
          {summary.size_bytes ? (
            <>
              <dt>{t('gui.export.summary_size')}</dt>
              <dd>{size(summary.size_bytes)}</dd>
            </>
          ) : null}
          <dt>{t('gui.export.summary_redaction')}</dt>
          <dd>
            {t('gui.export.summary_counts', {
              auto: summary.auto_replaced,
              kept: summary.kept,
              redacted: summary.redacted,
              acknowledged: summary.acknowledged,
            })}
          </dd>
          <dt>{t('gui.export.completeness')}</dt>
          <dd>{summary.completeness}</dd>
        </dl>
      )}
      {summary?.warnings.length ? (
        <ul className="export-warnings">{summary.warnings.map((warning, i) => <li key={i}>{warning}</li>)}</ul>
      ) : null}
      {phase === 'ready' && summary?.classification === 'needs_review' ? (
        <label className="export-risk">
          <input type="checkbox" checked={s.riskAccepted} onChange={(e) => store.acceptRisk(e.currentTarget.checked)} />
          <span>{t('gui.export.risk')}</span>
        </label>
      ) : null}
      {phase === 'uploading' ? (
        <p className="export-state"><span className="export-spin" aria-hidden="true" />{t('gui.export.uploading')}</p>
      ) : null}
      {phase === 'uploaded' && status?.receipt ? (
        <div className="export-done">
          <p>{t('gui.export.uploaded', { receipt: status.receipt.receipt_id })}</p>
          {status.receipt.duplicate ? <p className="export-hint">{t('gui.export.duplicate')}</p> : null}
        </div>
      ) : null}
      {phase === 'upload_failed' && status?.error ? (
        <p className="export-state" data-bad="true">{t('gui.export.upload_failed', { error: status.error.message })}</p>
      ) : null}
      {status?.record_dir && phase !== 'uploaded' ? (
        <p className="export-hint">{t('gui.export.kept_local', { dir: status.record_dir, id })}</p>
      ) : null}
      {s.report?.lost ? (
        <p className="export-lost" role="status">
          <span>{t('gui.export.lost', { error: s.report.lost })}</span>
          <button type="button" className="export-btn export-ghost" onClick={store.requery}>{t('gui.export.retry_status')}</button>
        </p>
      ) : null}
    </div>
  )
}
