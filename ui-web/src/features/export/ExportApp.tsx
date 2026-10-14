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
import { ExportPage } from './ExportPage'
import * as store from './store'
import { TurnsPage } from './TurnsPage'
import './styles.css'

import type { Fault, PageId } from './store'
import type { JSX } from 'react'

const STEP_NAME: Record<PageId, string> = {
  turns: 'gui.export.step_turns',
  export: 'gui.export.step_export',
}

function faultText(fault: Fault): string {
  switch (fault.kind) {
    case 'load': return t('gui.export.load_failed', { error: fault.detail })
    case 'changed': return t('gui.export.selection_changed')
    case 'limit': return t('gui.export.limit')
    case 'disabled': return t('gui.export.disabled')
    case 'gone': return t('gui.export.gone')
    case 'error': return t('gui.export.failed', { error: fault.detail })
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
    <div className="export-app">
      <header className="export-top">
        <h2 className="export-title">{t('gui.export.title_export')}</h2>
        {s.pages.length > 1 ? (
          <ol className="export-steps">
            {s.pages.map((id, i) => (
              <li key={id} className="export-step" data-state={i < at ? 'done' : i === at ? 'current' : 'upcoming'}>
                <span className="export-dot">{i < at ? '✓' : String(i + 1)}</span>
                <span>{t(STEP_NAME[id])}</span>
              </li>
            ))}
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
        {s.page === 'turns' ? <TurnsPage s={s} /> : <ExportPage s={s} onEnter={begin} />}
      </div>
      <footer className="export-foot">
        {at > 0 && !busy && !done ? (
          <button type="button" className="export-btn export-ghost" onClick={store.back}>{t('gui.export.back')}</button>
        ) : null}
        <span className="export-grow" />
        {s.error ? <span className="export-err" role="alert">{faultText(s.error)}</span> : null}
        {s.page === 'turns' ? (
          <button
            type="button"
            className="export-btn export-primary"
            disabled={!s.listed || !s.selected.length}
            onClick={store.next}
          >
            {t('gui.export.next')}
          </button>
        ) : done ? (
          <button type="button" className="export-btn export-primary" onClick={store.close}>{t('gui.export.finish')}</button>
        ) : (
          <button type="button" className="export-btn export-primary" disabled={busy} onClick={begin}>
            {s.job ? t('gui.export.retry') : t('gui.export.start')}
          </button>
        )}
      </footer>
    </div>
  )
}
