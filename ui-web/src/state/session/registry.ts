/* Which conversation the page is on, and what becomes of the one it leaves.
 *
 * One `SessionRuntime` per session_key, one more for the draft that has no key
 * yet, and one `active` pointer saying which of them the page is showing. A
 * frame off the socket names its subscription, and the subscription names its
 * runtime -- so "which conversation is this about" is one lookup, and the four
 * page-level names that used to answer it (`live.subId`, `turnOwner`,
 * `viewGen`, and the two owner-taking dispatchers) are gone.
 *
 * The switch is here because only a switch can spend a view ticket: a
 * `session.resume` is a round trip, and the reader can leave while it is in
 * flight, so an answer whose ticket has been spent is dropped rather than
 * painted over the conversation that overtook it.
 */

import { drawMeter, goPaint as goState, loadDraft, parkDraft, queueClear, setLiveAnchor, turn } from '../../features/composer/mount'
import { loadProviders } from '../../features/model/source'
import { rowFrom, SESS_CHANNELS } from '../../features/rail/source'
import { draw as sessionDraw, endRename, markNew as markNewCurrent, reconcileRows } from '../../features/rail/store'
import { plainTitle } from '../../features/rail/title'
import { leaveDeletedSession } from '../../features/rail/wire'
import { loadPermMode } from '../../features/settings/source'
import { killStatus, status as transcriptStatus, stopStream } from '../../features/transcript/mount'
import { msOfIso, renderHistory } from '../../features/transcript/source'
import { wsOnHistory } from '../../features/workspace/record'
import { wsSetRoot } from '../../features/workspace/source'
import { loadDeliveries } from '../../features/workspace/store'
import { t } from '../../i18n/t'
import { $ } from '../../lib/dom'
import { current as sessionCurrent, setCurrent as sessionSet } from '../../lib/session'
import { gateway } from '../../rpc/gateway'
import { draw as drawBanner } from '../banner'
import { set as setCtx } from '../ctxChip'
import { load as loadTier } from '../tier'
import { show as toast } from '../toast'
import { reset as wsReset, setOpen as setWs } from '../ws'
import { pitch, unpitch } from './conversation'
import { dropAll as dropHeldHosts } from './hosts'
import { park, resume as resumeInPlace } from './residency'
import { refreshDag, resume as resumeConversation } from './resume'
import { open as sessionOpen, replace as sessionReplace, rows as sessionRows, sess } from './rows'
import { drain, SessionRuntime, reset as resetTurnState } from './runtime'
import { dispatch } from './stages'

import type { SessRow } from '../../features/rail/types'

const byKey = new Map<string, SessionRuntime>()
const bySub = new Map<string, SessionRuntime>()

/* The new-task screen: a conversation that does not exist on disk yet. It is a
   runtime like any other so that a model, a tier and a permission mode picked
   before the first message have somewhere to live, and `mint` hands the whole
   of it to the session the first message creates. */
let draftRt: SessionRuntime | null = null
let activeRt: SessionRuntime | null = null

/* Which switch the visible page belongs to. Every switch takes the next
   ticket, and an answer whose ticket has been spent is dropped rather than
   painted, which is lossless because a re-open reads the same transcript back
   off disk. */
let switches = 0

export const switchToken = (): number => switches

const nextToken = (): number => { switches += 1; return switches }

export function draft(): SessionRuntime {
  if (!draftRt) draftRt = new SessionRuntime(null)
  return draftRt
}

/** The runtime one conversation's frames belong to, or nothing. */
export const get = (key: string | null | undefined): SessionRuntime | undefined =>
  (key ? byKey.get(key) : undefined)

/** The same, made if this is the first frame for that conversation. */
export function ensure(key: string | null | undefined): SessionRuntime {
  if (!key) return draft()
  const found = byKey.get(key)
  if (found) return found
  const made = new SessionRuntime(key)
  byKey.set(key, made)
  return made
}

/** The conversation the page is showing. */
export function viewRuntime(): SessionRuntime {
  if (!activeRt) activeRt = ensure(sessionCurrent())
  return activeRt
}

export const isActiveRuntime = (rt: SessionRuntime): boolean => activeRt === rt

/** The conversation the reader is now looking at. */
export function adopt(key: string | null | undefined): SessionRuntime {
  activeRt = ensure(key)
  return activeRt
}

export const adoptRuntime = (rt: SessionRuntime): void => { activeRt = rt }

/* Nobody sets the pointer for a frame: `viewRuntime` above answers with the
   conversation the session pointer names when no switch has claimed one, which
   is what replaced recording the turn's owner by hand at three send sites. */

/** Nothing is on screen: the switch is between two conversations. */
export const release = (): void => { activeRt = null }

/** Whether the page is on the new-task screen rather than in a conversation. */
export const isDraft = (): boolean => activeRt !== null && activeRt === draftRt

/* The draft became a conversation. Everything it was holding belongs to that
   conversation now -- the staged picks, the lane it drew into, the phase of the
   turn already being sent -- so the runtime is given the key rather than
   replaced by a new one, and the next new-task screen gets a fresh draft. */
export function mint(rt: SessionRuntime, key: string): void {
  rt.key = key
  byKey.set(key, rt)
  if (rt.subscriptionId) bySub.set(rt.subscriptionId, rt)
  if (draftRt === rt) draftRt = null
  activeRt = rt
}

/* ---- subscriptions ----------------------------------------------------- */

/* Whether the gateway handed this subscription a turn in flight, or `null`
   when there was nothing to ask: the conversation already had a stream, or the
   call failed. Answered from the far side of the registration, so a `false`
   means the turn ended before this subscription existed and no event of it is
   coming -- see turn_subscribe in raven/rpc/methods/turn.py. */
export async function subscribe(sessionKey: string): Promise<boolean | null> {
  // One subscription per session per socket: re-opening a session reuses its
  // stream, so a conversation's frames never double up.
  const known = ensure(sessionKey)
  if (known.subscriptionId) return null
  try {
    const r = await gateway().call('turn.subscribe', { session_key: sessionKey })
    record(sessionKey, r.subscription_id)
    return !!r.running
  } catch (e) {
    toast(t('gui.op.subscribe_failed', { detail: (e as Error).message || e }))
    return null
  }
}

/* A conversation's stream, by the id the gateway answered with. Its own name
   because a test has to be able to put one on the books without a round trip. */
export function record(key: string | null | undefined, subscriptionId: string | null): void {
  const rt = ensure(key)
  if (rt.subscriptionId) bySub.delete(rt.subscriptionId)
  rt.subscriptionId = subscriptionId
  if (subscriptionId) bySub.set(subscriptionId, rt)
}

/** Which conversation a frame that names a subscription belongs to. */
export const bySubscription = (id: string): string | undefined => bySub.get(id)?.key ?? undefined

/** The runtime itself, which is what the pipeline routes to. */
export const bySubscriptionRuntime = (id: string): SessionRuntime | undefined => bySub.get(id)

/** Stop reading a conversation's stream, and forget that it had one. */
export function forget(key: string): void {
  const rt = get(key)
  const subId = rt && rt.subscriptionId
  if (!rt || !subId) return
  rt.subscriptionId = null
  bySub.delete(subId)
  gateway().call('turn.unsubscribe', { subscription_id: subId }).catch(() => {})
}

export async function refreshList(): Promise<void> {
  try {
    const r = await gateway().call('session.list', { channels: SESS_CHANNELS })
    const rows = (r.sessions || []).map(rowFrom)
    // Pins and persisted fields come from the server. Only the running/done
    // marker is client state; a not-yet-saved current row also survives until
    // the first list response that contains it.
    const reconciled = reconcileRows(sessionRows(), rows, sessionCurrent())
    const currentMissing = reconciled.currentMissing
    sessionReplace(reconciled.rows)
    if (currentMissing) await leaveDeletedSession(sessionCurrent() as string)
    else sessionDraw()
  } catch { /* keep the stale list */ }
}

/* ---- the switch -------------------------------------------------------- */

function resetView(rt: SessionRuntime): void {
  turn.dispatch({ type: 'idle' }); queueClear()
  stopStream()
  /* The turn state belongs to the conversation, so one that kept a turn while
     it was away keeps it: the wipe is for a conversation arrived at fresh,
     which is every conversation that reads itself back off disk. */
  if (!rt.events) resetTurnState(rt)
  wsReset()
  setWs(false)
  /* The empty state ends when the reader leaves it, not when its replacement
     finishes arriving: holding it across the round trip left the wordmark and
     the centred composer sitting over an empty stage, and dropped them 81px the
     moment the transcript landed. The draft pitches again right after. */
  unpitch()
  /* Whatever is still on the stage, which is never the lane of a conversation
     that kept its turn: `park` above took that host off and filed it on the
     runtime it belongs to. */
  $('#stage')!.innerHTML = ''
  $('#flash')!.textContent = ''
  drawMeter(); goState(); drawBanner()
}

export function switchToDraft(): void {
  /* Before anything reaches for h1#title -- here and in `switchTo`, the two
     paths that change which conversation is open. An open title editor stands
     IN PLACE OF that heading, so leaving it up would take the heading away for
     the life of the tab. Ending it commits, which is where a name typed there
     belongs: the conversation it was typed over, not the one being opened. */
  endRename()
  const gen = nextToken()
  park()
  parkDraft(); loadDraft('new')
  /* A fresh draft, so the model, tier and permission mode a previous one staged
     and never sent go with it: an invisible choice must not cross from one
     conversation to another. */
  draftRt = null
  const rt = draft()
  adoptRuntime(rt)
  resetView(rt)
  sessionSet(null)
  // A draft runs the configured default, so leaving a conversation for one has
  // to read that default back or the chip keeps claiming the model of the
  // conversation just left. A null session omits the field, which is the
  // default's own answer. The tier and the permission mode are the same.
  void loadProviders(null, gen)
  void loadTier()
  void loadPermMode(null, gen)
  $('#title')!.textContent = t('gui.new_task')
  pitch(); sessionDraw(); $('#ta')!.focus()
}

export async function switchTo(s: SessRow): Promise<void> {
  endRename()
  const gen = nextToken()
  park()
  parkDraft(); loadDraft(s.id)
  const rt = ensure(s.id)
  adoptRuntime(rt)
  // The model is per conversation now, so the chip must follow the one being
  // opened -- otherwise it keeps the model of the session left behind. Keyed to
  // s.id rather than sessionCurrent(): the current session is not switched over
  // until below (and not at all on the parked-turn path). The gen lets the
  // refresh drop itself if a later open overtakes it, since model.options
  // answers off-thread and can land out of order.
  rt.staged.model = null; rt.staged.tier = null; rt.staged.perm = null
  void loadProviders(s.id, gen)
  /* Read here rather than from `session.onChange`, which is what the tier used
     and what left it stale: `setCurrent` returns early when the id is unchanged,
     and the reconnect path reopens the SAME id. The loop holds session policies
     in memory (`_session_policies`, no persistence), so a gateway restart puts
     every session back on the catalogue default -- and the chip went on naming
     the tier from before the gap. */
  void loadTier()
  void loadPermMode(s.id, gen)
  // Opening it IS reading it. ``s`` can be a bare {id, title} from the
  // reconnect path, so clear the flag on the row in sessionRows(), not on the arg.
  const row = sess(s.id)
  /* `ask` clears for the same reason `done` does: opening the conversation is
     answering the notice. The rack mounts whatever was waiting the moment this
     conversation is the open one, so the row has done its job and a badge left
     behind would outlive the sheet it was pointing at. */
  if (row && (row.status === 'done' || row.status === 'ask')) row.status = null
  markNewCurrent()
  resetView(rt)
  $('#title')!.textContent = plainTitle(s.title)
  // A kept turn resumes in place of a disk reload: the transcript on disk does
  // not have the still-streaming content, the lane this conversation kept does.
  if (rt.events) {
    sessionSet(s.id)
    resume(rt)
    /* Unconditional, where this used to claim the recorded subscription itself
       and fall back to subscribing when there was none: `subscribe` already
       does exactly that, and one path through it is what keeps the rule in a
       single place. Nothing follows the await, so the extra microtask on the
       reuse case changes no ordering. */
    await subscribe(s.id)
    /* The graph alone, and after the replay above so it reads whatever that put
       back. A conversation buffers the frames it misses, but only while its
       turn is running -- and a graph outlives the turn that started it, so every
       node report after that turn ended was dropped and the sheet is stale. Not
       the whole of `view.resume`: its desk half replays the reader's opens
       through the same verbs a click goes through, and every window they had
       would come back twice. Not awaited, for the reason the resume below is
       not. */
    refreshDag(s.id)
    return
  }
  try {
    const r = await gateway().call('session.resume', { session_id: s.id })
    /* Somebody else's page now. Everything below writes what the reader is
       looking at -- the pointer, the rail selection, the context ring, the
       transcript, the panes -- so a spent ticket has to stop here rather than
       repaint over the switch that overtook it. */
    if (gen !== switches) return
    if (r.session_id && r.session_id !== s.id) s.id = r.session_id
    sessionSet(s.id)
    /* resume hands back the canonical id, so the row rendered from the listed
       id no longer matches the current pointer -- without this redraw the rail
       shows nothing selected until the reader clicks a session themselves. */
    sessionDraw()
    /* The session's working directory, for the path shortener. It rides on
       every init bundle and used to be learned from a directory listing, which
       is a call the page no longer makes. */
    rt.wsRoot = (r.info && r.info.cwd) || null
    wsSetRoot(r.info && r.info.cwd)
    const u = (r.info && r.info.usage) || {}
    /* context_estimated rides along in this payload and is not passed on: the
       ring has nowhere to say an estimate, so the writer takes two numbers.
       See state/ctxChip.ts. */
    setCtx(u.context_used, u.context_max)
    /* The turn this conversation is in the middle of, which nothing else on a
       page that has just loaded can know: its frames went to a socket this page
       did not have. Put the machine back into the state `message.start` would
       have left it in, so the stop button is there, a send queues instead of
       being refused as -32003, and the deltas still to come open a step of
       their own. What already streamed is not recoverable and is not pretended
       at -- the reader picks the answer up from where it has got to. */
    if (r.info && r.info.running) {
      /* Anchored to the question's own stamp, before anything paints the live
         row: left to default to `Date.now()`, the clock on it started again
         from zero at every reload of the same turn. */
      const asked = (r.messages || []).filter((m) => m && m.role === 'user').pop()
      const askedAt = asked ? msOfIso(asked.timestamp) : 0
      if (askedAt) { setLiveAnchor(askedAt); rt.startedAt = askedAt }
      rt.dispatch({ type: 'stream', cancellable: true }); goState()
    }
    /* Every graph this conversation started, oldest first, as the gateway
       stamped them onto the rows that started them. This is the only source
       that survives a run the reader never saw start: no live event reached
       this page for it, so nothing was written down -- see state/session/resume.ts. */
    const dagRuns = (r.messages || [])
      .map((m) => (m && m.dag_run_id) || '')
      .filter((id): id is string => !!id)
    if (r.messages && r.messages.length) {
      renderHistory(r.messages)
      /* Rebuild what the panel can from the replay. Stored messages keep the
         tool name and its result but not the call arguments, so this recovers
         the changed paths and counts the rest -- see wsOnHistory. */
      wsOnHistory(r.messages)
    } else pitch()
    /* After the replay, because the replay is the better answer where it has
       one: a manifest on a stored turn knows which turn delivered the file, and
       the registry only knows that this conversation did. What the registry
       adds is everything the replay cannot carry -- a turn still in flight when
       the socket dropped, and one whose messages a compaction has since
       archived. Not awaited: the shelf fills when it answers. */
    loadDeliveries(s.id)
    const running = await subscribe(s.id)
    /* Checked again on this side of the subscribe: the round trip is one more
       place a reader can leave from, and a replayed file window opens on
       whichever desk is on screen. */
    if (gen !== switches) return
    /* The turn ended between the two round trips: resume said it was running,
       the subscription that would carry its end says it is not, and the machine
       armed above is still waiting for an event that will never arrive. Open the
       conversation again rather than pushing the machine back to idle -- the
       transcript painted above is missing the answer for the same reason, and a
       re-open reads both back off disk. The second pass cannot come back here:
       resume now reports the turn as finished. */
    if (r.info && r.info.running && running === false && turn.busy()) {
      await switchTo(s)
      return
    }
    /* Last, and only on this path. The reader may be arriving here after a
       reload -- or after an upgrade replaced the page under them -- in which
       case the graph they were watching and the windows they had open are
       recorded but not on screen. The kept-turn path above takes the graph half
       of this and not the desk half: its conversation never left this page, so
       its windows are still open and replaying them would give the reader each
       one twice -- but its graph went on running with nobody listening, which is
       why that path calls `refreshDag` rather than returning outright.
       Not awaited: it reads the run and the panes back from the gateway, and
       the transcript is already up. */
    resumeConversation(s.id, dagRuns)
  } catch (e) {
    /* Same for the failure: a session the reader has already left must not
       empty their stage, and must not raise a toast about a page nobody is on. */
    if (gen !== switches) return
    pitch()
    toast(t('gui.op.open_failed', { detail: (e as Error).message || e }))
  }
}

/* ---- a fresh socket ---------------------------------------------------- */

/* A fresh socket voids every server-side subscription, and the frames a kept
   turn missed while the socket was down are unrecoverable -- drop what every
   conversation kept and let re-opens rebuild from disk. */
export async function reconnect(): Promise<void> {
  killStatus()
  for (const rt of byKey.values()) { rt.subscriptionId = null; rt.events = null; rt.host = null }
  dropHeldHosts()
  draft().subscriptionId = null
  bySub.clear()
  sessionRows().forEach((s: SessRow) => { if (s.status === 'run') s.status = null })
  /* Re-subscribing is not enough: events emitted while the socket was down
   are gone, and if the turn ENDED in that gap the client would keep its
   busy spinner forever. Reload the whole session from disk instead -- the
   transcript is persisted server-side, so a full re-open is lossless, and
   a turn that is genuinely still running keeps streaming into the fresh
   subscription that the switch sets up. */
  const current = sessionCurrent()
  if (current && !isDraft()) {
    turn.dispatch({ type: 'idle' })
    /* End the editor before reading anything it can change. The teardown
     inside the switch is too late for THIS caller: it is the only one that
     builds a detached { id, title } instead of handing over the live row, so a
     title read here and committed there gets painted stale over the heading the
     commit had just restored. Every other caller passes the row itself and is
     immune by construction.

     And the row is what carries the title; the heading is only where it is
     drawn. Reading the heading FIRST died here with an editor open --
     h1#title does not exist while the input stands in its place, so the
     handler threw on this line and the conversation was never reloaded,
     never re-subscribed and never told the reader it was back. It is the
     wrong first source twice over besides: it holds the plain form with any
     leading icon stripped, and it holds nothing at all while a name is being
     generated. It stays as the last resort for the one thing the row cannot
     answer -- a current conversation that is not in the list. */
    endRename()
    const open = sess(current)
    const heading = $('#title')
    const title = (open && open.title) || (heading && heading.textContent) || ''
    await sessionOpen({ id: current, title })
    transcriptStatus(t('gui.reconnected'))
    setTimeout(() => killStatus(), 2500)
  } else if (current) {
    void subscribe(current)
  }
}

/* ---- residency --------------------------------------------------------- */

export { park }
export { dispatchTo } from './residency'

/* The half of a return that residency cannot do on its own: what a buffered
   frame means is the pipeline's, so the applier is handed over here. */
export function resume(rt: SessionRuntime): void {
  resumeInPlace(rt, dispatch)
  /* The queue moves once the conversation is back and its phase with it: a
     turn that ended while it was away leaves the engine free. */
  if (!turn.busy()) drain()
}

/** Whether the conversation still holds its turn while it is off screen. */
export const isResident = (key: string): boolean => !!get(key)?.events

/** The runtime holding that turn, for a caller that wants what it kept. */
export function parked(key: string): SessionRuntime | undefined {
  const rt = get(key)
  return rt && rt.events ? rt : undefined
}

/* Whether any conversation is holding this lane host -- the question the
   transcript island asks before it drops a detached one. Kept as a set of its
   own (./hosts) so that the island's renderer can ask it without importing the
   session layer behind it. */
export { holdsHost } from './hosts'

/* Test seam only: the draft runtime, the active pointer and the switch counter
   are the module's. The runtimes themselves are dropped with them: a case's
   conversation must not be the next case's active one. */
export function _resetForTests(): void {
  draftRt = null
  activeRt = null
  switches = 0
}
