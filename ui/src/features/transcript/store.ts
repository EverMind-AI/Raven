import { ds, shell, t } from '../../shell/bridge'
import { md } from '../../shell/prose'

import type {
  AnswerData, AskData, CallData, CallHandle, DagChip, DeliveredData, FoldData, HistoryMessage,
  Hunk, Lane, NoteData, NoteHandle, QaData, Seg, StatusData, StepData, StepHandle, TranscriptSource,
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

function verb_<K extends keyof Shell>(name: K): NonNullable<Shell[K]> {
  const v = shell()[name]
  if (!v) throw new Error(`RavenShell.${String(name)} is not wired`)
  return v as NonNullable<Shell[K]>
}

/* Straight to the renderer rather than out through the shell: prose.ts is a
   pure function in this same bundle, and a bridge verb would round-trip
   window.RavenShell.md -> window.md -> back into it while hiding the
   transcript from anyone auditing md()'s callers. */
export const mdHtml = (src: string): string => md(src)
export const durText = (ms: number): string => verb_('dur')(ms)

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
    case 'spawn': return String(a.label || String(a.task || '').split('\n')[0])
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
   path is the one this exists for. */
export const dagRunIdFrom = (res: unknown): string | null =>
  (/^DAG\s+(?:run\s+)?(\S+?)[:\s]/.exec(String(res || '')) || [])[1] || null

export const DOT_OF: Record<string, string> = {
  pending: '', running: 'run', completed: 'ok',
  failed: 'bad', skipped: 'skip', interrupted: 'bad',
}

/* ── lanes ─────────────────────────────────────────────────────────────── */

let segId = 0
const nextId = (): number => (segId += 1)

const lanes = new Set<Lane>()

export function newLane(key: string, main: boolean): Lane {
  const lane: Lane = {
    key, main, epoch: 0, listV: 0, scrollReq: 0, segs: [], listeners: new Set(),
    pend: '', pendStep: null, flush: null,
    agentKey: null, agentDrawn: 0, running: false, empty: '',
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
  if (dagPending && dagPending.lane === lane) dagPending = null
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

export function delivered(lane: Lane, p: { label: string; isDag: boolean; err: boolean; open: () => void }): void {
  push(lane, { v: 0, id: nextId(), kind: 'sdlv', ...p } satisfies DeliveredData)
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
  const sh = bridge()
  if (name === 'edit_file' && typeof a.old_text === 'string' && typeof a.new_text === 'string' && sh.hunkFromEdit) {
    return sh.hunkFromEdit(a.old_text, a.new_text) as Hunk
  }
  if (name === 'write_file' && typeof a.content === 'string' && sh.hunkFromWrite) {
    return sh.hunkFromWrite(a.content) as Hunk
  }
  return null
}

/* The trail's dag card is bound to its run through these: run_id arrives on
   the first dag.* event, and the newest unbound card claims it. */
let dagPending: { lane: Lane; call: CallData } | null = null
const dagLive = new Map<string, { lane: Lane; call: CallData }>()

export function dagFeed(type: string, p: { run_id?: string; node?: string; status?: string; files?: Array<{ node: string; status: string }> } | null): void {
  if (!p) return
  if (type === 'dag.run_started' && dagPending) {
    dagLive.set(String(p.run_id), dagPending)
    dagPending = null
  }
  const f = dagLive.get(String(p.run_id))
  if (!f) return
  const { lane, call } = f
  if (type === 'dag.run_started') {
    call.runId = String(p.run_id)
    call.chipsLive = true
    bump(lane, call)
  } else if (type === 'dag.node_updated') {
    setChip(lane, call, String(p.node), String(p.status))
  } else if (type === 'dag.run_completed') {
    ;(p.files || []).forEach((x) => setChip(lane, call, x.node, x.status))
    dagLive.delete(String(p.run_id))
  }
}

function setChip(lane: Lane, call: CallData, nodeId: string, st: string): void {
  const chip = call.chips.find((c) => c.id === nodeId)
  if (!chip) return
  chip.st = st || 'pending'
  bump(lane, call)
}

/* A card restored from history saw none of its run's events; recover the run
   id from the result line and read the node states off disk. */
function hydrateDag(lane: Lane, call: CallData): void {
  if (call.runId) return
  const id = dagRunIdFrom(call.res)
  if (!id) return
  call.runId = id
  call.chipsLive = true
  const rows = source().dagRows
  if (!rows) return
  rows(id).then((list) => {
    ;(list || []).forEach((r) => setChip(lane, call, r.node, r.status))
  }).catch(() => { /* a run whose dir is gone still lists its nodes */ })
}

function newCallData(id: ReturnType<typeof actId>, kind: CallData['kind'], display?: string | null): CallData {
  const a = id.args
  const c: CallData = {
    v: 0, id: nextId(), kind, name: id.name, args: a, via: id.via, srv: id.srv,
    display: display ? String(display) : '',
    label: actLabel(id.name, a, display), rowLabel: '',
    done: false, ok: true, ms: 0, res: '', truncated: false,
    hunk: kind === 'plain' ? hunkFor(id.name, a) : null,
    open: false, t0: Date.now(), runId: null, chips: [], chipsLive: false,
  }
  if (kind === 'spawn') {
    const who = a.agent ? String(a.agent) + (a.instance ? ' @' + a.instance : '') : t('gui.deleg.self')
    c.rowLabel = c.label ? `${who} · ${c.label}` : who
  }
  if (kind === 'dag') {
    const nodes = Array.isArray(a.nodes) ? (a.nodes as Array<{ id?: string; subagent?: string; instance?: string }>).filter((n) => n && n.id) : []
    const agents = [...new Set(nodes.map((n) => n.subagent).filter(Boolean))]
    c.chips = nodes.map((n): DagChip => ({
      id: String(n.id), subagent: n.subagent || null, instance: n.instance || null, st: 'pending',
    }))
    c.label = nodes.length
      ? t('gui.deleg.dag_meta', { n: String(nodes.length), m: String(agents.length || 1) }) : ''
    c.rowLabel = c.label
  }
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
  if (diff && bridge().hunkFromUnified) c.hunk = bridge().hunkFromUnified!(diff) as Hunk
  if (c.kind === 'dag') {
    if (dagPending && dagPending.call === c) dagPending = null
    hydrateDag(lane, c)
  }
  const sh = bridge()
  sh.tlPush?.({ name: c.name, arg: c.label, ms: c.ms, ok })
  sh.rawPush?.(`tool.complete  ${c.name}  ok=${ok}  ${c.ms}ms`)
  if (!ok) seg.failed = true
  poke(lane)
  bump(lane, c)
  paintWork(lane, seg, false)
}

export function newStep(lane: Lane): StepHandle {
  const seg: StepData = {
    v: 0, id: nextId(), kind: 'step',
    think: '', thinkLive: false, thinkOpen: false, thinkPinned: false, thinkShown: false,
    thinkT0: 0, thinkMs: 0, thinkSecs: null,
    say: '', sayCaret: false, hasSay: false, hasThink: false, hasQA: false, failed: false,
    calls: [], wkOpen: true, wkPinned: false, merged: false,
  }
  push(lane, seg)

  const reveal = (): void => {
    seg.thinkShown = true
    if (!seg.thinkLive) {
      seg.thinkLive = true
      seg.thinkT0 = Date.now()
      if (!seg.thinkPinned) seg.thinkOpen = true
    }
    poke(lane)
    bump(lane, seg)
  }

  const thinkDone = (secs?: number | null): void => {
    if (!seg.thinkShown) return
    if (seg.thinkLive) {
      seg.thinkLive = false
      seg.thinkMs += Date.now() - seg.thinkT0
      if (!seg.thinkPinned) seg.thinkOpen = false
    }
    const s = secs != null ? secs : Math.round(seg.thinkMs / 1000)
    if (s > 0) seg.thinkSecs = s
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
        seg.thinkT0 = Date.now()
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
    tool(name: string, args: unknown, display?: string | null): CallHandle {
      thinkDone()
      const id = actId(name || 'tool', args)
      const kind: CallData['kind'] = id.name === 'spawn' ? 'spawn' : id.name === 'run_subagent_dag' ? 'dag' : 'plain'
      const c = newCallData(id, kind, display)
      const grew = seg.calls.length === 1
      seg.calls.push(c)
      if (kind === 'dag') dagPending = { lane, call: c }
      poke(lane)
      paintWork(lane, seg, grew)
      bumpList(lane)
      return { done: (ok, res, ms, diff, truncated) => callDone(lane, seg, c, ok, res, ms, diff, truncated) }
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

/* Once the answer has landed, everything that led to it collapses behind one
   line. A turn gets ONE fold: work landing after an early fold joins it. */
export function collapse(lane: Lane, time?: string | null): void {
  const segs = lane.segs
  const loose: StepData[] = []
  let fold: FoldData | null = null
  let firstAt = -1
  for (let i = segs.length - 1; i >= 0; i -= 1) {
    const s = segs[i] as Seg
    if (s.kind === 'ask') break
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
  const f: FoldData = { v: 0, id: nextId(), kind: 'fold', time: time || null, open: false, steps: [] }
  segs.splice(firstAt, 0, f)
  loose.forEach((s) => {
    const i = segs.indexOf(s)
    if (i >= 0) segs.splice(i, 1)
    f.steps.push(s)
  })
  bumpList(lane)
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
    thinkT0: 0, thinkMs: 0, thinkSecs: null,
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
  if (st && st.seg.say.trim()) {
    const seg = st.seg
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
  collapse(lane, time)
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
  bump(lane, c)
}

export function toggleFold(lane: Lane, f: FoldData): void {
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

export function history(lane: Lane, messages: HistoryMessage[]): void {
  const src = source()
  let toolRun: StepHandle | null = null
  const sealTools = (): void => { if (toolRun) { toolRun.seal(); toolRun = null } }
  const calls = callIndex(messages)
  /* Only the LAST assistant text before the next user message is the turn's
     answer; the ones before it are the model narrating mid-turn. */
  const isFinal = messages.map((m, i) => {
    if (!(m && m.role === 'assistant' && m.text && m.text.trim())) return false
    for (let j = i + 1; j < messages.length; j += 1) {
      const n = messages[j] as HistoryMessage
      if (n && n.role === 'user' && n.text && n.text.trim()) return true
      if (n && n.role === 'assistant' && n.text && n.text.trim()) return false
    }
    return true
  })
  /* The gap between the question and the answer IS how long the turn took. */
  let turnAt = 0
  const msOf = (x: unknown): number => { const v = new Date(x as string).getTime(); return isNaN(v) ? 0 : v }
  const foldClose = (endAt: number): void => {
    collapse(lane, turnAt && endAt && endAt - turnAt >= 1000 ? durText(endAt - turnAt) : null)
  }
  messages.forEach((m, i) => {
    if (m.role === 'user' && m.text && m.text.trim()) {
      sealTools()
      turnAt = msOf(m.timestamp)
      askText(lane, m.text, stamp(m.timestamp as string))
      return
    }
    if (m.role === 'assistant' && m.notice) {
      sealTools()
      foldClose(msOf(m.timestamp))
      note(lane, t('gui.notice.' + (m.notice.kind || ''), undefined, m.notice.kind || ''),
        m.notice.detail || '', { quiet: true })
      return
    }
    if (m.role === 'assistant' && m.turn_ended) {
      sealTools()
      foldClose(msOf(m.timestamp))
      const stopped = m.turn_ended.status === 'cancelled'
      note(lane, stopped ? t('gui.halted') : t('gui.turn_died', { e: '' }).replace(/\s*[-·]\s*$/, ''),
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
        toolRun.thinkDone(m.reasoning_ms != null ? Math.round(m.reasoning_ms / 1000) : 0)
      }
      if (!text) return
      if (isFinal[i]) {
        sealTools()
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
      if (!toolRun) toolRun = newStep(lane)
      const hit = calls.get(String(m.tool_call_id || '')) || { name: '', args: null }
      const parts = callParts(hit.name || m.name || 'tool')
      const h = toolRun.tool(parts.name, hit.args || null, parts.display || null)
      const preview = src.clean(m.text).split('\n').slice(0, 8).map((l) => l.slice(0, 160)).join('\n')
      h.done(src.okOf(m.name || '', preview), preview, m.duration_ms != null ? m.duration_ms : 0, m.diff)
    }
  })
  sealTools()
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
    lane.epoch += 1
  }
  /* An answer being streamed is held back until it settles -- but only an
     assistant message; the user prompt is never rewritten and for most of a
     run it is the only message there is. */
  const last = msgs[msgs.length - 1]
  const streaming = running && last && last.role === 'assistant'
  const commit = streaming ? msgs.length - 1 : msgs.length
  const tail = msgs.slice(lane.agentDrawn, commit)
  const did = fresh && r ? agentFlatCalls(r) : []
  if (tail.length || did.length) {
    if (did.length) {
      history(lane, tail.filter((m) => m && m.role === 'user'))
      agentDidStep(lane, did)
      history(lane, tail.filter((m) => !(m && m.role === 'user')))
      if (r) agentFoldTime(lane, r)
    } else {
      history(lane, tail)
    }
    lane.agentDrawn = commit
  }
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

export function openDagNode(runId: string, nodeId: string): void {
  try { source().openDagNode?.(runId, nodeId) } catch { /* no opener wired */ }
}

export function openSpawn(agent: string, label: string): void {
  const src = source()
  if (src.openSpawn) { src.openSpawn(agent, label); return }
  shell().showWorkspace?.('agents')
}

/* Test seam. */
export function _resetForTests(): void {
  lanes.clear()
  dagLive.clear()
  dagPending = null
  segId = 0
}
