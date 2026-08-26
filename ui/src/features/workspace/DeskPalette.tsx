/** Draggable floating navigation palette for workspace content. */

import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { AgentList } from '../subagents/SubagentsPage'
import * as agents from '../subagents/store'
import { t } from '../../shell/bridge'
import { DeskIcon } from './DeskIcon'
import {
  anchoredGeometry,
  clampGeometry,
  defaultGeometry,
  DESK_GEOMETRY_KEY,
  magnetGeometry,
} from './deskGeometry'
import * as deliveries from './deliveries'
import * as desk from './deskStore'
import { fileKind } from './store'
import * as workspace from './store'

import type { DeskGeometry, DeskTab } from './deskTypes'
import type { DeliveryRow } from './types'
import type { CSSProperties, JSX, PointerEvent as ReactPointerEvent } from 'react'

/* The three tabs, each carrying what is new in it. The bubble is the same thing
   on all three -- changes written, files delivered, background work started --
   because a reader who is on one tab has exactly one question about the other
   two, and it is the same question.

   The label is marked with a class rather than left to `span:last-child`: the
   bubble is a sibling, and the moment one existed the positional selector
   stopped matching the label, which put the full label on every collapsed
   34px tab. */
/* Slow on purpose: this feeds a number on a tab, not a spinner. */
const AGENTS_POLL_MS = 8000

function DeskTabs({ value, onChange }: { value: DeskTab; onChange: (tab: DeskTab) => void }): JSX.Element {
  return (
    <div className="desk-tabs" role="tablist">
      {(['diff', 'deliverables', 'agents'] as DeskTab[]).map((tab) => {
        const label = tab === 'diff' ? 'Diff'
          : tab === 'deliverables' ? t('gui.ws.deliverables') : t('gui.ws.agents')
        const fresh = desk.unseen(tab)
        /* The count goes in the button's OWN name. An explicit `aria-label`
           replaces the whole subtree as the accessible name, so a label on the
           bubble inside it is never announced -- and `<i>` maps to a generic
           role, where `aria-label` does not apply at all. Hidden from the tree
           afterwards so the number is not read twice. */
        const name = fresh ? `${label}, ${t('gui.ws.unseen_tab', { n: String(fresh) })}` : label
        return (
          <button key={tab} role="tab" aria-label={name} aria-selected={value === tab} onClick={() => onChange(tab)}>
            <DeskIcon kind={tab} />
            <span className="lb">{label}</span>
            {fresh ? <i className="desk-count" aria-hidden="true">{fresh}</i> : null}
          </button>
        )
      })}
    </div>
  )
}

function DiffNav(): JSX.Element {
  const changes = workspace.shared().changes
  if (!changes.length) return <div className="desk-empty">{t('gui.ws.no_changes')}</div>
  return (
    <div className="desk-list">
      {changes.map((change) => (
        <button key={`${change.key}:${change.turn}`} className="desk-row desk-diff-row" onClick={() => desk.openDeskDiff(change)}>
          <i className={`chgc ${change.kind}`}>{t(`gui.ws.chip.${change.kind}`)}</i>
          <span className="desk-name" title={change.key}>{change.dir}<b>{change.name}</b></span>
          <span className="chgs">
            {change.add ? <i className="a">+{change.add}</i> : null}
            {change.del ? <i className="d">−{change.del}</i> : null}
          </span>
        </button>
      ))}
    </div>
  )
}

function DeliverableRow({ row, here }: { row: DeliveryRow; here: boolean }): JSX.Element {
  const size = deliveries.humanSize(row.size)
  /* A row recovered from the gateway's registry has no turn to name -- it says
     what the file is and leaves the reading position out rather than inventing
     one. */
  const when = here || row.turn == null ? '' : t('gui.ws.dlv_turn', { n: String(row.turn) })
  const meta = [row.name, size, when].filter(Boolean).join(' \u00b7 ')
  return (
    <button
      className={'desk-row desk-dlv-row' + (row.missing ? ' gone' : '')}
      title={row.description || row.path}
      /* Through the workspace's own opener, not straight at the desk: that is
         the door the transcript's delivery card uses, and it is what keeps a
         source that cannot read files (the demo shell) answering with its own
         note instead of a viewer that has nothing to show.
         Left clickable on purpose when the file is gone: the viewer says so in
         words, which is more than a dead row does. */
      onClick={() => workspace.openDelivery(row.path, row.downloadPath)}
    >
      <span className="dlv-kind" data-kind={fileKind(row.name)}>
        {row.ext ? row.ext.slice(0, 4).toUpperCase() : t('gui.arts.file')}
      </span>
      <span className="desk-name">
        <b>{row.title}</b>
        <s>{meta}</s>
      </span>
      {row.missing ? <i className="dlv-gone">{t('gui.arts.missing')}</i> : null}
    </button>
  )
}

/* Everything this session handed over, newest turn first. Not the working
   directory: a file only reaches this list by being delivered, which is the
   difference between what Raven wrote and what it gave you. */
function DeliverablesNav(): JSX.Element {
  useSyncExternalStore(deliveries.subscribe, deliveries.getVersion)
  const rows = deliveries.list()
  if (!rows.length) return (
    <div className="desk-empty desk-dlv-empty">
      <DeskIcon kind="deliverables" />
      <b>{t('gui.ws.dlv_none')}</b>
      <span>{t('gui.ws.dlv_none_sub')}</span>
    </div>
  )
  const now = workspace.currentTurn()
  const out: JSX.Element[] = []
  let group: string | null = null
  rows.forEach((row) => {
    const here = row.turn === now
    const key = here ? 'gui.ws.turn_now' : 'gui.ws.turn_earlier'
    if (key !== group) {
      group = key
      out.push(<div key={`grp:${key}:${row.path}`} className="desk-grp">{t(key)}</div>)
    }
    out.push(<DeliverableRow key={row.path} row={row} here={here} />)
  })
  return <div className="desk-list">{out}</div>
}

function AgentsNav(): JSX.Element {
  const state = useSyncExternalStore(agents.subscribe, agents.getState)
  useEffect(() => {
    agents.refreshInstances()
    agents.refreshRoster()
  }, [])
  return <AgentList s={state} onOpen={desk.openDeskAgent} compact />
}

function storedGeometry(): DeskGeometry {
  try {
    const saved = JSON.parse(localStorage.getItem(DESK_GEOMETRY_KEY) || '') as Partial<DeskGeometry>
    if ([saved.x, saved.y, saved.w, saved.h].every(Number.isFinite)) {
      return clampGeometry({
        x: saved.x!, y: saved.y!, w: saved.w!, h: saved.h!, detached: saved.detached === true,
      })
    }
  } catch {}
  return defaultGeometry()
}

export function DeskPalette(): JSX.Element | null {
  const state = useSyncExternalStore(desk.subscribe, desk.getState)
  /* On the root, not on the tab bodies: the tab strip reads all three sources
     for its bubbles and is drawn whichever tab is showing, so subscribing only
     where each body mounts left every badge out of the one case it exists for
     -- something landing while the reader is on another tab. (The workspace's
     own changes arrive through `DeskApp`, which subscribes to that store.) */
  useSyncExternalStore(deliveries.subscribe, deliveries.getVersion)
  useSyncExternalStore(agents.subscribe, agents.getState)
  /* Looking at a tab is what makes its contents no longer new -- including what
     lands while the reader is sitting on it, which is why this runs on every
     render rather than only on a switch. */
  useEffect(() => {
    if (state.paletteOpen) desk.seeTab(state.tab)
  })
  /* The other two tabs are told what happened: a write reaches the workspace
     record and a delivery reaches the registry, both on the turn's own events.
     Background work is not -- the instance list is asked for, by the panel when
     it draws and by a resume, and nothing asks while a turn quietly spawns a
     sub-agent. So the tab that is NOT showing would have counted zero for the
     whole run and badged nothing.
     Asked here rather than wired into the turn stream because the list is
     already a polled thing with its own floor (2.5s in the agents store, which
     rate-limits this), and the alternative is a second definition of "something
     started" living in the event handler. Only while the palette is open, and
     only for the tab the reader is not on -- the panel refreshes its own. */
  useEffect(() => {
    if (!state.paletteOpen || state.tab === 'agents') return
    const ask = (): void => { void agents.refreshInstances() }
    ask()
    const timer = setInterval(ask, AGENTS_POLL_MS)
    return () => clearInterval(timer)
  }, [state.paletteOpen, state.tab])
  const pointerCleanup = useRef<(() => void) | null>(null)
  const [geom, setGeom] = useState(storedGeometry)
  useEffect(() => {
    try { localStorage.setItem(DESK_GEOMETRY_KEY, JSON.stringify(geom)) } catch {}
  }, [geom])
  useEffect(() => {
    const resize = (): void => setGeom((value) => clampGeometry(value))
    window.addEventListener('resize', resize)
    return () => {
      window.removeEventListener('resize', resize)
      pointerCleanup.current?.()
    }
  }, [])
  const drag = (event: ReactPointerEvent<HTMLDivElement>): void => {
    if ((event.target as HTMLElement).closest('button')) return
    event.preventDefault()
    pointerCleanup.current?.()
    const palette = event.currentTarget.closest('.desk-palette')
    const rect = palette?.getBoundingClientRect()
    const start = {
      pointerX: event.clientX,
      pointerY: event.clientY,
      x: rect?.left ?? geom.x,
      y: rect?.top ?? geom.y,
    }
    const target = event.currentTarget
    const pointerId = event.pointerId
    const controller = new AbortController()
    target.dataset.dragging = 'true'
    target.setPointerCapture(pointerId)
    const move = (e: globalThis.PointerEvent): void => {
      if (e.pointerId !== pointerId) return
      setGeom((now) => magnetGeometry({
        ...now,
        x: start.x + e.clientX - start.pointerX,
        y: start.y + e.clientY - start.pointerY,
      }))
    }
    const finish = (e?: globalThis.PointerEvent): void => {
      if (e && e.pointerId !== pointerId) return
      target.dataset.dragging = 'false'
      if (target.hasPointerCapture(pointerId)) target.releasePointerCapture(pointerId)
      controller.abort()
      if (pointerCleanup.current === cleanup) pointerCleanup.current = null
    }
    const cleanup = (): void => finish()
    pointerCleanup.current = cleanup
    window.addEventListener('pointermove', move, { signal: controller.signal })
    window.addEventListener('pointerup', finish, { signal: controller.signal })
    window.addEventListener('pointercancel', finish, { signal: controller.signal })
    target.addEventListener('lostpointercapture', finish, { signal: controller.signal })
  }
  const resize = (event: ReactPointerEvent<HTMLDivElement>): void => {
    event.preventDefault()
    event.stopPropagation()
    pointerCleanup.current?.()
    const start = { pointerX: event.clientX, pointerY: event.clientY, ...geom }
    const target = event.currentTarget
    const pointerId = event.pointerId
    const controller = new AbortController()
    target.setPointerCapture(pointerId)
    const move = (e: globalThis.PointerEvent): void => {
      if (e.pointerId !== pointerId) return
      setGeom((now) => clampGeometry({
        ...now,
        w: start.w + e.clientX - start.pointerX,
        h: start.h + e.clientY - start.pointerY,
      }))
    }
    const finish = (e?: globalThis.PointerEvent): void => {
      if (e && e.pointerId !== pointerId) return
      if (target.hasPointerCapture(pointerId)) target.releasePointerCapture(pointerId)
      controller.abort()
      if (pointerCleanup.current === cleanup) pointerCleanup.current = null
    }
    const cleanup = (): void => finish()
    pointerCleanup.current = cleanup
    window.addEventListener('pointermove', move, { signal: controller.signal })
    window.addEventListener('pointerup', finish, { signal: controller.signal })
    window.addEventListener('pointercancel', finish, { signal: controller.signal })
    target.addEventListener('lostpointercapture', finish, { signal: controller.signal })
  }
  if (!state.paletteOpen) return null
  return (
    <div
      className="desk-palette"
      data-anchored={!geom.detached}
      style={{
        ...(geom.detached ? { left: geom.x, top: geom.y } : {}),
        width: geom.w,
        height: geom.h,
      } as CSSProperties}
    >
      <div className="desk-drag" onPointerDown={drag}>
        <DeskTabs value={state.tab} onChange={(tab) => desk.update({ tab })} />
      </div>
      <div className="desk-body">
        {state.tab === 'diff' ? <DiffNav /> : state.tab === 'deliverables' ? <DeliverablesNav /> : <AgentsNav />}
      </div>
      <div className="desk-resize" onPointerDown={resize} />
    </div>
  )
}
