/* The problem, as `raven trajectory report-bug` asks for it: a description,
   how bad it is, what was expected and what happened, how to get there, and
   who is reporting. Every field is uncontrolled and writes the store's draft
   (store.editProblem), so typing re-renders nothing and the fields come back
   as they were left when the page does. Once filed, the fields lock and the
   page says the snapshot is being collected and redacted; a report that
   failed or was cancelled elsewhere says why here, and can be filed again. */

import { t } from '../../i18n/t'
import * as store from './store'

import type { ExportState } from './store'
import type { Severity } from './types'
import type { JSX, KeyboardEvent } from 'react'

const SEVERITIES: Array<[Severity, string]> = [
  ['low', 'gui.export.severity_low'],
  ['medium', 'gui.export.severity_medium'],
  ['high', 'gui.export.severity_high'],
  ['critical', 'gui.export.severity_critical'],
]

type TextField = 'description' | 'expected' | 'actual' | 'steps'

function Area({ field, label, rows, locked, epoch }: {
  field: TextField
  label: string
  rows: number
  locked: boolean
  epoch: number
}): JSX.Element {
  return (
    <label className="export-field">
      <span>{label}</span>
      <textarea
        key={epoch}
        className="export-text"
        rows={rows}
        defaultValue={store.problemDraft()[field]}
        disabled={locked}
        onInput={(e) => store.editProblem(field, e.currentTarget.value)}
      />
    </label>
  )
}

export function ProblemPage({ s }: { s: ExportState }): JSX.Element {
  const draft = store.problemDraft()
  const locked = store.working(s)
  const status = s.report?.status
  const ended = status?.phase === 'failed' || status?.phase === 'cancelled'
  /* Enter in the one single-line field files the report, unless an IME
     composition is still open; in the multi-line ones it is a new line. */
  const enter = (e: KeyboardEvent<HTMLInputElement>): void => {
    if (e.key !== 'Enter' || e.nativeEvent.isComposing || e.keyCode === 229) return
    e.preventDefault()
    void store.startReport()
  }
  return (
    <div className="export-page">
      <Area field="description" label={t('gui.export.description')} rows={5} locked={locked} epoch={s.epoch} />
      <p className="export-hint">{t('gui.export.description_hint')}</p>
      <label className="export-field">
        <span>{t('gui.export.severity')}</span>
        <select
          key={s.epoch}
          className="export-select"
          defaultValue={draft.severity}
          disabled={locked}
          onChange={(e) => store.editProblem('severity', e.currentTarget.value as Severity)}
        >
          {SEVERITIES.map(([value, key]) => <option key={value} value={value}>{t(key)}</option>)}
        </select>
      </label>
      <Area field="expected" label={t('gui.export.expected')} rows={3} locked={locked} epoch={s.epoch} />
      <Area field="actual" label={t('gui.export.actual')} rows={3} locked={locked} epoch={s.epoch} />
      <Area field="steps" label={t('gui.export.steps')} rows={3} locked={locked} epoch={s.epoch} />
      <label className="export-field">
        <span>{t('gui.export.reporter')}</span>
        <input
          key={s.epoch}
          className="export-name"
          type="text"
          defaultValue={draft.reporter}
          disabled={locked}
          onInput={(e) => store.editProblem('reporter', e.currentTarget.value)}
          onKeyDown={enter}
        />
      </label>
      {s.reportStarting || status?.phase === 'collecting' ? (
        <p className="export-state"><span className="export-spin" aria-hidden="true" />{t('gui.export.collecting')}</p>
      ) : null}
      {s.report?.lost ? (
        <p className="export-lost" role="status">
          <span>{t('gui.export.lost', { error: s.report.lost })}</span>
          <button type="button" className="export-btn export-ghost" onClick={store.requery}>{t('gui.export.retry_status')}</button>
        </p>
      ) : null}
      {ended ? (
        <p className="export-state" data-bad="true">
          {status?.phase === 'cancelled'
            ? t('gui.export.report_cancelled')
            : t('gui.export.report_failed', { error: status?.error?.message ?? '' })}
        </p>
      ) : null}
    </div>
  )
}
