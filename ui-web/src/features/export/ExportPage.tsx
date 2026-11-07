/* The export page: the file name, the warning that the package is the raw
   trace, and the job from start to download.

   The name field is uncontrolled -- what is typed goes to the store's draft,
   so a keystroke re-renders nothing and an IME composition is never cut short,
   and the field is rebuilt from that draft when the page comes back -- and
   Enter starts the export unless a composition is still open. The
   download is the page's own anchor: clicked once when the job is done, and
   left on the page as "download again" while the dialog stays up. */

import { useEffect, useRef } from 'react'

import { t } from '../../i18n/t'
import * as store from './store'

import type { ExportState } from './store'
import type { JSX, KeyboardEvent } from 'react'

export function size(bytes: number): string {
  const units = ['B', 'KB', 'MB', 'GB']
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024
    unit += 1
  }
  return `${unit ? value.toFixed(value < 10 ? 1 : 0) : Math.round(value)} ${units[unit]}`
}

function Progress({ s }: { s: ExportState }): JSX.Element | null {
  const job = s.job
  if (s.starting) {
    return <p className="export-state"><span className="export-spin" aria-hidden="true" />{t('gui.export.starting')}</p>
  }
  if (!job) return null
  const status = job.status
  if (status?.state === 'failed') {
    return <p className="export-state" data-bad="true">{t('gui.export.failed', { error: status.error ?? '' })}</p>
  }
  if (status?.state === 'cancelled') return <p className="export-state">{t('gui.export.cancelled')}</p>
  if (status?.state === 'done') return null
  return (
    <div className="export-progress">
      <p className="export-state">
        <span className="export-spin" aria-hidden="true" />
        {status?.phase === 'writing' ? t('gui.export.phase_writing') : t('gui.export.phase_collecting')}
      </p>
      {job.lost !== null ? (
        <p className="export-lost" role="status">
          <span>{t('gui.export.lost', { error: job.lost })}</span>
          <button type="button" className="export-btn export-ghost" onClick={store.requery}>
            {t('gui.export.retry_status')}
          </button>
        </p>
      ) : null}
    </div>
  )
}

function Download({ s }: { s: ExportState }): JSX.Element | null {
  const link = useRef<HTMLAnchorElement>(null)
  const job = s.job
  const status = job?.status
  const ready = status?.state === 'done' && Boolean(status.download_url)

  useEffect(() => {
    if (!job || !ready || s.downloaded === job.id) return
    link.current?.click()
    store.markDownloaded(job.id)
  }, [job, ready, s.downloaded])

  if (!job || !status || !ready) return null
  const counts = status.counts
  return (
    <div className="export-done">
      <p>
        {t('gui.export.done', {
          size: size(status.size_bytes ?? 0),
          spans: counts?.spans ?? 0,
          artifacts: counts?.artifacts ?? 0,
          messages: counts?.messages ?? 0,
        })}
      </p>
      <a ref={link} className="export-download" href={status.download_url ?? undefined} download={status.file_name}>
        {t('gui.export.download_again')}
      </a>
    </div>
  )
}

export function ExportPage({ s, onEnter }: { s: ExportState; onEnter: () => void }): JSX.Element {
  const locked = store.working(s) || s.job?.status?.state === 'done'
  const enter = (e: KeyboardEvent<HTMLInputElement>): void => {
    if (e.key !== 'Enter' || e.nativeEvent.isComposing || e.keyCode === 229) return
    e.preventDefault()
    onEnter()
  }
  return (
    <div className="export-page">
      <label className="export-field">
        <span>{t('gui.export.file_name')}</span>
        <input
          key={s.epoch}
          className="export-name"
          type="text"
          spellCheck={false}
          defaultValue={store.fileName(s)}
          disabled={locked}
          onInput={(e) => store.editName(e.currentTarget.value)}
          onKeyDown={enter}
        />
      </label>
      <div className="export-warn" role="note">{t('gui.export.unredacted')}</div>
      <p className="export-count">{t('gui.export.selected_count', { n: s.selected.length })}</p>
      <Progress s={s} />
      <Download s={s} />
    </div>
  )
}
