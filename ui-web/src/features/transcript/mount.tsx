import { createRoot } from 'react-dom/client'
import { flushSync } from 'react-dom'

import { AgentStageView, StageView } from './TranscriptPage'
import * as store from './store'

import type { AgentCtxLike } from './store'
import type { AnswerData, HistoryMessage, Lane, NoteHandle, StepHandle } from './types'
import type { ReactElement } from 'react'
import type { Root } from 'react-dom/client'

/* The transcript island owns a lane host it appends INSIDE the container --
 * #stage for the conversation, a stage box for an agent pane -- instead of
 * the container itself. The container is shared ground the legacy layers
 * still write to (the .turnlive glyph, `innerHTML = ''` session wipes, the
 * parked-turn machinery that moves #stage's children wholesale), and a React
 * root cannot share a container with foreign writers. The host is one
 * `display: contents` element, so layout, selectors on the segment classes
 * and the scroll math all read exactly as before; a wipe detaches the host
 * and the next segment simply starts a fresh lane (the epoch remount), while
 * a parked host carries its lane with it and resumes on reattach.
 */

interface Mounted {
  host: HTMLElement
  lane: Lane
  root: Root
}

const HOSTS = new WeakMap<HTMLElement, Mounted>()
/* The same records as a walkable list: releasing a lane means finding the
   hosts that are gone, and a WeakMap cannot be walked. */
const MOUNTED: Mounted[] = []
let seq = 0

/* A host the page threw away takes its lane and its React root with it --
   otherwise the lane stays in the store's set and every redraw() renders a
   transcript nobody can see, once more per session switch, unbounded.
   Detached is NOT the whole test: leaving a session mid-turn PARKS the
   transcript, and live/060-parked.js parks it by keeping #stage's children in
   a detached array that restoreTurn appends back, so a disconnected host may
   still be the only copy of a streaming turn. Ask the source before dropping
   one; a canvas with no parking answers no. Called when a fresh host is
   built, which is the moment after a wipe. */
function release(): void {
  const parked = store.source().parked
  for (let i = MOUNTED.length - 1; i >= 0; i -= 1) {
    const m = MOUNTED[i]!
    if (m.host.isConnected || parked?.(m.host)) continue
    MOUNTED.splice(i, 1)
    HOSTS.delete(m.host)
    store.dropLane(m.lane)
    m.root.unmount()
  }
}

function laneIn(container: HTMLElement, main: boolean, view: (lane: Lane) => ReactElement): Lane {
  let host = container.querySelector<HTMLElement>(':scope > [data-tsl]')
  if (host) {
    const known = HOSTS.get(host)
    if (known) return known.lane
    host.remove()
  }
  release()
  host = document.createElement('div')
  host.dataset.tsl = '1'
  host.style.display = 'contents'
  /* An agent stage box is not shared ground the way #stage is: the subagents
     island writes its own placeholders straight into it (emptyStage and
     failStage in features/subagents/store.ts), and the renderer this replaces
     opened with `box.innerHTML = ''`. Start a fresh lane there from an empty
     box, or a failed first fetch leaves its error note pinned above the run's
     transcript until the status changes. */
  if (!main) container.textContent = ''
  container.appendChild(host)
  seq += 1
  const lane = store.newLane(`${main ? 'main' : 'agent'}:${seq}`, main)
  const root = createRoot(host)
  /* Synchronous like the renderer it replaces: the very next legacy line may
     read the drawn DOM (the dag sheet selects its node right after). */
  flushSync(() => root.render(view(lane)))
  const rec: Mounted = { host, lane, root }
  HOSTS.set(host, rec)
  MOUNTED.push(rec)
  return lane
}

/* Detached lane for a page without a #stage (unit tests exercise the store
   through the mounted views instead; this keeps the shims from throwing). */
let orphan: Lane | null = null

export function mainLane(): Lane {
  const stage = document.getElementById('stage')
  if (!stage) {
    if (!orphan) orphan = store.newLane('main:orphan', true)
    return orphan
  }
  return laneIn(stage, true, (lane) => <StageView lane={lane} />)
}

/* ── the public face the legacy shims call ─────────────────────────────── */

export function ask(text: string, when?: string | null): void {
  store.askText(mainLane(), String(text), when ?? null)
}

export function step(): StepHandle {
  return store.newStep(mainLane())
}

export function answer(text: string, when?: string | null): AnswerData {
  return store.answer(mainLane(), text, when ?? null)
}

/* The demo replay's typing effect drives the same answer segment. */
export function answerTyped(text: string): { progress(shown: number): void; done(): void } {
  const lane = mainLane()
  const seg = store.answer(lane, text)
  seg.shown = 0
  return {
    progress: (shown) => store.answerProgress(lane, seg, Math.min(shown, text.length)),
    done: () => store.answerProgress(lane, seg, null),
  }
}

export function note(label: string, detail: string,
  opts?: { quiet?: boolean; retry?: (() => void) | null } | null): NoteHandle {
  return store.note(mainLane(), label, detail, opts)
}

export function qa(question: string, answerText: string, opts?: { skipped?: boolean } | null): void {
  store.qa(mainLane(), question, answerText, opts)
}

export function status(text: string): void {
  store.status(mainLane(), text)
}

export function killStatus(): void {
  store.killStatus(mainLane())
}

export function collapse(time?: string | null): void {
  store.collapse(mainLane(), time ?? null)
}

export function foldRuns(steps: StepHandle[]): void {
  store.foldRuns(mainLane(), steps.map((h) => h.seg))
}

export function finishTurn(st: StepHandle | null, steps: StepHandle[], time?: string | null): void {
  const segs = steps.map((h) => h.seg)
  store.finishTurn(mainLane(), st, segs, time ?? null)
}

/* The turn's products, as its closing line. The turn number is the workspace
   record's own; the list is read from the source when the tiles draw. */
export function artifacts(turn: number): void {
  store.artifacts(mainLane(), turn)
}

export function delivery(turn: number, metadata: unknown, callId?: string | null): void {
  store.recordDelivery(mainLane(), turn, metadata, callId)
}

export function turnKept(): boolean {
  return store.turnKept(mainLane())
}

export function history(messages: HistoryMessage[]): void {
  store.history(mainLane(), messages)
}

export function delivered(p: {
  label: string; isDag: boolean; status?: string; open: () => void; body?: string
}): void {
  store.delivered(mainLane(), p)
}

export function dagFeed(type: string, p: Parameters<typeof store.dagFeed>[1]): void {
  store.dagFeed(type, p)
}

export function spawnFeed(p: Parameters<typeof store.spawnFeed>[0]): void {
  store.spawnFeed(p)
}

export function stopStream(): void {
  store.stopFlush(mainLane())
}

export function nudge(): void {
  store.nudge(mainLane())
}

export function redraw(): void {
  store.redraw()
}

/* ── the agent stage: a delegated run in a workspace pane ──────────────
   Painted synchronously (the dag sheet reads the drawn DOM in the same
   task), keeping the reader's scroll unless they were at the tail. */
export function agentStage(box: HTMLElement, ctx: AgentCtxLike | null,
  opts?: { key?: string; empty?: string; reset?: boolean } | null): void {
  const atEnd = box.scrollTop + box.clientHeight >= box.scrollHeight - 4
  const top = box.scrollTop
  const lane = laneIn(box, false, (l) => <AgentStageView lane={l} />)
  flushSync(() => {
    store.agentPaintLane(lane, ctx, opts ?? null)
  })
  box.scrollTop = atEnd ? box.scrollHeight : top
}
