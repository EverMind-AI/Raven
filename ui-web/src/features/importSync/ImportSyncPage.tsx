/* The import row in the rail's foot: one line saying what is happening, the
   number on the right, a thin bar. Which agent, how many items, which one
   failed -- none of that is said here; the row is a status, not a report.
   Mounted by src/main.tsx into #importRow, the slot the rail's foot keeps for
   it (src/chrome/Rail.tsx). */

import { useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import * as store from './store'

import type { RowKind } from './store'
import type { JSX } from 'react'
import './styles.css'

/* Literal keys, so the i18n gate can read each one. */
const LABEL: Record<Exclude<RowKind, 'hidden' | 'wrap'>, string> = {
  scan: 'gui.importSync.scan',
  run: 'gui.importSync.run',
  paused: 'gui.importSync.paused',
  done: 'gui.importSync.done',
}
const PHASE: Record<'profile' | 'skills', string> = {
  profile: 'gui.importSync.profile',
  skills: 'gui.importSync.skills',
}

function Close(): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18" /></svg>
  )
}

export function ImportSyncApp(): JSX.Element | null {
  const s = useSyncExternalStore(store.subscribe, store.get)
  useSyncExternalStore(lang.subscribe, lang.get)
  const v = store.view(s)
  if (v.kind === 'hidden') return null
  const running = v.kind === 'run' || v.kind === 'wrap'
  const label = v.kind === 'wrap' && v.phase ? t(PHASE[v.phase.kind]) : t(LABEL[v.kind as Exclude<RowKind, 'hidden' | 'wrap'>])
  const count = v.kind === 'scan' ? ''
    : v.kind === 'wrap' && v.phase ? `${v.phase.current}/${v.phase.total}`
    : v.kind === 'done' ? (v.failed ? t('gui.importSync.failed_n', { n: v.failed }) : '')
    : `${v.pct}%`
  const action = v.kind === 'paused' ? t('gui.importSync.resume') : t('gui.importSync.retry')
  return (
    <div className={`importSync importSync-${v.kind}${v.failed ? ' importSync-warn' : ''}`} role="status">
      <div className="importSync-l1">
        <button
          type="button"
          className="importSync-main"
          disabled={!v.clickable}
          title={v.clickable ? action : undefined}
          aria-label={v.clickable ? action : undefined}
          onClick={() => void store.resume()}
        >
          <span className="importSync-led" aria-hidden="true" />
          <span className="importSync-nm">{label}</span>
          {count ? <span className="importSync-pc">{count}</span> : null}
        </button>
        {running ? (
          <button type="button" className="importSync-x" aria-label={t('gui.importSync.stop')} onClick={() => void store.stop()}><Close /></button>
        ) : v.kind === 'done' && !v.failed ? (
          <button type="button" className="importSync-x" aria-label={t('gui.importSync.dismiss')} onClick={store.dismiss}><Close /></button>
        ) : null}
      </div>
      <div className="importSync-bar"><i style={{ width: `${v.kind === 'scan' ? 32 : v.pct}%` }} /></div>
    </div>
  )
}
