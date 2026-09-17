/* What a conversation keeps while it is off screen, and what it gets back.
 *
 * Server-side subscriptions are additive and never torn down here, so a
 * background conversation's turn keeps streaming over the socket. The rule is
 * the one the page has always had: a conversation with no turn running keeps
 * nothing -- it is re-read from disk on the way back in -- and one with a turn
 * in flight keeps the turn, the lane it is streaming into, and the frames that
 * arrive while it is away.
 *
 * "Keeps the lane" is a DOM host, not a copy of the segments: the segments are
 * in the transcript store already, and it is the store's lane whose life is
 * tied to that element (features/transcript/mount.tsx). So leaving takes the
 * host off `#stage` and files it on the runtime; coming back puts it back.
 */

import { liveAnchor, setLiveAnchor } from '../../features/composer/mount'
import { nudge, stopStream } from '../../features/transcript/mount'
import { down as scrollTranscriptDown } from '../../features/transcript/tail'
import { restore as wsRestoreSnapshot, snapshot as wsSnapshot } from '../../features/workspace/store'
import { draw as drawBanner } from '../banner'
import { current as sessionCurrent } from '../../lib/session'
import { drawMeter, goPaint as goState, queueRestore, queueSnapshot, turn } from '../../features/composer/mount'
import { $ } from '../../lib/dom'
import { draw as sessionDraw } from '../../features/rail/store'
import { draw as drawWs, open as wsOpen, restore as wsRestore, view as wsView } from '../ws'
import { sess } from './rows'
import { drop as dropHost, hold as holdHost } from './hosts'
import { adoptRuntime, get, release, viewRuntime } from './registry'

import type { SessionRuntime } from './runtime'
import type { TurnEvent } from '../../features/composer/turn'
import type { WorkspaceSnapshot } from '../../features/workspace/types'

/* A phase event for one conversation, whether or not it is on screen. Here
   rather than with the switch because what becomes of it is the residency
   rule: the conversation being looked at moves the island's machine, one
   holding a turn off screen folds the event into the copy it kept, and one
   doing neither has nowhere to put it. */
export const dispatchTo = (key: string, event: TurnEvent): void => { get(key)?.dispatch(event) }

const laneHost = (): HTMLElement | null => {
  const stage = $('#stage')
  return stage ? stage.querySelector(':scope > [data-tsl]') : null
}

/* Leaving. The conversation being left is the one the registry has been
   showing, never the session pointer: every rail click moves that pointer
   before the switch runs, so by the time this is reached it already names the
   conversation being OPENED. Filing under it would put the old transcript in
   the wrong drawer and hand it straight back as the new conversation's
   content. */
export function park(): void {
  const rt = viewRuntime()
  release()
  if (!turn.busy()) return
  stopStream()
  const s = sess(rt.key)
  if (s) s.status = 'run'
  rt.phase = turn.snapshot()
  rt.queue = queueSnapshot()
  /* The live clock's anchor. It is the composer island's own state, and the
     away conversation's idle turn-live paint zeroes it -- without carrying it
     here, a turn ten minutes in read "2s" after a round trip through another
     conversation. */
  rt.liveT0 = liveAnchor()
  rt.ws = wsSnapshot()
  /* Asked for, not read off the panel's own bindings: this is where the pane
     state is kept, not where it is owned. */
  rt.pane = wsView()
  const host = laneHost()
  if (host) { host.remove(); rt.host = host; holdHost(host) }
  rt.events = []
}

/* Arriving, in place of a disk read: the transcript on disk does not have the
   still-streaming content, and this does. `apply` is what a buffered frame
   means, handed in rather than imported, so that where a conversation's things
   live stays free of the event vocabulary. */
export function resume(rt: SessionRuntime, apply?: (ev: unknown) => void): void {
  adoptRuntime(rt)
  const stage = $('#stage')
  if (rt.host && stage) { stage.appendChild(rt.host); dropHost(rt.host); rt.host = null }
  turn.restore(rt.phase); queueRestore(rt.queue)
  /* Before drawMeter below: its turn-live paint keeps a non-zero anchor, so the
     clock resumes from the turn's real start rather than from the switch. */
  setLiveAnchor(rt.liveT0 || 0)
  wsRestoreSnapshot(rt.ws as WorkspaceSnapshot)
  if (rt.pane) wsRestore(rt.pane.tab, rt.pane.picked)
  const s = sess(sessionCurrent())
  if (s && s.status === 'run') s.status = null
  const backlog = rt.events || []
  rt.events = null
  if (apply) {
    backlog.forEach((ev) => { try { apply(ev) } catch { /* one bad frame must not eat the rest */ } })
  }
  nudge()
  drawMeter(); goState(); sessionDraw(); drawBanner()
  if (typeof drawWs === 'function' && wsOpen) drawWs()
  scrollTranscriptDown()
}
