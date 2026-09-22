/** Draggable floating navigation palette for workspace content. */

import { useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { t } from '../../i18n/t'
import * as tasksStore from '../tasks/store'
import { TasksApp } from '../tasks/TasksPage'
import * as deliveries from '../workspace/deliveries'
import { fileKind } from '../workspace/store'
import * as workspace from '../workspace/store'
import { DeskIcon } from './DeskIcon'
import {
  clampGeometry,
  defaultGeometry,
  DESK_DRAG_THRESHOLD,
  DESK_GEOMETRY_KEY,
  deskReserve,
  magnetGeometry,
} from './geometry'
import * as desk from './store'

import type { TaskFile, TaskNode, TaskRow } from '../tasks/types'
import type { DeliveryRow } from '../workspace/types'
import type { DeskGeometry, DeskTab } from './types'
import type { CSSProperties, JSX, PointerEvent as ReactPointerEvent } from 'react'

/* The three tabs, in the prototype's own order (deliverables, tasks, diff), each
   carrying its own badge. Two of the three carry what is new in them, because
   a reader who is on one tab has exactly one question about the other two.
   The tasks tab carries something else: how many are running right now,
   which is what a reader who has already opened every task still wants to
   know -- "new" would go quiet on it the moment they had looked once.

   The label is marked with a class rather than left to `span:last-child`: the
   bubble is a sibling, and the moment one existed the positional selector
   stopped matching the label, which put the full label on every collapsed
   34px tab. */
function DeskTabs({ value, onChange }: { value: DeskTab; onChange: (tab: DeskTab) => void }): JSX.Element {
  return (
    <div className="desk-tabs" role="tablist">
      {(['deliverables', 'tasks', 'diff'] as DeskTab[]).map((tab) => {
        const label = tab === 'diff' ? 'diff'
          : tab === 'deliverables' ? t('gui.ws.deliverables') : t('gui.ws.tasks')
        const fresh = tab === 'tasks' ? desk.runningTaskCount() : desk.unseen(tab)
        /* Shown only on a tab the reader is not already looking at, the way
           the prototype's own count reads: a selected tab's badge would be
           telling the reader about the screen already in front of them. */
        const showBubble = fresh > 0 && value !== tab
        return (
          <button key={tab} role="tab" aria-label={label} title={label} aria-selected={value === tab} onClick={() => onChange(tab)}>
            <DeskIcon kind={tab} />
            <span className="lb">{label}</span>
            {showBubble ? <i className="desk-count">{fresh}</i> : null}
          </button>
        )
      })}
    </div>
  )
}

/* One nothing for all three tabs: the tab's own icon and one line of fact.
   Nothing else -- an empty tab is there so a reader can see at a glance that
   there is nothing here, and a second sentence telling them where things come
   from, or a link to the other tab, makes three tabs' nothing read as three
   different kinds of nothing. The prototype's own is icon + one bold line. */
function DeskEmpty({ kind, title }: { kind: DeskTab; title: string }): JSX.Element {
  return (
    <div className="desk-empty">
      <DeskIcon kind={kind} />
      <b>{title}</b>
    </div>
  )
}

/* Every file a task's nodes wrote or edited, across every task -- beside the
   session's own rows rather than instead of them: a sub-agent's writes never
   reach the session's own change list, and they are changes to the same working
   directory. Not the shelf: what a task wrote is not what the conversation
   handed over, which only `deliver_files` decides. */
interface TaskFileEntry { row: TaskRow; node: TaskNode; file: TaskFile }

function taskFileEntries(): TaskFileEntry[] {
  const out: TaskFileEntry[] = []
  tasksStore.rows().forEach((row) => row.nodes.forEach((node) => node.files.forEach((file) => {
    out.push({ row, node, file })
  })))
  return out
}

function DiffNav(): JSX.Element {
  useSyncExternalStore(tasksStore.subscribe, tasksStore.get)
  const changes = workspace.shared().changes
  const taskDiffs = taskFileEntries()
  if (!changes.length && !taskDiffs.length) {
    return (
      <DeskEmpty kind="diff" title={t('gui.ws.no_changes')} />
    )
  }
  return (
    <div className="desk-list">
      {changes.map((change) => (
        <button key={`${change.key}:${change.turn}`} className="desk-row desk-diff-row" onClick={() => desk.openDeskDiff(change)}>
          <i className={`chgc ${change.kind}`}>{change.kind === 'add' ? '+' : 'M'}</i>
          <span className="desk-name" title={change.key}>{change.dir}<b>{change.name}</b></span>
          <span className="chgs">
            {change.add ? <i className="a">+{change.add}</i> : null}
            {change.del ? <i className="d">−{change.del}</i> : null}
          </span>
        </button>
      ))}
      {taskDiffs.length ? (
        <>
          <div className="desk-grp">{t('gui.ws.task_changes')}</div>
          {taskDiffs.map(({ row, node, file }) => {
            const kind = tasksStore.taskChangeKind(file)
            return (
              <button
                key={tasksStore.taskChangeKey(row, node, file)}
                className="desk-row desk-diff-row"
                onClick={() => { void tasksStore.fileDiffChange(row, node, file).then(desk.openDeskDiff) }}
              >
                <i className={`chgc ${kind}`}>{kind === 'add' ? '+' : 'M'}</i>
                <span className="desk-name" title={file.path}>{file.path}</span>
                <span className="chgs">
                  <i className="a">+{file.add}</i>
                  <i className="d">−{file.del}</i>
                </span>
              </button>
            )
          })}
        </>
      ) : null}
    </div>
  )
}

function DeliverableRow({ row, here }: { row: DeliveryRow; here: boolean }): JSX.Element {
  const size = deliveries.humanSize(row.size)
  /* A row recovered from the gateway's registry has no turn to name -- it says
     what the file is and leaves the reading position out rather than inventing
     one. A delegated stream's row is the same case for a different reason: it
     HAS a turn, and that turn is its own stream's. Each of them counts from one,
     so drawing it here would put a sub-agent's second turn on a conversation
     that has had five and call it the same number. */
  const mine = row.scope === deliveries.SESSION
  const when = here || !mine || row.turn == null ? '' : t('gui.ws.dlv_turn', { n: String(row.turn) })
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
      onClick={() => workspace.openDelivery(row.path)}
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
    return (
      <DeskEmpty kind="deliverables" title={t('gui.ws.dlv_none')} />
    )
  }
  const now = workspace.currentTurn()
  /* "This turn" is the reader's position in THIS conversation, so only a row the
     conversation delivered can be in it. A delegated stream counts its own turns
     from one, and a sub-agent's fifth was grouped as "this turn" whenever the
     conversation happened to be on five. */
  const here = (row: DeliveryRow): boolean => row.scope === deliveries.SESSION && row.turn === now
  /* Two headings for two groups, not one per run of rows. Emitting a heading
     whenever the key changed from the row before was right while the list was
     sorted by turn and same-turn rows were therefore adjacent; ranked by when
     each delivery happened, two streams interleave and that drew "This turn" /
     "Earlier" / "This turn". Each group keeps the ranking inside it. */
  const groups: Array<[string, DeliveryRow[]]> = [
    ['gui.ws.turn_now', rows.filter(here)],
    ['gui.ws.turn_earlier', rows.filter((row) => !here(row))],
  ]
  const out: JSX.Element[] = []
  groups.forEach(([key, group]) => {
    if (!group.length) return
    out.push(<div key={`grp:${key}`} className="desk-grp">{t(key)}</div>)
    group.forEach((row) => out.push(<DeliverableRow key={row.path} row={row} here={here(row)} />))
  })
  return <div className="desk-list">{out}</div>
}

/* What this conversation set going, whichever way it was set going. Replaces
   the agents list, which answered a narrower question: it drew the instances a
   session had spawned, so a graph and a playbook run -- the two a reader is
   most likely to be waiting on -- appeared nowhere in the palette.

   The roster half of the old list is not lost with it. `AgentList` is also the
   standalone subagents page's list, and starting an instance and opening a
   direct chat still live there. */
function TasksNav(): JSX.Element {
  const state = useSyncExternalStore(tasksStore.subscribe, tasksStore.get)
  useEffect(() => { void tasksStore.refresh() }, [])
  /* Answered here rather than inside the view: in the desk, one nothing has to
     look like the other two, and TasksApp's own note belongs to the panel. */
  if (!state.rows.length) {
    return <DeskEmpty kind="tasks" title={t('gui.ws.tasks_none')} />
  }
  return <TasksApp />
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
  } catch { /* private mode, or a geometry written by an older shape */ }
  return defaultGeometry()
}

export function DeskPalette(): JSX.Element | null {
  const state = useSyncExternalStore(desk.subscribe, desk.get)
  /* Not `state.paletteOpen`: a fullscreen pane is the whole window and the desk
     is not on it (store.showing). Everything below reads this one answer,
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
  useSyncExternalStore(tasksStore.subscribe, tasksStore.get)
  /* Looking at a tab is what makes its contents no longer new -- including what
     lands while the reader is sitting on it, which is why this runs on every
     render rather than only on a switch. */
  useEffect(() => {
    if (shown) desk.seeTab(state.tab)
  })
  const pointerCleanup = useRef<(() => void) | null>(null)
  const [geom, setGeom] = useState(storedGeometry)
  useEffect(() => {
    try { localStorage.setItem(DESK_GEOMETRY_KEY, JSON.stringify(geom)) } catch { /* private mode */ }
  }, [geom])
  /* What the chat gives up so the panel is not sitting on the transcript -- see
     `deskReserve` for the overlap this answers and why it is all-or-nothing.

     On the root because the stylesheet spends it on two elements that are not
     this one's ancestors, which is the same reason `--wsw` lives there.

     Observed rather than computed once: the answer turns on the chat's width,
     and that changes without this component rendering -- the window resizes,
     the rail collapses, the workspace column opens beside it. `#split` is what
     DeskSurface watches for the same reason; this watches the chat itself,
     because the chat is what the reserve is taken out of.

     Keyed on `shown` rather than `phase`, so the column travels WITH the panel's
     190ms animation rather than after it. Cleared on unmount as well as when it
     reaches zero: a desk that stops being rendered would otherwise leave the
     transcript narrow with nothing on screen to explain why. */
  useEffect(() => {
    const chat = document.querySelector('.chat')
    const publish = (): void => {
      const reserve = deskReserve({
        shown,
        detached: geom.detached,
        w: geom.w,
        chatWidth: chat?.getBoundingClientRect().width ?? window.innerWidth,
      })
      const root = document.documentElement.style
      if (reserve > 0) root.setProperty('--desk-reserve', `${reserve}px`)
      else root.removeProperty('--desk-reserve')
    }
    publish()
    const observer = chat ? new ResizeObserver(publish) : null
    if (chat && observer) observer.observe(chat)
    window.addEventListener('resize', publish)
    return () => {
      observer?.disconnect()
      window.removeEventListener('resize', publish)
      document.documentElement.style.removeProperty('--desk-reserve')
    }
  }, [shown, geom.detached, geom.w])
  useEffect(() => {
    const resize = (): void => setGeom((value) => clampGeometry(value))
    window.addEventListener('resize', resize)
    return () => {
      window.removeEventListener('resize', resize)
      pointerCleanup.current?.()
    }
  }, [])
  /* Set when a press on the handle turned into a drag, and read by the capture
     handler on the handle itself: the gesture ends on a button whose click is
     about to fire, and that click belongs to the drag, not to the tab. */
  const dragged = useRef(false)
  const drag = (event: ReactPointerEvent<HTMLDivElement>): void => {
    /* No early return for a press on a button. The handle IS mostly buttons --
       measured on the running page, 182 of its 298px, with the rest broken into
       3px slivers between the tabs -- so refusing to start a drag there left a
       title bar that mostly could not be grabbed, and the press fell through to
       the browser, which began selecting text across the page instead. The
       gesture decides now: past `DESK_DRAG_THRESHOLD` it is a drag and the
       click that would have followed is swallowed; below it the tab keeps its
       click. */
    event.preventDefault()
    /* Like `resize` below, and like every other drag handle in the app
       (`chrome/behaviour/scrollbars.ts` does both on its thumb): the press has done its
       job here, and letting it climb is what let an ancestor act on the same
       gesture. */
    event.stopPropagation()
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
    let moved = false
    /* Capture is taken when the gesture becomes a drag, not when it starts.
       Held from `pointerdown`, it retargets the browser's own click to this
       handle -- pointer events dispatch the click at the capture target when
       `pointerup` happens under capture -- so the click never reached the tab
       under the finger and pressing a tab stopped switching tabs. */
    const move = (e: globalThis.PointerEvent): void => {
      if (e.pointerId !== pointerId) return
      const dx = e.clientX - start.pointerX
      const dy = e.clientY - start.pointerY
      if (!moved) {
        if (Math.abs(dx) + Math.abs(dy) < DESK_DRAG_THRESHOLD) return
        moved = true
        dragged.current = true
        target.dataset.dragging = 'true'
        target.setPointerCapture(pointerId)
      }
      /* Free of the magnet while the hand is down. Snapping on every move froze
         the panel in a band around the anchor -- measured, ~34px of it -- and
         the anchor is at the top of the window, so a reader pushing the panel
         upwards found it would not go. A magnet belongs on the release. */
      setGeom((now) => clampGeometry({
        ...now,
        x: start.x + dx,
        y: start.y + dy,
        detached: true,
      }))
    }
    const finish = (e?: globalThis.PointerEvent): void => {
      if (e && e.pointerId !== pointerId) return
      target.dataset.dragging = 'false'
      if (target.hasPointerCapture(pointerId)) target.releasePointerCapture(pointerId)
      controller.abort()
      /* Here rather than during: let go near where it came from and it goes
         home, and nowhere else does it fight the hand. */
      if (moved) setGeom((now) => magnetGeometry(now))
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
        /* Anchored, the right edge is pinned (styles/page.css's `right: calc(...)`
           on `[data-anchored="true"]`), so the only way to grow is leftward --
           a rightward drag has to shrink it instead. Detached has no pinned
           edge, so the hand's own direction is the width's. */
        w: start.w + (start.detached ? 1 : -1) * (e.clientX - start.pointerX),
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
      role="dialog"
      aria-label={t('gui.workspace')}
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
      <div
        className="desk-drag"
        onPointerDown={drag}
        /* Capture, so it runs before the tab's own click: a gesture that moved
           the panel must not also switch what it is showing. Cleared here
           rather than on the next press, so one drag swallows exactly one
           click. */
        onClickCapture={(clicked) => {
          if (!dragged.current) return
          dragged.current = false
          clicked.preventDefault()
          clicked.stopPropagation()
        }}
      >
        <DeskTabs value={state.tab} onChange={(tab) => desk.set({ tab })} />
      </div>
      <div className="desk-body">
        {state.tab === 'diff' ? <DiffNav /> : state.tab === 'deliverables' ? <DeliverablesNav /> : <TasksNav />}
      </div>
      <div className="desk-resize" aria-hidden="true" onPointerDown={resize} />
    </div>
  )
}
