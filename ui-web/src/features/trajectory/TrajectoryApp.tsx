/* The trajectory island: what `#trajHost` shows while the trajectory view is
 * up. A status line for what the index wants said, the list with the details
 * pane beside it, and the strip the duration bar will take.
 *
 * The root subscribes to the language itself, like every island, and to the
 * store for the few facts the status line reads; the list and the pane have
 * their own subscriptions so a scroll in one does not redraw the other. The
 * root also measures its own width for the pane: how wide the pane may be,
 * and whether the area is too narrow for two columns at all.
 */

import { useCallback, useRef, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import { Details, Grip } from './Details'
import * as details from './detailStore'
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

/* The list and the pane side by side, or the pane over the list when the
   area is narrow. Open or not is the details store's; the width it is given
   is the store's clamp of the reader's own number against the area measured
   here. */
function Body(): JSX.Element {
  const d = useSyncExternalStore(details.subscribe, details.get)
  const observer = useRef<ResizeObserver | null>(null)
  const attach = useCallback((el: HTMLDivElement | null): void => {
    observer.current?.disconnect()
    observer.current = null
    if (!el) return
    const measure = (): void => { details.setAreaWidth(el.clientWidth) }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    observer.current = ro
  }, [])
  const narrow = details.narrow(d)
  return (
    <div className="trajectory-body" ref={attach} data-details={d.open ? '' : undefined} data-narrow={narrow ? '' : undefined}>
      <EntryList />
      {d.open && !narrow ? <Grip /> : null}
      <Details />
    </div>
  )
}

export function TrajectoryApp(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <div className="trajectory-root">
      <Status />
      <Body />
      <div className="trajectory-bar" aria-hidden="true" />
    </div>
  )
}
