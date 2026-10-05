/* The trajectory island: what `#trajHost` shows while the trajectory view is
 * up. A status line for what the index wants said, the list, and the strip
 * the duration bar will take.
 *
 * The root subscribes to the language itself, like every island, and to the
 * store for the few facts the status line reads; the list has its own
 * subscription so a scroll does not redraw the frame around it.
 */

import { useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import { EntryList, mib } from './EntryList'
import * as store from './store'
import './styles.css'

import type { JSX } from 'react'

function Status(): JSX.Element | null {
  const s = useSyncExternalStore(store.subscribe, store.get)
  const lines: JSX.Element[] = []
  const index = s.indexState
  if (index && index.phase !== 'ready' && s.entries.length) {
    lines.push(<span key="scan">{t('gui.trajectory.scanning', { done: mib(index.scanned_bytes), total: mib(index.total_bytes) })}</span>)
  }
  if (index && index.head_truncated > 0) {
    lines.push(<span key="head">{t('gui.trajectory.truncated', { n: index.head_truncated })}</span>)
  }
  if (index && index.failure) {
    lines.push(<span key="index" className="trajectory-status-bad">{t('gui.trajectory.failure', { detail: index.failure })}</span>)
  }
  if (s.fault) {
    lines.push(<span key="fault" className="trajectory-status-bad">{t('gui.trajectory.fault', { detail: s.fault })}</span>)
  }
  if (s.stateKnown && !s.recording) {
    lines.push(<span key="rec">{t('gui.trajectory.not_recording')}</span>)
  }
  if (!lines.length) return null
  return <div className="trajectory-status" role="status">{lines}</div>
}

export function TrajectoryApp(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <div className="trajectory-root">
      <Status />
      <EntryList />
      <div className="trajectory-bar" aria-hidden="true" />
    </div>
  )
}
