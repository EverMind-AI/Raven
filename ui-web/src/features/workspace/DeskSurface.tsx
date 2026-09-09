/** Pane chrome and responsive layout for the floating workspace surface. */

import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore } from 'react'
import { createPortal, flushSync } from 'react-dom'

import { AgentRecordConversation, InstanceConversation } from '../subagents/SubagentsPage'
import { InstanceMode } from '../subagents/InstanceMode'
import * as agents from '../subagents/store'
import { t } from '../../shell/bridge'
import { ChgDiff, FileView } from './WorkspacePage'
import * as deliveries from './deliveries'
import { DeskIcon } from './DeskIcon'
import { dragProposal, slotRects } from './deskDrag'
import {
  workspaceAvailableWidth,
  workspaceColumnCount,
  workspaceTransitionWidth,
} from './deskGeometry'
import * as desk from './deskStore'
import * as workspace from './store'

import type { DeskArrangement, SlotRect } from './deskDrag'
import type { DeskPane } from './deskTypes'
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

interface PaneProps {
  pane: DeskPane
  /* The header is the handle: the body scrolls and selects, the chrome moves
     the window. Wired from the surface, which owns the pointer for the whole
     gesture -- a drag crosses pane boundaries by definition. */
  onGrab: (id: string, event: ReactPointerEvent<HTMLElement>) => void
  refPane: (id: string, el: HTMLElement | null) => void
}

function Pane({ pane, onGrab, refPane }: PaneProps): JSX.Element {
  const state = useSyncExternalStore(desk.subscribe, desk.getState)
  /* The instance as the list has it NOW, not as it was when the pane opened.
     `pane.row` is the snapshot the desk stored on the way in, and an instance
     the reader started themselves has no name until its first message lands --
     which is after the pane exists. Headed by the snapshot, such a pane kept the
     handle forever while the composer one file over, which re-reads the list,
     had already started addressing it by name: two names for one instance on one
     screen. The pane falls back to its snapshot for a row the list has since
     dropped, so a forgotten instance's window keeps its heading. */
  const live = useSyncExternalStore(agents.subscribe, agents.getState)
  const row = pane.kind === 'agent'
    ? live.instances.find((it) => it.agent === pane.row.agent && it.handle === pane.row.handle) || pane.row
    : null
  const full = state.solo === pane.id
  const title = pane.kind === 'agent'
    ? row!.title || row!.nodeId || row!.handle
    : pane.kind === 'agent-record'
      /* What it did before what it is called. `label` is the node's own summary
         for a graph node and the run's label for a spawn; `node` is the plan's
         slug, a name for the machine. Read the other way round, a graph node's
         pane was headed by its id whatever the run knew about it. */
      ? pane.row.label || pane.row.node || pane.row.id || t('gui.ws.agents')
      : pane.kind === 'file' ? pane.file.path.split('/').pop() || pane.file.path : pane.change.name
  return (
    <section
      ref={(el) => refPane(pane.id, el)}
      className="desk-pane"
      data-active={state.active === pane.id}
      onPointerDown={() => desk.setActive(pane.id)}
    >
      <header onPointerDown={(event) => onGrab(pane.id, event)}>
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
        {/* Live instances only. A record is a run that already happened, and the
            mode it ran under is not a thing a reader can still change. */}
        {pane.kind === 'agent' ? <InstanceMode row={row!} /> : null}
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

/* Whether the stylesheet is actually showing more than one pane. Below 840px
   the grid collapses to the active pane alone (page.css's narrow block), and
   there is no layout for a drag to rearrange -- a grab there lifted the
   visible pane over a phantom grid and silently rewrote an arrangement the
   reader could not see. Asked of the CSS's own outcome rather than of a
   matchMedia mirror of it: the breakpoint then cannot drift from the guard,
   and an embedding whose window metrics disagree with its layout (one was
   met) still gets the answer the reader is looking at. */
const twoShowing = (els: Iterable<HTMLElement>): boolean => {
  let showing = 0
  for (const el of els) {
    if (getComputedStyle(el).display !== 'none' && ++showing >= 2) return true
  }
  return false
}

/* One drag, from grab to settle. Everything the move handler needs is closed
   over here rather than kept in React state: the pointer moves at the frame
   rate, and the only state a frame is allowed to touch is the drop indicator,
   which changes when the proposal does and not when the pointer does. */
interface DragRun {
  id: string
  pointerId: number
  startX: number
  startY: number
  dx: number
  dy: number
  lifted: boolean
  gridRect: DOMRect
  halfGap: number
  proposal: DeskArrangement | null
  controller: AbortController
}

const SETTLE_MS = 220

/* The slot `id` occupies under `next`, in grid coordinates. */
function slotOf(next: DeskArrangement, id: string, splits: { column: number; left: number; right: number },
  width: number, height: number, halfGap: number): SlotRect | null {
  const at = next.order.indexOf(id)
  if (at < 0) return null
  return slotRects(next.order.length, next.duo, splits, width, height, halfGap)[at] ?? null
}

export function DeskSurface(): JSX.Element | null {
  const state = useSyncExternalStore(desk.subscribe, desk.getState)
  const previousColumns = useRef<0 | 1 | 2>(0)
  const gridRef = useRef<HTMLDivElement | null>(null)
  const paneEls = useRef(new Map<string, HTMLElement>())
  const dragRef = useRef<DragRun | null>(null)
  /* One pending cleanup per settling pane. Held so a re-grab inside the settle
     window can cancel it: left to fire, the previous drop's timer stripped the
     lift off the pane currently in hand, and the pane in flight painted UNDER
     its siblings for the rest of the gesture. */
  const settleTimers = useRef(new Map<string, number>())
  const [drop, setDrop] = useState<SlotRect | null>(null)
  const refPane = (id: string, el: HTMLElement | null): void => {
    if (el) paneEls.current.set(id, el)
    else paneEls.current.delete(id)
  }

  /* Land every pane, animated, after the drop (or the cancel) decided what the
     desk now is. FLIP: the untransformed offsets are measured before the store
     moves anything, the arrangement is applied synchronously, and each pane is
     handed the inverse of how far it jumped, then released to glide to zero.
     Transforms only -- a pane's size snaps to its new slot rather than being
     scaled through the transition, because a scale would distort a pane's
     whole body for the duration and these bodies are text. */
  const settleDrag = (run: DragRun, next: DeskArrangement | null): void => {
    const first = new Map<string, { left: number; top: number }>()
    paneEls.current.forEach((el, paneId) => first.set(paneId, { left: el.offsetLeft, top: el.offsetTop }))
    if (next) flushSync(() => desk.arrange(next.order, next.duo))
    paneEls.current.forEach((el, paneId) => {
      const before = first.get(paneId)
      if (!before) return
      const held = paneId === run.id
      const shiftX = before.left - el.offsetLeft + (held ? run.dx : 0)
      const shiftY = before.top - el.offsetTop + (held ? run.dy : 0)
      if (!shiftX && !shiftY) {
        el.classList.remove('desk-pane-lift')
        el.style.transform = ''
        el.style.transition = ''
        return
      }
      el.style.transition = 'none'
      el.style.transform = `translate3d(${shiftX}px, ${shiftY}px, 0)`
      el.classList.add('desk-pane-settle')
      requestAnimationFrame(() => {
        el.style.transition = ''
        el.style.transform = ''
      })
      window.clearTimeout(settleTimers.current.get(paneId))
      settleTimers.current.set(paneId, window.setTimeout(() => {
        settleTimers.current.delete(paneId)
        el.classList.remove('desk-pane-settle', 'desk-pane-lift')
      }, SETTLE_MS + 60))
    })
  }

  const grab = (id: string, event: ReactPointerEvent<HTMLElement>): void => {
    if (event.button !== 0 || dragRef.current) return
    if ((event.target as HTMLElement).closest('button')) return
    const held = desk.getState()
    if (held.solo || held.panes.length < 2) return
    if (!twoShowing(paneEls.current.values())) return
    const grid = gridRef.current
    const el = paneEls.current.get(id)
    if (!grid || !el) return
    event.preventDefault()
    const header = event.currentTarget
    const controller = new AbortController()
    const run: DragRun = {
      id, pointerId: event.pointerId, startX: event.clientX, startY: event.clientY,
      dx: 0, dy: 0, lifted: false, gridRect: grid.getBoundingClientRect(), halfGap: 3, proposal: null, controller,
    }
    dragRef.current = run
    try { header.setPointerCapture(event.pointerId) } catch { /* header torn down mid-gesture */ }

    const arrangementNow = (): DeskArrangement => {
      const now = desk.getState()
      return { order: now.panes.map((pane) => pane.id), duo: now.duo }
    }
    /* The indicator always shows where the pane would LAND -- under the standing
       proposal, or back in its own slot when there is none, which is also what a
       drop right now would mean. */
    const indicate = (next: DeskArrangement | null): void => {
      setDrop(slotOf(next ?? arrangementNow(), id, desk.getState().splits,
        run.gridRect.width, run.gridRect.height, run.halfGap))
    }

    const move = (ev: globalThis.PointerEvent): void => {
      if (ev.pointerId !== run.pointerId) return
      const dx = ev.clientX - run.startX
      const dy = ev.clientY - run.startY
      if (!run.lifted) {
        /* A slack of a few pixels, so a click on the header is a click. */
        if (Math.hypot(dx, dy) < 6) return
        run.lifted = true
        run.gridRect = grid.getBoundingClientRect()
        /* The stylesheet narrows the gap on a small window; measured, not
           assumed, so the indicator and the hit rects keep matching the CSS. */
        const measured = parseFloat(getComputedStyle(grid).getPropertyValue('--desk-half-gap'))
        run.halfGap = Number.isFinite(measured) ? measured : 3
        /* A pane grabbed back mid-settle: its cleanup must not fire under the
           new gesture and strip the lift off the pane in hand. */
        window.clearTimeout(settleTimers.current.get(id))
        settleTimers.current.delete(id)
        el.classList.remove('desk-pane-settle')
        grid.dataset.dragging = 'true'
        el.classList.add('desk-pane-lift')
        el.style.transition = 'none'
        indicate(null)
      }
      run.dx = dx
      run.dy = dy
      el.style.transform = `translate3d(${dx}px, ${dy}px, 0)`
      const x = ev.clientX - run.gridRect.left
      const y = ev.clientY - run.gridRect.top
      const current = arrangementNow()
      /* Hit rects from the same arithmetic the indicator draws with, not from
         the DOM: the lifted pane is mid-transform and the others never move
         during a drag, so the settled slots ARE the geometry in play. */
      const slots = slotRects(current.order.length, current.duo, desk.getState().splits,
        run.gridRect.width, run.gridRect.height, run.halfGap)
      const rects = new Map<string, SlotRect>()
      current.order.forEach((paneId, index) => {
        const rect = slots[index]
        if (rect) rects.set(paneId, rect)
      })
      const inGrid = x >= 0 && y >= 0 && x <= run.gridRect.width && y <= run.gridRect.height
      const next = inGrid
        ? dragProposal({ arrangement: current, draggedId: id, x, y,
          grid: { width: run.gridRect.width, height: run.gridRect.height }, rects })
        : null
      if (JSON.stringify(next) !== JSON.stringify(run.proposal)) {
        run.proposal = next
        indicate(next)
      }
    }

    const finish = (ev: globalThis.PointerEvent | null, cancelled: boolean): void => {
      if (ev && ev.pointerId !== run.pointerId) return
      controller.abort()
      dragRef.current = null
      try {
        if (header.hasPointerCapture(run.pointerId)) header.releasePointerCapture(run.pointerId)
      } catch { /* already released */ }
      delete grid.dataset.dragging
      setDrop(null)
      if (!run.lifted) return
      settleDrag(run, cancelled ? null : run.proposal)
    }

    window.addEventListener('pointermove', move, { signal: controller.signal })
    window.addEventListener('pointerup', (ev) => finish(ev, false), { signal: controller.signal })
    window.addEventListener('pointercancel', (ev) => finish(ev, true), { signal: controller.signal })
    window.addEventListener('keydown', (ev) => {
      if (ev.key === 'Escape') finish(null, true)
    }, { signal: controller.signal })
  }
  const host = document.getElementById('ws')
  const split = document.getElementById('split')
  const count = state.panes.length
  useLayoutEffect(() => {
    if (!host || !split || count <= 0) {
      previousColumns.current = 0
      return
    }
    const nextColumns = workspaceColumnCount(count, state.duo) as 1 | 2
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
  }, [count, host, split, state.panes, state.duo])
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
    <div ref={gridRef} className={`desk-grid n${shown.length}`} data-solo={Boolean(state.solo)}
      data-duo={shown.length === 2 ? state.duo : undefined}
      data-can-drag={!state.solo && shown.length >= 2 ? 'true' : undefined}
      style={style}>
      {shown.map((pane) => <Pane key={pane.id} pane={pane} onGrab={grab} refPane={refPane} />)}
      {/* Two side-by-side panes share the three-pane case's seam: one column
          split, resized by the same divider. */}
      {shown.length >= 3 || (shown.length === 2 && state.duo === 'cols') ? <Divider axis="column" /> : null}
      {shown.length >= 3 || (shown.length === 2 && state.duo === 'rows') ? <Divider axis="row" side="left" /> : null}
      {shown.length === 4 ? <Divider axis="row" side="right" /> : null}
      {drop ? (
        <div className="desk-drop"
          style={{ transform: `translate(${drop.left}px, ${drop.top}px)`, width: drop.width, height: drop.height }} />
      ) : null}
    </div>,
    host,
  )
}

export function DeskFollowToggle(): JSX.Element | null {
  const state = useSyncExternalStore(desk.subscribe, desk.getState)
  /* The launcher speaks for all three tabs while they are down, so it reads
     what all three read. Without these it drew the count it happened to be
     mounted with, which is zero for the whole of the case it exists for.
     (The workspace's own changes arrive through `DeskApp`.) */
  useSyncExternalStore(deliveries.subscribe, deliveries.getVersion)
  useSyncExternalStore(agents.subscribe, agents.getState)
  /* Only while the strip is down. With the palette up the three tabs each say
     their own number an inch below this, and a sum repeating them there is a
     second voice for one fact -- on the button whose job at that moment is to
     put the desk away. The other signal has no such double: nothing in the
     strip says a run is still going, so that one stands whether or not the
     palette is up. */
  const fresh = desk.showing() ? 0 : desk.unseenAll()
  const busy = desk.working()
  /* The bubble pops on the way UP and only then. A number that drops is the
     reader having just read something -- announcing that with the same flourish
     as an arrival would be the page telling them news they made themselves.
     Keyed so the element remounts and the one-shot animation restarts; a text
     change alone would not replay it. */
  const before = useRef(fresh)
  const grew = fresh > before.current
  useEffect(() => { before.current = fresh })
  /* Nothing of the desk is over a fullscreen pane, its own handle included: the
     pane IS the window while it is up, and the way back out is the pane's own
     restore button. The palette leaves for the same reason (DeskPalette), and
     both come back as they were when it does -- the flag and the geometry are
     untouched by this. */
  if (state.solo) return null
  /* Both facts in words, in the button's own name. Neither channel is
     announceable -- an animation is not, and the bubble is `<i>`, which maps to
     a generic role where `aria-label` does not apply at all -- and an explicit
     name on the button replaces the whole subtree, so this is the only place
     they can be said. */
  const name = [
    t(state.paletteOpen ? 'gui.collapse_ws' : 'gui.workspace'),
    ...(fresh ? [t('gui.ws.unseen_tab', { n: String(fresh) })] : []),
    ...(busy ? [t('gui.ws.agents_working')] : []),
  ].join(', ')
  return (
    <button
      className="ghost-ic desk-follow-toggle"
      aria-label={name}
      aria-expanded={state.paletteOpen}
      /* The two signals are separate channels on purpose: the motion lives
         inside the glyph and the bubble hangs off the corner, so a session that
         is both working and holding news shows both without either moving the
         other. Only one of them loops. */
      data-working={busy || undefined}
      onClick={desk.toggleDesk}
    >
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
        <path d="M4 7h4M12 7h8M4 12h9M17 12h3M4 17h2M10 17h10" />
        <circle cx="10" cy="7" r="2" />
        <circle cx="15" cy="12" r="2" />
        <circle cx="8" cy="17" r="2" />
      </svg>
      {fresh ? (
        <i className="desk-count" key={grew ? fresh : 'held'} data-pop={grew || undefined} aria-hidden="true">
          {fresh}
        </i>
      ) : null}
    </button>
  )
}
