/* The line a report page shows while the report's status cannot be read: why,
   and a button that asks again now. The store keeps the same report and keeps
   asking on its own, and nothing that changes the report can be sent until an
   answer lands, so this is the only thing the reader can do meanwhile. */

import { t } from '../../i18n/t'
import * as store from './store'

import type { ExportState } from './store'
import type { JSX } from 'react'

export function ReportLost({ s }: { s: ExportState }): JSX.Element | null {
  if (!s.report?.lost) return null
  return (
    <p className="export-lost" role="status">
      <span>{t('gui.export.lost', { error: s.report.lost })}</span>
      <button type="button" className="export-btn export-ghost" onClick={store.requery}>{t('gui.export.retry_status')}</button>
    </p>
  )
}
