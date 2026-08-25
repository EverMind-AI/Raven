/** Pane chrome and responsive layout for the floating workspace surface. */

import { useEffect, useLayoutEffect, useRef, useSyncExternalStore } from 'react'
import { createPortal } from 'react-dom'

import { AgentRecordConversation, InstanceConversation } from '../subagents/SubagentsPage'
import { t } from '../../shell/bridge'
import { ChgDiff, FileView } from './WorkspacePage'
import * as deliveries from './deliveries'
import { DeskIcon } from './DeskIcon'
import {
  workspaceAvailableWidth,
  workspaceColumnCount,
  workspaceTransitionWidth,
} from './deskGeometry'
import * as desk from './deskStore'
import * as workspace from './store'

import type { DeskPane } from './deskTypes'
import type { DeliveryRow } from './types'
import type { CSSProperties, JSX, PointerEvent as ReactPointerEvent } from 'react'

function FullscreenIcon({ active }: { active: boolean }): JSX.Element {
  return active ? (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M19 10h-5V5M5 14h5v5M13 11l6-6M11 13l-6 6" />
    </svg>
  ) : (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <path d="M14 5h5v5M10 19H5v-5M19 5l-6 6M5 19l6-6" />
    </svg>
  )
}

/* What a delivered file is, above what it contains. Only a path this session
   handed over gets one: the same viewer opened from an answer's path chip is
   showing a file, not a product, and has nothing to put here. */
function DeliveryStrip({ row }: { row: DeliveryRow }): JSX.Element {
  const size = deliveries.humanSize(row.size)
  const when = row.turn === workspace.currentTurn()
    ? t('gui.ws.dlv_here')
    : t('gui.ws.dlv_turn', { n: String(row.turn) })
  return (
    <div className="dlv-strip">
      <b>{row.title}</b>
      {row.description ? <p>{row.description}</p> : null}
      <div className="dlv-meta">
        {row.ext ? <i>{row.ext.toUpperCase()}</i> : null}
        {size ? <i>{size}</i> : null}
        <i>{when}</i>
        {row.missing ? <i className="bad">{t('gui.arts.missing')}</i> : null}
      </div>
    </div>
  )
}

function Pane({ pane }: { pane: DeskPane }): JSX.Element {
  const state = useSyncExternalStore(desk.subscribe, desk.getState)
  useSyncExternalStore(deliveries.subscribe, deliveries.getVersion)
  /* Looked up rather than carried on the pane: a file reaches this viewer from
     the shelf, from the transcript's delivery card, from a changed-file row and
     from a path in an answer, and it is one pane whichever door was used. */
  const delivery = pane.kind === 'file' ? deliveries.byPath(pane.file.path) : null
  const full = state.solo === pane.id
  const title = pane.kind === 'agent'
    ? pane.row.title || pane.row.nodeId || pane.row.handle
    : pane.kind === 'agent-record'
      ? pane.row.node || pane.row.label || pane.row.id || t('gui.ws.agents')
      : pane.kind === 'file' ? pane.file.path.split('/').pop() || pane.file.path : pane.change.name
  return (
    <section
      className="desk-pane"
      data-active={state.active === pane.id}
      onPointerDown={() => desk.setActive(pane.id)}
    >
      <header>
        <DeskIcon kind={pane.kind === 'agent' || pane.kind === 'agent-record' ? 'agents' : pane.kind} />
        <b title={title}>{title}</b>
        {pane.kind === 'agent' || pane.kind === 'agent-record'
          ? (
            <span className="pane-meta">
              {[pane.kind === 'agent' ? pane.row.runTitle : null, pane.row.agent].filter(Boolean).join(' \u00b7 ')}
            </span>
          )
          : null}
        <span className="pane-spacer" />
        <button
          className="pane-fullscreen"
          onClick={() => desk.toggleSolo(pane.id)}
          aria-label={t(full ? 'gui.ws.restore_panel' : 'gui.ws.expand_panel')}
          title={t(full ? 'gui.ws.restore_panel' : 'gui.ws.expand_panel')}
        >
          <FullscreenIcon active={full} />
        </button>
        <button onClick={() => desk.closePane(pane.id)} aria-label={t('gui.close')}>×</button>
      </header>
      {delivery ? <DeliveryStrip row={delivery} /> : null}
      <div className="desk-pane-body">
        {pane.kind === 'diff' ? <ChgDiff c={pane.change} /> : null}
        {pane.kind === 'file' ? <FileView ws={workspace.shared()} file={pane.file} /> : null}
        {pane.kind === 'agent' ? <InstanceConversation row={pane.row} /> : null}
        {pane.kind === 'agent-record' ? <AgentRecordConversation row={pane.row} /> : null}
      </div>
    </section>
  )
}

function Divider({ axis, side }: { axis: 'column' | 'row'; side?: 'left' | 'right' }): JSX.Element {
  const down = (event: ReactPointerEvent<HTMLDivElement>): void => {
    event.preventDefault()
    const target = event.currentTarget
    const grid = target.parentElement
    if (!grid) return
    const pointerId = event.pointerId
    const controller = new AbortController()
    target.setPointerCapture(pointerId)
    const move = (nextEvent: globalThis.PointerEvent): void => {
      if (nextEvent.pointerId !== pointerId) return
      const rect = grid.getBoundingClientRect()
      const value = axis === 'column'
        ? ((nextEvent.clientX - rect.left) / rect.width) * 100
        : ((nextEvent.clientY - rect.top) / rect.height) * 100
      const next = Math.max(28, Math.min(72, value))
      desk.updateSplits(axis === 'column' ? { column: next } : side === 'right' ? { right: next } : { left: next })
    }
    const finish = (nextEvent?: globalThis.PointerEvent): void => {
      if (nextEvent && nextEvent.pointerId !== pointerId) return
      if (target.hasPointerCapture(pointerId)) target.releasePointerCapture(pointerId)
      controller.abort()
    }
    window.addEventListener('pointermove', move, { signal: controller.signal })
    window.addEventListener('pointerup', finish, { signal: controller.signal })
    window.addEventListener('pointercancel', finish, { signal: controller.signal })
    target.addEventListener('lostpointercapture', finish, { signal: controller.signal })
  }
  return <div className={`desk-divider ${axis} ${side || ''}`} onPointerDown={down} />
}

export function DeskSurface(): JSX.Element | null {
  const state = useSyncExternalStore(desk.subscribe, desk.getState)
  const previousColumns = useRef<0 | 1 | 2>(0)
  const host = document.getElementById('ws')
  const split = document.getElementById('split')
  const count = state.panes.length
  useLayoutEffect(() => {
    if (!host || !split || count <= 0) {
      previousColumns.current = 0
      return
    }
    const nextColumns = workspaceColumnCount(count) as 1 | 2
    const rootStyle = getComputedStyle(document.documentElement)
    const current = parseFloat(rootStyle.getPropertyValue('--wsw')) || host.getBoundingClientRect().width
    const configuredChatMin = parseFloat(rootStyle.getPropertyValue('--chat-min'))
    const available = workspaceAvailableWidth(
      split.getBoundingClientRect().width,
      window.innerWidth,
      Number.isFinite(configuredChatMin) ? configuredChatMin : undefined,
    )
    const next = workspaceTransitionWidth({
      previousWidth: current,
      previousColumns: previousColumns.current,
      nextColumns,
      availableWidth: available,
      firstPane: state.panes[0],
    })
    if (Math.abs(next - current) > 0.5) document.documentElement.style.setProperty('--wsw', `${next}px`)
    previousColumns.current = nextColumns
  }, [count, host, split, state.panes])
  useEffect(() => {
    if (!split || count <= 0) return
    const clamp = (): void => {
      const rootStyle = getComputedStyle(document.documentElement)
      const current = parseFloat(rootStyle.getPropertyValue('--wsw')) || 0
      const configuredChatMin = parseFloat(rootStyle.getPropertyValue('--chat-min'))
      const available = workspaceAvailableWidth(
        split.getBoundingClientRect().width,
        window.innerWidth,
        Number.isFinite(configuredChatMin) ? configuredChatMin : undefined,
      )
      if (current > available) document.documentElement.style.setProperty('--wsw', `${available}px`)
    }
    const observer = new ResizeObserver(clamp)
    observer.observe(split)
    window.addEventListener('resize', clamp)
    clamp()
    return () => {
      observer.disconnect()
      window.removeEventListener('resize', clamp)
    }
  }, [count, split])
  if (!host) return null
  const shown = state.solo ? state.panes.filter((pane) => pane.id === state.solo) : state.panes
  const style = {
    '--desk-col': `${state.splits.column}%`,
    '--desk-left-row': `${state.splits.left}%`,
    '--desk-right-row': `${state.splits.right}%`,
  } as CSSProperties
  return createPortal(
    <div className={`desk-grid n${shown.length}`} data-solo={Boolean(state.solo)} style={style}>
      {shown.map((pane) => <Pane key={pane.id} pane={pane} />)}
      {shown.length >= 3 ? <Divider axis="column" /> : null}
      {shown.length >= 2 ? <Divider axis="row" side="left" /> : null}
      {shown.length === 4 ? <Divider axis="row" side="right" /> : null}
    </div>,
    host,
  )
}

export function DeskFollowToggle(): JSX.Element {
  const state = useSyncExternalStore(desk.subscribe, desk.getState)
  return (
    <button
      className="ghost-ic desk-follow-toggle"
      aria-label={t(state.paletteOpen ? 'gui.collapse_ws' : 'gui.workspace')}
      aria-expanded={state.paletteOpen}
      onClick={desk.toggleDesk}
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
        <path d="M4 7h4M12 7h8M4 12h9M17 12h3M4 17h2M10 17h10" />
        <circle cx="10" cy="7" r="2" />
        <circle cx="15" cy="12" r="2" />
        <circle cx="8" cy="17" r="2" />
      </svg>
    </button>
  )
}
