/** Floating workspace composition and public compatibility entry points. */

import { useEffect, useSyncExternalStore } from 'react'

import * as turn from '../composer/turn'
import * as agents from '../subagents/store'
import { DeskPalette } from './DeskPalette'
import * as desk from './deskStore'
import { DeskFollowToggle, DeskSurface } from './DeskSurface'
import * as workspace from './store'

import type { JSX } from 'react'

export {
  claimDraft,
  notifyDesk,
  openDeskAgent,
  openDeskAgentRecord,
  openDeskDiff,
  openDeskFile,
  openDeskTab,
  reset,
  sync,
  toggleDesk,
} from './deskStore'

/* Slow on purpose: this feeds a number on a tab and a glyph on a button, not a
   spinner. The agents store keeps its own 2.5s floor besides. */
const AGENTS_POLL_MS = 8000

/* Whether anyone needs the instance list refreshed right now.
 *
 * Both of the desk's readings of that list are told nothing: a write reaches
 * the workspace record and a delivery reaches the delivery registry on the
 * turn's own events, but the instance list is ASKED for -- by the panel when it
 * draws, and by a resume -- and nothing asks while a turn quietly spawns a
 * sub-agent. So the two places that report background work, the agents tab's
 * bubble and the launcher's own glyph, were both reporting an answer from
 * whenever the list was last fetched.
 *
 * Asked from here rather than from the palette, which is where it used to live:
 * the launcher badges the same list with the palette DOWN, and a timer mounted
 * with the palette cannot feed it. And asked on a condition rather than always,
 * because an idle conversation with nothing running has nothing to discover and
 * polling it all afternoon buys nothing.
 *
 * Two conditions, not one, and the second is the glyph's way back to off. A run
 * STARTING can only happen under a live turn -- but a run ENDING is read from
 * this same list, and by then the turn is idle and the desk is still down, so a
 * condition of `turn.busy()` alone stopped asking and left the launcher
 * breathing forever. It settled only when the reader opened the desk or sent
 * the next message: stuck on for exactly as long as they were relying on it
 * instead of the palette. So we also ask while we still believe something is
 * live, which is the only state in which the answer can change anything, and it
 * converges one tick after the run settles.
 *
 * The agents tab is the exception in the other direction: the panel refreshes
 * its own list while it is the one on screen, so asking again here is a second
 * request for what is already in flight. */
function wanted(): boolean {
  if (desk.showing()) return desk.getState().tab !== 'agents'
  return turn.busy() || desk.working()
}

export function DeskApp(): JSX.Element {
  useSyncExternalStore(workspace.subscribe, workspace.getState)
  useEffect(() => {
    document.documentElement.classList.add('desk-ready')
    return () => document.documentElement.classList.remove('desk-ready')
  }, [])
  /* One timer for the desk, mounted for as long as the desk is, with the
     condition inside it. The condition is re-read on every tick rather than
     turned into an effect dependency because a turn starting is not something
     this component is told about -- the turn machine has no subscription, and
     giving it one to save a comparison every 8s would be the tail wagging the
     dog. */
  useEffect(() => {
    const ask = (): void => { if (wanted()) void agents.refreshInstances() }
    ask()
    const timer = setInterval(ask, AGENTS_POLL_MS)
    return () => clearInterval(timer)
  }, [])
  return <><DeskPalette /><DeskFollowToggle /><DeskSurface /></>
}
