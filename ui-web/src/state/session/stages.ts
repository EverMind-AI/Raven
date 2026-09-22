/* What a turn event means to the conversation it belongs to.
 *
 * One stage per contract event, declared in the order the page has always
 * handled them, and one exhaustive `switch` that picks the stage -- so a member
 * added to `TurnEvent` is a compile error here rather than a frame the page
 * drops in silence. The three the contract declares and the page does not draw
 * (`tool.progress`, `dag.node_stalled`, `media`) get a stage that says so; the
 * two the page used to handle and nothing ever sent (`cron.started`,
 * `cron.finished`) are gone.
 *
 * Every stage takes the runtime the frame is about, which is what replaced the
 * page-level `live` object AND the page-level name for whose turn was running:
 * a frame's conversation is the one that holds the open step, the say buffer
 * and the calls in flight it is going to touch, and the one the page is showing
 * is the only conversation a frame is ever applied to -- `stream` in
 * ./pipeline.ts holds the others' frames rather than applying them.
 */

import { drawMeter, goPaint as goState, turn } from '../../features/composer/mount'
import * as dagRun from '../../features/dag/mount'
import { fromStarted } from '../../features/dag/nodes'
import { touchSession } from '../../features/rail/source'
import { draw as sessionDraw } from '../../features/rail/store'
import { directEvent } from '../../features/subagents/store'
import * as transcript from '../../features/transcript/mount'
import { cleanPreview, okOf } from '../../features/transcript/source'
import { wsOnTool, wsOnToolDone } from '../../features/workspace/record'
import { advanceTurn, currentTurn as wsCurrentTurn } from '../../features/workspace/store'
import { t } from '../../i18n/t'
import { current as sessionCurrent } from '../../lib/session'
import { hasToolOk } from '../../rpc/capabilities'
import { session as sheetSession } from '../sheetRack'
import { ds, sources } from '../sources'
import { show as toast } from '../toast'
import { ask, noteRow, unask } from './conversation'
import { namingEnded, settleNaming } from './naming'
import { viewRuntime } from './registry'
import {
  drain, ensureStep, finishTurn, flushSay, send, softStop,
} from './runtime'

import type { DirectTarget, TurnEvent } from '../../rpc/generated'
import type { SessionRuntime } from './runtime'

type EventType = TurnEvent['type']
type Of<T extends EventType> = Extract<TurnEvent, { type: T }>

export interface Stage {
  readonly handles: readonly EventType[]
  run(rt: SessionRuntime, ev: TurnEvent): void
}

/* One stage, with its payload typed by the member it handles. */
function arm<T extends EventType>(
  handles: T | readonly T[],
  run: (rt: SessionRuntime, p: Of<T>['payload']) => void,
): Stage {
  return {
    handles: (Array.isArray(handles) ? handles : [handles]) as readonly EventType[],
    run: (rt, ev) => (run as (r: SessionRuntime, p: unknown) => void)(rt, (ev as { payload?: unknown }).payload),
  }
}

/* A stage that exists to say the page draws nothing for these, which is a
   different statement from a frame no arm names. */
const unhandled = (handles: readonly EventType[]): Stage => ({ handles, run: () => {} })

/* The stages, in the order the page has always taken them. A frame carries one
   type, so the order is the table rather than a pipeline the frame runs down --
   it is here because reading them in this order is how the turn reads. */
export const STAGES: readonly Stage[] = [
  arm('message.start', (rt, p) => {
    /* Read BEFORE the phase is set: the window that sent this turn has already
       drawn the question; a window that is only watching has not.

       A message already drawn from `message.injected` is re-filed rather than
       drawn twice: the host turn ended before draining it, so it is running as
       a turn of its own, and the bubble that was drawn inside the ended turn
       belongs at the head of this one -- below that turn's answer, which is
       where a reload puts it. Taking it back and letting the ordinary path
       draw it is what moves it. */
    if (!turn.busy() && p.content) {
      const drawn = rt.injected.get(p.turn_id)
      if (drawn !== undefined) { unask(drawn); rt.injected.delete(p.turn_id) }
      ask(p.content)
    }
    if (p.content) touchSession(sessionCurrent(), p.content)
    rt.dispatch({ type: 'stream', cancellable: true }); goState(); drawMeter()
    advanceTurn()
  }),

  /* A message merged into the turn already running. It joins the turn rather
     than opening one, so none of the turn bookkeeping runs: no workspace turn,
     no phase change, no clock re-anchored. The open step is let go of rather
     than sealed -- the transcript appends at the tail, so the narration that
     follows opens a step BELOW the bubble instead of writing into the step
     that was open above it. */
  arm('message.injected', (rt, p) => {
    /* Marked as mid-turn: every scan that walks back to find where a turn
       began -- the fold, the merge of silent steps, the stop note's "output
       above" -- would otherwise read this bubble as the start of one and file
       the work that follows it above it. */
    rt.injected.set(p.turn_id, ask(p.content, undefined, { midTurn: true }))
    rt.st = null
    touchSession(sessionCurrent(), p.content)
  }),

  arm('turn.started', (rt, p) => {
    /* A turn the RUNTIME opened (a delegated result re-entering) has begun.
       The spine suppresses message.start for these, so this event is the whole
       opening: the workspace turn advances, the client enters the busy state
       (a queued send must wait for this turn's message.complete), and the
       delivery row is drawn HERE -- this is the moment the result is actually
       visible, not the moment it was submitted while its parent still owned
       the lane. `delegated` carries the identity AND the injected text, the
       same identity a stored entry carries on replay, so the two views draw
       the same row at the same place. */
    advanceTurn()
    if (p.delegated) {
      const d = p.delegated
      const isDag = d.kind === 'dag'
      transcript.delivered({
        label: d.label || '',
        isDag,
        status: d.status,
        body: d.content || '',
        open: () => {
          if (isDag) { ds('transcript').openDagRun!(d.run_id || d.label || ''); return }
          ds('transcript').openSpawn!('', d.label || '', d.node_id)
        },
      })
    }
    /* Re-anchor the fallback clock. Nobody typed this turn, so no send ran to
       move it, and it was last set when the PARENT turn ended -- with the
       sub-agent's whole run sitting in between. The server's `duration_ms`
       covers the number that gets drawn; this covers the paths that fall back
       to timing it here, a stop being the one that always does. */
    rt.startedAt = Date.now()
    rt.answerAt = 0
    rt.dispatch({ type: 'stream', cancellable: false }); goState(); drawMeter()
  }),

  arm('episode.start', (rt) => {
    if (rt.st) { rt.st.seal() }
    flushSay(rt)
    rt.st = transcript.step(); rt.steps.push(rt.st); rt.sawEpisode = true
  }),

  /* The server named the session. Replaces whatever the row shows without
     comparing: the event is emitted only when the title actually changed. */
  arm('session.titled', (_rt, p) => { settleNaming(p.session_id, p.title) }),

  /* No title is coming after all: the model answered without calling the
     naming tool, the call outran its budget, this code raised, or a person
     named the session while it ran. `namingEnded` decides what that means for
     the row -- the last of those is not settled onto the opening line. The
     timer is only a backstop for a server that says neither of these. */
  arm('session.naming_ended', (_rt, p) => { void namingEnded(p.session_id, p.reason) }),

  arm('notice', (rt, p) => {
    transcript.killStatus()
    /* Seals the open step first: this ends the turn, so the streamed prose
       above stays where it was said. */
    if (rt.st) { rt.st.seal(); rt.st = null }
    flushSay(rt)
    noteRow(t('gui.notice.' + (p.kind || ''), null, p.kind || ''), p.detail || '', { quiet: true })
  }),

  /* The smart-mode reviewer runs inside the tool dispatch; name the pause. */
  arm('permission.review', (_rt, p) => {
    if (p.phase === 'started') transcript.status(t('gui.perm.reviewing'))
    else transcript.killStatus()
  }),

  arm('thinking.delta', (rt, p) => {
    transcript.killStatus()
    ensureStep(rt).thinkAppend(p.text || '')
  }),

  arm('token.delta', (rt, p) => {
    transcript.killStatus()
    const st = ensureStep(rt)
    /* sayDelta folds a finished thought before the prose lands. */
    st.sayDelta(p.text || '')
    rt.say += p.text || ''
    rt.answerAt = Date.now()
  }),

  arm('tool.start', (rt, p) => {
    transcript.killStatus()
    const st = ensureStep(rt)
    /* The call id travels with the row: a `run_subagent_dag` names it on every
       progress event, and it is what binds the graph to this card rather than to
       whichever dag card happened to be the newest. */
    const h = st.tool(p.name || 'tool', p.arguments, p.display, p.tool_call_id)
    rt.open.set(p.tool_call_id, { h, st, t0: Date.now(), name: p.name, args: p.arguments })
    /* The workspace panel gets the WHOLE argument object, not the one-line
       display string: edit_file's old_text/new_text is the diff. */
    if (typeof wsOnTool === 'function') wsOnTool(p.name, p.arguments, false)
  }),

  arm('tool.complete', (rt, p) => {
    if (p.metadata) transcript.delivery(wsCurrentTurn(), p.metadata, p.tool_call_id)
    const o = rt.open.get(p.tool_call_id)
    if (!o) return
    rt.open.delete(p.tool_call_id)
    const preview = cleanPreview(p.result_preview).split('\n').map((l) => l.slice(0, 160)).join('\n')
    // The emit site's verdict is authoritative; the text heuristic survived
    // only as the backstop for an old server that does not send the field.
    const ok = hasToolOk(p.ok) ? p.ok : okOf(o.name || '', preview)
    const took = Date.now() - o.t0
    o.h.done(ok, preview, took, null, p.truncated)
    /* p.diff is the real change on disk -- the only place a whole-file write's
       previous content survives; p.file_change says whether there was a file
       there at all. */
    if (typeof wsOnToolDone === 'function') {
      wsOnToolDone(o.name, o.args, ok, preview, took, p.diff, p.file_change)
    }
  }),

  /* Our own cancel already folded and reset the visible turn. The server can
     finish unwinding before turn.cancel replies; only that reply may release
     the queued send. */
  arm('message.complete', (rt, p) => {
    if (turn.phase() === 'cancelling') return
    finishTurn(p, rt)
  }),

  arm('error', (rt, p) => {
    transcript.killStatus()
    /* A cancelled turn is the one "error" a person asked for; the event still
       matters when the cancel came from ANOTHER client on the same session. */
    if (p.reason === 'cancelled_by_client') {
      if (turn.phase() === 'cancelling') return
      if (turn.busy()) softStop(false, rt)
      setTimeout(drain, 400)
      return
    }
    rt.dispatch({ type: 'idle' })
    noteRow(p.message || 'error', p.detail || p.reason || '',
      rt.lastAsk ? { retry: () => send(rt.lastAsk) } : null)
    goState(); drawMeter(); sessionDraw()
  }),

  arm('cron.delivered', (_rt, p) => { toast(t('gui.cron.new_output', { name: p.name })) }),

  /* One-shot reminders whose time passed while the backend was down. Queued
     at bring-up and flushed to the first subscription, so this arrives once
     per restart rather than per job -- the count is the payload's own. */
  arm('cron.missed', (_rt, p) => { toast(t('gui.cron.missed_x', { count: p.count })) }),

  /* The run's own lifecycle, which is not the spawn tool call's: the tool
     returns when the work is dispatched. This is what tells the card who it
     dispatched (`instance`, `agent`, `label`, all on the first frame) and,
     from `running`, the record id its stream is read by. Through the island
     for the same reason the dag events go through it: what a frame means to a
     card is one definition, next to the model it moves. */
  arm('subagent.status', (_rt, p) => {
    transcript.spawnFeed(p)
    sources.tasks?.onSubagentStatus?.(p)
  }),

  /* A result was submitted, not yet visible: the turn it opens is still queued
     behind its parent, so the row does NOT belong here. It arrives with the
     turn's own opening (turn.started, carrying the same identity) -- until then
     this event is nothing, kept for older servers that still send it. */
  unhandled(['subagent.delivered']),

  arm('dag.run_started', (_rt, p) => {
    /* The trail's delegation card paints the same events as the sheet below:
       one feed call per branch, before the sheet's own bookkeeping. */
    transcript.dagFeed('dag.run_started', p)
    /* The graph arrives whole, before any node runs. Filed under the
       conversation it belongs to: a stage only ever runs for the conversation
       on screen, so the current key is the owning key on both paths. */
    /* Through the same adapter the transcript's dag card reads (the bundle's
       features/dag/nodes.ts): the payload was being unpacked field by field here
       as well, so "what a node is" had two definitions that only happened to
       agree. */
    const started = fromStarted(p)
    const key = sheetSession()
    dagRun.start(key, {
      run_id: p.run_id,
      session: key,
      order: started.map((n) => n.id),
      nodes: new Map(started.map((n) => [n.id, n])),
      summary: null, done: false, folded: false,
      task_summary: p.task_summary || null,
    })
    sources.tasks?.onRunStarted?.(p)
  }),

  arm('dag.node_updated', (_rt, p) => {
    transcript.dagFeed('dag.node_updated', p)
    /* Through the island rather than into the run's node map from here: what a
       report means to a node is one definition, next to the model it moves, and
       the copy that lived here had drifted into inventing a clock. */
    dagRun.advance(sheetSession(), p)
    sources.tasks?.onNodeUpdated?.(p)
  }),

  arm('dag.run_completed', (_rt, p) => {
    transcript.dagFeed('dag.run_completed', p)
    dagRun.settle(sheetSession(), p)
    sources.tasks?.onRunCompleted?.(p)
  }),

  /* The trail card alone: one run is held per conversation by design, so a
     replanned run's state just keeps the old graph until the new run's own
     dag.run_started arrives and replaces it wholesale. The tasks panel's
     own row is not so lucky -- there is no second card to swap, so it marks
     the superseded run cancelled itself. */
  arm('dag.run_replanned', (_rt, p) => {
    transcript.dagFeed('dag.run_replanned', p)
    sources.tasks?.onRunReplanned?.(p)
  }),

  /* Declared by the contract and drawn by nothing. Named rather than left to
     fall off the end, so that "the page does not render this" is a decision
     with a place to be revisited. */
  unhandled(['tool.progress', 'dag.node_stalled', 'media']),
]

const BY_TYPE = new Map<EventType, Stage>()
for (const stage of STAGES) for (const type of stage.handles) BY_TYPE.set(type, stage)

/* The compiler's half of the coverage gate: a member added to `TurnEvent` has
   no case here and falls to `assertNever`, which does not accept it. */
function assertNever(ev: never): undefined {
  void ev
  return undefined
}

function stageOf(ev: TurnEvent): Stage | undefined {
  switch (ev.type) {
    case 'message.start':
    case 'message.injected':
    case 'turn.started':
    case 'episode.start':
    case 'session.titled':
    case 'session.naming_ended':
    case 'notice':
    case 'permission.review':
    case 'thinking.delta':
    case 'token.delta':
    case 'tool.start':
    case 'tool.progress':
    case 'tool.complete':
    case 'message.complete':
    case 'error':
    case 'cron.delivered':
    case 'cron.missed':
    case 'subagent.status':
    case 'subagent.delivered':
    case 'dag.run_started':
    case 'dag.node_updated':
    case 'dag.run_completed':
    case 'dag.run_replanned':
    case 'dag.node_stalled':
    case 'media':
      return BY_TYPE.get(ev.type)
    default:
      return assertNever(ev)
  }
}

const targetOf = (ev: TurnEvent): DirectTarget | undefined =>
  (ev.payload as { target?: DirectTarget } | undefined)?.target

/** One turn event, for the conversation this page is showing. */
export function dispatch(ev: unknown): void {
  const frame = (ev || {}) as TurnEvent
  /* A turn addressed to a sub-agent instance, not to this conversation. The
     client holds ONE subscription per session, and the lane stamps every event
     of a direct chat with its `target` precisely so the two can be told apart
     here (see `_subscription` in raven/rpc/spine.py); an untagged frame is the
     main agent's.

     Dropping them was already the effect -- a delta arriving with no turn of
     ours open finds no slot to land in -- but only by accident. Send a direct
     turn while the main agent is answering and that slot exists, and one
     instance's words get typed into the conversation as if raven had said them.
     These belong on the instance's own page, which reads them back through
     `subagents.instance.history`. */
  const target = targetOf(frame)
  if (target) { directEvent(target, frame.type, frame.payload as { content?: string }); return }
  const stage = stageOf(frame)
  if (!stage) return
  stage.run(viewRuntime(), frame)
}
