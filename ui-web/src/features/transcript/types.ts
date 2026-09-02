/* The transcript's segment model. A lane is one transcript surface (the main
 * stage, or one agent stage pane); its content is a list of segments, and a
 * turn reads as: an ask, then steps (thought, narration, calls), then the
 * answer -- with the steps folding behind one line once the answer lands.
 */

import type { SnapshotRow } from '../dag/nodes'
import type { DagNode } from '../dag/types'
import type { WsChange } from '../workspace/types'

export interface Hunk {
  add: number
  del: number
  rows: Array<[string, string | string[], (number | null)?, (number | null)?]>
}

export type CallKind = 'plain' | 'spawn' | 'dag'

/* What `dag.get` answers, as much of it as the transcript reads. */
export interface DagRunLike {
  files?: SnapshotRow[]
  task_summary?: string
}

/* What `subagent.context` answers. `messages` is the same wire shape
   `session.resume` returns -- which is the whole point of that shape: one
   renderer draws a delegated run's conversation and a session's, so the two
   cannot drift. */
/* One row of `subagent.list`. Only the fields a card reads: the row also carries
   the clocks and the token cost, which are the panel's business. */
export interface SpawnListRow {
  agent?: string | null
  /* ISO here, where the event sends ms. Both name the run's own clock. */
  started_at?: string | null
  ended_at?: string | null
  id?: string
  instance?: string | null
  kind?: string
  label?: string
  status?: string
}

export interface SpawnRecordLike {
  messages?: HistoryMessage[]
  status?: string | null
}

/* One `subagent.status` frame, as the live layer forwards it. The fields this
   card reads, not the whole wire shape: `task_id`, `started_at` and `ended_at`
   are the live-agents strip's business, and the card has its own clock. */
export interface SubagentStatusLike {
  agent?: string
  call_id?: string | null
  /* The RUN's own clock, in ms. Present from `running` onward and on the terminal
     frame; the tool row's clock measures the dispatch, not the work. */
  started_at?: number | null
  ended_at?: number | null
  instance?: string | null
  label?: string
  status?: string
  tool_call_id?: string | null
}

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
  /* Who the run is and what it was asked, from `subagent.status` rather than
     from the model's own arguments. The event is the authority: the caller may
     omit `instance` and have one minted for it, and the header has to name the
     handle that was actually used. All three land on the `pending` frame, before
     any record exists. */
  spawnAgent: string
  spawnInstance: string
  spawnLabel: string
  /* The run's own lifecycle word, which is not the tool call's. A spawn returns
     when the work is *dispatched*, so the tool row is `done` while the run is
     still going -- reading `done` as "the sub-agent finished" is what made a
     finished-looking card sit on a live run. */
  spawnStatus: string
  /* The record id `subagent.context` reads. Empty until the run reports
     `running`, which is when its record is opened. */
  spawnId: string
  /* The manager's task id, which is what a RESTORED card has instead of a record
     id: a record's directory is `<stamp>-<task_id>`, so one `subagent.list` read
     turns this into `spawnId`. Empty on a live card, which never needs it. */
  spawnTaskId: string
  /* Whether that list read has been made for this card. Once, like the dag
     card's `asked`: the answer cannot change for a settled run. */
  spawnAsked: boolean
  /* The RUN's own clock, in ms since the epoch, from whichever source named it.
     Not the tool call's: a spawn returns when the work is dispatched, so `ms` is
     near zero on every spawn card and reading it printed `耗时 0.0s` over a run
     that had taken eight seconds. Zero means unknown. */
  spawnT0: number
  spawnT1: number
  /* The run's own messages, newest last, as `subagent.context` answers them.
     Live while it runs: the acp backend republishes its transcript into the
     activity index on every update, and the method serves that when the record's
     file does not exist yet. */
  stream: HistoryMessage[]
  /* Whether a read is in flight, so a slow answer cannot stack up behind the
     heartbeat. */
  reading: boolean
  runId: string | null
  /* What the graph was dispatched for. On the arguments for a model-composed
     graph, and only from `dag.get` for a playbook load, whose arguments name
     the playbook and not the graph it assembles. */
  runTitle: string
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
  /* The provider's id for this tool call, which is what a dag run names itself
     the child of. The card is bound to its run through this rather than through
     "the newest dag card that has no run yet": the run announces itself on a
     side channel that is not ordered against the tool events, so the guess was
     wrong exactly when two things raced -- and a card that lost the race then
     took no update for the rest of the run. Empty for a card restored from
     history, whose events are long gone and which binds through its result. */
  callId: string
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
  status: 'ok' | 'error' | 'exception'
  open: () => void
  /* What was delivered, as a reader may read it: the text from inside the
     untrusted fence, with the fence and the framing the model was given left
     out. Empty when the delivery carried none -- and then the row has no fold,
     which is also what an older session's stored entry gives. */
  body: string
  /* Whether that fold is open. */
  shown: boolean
}

/* One file this turn produced, as the bar shows it. `head` is the file's own
   first lines when the page already has them -- a write tool's hunk carries
   what it wrote, so a text artifact needs no fetch to draw a miniature of
   itself. Absent means the page has no content for it (a binary, or a replay
   that kept no diff) and the tile shows its kind instead. */
export interface ArtifactRow {
  path: string
  dir: string
  name: string
  ext: string
  head: string | null
  lines: number
  deleted: number
  change: 'new' | 'edit'
}

export interface ArtsData {
  v: number
  id: number
  kind: 'arts'
  /* Which turn's products these are. The list is READ from the source at
     render time rather than copied in here: the workspace record is rebuilt
     from history on a reload, and a copy taken while the turn was live would
     then disagree with it. */
  turn: number
  deliveriesOpen: boolean
  changesOpen: boolean
}

export interface FoldData {
  v: number
  id: number
  kind: 'fold'
  time: string | null
  open: boolean
  steps: StepData[]
}

export type Seg =
  | AskData | StepData | AnswerData | NoteData | QaData | StatusData | DeliveredData
  | ArtsData | FoldData

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
  /* How many segments were settled at the end of the last paint. Anything past
     it is the answer still being written, redrawn from the record on every
     paint rather than appended to; 0 means nothing is provisional. */
  agentHold: number
  running: boolean
  empty: string
}

/* What a legacy caller gets back from newStep()/tool(): the same handle
   surface the old widgets returned, driving the store instead of the DOM. */
export interface CallHandle {
  done(ok: boolean, res: unknown, ms: number, diff?: string | string[] | null, truncated?: boolean): void
  /* The run a restored spawn call started, from the `spawn_task_id` the server
     stamped on its result row. A live card learns its run from
     `subagent.status`; a restored one saw none of those frames, and this is the
     only thread back to the record -- the same one ui-tui pulls on. */
  spawnTask?(taskId: string): void
}

export interface StepHandle {
  seg: StepData
  hasThink: boolean
  hasSay: boolean
  hasQA: boolean
  failed: boolean
  thinkAppend(text: string): void
  reveal(): void
  thinkDone(): void
  setThinkOpen(open: boolean): void
  setSay(text: string): void
  sayDelta(text: string): void
  tool(name: string, args: unknown, display?: string | null, callId?: string | null): CallHandle
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
  /* The run a spawn call started, stamped by the server onto the result row
     whose sentence names it (`session.resume`). Absent on every other tool, and
     on a spawn refused before it ran. What a restored card resolves itself by --
     it saw no `subagent.status` and has no record id without this. */
  spawn_task_id?: string
  text?: string
  name?: string
  timestamp?: string | number
  tool_call_id?: string
  tool_calls?: Array<{ id?: string; name?: string; arguments?: string }>
  reasoning_content?: string
  reasoning_ms?: number
  duration_ms?: number
  diff?: string | string[]
  metadata?: Record<string, unknown>
  notice?: { kind?: string; detail?: string }
  origin?: string
  turn_ended?: { status?: string; reason?: string }
  /* Present on a USER entry the runtime wrote: a delegated run's result coming
     back. The model reads `text`, a reader must not -- see the note on the
     delivery branch in history(). */
  delegated?: { kind?: string; label?: string; status?: string; run_id?: string }
}

/* What the artifact bar reads, and all it reads: the workspace record's rows
   for one turn, exactly as that record holds them.
   Deliberately NOT the finished list. Which of those rows counts as a product,
   and what a tile can draw of it, are presentation decisions -- they belong to
   the island that draws them, where they can be tested, rather than to the
   legacy layer that happens to own the record. */
export interface ArtifactsSource {
  changes(turn: number): WsChange[]
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
  /* The whole run, not its rows. It was `dagRows` and answered `run.files`,
     which is why the card could name every node and never the graph: a field
     this seam did not return was a field no card could draw. */
  dagRun?: (runId: string) => Promise<DagRunLike>
  /* One spawned run's messages so far, by the record id `subagent.status`
     reported. Answers a moving stream while the run is live, not a finished
     transcript: see MsgLike. */
  spawnRecord?: (callId: string) => Promise<SpawnRecordLike>
  /* Every delegated call this conversation made, as `subagent.list` answers it.
     Read to turn a restored card's task id into the record id its stream is
     read by -- once per conversation, not once per card. */
  spawnList?: () => Promise<SpawnListRow[]>
  /* The node's own summary rides with its id: the pane that opens is headed by
     it, and the id is a slug from the plan. Optional, so a caller that has only
     an id still opens the node. */
  openDagNode?: (runId: string, nodeId: string, summary?: string | null) => void
  openSpawn?: (agent: string, label: string) => void
  /* Open the delegated GRAPH a delivery came from. One verb rather than the
     live event handler doing it inline, because the replayed row has to open
     the same thing the live row does. */
  openDagRun?: (runId: string) => void
  /* Whether a detached lane host is parked rather than discarded: leaving a
     session mid-turn keeps the transcript as detached DOM and puts it back on
     return, so off the page does not mean finished with. */
  parked?: (node: HTMLElement) => boolean
}
