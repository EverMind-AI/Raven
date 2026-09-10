import * as dagNodes from '../dag/nodes'
import { ds, shell, t, verb } from '../../shell/bridge'
import { formatDuration } from '../../shell/duration'
import { md } from '../../shell/prose'
import * as deliveries from '../workspace/deliveries'
import * as hunks from '../workspace/hunks'

import type { DeliveryRow, WsChange } from '../workspace/types'
import type {
  AnswerData, ArtifactRow, ArtifactsSource, ArtsData, AskData, CallData, CallHandle,
  DeliveredData, FoldData, HistoryMessage, Hunk, Lane, NoteData, NoteHandle, QaData, Seg,
  SpawnListRow, StatusData, StepData, StepHandle, SubagentStatusLike, TranscriptSource,
} from './types'
import type { Shell } from '../../shell/bridge'

/* Plain external store. The legacy layers drive the transcript imperatively
 * (the replay, the live turn machine, the history reader all push segments),
 * so state lives here where the shims can reach it, mutated in place; each
 * segment carries its own version and the components subscribe per segment,
 * which is what keeps a token append from re-rendering anything but the
 * streaming leaf.
 */

export const source = (): TranscriptSource => ds<TranscriptSource>('transcript')

/* Tolerated missing rather than thrown on: a page that has installed no
   artifacts source has no products to show, and the bar is drawn from the same
   boot sequence that installs it. */
export function artifactsSource(): ArtifactsSource {
  try {
    return ds<ArtifactsSource>('artifacts')
  } catch {
    return { changes: () => [] }
  }
}

/* The file's own first lines, out of the row the workspace record already
   holds. A write tool's hunk IS what it wrote -- hunkFromWrite keeps the first
   forty lines as rows and folds the rest into one gap row -- so a text product
   draws a miniature of itself with nothing fetched, live and on replay both
   (session.resume carries the write's arguments, and the panel's replay
   rebuilds the same hunk from them). Null when the row carries no content, and
   then the tile shows the file's kind rather than inventing a picture. */
export function artifactHead(c: WsChange): string | null {
  const out: string[] = []
  for (const h of c.hunks || []) {
    for (const r of h.rows || []) {
      if (r[0] === 'add') out.push(String(r[1] == null ? '' : r[1]))
      else if (r[0] === 'gap' && Array.isArray(r[1])) for (const l of r[1]) out.push(String(l))
    }
  }
  return out.length ? out.join('\n') : null
}

/* Every file this turn created or edited. The workspace record already owns
   that classification and survives live/replay through the same tool rows. */
export function artifactsOf(lane: Lane, turn: number): ArtifactRow[] {
  if (!lane.main) return []
  let rows: WsChange[] = []
  try {
    rows = artifactsSource().changes(turn) || []
  } catch {
    return []
  }
  return rows.filter(Boolean).map((c) => {
    const shown = `${c.dir || ''}${c.name || ''}` || String(c.key || '')
    const name = String(c.name || shown)
    const dot = name.lastIndexOf('.')
    return {
      path: String(c.key || ''),
      dir: String(c.dir || ''),
      name,
      ext: dot > 0 ? name.slice(dot + 1).toLowerCase() : '',
      head: artifactHead(c),
      lines: c.add || 0,
      deleted: c.del || 0,
      change: c.kind === 'write' ? 'new' : 'edit',
    }
  })
}

/* Not held here: the desk's shelf lists the same rows for the whole session, so
   the registry they live in is shared ground (workspace/deliveries.ts).

   The lane IS a parameter of it, though the comment here used to say otherwise.
   `history()` records a delivery off any lane it replays, and `agentPaintLane`
   replays a delegated run through it -- so a sub-agent's deliveries land in the
   same registry as the conversation's, under turn numbers that both count from
   one. The scope is what keeps them apart. */
export function recordDelivery(lane: Lane, turn: number, metadata: unknown, callId?: string | null): void {
  deliveries.record(scopeOf(lane), turn, metadata, String(callId || ''))
}

const scopeOf = (lane: Lane): string =>
  (lane.main ? deliveries.SESSION : `agent:${lane.agentKey || '?'}`)

export const deliveriesOf = (lane: Lane, turn: number): DeliveryRow[] =>
  deliveries.ofTurn(scopeOf(lane), turn)

/* Straight to the renderer rather than out through the shell: prose.ts is a
   pure function in this same bundle, and a bridge verb would round-trip
   window.RavenShell.md -> window.md -> back into it while hiding the
   transcript from anyone auditing md()'s callers. */
export const mdHtml = (src: string): string => md(src)
export const durText = formatDuration

const shortPath = (p: string): string => {
  try {
    return ds<{ shortPath(p: string): string }>('workspace').shortPath(p)
  } catch {
    return String(p || '')
  }
}

/* ── segment vocabulary helpers (ported with the renderer) ─────────────── */

export const MIN_RUN_STEPS = 2
export const DTL_MAX_LINES = 80

export const shortArg = (a: unknown, max = 40): string => {
  if (!a) return ''
  const one = String(a).replace(/\s+/g, ' ').trim()
  return one.length > max ? one.slice(0, max - 1) + '…' : one
}

const MCP_RE = /^mcp_([^_]+)_(.+)$/
const rawVerb = (n: string): string => {
  const m = MCP_RE.exec(n || '')
  return m ? (m[2] as string).split('_').join(' ') : String(n || '').split('_').join(' ')
}
export const verbOf = (n: string): string => t('gui.act.v.' + n, undefined, rawVerb(n))
export const verbIngOf = (n: string): string => t('gui.act.ing.' + n, undefined, rawVerb(n))

/* The demo replay hands the one string it displays where the live RPC hands
   the argument object; normalised here exactly as the legacy wsArgs did. */
function parseArgs(name: string, args: unknown): Record<string, unknown> {
  if (args && typeof args === 'object') return args as Record<string, unknown>
  const s = String(args == null ? '' : args)
  if (name === 'exec') return { command: s }
  if (name === 'web_fetch') return { url: s }
  if (name === 'web_search') return { query: s }
  if (name === 'spawn') return { label: s }
  return { path: s }
}

/* Unwrap the tool_call meta tool and spot mcp_<server>_<tool> names. */
function actId(name: string, rawArgs: unknown): { name: string; args: Record<string, unknown>; via: boolean; srv: string | null } {
  let a = (rawArgs && typeof rawArgs === 'object') ? rawArgs as Record<string, unknown> : parseArgs(name, rawArgs)
  let via = false
  if (name === 'tool_call' && typeof a.name === 'string') {
    via = true
    name = a.name
    a = (a.arguments && typeof a.arguments === 'object') ? a.arguments as Record<string, unknown> : {}
  }
  const m = MCP_RE.exec(name)
  return { name, args: a, via, srv: m ? (m[1] as string) : null }
}

/* The file a call names, wherever it named it -- schema violations included. */
export function argPath(a: Record<string, unknown>): string {
  for (const k of ['path', 'file_path', 'filename', 'file', 'target']) {
    if (a && typeof a[k] === 'string' && a[k]) return a[k] as string
  }
  return ''
}

/* One-line label for a call, derived from its arguments. */
export function actLabel(name: string, a: Record<string, unknown>, display?: string | null): string {
  if (display) return String(display)
  const s = (k: string): string => String(a[k] || '')
  switch (name) {
    case 'read_file': case 'write_file': case 'edit_file':
      return shortPath(argPath(a))
    case 'list_dir': return a.path ? shortPath(String(a.path)) + '/' : ''
    case 'grep': return s('pattern') + (a.glob ? '  ' + s('glob') : '')
    case 'find': return s('pattern')
    case 'exec': return String(a.intent || a.command || '')
    case 'web_search': case 'deep_research': case 'tool_search':
      return a.query ? '“' + s('query') + '”' : ''
    case 'web_fetch': return s('url').replace(/^https?:\/\//, '')
    case 'understand_media': {
      const ps = Array.isArray(a.paths) ? (a.paths as unknown[]).map((p) => String(p).split('/').pop()) : []
      return ps.length > 2 ? `${ps[0]} ×${ps.length}` : ps.join(' ')
    }
    case 'spawn': return String(a.task_summary || a.label || String(a.prompt_template || a.task || '').split('\n')[0])
    /* The graph's own line, which the model is required to write. Not left to
       the default branch below: it picks the first string in the arguments,
       which for a dag call is whichever key pydantic happened to serialise
       first, and for `load_playbook` is the playbook's directory name -- the
       one thing about that call that is not what it dispatched. A playbook load
       carries no summary in its arguments at all, so it stays empty here and
       the card fills it in from `dag.get`. */
    case 'run_subagent_dag': return s('task_summary')
    /* The playbook's name, which is all its arguments carry -- the graph it
       assembles does not exist yet, so what running it dispatched arrives later
       from `dag.get` and lands on `runTitle`, not here. Spelled out rather than
       left to the default branch, which picks whichever string comes first. */
    case 'load_playbook': return s('name')
    case 'message': return a.channel ? '→ ' + s('channel') : s('content').split('\n')[0] as string
    case 'cron': return [a.action, a.cron_expr, a.every_seconds ? `${a.every_seconds}s` : '',
      s('message').split('\n')[0]].filter(Boolean).join(' · ')
    case 'use_skill': case 'read_skill': return s('skill_id')
    case 'image_generate': case 'video_generate': return s('prompt').split('\n')[0] as string
    case 'text_to_speech': return s('text').split('\n')[0] as string
    default: {
      const v = Object.values(a || {}).find((x) => typeof x === 'string' && x.trim())
      return v ? String(v) : ''
    }
  }
}

/* The line shown on a failed row: the first error-shaped line if any. */
export const firstErrLine = (res: unknown, cap?: number): string => {
  const lines = String(res || '').split('\n').filter((x) => x.trim())
  const hit = lines.find((x) => /error|failed|traceback|could not|denied|exception/i.test(x))
  const l = hit || lines[0]
  return l ? shortArg(l.replace(/^\s*\[|\]\s*$/g, ''), cap || 62) : ''
}

/* Verbs and counts only -- the folded line answers "what kind of work". */
export function phraseOf(calls: CallData[]): string {
  const n = new Map<string, number>()
  calls.forEach((c) => n.set(c.name, (n.get(c.name) || 0) + 1))
  return [...n].map(([name, k]) => (k === 1
    ? verbOf(name)
    : t('gui.act.n.' + name, { n: k }, `${verbOf(name)} ×${k}`))).join(' · ')
}

/* When an answer landed: today needs only a clock, older needs the date. */
export function stamp(when: number | string | Date): string {
  const d = when instanceof Date ? when : new Date(when)
  if (!d || isNaN(d.getTime())) return ''
  const p = (n: number): string => String(n).padStart(2, '0')
  const hm = `${p(d.getHours())}:${p(d.getMinutes())}`
  const now = new Date()
  const sameYear = d.getFullYear() === now.getFullYear()
  const today = sameYear && d.getMonth() === now.getMonth() && d.getDate() === now.getDate()
  if (today) return hm
  const md = `${p(d.getMonth() + 1)}-${p(d.getDate())} ${hm}`
  return sameYear ? md : `${d.getFullYear()}-${md}`
}

/* Two result shapes carry the run id and only one is persisted; the restore
   path is the one this exists for.

   Per line (`m`), not from the start of the string: a stored tool result arrives
   wrapped in the untrusted-content fence, whose `[BEGIN UNTRUSTED ...]` header
   is the first line. Anchored at the string start this matched only the
   unfenced form -- so every *restored* card failed to bind its run, and with no
   run id it never asked `dagRun` for the node states. A live card looked right
   because its events had already filled it in; the same card after a refresh
   showed its node names with no status and a 0.0s clock. Still line-anchored
   rather than a bare search, so a run id is only read from a line that opens
   with the announcement. */
export const dagRunIdFrom = (res: unknown): string | null =>
  (/^DAG\s+(?:run\s+)?(\S+?)[:\s]/m.exec(String(res || '')) || [])[1] || null

/* Every word `DagNodeStatus` can carry, and one more the reconciler writes.

   A word missing here is not a node drawn plainly -- it is a node the tally
   counts as none of its four cases, so it leaves the line entirely. `cancelled`
   was missing, and a graph of three stopped after one finished read "1 done":
   the same sentence a graph of one produces.

   `cancelled` gets its own value rather than joining either neighbour. Not
   `skipped`: those are different facts -- a skipped node was never going to run,
   a cancelled one was running and was stopped. And not `bad` either, which is
   what this first tried: the tally renders `bad` through `gui.deleg.dag_bad`,
   which reads "failed" in both locales, so a stopped node came out labelled as
   one that went wrong. The runner keeps the two apart on purpose
   (docs/plans/2026-08-17-acp-cancel-and-node-status.md); the line has to as well. */
export const DOT_OF: Record<string, string> = {
  pending: '', running: 'run', completed: 'ok',
  failed: 'bad', cancelled: 'stop', skipped: 'skip', interrupted: 'bad',
}

/* A graph node that has stopped, whatever it stopped as. `pending` and
   `running` are the two that have not. */
const NODE_SETTLED = new Set(['completed', 'failed', 'skipped', 'cancelled', 'interrupted'])

/* How long the GRAPH has been going, which is not how long the call took.
   `run_subagent_dag` is backgrounded by default, so the call returns as soon as
   the run is submitted: `c.ms` is that submit, a number near zero that never
   moves again while the graph runs for minutes. The nodes carry the real clock.
   `null` when no node has started -- there is nothing to report yet, and zero
   would read as an answer.

   `open` and a null `to` are two different facts and the caller needs both.
   `open` means a node has not stopped, so the clock is still running. A null
   `to` on a CLOSED span means every node stopped without leaving an end stamp,
   which is what a cancel looks like over the wire: the transition the runner
   publishes for `cancelled` and `skipped` carries neither stamp, so a node that
   got `started_at` from its `running` event settles with `ended_at` still null.
   Reading that as open is what makes a cancelled graph count up forever. */
export function dagSpan(nodes: Array<{ status?: string; started_at?: number | null; ended_at?: number | null }>):
  { from: number; to: number | null; open: boolean } | null {
  const started = nodes.map((n) => n.started_at).filter((x): x is number => typeof x === 'number' && x > 0)
  if (!started.length) return null
  const from = Math.min(...started)
  const open = nodes.some((n) => !NODE_SETTLED.has(String(n.status || 'pending')))
  if (open) return { from, to: null, open }
  const ended = nodes.map((n) => n.ended_at).filter((x): x is number => typeof x === 'number' && x > 0)
  return { from, to: ended.length ? Math.max(...ended) : null, open }
}

/* ── lanes ─────────────────────────────────────────────────────────────── */

let segId = 0
const nextId = (): number => (segId += 1)

const lanes = new Set<Lane>()

export function newLane(key: string, main: boolean): Lane {
  const lane: Lane = {
    key, main, epoch: 0, listV: 0, scrollReq: 0, segs: [], listeners: new Set(),
    pend: '', pendStep: null, flush: null,
    agentKey: null, agentDrawn: 0, agentHold: null, agentTurn: 0, running: false, empty: '',
  }
  lanes.add(lane)
  return lane
}

/* Releasing a lane whose host the page threw away (mount.tsx decides which
   those are). Everything still holding it goes too: a buffered flush would
   fire into an unmounted root, and a dag card whose run never reported
   completion would pin the lane for the life of the page. */
export function dropLane(lane: Lane): void {
  lanes.delete(lane)
  stopFlush(lane)
  for (const [id, f] of dagLive) if (f.lane === lane) dagLive.delete(id)
  for (const [id, f] of dagByCall) if (f.lane === lane) dagByCall.delete(id)
  if (dagPending && dagPending.lane === lane) dagPending = null
  for (const [id, f] of spawnByCall) if (f.lane === lane) spawnByCall.delete(id)
}

export function subscribe(lane: Lane, l: () => void): () => void {
  lane.listeners.add(l)
  return () => { lane.listeners.delete(l) }
}

function emit(lane: Lane): void {
  for (const l of [...lane.listeners]) l()
}

function bumpList(lane: Lane): void {
  lane.listV += 1
  emit(lane)
}

function bump(lane: Lane, seg: { v: number }): void {
  seg.v += 1
  emit(lane)
}

/* The appends the legacy renderer followed with down(): toggles never
   scroll (they pin the clicked row instead), so this is its own counter. */
function poke(lane: Lane): void {
  lane.scrollReq += 1
}

/* A language flip changes no data; every visible word comes from t() at
   render time, so bumping every version is the whole repaint. */
export function redraw(): void {
  for (const lane of lanes) {
    const walk = (segs: Seg[]): void => segs.forEach((s) => {
      s.v += 1
      if (s.kind === 'fold') walk(s.steps)
      if (s.kind === 'step') s.calls.forEach((c) => { c.v += 1 })
    })
    walk(lane.segs)
    lane.listV += 1
    emit(lane)
  }
}

/* ── streaming: one paint per frame ────────────────────────────────────
   The answer prose is re-read from the whole buffer, so notifying per delta
   would mean a render per token. Deltas land in the segment's buffer and the
   version bump rides requestAnimationFrame; a hidden window falls back to a
   timer, because frame callbacks never fire there and a turn streaming into
   a backgrounded window still has to land its text. */
function scheduleFlush(lane: Lane, seg: StepData): void {
  if (lane.flush) return
  const run = (): void => {
    lane.flush = null
    poke(lane)
    bump(lane, seg)
  }
  lane.flush = (typeof document !== 'undefined' && document.hidden)
    ? { t: setTimeout(run, 120) as unknown as number, timer: true }
    : { t: requestAnimationFrame(run), timer: false }
}

export function stopFlush(lane: Lane): void {
  if (!lane.flush) return
  if (lane.flush.timer) clearTimeout(lane.flush.t)
  else cancelAnimationFrame(lane.flush.t)
  lane.flush = null
}

/* Re-emit whatever is buffered; the restore path pokes this. */
export function nudge(lane: Lane): void {
  bumpList(lane)
}

/* ── segment ops ───────────────────────────────────────────────────────── */

function push(lane: Lane, seg: Seg): void {
  lane.segs.push(seg)
  poke(lane)
  bumpList(lane)
}

export function ask(lane: Lane, body: string, atts: string[], when?: string | null): void {
  push(lane, {
    v: 0, id: nextId(), kind: 'ask', body, atts,
    when: when != null ? when : stamp(Date.now()), expanded: false,
    clipped: body.length > 640 || body.split('\n').length > 12,
    clipOpen: false,
  } satisfies AskData)
}

export function note(lane: Lane, label: string, detail: string, opts?: { quiet?: boolean; retry?: (() => void) | null } | null): NoteHandle {
  const seg: NoteData = {
    v: 0, id: nextId(), kind: 'note', label: String(label || ''), detail: String(detail || ''),
    quiet: !!(opts && opts.quiet), retry: (opts && opts.retry) || null,
  }
  push(lane, seg)
  return {
    set(l: string, d: string) { seg.label = String(l || ''); seg.detail = String(d || ''); bump(lane, seg) },
    remove() {
      const i = lane.segs.indexOf(seg)
      if (i >= 0) { lane.segs.splice(i, 1); bumpList(lane) }
    },
    get title() { return seg.detail ? `${seg.label} · ${seg.detail}` : seg.label },
  }
}

export function qa(lane: Lane, question: string, answer: string, opts?: { skipped?: boolean } | null): void {
  push(lane, {
    v: 0, id: nextId(), kind: 'qa',
    q: String(question || '').replace(/\s+/g, ' ').trim(),
    a: String(answer || '').replace(/\s+/g, ' ').trim(),
    skipped: !!(opts && opts.skipped), open: false,
  } satisfies QaData)
}

/* What a delegated result looks like to a reader: the text from INSIDE the
   untrusted fence, and nothing else.

   One rule, and it is the fence's own contract rather than a guess about the
   prose around it. A spawn's injection wraps the result in framing the model
   was given ("Task: ...", "Summarize this naturally...") and that framing sits
   OUTSIDE the fence, so it drops out; a dag's injection is the fence and
   nothing else, so all of it stays. The live path runs this over the event's
   `content` and the replay path over the stored entry's `text` -- the same
   string in both cases, which is why the two views cannot disagree about what
   was delivered.

   No markers at all means nothing was fenced (wrap_untrusted returns blank
   content unchanged): fall back to every line that is not a marker, which is
   what the tool-preview cleaner does with the same input. */
/* `status` is loosely typed and normalised here rather than by the caller,
   because one caller is the live path's plain-JS turn handler, which has no
   import statements and only ever hands this function a raw wire string. */
const deliveredStatus = (v: unknown): DeliveredData['status'] =>
  v === 'error' ? 'error' : v === 'exception' ? 'exception' : 'ok'

export function delivered(lane: Lane, p: {
  label: string; isDag: boolean; status?: string; open: () => void; body?: string
}): void {
  push(lane, {
    v: 0, id: nextId(), kind: 'sdlv',
    label: p.label, isDag: p.isDag, status: deliveredStatus(p.status), open: p.open,
    body: defence(p.body || ''), shown: false,
  } satisfies DeliveredData)
}

const FENCE_OPEN = /^\s*\[BEGIN UNTRUSTED ([^ #]+) #([^ ]+) /
const FENCE_CLOSE = /^\s*\[END UNTRUSTED ([^ #]+) #([^ ]+) /

/* A forged close marker must not end the fence. wrap_untrusted tags both ends
   with a per-call nonce for exactly that reason: the fence ends only at the
   line whose source AND nonce match the opening line's. Anything else --
   including a close-shaped line with a different tag -- is content, and if the
   genuine close never appears the whole rest is content too. */
export function defence(text: string): string {
  const lines = String(text || '').split('\n')
  const start = lines.findIndex((l) => FENCE_OPEN.test(l))
  if (start < 0) {
    return lines.filter((l) => !FENCE_CLOSE.test(l)).join('\n').trim()
  }
  const m = FENCE_OPEN.exec(lines[start] || '')
  const tag = m ? `${m[1]} #${m[2]}` : null
  const rest = lines.slice(start + 1)
  let end = -1
  if (tag) {
    const close = new RegExp(`^\\s*\\[END UNTRUSTED ${tag.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\]`)
    end = rest.findIndex((l) => close.test(l))
  }
  return (end < 0 ? rest : rest.slice(0, end)).join('\n').trim()
}

export function toggleDelivered(lane: Lane, seg: DeliveredData): void {
  seg.shown = !seg.shown
  bump(lane, seg)
}

/* The turn's products, as its closing line. Appended once the turn is over and
   only when it produced something: a bar reading "0 products" is furniture the
   reader learns to skip, and then skips on the turn that had some.

   Nothing is copied in. The row list is read from the source when the tiles
   draw, so a reload -- which rebuilds the workspace record from history -- and
   a live turn cannot disagree about what a turn produced. */
export function artifacts(lane: Lane, turn: number): void {
  if (!artifactsOf(lane, turn).length && !deliveriesOf(lane, turn).length) return
  push(lane, {
    v: 0, id: nextId(), kind: 'arts', turn, deliveriesOpen: false, changesOpen: false,
  } satisfies ArtsData)
}

export function toggleArts(lane: Lane, seg: ArtsData, section: 'deliveries' | 'changes'): void {
  if (section === 'deliveries') seg.deliveriesOpen = !seg.deliveriesOpen
  else seg.changesOpen = !seg.changesOpen
  bump(lane, seg)
}

export function status(lane: Lane, text: string): void {
  killStatus(lane)
  push(lane, { v: 0, id: nextId(), kind: 'status', text } satisfies StatusData)
}

export function killStatus(lane: Lane): void {
  const i = lane.segs.findIndex((s) => s.kind === 'status')
  if (i >= 0) { lane.segs.splice(i, 1); bumpList(lane) }
}

export function answer(lane: Lane, text: string, when?: string | null, at?: number | null): AnswerData {
  const seg: AnswerData = {
    v: 0, id: nextId(), kind: 'answer', text,
    when: when != null ? when : stamp(Date.now()), shown: null,
  }
  if (at != null && at >= 0 && at <= lane.segs.length) lane.segs.splice(at, 0, seg)
  else lane.segs.push(seg)
  poke(lane)
  bumpList(lane)
  return seg
}

/* The demo replay's typing effect: progress moves the visible slice. */
export function answerProgress(lane: Lane, seg: AnswerData, shown: number | null): void {
  seg.shown = shown
  poke(lane)
  bump(lane, seg)
}

/* ── steps and calls ───────────────────────────────────────────────────── */

const bridge = (): Shell => shell()

function hunkFor(name: string, a: Record<string, unknown>): Hunk | null {
  if (name === 'edit_file' && typeof a.old_text === 'string' && typeof a.new_text === 'string') {
    return hunks.fromEdit(a.old_text, a.new_text)
  }
  if (name === 'write_file' && typeof a.content === 'string') {
    return hunks.fromWrite(a.content)
  }
  return null
}

/* What the four `dag.*` events carry, as one type: the card reads `nodes` off
   the first, node/status/times off the second, a list of the same off the
   third, and the successor run id off the fourth. The adapters in
   features/dag/nodes.ts do the reading -- this only has to be loose enough to
   hand them. */
export interface DagFeedPayload {
  run_id?: string
  tool_call_id?: string
  node?: string
  status?: string
  started_at?: number
  ended_at?: number
  files?: Array<{ node?: string; status?: string }>
  nodes?: Array<Record<string, unknown>>
  replan_run_id?: string
}

/* The trail's dag card is bound to its run through these.
 *
 * `tool_call_id` is the binding, and it is on every dag event the gateway sends:
 * the run names the tool call it belongs to, so neither side has to guess. What
 * it replaces was a guess -- the newest dag card with no run yet claimed the
 * next `dag.run_started` -- and the guess is wrong whenever the announcement
 * beats the tool row it belongs under. The two travel on different channels (the
 * dag tool publishes its own progress rather than going through the delivery
 * hub, see raven/rpc/spine.py), so nothing orders them, and a card that lost
 * that race then dropped every node update for the rest of the run: it sat at
 * "3 nodes, all waiting" while the sheet above the composer drew the same graph
 * finishing.
 *
 * `dagPending` stays as the fallback for a server that sends no `tool_call_id`,
 * and `dagEarly` holds an announcement that arrived before its card, so the
 * ordering stops mattering in both directions. */
let dagPending: { lane: Lane; call: CallData } | null = null
const dagLive = new Map<string, { lane: Lane; call: CallData }>()
const dagByCall = new Map<string, { lane: Lane; call: CallData }>()
/* What a run said before its card existed, in arrival order, per tool call.
   Only ever holds events for a run that has announced itself and found nobody
   -- an update for any other unknown run is an update for another card's run,
   and buffering those would be a leak with no reader. */
const dagEarly = new Map<string, Array<[string, DagFeedPayload]>>()
/* A card that never arrives would otherwise buffer for the life of the page.
   The window this covers is one tool event wide; anything past this is not the
   race it was built for. */
const EARLY_MAX = 64

/* A card just appeared. It takes whatever announced itself while it was in
   flight, and otherwise stands as the fallback claimant.
 *
 * One object in both maps, not two that describe the same card: the identity is
 * how `dagFeed` knows the claim it just made was the fallback's and clears it.
 * Built twice, that check could never be true for a card claimed by id, so the
 * fallback stayed armed pointing at a card that already had its run -- and the
 * NEXT graph to announce itself before its own row landed on that card, taking
 * its run id and merging its nodes in. Worse than the guess this replaced, which
 * at least left the first card alone. */
function claimRun(lane: Lane, call: CallData): void {
  const owner = { lane, call }
  dagPending = owner
  if (!call.callId) return
  dagByCall.set(call.callId, owner)
  const early = dagEarly.get(call.callId)
  if (!early) return
  dagEarly.delete(call.callId)
  /* In arrival order, through the same door a live event comes in by: the first
     is the announcement, which binds, and the rest apply to what it bound. */
  early.forEach(([type, p]) => dagFeed(type, p))
}

/* Which agent a spawn named, from the model's own arguments. Both spellings:
   the field was renamed `agent` -> `subagent`, and these arguments are recorded
   with the call, so a conversation opened from history hands us calls written
   before the rename for as long as those transcripts exist. Reading only the new
   name dropped the agent out of every delegated row -- the label fell back to
   "raven" whichever agent had actually run. Same tolerance the reader and the
   spawn tool itself already carry. */
export const spawnAgentOf = (a: Record<string, unknown>): string =>
  String(a.subagent || a.agent || '')

/* The graph as a run-started payload carries it. Same shape the arguments path
   builds, so one renderer draws both. */
/* A failure is the one thing worth opening unasked -- the rule the step's own
   fold already follows. Written into `sel` rather than derived at render time:
   derived, it made `null` mean both "nobody picked one" and "the reader closed
   it", so the panel it opened could not be shut. Once per card, so closing it
   stays closed and a later failure does not reopen it.
 *
 * Called wherever the node list changes, because a card is built before its
 * nodes have any state: the failure arrives afterwards, from an event or from
 * `dag.get`. */
function openFirstFailure(call: CallData): void {
  if (call.selAuto || call.sel) return
  const failed = call.nodes.find((n) => n.status === 'failed')
  if (!failed) return
  call.sel = failed.id
  call.selAuto = true
}

/* Test seam: what each raced opening is holding, as (call id, event) pairs.
   Applying an announcement twice is invisible on screen -- `merge` is
   idempotent and `fromStarted` cannot walk a status back -- so the buffer's own
   contents are the only place the claim "buffered once" can be checked. */
export const _earlyForTests = (): Array<[string, string]> =>
  [...dagEarly].flatMap(([id, evs]) => evs.map((e): [string, string] => [id, e[0]]))

/* Terminal words for a spawned RUN, which are not the tool call's.

   `subagent.status` reports the manager's own vocabulary; anything outside this
   set is the run still going. Read as a closed set rather than as "not running",
   so a word this build has never seen keeps the stream open instead of freezing
   a live run at whatever it had said last. */
const SPAWN_SETTLED = new Set(['completed', 'failed', 'cancelled', 'aborted'])

/* Whether this card's run is still producing. `done` is deliberately not
   consulted: the spawn tool returns when the work is DISPATCHED, so the tool row
   settles while the sub-agent is still working -- which is the whole reason this
   card looked finished on a live run. */
export function spawnLive(c: CallData): boolean {
  return c.kind === 'spawn' && !!c.spawnId && !SPAWN_SETTLED.has(c.spawnStatus)
}

/* Which of the three words the card's state cell says, for a spawn.

   `done`/`ok` describe the DISPATCH: the tool row settles `ok` within a second
   of a run that may go on for minutes, and stays `ok` if that run later fails.
   Before the run has said anything the dispatch is all there is, which is what
   the fallback is for -- a spawn refused before it ran never reports at all. */
/* Settled without succeeding. `cancelled` belongs here and not with `completed`:
   stopping is not finishing, and this front end already decided that two files
   away -- features/subagents/history.ts puts `cancelled` in its own BAD set, and
   the comment there names this exact failure, "how a running instance and a
   failed one both came to wear the green finished dot". */
const SPAWN_BAD = new Set(['failed', 'aborted', 'cancelled', 'interrupted'])

export function spawnState(c: CallData): 'ok' | 'bad' | 'run' {
  if (SPAWN_BAD.has(c.spawnStatus)) return 'bad'
  if (SPAWN_SETTLED.has(c.spawnStatus)) return 'ok'
  if (c.spawnStatus) return 'run'
  return c.done ? (c.ok ? 'ok' : 'bad') : 'run'
}

const msOfIso = (raw: unknown): number => {
  const ms = Date.parse(String(raw || ''))
  return Number.isFinite(ms) ? ms : 0
}

/* How long the RUN took, or has been going. Empty when nothing named its clock,
   so the row is dropped rather than printed with a blank -- a labelled row with
   no value reads as a broken render. */
export function spawnCost(c: CallData, now: number): string {
  if (!c.spawnT0) return ''
  const end = c.spawnT1 || now
  const ms = end - c.spawnT0
  return ms > 0 ? durText(ms) : ''
}

/* `subagent.list` and `subagent.status` name the same states differently -- the
   list says `run | ok | error | cancelled | queued | skipped`, the event says
   `pending | running | completed | failed | cancelled`. Translated here rather
   than by teaching `SPAWN_SETTLED` both, so a card holds one vocabulary and the
   difference lives at the one boundary it crosses.

   This was a real bug, and the test that should have caught it did not: its
   fixture spoke the event's words where the server sends the list's, so a
   restored card carried `ok`, `SPAWN_SETTLED` did not recognise it, and a run
   that had ended days ago drew a live second line.

   An unknown word settles rather than runs. The opposite default is what
   produced the bug, and a restored card is a finished run far more often than
   not. */
const LIST_STATUS: Record<string, string> = {
  run: 'running',
  queued: 'pending',
  ok: 'completed',
  error: 'failed',
  cancelled: 'cancelled',
  skipped: 'cancelled',
}

const fromListStatus = (raw: unknown): string => LIST_STATUS[String(raw || '')] || 'completed'

/* One `subagent.list` read for the whole conversation, shared by every restored
   card in it. The promise is memoised rather than the rows, so two cards opened
   in the same tick share one request. */
let spawnRoster: Promise<SpawnListRow[]> | null = null

/* Turn a restored card's task id into the record id its stream is read by.

   A spawn record's directory is `<stamp>-<task_id>` (the manager's own
   `make_call_id`), and the task id is what the tool's result sentence names --
   stamped onto the transcript row as `spawn_task_id` by the server that writes
   that sentence. So the row is found by suffix, the same match
   ui-tui/src/domain/spawnRun.ts makes.

   Called on open, not at restore: a transcript can hold a dozen delegated calls
   and asking for all of them would be a dozen requests for detail nobody has
   looked at -- the rule `hydrateDag` already follows.

   The list also supplies the header a restored card could not otherwise draw: its
   arguments hold no minted handle, so a resumed conversation named the agent and
   never the instance. Nothing the card already knows is overwritten -- a live
   card never comes here, and if one did, the event is the fresher word. */
export function resolveSpawn(lane: Lane, c: CallData): void {
  if (c.spawnId || !c.spawnTaskId || c.spawnAsked) return
  const read = source().spawnList
  if (!read) return
  c.spawnAsked = true
  if (!spawnRoster) spawnRoster = read()
  const want = `-${c.spawnTaskId}`
  spawnRoster.then((rows) => {
    const row = (rows || []).find((r) => (r.kind || 'spawn') === 'spawn' && String(r.id || '').endsWith(want))
    if (!row || !row.id) return
    c.spawnId = String(row.id)
    c.spawnAgent = c.spawnAgent || String(row.agent || '')
    c.spawnInstance = c.spawnInstance || String(row.instance || '')
    c.spawnLabel = c.spawnLabel || String(row.label || '')
    c.spawnStatus = c.spawnStatus || fromListStatus(row.status)
    c.spawnT0 = c.spawnT0 || msOfIso(row.started_at)
    c.spawnT1 = c.spawnT1 || msOfIso(row.ended_at)
    bump(lane, c)
    readSpawn(lane, c)
  }).catch(() => {
    /* Asked again if the reader reopens the card, and the memoised promise goes
       with it -- kept, every later card would inherit this one rejection. */
    c.spawnAsked = false
    spawnRoster = null
  })
}

/* Read one spawned run's messages, once.

   Called on a beat by the card that draws it, not by a store-owned timer: the
   card's own mount decides when the read is worth making, so a run scrolled out
   of the trail or a session left mid-run stops costing anything without a
   reaper.

   `reading` rather than a queue: a read slower than the beat is answered by the
   next beat, and stacking them would multiply requests against a sub-agent that
   is already the slow part. A failure clears the flag and keeps whatever the card
   had -- the failure is about the read, not about the run. */
export function readSpawn(lane: Lane, c: CallData): void {
  const read = source().spawnRecord
  if (!read || !c.spawnId || c.reading) return
  c.reading = true
  read(c.spawnId).then((rec) => {
    c.reading = false
    const msgs = (rec && rec.messages) || []
    /* Length, not identity: the method rebuilds the list from the activity
       collector on every call, so every answer is a fresh array and a reference
       check would repaint on every beat. A stream only ever grows, and the last
       row's own text moves as it streams -- so both are compared. */
    if (msgs.length === c.stream.length && tailText(msgs) === tailText(c.stream)) return
    c.stream = msgs
    bump(lane, c)
  }).catch(() => {
    c.reading = false
  })
}

/* The newest thing the run said, flattened to one line for the collapsed row.

   Three sources in falling order of "is this what it is doing right now": the
   message's own text, the thought it is forming, and failing both the tool it
   just reached for. The third matters most -- a sub-agent spends most of a run
   inside tool calls, where there is no text yet, and a tail that went blank
   there would stop moving during exactly the stretch the reader is watching.

   Scanned from the newest backwards rather than reading the last row: the last
   row can be one with nothing to show yet, and taking it would blank a tail that
   had been moving. */
export function tailText(msgs: HistoryMessage[]): string {
  for (let i = msgs.length - 1; i >= 0; i -= 1) {
    const m = msgs[i] as HistoryMessage
    const body = flat(m.text) || flat(m.reasoning_content) || flat(toolNames(m))
    if (!body) continue
    /* A tool's result named by the tool that produced it. The name alone is the
       row before this one; what makes this row worth a line is that an answer
       came back, and whose. */
    return m.role === 'tool' && m.name ? `${m.name} · ${body}` : body
  }
  return ''
}



/* The calls one message reached for, named. Joined rather than counted: a name
   says what is happening, `2 calls` does not. */
function toolNames(m: HistoryMessage): string {
  return (m.tool_calls || []).map((call) => call && call.name).filter(Boolean).join(', ')
}

/* `defence` and not a marker-stripping regex: the fence is nonce-tagged on both
   ends precisely so a forged close cannot end it, and the one place that check
   lives is `defence`. Without it the tail spent whole tool calls showing
   `[BEGIN UNTRUSTED list_dir #7d8064e3 - everything below until the matching...]`
   -- a boundary written for the model -- instead of what the run had found. */
const flat = (v: unknown): string =>
  (typeof v === 'string' ? defence(v).replace(/\s+/g, ' ').trim() : '')

/* The header one spawn card carries: `Spawn <instance>@<agent>: <task_summary>`.

   The event's values win over the model's arguments wherever it has landed. It
   has to: a caller that names no `instance` has one minted for it, so the
   arguments hold no handle at all while the run has a real one -- and the header
   must name the handle the conversation can actually be resumed by. Before the
   first frame the arguments are all there is, which is the whole reason for the
   fallbacks rather than an empty header.

   `agent` empty means the built-in loop, which the arguments spell as absent
   rather than as a name. */
export function spawnHead(c: CallData): { agent: string; instance: string; task: string } {
  const a = c.args as { instance?: string; task?: string }
  return {
    agent: c.spawnAgent || String(spawnAgentOf(a) || '') || t('gui.deleg.self'),
    instance: c.spawnInstance || String(a.instance || ''),
    task: c.spawnLabel || c.label || String(a.task || '').slice(0, 160),
  }
}

/* A spawn card is bound to its run through this, the same way a dag card is
   bound through `dagByCall` -- and for the same reason: `subagent.status`
   publishes on the delivery spine while the spawn's own tool row rides the turn
   channel, so nothing orders the two and a card that lost the race would drop
   every frame for the rest of the run.

   Separate maps rather than one shared with dag, because the two event families
   name different things by `tool_call_id`: sharing would let a graph's
   announcement land on a spawn card that happened to be keyed the same.

   No `dagPending`-style fallback here. Measured on this host, every
   `subagent.status` frame carries `tool_call_id` and it matches the spawn
   `tool.start` exactly, so there is nothing to guess -- and guessing is the bug
   the dag comment above describes. A frame without one is dropped: a spawn drawn
   on the wrong card is worse than one drawn with no stream. */
const spawnByCall = new Map<string, { lane: Lane; call: CallData }>()
/* What a run said before its card existed, per tool call. Bounded like
   `dagEarly` and for the same reason: a card that never arrives would otherwise
   buffer for the life of the page. */
const spawnEarly = new Map<string, SubagentStatusLike[]>()

/* A spawn card just appeared: it takes whatever announced itself while the row
   was in flight. */
function claimSpawn(lane: Lane, call: CallData): void {
  if (!call.callId) return
  spawnByCall.set(call.callId, { lane, call })
  const early = spawnEarly.get(call.callId)
  if (!early) return
  spawnEarly.delete(call.callId)
  early.forEach((p) => spawnFeed(p))
}

/* One lifecycle frame for a spawned run.

   The identity lands from `pending`, before any record exists: `instance`,
   `agent` and `label` are all on the first frame, which is what lets the card
   name the run it is waiting on rather than showing a bare tool call. The record
   id arrives with `running` -- that is what `subagent.context` reads, so it is
   what opens the stream. */
export function spawnFeed(p: SubagentStatusLike | null): void {
  if (!p) return
  const callId = p.tool_call_id ? String(p.tool_call_id) : ''
  if (!callId) return
  const owner = spawnByCall.get(callId)
  if (!owner) {
    const waiting = spawnEarly.get(callId)
    if (waiting) {
      if (waiting.length < EARLY_MAX) waiting.push(p)
    } else {
      spawnEarly.set(callId, [p])
    }
    return
  }
  const { lane, call } = owner
  call.spawnAgent = String(p.agent || '') || call.spawnAgent
  call.spawnInstance = String(p.instance || '') || call.spawnInstance
  call.spawnLabel = String(p.label || '') || call.spawnLabel
  call.spawnStatus = String(p.status || '') || call.spawnStatus
  /* Only from `running` onward, and never unset: a terminal frame carries it
     too, and the finished stream is read through the same id. */
  if (p.call_id) call.spawnId = String(p.call_id)
  if (p.started_at) call.spawnT0 = Number(p.started_at) || 0
  if (p.ended_at) call.spawnT1 = Number(p.ended_at) || 0
  bump(lane, call)
}

export function dagFeed(type: string, p: DagFeedPayload | null): void {
  if (!p) return
  const callId = p.tool_call_id ? String(p.tool_call_id) : ''
  if (type === 'dag.run_started') {
    /* By id when the run named one, and only otherwise by "whoever asked last".
       Both clear the fallback: a claim that has been made must not be made
       twice, or the next graph the turn dispatches lands on this card. */
    const owner = (callId && dagByCall.get(callId)) || dagPending
    if (owner) {
      dagLive.set(String(p.run_id), owner)
      if (owner === dagPending) dagPending = null
    } else if (callId) {
      /* The announcement outran the tool row. Kept for the card to collect when
         it arrives rather than dropped, which is what left the graph unbound.
         Returning here rather than falling through to the unbound guard below,
         which would find the array this line just created and push the same
         event onto it a second time. Harmless while an announcement is
         idempotent, and the duplicate sits adjacent to the original so it
         replays before any node report -- but both of those are accidents of
         how the array is built, and the first thing that makes a run's opening
         count for something inherits a double-fire with nothing covering it. */
      dagEarly.set(callId, [[type, p]])
      return
    }
  }
  const f = dagLive.get(String(p.run_id))
  if (!f) {
    /* A node that reported inside the same gap. Buffered only behind an
       announcement already waiting for this call, so this is the rest of one
       run's opening rather than a bucket for every event nothing claims. */
    const waiting = callId ? dagEarly.get(callId) : undefined
    if (waiting && waiting.length < EARLY_MAX) waiting.push([type, p])
    return
  }
  const { lane, call } = f
  if (type === 'dag.run_started') {
    call.runId = String(p.run_id)
    call.live = true
    /* Merged, not assigned. A dag call the model made already holds the whole
       request from its own arguments, and this event carries structure only -- so
       letting it win would replace every prompt template with nothing. For the
       load of a `mode: dag` playbook it is the other way round: the arguments are
       `{name, params, fills}` and this event is the first time the graph exists at
       all. Both cases are the same merge. */
    call.nodes = dagNodes.merge(call.nodes, dagNodes.fromStarted(p))
    /* Which is also when the rest of the request becomes worth asking for: the
       event says who and in what order, never what each node was asked. */
    if (call.open) hydrateDag(lane, call)
    bump(lane, call)
  } else if (type === 'dag.node_updated') {
    call.nodes = dagNodes.applyUpdate(call.nodes, p)
    openFirstFailure(call)
    bump(lane, call)
  } else if (type === 'dag.run_completed') {
    ;(p.files || []).forEach((x) => { call.nodes = dagNodes.applyUpdate(call.nodes, x) })
    dagLive.delete(String(p.run_id))
    if (call.callId) dagByCall.delete(call.callId)
    openFirstFailure(call)
    bump(lane, call)
  } else if (type === 'dag.run_replanned') {
    /* Always arrives before this run's own `dag.run_completed` -- the backend
       emits it right after the waiting node's decision is recorded, not once
       the old run winds down -- so `dagLive` still holds this card by the time
       it is looked up above; no early-buffer handling is needed here the way
       `dag.run_started` needs one. */
    if (p.replan_run_id) call.replannedInto = String(p.replan_run_id)
    bump(lane, call)
  }
}

/* A card restored from history saw none of its run's events, so the run id has to
   come out of the result line it kept. Separate from the read below because it is
   needed earlier and costs nothing: a node is only openable once its run has an
   identity, and that is true whether or not anyone opened the card. */
function bindRun(call: CallData): void {
  if (call.runId) return
  const id = dagRunIdFrom(call.res)
  if (!id) return
  call.runId = id
  call.live = true
}

/* Read the run back off disk: `dag.get` is the only source that carries state
   *and* the whole request, so it serves two cases that used to be handled apart.
   A card restored from history saw none of its events and needs everything; a
   live card dispatched by the engine has structure from `run_started` and is
   missing the templates and inputs. Merging covers both, and the merge is what
   keeps the second from erasing what the first already knew.
 *
 * Asked once per card (`asked`), because it answers a question that cannot change
 * for a finished run and because a card is re-rendered on every node update. */
function hydrateDag(lane: Lane, call: CallData): void {
  bindRun(call)
  if (!call.runId || call.asked) return
  const read = source().dagRun
  if (!read) return
  call.asked = true
  const id = call.runId
  read(id).then((run) => {
    call.nodes = dagNodes.merge(call.nodes, dagNodes.fromSnapshot(run?.files || []))
    /* Only when the card has none. A model-composed graph put its line on the
       arguments and has it from the first paint; a playbook load has nothing
       until here. Letting the read win either way would replace a title that is
       already on screen with the identical string on every hydrate. */
    if (!call.runTitle) call.runTitle = String(run?.task_summary || '')
    openFirstFailure(call)
    bump(lane, call)
  }).catch(() => {
    /* A run whose dir is gone keeps whatever the card already had, and may be
       asked again: the failure is about the read, not about the run. */
    call.asked = false
  })
}

function newCallData(
  id: ReturnType<typeof actId>,
  kind: CallData['kind'],
  display?: string | null,
  callId?: string | null,
): CallData {
  const a = id.args
  const c: CallData = {
    v: 0, id: nextId(), kind, name: id.name, args: a, via: id.via, srv: id.srv,
    display: display ? String(display) : '',
    label: actLabel(id.name, a, display), rowLabel: '',
    done: false, ok: true, ms: 0, res: '', truncated: false,
    hunk: kind === 'plain' ? hunkFor(id.name, a) : null,
    open: false, t0: Date.now(), runId: null, runTitle: '', nodes: [], live: false, sel: null, selAuto: false, selFull: false, asked: false,
    callId: callId ? String(callId) : '',
    spawnAgent: '', spawnInstance: '', spawnLabel: '', spawnStatus: '', spawnId: '',
    spawnTaskId: '', spawnAsked: false, spawnT0: 0, spawnT1: 0, stream: [], reading: false,
  }
  if (kind === 'dag') c.runTitle = String(a.task_summary || '')
  if (kind === 'spawn') {
    const who = spawnAgentOf(a) ? String(spawnAgentOf(a)) + (a.instance ? ' @' + a.instance : '') : t('gui.deleg.self')
    c.rowLabel = c.label ? `${who} · ${c.label}` : who
  }
  // The graph, when the call itself described one. A `load_playbook` in dag mode
  // does not: its arguments are `{name, params, fills}` and the graph only exists
  // once the engine has assembled it, so those nodes arrive later, from the
  // run-started event or from `dag.get`. Neither label is written here -- the row
  // reads `label` (the playbook's name, or nothing) beside the shape it derives
  // from whatever nodes it has by then, which is a rendering decision and moves
  // as the nodes do.
  if (kind === 'dag') c.nodes = dagNodes.fromArgs(a)
  return c
}

/* Row-fold transitions the legacy paintWork made: the step that just outgrew
   a single call folds its rows for the first time, and a failure is the one
   thing worth opening unasked -- unless the reader pinned the fold. */
function paintWork(lane: Lane, seg: StepData, grewPast1: boolean): void {
  if (seg.calls.length > 1 && grewPast1 && !seg.wkPinned) seg.wkOpen = false
  if (seg.calls.length > 1 && seg.failed && !seg.wkPinned && !seg.wkOpen) seg.wkOpen = true
  bump(lane, seg)
}

function callDone(lane: Lane, seg: StepData, c: CallData,
  ok: boolean, res: unknown, ms: number, diff?: string | string[] | null, truncated?: boolean): void {
  c.done = true
  c.ok = ok
  c.res = String(res == null ? '' : res)
  c.ms = ms || 0
  c.truncated = !!truncated
  if (diff) c.hunk = hunks.fromUnified(diff)
  if (c.kind === 'dag') {
    if (dagPending && dagPending.call === c) dagPending = null
    /* The id only. The rest of the run is read when someone opens the card:
       asking here would be one request per dag card of every conversation
       restored, for detail nobody has looked at. */
    bindRun(c)
    if (c.open) hydrateDag(lane, c)
  }
  if (!ok) seg.failed = true
  poke(lane)
  bump(lane, c)
  paintWork(lane, seg, false)
}

export function newStep(lane: Lane): StepHandle {
  const seg: StepData = {
    v: 0, id: nextId(), kind: 'step',
    think: '', thinkLive: false, thinkOpen: false, thinkPinned: false, thinkShown: false,
    say: '', sayCaret: false, hasSay: false, hasThink: false, hasQA: false, failed: false,
    calls: [], wkOpen: true, wkPinned: false, merged: false,
  }
  push(lane, seg)

  const reveal = (): void => {
    seg.thinkShown = true
    if (!seg.thinkLive) {
      seg.thinkLive = true
      if (!seg.thinkPinned) seg.thinkOpen = true
    }
    poke(lane)
    bump(lane, seg)
  }

  const thinkDone = (): void => {
    if (!seg.thinkShown) return
    if (seg.thinkLive) {
      seg.thinkLive = false
      if (!seg.thinkPinned) seg.thinkOpen = false
    }
    bump(lane, seg)
  }

  const h: StepHandle = {
    seg,
    get hasThink() { return seg.hasThink },
    set hasThink(x: boolean) { seg.hasThink = x; bump(lane, seg) },
    get hasSay() { return seg.hasSay },
    set hasSay(x: boolean) { seg.hasSay = x; bump(lane, seg) },
    get hasQA() { return seg.hasQA },
    set hasQA(x: boolean) { seg.hasQA = x; bump(lane, seg) },
    get failed() { return seg.failed },
    set failed(x: boolean) { seg.failed = x; bump(lane, seg) },
    thinkAppend(text: string) {
      seg.hasThink = true
      seg.think += text || ''
      seg.thinkShown = true
      if (!seg.thinkLive) {
        seg.thinkLive = true
        if (!seg.thinkPinned) seg.thinkOpen = true
      }
      poke(lane)
      scheduleFlush(lane, seg)
    },
    reveal,
    thinkDone,
    setThinkOpen(open: boolean) {
      seg.thinkOpen = open
      bump(lane, seg)
    },
    setSay(text: string) {
      seg.hasSay = true
      seg.say = String(text || '')
      bump(lane, seg)
    },
    sayDelta(text: string) {
      thinkDone()
      seg.hasSay = true
      seg.say += text || ''
      seg.sayCaret = true
      scheduleFlush(lane, seg)
    },
    tool(name: string, args: unknown, display?: string | null, callId?: string | null): CallHandle {
      thinkDone()
      const id = actId(name || 'tool', args)
      // `load_playbook` is a dag call too, from the card's point of view: a
      // `mode: dag` playbook is dispatched by the engine, so the only call the
      // model makes is the load, and the graph is what that call produced.
      // Keyed on the tool name alone, the run's events arrived with no card
      // waiting for them and every one of them was dropped -- the sheet drew
      // the graph while the trail showed a plain call that had somehow started
      // six sub-agents. A `mode: prompt` load starts no run and leaves the
      // pending slot unclaimed, which `callDone` clears.
      const DAG_CALLS = ['run_subagent_dag', 'load_playbook']
      const kind: CallData['kind'] = id.name === 'spawn' ? 'spawn' : DAG_CALLS.includes(id.name) ? 'dag' : 'plain'
      const c = newCallData(id, kind, display, callId)
      const grew = seg.calls.length === 1
      seg.calls.push(c)
      if (kind === 'dag') claimRun(lane, c)
      else if (kind === 'spawn') claimSpawn(lane, c)
      poke(lane)
      paintWork(lane, seg, grew)
      bumpList(lane)
      return {
        done: (ok, res, ms, diff, truncated) => callDone(lane, seg, c, ok, res, ms, diff, truncated),
        spawnTask: (taskId) => { c.spawnTaskId = String(taskId || '') },
      }
    },
    seal() {
      thinkDone()
      seg.sayCaret = false
      paintWork(lane, seg, false)
    },
  }
  return h
}

/* ── folding ───────────────────────────────────────────────────────────── */

const stepSolid = (s: StepData): boolean =>
  s.calls.length > 0 || (s.thinkShown && s.hasThink) || !!s.say.trim()

/* Whether anything under this fold is still running.
 *
 * A backgrounded sub-agent outlives the turn that dispatched it -- by minutes,
 * and the card carries a live tail of what it is saying, which is the only
 * place a reader can see that at all. */
function holdsALiveRun(fold: FoldData): boolean {
  return fold.steps.some((step) => step.calls.some(spawnLive))
}

/* Every fold this lane opened by itself, shut. Two are left alone.
 *
 * The reader's own: `toggleFold` clears `auto`, so a fold they have touched is
 * no longer one of these.
 *
 * And any that still holds a running sub-agent. Shutting it takes the run's
 * live tail off the screen while the run is still going, and a shut body is
 * not built at all, so nothing is left to watch. Measured: a spawn dispatched
 * in one turn, the reader asks "how is it going" in the next, that next turn's
 * fold opens and shuts this one -- and the answer had to be fetched by reading
 * a file, because the page was no longer showing what the page already knew.
 *
 * The fold's own note already said half of this: "a backgrounded graph
 * outlives the turn that dispatched it, so a reader saw `done` over a task
 * still running below." That was answered by renaming the row. The row was
 * never the part that was hidden. */
function shutAutoFolds(lane: Lane, except: FoldData | null): void {
  lane.segs.forEach((s) => {
    if (s.kind !== 'fold' || s === except || !s.auto) return
    if (holdsALiveRun(s)) return
    s.auto = false
    s.open = false
    bump(lane, s)
  })
}

/* Once the answer has landed, everything that led to it collapses behind one
   line. A turn gets ONE fold: work landing after an early fold joins it.
 *
 * `live` is the turn the reader just watched, and its fold opens. Shut, it
 * took a sequence they had been reading -- five rows, each naming what the
 * step did -- and replaced it with the word "steps", at the one moment they
 * were most likely to want it. Nothing on the row said five things were under
 * it, so there was not even a reason to click.
 *
 * One at a time, and the previous one shuts as the next opens, because the
 * weight is what a SESSION accumulates: a forty-turn session built 7361 nodes
 * and 6400 of them sat in shut fold bodies. One open fold is the turn on
 * screen, not a session's worth.
 *
 * Replay opens at most one too -- see :func:`openLastFold`, which is the same
 * rule reached from the other entry point -- so "shut unless the reader is
 * looking at it" is the whole of it, whichever way the turn arrived. */
export function collapse(lane: Lane, time?: string | null, live = false): void {
  const segs = lane.segs
  const loose: StepData[] = []
  let fold: FoldData | null = null
  let firstAt = -1
  for (let i = segs.length - 1; i >= 0; i -= 1) {
    const s = segs[i] as Seg
    /* Where THIS turn began. A question is one opening; a delivery row is the
       other -- a delegated result re-entering opens a turn with no question
       typed, so a scan that only stopped at `ask` walked back over it into the
       previous turn, folded this turn's work into that turn's fold and wrote
       this turn's clock onto it. The reader then saw one fold whose header
       said 20s over a thought inside it that said 21s. */
    if (s.kind === 'ask' || s.kind === 'sdlv') break
    if (s.kind === 'fold') { fold = s; break }
    if (s.kind === 'step') { loose.unshift(s); firstAt = i }
  }
  if (!loose.length) return
  if (fold) {
    loose.forEach((s) => {
      const i = segs.indexOf(s)
      if (i >= 0) segs.splice(i, 1)
      fold!.steps.push(s)
    })
    fold.time = time || null
    bump(lane, fold)
    bumpList(lane)
    return
  }
  if (!loose.some(stepSolid)) return
  const f: FoldData = {
    v: 0, id: nextId(), kind: 'fold', time: time || null, open: live, auto: live, steps: [],
  }
  if (live) shutAutoFolds(lane, f)
  segs.splice(firstAt, 0, f)
  loose.forEach((s) => {
    const i = segs.indexOf(s)
    if (i >= 0) segs.splice(i, 1)
    f.steps.push(s)
  })
  bumpList(lane)
}

/* The fold over the turn a replayed conversation ends on, opened.
 *
 * Same reason as the live one -- the reader is looking at the bottom of the
 * conversation and that is the turn they came back for -- and the same limit:
 * ONE body built, not a session's worth, so the weight the shut default is for
 * is still not there.
 *
 * Only when the last turn is the one that fold belongs to. The scan stops at a
 * question or a delivery exactly as `collapse`'s does, because it is asking the
 * same thing from the other end: a conversation whose final turn answered with
 * no work of its own has its last fold one turn further back, and opening that
 * would open a turn the reader did not return to.
 *
 * Marked `auto`, so the reader's first question shuts it like any other -- it
 * is the runtime's, not theirs, until they touch it. */
export function openLastFold(lane: Lane): void {
  for (let i = lane.segs.length - 1; i >= 0; i -= 1) {
    const s = lane.segs[i] as Seg
    if (s.kind === 'ask' || s.kind === 'sdlv') return
    if (s.kind !== 'fold') continue
    s.open = true
    s.auto = true
    bump(lane, s)
    return
  }
}

const isSilent = (s: StepData): boolean =>
  !s.hasSay && !s.hasThink && !s.hasQA && !s.failed && s.calls.length > 0

/* Consecutive silent steps read as one stretch: their rows merge under one
   summary line. Operates on the turn's own steps, wherever they now sit. */
export function foldRuns(lane: Lane, steps: StepData[]): void {
  let i = 0
  while (i < steps.length) {
    if (!isSilent(steps[i] as StepData)) { i += 1; continue }
    let j = i
    while (j < steps.length && isSilent(steps[j] as StepData)) j += 1
    if (j - i >= MIN_RUN_STEPS) mergeRun(lane, steps.slice(i, j))
    i = j
  }
}

/* An episode that produced ONLY a thought: no prose, no call, no failure. The
   agent loop opens an episode per model call, and a call that comes back with
   nothing usable is retried -- so a turn the model had to retry four times left
   four "thought" rows behind, one per attempt, over a single answer. Reloading
   the same turn showed one, because the session keeps only the message that
   landed: the live turn and its own replay disagreeing about what happened. */
const isThoughtOnly = (s: StepData): boolean =>
  s.hasThink && !s.hasSay && !s.hasQA && !s.failed && !s.calls.length

/* One row for the run, holding every thought in it -- nothing the reader
   watched arrive is thrown away, it is just no longer one row per attempt. */
function mergeThoughts(lane: Lane, group: StepData[]): void {
  const head = group[0] as StepData
  head.think = group.map((s) => s.think).filter((x) => x.trim()).join('\n\n')
  group.slice(1).forEach((s) => replaceStep(lane, s, null))
  bump(lane, head)
  bumpList(lane)
}

/* Same shape as foldRuns, over the other kind of run. Separate passes because
   the two merges keep different things: a silent run keeps its calls under a
   new holder, a thought run keeps its first step and absorbs the rest. */
export function foldThoughts(lane: Lane, steps: StepData[]): void {
  let i = 0
  while (i < steps.length) {
    if (!isThoughtOnly(steps[i] as StepData)) { i += 1; continue }
    let j = i
    while (j < steps.length && isThoughtOnly(steps[j] as StepData)) j += 1
    if (j - i >= MIN_RUN_STEPS) mergeThoughts(lane, steps.slice(i, j))
    i = j
  }
}

function replaceStep(lane: Lane, at: StepData, next: StepData | null): boolean {
  const i = lane.segs.indexOf(at)
  if (i >= 0) {
    if (next) lane.segs.splice(i, 1, next)
    else lane.segs.splice(i, 1)
    return true
  }
  for (const s of lane.segs) {
    if (s.kind !== 'fold') continue
    const j = s.steps.indexOf(at)
    if (j >= 0) {
      if (next) s.steps.splice(j, 1, next)
      else s.steps.splice(j, 1)
      bump(lane, s)
      return true
    }
  }
  return false
}

function mergeRun(lane: Lane, group: StepData[]): void {
  const calls = group.flatMap((s) => s.calls)
  if (!calls.length || !calls.every((c) => c.done)) return
  const holder: StepData = {
    v: 0, id: nextId(), kind: 'step',
    think: '', thinkLive: false, thinkOpen: false, thinkPinned: false, thinkShown: false,
    say: '', sayCaret: false, hasSay: false, hasThink: false, hasQA: false,
    failed: calls.some((c) => !c.ok),
    calls, wkOpen: false, wkPinned: false, merged: true,
  }
  replaceStep(lane, group[0] as StepData, holder)
  group.slice(1).forEach((s) => replaceStep(lane, s, null))
  bumpList(lane)
}

/* The render half of the turn's landing: promote the streamed prose into the
   answer block where the prose stood, merge the silent stretches, fold. */
export function finishTurn(lane: Lane, st: StepHandle | null, steps: StepData[], time?: string | null): void {
  stopFlush(lane)
  if (st) st.seal()
  /* The last step that said something, which is usually the open one -- but a
     step boundary opens on every episode, and a notice clears the open step
     outright, so a turn stopped in a later episode has its prose in an earlier
     one. Promoting only the open step left that prose as narration for the
     fold to close over, while a reload of the same turn showed it as the
     answer: the two halves disagreeing again, one case over. */
  const said = st && st.seg.say.trim()
    ? st.seg
    : [...steps].reverse().find((g) => g.say.trim()) || null
  if (said) {
    const seg = said
    const text = seg.say
    let at = lane.segs.indexOf(seg)
    seg.say = ''
    seg.hasSay = false
    seg.sayCaret = false
    if (!seg.hasThink && !seg.calls.length) {
      replaceStep(lane, seg, null)
      const k = steps.indexOf(seg)
      if (k >= 0) steps.splice(k, 1)
    } else {
      at += 1
      bump(lane, seg)
    }
    answer(lane, text, stamp(Date.now()), at >= 0 ? at : null)
  }
  foldRuns(lane, steps)
  /* After the answer is promoted, not before: promoting empties the last
     step's prose, which is what makes it the tail of the retry run it belongs
     to. */
  foldThoughts(lane, steps)
  /* The one live caller, and the only fold that opens. */
  collapse(lane, time, true)
}

/* Whether this turn put anything on the stage: an answer, a fold, or a step
   still loose. The stop note's promise is checked against this -- "the output
   above is kept" over a bare question is a promise about nothing. Runtime rows
   (a note, a status line) are not the turn's output and do not count. */
export function turnKept(lane: Lane): boolean {
  for (let i = lane.segs.length - 1; i >= 0; i -= 1) {
    const s = lane.segs[i] as Seg
    if (s.kind === 'ask') return false
    if (s.kind === 'note' || s.kind === 'status') continue
    return true
  }
  return false
}

/* ── toggles (the components call these; scroll pinning stays with them) ── */

export function toggleThink(lane: Lane, seg: StepData): void {
  seg.thinkPinned = true
  seg.thinkOpen = !seg.thinkOpen
  bump(lane, seg)
}

export function toggleWork(lane: Lane, seg: StepData): void {
  seg.wkPinned = true
  seg.wkOpen = !seg.wkOpen
  bump(lane, seg)
}

export function toggleCall(lane: Lane, c: CallData): void {
  c.open = !c.open
  /* Opening a dag card is what makes the rest of the request worth fetching: a
     card the engine dispatched has only structure until then, and asking on
     every card of every restored conversation would be a read per card for
     detail nobody opened. */
  if (c.open && c.kind === 'dag') hydrateDag(lane, c)
  bump(lane, c)
}

/* The node whose detail the card shows. Clicking the open one closes it, which
   is what makes the graph readable again without a second control.

   Selecting rather than opening the run's transcript: the panel that does that
   takes half the screen, and comparing two nodes' configuration is the thing a
   reader of this card is most often doing. The panel is one explicit click away. */
export function pickDagNode(lane: Lane, c: CallData, id: string): void {
  c.sel = c.sel === id ? null : id
  c.selFull = false
  bump(lane, c)
}

export function toggleDagFull(lane: Lane, c: CallData): void {
  c.selFull = !c.selFull
  bump(lane, c)
}

export function toggleFold(lane: Lane, f: FoldData): void {
  /* The reader has taken this one over. Whatever they leave it at is where it
     stays: the next turn shuts only the folds nobody has touched. */
  f.auto = false
  f.open = !f.open
  bump(lane, f)
}

export function toggleQa(lane: Lane, s: QaData): void {
  s.open = !s.open
  bump(lane, s)
}

export function toggleAskClip(lane: Lane, s: AskData): void {
  s.clipOpen = !s.clipOpen
  bump(lane, s)
}

export function expandAskAtts(lane: Lane, s: AskData): void {
  s.expanded = true
  bump(lane, s)
}

/* ── history: reading a stored turn as segments ────────────────────────── */

function callIndex(messages: HistoryMessage[]): Map<string, { name: string; args: Record<string, unknown> }> {
  const byId = new Map<string, { name: string; args: Record<string, unknown> }>()
  messages.forEach((m) => {
    if (!m || m.role !== 'assistant' || !Array.isArray(m.tool_calls)) return
    m.tool_calls.forEach((c) => {
      let args: Record<string, unknown> = {}
      try { args = JSON.parse(c.arguments || '{}') || {} } catch { args = {} }
      byId.set(String(c.id || ''), { name: c.name || '', args })
    })
  })
  return byId
}

/* An ACP transcript puts a human title where a tool name goes: the program
   becomes the verb and the command the argument. A raven tool name has no
   colon and comes back unchanged; a shell title spans newlines on purpose. */
const ACP_TITLE_RE = /^([A-Za-z_][\w.-]{0,31}):\s*(\S[\s\S]*)$/
export const callParts = (raw: unknown): { name: string; display: string } => {
  const m = ACP_TITLE_RE.exec(String(raw || ''))
  return m ? { name: m[1] as string, display: m[2] as string } : { name: String(raw || ''), display: '' }
}

export function history(lane: Lane, messages: HistoryMessage[], after: HistoryMessage[] = []): void {
  /* A different conversation, so a different roster -- but only on the lane that
     IS one.

     `subagent.list` is asked per session and the memo was not, so a card restored
     in the second conversation searched the first one's rows, matched nothing,
     and stayed unresolved for good: `spawnAsked` is set once. Cleared here rather
     than keyed by a session id the store does not hold -- this is the call that
     means "the transcript is now showing a different conversation", and the cards
     it is about to build are new objects anyway, so the memo and the flags that
     read it reset together.

     `lane.main` is load-bearing. `agentPaintLane` replays through here as well,
     four times, to draw a delegated run's own messages; clearing on those threw
     the memo away every time a card's stream painted, and the second card opened
     in one conversation read the list all over again. One read per conversation
     is the contract, and this is what separates the two callers.

     A reconnect replay of the same conversation does come through and pays one
     extra read, which is the right price: the list may have grown while the
     socket was down. */
  if (lane.main) spawnRoster = null
  const src = source()
  /* This conversation's own delivery rows go: they are about to be replayed, or
     it is a different conversation now. Its scope, and only its scope.

     `lane.main` again, and load-bearing for the same reason as the roster above.
     `agentPaintLane` replays through here on a lane of its own, four times per
     poll, to draw a delegated run -- and clearing the whole registry on those
     emptied the deliveries of the conversation underneath. Nothing brought them
     back either: `loadDeliveries` only runs when a conversation is opened. So
     the turn's card and the desk's shelf, which read the same registry, both
     went blank the moment a reader opened a sub-agent panel.

     A delegated stream's own rows are not cleared here. They end where the
     conversation does -- `resetView` calls `wsReset`, and the workspace store's
     `resetShared` restores an empty registry. */
  if (lane.main) deliveries.dropScope(deliveries.SESSION)
  let toolRun: StepHandle | null = null
  const sealTools = (): void => { if (toolRun) { toolRun.seal(); toolRun = null } }
  const calls = callIndex(messages)
  /* Only the LAST assistant text before the next user message is the turn's
     answer; the ones before it are the model narrating mid-turn -- and a
     marker the runtime wrote is neither. A marker carries `text` too (the
     closing entry of a stopped turn reads "(turn cancelled by the user)", a
     notice says what the runtime did) because the model reads it on the next
     turn; counting it as model prose meant the turn's real last words were no
     longer the last, so they were drawn as narration and the fold closed over
     them -- under a note promising the output above was kept. */
  const spoken = (m: HistoryMessage | undefined): boolean =>
    !!m && m.role === 'assistant' && !m.turn_ended && !m.notice && !!m.text && !!m.text.trim()
  /* Scanned over what FOLLOWS as well, which is not always drawn. The agent
     stage paints a run in slices -- each poll hands over only what arrived
     since the last one -- and "is anything still to be said" cannot be answered
     from inside one slice. Answered from inside it, the last assistant message
     of every slice looked like the turn's answer, so a run came out as a
     column of finished answers, one per poll, each with its own copy button,
     instead of narration folded under the work it introduced. `after` is the
     rest of the conversation; nothing in it is drawn here. */
  const ahead = after.length ? messages.concat(after) : messages
  const isFinal = messages.map((m, i) => {
    if (!(m && m.role === 'assistant' && m.text && m.text.trim())) return false
    for (let j = i + 1; j < ahead.length; j += 1) {
      const n = ahead[j] as HistoryMessage
      if (n && n.role === 'user' && n.text && n.text.trim()) return true
      if (spoken(n)) return false
    }
    return true
  })
  /* The gap between the question and the answer IS how long the turn took. */
  let turnAt = 0
  const msOf = (x: unknown): number => { const v = new Date(x as string).getTime(); return isNaN(v) ? 0 : v }
  const foldClose = (endAt: number): void => {
    collapse(lane, turnAt && endAt && endAt - turnAt >= 1000 ? durText(endAt - turnAt) : null)
  }
  /* An answer whose own message also carried tool calls waits for the end of
     the turn. The text was said BEFORE those calls, and their results come
     after it in the payload, so emitting it here would fold the work in
     afterwards and leave the answer sitting ABOVE the calls it introduced --
     the reverse of the order the same turn ends in live, where finishTurn
     inserts the answer past the step. */
  let held: { text: string; when: string | null } | null = null
  /* The turn number the workspace record files a change under, counted the way
     it counts them: one per user message with text (wsOnHistory in
     demo/100-workspace.js does exactly this, over these same messages). Two
     readers of one numbering rather than a number passed between them, because
     the panel's replay and this one are separate entry points on the same
     payload -- but that makes the rule itself the contract, so it is stated
     here and there in the same words.

     A delegated lane resumes its own count rather than starting over: it is
     painted a slice at a time, so counting from zero over each slice filed every
     poll's first turn under the same key as the last poll's. The conversation's
     lane is handed its whole list and genuinely starts at zero. */
  let turnNo = lane.main ? 0 : lane.agentTurn || 0
  /* The fold and the answer the turn was holding. */
  const closeTurn = (endAt: number): void => {
    foldClose(endAt)
    if (held) {
      answer(lane, held.text, held.when)
      held = null
    }
  }
  /* The products, last. Two call sites, and every turn reaches exactly one:
     the next question closes the turn before it, and the end of the payload
     closes the final one. Neither runs before the note a stop marker leaves,
     which is the order softStop appends them in live -- the note, then the
     products. */
  const closeProducts = (): void => { artifacts(lane, turnNo) }
  messages.forEach((m, i) => {
    if (m.role === 'user' && (m.origin || m.delegated)) {
      /* A user entry the RUNTIME wrote, not a person typing. Two shapes:
         - `delegated` (a sub-agent / dag result re-entering): the full
           delivery identity. It opens a turn like a question does -- the turn
           bookkeeping below is the same -- and the row a live client draws
           from the boundary is drawn here off the same identity, so the two
           views agree.
         - `origin` alone (a cron reminder, a sentinel notice, a sub-agent
           announce an older server wrote): no identity to draw, so the row is
           the quiet marker that a live view showed, and no workspace turn
           opens -- that bookkeeping belongs to the delegated shape. */
      sealTools()
      closeTurn(msOf(m.timestamp))
      const d = m.delegated
      if (d) {
        /* Close the PARENT's products before opening this turn -- the same
           moment a plain question would, so the two branches agree about
           which turn a file belongs to. */
        closeProducts()
        turnNo += 1
        turnAt = msOf(m.timestamp)
        const isDag = d.kind === 'dag'
        delivered(lane, {
          label: String(d.label || ''),
          isDag,
          status: d.status,
          body: m.text || '',
          open: () => {
            const src = source()
            if (isDag) src.openDagRun?.(String(d.run_id || d.label || ''))
            else src.openSpawn?.('', String(d.label || ''))
          },
        })
      } else {
        foldClose(msOf(m.timestamp))
        /* Where the cron reminder's own turn starts. The delegated branch above
           moves the clock's anchor and this one did not, which nothing showed
           while both turns shared one fold -- the span was wrong but it was
           wrong on a header nobody could attribute. Now that the turn has a
           fold of its own, that header reads as ITS duration, so it has to be
           measured from here. `turnNo` stays put: the workspace-turn
           bookkeeping belongs to the delegated shape, as the note above says. */
        turnAt = msOf(m.timestamp)
        delivered(lane, { label: String(m.origin || ''), isDag: false, status: 'ok', open: () => {} })
      }
      return
    }
    if (m.role === 'user' && m.text && m.text.trim()) {
      sealTools()
      closeTurn(msOf(m.timestamp))
      closeProducts()
      turnNo += 1
      turnAt = msOf(m.timestamp)
      askText(lane, m.text, stamp(m.timestamp as string))
      return
    }
    if (m.role === 'assistant' && m.notice) {
      sealTools()
      closeTurn(msOf(m.timestamp))
      note(lane, t('gui.notice.' + (m.notice.kind || ''), undefined, m.notice.kind || ''),
        m.notice.detail || '', { quiet: true })
      return
    }
    if (m.role === 'assistant' && m.turn_ended) {
      sealTools()
      closeTurn(msOf(m.timestamp))
      const stopped = m.turn_ended.status === 'cancelled'
      /* Same promise as the live stop, checked the same way: a replayed turn
         whose whole content is this marker has no output above to keep. */
      const halted = turnKept(lane) ? 'gui.halted' : 'gui.halted_bare'
      note(lane, stopped ? t(halted) : t('gui.turn_died', { e: '' }).replace(/\s*[-·]\s*$/, ''),
        stopped ? '' : (m.turn_ended.reason || ''), { quiet: stopped })
      return
    }
    if (m.role === 'assistant') {
      const thought = String(m.reasoning_content || '').trim()
      const text = String(m.text || '').trim()
      if (thought) {
        sealTools()
        toolRun = newStep(lane)
        toolRun.seg.hasThink = true
        toolRun.seg.think = thought
        toolRun.reveal()
        toolRun.thinkDone()
      }
      if (!text) return
      if (isFinal[i]) {
        sealTools()
        if ((m.tool_calls || []).length) {
          held = { text, when: stamp(m.timestamp as string) }
          return
        }
        foldClose(msOf(m.timestamp))
        answer(lane, text, stamp(m.timestamp as string))
      } else if (thought && toolRun) {
        toolRun.setSay(text)
      } else {
        sealTools()
        const st = newStep(lane)
        st.setSay(text)
        st.seal()
      }
      return
    }
    if (m.role === 'tool') {
      recordDelivery(lane, turnNo, m.metadata, m.tool_call_id)
      if (!toolRun) toolRun = newStep(lane)
      const hit = calls.get(String(m.tool_call_id || '')) || { name: '', args: null }
      const parts = callParts(hit.name || m.name || 'tool')
      const h = toolRun.tool(parts.name, hit.args || null, parts.display || null)
      /* Before `done`, which is when a spawn card first has anything to resolve:
         the server stamps the run's task id on the result row it wrote the
         sentence for, and it is the only thread a restored card has back to the
         record. */
      if (m.spawn_task_id && h.spawnTask) h.spawnTask(String(m.spawn_task_id))
      const preview = src.clean(m.text).split('\n').slice(0, 8).map((l) => l.slice(0, 160)).join('\n')
      h.done(src.okOf(m.name || '', preview), preview, m.duration_ms != null ? m.duration_ms : 0, m.diff)
    }
  })
  sealTools()
  /* The last turn has no following question to close it. */
  closeTurn(0)
  closeProducts()
  /* Where the next slice of this stream picks the count up. */
  if (!lane.main) lane.agentTurn = turnNo
}

/* The composer bakes an "[attachments]" note plus "- path" bullets into the
   message; the reader gets chips instead. Parsed against both language
   variants, since history may have been written under the other one. */
export function askText(lane: Lane, text: string, when?: string | null): void {
  const notes = bridge().attNotes ? bridge().attNotes!() : []
  const s = String(text)
  for (const noteWord of notes) {
    if (!noteWord) continue
    const ix = s.lastIndexOf('\n\n' + noteWord + '\n')
    if (ix < 0) continue
    const tail = s.slice(ix + noteWord.length + 3).split('\n')
    if (!tail.length || !tail.every((l) => !l.trim() || /^- /.test(l))) continue
    ask(lane, s.slice(0, ix), tail.filter((l) => /^- /.test(l)).map((l) => l.slice(2).trim()), when)
    return
  }
  ask(lane, s, [], when)
}

/* ── the agent stage: a delegated run drawn with this same renderer ─────
   `agentDrawn` counts messages, not nodes: a poll appends what arrived since
   the last one and never touches what is on screen. */
function agentFlatCalls(ctx: { tool_calls?: unknown[]; messages?: HistoryMessage[] } | null): unknown[] {
  const calls = (ctx && ctx.tool_calls) || []
  const msgs = (ctx && ctx.messages) || []
  if (!calls.length || msgs.length > 2) return []
  if (msgs.some((m) => m && m.tool_calls && m.tool_calls.length)) return []
  return calls
}

function agentDidStep(lane: Lane, calls: unknown[]): void {
  const st = newStep(lane)
  calls.forEach((title) => {
    const parts = callParts(title)
    st.tool(parts.display ? parts.name : 'subagent_call', {}, parts.display || String(title)).done(true, '', 0)
  })
  st.seal()
}

/* The fold the history read just closed carries no clock; the record's own
   start and end are the truer span for a delegated run anyway. */
function agentFoldTime(lane: Lane, ctx: { started_at?: string; ended_at?: string }): void {
  let fold: FoldData | null = null
  lane.segs.forEach((s) => { if (s.kind === 'fold') fold = s })
  if (!fold) return
  const from = Date.parse((ctx && ctx.started_at) || '')
  const to = Date.parse((ctx && ctx.ended_at) || '')
  if (from && to && to > from) {
    ;(fold as FoldData).time = durText(to - from)
    bump(lane, fold)
  }
}

export interface AgentCtxLike {
  status?: string
  messages?: HistoryMessage[]
  tool_calls?: unknown[]
  started_at?: string
  ended_at?: string
}

/* What the reader opened on the rows a poll is about to redraw. The provisional
   rows are thrown away and drawn again from the messages, so a fold opened while
   its run was still going closed again on the next poll. Read by position and
   written back by position: the redraw walks the same messages, so an existing
   row keeps its place and new rows only follow. Only the reader's own choices
   travel -- a thought's live open/close is the step's, and is left to it. */
interface FoldToggles { open: boolean; steps: Array<{ think: boolean | null; work: boolean | null; calls: boolean[] }> }

function readToggles(lane: Lane): FoldToggles[] {
  return lane.segs.flatMap((s) => (s.kind !== 'fold' ? [] : [{
    open: s.open,
    steps: s.steps.map((st) => ({
      think: st.thinkPinned ? st.thinkOpen : null,
      work: st.wkPinned ? st.wkOpen : null,
      calls: st.calls.map((c) => c.open),
    })),
  }]))
}

function writeToggles(lane: Lane, saved: FoldToggles[]): void {
  const folds = lane.segs.filter((s): s is FoldData => s.kind === 'fold')
  saved.forEach((t, i) => {
    const f = folds[i]
    if (!f) return
    f.open = t.open
    t.steps.forEach((ts, j) => {
      const st = f.steps[j]
      if (!st) return
      if (ts.think != null) { st.thinkPinned = true; st.thinkOpen = ts.think }
      if (ts.work != null) { st.wkPinned = true; st.wkOpen = ts.work }
      ts.calls.forEach((o, k) => { const c = st.calls[k]; if (c) c.open = o })
      bump(lane, st)
    })
    bump(lane, f)
  })
}

export function agentPaintLane(lane: Lane, r: AgentCtxLike | null,
  opts?: { key?: string; empty?: string; reset?: boolean } | null): void {
  const msgs = (r && r.messages) || []
  const running = !!(r && r.status === 'run')
  const key = (opts && opts.key) || ''
  if (opts && opts.reset) lane.agentKey = null
  const fresh = lane.agentKey !== key
  if (fresh) {
    lane.segs = []
    lane.agentKey = key
    lane.agentDrawn = 0
    lane.agentHold = null
    lane.agentTurn = 0
    lane.epoch += 1
  }
  /* An answer still being written is redrawn rather than appended: it is the
     one message a later read can replace, and only an assistant message -- the
     user prompt is never rewritten and for most of a run it is the only
     message there is.

     Drawn, not withheld. It used to be skipped until the record called itself
     settled, which made the whole answer depend on a status this stage does not
     own: a node whose list row had aged out, or whose last turn was still
     flagged live, kept saying "working" forever and its answer -- written,
     complete, sitting in the record -- was never drawn at all. */
  const said = (m: HistoryMessage | undefined): boolean =>
    !!m && m.role === 'assistant' && !m.turn_ended && !m.notice && !!m.text && !!m.text.trim()
  /* Where the certain part of this snapshot ends. While the run is going, the
     last thing the model said is not yet known to be the turn's answer -- the
     next round can make it narration -- and neither is what follows it, so the
     hold starts there rather than at the last message. Snapshots do not always
     end on an assistant row: the runtime records a tool result as soon as it
     has one, before the next model round, and a slice that ends there used to
     commit the narration above it as a finished answer, which the next round's
     real answer then stood beside. Nothing said means nothing at risk. */
  let commit = msgs.length
  if (running) {
    for (let i = msgs.length - 1; i >= 0; i -= 1) {
      /* Only this turn's own words are unsettled. A pane that stays open across
         turns hands over the whole conversation each poll, so a search that ran
         past the question would pick the PREVIOUS turn's answer the moment a
         second question arrived and nothing had been said yet -- and redraw
         that answer, and the new question, under the rows already on screen. */
      const m = msgs[i] as HistoryMessage | undefined
      if (m && m.role === 'user' && m.text && m.text.trim()) break
      if (said(m)) { commit = i; break }
    }
  }
  /* And never behind what is already committed: those rows are on screen, and
     a hold that started before them would draw them a second time. */
  if (commit < lane.agentDrawn) commit = lane.agentDrawn
  const toggles = lane.agentHold ? readToggles(lane) : null
  /* Whatever the previous paint drew provisionally goes first, so the answer
     grows in place instead of stacking one copy per poll. */
  if (lane.agentHold) {
    lane.segs = lane.agentHold.segs
    lane.agentHold.folds.forEach(({ fold, steps, time }) => {
      fold.steps = steps
      fold.time = time
      bump(lane, fold)
    })
    lane.agentHold = null
  }
  const tail = msgs.slice(lane.agentDrawn, commit)
  const did = fresh && r ? agentFlatCalls(r) : []
  if (tail.length || did.length) {
    const after = msgs.slice(commit)
    if (did.length) {
      history(lane, tail.filter((m) => m && m.role === 'user'), after)
      agentDidStep(lane, did)
      history(lane, tail.filter((m) => !(m && m.role === 'user')), after)
      if (r) agentFoldTime(lane, r)
    } else {
      history(lane, tail, after)
    }
    lane.agentDrawn = commit
  }
  if (commit < msgs.length) {
    lane.agentHold = {
      segs: lane.segs.slice(),
      folds: lane.segs.flatMap((s) => (s.kind === 'fold' ? [{ fold: s, steps: s.steps.slice(), time: s.time }] : [])),
    }
    history(lane, msgs.slice(commit))
  }
  if (toggles) writeToggles(lane, toggles)
  lane.running = running
  lane.empty = (opts && opts.empty) || t('gui.ws.agents_none')
  bumpList(lane)
}

/* ── answer actions ────────────────────────────────────────────────────── */

export function copyText(text: string): void {
  if (typeof navigator !== 'undefined' && navigator.clipboard) void navigator.clipboard.writeText(text)
}

export function branchOf(lane: Lane): ((text: string) => void) | null {
  if (!lane.main) return null
  try { return source().branch || null } catch { return null }
}

export function openDagNode(runId: string, nodeId: string, summary?: string | null): void {
  try { source().openDagNode?.(runId, nodeId, summary) } catch { /* no opener wired */ }
}

export function openSpawn(agent: string, label: string): void {
  const src = source()
  if (src.openSpawn) { src.openSpawn(agent, label); return }
  shell().showWorkspace?.('agents')
}

/* Test seam. */
export function _resetForTests(): void {
  /* The delivery registry too. It was never cleared here: the unconditional
     `deliveries.reset()` at the top of `history()` happened to wipe the previous
     case's rows, so every test that painted a transcript started clean by
     accident. Scoping that reset to the conversation took the accident away and
     the leak showed up as a count that was one too high -- in a case that passes
     on its own and fails in the file. Cleared deliberately now. */
  deliveries.reset()
  lanes.clear()
  dagLive.clear()
  dagByCall.clear()
  spawnByCall.clear()
  spawnEarly.clear()
  spawnRoster = null
  dagEarly.clear()
  dagPending = null
  segId = 0
}
