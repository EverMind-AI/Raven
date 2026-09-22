/* Every frame the gateway pushes, and where it lands.
 *
 * The `event` envelope names the subscription it came in on, the subscription
 * names the conversation, and the conversation is a `SessionRuntime` -- so a
 * frame is routed by the registry and never by "whichever conversation happens
 * to be open". A conversation the reader is looking at paints; one holding a
 * turn off screen buffers, up to a cap, and replays on the way back in; one
 * doing neither has nowhere to put the frame, and dropping it is what the page
 * has always done.
 *
 * The side-channel requests below are the other half: they are not turn events
 * and carry their own conversation id, which is the conversation whose turn is
 * blocked on the answer -- not the one on screen.
 */

import { closeApproval as approvalClose, open as approveSheet, openApproval as approvalSheet } from '../../features/composer/approve'
import { close as clarifyClose, open as clarifySheet } from '../../features/composer/clarify'
import { drawMeter, goPaint as goState } from '../../features/composer/mount'
import { touchSession } from '../../features/rail/source'
import { draw as sessionDraw } from '../../features/rail/store'
import { t } from '../../i18n/t'
import { current as sessionCurrent } from '../../lib/session'
import { gateway } from '../../rpc/gateway'
import { show as toast } from '../toast'
import { bySubscriptionRuntime, dispatchTo, refreshList, viewRuntime } from './registry'
import { sess } from './rows'
import { dispatch } from './stages'

import type { TurnEvent as PhaseEvent } from '../../features/composer/turn'

export { dispatch } from './stages'

/* How many frames a conversation holds for while it is off screen. Past it the
   frame is dropped: a buffer that grows without bound is a page that dies
   rather than a transcript that is complete. */
const BACKLOG_CAP = 4000

/** A phase change for one conversation, and the paint it is owed. */
export function notify(owner: string, event: PhaseEvent): void {
  dispatchTo(owner, event)
  if (owner === sessionCurrent()) { drawMeter(); goState(); sessionDraw(); return }
  /* Asked something in a conversation the reader is not looking at. Nothing
     above repaints for that case -- the phase folds into the conversation's own
     copy and says nothing, and the three writers below paint the open
     conversation only -- so the row was the reader's only possible notice and it
     was never drawn. An approval's sheet mounts only on its own screen and the
     turn behind it waits for the person, so a row that reads like every other
     one is a conversation that never finishes.
     Back to `run` when the wait ends rather than to nothing: the turn that
     raised it is still open. */
  const s = sess(owner)
  if (s) {
    if (event.type === 'wait') s.status = 'ask'
    else if (s.status === 'ask') s.status = 'run'
    touchSession(owner)
  }
  void refreshList()
}

interface Envelope { subscription_id: string; event?: { type?: string; payload?: { reason?: string } } }

/** The `event` envelope: a turn event plus the subscription it arrived on. */
export function stream(frame: unknown): void {
  const params = frame as Envelope
  const rt = bySubscriptionRuntime(params.subscription_id)
  if (!rt) return
  const ev = params.event || {}
  /* Holding a turn off screen: the frames are kept and replayed on the way
     back in, because nothing else has them. */
  if (rt.events) {
    if (rt.events.length < BACKLOG_CAP) rt.events.push(ev)
    if (ev.type === 'message.complete' || ev.type === 'error') {
      const s = sess(rt.key)
      // This branch only ever runs for a conversation the reader is NOT looking
      // at, so a clean finish is news: hold the row on 'done' until they open
      // it. The switch is what clears it. A cancel is a stop somebody chose,
      // not a failure -- no red dot for doing what was asked.
      const cancelled = ev.type === 'error' && (ev.payload || {}).reason === 'cancelled_by_client'
      if (s) { s.status = ev.type === 'error' && !cancelled ? 'err' : 'done'; touchSession(rt.key) }
      void refreshList()
    }
    return
  }
  /* On screen: straight to the stages. */
  if (rt === viewRuntime()) {
    const ended = ev.type === 'message.complete' || ev.type === 'error'
    dispatch(ev)
    /* The row's own badge, when it was the server that put it there: a page
       that reloaded into a running turn reads `run` off session.list, and this
       end is the only notice it will ever get -- the refreshes above are for
       the conversations this page parked, not for the one it is looking at.
       Cleared the way a return from parking clears it (./residency.ts). */
    if (ended) {
      const s = sess(rt.key)
      if (s && s.status === 'run') { s.status = null; sessionDraw() }
    }
    return
  }
  /* Neither: no turn of its own and not being looked at, so there is nowhere
     for the frame to be replayed from. */
}

/* Approval wears the ask_user sheet (approveSheet), so a blocked turn always
 interrupts in the same place and shape. Closing it is a denial, never a
 silent drop -- the engine is waiting on an answer either way.

 Filed under the conversation the server says it asked on behalf of, for the
 same reason clarify.request is (below): the request belongs to the turn that
 raised it, not to whichever conversation the reader had open when it landed.
 A frame that names none -- a dispatch with no conversation to name -- keeps
 the old fallback and docks where the reader is. */
export function confirmRequest(frame: unknown): void {
  const p = frame as { request_id: string; prompt?: string; conversation_id?: string }
  const owner = p.conversation_id || sessionCurrent()!
  notify(owner, { type: 'wait' })
  const say = (answer: boolean) => {
    notify(owner, { type: 'resume' })
    gateway().call('confirm.respond', { request_id: p.request_id, answer }).catch(() => {})
  }
  approveSheet(p.prompt || '', () => say(true), () => say(false), owner)
}

/* The permission gate's ask. Same docking rules as confirm above; what an
 answer is differs: deny (the agent reads the refusal and keeps going), allow
 and save the rule the runtime suggested, or allow once. The request carries
 what the sheet is drawn from -- the layout, the family that words it, who is
 asking, the tool's own account of the call -- and the sheet reads those; the
 transport here only answers. approval.closed below is how the sheet learns
 the question is over without an answer from this page. */
export function approvalRequest(frame: unknown): void {
  const p = frame as {
    approval_id: string; command?: string; description?: string
    suggested_pattern?: string; conversation_id?: string
    kind?: string; family?: string; origin?: { kind: string; name: string }
    evidence?: Record<string, unknown>
  }
  const owner = p.conversation_id || sessionCurrent()!
  notify(owner, { type: 'wait' })
  approvalSheet(
    {
      approvalId: p.approval_id,
      command: p.command || '',
      description: p.description || '',
      suggestedPattern: p.suggested_pattern || '',
      kind: p.kind || 'unknown',
      family: p.family || '',
      origin: p.origin || { kind: '', name: '' },
      evidence: p.evidence || {},
    },
    {
      onChoice: (choice: string, feedback: string, pattern?: string) => {
        notify(owner, { type: 'resume' })
        /* Whether the engine took it: a stale id or a dropped socket answers
           false, and the sheet says the turn is still waiting. */
        return gateway().call('approval.respond', {
          approval_id: p.approval_id, choice, session_id: owner,
          ...(feedback ? { feedback } : {}),
          ...(pattern ? { pattern } : {}),
        }).then((r) => !!(r as { ok?: boolean } | null)?.ok, () => false)
      },
      /* The rule this answer wrote is the reader's to take back; the call that
         was allowed has run, and nothing else about it changes. Named by the
         answer rather than by the rule's text, so the engine takes back what
         this grant put on disk and nothing the reader wrote themselves. */
      onRevoke: () => gateway().call('approval.revoke', { approval_id: p.approval_id })
        .then((r) => !!(r as { ok?: boolean } | null)?.ok, () => false),
    },
    owner,
  )
}

/* The requests still open on the engine, drawn again. A page that reloaded, or
 came back on a fresh socket, has no sheet for a question its conversation is
 still stopped on -- and with no deadline behind the question, nothing would
 ever move again. Each is filed under its own conversation, so one the reader
 is not looking at parks and lights the waiting line. */
export async function replayPendingApprovals(): Promise<void> {
  const r = await gateway().call('approval.pending', {}).catch(() => null)
  const requests = (r as { requests?: unknown[] } | null)?.requests || []
  for (const frame of requests) approvalRequest(frame)
}

export function approvalClosed(frame: unknown): void {
  const p = frame as { approval_id: string; conversation_id?: string; reason?: string }
  notify(p.conversation_id || sessionCurrent()!, { type: 'resume' })
  approvalClose(p.approval_id)
  /* `reason` was arriving and being dropped. A sheet the reader answered closes
   because they answered it, and needs no notice; one that expired closes the
   same way and said nothing at all, so a run whose approvals had merely lapsed
   went on to tell the reader it had hit a system error. The only two reasons
   nobody chose are these, and both mean the action did not run.

   Not scoped to the conversation on screen: a request that lapsed in another
   one stalled that run just as completely, and the reader is the only person
   who can unstick either. */
  if (p.reason === 'timeout' || p.reason === 'error') toast(t('gui.confirm.lapsed'))
}

/* The question the agent asks mid-turn. The sheet is the island's
 (features/composer/clarify.ts); what is left here is the transport and the
 step marking -- it answers with one string per question of the batch it drew,
 whichever control the reader used, including the skip, whose wording is the
 sheet's copy.

 No echo row: the asking tool's own row renders the full question-to-answer
 exchange in its detail once the tool returns, so a separate answered line
 would say the same thing twice. The step is still marked hasQA so the
 exchange keeps its own step instead of merging into a silent work run.

 The step marked is the one on SCREEN, not the asking conversation's: a known
 defect, kept because this refactor changes no behaviour. See the design's
 issue list. */
export function clarifyRequest(frame: unknown): void {
  const p = frame as { request_id: string; conversation_id?: string; index?: number }
  const owner = p.conversation_id || sessionCurrent()!
  notify(owner, { type: 'wait' })
  clarifySheet(p, (answers: string[]) => {
    notify(owner, { type: 'resume' })
    /* Both: `answer` is this question's own, which is all a broker that never
       heard of a batch reads, and `answers` is the whole form, which the broker
       stashes so the questions still to come are answered without asking
       again. */
    gateway().call('clarify.respond', {
      request_id: p.request_id, answer: answers[p.index ?? 0] ?? '', answers,
    }).catch(() => {})
    const open = viewRuntime().st
    if (open) open.hasQA = true
  })
}

/* The question is over and nobody answered it: it timed out, its turn was
 interrupted, or a later question replaced it. Only the server knows -- a sheet
 cannot tell "still waiting" from "waited out" -- so until it said so the sheet
 stayed up offering an answer that had nowhere to go. The turn resumes for the
 same reason it resumes on an answer: it is no longer blocked on the reader. */
export function clarifyClosed(frame: unknown): void {
  const p = frame as { request_id: string; conversation_id?: string }
  notify(p.conversation_id || sessionCurrent()!, { type: 'resume' })
  clarifyClose(p.request_id)
}

/* One handler per name: `gateway().on` keeps a set, so a second registrar
   would be added beside the first rather than replace it. */
export function installPipeline(): void {
  gateway().on('event', stream)
  gateway().on('confirm.request', confirmRequest)
  gateway().on('approval.request', approvalRequest)
  gateway().on('approval.closed', approvalClosed)
  gateway().on('clarify.request', clarifyRequest)
  gateway().on('clarify.closed', clarifyClosed)
}
