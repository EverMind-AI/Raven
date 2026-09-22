/* Renderers for one node's own transcript: a thought folded together with the
 * calls it led to, a verb-labelled tool row, a per-tool detail card, and the
 * multi-call summary row -- the parts of proto.js's `ctxView` / `stepView` /
 * `callRow` / `plainDtl` / `delegDtl` that TasksPage.tsx's `Process` and
 * `ContextTab` only wire in.
 *
 * The verb tables (`verbOf` / `verbIngOf` / `phraseOf` / `firstErrLine`) and
 * the MCP server split live in `lib/actVerbs.ts`, pure and shared with the
 * transcript island. `actLabel` stays behind the transcript's own seam
 * (`ds('transcript').actLabel`) because it also asks the workspace domain to
 * shorten a path -- a store dependency this module does not carry.
 */

import { Fragment, useSyncExternalStore } from 'react'

import { Glyph } from '../../components/Ico'
import { t } from '../../i18n/t'
import { argPath, firstErrLine, phraseOf, splitMcp, verbIngOf, verbOf } from '../../lib/actVerbs'
import { copy } from '../../lib/clipboard'
import { formatDuration } from '../../lib/duration'
import { fromEdit, fromWrite } from '../../lib/hunks'
import { md } from '../../lib/prose'
import { useTick } from '../../lib/tick'
import { ds } from '../../state/sources'
import { parseDagReceipt, parseSpawnReceipt } from './nestedRun'
import * as store from './store'

import type { NodeStep, TaskRow } from './types'
import type { ReactNode } from 'react'
import type { JSX } from 'react'

/* ── icons ─────────────────────────────────────────────────────────────── */

/* The tool-row glyphs, the same paths TranscriptPage.tsx draws its own call
   rows with (that file's own `ACT_ICO`) -- kept as a private copy here rather
   than an import, since that table is a component-local one with no public
   seam of its own (CONTRIBUTING 2.2: a sibling reaches a domain's source.ts
   or types.ts, never a component). */
const ACT_ICO: Record<string, string> = {
  doc: 'M7 3.5h7L18.5 8v10.5a2 2 0 0 1-2 2h-9a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2ZM13.5 3.5V8h4.5',
  folder: 'M4 7.5c0-1.1.9-2 2-2h3.5l2 2.5H18c1.1 0 2 .9 2 2v7c0 1.1-.9 2-2 2H6c-1.1 0-2-.9-2-2v-9.5Z',
  term: 'M3.5 5h17v14h-17zM7.5 10l2.5 2-2.5 2M12.5 14.5H16',
  globe: 'M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17ZM3.5 12h17M12 3.5c-4 4.3-4 12.7 0 17'
    + 'M12 3.5c4 4.3 4 12.7 0 17',
  find: 'M10.5 4a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13ZM15.4 15.4 20 20',
  pen: 'M4.5 19.5h4L19 9a2.12 2.12 0 0 0-3-3L5.5 16.5v3ZM15.5 6.5l2 2',
  star: 'M12 4l1.9 5.3L19 11l-5.1 1.7L12 18l-1.9-5.3L5 11l5.1-1.7Z',
  chat: 'M4.5 6.5a2 2 0 0 1 2-2h11a2 2 0 0 1 2 2v6.5a2 2 0 0 1-2 2H10l-4 3.5V15H6.5a2 2 0 0 1-2-2Z',
  clock: 'M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17ZM12 7.5V12l3.4 2',
  image: 'M4.5 5.5h15v13h-15zM8.6 11.2a1.55 1.55 0 1 0 0-3.1 1.55 1.55 0 0 0 0 3.1ZM6 17.5l4.5-4.5 3.5 3.5 2.5-2.5 2.5 2.5',
  sound: 'M5 10h3l4-3.5v11L8 14H5ZM15.5 9.5a4 4 0 0 1 0 5',
  video: 'M4.5 6.5h10v11h-10zM14.5 11l5-3v8l-5-3',
  ask: 'M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17ZM9.8 9.6a2.2 2.2 0 1 1 3.4 1.9c-.8.5-1.2 1-1.2 2M12 16.6h.01',
  bad: 'M12 4.5 20.5 19H3.5L12 4.5ZM12 10v3.6M12 16.4h.01',
  dot: 'M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17ZM8.5 12h7',
  chev: 'M9.5 6.5 15 12l-5.5 5.5',
  dag: 'M7.4 12a2.2 2.2 0 1 1-4.4 0 2.2 2.2 0 0 1 4.4 0ZM21 6a2.2 2.2 0 1 1-4.4 0 2.2 2.2 0 0 1 4.4 0Z'
    + 'M21 18a2.2 2.2 0 1 1-4.4 0 2.2 2.2 0 0 1 4.4 0ZM7.3 11l9.4-4M7.3 13l9.4 4',
}

function actIco(name: string): string {
  switch (name) {
    case 'read_file': case 'read_skill': case 'understand_media': case 'deliver_files': return ACT_ICO.doc as string
    case 'write_file': case 'edit_file': return ACT_ICO.pen as string
    case 'list_dir': return ACT_ICO.folder as string
    case 'grep': case 'find': case 'tool_search': return ACT_ICO.find as string
    case 'exec': return ACT_ICO.term as string
    case 'web_search': case 'web_fetch': case 'deep_research': return ACT_ICO.globe as string
    case 'image_generate': return ACT_ICO.image as string
    case 'video_generate': return ACT_ICO.video as string
    case 'text_to_speech': return ACT_ICO.sound as string
    case 'message': return ACT_ICO.chat as string
    case 'cron': return ACT_ICO.clock as string
    case 'spawn': case 'use_skill': return ACT_ICO.star as string
    case 'run_subagent_dag': case 'load_playbook': return ACT_ICO.dag as string
    case 'ask_user': return ACT_ICO.ask as string
    default: return ACT_ICO.dot as string
  }
}

/* ── copy button ───────────────────────────────────────────────────────── */

function CopyIcon(): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true">
      <rect x="8" y="8" width="12" height="12" rx="2" /><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
    </svg>
  )
}

/* ── args / diff ───────────────────────────────────────────────────────── */

function parseArgs(raw: string): Record<string, unknown> {
  try {
    const v: unknown = JSON.parse(raw)
    return v && typeof v === 'object' && !Array.isArray(v) ? (v as Record<string, unknown>) : {}
  } catch {
    return {}
  }
}

/* A write/edit call's own +N/-M, the same approximation the file chip's own
   patch preview draws from (diffs.ts's `hunksForFile`): a write is read as
   "all added", an edit as the real before/after slice. The tool's own result
   never carries a diff (contract G1), so this is the only source there is. */
function hunkOfCall(name: string, args: Record<string, unknown>): { add: number; del: number } | null {
  if (name === 'write_file' && typeof args.content === 'string') return fromWrite(args.content)
  if (name === 'edit_file' && typeof args.old_text === 'string' && typeof args.new_text === 'string') {
    return fromEdit(args.old_text, args.new_text)
  }
  return null
}

/* ── grouping: a thought, what it said, and the calls it led to ──────────
   proto.js's `toSteps`, with one more boundary: a new group opens on a
   thought that arrives after the current one already has calls or something
   said, so a run of "think, call, call, think, call" reads as two steps
   rather than five flat rows -- and on narration that arrives after calls,
   so a run whose model returns no reasoning text (a lane that bills thinking
   but does not hand it back) still reads as one step per thing it said
   before calling, rather than one fold with every call in it and the
   narration piled above them. Narration after a thought stays in that
   thought's step: "think, say, call" is one step, as it was. */

type ToolCall = Extract<NodeStep, { kind: 'tool' }>
interface Group { key: number; kind: 'group'; think: string | null; say: string | null; calls: ToolCall[] }
interface ConsoleItem { key: number; kind: 'console'; text: string }
type GroupOrConsole = Group | ConsoleItem

export function groupSteps(steps: NodeStep[]): GroupOrConsole[] {
  const out: GroupOrConsole[] = []
  let cur: Group | null = null
  let key = 0
  steps.forEach((s) => {
    if (s.kind === 'console') { out.push({ key: key++, kind: 'console', text: s.text }); cur = null; return }
    if (s.kind === 'think' && cur && (cur.calls.length || cur.say)) cur = null
    if (s.kind === 'say' && cur && cur.calls.length) cur = null
    if (!cur) { cur = { key: key++, kind: 'group', think: null, say: null, calls: [] }; out.push(cur) }
    if (s.kind === 'think') cur.think = cur.think ? `${cur.think}\n${s.text}` : s.text
    else if (s.kind === 'say') cur.say = cur.say ? `${cur.say}\n\n${s.text}` : s.text
    else cur.calls.push(s)
  })
  return out
}

/* ── the answer / mid-run say, as markdown ────────────────────────────────
   `.prose` is the page's own shared markdown vocabulary (already read by the
   transcript, the workspace and the knowledge base -- CONTRIBUTING 7 / this
   sheet's `SHARED`): a node's answer is a document like any other, and this
   is one rule rather than a fourth copy of it. */

/* Trails a running node's own still-open answer with the prototype's own
   streaming caret (proto.css's `.caret`, tk-prefixed here since the
   page-wide name is not this domain's to borrow -- CONTRIBUTING 7): the same
   plain string-append `TranscriptPage.tsx`'s own `AnswerView` uses for the
   main chat's streaming reply, rather than an imperative DOM write. */
const withCaret = (html: string): string => `${html}<span class="tkcaret" aria-hidden="true"></span>`

function Prose({ text, cls, caret = false }: { text: string; cls: string; caret?: boolean }): JSX.Element {
  const html = md(text)
  return <div className={cls + ' prose'} dangerouslySetInnerHTML={{ __html: caret ? withCaret(html) : html }} />
}

/* `running`: this node has no token stream of its own, but `useNodeRecord`
   re-reads the record on every live `node_updated` event, so a trailing
   answer that arrives while the node is still `running` is not necessarily
   the final one -- the caret says so the way the main chat's does, rather
   than a second "in progress" sentence. */
export function Answer({ text, at, running = false }: { text: string; at: number | null; running?: boolean }): JSX.Element {
  return (
    <div className="tkanswer">
      <Prose text={text} cls="tkans" caret={running} />
      <div className="tkansfoot">
        <button
          className="tkfootcopy" aria-label={t('gui.tasks.copy')}
          onClick={() => copy(text, t('gui.tasks.copied_answer'))}
        >
          <CopyIcon />
        </button>
        {at != null ? <span className="tkturnmeta">{hhmm(at)}</span> : null}
      </div>
    </div>
  )
}

function hhmm(ms: number): string {
  const d = new Date(ms)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/* ── the thought ───────────────────────────────────────────────────────── */

function ThinkBlock({ text, live, nodeKey, foldKey }: {
  text: string; live: boolean; nodeKey: string; foldKey: string
}): JSX.Element {
  const touched = useSyncExternalStore(store.subscribe, () => store.foldOf(nodeKey, foldKey))
  const open = touched ?? true
  return (
    <div className={'tkthink' + (live ? ' live' : '')}>
      <button className="tkthh" aria-expanded={open} onClick={() => store.setFold(nodeKey, foldKey, !open)}>
        <span className="tkthlb">{t(live ? 'gui.tasks.rec_thinking' : 'gui.tasks.rec_thought')}</span>
      </button>
      {open ? <blockquote>{text}</blockquote> : null}
    </div>
  )
}

/* ── one call's own detail card ───────────────────────────────────────── */

function DtlHead({ name, hunk, copyText }: {
  name: string; hunk: { add: number; del: number } | null; copyText: string
}): JSX.Element {
  return (
    <div className="tkdtlhd">
      <span className="tkdtlnm">{name}</span>
      {hunk && (hunk.add || hunk.del)
        ? (
          <span className="tkdstat">
            <b className="add">{`+${hunk.add}`}</b> <b className="del">{`\u2212${hunk.del}`}</b>
          </span>
        )
        : null}
      <button
        type="button" className="tkdtlcp" aria-label={t('gui.tasks.copy')}
        onClick={() => copy(copyText, t('gui.tasks.copied'))}
      >
        <CopyIcon />
      </button>
    </div>
  )
}

function PlainDtl({ call, bare, args, label }: {
  call: ToolCall; bare: string; args: Record<string, unknown>; label: string
}): JSX.Element {
  const text = call.result == null ? '' : String(call.result)
  const hunk = hunkOfCall(bare, args)
  let head: string
  let copyText: string
  const body: JSX.Element[] = []
  if (bare === 'write_file' || bare === 'edit_file') {
    head = label || t('gui.dtl.plain')
    /* The head is elided (`label` is `shortPath`'s own truncation); a reader
       who copies a path wants the whole thing, not a leading ellipsis. */
    copyText = argPath(args) || label
    if (text.trim()) body.push(<pre key="o">{text.replace(/\s+$/, '')}</pre>)
  } else if (bare === 'exec') {
    const cmd = String(args.command || '')
    head = cmd || t('gui.dtl.output')
    copyText = cmd || text
    const lines = text.replace(/\s+$/, '').split('\n')
    const m = /^Exit code:\s*(-?\d+)$/.exec(lines[lines.length - 1] || '')
    let exit: number | null = null
    if (m) { exit = Number(m[1]); lines.pop() }
    if (cmd) body.push(<div className="tkcmd" key="cmd">{cmd}</div>)
    if (lines.join('\n').trim()) body.push(<pre key="o">{lines.join('\n')}</pre>)
    if (exit != null) {
      body.push(
        <div className="tkexitrow" key="exit">
          <span className={'tkexit ' + (exit === 0 ? 'ok' : 'bad')}>{t('gui.tasks.exit_code', { n: exit })}</span>
        </div>,
      )
    }
  } else if (bare === 'web_fetch') {
    const url = String(args.url || '')
    head = url || t('gui.dtl.plain')
    copyText = url || text
    if (text.trim()) body.push(<pre key="o">{text.replace(/\s+$/, '')}</pre>)
  } else {
    head = label || t('gui.dtl.plain')
    copyText = label || text
    if (text.trim()) body.push(<pre key="o">{text.replace(/\s+$/, '')}</pre>)
  }
  return (
    <div className="tkdtl">
      <DtlHead name={head} hunk={hunk} copyText={copyText} />
      {body.length ? <div className="tkdtlbd">{body}</div> : null}
    </div>
  )
}

/* The three-way state a nested run settles into -- the same colours
   TasksPage.tsx's own `tdotState` reads off a `TaskRow`: moss while
   running, clay only for a failure or an interruption, faint for
   everything else settled (completed and cancelled alike). */
function rowState(status: TaskRow['status']): 'run' | 'ok' | 'bad' {
  return status === 'running' ? 'run' : (status === 'failed' || status === 'interrupted') ? 'bad' : 'ok'
}

/* Node count, agent count, then the agent names themselves -- proto.js's own
   three-part join, built from the run's real nodes rather than guessed at. */
function dagScale(row: TaskRow): string {
  const agents = [...new Set(row.nodes.map((n) => n.agent).filter(Boolean))]
  const meta = t('gui.deleg.dag_meta', { n: row.nodes.length, m: agents.length })
  return agents.length ? `${meta} · ${agents.join(' · ')}` : meta
}

/* The delegated call's own key/value grid -- proto.js's `delegDtl`. The
   receipt a spawn / dag call's own result returns the instant its dispatch
   is accepted names the run (`nestedRun.ts`'s two parsers); state, elapsed
   time and scale resolve against that run's own row in the tasks store,
   which carries the facts the receipt cannot -- how it is actually going,
   not just that it was accepted. A run outside this conversation's list
   (nothing here has read it, or it belongs to another session entirely)
   falls back to what the receipt text alone gives: an id, and no control to
   open it. */
function DelegDtl({ call, kind, name, label, done, bad }: {
  call: ToolCall; kind: 'deleg' | 'dag'; name: string; label: string; done: boolean; bad: boolean
}): JSX.Element {
  useSyncExternalStore(store.subscribe, store.get)
  const args = parseArgs(call.args)
  const result = call.result ?? ''
  const spawnReceipt = kind === 'deleg' && done ? parseSpawnReceipt(result) : null
  const dagReceipt = kind === 'dag' && done ? parseDagReceipt(result) : null
  /* The spawn tool requires the model to name the run's own record id
     (`node_id`); the receipt's own "(id: ...)" is the manager's internal
     handle instead and only doubles as the record id when the model left
     `node_id` out (`manager.py`'s own `node_id or task_id` fallback). */
  const nodeId = (typeof args.node_id === 'string' && args.node_id) || spawnReceipt?.taskId || ''
  const runId = dagReceipt?.runId || ''
  const row: TaskRow | null = kind === 'deleg'
    ? (nodeId ? store.byKey('spawn', nodeId) : null)
    : (runId ? store.byKey('dag', runId) : null)
  const running = row?.status === 'running'
  const elapsed = useTick(running, row?.started_at ?? 0)
  const cost = row
    ? row.status === 'running'
      ? (row.started_at != null ? formatDuration(elapsed) : '')
      : (row.started_at != null && row.ended_at != null ? formatDuration(row.ended_at - row.started_at) : '')
    : ''

  const rows: Array<{ k: string; v: ReactNode; onOpen?: () => void }> = []
  if (kind === 'deleg') {
    /* `openByNode` is the same seam the transcript's own spawn row already
       opens a run through; gated on the store holding the row, not merely
       on the id, so a run outside this conversation's list reads as plain
       text rather than a link that silently does nothing on click. */
    rows.push({
      k: t('gui.deleg.d_task'), v: label,
      onOpen: row ? () => { ds('tasks').openByNode?.(nodeId) } : undefined,
    })
    rows.push({ k: t('gui.deleg.d_agent'), v: String(args.agent || '') || t('gui.deleg.self') })
  } else {
    const runTitle = row?.task_summary || null
    if (runTitle) {
      rows.push({ k: t('gui.deleg.d_task'), v: runTitle, onOpen: () => { ds('tasks').openRun?.(runId) } })
    }
    rows.push({ k: t('gui.deleg.d_scale'), v: row ? dagScale(row) : (label || t('gui.tasks.deleg_a_graph')) })
    if (name === 'load_playbook' && label && label !== runTitle) rows.push({ k: t('gui.dag.playbook'), v: label })
  }
  const state = row ? rowState(row.status) : (!done ? 'run' : bad ? 'bad' : 'ok')
  const stateWord = state === 'run' ? t('gui.deleg.st_run') : state === 'ok' ? t('gui.deleg.st_ok') : t('gui.deleg.st_bad')
  const errSrc = row ? row.nodes.find((n) => n.error)?.error ?? null : call.result
  const err = state === 'bad' ? firstErrLine(errSrc) : ''
  rows.push({
    k: t('gui.deleg.d_state'),
    v: (
      <span className="tkstt">
        <span className={'dot ' + state} />
        {err ? `${stateWord} · ${err}` : stateWord}
      </span>
    ),
  })
  if (cost) rows.push({ k: t('gui.deleg.d_cost'), v: cost })
  if (kind === 'dag' && runId) rows.push({ k: t('gui.dag.run_id'), v: runId })
  return (
    <div className="tkdtl tkdlg">
      <div className="tkdtlbd">
        <div className="tkdgr">
          {rows.map((r, i) => (
            <Fragment key={i}>
              <div className="tkdgk">{r.k}</div>
              {r.onOpen
                ? (
                  <div
                    className="tkdgv tkgov" role="button" tabIndex={0} title={t('gui.deleg.open_hint')}
                    onClick={r.onOpen}
                    onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); r.onOpen!() } }}
                  >
                    {r.v}
                  </div>
                )
                : <div className="tkdgv">{r.v}</div>}
            </Fragment>
          ))}
        </div>
      </div>
    </div>
  )
}

/* ── one call's own row ───────────────────────────────────────────────── */

function CallRow({ call, nodeKey, foldKey, running }: {
  call: ToolCall; nodeKey: string; foldKey: string; running: boolean
}): JSX.Element {
  const { srv, bare } = splitMcp(call.name)
  const done = call.result != null
  /* A call with no result is in flight only while the node is. Settled, the
     label says only what the record knows -- no result came back -- because
     one shape covers two facts the wire cannot tell apart: an acp `tool_call`
     frame means the call was initiated (`status: in_progress`) and may have
     finished with side effects before the cancel landed, while the in-process
     lane advertises a round's calls before it reaches them. */
  const busy = !done && running
  const noResult = !done && !running
  const bad = call.ok === false
  const kind: 'deleg' | 'dag' | 'plain' = bare === 'spawn'
    ? 'deleg'
    : (bare === 'run_subagent_dag' || bare === 'load_playbook') ? 'dag' : 'plain'
  const args = parseArgs(call.args)
  const label = ds('transcript').actLabel?.(bare, args) ?? bare.split('_').join(' ')
  const touched = useSyncExternalStore(store.subscribe, () => store.foldOf(nodeKey, foldKey))
  const open = touched ?? false
  const hunk = kind === 'plain' ? hunkOfCall(bare, args) : null
  const withDtl = done || (kind !== 'plain' && !noResult)
  const inner = (
    <>
      <Glyph d={bad ? ACT_ICO.bad as string : actIco(bare)} cls="tkic" />
      <span className="tkvb">
        {srv ? <span className="tksrv">{`[${srv}] `}</span> : null}
        {busy ? verbIngOf(bare) : verbOf(bare)}
      </span>
      {kind !== 'plain' ? <span className="tkar">{label}</span> : null}
      {/* Only once the call has returned: the counts come off its own
         arguments (hunkOfCall), which exist the moment it is issued, so an
         in-flight write would otherwise advertise a result it has not
         produced yet beside a still-breathing icon. */}
      {kind === 'plain' && done && hunk && (hunk.add || hunk.del)
        ? (
          <span className="tkdstat">
            <b className="add">{`+${hunk.add}`}</b> <b className="del">{`\u2212${hunk.del}`}</b>
          </span>
        )
        : null}
      {kind === 'plain' && bad ? <span className="err">{firstErrLine(call.result)}</span> : null}
      {noResult ? <span className="tknoresultchip">{t('gui.tasks.call_no_result')}</span> : null}
      {withDtl ? <Glyph d={ACT_ICO.chev as string} cls="tkcv" /> : null}
    </>
  )
  return (
    <div className={'tkwrow' + (busy ? ' tkbusy' : '') + (noResult ? ' tknoresult' : '') + (bad ? ' tkbad' : '') + (withDtl ? ' tktog' : '') + (withDtl && open ? ' tkopen' : '')}>
      {withDtl
        ? <button type="button" className="tkwhd" aria-expanded={open} onClick={() => store.setFold(nodeKey, foldKey, !open)}>{inner}</button>
        : <div className="tkwhd">{inner}</div>}
      {withDtl && open
        ? (kind === 'plain'
          ? <PlainDtl call={call} bare={bare} args={args} label={label} />
          : <DelegDtl call={call} kind={kind} name={bare} label={label} done={done} bad={bad} />)
        : null}
    </div>
  )
}

/* ── a step's calls: one row, or a folded multi-call summary ─────────────
   proto.js's `stepView`: more than one call folds behind a summary naming
   the kind of work and how many failed; a single call is just its own row,
   always open. */

function CallsBlock({ calls, nodeKey, foldKey, running }: {
  calls: ToolCall[]; nodeKey: string; foldKey: string; running: boolean
}): JSX.Element {
  const many = calls.length > 1
  const touched = useSyncExternalStore(store.subscribe, () => store.foldOf(nodeKey, foldKey))
  const open = touched ?? false
  const bad = calls.filter((c) => c.ok === false).length
  /* Any call in the fold still out -- the same reason a single un-returned
     call's own row breathes, carried onto the summary that stands in for
     every row folded behind it. Once the node has settled, the same calls
     are the ones with no result, and the summary counts them. */
  const unanswered = calls.filter((c) => c.result == null).length
  const flying = running && unanswered > 0
  const noResult = running ? 0 : unanswered
  const bareNames = calls.map((c) => splitMcp(c.name).bare)
  const oneKind = new Set(bareNames).size === 1
  return (
    <div className="tkwk">
      {many
        ? (
          <button
            type="button" className={'tkwrow tktog tksum' + (flying ? ' tkbusy' : '') + (open ? ' tkopen' : '')}
            aria-expanded={open} onClick={() => store.setFold(nodeKey, foldKey, !open)}
          >
            <Glyph d={oneKind ? actIco(bareNames[0] as string) : ACT_ICO.dot as string} cls="tkic" />
            <span className="tkar">{phraseOf(bareNames.map((name) => ({ name })))}</span>
            {bad ? <span className="tkbadchip">{t('gui.tasks.call_failed_n', { n: bad })}</span> : null}
            {noResult ? <span className="tknoresultchip">{t('gui.tasks.call_no_result_n', { n: noResult })}</span> : null}
            <Glyph d={ACT_ICO.chev as string} cls="tkcv" />
          </button>
        )
        : null}
      <div className="tkwkin" hidden={many && !open}>
        {calls.map((c, i) => (
          <CallRow call={c} key={c.id || i} nodeKey={nodeKey} foldKey={`${foldKey}:${i}`} running={running} />
        ))}
      </div>
    </div>
  )
}

/* ── one whole step ────────────────────────────────────────────────────── */

function StepGroupView({ group, live, nodeKey }: { group: Group; live: boolean; nodeKey: string }): JSX.Element {
  return (
    <div className="tkstep">
      {group.think
        ? (
          <ThinkBlock
            text={group.think} live={live && !group.calls.length && !group.say}
            nodeKey={nodeKey} foldKey={`think:${group.key}`}
          />
        )
        : null}
      {group.say ? <Prose text={group.say} cls="tkans" /> : null}
      {group.calls.length
        ? <CallsBlock calls={group.calls} nodeKey={nodeKey} foldKey={`wk:${group.key}`} running={live} />
        : null}
    </div>
  )
}

/* The process fold's own body: every step, grouped, plus the cli channel's
   synthetic stand-in rendered on its own (it carries no thought and no
   calls, just the one fixed sentence). */
export function StepList({ steps, running, nodeKey = '' }: {
  steps: NodeStep[]; running: boolean; nodeKey?: string
}): JSX.Element {
  return (
    <>
      {groupSteps(steps).map((g) => (g.kind === 'console'
        ? <div className="tkconsole" key={g.key}>{t('gui.tasks.ctx_console')}</div>
        : <StepGroupView group={g} live={running} nodeKey={nodeKey} key={g.key} />))}
    </>
  )
}
