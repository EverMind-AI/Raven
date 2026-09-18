/** The desk's root: the palette, the follow toggle and the pane surface. */

import { useEffect, useSyncExternalStore } from 'react'

import * as lang from '../../state/lang'
import * as agents from '../subagents/store'
import * as workspace from '../workspace/store'
import { DeskPalette } from './DeskPalette'
import { DeskFollowToggle, DeskSurface } from './DeskSurface'

import type { JSX } from 'react'

/* Slow on purpose: this feeds a number on a tab and a glyph on a button, not a
   spinner. The agents store keeps its own 2.5s floor besides. */
const AGENTS_POLL_MS = 8000

/* Nobody tells the desk about background work, so the desk asks.
 *
 * Its other two readings are pushed: a write reaches the workspace record and a
 * delivery reaches the delivery registry on the turn's own events. The instance
 * list is not -- it is ASKED for, by the panel when it draws and by a resume --
 * so the two places that report delegated work, the agents tab's bubble and the
 * launcher's glyph, are only ever as current as the last fetch.
 *
 * Asked from here rather than from the palette, because the launcher badges the
 * same list with the palette DOWN and a timer mounted with the palette cannot
 * feed it.
 *
 * Asked unconditionally, which is the part worth reading before changing.
 * Twice this was given a condition meant to spare an idle conversation, and
 * both times the condition cost the feature outright:
 *
 *   1. `turn.busy()` -- on the argument that only a live turn can spawn a
 *      sub-agent. True of a run STARTING. A run ENDING is read from this same
 *      list, and by then the turn is idle, so nothing asked again and the glyph
 *      breathed forever.
 *   2. `turn.busy() || working()` -- which fixed that end and left the start
 *      deadlocked. A playbook runs in the BACKGROUND: the launching turn says
 *      so and finishes in seconds while its sub-agents work for minutes. There
 *      is then no moment when a turn is busy and a run is already known to be
 *      live, and `working()` can only become true from a fetch this condition
 *      is refusing to make. Nothing asked, so nothing was known, so nothing
 *      asked -- and the glyph never came on in the ordinary case.
 *
 * The saving those were defending is one request every 8s on an open page, with
 * the agents store's own 2.5s floor underneath it and an early return when no
 * conversation is open. That is smaller than the feature. Anyone reaching for a
 * condition here again needs a test for a run that outlives the turn that
 * started it -- `DeskApp.test.tsx` has one.
 *
 * The agents tab is not excepted either. It looks like it should be, since the
 * panel refreshes when it draws -- but only when it draws. The recurring poll
 * in the subagents store (its `onPoll`) gates on `panel().view()`, which
 * reports the WORKSPACE PANEL's tab (state/ws.ts) and is never set by the desk
 * palette, so excepting the tab froze its list at whatever it held when the
 * reader opened it. */

export function DeskApp(): JSX.Element {
  useSyncExternalStore(workspace.subscribe, workspace.get)
  /* The language the page resolved, so a pick repaints this island: every word
     below is a t(key) read at render time (state/lang/store.ts). */
  useSyncExternalStore(lang.subscribe, lang.get)
  useEffect(() => {
    document.documentElement.classList.add('desk-ready')
    return () => document.documentElement.classList.remove('desk-ready')
  }, [])
  /* One timer for the desk, mounted for as long as the desk is. */
  useEffect(() => {
    const ask = (): void => { void agents.refreshInstances() }
    ask()
    const timer = setInterval(ask, AGENTS_POLL_MS)
    return () => clearInterval(timer)
  }, [])
  return <><DeskPalette /><DeskFollowToggle /><DeskSurface /></>
}
