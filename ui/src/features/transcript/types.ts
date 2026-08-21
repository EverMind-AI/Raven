/* The transcript's segment model. A lane is one transcript surface (the main
 * stage, or one agent stage pane); its content is a list of segments, and a
 * turn reads as: an ask, then steps (thought, narration, calls), then the
 * answer -- with the steps folding behind one line once the answer lands.
 */

import type { SnapshotRow } from '../dag/nodes'
import type { DagNode } from '../dag/types'

export interface Hunk {
  add: number
  del: number
  rows: Array<[string, string | string[], (number | null)?, (number | null)?]>
}

export type CallKind = 'plain' | 'spawn' | 'dag'

export interface CallData {
  v: number
  id: number
  kind: CallKind
  name: string
  args: Record<string, unknown>
  via: boolean
  srv: string | null
  display: string
  label: string
  rowLabel: string
  done: boolean
  ok: boolean
  ms: number
  res: string
  truncated: boolean
  hunk: Hunk | null
  open: boolean
  /* spawn / dag cards */
  t0: number
  runId: string | null
  /* The graph, in the shape features/dag holds it -- not a reduction of it. The
     card used to keep four fields per node (id, agent, handle, status) and there
     was nowhere for the rest to go: the dependencies that make it a graph, and
     the template and inputs that say what each step was asked. */
  nodes: DagNode[]
  /* Whether the run has an identity, which is what makes a node openable. A node
     that looked like a door before its run_id was known opened nothing. */
  live: boolean
  /* The node whose detail is open inside the card. View state, like `open`, and
     the only thing that decides what the panel shows -- deriving a fallback from
     it gave `null` two meanings, "nobody picked one" and "the reader closed it",
     and the unasked open on a failure then could not be closed at all. */
  sel: string | null
  /* Whether the unasked open on a failure has already happened. Once, like the
     step's own fold: a second failure does not reopen a panel the reader shut. */
  selAuto: boolean
  /* Whether that detail shows the whole prompt template or a clamp of it. */
  selFull: boolean
  /* Set once a `dag.get` has been asked for, so a card whose arguments carried no
     graph asks once rather than on every re-render. */
  asked: boolean
}

export interface StepData {
  v: number
  id: number
  kind: 'step'
  think: string
  thinkLive: boolean
  thinkOpen: boolean
  thinkPinned: boolean
  thinkShown: boolean
  thinkT0: number
  thinkMs: number
  thinkSecs: number | null
  say: string
  sayCaret: boolean
  hasSay: boolean
  hasThink: boolean
  hasQA: boolean
  failed: boolean
  calls: CallData[]
  wkOpen: boolean
  wkPinned: boolean
  merged: boolean
}

export interface AskData {
  v: number
  id: number
  kind: 'ask'
  body: string
  atts: string[]
  when: string
  expanded: boolean
  clipped: boolean
  clipOpen: boolean
}

export interface AnswerData {
  v: number
  id: number
  kind: 'answer'
  text: string
  when: string
  /* demo typing effect: characters shown so far; null once settled */
  shown: number | null
}

export interface NoteData {
  v: number
  id: number
  kind: 'note'
  label: string
  detail: string
  quiet: boolean
  retry: (() => void) | null
}

export interface QaData {
  v: number
  id: number
  kind: 'qa'
  q: string
  a: string
  skipped: boolean
  open: boolean
}

export interface StatusData {
  v: number
  id: number
  kind: 'status'
  text: string
}

export interface DeliveredData {
  v: number
  id: number
  kind: 'sdlv'
  label: string
  isDag: boolean
  err: boolean
  open: () => void
}

export interface FoldData {
  v: number
  id: number
  kind: 'fold'
  time: string | null
  open: boolean
  steps: StepData[]
}

export type Seg = AskData | StepData | AnswerData | NoteData | QaData | StatusData | DeliveredData | FoldData

export interface Lane {
  key: string
  main: boolean
  epoch: number
  listV: number
  /* bumps when legacy would have tail-followed; the view scrolls on it */
  scrollReq: number
  segs: Seg[]
  listeners: Set<() => void>
  /* streaming say buffer for the open step, flushed once per frame */
  pend: string
  pendStep: StepData | null
  flush: { t: number; timer: boolean } | null
  /* agent stage bookkeeping: messages already drawn, running glyph */
  agentKey: string | null
  agentDrawn: number
  running: boolean
  empty: string
}

/* What a legacy caller gets back from newStep()/tool(): the same handle
   surface the old widgets returned, driving the store instead of the DOM. */
export interface CallHandle {
  done(ok: boolean, res: unknown, ms: number, diff?: string | string[] | null, truncated?: boolean): void
}

export interface StepHandle {
  seg: StepData
  hasThink: boolean
  hasSay: boolean
  hasQA: boolean
  failed: boolean
  thinkAppend(text: string): void
  reveal(): void
  thinkDone(secs?: number | null): void
  setThinkOpen(open: boolean): void
  setSay(text: string): void
  sayDelta(text: string): void
  tool(name: string, args: unknown, display?: string | null): CallHandle
  seal(): void
}

export interface NoteHandle {
  set(label: string, detail: string): void
  remove(): void
  readonly title: string
}

/* The history messages session.resume hands over. */
export interface HistoryMessage {
  role: string
  text?: string
  name?: string
  timestamp?: string | number
  tool_call_id?: string
  tool_calls?: Array<{ id?: string; name?: string; arguments?: string }>
  reasoning_content?: string
  reasoning_ms?: number
  duration_ms?: number
  diff?: string | string[]
  notice?: { kind?: string; detail?: string }
  turn_ended?: { status?: string; reason?: string }
}

/* The pull half of the seam. Event pushes arrive through the island API the
   live layer forwards into (window.RavenIslands.transcript). */
export interface TranscriptSource {
  clean(text: unknown): string
  okOf(name: string, preview: string): boolean
  branch?: (text: string) => void
  /* `dag.get`'s own `run.files` rows, unreduced. They were being mapped down to
     four fields on the way in, which is why a restored card could never show a
     dependency or a prompt: the adapter (features/dag/nodes.ts) reads the wire
     shape, so this seam does not need to know which fields matter. */
  dagRows?: (runId: string) => Promise<SnapshotRow[]>
  openDagNode?: (runId: string, nodeId: string) => void
  openSpawn?: (agent: string, label: string) => void
  /* Whether a detached lane host is parked rather than discarded: leaving a
     session mid-turn keeps the transcript as detached DOM and puts it back on
     return, so off the page does not mean finished with. */
  parked?: (node: HTMLElement) => boolean
}
