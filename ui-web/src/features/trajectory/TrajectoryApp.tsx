/* The trajectory island: what `#trajHost` shows while the trajectory view is
 * up. A status line for what the index wants said, then the agent waterfall
 * with the details pane floating over it.
 *
 * The root subscribes to the language itself, like every island, and to the
 * store for the few facts the status line reads; the waterfall and the pane
 * have their own subscriptions so a scroll in one does not redraw the other. The
 * root also measures its own size for the pane: how wide the card may be,
 * whether the area is too narrow to leave the list any of its width, and the
 * bounds the card is moved and sized within.
 */

import { Component, useCallback, useRef, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import * as lang from '../../state/lang'
import { Details } from './Details'
import * as details from './detailStore'
import * as store from './store'
import { Waterfall, mib } from './waterfall/Waterfall'
import './styles.css'

import type { ErrorInfo, JSX, ReactNode } from 'react'

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

/* The list at its whole width, the pane floating over its right side -- or
   over all of it when the area is narrow. Open or not is the details store's;
   the card's size and place are the store's clamp of the reader's own numbers
   against the area measured here. */
function Body(): JSX.Element {
  /* Only what the body itself says: the card's size and place are the card's,
     and the corner writes them on every pointer move -- read whole, the store
     redrew every row under the card each frame. */
  const open = useSyncExternalStore(details.subscribe, () => details.get().open)
  const narrow = useSyncExternalStore(details.subscribe, () => details.narrow())
  const observer = useRef<ResizeObserver | null>(null)
  const attach = useCallback((el: HTMLDivElement | null): void => {
    observer.current?.disconnect()
    observer.current = null
    if (!el) return
    const measure = (): void => {
      details.setAreaWidth(el.clientWidth)
      details.setAreaHeight(el.clientHeight)
    }
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    observer.current = ro
  }, [])
  return (
    <div className="trajectory-body" ref={attach} data-details={open ? '' : undefined} data-narrow={narrow ? '' : undefined}>
      <Waterfall />
      <Details />
    </div>
  )
}

/* The island render throws, the page loses the whole column -- a blank page
   with no way back and no message. This keeps the failure where it happened:
   the reader sees what went wrong and can try again. */
export class CrashBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state: { error: Error | null } = { error: null }

  static getDerivedStateFromError(error: Error): { error: Error } {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error('trajectory island:', error, info.componentStack)
  }

  render(): ReactNode {
    if (this.state.error === null) return this.props.children
    return (
      <div className="trajectory-crash" role="alert">
        <div className="trajectory-crash-text">{t('gui.trajectory.crash', { detail: this.state.error.message })}</div>
        <button className="trajectory-wf-tool" onClick={() => this.setState({ error: null })}>
          {t('gui.trajectory.crash_retry')}
        </button>
      </div>
    )
  }
}

export function TrajectoryApp(): JSX.Element {
  useSyncExternalStore(lang.subscribe, lang.get)
  return (
    <div className="trajectory-root">
      <Status />
      <CrashBoundary>
        <Body />
      </CrashBoundary>
    </div>
  )
}
