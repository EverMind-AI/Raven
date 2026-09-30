/* One conversation's turn: its clock, its name, its sends and the things a
 * reader can do to it.
 *
 * A `SessionRuntime` is everything that belongs to ONE conversation and
 * outlives a look at another: the phase its turn is in, the step that is open,
 * the calls in flight, the say buffer the rail's preview reads, the queue
 * waiting behind the turn, the wait for a generated title, the model, tier and
 * permission mode a draft picked before it had a conversation to scope them to,
 * and the DOM host its transcript lane lives in.
 *
 * The page shows one of them at a time, and that one -- the registry's `active`
 * -- is the only one whose phase and queue are mirrored into the composer
 * island's own stores, because those stores ARE the visible turn. Everything
 * else is held here until the conversation is looked at again, which is what
 * replaced the page-level `live` object, the parked-turn snapshots and the four
 * names that used to say which conversation a frame was about.
 */

import { claimDraft as claimComposerDraft } from '../../features/composer/mount'
import { drawMeter, goPaint as goState, queuePush, queueShift, turn } from '../../features/composer/mount'
import { reduce } from '../../features/composer/turn'
import { claimDraft as claimDeskDraft } from '../../features/desk/store'
import { loadProviders, stagedTier } from '../../features/model/source'
import { rowPreview, touchSession } from '../../features/rail/source'
import { draw as sessionDraw } from '../../features/rail/store'
import { plainTitle } from '../../features/rail/title'
import { loadPermMode, stagedPerm } from '../../features/settings/source'
import * as transcript from '../../features/transcript/mount'
import { down as scrollTranscriptDown } from '../../features/transcript/tail'
import { wsSetRoot as wsSetRootImpl } from '../../features/workspace/source'
import { currentTurn as wsCurrentTurn } from '../../features/workspace/store'
import { t } from '../../i18n/t'
import { $ } from '../../lib/dom'
import { formatDuration as dur } from '../../lib/duration'
import { show as ntfPush } from '../../lib/notifications'
import { current as sessionCurrent, setCurrent as sessionSet } from '../../lib/session'
import { hasNamingFlag, hasTurnDuration } from '../../rpc/capabilities'
import { gateway } from '../../rpc/gateway'
import { ask as confirmAsk } from '../confirm'
import { set as setCtx } from '../ctxChip'
import { clearStaged as clearStagedHarness, staged as stagedHarness } from '../harness'
import { ds, sources } from '../sources'
import { load as loadTier } from '../tier'
import { show as toast } from '../toast'
import { clearStaged as clearStagedWorkdir, staged as stagedWorkdir } from '../workdir'
import { ask, noteRow, noteSay, pitch, splitAtts } from './conversation'
import { generation } from './generation'
import { beginNaming, namingDeclined } from './naming'
import { get, isActiveRuntime, isDraft as registryIsDraft, mint, subscribe, viewRuntime } from './registry'
import { rows as sessionRows, sess } from './rows'

import type { TurnEvent, TurnSnapshot } from '../../features/composer/turn'
import type { SessRow } from '../../features/rail/types'
import type { Staging } from './staging'

/* The composer island's stores hold the visible conversation's phase and
   queue, so a runtime that is NOT on screen keeps its own copies here and the
   one that is reads and writes theirs. */
const IDLE: TurnSnapshot = { phase: 'idle', cancellable: false, resume: null }

interface OpenCall { h: { done(...a: unknown[]): void }; st: unknown; t0: number; name: string; args: unknown }

export class SessionRuntime {
  /* null while this is the new-task screen: a draft is a conversation that
     does not exist on disk yet, and `mint` gives it its key. */
  key: string | null

  subscriptionId: string | null = null

  /* Only read while this runtime is off screen; the visible one's phase is the
     composer island's, which is what `goState` and the stop button read. */
  phase: TurnSnapshot = IDLE

  st: ReturnType<typeof transcript.step> | null = null
  steps: Array<ReturnType<typeof transcript.step>> = []
  say = ''
  open = new Map<string, OpenCall>()
  sawEpisode = false
  startedAt = 0
  answerAt = 0

  /* Only read while off screen, same as `phase`. */
  queue: string[] = []

  /* The mid-turn messages already drawn from `message.injected`, by turn id,
     each with the bubble drawn for it. Kept past the end of the turn on
     purpose: the fallback -- the host turn ended before its next drain -- runs
     the same message as a turn of its own and opens it with `message.start`
     under the same id, which arrives after this turn has been reset. The
     bubble is what that frame re-files, from inside the ended turn to the head
     of the turn now opening. */
  injected = new Map<string, number>()

  /* The message a retry would re-send. Held here rather than read back off the
     last `.ask` bubble, which is markup and may belong to another
     conversation. */
  lastAsk = ''

  /* The wait for a generated title: the opening line to fall back to, and the
     backstop timer. */
  naming: { fallback: string; timer: ReturnType<typeof setTimeout> } | null = null

  /* A model, tier and permission mode picked while this was still a draft.
     Applied to the conversation the first message mints, then forgotten. */
  staged: Staging = { model: null, tier: null, perm: null }

  wsRoot: string | null = null

  /* The transcript lane's DOM host, held while this conversation is off screen
     with a turn still running: the streamed tokens live in the lane's store and
     the lane's life is tied to this element (features/transcript/mount.tsx). */
  host: HTMLElement | null = null

  /* The frames that arrived while this conversation was off screen, or null
     when it is not holding a turn at all. Non-null IS "parked". */
  events: unknown[] | null = null

  /* The rest of what a look at another conversation would otherwise zero: the
     live clock's anchor, the workspace record and which pane was open. */
  liveT0 = 0
  ws: unknown = null
  pane: { tab: string; picked: unknown } | null = null

  constructor(key: string | null) {
    this.key = key
    this.startedAt = Date.now()
  }

  /* A phase event for THIS conversation, whether or not it is on screen. The
     one on screen moves the island's machine, which is what the composer and
     the stop button read; one that is holding a turn off screen folds the
     event into the copy it kept. A conversation that is neither -- no turn of
     its own and not being looked at -- has nowhere to put it, and dropping it
     is what the page has always done. */
  dispatch(event: TurnEvent): void {
    /* The one on screen keeps its phase in the island's machine, which is what
       the composer and the stop button read; `phase` below is only the copy a
       conversation takes when it leaves, so nothing writes it here. */
    if (isActiveRuntime(this)) { turn.dispatch(event); return }
    if (this.events) this.phase = reduce(this.phase, event)
  }
}

/* ---- the turn's own state ---------------------------------------------- */

/** The open step, the streamed say buffer, the calls in flight, the stamps. */
export const state = (): SessionRuntime => viewRuntime()

/** Back to a conversation with no turn running. */
export function reset(rt: SessionRuntime = viewRuntime()): void {
  transcript.stopStream()
  rt.st = null; rt.steps = []; rt.say = ''; rt.open.clear(); rt.sawEpisode = false
  rt.startedAt = Date.now()
  rt.answerAt = 0
}

export function ensureStep(rt: SessionRuntime = viewRuntime()) {
  if (!rt.st) { rt.st = transcript.step(); rt.steps.push(rt.st) }
  return rt.st
}

/** Only the local buffer resets; the step keeps its streamed narration. */
export function flushSay(rt: SessionRuntime = viewRuntime()): void {
  rt.say = ''
}

/* ---- the clock -------------------------------------------------------- */

/* How long the turn took. The runtime measures it and sends it on
   message.complete, so that number is the one to draw: timing it here measures
   when the events reached this page, which is a different span and one a
   reload cannot reproduce -- a page that was not open for the turn never saw
   its start, so the same turn came out one number live and another on replay.

   The local subtraction stays as the fallback, for a server too old to send
   the field and for a stop, which ends the turn without a message.complete. It
   stops at the last word of the answer, which is NOT what a reloaded
   transcript computes: the stamps on disk are written by `_save_turn` after
   the turn is fully unwound, so post-turn housekeeping is inside their span
   and outside this one. Hence a fallback, not a second opinion. */
export function duration(serverMs?: number, rt: SessionRuntime = viewRuntime()): string | null {
  if (hasTurnDuration(serverMs)) return dur(Math.max(serverMs as number, 1000))
  const ms = (rt.answerAt || Date.now()) - rt.startedAt
  return rt.startedAt ? dur(Math.max(ms, 1000)) : null
}

export const fmtTok = (n: number): string => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n))

/* The turn folds ONCE, at message.complete: mid-stream nothing can tell the
   answer from another line of narration. */
export function finishTurn(payload: unknown, rt: SessionRuntime = viewRuntime()): void {
  const p = (payload || {}) as { usage?: Record<string, number>; duration_ms?: number }
  const usage = p.usage || {}
  transcript.killStatus()
  /* The island promotes the streamed prose into the answer block where the
     prose stood, merges the silent stretches and folds the turn. */
  transcript.finishTurn(rt.st, rt.steps, duration(p.duration_ms, rt))
  /* The turn's products close it, after the answer and after any note: the
     bar is the last line of a turn, and it is only drawn once the turn is
     over -- nothing grows it mid-flight. */
  transcript.artifacts(wsCurrentTurn())
  turn.dispatch({ type: 'idle' })
  const inTok = usage.input_tokens || usage.prompt_tokens || 0
  /* The window fill is the turn's prompt, not the running total. */
  setCtx(usage.context_used || inTok, usage.context_max as number)
  const s = sess(sessionCurrent())
  if (s && rt.say.trim()) s.last = rt.say.trim().split('\n')[0]!.slice(0, 60)
  touchSession(sessionCurrent(), rt.say)
  // OS notification when the answer lands while the window is in the
  // background; ntfPush itself checks focus and the user's preference.
  ntfPush(t('gui.set.ntf.done'), (s && s.title) || rt.say.trim().slice(0, 80))
  reset(rt)
  drawMeter(); goState(); sessionDraw(); scrollTranscriptDown()
  const nx = queueShift()
  if (nx !== undefined) send(nx)
}

/* ---- sending ---------------------------------------------------------- */

/* The attachment note baked into the message is the record of what was handed
   over -- the reader's own bubble renders its chips from it, and it is what
   survives into session history. So the paths ride to the model as a typed
   `media` field as well, recovered from that same note rather than threaded
   separately: a queued message is a plain string by the time it is drained,
   and the draft path sends from a second place, so deriving here covers all
   three with one rule. */
export const mediaOf = (text: unknown): { media?: string[] } => {
  const paths = splitAtts(String(text)).atts
  return paths.length ? { media: paths } : {}
}

/* Into the turn that is already running: the server merges it at that turn's
   next gap, so the reader can correct it while it works instead of waiting for
   the answer to a question they have already changed their mind about.

   Nothing here touches the runtime. The turn under way is still the turn, its
   clock and its retry text are still its own, and no bubble is drawn from this
   side -- `message.injected` draws it, in every window at once, which is the
   only way the sender and a second tab can agree on where it sits.

   Any refusal falls back to the queue the composer has always had: an old
   gateway that does not know the field (-32602), a lane that went idle between
   the busy check and the call (-32003), and a socket that dropped all look the
   same to the reader, and the message is still in front of them either way. */
export function sendMidTurn(text: string): void {
  const current = sessionCurrent()
  /* No conversation, no turn to merge into: the caller's guard reads the
     registry, this one reads the key actually being sent, and a send with none
     is refused by the gateway rather than queued. */
  if (!current) { queuePush(text); return }
  gateway().call('turn.send', { session_key: current, content: text, busy: 'inject', ...mediaOf(text) })
    .catch((e: unknown) => {
      queuePush(text)
      const err = e as { message?: string }
      noteRow(t('gui.err.send'), err.message === 'not connected' ? t('gui.err.disconnected') : (err.message || String(e)),
        { retry: () => send(text) })
    })
}

/** Send, into the turn that is running or as one of its own. */
export function send(text: string): void {
  /* There is no turn to merge into until the conversation exists. A draft's
     first message is still being turned into one -- `registryIsDraft` is
     already false while `promoting` runs its staged writes -- so a second
     message typed a moment later would call the gateway with no session key at
     all, which is a refusal the reader sees as a red row over a message the
     queue then delivers anyway. It waits in the queue, as it always has. */
  if (turn.busy() && !registryIsDraft() && !promoting) { sendMidTurn(text); return }
  if (turn.busy()) { queuePush(text); return }
  const rt = viewRuntime()
  /* What a retry re-sends. Recorded after the attachment note is folded in, so
     the second attempt carries the same message as the first. */
  rt.lastAsk = text
  ask(text)
  turn.dispatch({ type: 'send' })
  reset(rt)
  drawMeter(); goState(); sessionDraw()
  const failed = (e: unknown) => {
    const err = e as { message?: string }
    transcript.killStatus()
    turn.dispatch({ type: 'idle' })
    noteRow(t('gui.err.send'), err.message === 'not connected' ? t('gui.err.disconnected') : (err.message || String(e)),
      { retry: () => send(text) })
    goState(); drawMeter()
  }
  if (!registryIsDraft()) {
    sendOnSession(text, failed)
    return
  }
  // The draft becomes a real session here, on its first message.
  ;(async () => {
    const id = await openConversation(rowPreview(text))
    beginNaming(text)
    /* `sessionCurrent()` rather than `id`, which is what this has always sent:
       the two differ only when the reader opened another conversation inside
       the promotion above, and making them agree is a fix about where a raced
       first message lands, not about this one. Left alone deliberately. */
    const sent = await gateway().call('turn.send', { session_key: sessionCurrent() as string, content: text, ...mediaOf(text) })
    if (hasNamingFlag(sent) && sent.naming === false) namingDeclined(id as string)
  })().catch(failed)
}

/* A send onto a conversation that already exists, held until that conversation
 * is finished being made.
 *
 * A promotion the ROSTER started may still be running. It moves the pointer and
 * lowers the draft flag before awaiting the staged model, tier and permission
 * writes and the subscription, so the page reads as a settled conversation
 * while the draft's own settings are not on it yet and its events have nowhere
 * to arrive. Sending into that window starts the first turn on a conversation
 * that is half made, under settings the reader chose and did not get.
 *
 * Joined rather than queued, because `openConversation` answers exactly when
 * that setup is complete, which is exactly when this send is safe. The guard is
 * on this function rather than on its caller so that the wait cannot be
 * bypassed by a second way in -- the caller is one line either way. */
export function sendOnSession(text: string, failed: (e: unknown) => void): void {
  if (promoting) {
    openConversation().then(() => dispatchSend(text, failed), failed)
    return
  }
  dispatchSend(text, failed)
}

export function dispatchSend(text: string, failed: (e: unknown) => void): void {
  const current = sessionCurrent()
  touchSession(current, text)
  beginNaming(text)
  /* `=== false`, not falsy: a server too old to carry the field says nothing
     at all, and reading that as "declined" would tear down a placeholder
     while a title really is on its way. Which servers carry it is
     rpc/capabilities.ts's question; what the verdict means stays here. */
  gateway().call('turn.send', { session_key: current as string, content: text, ...mediaOf(text) })
    .then((r) => { if (hasNamingFlag(r) && r.naming === false) namingDeclined(current as string) })
    .catch(failed)
}

/* The stop button: cancel the turn the reader started. */
export function stop(): void {
  /* A runtime turn (a delegated result re-entering) is NOT cancellable:
   turn.cancel resolves only handles turn.send registered, and the stop
   button claiming the UI here would reset the stage while the delegated
   deltas are still streaming into it. The reader's stop does nothing until
   the turn is one they can stop. */
  if (!turn.cancellable()) return
  const owner = sessionCurrent()
  turn.dispatch({ type: 'cancel' })
  gateway().call('turn.cancel', { session_key: owner as string })
    .then(() => {
      get(owner)?.dispatch({ type: 'idle' })
      if (sessionCurrent() === owner) { drawMeter(); goState(); sessionDraw(); drain() }
    }, () => {
      get(owner)?.dispatch({ type: 'idle' })
      if (sessionCurrent() === owner) { drawMeter(); goState(); sessionDraw() }
    })
  softStop(true)
}

/* A stop is not a failure: everything already streamed stays on the stage, and
   the only new line is the note that a person asked for the stop. Shared by
   the button and by the cancelled event another client can cause.

   The turn ends the same way a finished one does -- finishTurn promotes the
   prose that streamed into the answer block. Sealing the open step instead
   left that prose as narration, which the fold then closed over: the reader
   pressed stop and watched the half-written answer disappear behind
   "done", under a note saying the output was kept. */
export function softStop(keepCancelling?: boolean, rt: SessionRuntime = viewRuntime()): void {
  transcript.killStatus()
  transcript.finishTurn(rt.st, rt.steps, duration(undefined, rt))
  if (!keepCancelling) turn.dispatch({ type: 'idle' })
  /* Only promise the output was kept when there is output above to keep. */
  noteRow(t(transcript.turnKept() ? 'gui.halted' : 'gui.halted_bare'), '',
    { quiet: true, host: $('#stage') })
  /* A stopped turn still produced what it produced. */
  transcript.artifacts(wsCurrentTurn())
  reset(rt)
  drawMeter(); goState(); sessionDraw()
}

/* Queued messages were waiting for the engine, and a stop is the engine coming
   free -- so the queue drains into it, same as after a finished turn. */
export function drain(): void {
  if (turn.busy()) return
  const nx = queueShift()
  if (nx !== undefined) send(nx)
}

/* ---- promotion --------------------------------------------------------- */

/* A conversation to work in, made if there is not one yet.
 *
 * The draft on screen becomes a real session here, and two callers want that
 * while only one of them has a message: `send` above promotes on the reader's
 * first send, and the sub-agent roster's new-instance button promotes because
 * an instance has to live inside a conversation and pressing it is the reader
 * asking for both at once. What is here is what both need -- the row, the
 * pointer, the composer's draft, the staged settings and the subscription.
 * What only a send needs -- the naming request, the row's preview line --
 * stays with the send.
 *
 * Answers the open conversation when there already is one rather than refusing:
 * "give me a conversation" is what both callers actually want, and a seam that
 * has to be asked separately whether it applies is one a caller can get wrong.
 */
let promoting: Promise<string | null> | null = null

export async function openConversation(
  preview?: string,
  atPointer?: (id: string) => void,
): Promise<string | null> {
  /* One promotion, however many callers ask inside it, and it is asked about
     FIRST -- before the flag. `promote` lowers the draft partway through, while
     the staged writes and the subscription are still running, so a caller
     arriving in that window reads the page as already settled. Checking the
     flag first answered it "there is one, carry on" and sent it straight past
     the setup that had not finished: the send then started the first turn under
     settings the reader chose and did not get.

     Shared rather than refused, because both callers want the same answer and
     both are owed it -- ahead of the flag, a second press of the new-task
     screen's button also stops minting a SECOND conversation for one visit.
     The joiner's hook runs on the way out instead of from inside, which is the
     same guarantee later: what it is for is a pointer that has already moved. */
  if (promoting) {
    const joined = await promoting
    if (atPointer) atPointer(joined as string)
    return joined
  }
  if (!registryIsDraft()) return sessionCurrent()
  promoting = promote(preview, atPointer)
  try {
    return await promoting
  } finally {
    promoting = null
  }
}

export async function promote(preview?: string, atPointer?: (id: string) => void): Promise<string> {
  /* Taken before the first await, so a caller reporting on THIS conversation
     reads the view as it stood when the promotion began rather than whatever
     the reader has opened since. */
  const gen = generation()
  const draftRt = viewRuntime()
  /* The folder the draft picked goes on the create, because the create is the
     only moment the engine takes one (raven/rpc/methods/session.py); the row
     carries it too, so the rail groups the conversation and the chip reports it
     before the list is read back. Spent once the session exists, and left
     staged when the create fails, since the draft is still on screen. */
  const workdir = stagedWorkdir()
  /* Beside the folder and for its reason: the create is the only moment the
     engine freezes a Harness onto a conversation, so a Persona the reader
     picked off the wall rides along here or not at all. */
  const harness = stagedHarness()
  const r = await gateway().call('session.create', {
    ...(workdir ? { workdir } : {}),
    ...(harness ? { harness } : {}),
  })
  setWsRoot(draftRt, r.info && r.info.cwd)
  const s: SessRow = {
    id: r.session_id, title: t('gui.new_task'), last: preview || t('gui.sess.not_started'),
    when: t('gui.sess.just_now'), at: Math.floor(Date.now() / 1000), run: null, live: true, persisted: false,
    workdir: workdir || null,
  }
  sessionRows().unshift(s); sessionSet(s.id)
  clearStagedWorkdir()
  clearStagedHarness()
  /* The draft IS the conversation now: everything it was holding -- the staged
     model, tier and permission mode, the lane it drew into -- belongs to the
     session that was just minted. */
  mint(draftRt, s.id)
  /* The pointer has moved, and a caller with something to file under the new
     conversation files it HERE rather than after the round trips below: the
     send records the turn's owner at exactly this point, and a reader switching
     conversations inside the staged-settings calls would otherwise leave the
     in-flight turn parked under the wrong one. */
  if (atPointer) atPointer(s.id)
  // The composer was owned by 'new' until this point; keep later keystrokes
  // filed under the session that just came into being.
  /* One announcement, two owners: the composer's draft text and the desk's
     palette are both filed under the draft and have to follow it to the
     session (see features/desk/store.ts's claimDraft). */
  claimComposerDraft(sessionCurrent())
  claimDeskDraft(sessionCurrent())
  await applyStagedModel(draftRt, s.id, gen)
  await applyStagedTier(draftRt, s.id)
  await applyStagedPerm(draftRt, s.id)
  sessionDraw()
  await subscribe(s.id)
  return s.id
}

/** Whether the page is on a draft rather than in a conversation. */
export const isDraft = (): boolean => registryIsDraft()

/* ---- the three staged picks -------------------------------------------- */

export function setWsRoot(rt: SessionRuntime, root: unknown): void {
  rt.wsRoot = (root as string) || null
  wsSetRootImpl(root)
}

/* Apply a staged draft pick to the session the first message just minted.
   Awaited before that turn is sent, so the turn runs on the chosen model rather
   than racing the write.

   ``gen`` is the view as it stood when the send began, not when this runs: a
   refusal reconciles the chip to what THAT session actually runs, and the
   reader may have opened another conversation while the write was in flight. */
export async function applyStagedModel(rt: SessionRuntime, sessionId: string, gen: number): Promise<void> {
  if (!rt.staged.model) return
  const pm = rt.staged.model; rt.staged.model = null
  try {
    const r = await gateway().call('config.set', { key: 'model', value: pm.model, provider: pm.provider, session_id: sessionId })
    /* A refusal RESOLVES. Watching only for a raise is how a first run went
       quiet here: with no loop to bind to, the server answers applied:false,
       and the chip kept a model the session does not have. */
    if (r && r.applied === false) {
      toast(t('gui.op.switch_failed', { detail: t('gui.model.refused') }))
      void loadProviders(sessionId, gen)
    }
  } catch (e) {
    // Said out loud, not just reversed: the pick was announced as staged, so a
    // silent chip flip back would be an unexplained contradiction.
    toast(t('gui.op.switch_failed', { detail: detailOf(e) }))
    void loadProviders(sessionId, gen)
  }
}

/* The tier picked while this was still a draft, written now that there is a
   session_key to write it under. Awaited before the turn is sent, for the same
   reason the model is: the turn dispatches sub-agents, and a tier that lands
   after it starts is a tier that turn did not run at.

   A failure is said rather than reversed silently, and then re-read: the chip is
   showing the staged tier as though it were in force, so leaving it there after
   a refusal is the one outcome worse than the refusal. */
export async function applyStagedTier(_rt: SessionRuntime, sessionId: string): Promise<void> {
  const mode = stagedTier()
  if (!mode) return
  try {
    await gateway().call('session.set_mode', { session_key: sessionId, mode })
  } catch (e) {
    toast(`${t('gui.tier.title')}: ${detailOf(e)}`)
  }
  void loadTier()
}

/* The permission mode picked while this was still a draft, written now that
   there is a session_id to write it under. Awaited before the turn is sent so
   its first tool call already reads the chosen mode. */
export async function applyStagedPerm(_rt: SessionRuntime, sessionId: string): Promise<void> {
  const mode = stagedPerm()
  if (!mode) return
  try {
    await gateway().call('config.set', { key: 'permissions.mode', value: mode, scope: 'session', session_id: sessionId })
  } catch (e) {
    toast(`${t('gui.perm.title')}: ${detailOf(e)}`)
  }
  void loadPermMode(sessionId)
}

const detailOf = (e: unknown): string => {
  const o = e as { data?: { detail?: string }; message?: string } | null
  return (o && ((o.data && o.data.detail) || o.message)) || String(e)
}

/* ---- what a reader can do to a conversation ---------------------------- */

/** Fork it, and open the fork. */
export const branch = (_text = ''): void => ds('transcript').branch?.(_text)

/** Empty its transcript, from the slash palette. */
export const clear = (): void => ds('composer').slash.find((x) => x.id === 'gui.clear')?.fn()

/* Manual compaction. The runtime already compacts when a prompt outgrows the
   window; this forces the same pass early, which is what you want once the
   earlier half of a session has stopped being useful. */
export async function compressNow(): Promise<void> {
  const key = sessionCurrent()
  if (!key || registryIsDraft()) return
  const line = noteRow(t('gui.compress.running'), '', { quiet: true, host: $('#stage') })
  try {
    const r = await gateway().call('session.compress', { session_id: key })
    noteSay(line, r.removed
      ? t('gui.compress.done', { n: r.removed, before: fmtTok(r.before_tokens), after: fmtTok(r.after_tokens) })
      : t('gui.compress.noop'), '')
  } catch (e) {
    line.remove()
    /* Same rule as the clear handler, and here it is the failure path that
       needed it: `line` is a segment in the lane this started in, so a switch
       has already dropped it and writing to it lands nowhere -- but a bare
       noteRow asks for the CURRENT lane, so a compaction that failed for the
       conversation being left posted its error over the one being read. */
    const detail = detailOf(e)
    if (key !== sessionCurrent()) {
      const s = sess(key)
      toast(t('gui.sess.compress_failed', { title: plainTitle((s && s.title) || key), detail }))
      return
    }
    noteRow(t('gui.compress.fail', { err: '' }).replace(/[:：]\s*$/, ''), detail)
  }
  /* The tail this scrolls is the open conversation's. */
  if (key !== sessionCurrent()) return
  scrollTranscriptDown()
}

/** Compact it now, rather than when the window fills. */
export const compress = (): Promise<void> => compressNow()

/* The slash palette's `/clear`. */
export function clearConversation(): void {
  confirmAsk(t('gui.clear_title'), t('gui.clear_body'), t('gui.clear_yes'), () => {
    /* Which conversation was cleared, read once. The reply used to ask for the
     pointer again, and by then it can name a different one: clearing A and
     clicking B mid-flight wiped B's stage, gave B the new-task layout, and
     stamped B's row "cleared" while B's transcript sat untouched on disk. */
    const key = sessionCurrent() as string
    gateway().call('session.clear', { session_id: key })
      .then(() => {
        /* The row belongs to the conversation that was cleared, wherever the
         reader is now -- it really is empty, and a list that says otherwise
         is wrong until the next reload. */
        const s = sess(key); if (s) s.last = t('gui.sess.cleared')
        sessionDraw()
        /* The stage and the meter are the open conversation's, so they are
         only this reply's to touch while it IS the open one. */
        if (key !== sessionCurrent()) return
        $('#stage')!.innerHTML = ''; pitch()
        drawMeter()
      })
      /* A failure is news for the conversation it happened to. Posted on
         whatever is open, it reads as that conversation refusing to clear;
         dropped, the reader walks away believing a session was wiped when its
         transcript is still on disk, which is the one direction where being
         wrong costs something. So it goes where the other session actions put
         theirs -- a toast naming the conversation, same as delete, archive and
         rename in this file. Not the row's `status = 'err'` channel: nothing
         clears that (a session open only clears 'done'), so it would pin a
         failure marker on a row whose conversation is fine once opened. */
      .catch((e) => {
        const detail = detailOf(e)
        if (key !== sessionCurrent()) {
          const s = sess(key)
          toast(t('gui.sess.clear_failed', { title: plainTitle((s && s.title) || key), detail }))
          return
        }
        noteRow(t('gui.clear_title'), detail)
      })
  })
}

/** Delete it, transcript and all. */
export const remove = (s: SessRow): void => ds('rail').remove?.(s)

/** Hide it from the rail, with an undo. */
export const archive = (s: SessRow): void => ds('rail').archive?.(s)

/** Pin it to the top of the rail. */
export const pin = (id: string, pinned: boolean): void => ds('rail').pin?.(id, pinned)

/** Persist a title the reader typed. */
export const rename = (id: string, title: string, previous: string): void =>
  ds('rail').renamed?.(id, title, previous)

/** Delete every conversation, from the settings page. */
export const deleteAll = (): void => ds('rail').deleteAll?.()

/* ---- installs ---------------------------------------------------------- */

/* The two actions, installed on the source the composer already asks. `stop`
   is the go button's other half and the Escape key's; `send` is what the island
   hands a folded message to. */
export function installComposerActions(): void {
  if (!sources.composer) return
  sources.composer.send = send
  /* The one way anything outside the dock can get a conversation to work in.
   The composer has always made one on its first send; this is the same
   promotion offered by name, for a caller that needs the conversation and has
   no message to start it with. */
  sources.composer.startConversation = () => openConversation().then((id) => id as string)
  sources.composer.stop = stop
}

/* The slash palette's two session verbs, replaced on the rows the composer
   island declares. */
export function installSlashActions(): void {
  ds('composer').slash.forEach((x) => {
    if (x.id === 'gui.clear') x.fn = clearConversation
    if (x.id === 'gui.compress') x.fn = compressNow
  })
}

/* Test seam only: the promotion in flight is the module's, and a case that left
   one pending would hand it to the next. */
export function _resetForTests(): void {
  promoting = null
}
