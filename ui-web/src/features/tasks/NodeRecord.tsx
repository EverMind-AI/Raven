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

import { Fragment, useState } from 'react'

import { Glyph } from '../../components/Ico'
import { t } from '../../i18n/t'
import { firstErrLine, phraseOf, splitMcp, verbIngOf, verbOf } from '../../lib/actVerbs'
import { copy } from '../../lib/clipboard'
import { fromEdit, fromWrite } from '../../lib/hunks'
import { md } from '../../lib/prose'
import { ds } from '../../state/sources'

import type { NodeStep } from './types'
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
    case 'read_file': case 'read_skill': case 'understand_media': return ACT_ICO.doc as string
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
   Mirrors proto.js's `toSteps`: a new group opens on a thought that arrives
   after the current one already has calls or something said, so a run of
   "think, call, call, think, call" reads as two steps rather than five flat
   rows. */

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

function Prose({ text, cls }: { text: string; cls: string }): JSX.Element {
  return <div className={cls + ' prose'} dangerouslySetInnerHTML={{ __html: md(text) }} />
}

export function Answer({ text, at }: { text: string; at: number | null }): JSX.Element {
  return (
    <>
      <Prose text={text} cls="tkans" />
      <div className="tkansfoot">
        <button
          className="tkfootcopy" aria-label={t('gui.tasks.copy')}
          onClick={() => copy(text, t('gui.tasks.copied_answer'))}
        >
          <CopyIcon />
        </button>
        {at != null ? <span className="tkturnmeta">{hhmm(at)}</span> : null}
      </div>
    </>
  )
}

function hhmm(ms: number): string {
  const d = new Date(ms)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}

/* ── the thought ───────────────────────────────────────────────────────── */

function ThinkBlock({ text, live }: { text: string; live: boolean }): JSX.Element {
  const [open, setOpen] = useState(true)
  return (
    <div className="tkthink">
      <button className="tkthh" aria-expanded={open} onClick={() => setOpen(!open)}>
        {t(live ? 'gui.tasks.rec_thinking' : 'gui.tasks.rec_thought')}
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
    copyText = label
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

/* The delegated call's own key/value grid -- proto.js's `delegDtl`. The task,
   target-agent, scale and state rows (`d_task` / `d_agent` / `d_scale` /
   `d_state`) are producible from the call's own arguments and result; the
   elapsed time, the run id and the "open this run" link are not -- a node's
   own transcript carries no nested-run facts (contract: the desk tasks RPC
   design doc, section 2.6), so those three stay a data gap rather than a
   guess. */
function DelegDtl({ call, kind, label, done, bad }: {
  call: ToolCall; kind: 'deleg' | 'dag'; label: string; done: boolean; bad: boolean
}): JSX.Element {
  const args = parseArgs(call.args)
  const rows: Array<{ k: string; v: ReactNode }> = []
  if (kind === 'deleg') {
    rows.push({ k: t('gui.deleg.d_task'), v: label })
    rows.push({ k: t('gui.deleg.d_agent'), v: String(args.agent || '') || t('gui.deleg.self') })
  } else {
    /* No task row here: the prototype only shows one when the card has its
       own `runTitle` (the graph's dispatched-for line, read from `dag.get`),
       and a node's own transcript carries no such read (data gap) -- `label`
       is `actLabel`'s own guess at the call's arguments, which is what the
       scale row falls back to instead. */
    rows.push({ k: t('gui.deleg.d_scale'), v: label || t('gui.tasks.deleg_a_graph') })
  }
  const state = !done ? 'run' : bad ? 'bad' : 'ok'
  const stateWord = state === 'run' ? t('gui.deleg.st_run') : state === 'ok' ? t('gui.deleg.st_ok') : t('gui.deleg.st_bad')
  const err = state === 'bad' ? firstErrLine(call.result) : ''
  rows.push({
    k: t('gui.deleg.d_state'),
    v: (
      <span className="tkstt">
        <span className={'dot ' + state} />
        {err ? `${stateWord} · ${err}` : stateWord}
      </span>
    ),
  })
  return (
    <div className="tkdtl tkdlg">
      <div className="tkdtlbd">
        <div className="tkdgr">
          {rows.map((r, i) => (
            <Fragment key={i}>
              <div className="tkdgk">{r.k}</div>
              <div className="tkdgv">{r.v}</div>
            </Fragment>
          ))}
        </div>
      </div>
    </div>
  )
}

/* ── one call's own row ───────────────────────────────────────────────── */

function CallRow({ call }: { call: ToolCall }): JSX.Element {
  const { srv, bare } = splitMcp(call.name)
  const done = call.result != null
  const bad = call.ok === false
  const kind: 'deleg' | 'dag' | 'plain' = bare === 'spawn'
    ? 'deleg'
    : (bare === 'run_subagent_dag' || bare === 'load_playbook') ? 'dag' : 'plain'
  const args = parseArgs(call.args)
  const label = ds('transcript').actLabel?.(bare, args) ?? bare.split('_').join(' ')
  const [open, setOpen] = useState(false)
  const hunk = kind === 'plain' ? hunkOfCall(bare, args) : null
  const withDtl = kind !== 'plain' || done
  const inner = (
    <>
      <Glyph d={bad ? ACT_ICO.bad as string : actIco(bare)} cls="tkic" />
      <span className="tkvb">
        {srv ? <span className="tksrv">{`[${srv}] `}</span> : null}
        {done ? verbOf(bare) : verbIngOf(bare)}
      </span>
      {kind !== 'plain' ? <span className="tkar">{label}</span> : null}
      {kind === 'plain' && hunk && (hunk.add || hunk.del)
        ? (
          <span className="tkdstat">
            <b className="add">{`+${hunk.add}`}</b> <b className="del">{`\u2212${hunk.del}`}</b>
          </span>
        )
        : null}
      {kind === 'plain' && bad ? <span className="err">{firstErrLine(call.result)}</span> : null}
      {withDtl ? <Glyph d={ACT_ICO.chev as string} cls="tkcv" /> : null}
    </>
  )
  return (
    <div className={'tkwrow' + (!done ? ' tkrun' : '') + (bad ? ' tkbad' : '') + (withDtl ? ' tktog' : '') + (withDtl && open ? ' tkopen' : '')}>
      {withDtl
        ? <button type="button" aria-expanded={open} onClick={() => setOpen(!open)}>{inner}</button>
        : <div>{inner}</div>}
      {withDtl && open
        ? (kind === 'plain'
          ? <PlainDtl call={call} bare={bare} args={args} label={label} />
          : <DelegDtl call={call} kind={kind} label={label} done={done} bad={bad} />)
        : null}
    </div>
  )
}

/* ── a step's calls: one row, or a folded multi-call summary ─────────────
   proto.js's `stepView`: more than one call folds behind a summary naming
   the kind of work and how many failed; a single call is just its own row,
   always open. */

function CallsBlock({ calls }: { calls: ToolCall[] }): JSX.Element {
  const many = calls.length > 1
  const [open, setOpen] = useState(false)
  const bad = calls.filter((c) => c.ok === false).length
  const bareNames = calls.map((c) => splitMcp(c.name).bare)
  const oneKind = new Set(bareNames).size === 1
  return (
    <div className="tkwk">
      {many
        ? (
          <button
            type="button" className={'tkwrow tktog tksum' + (open ? ' tkopen' : '')}
            aria-expanded={open} onClick={() => setOpen(!open)}
          >
            <Glyph d={oneKind ? actIco(bareNames[0] as string) : ACT_ICO.dot as string} cls="tkic" />
            <span className="tkar">{phraseOf(bareNames.map((name) => ({ name })))}</span>
            {bad ? <span className="tkbadchip">{t('gui.tasks.call_failed_n', { n: bad })}</span> : null}
            <Glyph d={ACT_ICO.chev as string} cls="tkcv" />
          </button>
        )
        : null}
      <div className="tkwkin" hidden={many && !open}>
        {calls.map((c, i) => <CallRow call={c} key={c.id || i} />)}
      </div>
    </div>
  )
}

/* ── one whole step ────────────────────────────────────────────────────── */

function StepGroupView({ group, live }: { group: Group; live: boolean }): JSX.Element {
  return (
    <div className="tkstep">
      {group.think ? <ThinkBlock text={group.think} live={live && !group.calls.length && !group.say} /> : null}
      {group.say ? <Prose text={group.say} cls="tkans" /> : null}
      {group.calls.length ? <CallsBlock calls={group.calls} /> : null}
    </div>
  )
}

/* The process fold's own body: every step, grouped, plus the cli channel's
   synthetic stand-in rendered on its own (it carries no thought and no
   calls, just the one fixed sentence). */
export function StepList({ steps, running }: { steps: NodeStep[]; running: boolean }): JSX.Element {
  return (
    <>
      {groupSteps(steps).map((g) => (g.kind === 'console'
        ? <div className="tkconsole" key={g.key}>{t('gui.tasks.ctx_console')}</div>
        : <StepGroupView group={g} live={running} key={g.key} />))}
    </>
  )
}
