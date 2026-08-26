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
function DeskTabs({ value, onChange }: { value: DeskTab; onChange: (tab: DeskTab) => void }): JSX.Element {
  return (
    <div className="desk-tabs" role="tablist">
      {(['deliverables', 'agents', 'diff'] as DeskTab[]).map((tab) => {
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

/* One nothing for all three tabs: the tab's own icon, what is not here, and
   where it would come from. They were two designs -- a line of grey text on
   Diff, an illustrated block on the shelf -- and an empty tab is the state a
   reader sees most often, so the two read as two different kinds of nothing. */
function DeskEmpty({ kind, title, hint }: { kind: DeskTab; title: string; hint: string }): JSX.Element {
  return (
    <div className="desk-empty">
      <DeskIcon kind={kind} />
      <b>{title}</b>
      <span>{hint}</span>
    </div>
  )
}

function DiffNav(): JSX.Element {
  const changes = workspace.shared().changes
  if (!changes.length) {
    return <DeskEmpty kind="diff" title={t('gui.ws.no_changes')} hint={t('gui.ws.no_changes_sub')} />
  }
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
  if (!rows.length) {
    return <DeskEmpty kind="deliverables" title={t('gui.ws.dlv_none')} hint={t('gui.ws.dlv_none_sub')} />
  }
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
  /* Answered here rather than left to the shared list: `AgentList` is the
     standalone page's list as well, and its own note belongs to that page. In
     the desk, one nothing looks like the other two. */
  if (!state.instances.length && !state.roster.length) {
    return (
      <DeskEmpty
        kind="agents"
        title={t('gui.ws.agents_none')}
        hint={t(agents.absent() ? 'gui.ws.agents_absent' : 'gui.ws.agents_none_sub')}
      />
    )
  }
  return <AgentList s={state} onOpen={desk.openDeskAgent} compact />
}

/* Long enough to read as a movement, short enough that a reader who wanted the
   desk gone does not wait for it. Kept in step with the `desk-out` animation in
   page.css -- the number is here because this is what decides when the palette
   stops existing, and the stylesheet has no say in that. */
const EXIT_MS = 130

/* One animation's worth of life after the reader shuts it.
 *
 * A leaving element has to be on screen to leave, and React's answer to "it is
 * closed" is that it is not rendered -- so the palette outlives the flag by
 * exactly the length of the exit and then goes. `null` is the third state and
 * not a synonym for closed: it is the only one where nothing is in the DOM.
 *
 * The reader can also change their mind mid-exit, which the cleanup covers:
 * opening again cancels the pending removal and the phase goes straight back to
 * `in`, so a double-click on the launcher lands on an open desk rather than one
 * that vanishes a moment later. */
function usePhase(shown: boolean): 'in' | 'out' | null {
  const [phase, setPhase] = useState<'in' | 'out' | null>(shown ? 'in' : null)
  useEffect(() => {
    if (shown) {
      setPhase('in')
      return
    }
    setPhase((now) => (now === 'in' ? 'out' : null))
    /* Armed either way rather than only for a palette that was up: an updater
       does not run until React renders, so nothing here can read the phase it
       just asked for -- and a timer that lands on an already-null phase sets it
       to null again, which React drops. */
    const timer = setTimeout(() => setPhase(null), EXIT_MS)
    return () => clearTimeout(timer)
  }, [shown])
  return phase
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
  /* Not `state.paletteOpen`: a fullscreen pane is the whole window and the desk
     is not on it (deskStore.showing). Everything below reads this one answer,
     the marks included -- a tab marked seen behind a fullscreen pane is news
     the reader never saw. */
  const shown = desk.showing()
  const phase = usePhase(shown)
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
    if (shown) desk.seeTab(state.tab)
  })
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
  if (!phase) return null
  return (
    <div
      className="desk-palette"
      data-anchored={!geom.detached}
      /* Which way it is moving, for the stylesheet -- and genuinely inert
         while it leaves. A tab clicked on the way out would act on a desk the
         reader has already dismissed, a screen reader has no reason to be told
         about a strip that is going, and a focused tab would be left holding
         focus inside it. `inert` is the one attribute that answers all three;
         `aria-hidden` answers only the middle one. */
      data-phase={phase}
      inert={phase === 'out' || undefined}
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
