/* The export wizard, drawn into #exportBody inside the #exportVeil dialog that
 * src/App.tsx renders: a header with the flow's steps and the close button, the
 * page in the middle, and the foot that moves between pages.
 *
 * The veil's `data-open` is written here, from the store, the way the
 * onboarding island raises #onb: the dialog's frame is the page's, and whether
 * it is up is this domain's. The close button and Escape both go through
 * `dismiss` (Escape by clicking #exportClose, src/state/escapeOrder.ts), which
 * asks before stopping work under way.
 */

import { useEffect, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import { DecidePage } from './DecidePage'
import { ExportPage } from './ExportPage'
import { ProblemPage } from './ProblemPage'
import * as store from './store'
import { TurnsPage } from './TurnsPage'
import { UploadPage } from './UploadPage'
import './styles.css'

import type { ExportState, Fault, PageId } from './store'
import type { JSX } from 'react'

const STEP_NAME: Record<PageId, string> = {
  turns: 'gui.export.step_turns',
  export: 'gui.export.step_export',
  problem: 'gui.export.step_problem',
  decide: 'gui.export.step_decide',
  upload: 'gui.export.step_upload',
}

function faultText(fault: Fault, s: ExportState): string {
  switch (fault.kind) {
    case 'load': return t('gui.export.load_failed', { error: fault.detail })
    case 'changed': return t('gui.export.selection_changed')
    case 'limit': return t('gui.export.limit')
    case 'report_limit': return t('gui.export.report_limit')
    case 'disabled': return t('gui.export.disabled')
    case 'unavailable': return t('gui.export.unavailable')
    case 'gone': return t('gui.export.gone')
    case 'report_gone': return t('gui.export.report_gone')
    case 'conflict': return t('gui.export.conflict')
    case 'not_acknowledged': return t('gui.export.not_acknowledged')
    case 'undecided': return t('gui.export.undecided')
    case 'risk': return t('gui.export.risk_needed')
    case 'error':
      return s.flow === 'report'
        ? t('gui.export.report_failed', { error: fault.detail })
        : t('gui.export.failed', { error: fault.detail })
  }
}

function stepState(s: ExportState, id: PageId, i: number, at: number): 'done' | 'skipped' | 'current' | 'upcoming' {
  if (s.skipped.includes(id)) return 'skipped'
  return i < at ? 'done' : i === at ? 'current' : 'upcoming'
}

const CHECK = '✓'

/* The foot's main button: what moves this page on. */
function Primary({ s }: { s: ExportState }): JSX.Element {
  const busy = store.working(s)
  const button = (label: string, onClick: () => void, disabled = false): JSX.Element => (
    <button type="button" className="export-btn export-primary" disabled={disabled} onClick={onClick}>{label}</button>
  )
  switch (s.page) {
    case 'turns':
      return button(t('gui.export.next'), store.next, !s.listed || !s.selected.length)
    case 'export':
      if (s.job?.status?.state === 'done') return button(t('gui.export.finish'), store.close)
      return button(s.job ? t('gui.export.retry') : t('gui.export.start'), () => void store.start(), busy)
    case 'problem': {
      if (s.probeWaiting) return button(t('gui.export.probe_checking'), () => undefined, true)
      const blocked = busy || !s.problemReady || !s.selected.length
      if (store.probeRefused(s)) return button(t('gui.export.probe_continue'), store.continueAnyway, blocked)
      return button(
        s.report && store.reportOver(s.report) ? t('gui.export.restart') : t('gui.export.next'),
        () => void store.startReport(),
        blocked,
      )
    }
    case 'decide':
      return button(
        s.acting ? t('gui.export.applying') : t('gui.export.next'),
        () => void store.decide(),
        s.acting || s.report?.status?.phase !== 'deciding' || !store.acknowledgedAll(s),
      )
    case 'upload': {
      const status = s.report?.status
      if (status?.phase === 'uploaded') return button(t('gui.export.finish'), store.close)
      if (status?.phase === 'upload_failed' && status.error?.retryable) {
        return button(t('gui.export.retry'), () => void store.retryUpload(), s.acting)
      }
      const review = status?.summary?.classification === 'needs_review'
      return button(
        t('gui.export.upload'),
        () => void store.upload(),
        s.acting || status?.phase !== 'ready' || (review && !s.riskAccepted),
      )
    }
  }
}

/* The receiver check, before a report starts: why the receiver cannot be used
   and that the reader may still go on, or that a start is waiting on the check. */
function ProbeNotice({ s }: { s: ExportState }): JSX.Element | null {
  if (s.flow !== 'report' || s.report !== null) return null
  const shown = store.probeShown(s)
  if (shown?.state === 'down' || shown?.state === 'failed') {
    const text = shown.state === 'down'
      ? t('gui.export.probe_down', { target: shown.target, reason: shown.reason })
      : shown.detail === null ? t('gui.export.probe_late') : t('gui.export.probe_failed', { error: shown.detail })
    return (
      <div className="export-warn export-probe" role="status">
        <p>{text}</p>
        <p>{t('gui.export.probe_note')}</p>
        <button
          type="button"
          className="export-btn export-ghost"
          disabled={s.probe?.state === 'checking'}
          onClick={store.recheckProbe}
        >
          {t('gui.export.probe_recheck')}
        </button>
      </div>
    )
  }
  if (s.probeWaiting) return <div className="export-warn export-probe" role="status">{t('gui.export.probe_checking')}</div>
  return null
}

function Page({ s, begin }: { s: ExportState; begin: () => void }): JSX.Element {
  switch (s.page) {
    case 'turns': return <TurnsPage s={s} />
    case 'export': return <ExportPage s={s} onEnter={begin} />
    case 'problem': return <ProblemPage s={s} />
    case 'decide': return <DecidePage s={s} />
    case 'upload': return <UploadPage s={s} />
  }
}

export function ExportApp(): JSX.Element | null {
  useSyncExternalStore(lang.subscribe, lang.get)
  const s = useSyncExternalStore(store.subscribe, store.get)

  useEffect(() => {
    const veil = document.getElementById('exportVeil')
    if (veil) veil.dataset.open = String(s.open)
    if (s.open) document.getElementById('exportModal')?.focus()
  }, [s.open])

  if (!s.open) return null
  const at = s.pages.indexOf(s.page)
  const busy = store.working(s)
  const done = s.job?.status?.state === 'done'
  const begin = (): void => void store.start()

  return (
    <div className="export-app" data-flow={s.flow}>
      <header className="export-top">
        <h2 className="export-title">{s.flow === 'report' ? t('gui.export.title_report') : t('gui.export.title_export')}</h2>
        {s.pages.length > 1 ? (
          <ol className="export-steps">
            {s.pages.map((id, i) => {
              const state = stepState(s, id, i, at)
              return (
                <li key={id} className="export-step" data-state={state}>
                  <span className="export-dot">{state === 'done' ? CHECK : state === 'skipped' ? '-' : String(i + 1)}</span>
                  <span>{t(STEP_NAME[id])}</span>
                </li>
              )
            })}
          </ol>
        ) : <span className="export-grow" />}
        <button
          type="button"
          className="export-x"
          id="exportClose"
          aria-label={t('gui.export.close')}
          onClick={store.dismiss}
        >
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
            <path d="M6 6l12 12M18 6L6 18" />
          </svg>
        </button>
      </header>
      <div className="export-main">
        <ProbeNotice s={s} />
        <Page s={s} begin={begin} />
      </div>
      <footer className="export-foot">
        {at > 0 && !busy && !s.probeWaiting && !done && s.page !== 'decide' && s.page !== 'upload' ? (
          <button type="button" className="export-btn export-ghost" onClick={store.back}>{t('gui.export.back')}</button>
        ) : null}
        {s.page === 'decide' ? (
          <button type="button" className="export-btn export-ghost" onClick={store.dismiss}>{t('gui.export.cancel')}</button>
        ) : null}
        <span className="export-grow" />
        {s.error ? <span className="export-err" role="alert">{faultText(s.error, s)}</span> : null}
        <Primary s={s} />
      </footer>
    </div>
  )
}
