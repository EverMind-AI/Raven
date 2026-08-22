import { Fragment, memo, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { flushSync } from 'react-dom'

import * as dag from '../dag/graph'
import { shell, t } from '../../shell/bridge'
import { open as openChip } from '../../shell/chips'
import {
  RENDERED as WS_RENDERED, fileKind, fileURL, openPath as wsOpenPath,
} from '../workspace/store'
import * as store from './store'

import type { DagNode } from '../dag/types'
import type {
  AnswerData, ArtifactRow, ArtsData, AskData, CallData, DeliveredData, FoldData, Lane,
  NoteData, QaData, Seg, StatusData, StepData,
} from './types'
import type { KeyboardEvent, ReactElement, ReactNode } from 'react'
import * as lightbox from '../../shell/lightbox'

/* The transcript renderer: three voices, three folding depths. Machine work
 * renders as quiet activity rows, never cards; a stretch of consecutive
 * calls is one work segment. Class names and DOM shape are the legacy
 * renderer's, frozen -- page.css styles both without knowing which drew it.
 */

/* ── shared pieces ─────────────────────────────────────────────────────── */

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
  check: 'M5 12.5l4.5 4.5L19 7',
  dag: 'M7.4 12a2.2 2.2 0 1 1-4.4 0 2.2 2.2 0 0 1 4.4 0ZM21 6a2.2 2.2 0 1 1-4.4 0 2.2 2.2 0 0 1 4.4 0Z'
    + 'M21 18a2.2 2.2 0 1 1-4.4 0 2.2 2.2 0 0 1 4.4 0ZM7.3 11l9.4-4M7.3 13l9.4 4',
}

export function actIco(name: string): string {
  switch (name) {
    case 'read_file': case 'read_skill': return ACT_ICO.doc as string
    case 'write_file': case 'edit_file': return ACT_ICO.pen as string
    case 'list_dir': return ACT_ICO.folder as string
    case 'grep': case 'find': case 'tool_search': return ACT_ICO.find as string
    case 'exec': return ACT_ICO.term as string
    case 'web_search': case 'web_fetch': case 'deep_research': return ACT_ICO.globe as string
    case 'understand_media': return ACT_ICO.doc as string
    case 'image_generate': return ACT_ICO.image as string
    case 'video_generate': return ACT_ICO.video as string
    case 'text_to_speech': return ACT_ICO.sound as string
    case 'message': return ACT_ICO.chat as string
    case 'cron': return ACT_ICO.clock as string
    case 'spawn': case 'use_skill': return ACT_ICO.star as string
    case 'run_subagent_dag': return ACT_ICO.dag as string
    case 'ask_user': return ACT_ICO.ask as string
    default: return ACT_ICO.dot as string
  }
}

const COPY_ICO = '<rect x="9" y="9" width="11" height="11" rx="2.5"/>'
  + '<path d="M5.5 15H5a1.5 1.5 0 0 1-1.5-1.5v-8A1.5 1.5 0 0 1 5 4h8A1.5 1.5 0 0 1 14.5 5.5V6"/>'
const BRANCH_ICO = '<circle cx="7" cy="6" r="2.2"/><circle cx="7" cy="18" r="2.2"/>'
  + '<circle cx="17" cy="8" r="2.2"/><path d="M7 8.2v7.6"/>'
  + '<path d="M17 10.2v1.3a3.5 3.5 0 0 1-3.5 3.5H10"/>'
const DTL_COPY_ICO = '<rect x="9" y="9" width="11" height="11" rx="2.4"/>'
  + '<path d="M15 5.5A1.5 1.5 0 0 0 13.5 4H6a2 2 0 0 0-2 2v7.5A1.5 1.5 0 0 0 5.5 15"/>'
const SDLV_ICO = 'M5 5v5a4 4 0 0 0 4 4h9M14 10l4 4-4 4'

function Ico({ d, cls }: { d: string; cls?: string }): ReactElement {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
      aria-hidden="true" {...(cls ? { className: cls } : {})}>
      <path d={d} />
    </svg>
  )
}

const Chev = (): ReactElement => <Ico d={ACT_ICO.chev as string} cls="cv" />

function useSeg(lane: Lane, seg: { v: number }): number {
  return useSyncExternalStore((cb) => store.subscribe(lane, cb), () => seg.v)
}

/* Every fold changes the height of what is above the reader; pin the clicked
   row: measure it, mutate synchronously, correct the scroll -- with smooth
   scrolling off, or the correction itself animates. */
function pinRow(el: HTMLElement | null, mutate: () => void): void {
  const sc = document.querySelector<HTMLElement>('#scroll')
  if (!el || !sc) { flushSync(mutate); return }
  const was = el.getBoundingClientRect().top
  flushSync(mutate)
  const keep = sc.style.scrollBehavior
  sc.style.scrollBehavior = 'auto'
  sc.scrollTop += el.getBoundingClientRect().top - was
  sc.style.scrollBehavior = keep
}

const onKeyToggle = (fn: () => void) => (e: KeyboardEvent): void => {
  if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); fn() }
}

/* An icon-only action whose verb lives in the hover pill; the flash reports
   back through the same pill, so no toast for a tiny action. */
function TipButton({ label, icon, onClick, cls, flashWord }: {
  label: string; icon: string; onClick: () => void; cls?: string; flashWord?: string
}): ReactElement {
  const [tip, setTip] = useState<string | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])
  const flash = (word: string): void => {
    setTip(word)
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setTip(null), 1400)
  }
  return (
    <button {...(cls ? { className: cls } : {})} data-tip={tip ?? label} data-label={label}
      aria-label={label}
      onClick={(e) => { e.stopPropagation(); onClick(); if (flashWord) flash(flashWord) }}
      dangerouslySetInnerHTML={{ __html: `<svg viewBox="0 0 24 24" aria-hidden="true">${icon}</svg>` }} />
  )
}

function ctxRef(items: () => Array<{ label: string; fn: () => void }>) {
  return (el: HTMLElement | null): void => {
    if (el) (el as HTMLElement & { _ctx?: unknown })._ctx = items
  }
}

/* ── tool output: URLs and file paths are doors, not characters ─────────── */

const PRE_URL = /https?:\/\/[^\s<>"')\]]+[^\s<>"')\].,;:!?]/g
const PRE_PATH = /(?:^|[\s('"[])((?:~|\/)[\w.\-/@]+\.[A-Za-z0-9]{1,8})(?=$|[\s)'"\],;:])/g

function PreLinked({ text }: { text: string }): ReactElement {
  const s = String(text)
  const marks: Array<{ at: number; len: number; url?: string; path?: string }> = []
  let m: RegExpExecArray | null
  PRE_URL.lastIndex = 0
  while ((m = PRE_URL.exec(s)) !== null) marks.push({ at: m.index, len: m[0].length, url: m[0] })
  PRE_PATH.lastIndex = 0
  while ((m = PRE_PATH.exec(s)) !== null) {
    const at = m.index + m[0].length - (m[1] as string).length
    marks.push({ at, len: (m[1] as string).length, path: m[1] as string })
  }
  if (!marks.length) return <pre>{s}</pre>
  marks.sort((x, y) => x.at - y.at)
  const out: ReactNode[] = []
  let done = 0
  marks.forEach((k, i) => {
    if (k.at < done) return
    out.push(s.slice(done, k.at))
    const piece = s.substr(k.at, k.len)
    if (k.url) {
      out.push(
        <a key={i} href={k.url} target="_blank" rel="noopener"
          onClick={(e) => e.stopPropagation()}>{piece}</a>,
      )
    } else {
      /* The chip opens itself, and has to: stopPropagation is needed because
         the chip sits inside a step row that would toggle under it, and
         React's stopPropagation stops the NATIVE event too -- so the
         document-level click listener in shell/chips.ts never sees this one.
         Dropping the openChip call here breaks click-to-open outright.
         The keyboard path does go through chips.ts, as it went through the
         handler chips.ts replaced: nothing stops keydown, and the
         preventDefault there is also what keeps the button's own
         Enter-activates-a-click from opening the file a second time. */
      out.push(
        <button key={i} className="pth" data-p={k.path}
          onClick={(e) => { e.stopPropagation(); openChip({ p: k.path as string, dir: false }) }}>{piece}</button>,
      )
    }
    done = k.at + k.len
  })
  out.push(s.slice(done))
  return <pre>{out}</pre>
}

/* ── the detail block ──────────────────────────────────────────────────── */

function DtlHead({ name, hunk, copyText, openPath }: {
  name: string; hunk?: { add: number; del: number } | null; copyText?: string; openPath?: string
}): ReactElement {
  return (
    <div className="dhd">
      {/* Opens itself for the same reason as the path chips above: the
          document handler never sees a click React stopped, and the stop is
          needed for the detail block underneath. */}
      {openPath ? (
        <button className="nm pth" data-p={openPath} title={openPath}
          onClick={(e) => { e.stopPropagation(); openChip({ p: openPath, dir: false }) }}>{name}</button>
      ) : <span className="nm">{name}</span>}
      {hunk && (hunk.add || hunk.del) ? (
        <span className="ct"><span className="a">+{hunk.add}</span> <span className="d">-{hunk.del}</span></span>
      ) : null}
      {copyText ? (
        <TipButton cls="icb cp" label={t('gui.dtl.copy')} icon={DTL_COPY_ICO}
          flashWord={t('gui.answer.copied')} onClick={() => store.copyText(copyText)} />
      ) : null}
    </div>
  )
}

function dtlPre(text: string, key: string): ReactNode {
  let lines = String(text || '').split('\n')
  while (lines.length && !(lines[lines.length - 1] as string).trim()) lines.pop()
  if (!lines.length) return null
  const over = lines.length - store.DTL_MAX_LINES
  if (over > 0) lines = lines.slice(0, store.DTL_MAX_LINES)
  return (
    <Fragment key={key}>
      <PreLinked text={lines.join('\n')} />
      {over > 0 ? <div className="more">{`… +${over}`}</div> : null}
    </Fragment>
  )
}

/* Whether the settled call opens into a detail block at all: edits with no
   hunk and no failure and no label are the one shape that stays a bare row. */
export function hasDtl(c: CallData): boolean {
  if (c.name === 'edit_file' || c.name === 'write_file') {
    return !!(c.hunk && c.hunk.rows.length) || !c.ok || !!c.label
  }
  return true
}

function Dtl({ c, open }: { c: CallData; open: boolean }): ReactElement | null {
  const body: ReactNode[] = []
  let head: ReactNode = null
  if (c.name === 'edit_file' || c.name === 'write_file') {
    if (c.hunk && c.hunk.rows.length) {
      const rows = c.hunk.rows.slice(0, 120)
      const fp = store.argPath(c.args)
      head = <DtlHead name={shortOr(fp) || t('gui.dtl.plain')} hunk={c.hunk}
        copyText={rows.map((r) => (r[0] === 'gap' ? '' : String(r[1]))).join('\n')} {...(fp ? { openPath: fp } : {})} />
      rows.forEach((r, i) => {
        if (r[0] === 'gap') body.push(<div key={i} className="dline gap">{`⋯ ${(r[1] as string[]).length}`}</div>)
        else if (r[0] === 'hunk') body.push(<div key={i} className="dline gap">⋯</div>)
        else body.push(<div key={i} className={'dline' + (r[0] === 'add' ? ' a' : r[0] === 'del' ? ' d' : '')}>{(r[1] as string) || ' '}</div>)
      })
    }
    if (!c.ok) body.push(dtlPre(c.res, 'err'))
  } else if (c.name === 'exec') {
    const cmd = String(c.args.command || '')
    head = <DtlHead name={cmd || t('gui.dtl.output')} copyText={cmd || c.res} />
    if (cmd) body.push(<div key="cmd" className="cmd">{cmd}</div>)
    const lines = String(c.res || '').trimEnd().split('\n')
    const m = /^Exit code:\s*(-?\d+)$/.exec(lines[lines.length - 1] || '')
    let exit: number | null = null
    if (m) { exit = Number(m[1]); lines.pop() }
    body.push(dtlPre(lines.join('\n'), 'out'))
    if (exit != null) {
      body.push(
        <div key="exit" style={{ marginTop: '6px' }}>
          <span className={'exit ' + (exit === 0 ? 'ok' : 'bad')}>{t('gui.dtl.exit', { n: exit })}</span>
        </div>,
      )
    }
  } else if (c.name === 'web_fetch') {
    const url = String(c.args.url || '')
    head = <DtlHead name={url || t('gui.dtl.plain')} copyText={url || c.res} />
    if (url) body.push(<a key="url" href={url} target="_blank" rel="noopener">{url}</a>)
    body.push(dtlPre(c.res, 'out'))
  } else {
    let title = store.shortArg(c.label, 120)
    if (!title && (c.via || c.srv)) title = store.shortArg(JSON.stringify(c.args), 120)
    if (c.via) title = t('gui.dtl.via') + (title ? ' · ' + title : '')
    const fp = typeof c.args.path === 'string' && c.name !== 'list_dir' ? c.args.path : ''
    head = <DtlHead name={title || t('gui.dtl.plain')} copyText={String(c.res || '')} {...(fp ? { openPath: fp } : {})} />
    body.push(dtlPre(c.res, 'out'))
  }
  if (c.truncated) body.push(<div key="trunc" className="trunc">…</div>)
  const parts = body.filter(Boolean)
  if (!head && c.label) {
    const fp = typeof c.args.path === 'string' ? c.args.path : ''
    head = <DtlHead name={store.shortArg(c.label, 160)} copyText={c.label} {...(fp ? { openPath: fp } : {})} />
  }
  if (!head && !parts.length) return null
  return (
    <div className="dtl" hidden={!open}>
      {head}
      {parts.length ? <div className="bd">{parts}</div> : null}
    </div>
  )
}

const shortOr = (p: string): string => {
  try {
    return (window.DS?.workspace as { shortPath?: (p: string) => string } | undefined)?.shortPath?.(p) ?? p
  } catch { return p }
}

/* ── call rows ─────────────────────────────────────────────────────────── */

const CallRow = memo(function CallRow({ lane, seg, c }: { lane: Lane; seg: StepData; c: CallData }): ReactElement {
  useSeg(lane, c)
  const rowRef = useRef<HTMLDivElement | null>(null)
  if (c.kind === 'dag') return <DagCard lane={lane} seg={seg} c={c} />
  if (c.kind !== 'plain') return <DelegRow lane={lane} seg={seg} c={c} />
  const withDtl = c.done && hasDtl(c)
  const flip = (): void => pinRow(rowRef.current, () => store.toggleCall(lane, c))
  const cls = 'wrow'
    + (c.done ? '' : ' run')
    + (!c.done && c.name === 'ask_user' ? ' wait' : '')
    + (c.done && !c.ok ? ' bad' : '')
    + (withDtl ? ' tog' : '')
    + (withDtl && c.open ? ' open' : '')
  const err = c.done && !c.ok ? store.firstErrLine(c.res) : ''
  return (
    <>
      <div ref={rowRef} className={cls}
        {...(withDtl ? { tabIndex: 0, onClick: flip, onKeyDown: onKeyToggle(flip) } : {})}>
        <Ico d={c.done && !c.ok ? ACT_ICO.bad as string : actIco(c.name)} cls="ic" />
        <span className="vb">
          {c.srv ? <span className="srv">{`[${c.srv}] `}</span> : null}
          {c.done ? store.verbOf(c.name) : store.verbIngOf(c.name)}
        </span>
        {c.done && c.hunk && (c.hunk.add || c.hunk.del) ? (
          <span className="diffn"><span className="a">+{c.hunk.add}</span> <span className="d">-{c.hunk.del}</span></span>
        ) : null}
        {err ? <span className="err">{err}</span> : null}
        {!c.done ? <span className="cvslot" /> : withDtl ? <Chev /> : null}
      </div>
      {withDtl ? <Dtl c={c} open={c.open} /> : null}
    </>
  )
})

/* One elapsed clock per running card, self-stopping. */
function useTick(on: boolean, t0: number): number {
  const [, setN] = useState(0)
  useEffect(() => {
    if (!on) return
    const tick = setInterval(() => {
      setN((n) => n + 1)
      if (Date.now() - t0 > 3600e3) clearInterval(tick)
    }, 1000)
    return () => clearInterval(tick)
  }, [on, t0])
  return on ? Date.now() - t0 : 0
}

function DelegState({ state, err, extra }: { state: string; err?: string; extra?: string }): ReactElement {
  const word = t(state === 'run' ? 'gui.deleg.st_run' : state === 'ok' ? 'gui.deleg.st_ok' : 'gui.deleg.st_bad')
  return (
    <>
      <span className={'dot ' + state} />
      {state === 'bad' && err ? `${word} · ${err}` : word}
      {extra ? ` · ${extra}` : ''}
    </>
  )
}

const DelegRow = memo(function DelegRow({ lane, seg, c }: { lane: Lane; seg: StepData; c: CallData }): ReactElement {
  useSeg(lane, c)
  const rowRef = useRef<HTMLDivElement | null>(null)
  const elapsed = useTick(!c.done, c.t0)
  const flip = (): void => pinRow(rowRef.current, () => store.toggleCall(lane, c))
  const state = c.done ? (c.ok ? 'ok' : 'bad') : 'run'
  const cost = c.done ? (c.ms ? store.durText(c.ms) : '') : (elapsed >= 1000 ? store.durText(elapsed) : '…')
  const a = c.args as { agent?: string; instance?: string; task?: string }
  const who = store.spawnAgentOf(a) ? store.spawnAgentOf(a) + (a.instance ? ' @' + a.instance : '') : t('gui.deleg.self')
  const openTask = (): void => {
    if (c.kind === 'spawn') store.openSpawn(store.spawnAgentOf(a), c.label || '')
  }
  const grid: ReactNode[] = []
  const kv = (key: string, label: string, v: ReactNode, gov?: boolean): void => {
    grid.push(<div key={key + 'k'} className="k">{label}</div>)
    grid.push(gov ? (
      <div key={key + 'v'} className="v gov" role="button" tabIndex={0} title={t('gui.deleg.open_hint')}
        onClick={(e) => { e.stopPropagation(); openTask() }}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); openTask() }
        }}>{v}</div>
    ) : (
      <div key={key + 'v'} className={key === 'state' && state === 'bad' ? 'v err' : 'v'}>{v}</div>
    ))
  }
  kv('task', t('gui.deleg.d_task'), c.label || String(a.task || '').slice(0, 160), true)
  kv('agent', t('gui.deleg.d_agent'), <span className="who">{who}</span>)
  kv('state', t('gui.deleg.d_state'),
    <DelegState state={state} {...(state === 'bad' ? { err: store.firstErrLine(c.res) } : {})} />)
  kv('cost', t('gui.deleg.d_cost'), cost)
  return (
    <>
      <div ref={rowRef} className={'wrow' + (c.done ? '' : ' run') + ' tog' + (c.done && !c.ok ? ' bad' : '') + (c.open ? ' open' : '')}
        tabIndex={0} onClick={flip} onKeyDown={onKeyToggle(flip)}>
        <Ico d={c.done && !c.ok ? ACT_ICO.bad as string : actIco(c.name)} cls="ic" />
        <span className="vb">
          {c.srv ? <span className="srv">{`[${c.srv}] `}</span> : null}
          {c.done ? store.verbOf(c.name) : store.verbIngOf(c.name)}
        </span>
        <span className="ar">{c.rowLabel || ''}</span>
        <Chev />
      </div>
      <div className="dtl dlg" hidden={!c.open}>
        <div className="bd">
          <div className="dgr">{grid}</div>
        </div>
      </div>
    </>
  )
})

/* ── the dag card ───────────────────────────────────────────────────────────
   A `run_subagent_dag` call is a row like every other one; what is behind its
   caret is the request it made. That request has a shape -- which steps, in what
   order, each on which agent, each handed what -- and the card used to show four
   fields of it in a flat strip of chips, which is the structure thrown away and
   the arguments' remaining half never read at all.

   Three layers, and the split between the second and third is the point: the
   graph says what the orchestration *is*, the node panel says what one step was
   *asked*, and the run's own transcript -- one explicit click away, in the panel
   that already renders delegated work -- says what actually *happened*. Putting
   the third inside the card would be a second renderer for the same thing.

   Identical for a `load_playbook` call in dag mode. The graph is assembled by the
   engine there rather than written by the model, so it arrives from the event and
   `dag.get` instead of from the arguments -- which is a difference in where the
   nodes come from (features/dag/nodes.ts) and in nothing that is drawn. */

function DagGraph({ lane, c, selId }: { lane: Lane; c: CallData; selId: string | null }): ReactElement {
  const { W, H } = dag.CARD
  const nodes = c.nodes
  const { at, width, height } = dag.layout(nodes, dag.CARD)
  const done = new Set(nodes.filter((n) => n.status === 'completed').map((n) => n.id))
  /* A handle earns its place in the box only when it is shared, which is when it
     means "these steps continue one session". A handle held by one node is minted
     per node and reads as a mangled copy of the id above it. */
  const held = new Map<string, number>()
  nodes.forEach((n) => { if (n.instance) held.set(n.instance, (held.get(n.instance) || 0) + 1) })
  const edges: ReactNode[] = []
  nodes.forEach((n) => {
    n.depends_on.forEach((pid) => {
      const a = at.get(pid)
      const b = at.get(n.id)
      if (!a || !b) return
      const x1 = a.x + W, y1 = a.y + H / 2, x2 = b.x - 5, y2 = b.y + H / 2, mid = (x1 + x2) / 2
      const cls = done.has(pid) ? ' flowed' : ''
      edges.push(<path key={`e${pid}-${n.id}`} className={'edge' + cls}
        d={`M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}`} />)
      /* The head is its own path: a marker-end inherits the line's stroke width
         and ends up heavier than the line it caps. */
      edges.push(<path key={`t${pid}-${n.id}`} className={'tip' + cls}
        d={`M${x2 - 3.5} ${y2 - 3}L${x2 + 1} ${y2}l-4.5 3`} />)
    })
  })
  return (
    <div className="canvas">
      <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`}>
        {edges}
        {nodes.map((n) => {
          const p = at.get(n.id)
          if (!p) return null
          const pick = (): void => store.pickDagNode(lane, c, n.id)
          const mark = dag.MARKS[n.status]
          const handle = n.instance && (held.get(n.instance) || 0) > 1 ? ' @' + n.instance : ''
          return (
            <g key={n.id} className="gnd" transform={`translate(${p.x} ${p.y})`} role="button" tabIndex={0}
              data-st={n.status || 'pending'} {...(selId === n.id ? { 'data-sel': '1' } : {})}
              onClick={(e) => { e.stopPropagation(); pick() }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); pick() }
              }}>
              <rect width={W} height={H} rx={9} />
              {n.status === 'running'
                ? <g className="bars" transform={`translate(${16 - 5} ${H / 2 - 5})`}>
                    <rect x={0} y={2} width={2.4} height={6} rx={1.2} />
                    <rect x={4} y={0} width={2.4} height={10} rx={1.2} />
                    <rect x={8} y={2} width={2.4} height={6} rx={1.2} />
                  </g>
                : <path className={'mk ' + (mark ? mark.cls : 'wait')}
                    transform={`translate(16 ${H / 2})`}
                    d={mark ? mark.d : 'M-3.6 0a3.6 3.6 0 1 0 7.2 0a3.6 3.6 0 1 0 -7.2 0'} />}
              <text className="id" x={29} y={17}>{n.id}</text>
              <text className="ag" x={29} y={28}>{n.subagent + handle}</text>
              {/* On the second line, not beside the id as the wide sheet has it:
                  at card scale that layout has to reserve the clock's column on
                  the id's own line, and a node id then clips at nine characters. */}
              <text className="tm" x={W - 9} y={28} textAnchor="end">
                {n.status === 'running' ? '' : dag.took(n, Date.now())}
              </text>
              <title>{n.id + ' \u00b7 ' + n.subagent + (n.instance ? ' @' + n.instance : '')}</title>
            </g>
          )
        })}
      </svg>
    </div>
  )
}

/* One of a node's inputs, and where it came from. The three sources read
   differently on purpose: a literal is the words themselves, the other two name
   something to go and read. */
function InputRow({ k, v }: { k: string; v: unknown }): ReactElement {
  const obj = v && typeof v === 'object' ? (v as Record<string, unknown>) : null
  const file = obj && typeof obj.file === 'string' ? obj.file : null
  const node = obj && typeof obj.node === 'string' ? obj.node : null
  const kind = node ? 'node' : file ? 'file' : 'literal'
  return (
    <>
      <span className="key">{k}</span>
      <span className="src" data-k={kind}>{t('gui.dag.src_' + kind)}</span>
      <span className="val">{node || file || (typeof v === 'string' ? `"${v}"` : JSON.stringify(v))}</span>
    </>
  )
}

/* When the template needs a "show all of it". Length OR a line break: the clamp
   itself is four lines of CSS, and a template written across several short lines
   is clipped long before 150 characters -- while a single line of latin text that
   long is still two lines and needs no control. */
const TPL_CLAMP = 150
const tplIsLong = (tpl: string): boolean => tpl.length > TPL_CLAMP || tpl.includes('\n')

function DagNodePanel({ lane, c, n }: { lane: Lane; c: CallData; n: DagNode }): ReactElement {
  const rows: ReactNode[] = []
  const kv = (key: string, label: string, v: ReactNode): void => {
    rows.push(<div key={key + 'k'} className="k">{label}</div>)
    rows.push(<div key={key + 'v'} className="v">{v}</div>)
  }
  const shared = n.instance && c.nodes.filter((x) => x.instance === n.instance).length > 1
  kv('agent', t('gui.deleg.d_agent'), (
    <>
      {n.subagent + (n.instance ? ' @' + n.instance : '')}
      {n.instance ? <span className="hold">{t(shared ? 'gui.dag.held_shared' : 'gui.dag.held_own')}</span> : null}
    </>
  ))
  kv('deps', t('gui.dag.deps'), n.depends_on.length ? (
    <>
      {n.depends_on.map((pid: string) => (
        <button key={pid} type="button" className="dep"
          onClick={(e) => { e.stopPropagation(); store.pickDagNode(lane, c, pid) }}>{pid}</button>
      ))}
    </>
  ) : <span className="none">{t('gui.dag.deps_none')}</span>)
  const keys = Object.keys(n.inputs || {})
  kv('inputs', t('gui.dag.inputs'), keys.length ? (
    <div className="ins">
      {keys.map((k) => <InputRow key={k} k={k} v={(n.inputs as Record<string, unknown>)[k]} />)}
    </div>
  ) : <span className="none">{t('gui.dag.inputs_none')}</span>)
  if (n.prompt_template) {
    const long = tplIsLong(n.prompt_template)
    kv('tpl', t('gui.dag.tpl'), (
      <>
        <pre className={'tpl' + (long && !c.selFull ? ' clip' : '')}>
          {/* The placeholders are the part a reader is looking for: they are what
              ties this step to the ones before it. */}
          {n.prompt_template.split(/(\{\{[^}]*\}\})/).map((part: string, i: number) => (
            /^\{\{/.test(part) ? <span key={i} className="ph">{part}</span> : <Fragment key={i}>{part}</Fragment>
          ))}
        </pre>
        {long ? (
          <button type="button" className="more"
            onClick={(e) => { e.stopPropagation(); store.toggleDagFull(lane, c) }}>
            {t(c.selFull ? 'gui.dag.tpl_less' : 'gui.dag.tpl_more')}
          </button>
        ) : null}
      </>
    ))
  }
  const st = String(n.status)
  return (
    <div className="npanel">
      <div className="nhd">
        <span className="nm">{n.id}</span>
        <span className="st">{t('gui.dag.st_' + st, undefined, st)}{n.started_at ? ' · ' + dag.took(n, Date.now()) : ''}</span>
        {c.runId ? (
          <button type="button" className="go"
            onClick={(e) => { e.stopPropagation(); store.openDagNode(c.runId as string, n.id) }}>
            {t('gui.dag.open_run')}
          </button>
        ) : null}
      </div>
      <div className="rows">{rows}</div>
    </div>
  )
}

const DagCard = memo(function DagCard({ lane, seg, c }: { lane: Lane; seg: StepData; c: CallData }): ReactElement {
  useSeg(lane, c)
  const rowRef = useRef<HTMLDivElement | null>(null)
  const elapsed = useTick(!c.done, c.t0)
  const flip = (): void => pinRow(rowRef.current, () => store.toggleCall(lane, c))
  const state = c.done ? (c.ok ? 'ok' : 'bad') : 'run'
  const cost = c.done ? (c.ms ? store.durText(c.ms) : '') : (elapsed >= 1000 ? store.durText(elapsed) : '…')
  const nodes = c.nodes
  const tally = { ok: 0, bad: 0, skip: 0, run: 0 }
  nodes.forEach((n) => {
    const d = store.DOT_OF[n.status]
    if (d === 'ok') tally.ok += 1
    else if (d === 'bad') tally.bad += 1
    else if (d === 'skip') tally.skip += 1
    else if (d === 'run') tally.run += 1
  })
  const bits: string[] = []
  if (c.done && (tally.ok || tally.bad || tally.skip)) {
    bits.push(t('gui.deleg.dag_done', { ok: String(tally.ok) }))
    if (tally.bad) bits.push(t('gui.deleg.dag_bad', { n: String(tally.bad) }))
    if (tally.skip) bits.push(t('gui.deleg.dag_skip', { n: String(tally.skip) }))
  }
  const extra = bits.join(' · ')
  /* The graph is the picture; a single node is not one, and one box on its own
     reads worse than the line above it. */
  const drawn = nodes.length > 1
  /* One source for what the panel shows. The unasked open on a failure is done by
     the store, when the failure arrives -- derived here instead, it made `null`
     mean both "nobody picked one" and "the reader closed it", so the panel it
     opened swallowed the click meant to close it and reopened on the next. */
  const selId = c.sel
  const sel = selId ? nodes.find((n) => n.id === selId) : undefined
  const agents = [...new Set(nodes.map((n) => n.subagent).filter(Boolean))]
  const grid: ReactNode[] = []
  const kv = (key: string, label: string, v: ReactNode): void => {
    grid.push(<div key={key + 'k'} className="k">{label}</div>)
    grid.push(<div key={key + 'v'} className={key === 'state' && state === 'bad' ? 'v err' : 'v'}>{v}</div>)
  }
  /* The row says how much work and what shape it is; the agents are one line
     down, because the count of them is almost always one and appending them made
     the row too long to read at a glance. A playbook load keeps its name in
     front: which playbook ran is the first thing about that call. */
  const shape = nodes.length ? dag.shape(nodes) : ''
  const rowLabel = [c.label, shape].filter(Boolean).join(' · ') || t('gui.deleg.dag_title')
  kv('scale', t('gui.deleg.d_scale'),
    [shape, ...(agents.length ? [agents.join(' · ')] : [])].filter(Boolean).join(' · ')
    || c.label || t('gui.deleg.dag_title'))
  /* The receipt whenever the call failed, and the tally beside it. They are not
     two renderings of one fact: `okOf` calls a dag result bad only when its first
     word is error-shaped, which is the graph-level failure
     (`Error running DAG <id>: ...`) and never a node failure -- a run whose nodes
     failed returns a summary and reads as ok. So a bad state is exactly the case
     the nodes cannot explain, and withholding the receipt there left the card
     showing a node count and no cause. */
  kv('state', t('gui.deleg.d_state'),
    <DelegState state={state} {...(state === 'bad' ? { err: store.firstErrLine(c.res) } : {})}
      {...(extra ? { extra } : {})} />)
  kv('cost', t('gui.deleg.d_cost'), cost)
  return (
    <>
      <div ref={rowRef} className={'wrow' + (c.done ? '' : ' run') + ' tog' + (c.done && !c.ok ? ' bad' : '') + (c.open ? ' open' : '')}
        tabIndex={0} onClick={flip} onKeyDown={onKeyToggle(flip)}>
        <Ico d={c.done && !c.ok ? ACT_ICO.bad as string : actIco(c.name)} cls="ic" />
        <span className="vb">
          {c.srv ? <span className="srv">{`[${c.srv}] `}</span> : null}
          {c.done ? store.verbOf(c.name) : store.verbIngOf(c.name)}
        </span>
        <span className="ar">{rowLabel}</span>
        <Chev />
      </div>
      <div className="dtl dlg dagc" hidden={!c.open}>
        <div className="bd">
          <div className="dgr">{grid}</div>
          {drawn ? <DagGraph lane={lane} c={c} selId={selId} /> : null}
          {sel ? <DagNodePanel lane={lane} c={c} n={sel} /> : null}
        </div>
      </div>
    </>
  )
})

/* ── the step: thought, narration, work ────────────────────────────────── */

export const StepView = memo(function StepView({ lane, seg }: { lane: Lane; seg: StepData }): ReactElement {
  useSeg(lane, seg)
  const thinkRef = useRef<HTMLDivElement | null>(null)
  const cotRef = useRef<HTMLDivElement | null>(null)
  const sumRef = useRef<HTMLDivElement | null>(null)
  /* Follow the newest thought line only while the reader is at its bottom. */
  useEffect(() => {
    const cot = cotRef.current
    if (!cot || !seg.thinkLive || !seg.thinkOpen) return
    if (cot.scrollHeight - cot.scrollTop - cot.clientHeight < 40) cot.scrollTop = cot.scrollHeight
  })
  const flipThink = (): void => pinRow(thinkRef.current, () => store.toggleThink(lane, seg))
  const flipWork = (): void => pinRow(sumRef.current, () => store.toggleWork(lane, seg))
  const one = seg.calls.length > 0 && seg.calls.every((c) => c.name === (seg.calls[0] as CallData).name)
  const live = seg.calls.some((c) => !c.done)
  const failedN = seg.calls.filter((c) => c.done && !c.ok).length
  const showSum = seg.calls.length > 1
  const wkinHidden = seg.calls.length === 0 ? true : seg.calls.length === 1 ? false : !seg.wkOpen
  return (
    <div className="step in">
      <div ref={thinkRef}
        className={'think tog' + (seg.thinkLive ? ' live' : '') + (seg.thinkOpen ? ' open' : '')}
        hidden={!seg.thinkShown} tabIndex={0} onClick={flipThink} onKeyDown={onKeyToggle(flipThink)}>
        <span className="lb">{t(seg.thinkLive ? 'gui.think.live' : 'gui.think.label')}</span>
        <span className="tm">{seg.thinkSecs != null && seg.thinkSecs > 0 ? `${seg.thinkSecs}s` : ''}</span>
        <Chev />
      </div>
      <div ref={cotRef} className="cot" hidden={!seg.thinkOpen}>{seg.think}</div>
      <div className="say prose"
        dangerouslySetInnerHTML={{ __html: seg.say ? store.mdHtml(seg.say) : '' }} />
      <div className="wk">
        <div ref={sumRef}
          className={'wrow tog' + (showSum ? ' sum' : '') + (showSum && live ? ' run' : '') + (showSum && seg.wkOpen ? ' open' : '')}
          hidden={!showSum}
          {...(showSum ? { tabIndex: 0, onClick: flipWork, onKeyDown: onKeyToggle(flipWork) } : {})}>
          {showSum ? (
            <>
              <Ico d={one ? actIco((seg.calls[0] as CallData).name) : ACT_ICO.dot as string} cls="ic" />
              <span className="ar">{store.phraseOf(seg.calls)}</span>
              {failedN ? <span className="chip bad">{t('gui.n_failed', { n: failedN })}</span> : null}
              <Chev />
            </>
          ) : null}
        </div>
        <div className="wkin" hidden={wkinHidden}>
          {seg.calls.map((c) => <CallRow key={c.id} lane={lane} seg={seg} c={c} />)}
        </div>
      </div>
    </div>
  )
})

/* ── the other segments ────────────────────────────────────────────────── */

const AskView = memo(function AskView({ lane, seg }: { lane: Lane; seg: AskData }): ReactElement {
  useSeg(lane, seg)
  const bRef = useRef<HTMLDivElement | null>(null)
  const sh = shell()
  const imgs = seg.atts.filter((p) => sh.attImage?.(String(p)))
  const docs = seg.atts.filter((p) => !sh.attImage?.(String(p)))
  const openAll = (): void => store.expandAskAtts(lane, seg)
  const thumb = (p: string, liveImg: boolean): ReactElement => {
    const src = sh.attImage!(String(p)) as string
    const nm = String(p).split('/').pop() || ''
    return <img key={p} className="shot" src={src} alt={nm}
      {...(liveImg ? { title: t('gui.img.open', { name: nm }), onClick: () => lightbox.open(src, nm) } : {})} />
  }
  const showClip = seg.clipped && !seg.clipOpen
  return (
    <div className="ask in">
      {seg.atts.length ? (
        <div className={'abox' + (imgs.length > 1 ? ' set' : '')}>
          {imgs.length && (seg.expanded || imgs.length <= 3)
            ? imgs.map((p) => thumb(p, true))
            : imgs.length ? (
              <button className="pile" title={t('gui.att.show_all')} onClick={openAll}>
                {thumb(imgs[0] as string, false)}
                <span className="cnt">{`${imgs.length}`}</span>
              </button>
            ) : null}
          {docs.length && (seg.expanded || docs.length <= 2)
            ? docs.map((p) => (
              <button key={p} className="achip" title={p} onClick={() => wsOpenPath(p)}>
                <span className="nm">{String(p).split('/').pop()}</span>
              </button>
            ))
            : docs.length ? (
              <button className="achip more" title={t('gui.att.show_all')} onClick={openAll}>
                <span className="nm">{t('gui.att.n_files', { n: docs.length })}</span>
              </button>
            ) : null}
        </div>
      ) : null}
      {seg.body.trim() ? (
        <div ref={bRef} className={'b' + (showClip ? ' clip' : '')}>{seg.body}</div>
      ) : null}
      {seg.body.trim() && seg.clipped ? (
        <button className="qfold" aria-expanded={String(seg.clipOpen) as 'true' | 'false'}
          onClick={() => {
            const opening = !seg.clipOpen
            store.toggleAskClip(lane, seg)
            if (!opening && bRef.current) bRef.current.scrollIntoView({ block: 'nearest' })
          }}>{t(seg.clipOpen ? 'gui.ask.collapse' : 'gui.ask.expand')}</button>
      ) : null}
      <div className="ansfoot">
        <div className="acts">
          <TipButton label={t('gui.answer.copy')} icon={COPY_ICO}
            flashWord={t('gui.answer.copied')} onClick={() => store.copyText(seg.body)} />
        </div>
        <span className="turnmeta">{seg.when}</span>
      </div>
    </div>
  )
})

const AnswerView = memo(function AnswerView({ lane, seg }: { lane: Lane; seg: AnswerData }): ReactElement {
  useSeg(lane, seg)
  const branch = store.branchOf(lane)
  const typing = seg.shown != null && seg.shown < seg.text.length
  const html = seg.shown != null
    ? store.mdHtml(seg.text.slice(0, seg.shown)) + (typing ? '<span class="caret"></span>' : '')
    : store.mdHtml(seg.text)
  const items = (): Array<{ label: string; fn: () => void }> => {
    const list = [{
      label: t('gui.answer.copy'),
      fn: () => shell().copyToClip?.(seg.text, t('gui.answer.copied')),
    }]
    if (branch) list.push({ label: t('gui.answer.branch'), fn: () => branch(seg.text) })
    return list
  }
  return (
    <div className="answer in" ref={ctxRef(items)}>
      <div className="prose" dangerouslySetInnerHTML={{ __html: html }} />
      <div className="ansfoot">
        <div className="acts">
          <TipButton label={t('gui.answer.copy')} icon={COPY_ICO}
            flashWord={t('gui.answer.copied')} onClick={() => store.copyText(seg.text)} />
          {branch ? (
            <TipButton label={t('gui.answer.branch')} icon={BRANCH_ICO}
              onClick={() => branch(seg.text)} />
          ) : null}
        </div>
        {seg.when ? <span className="turnmeta">{seg.when}</span> : null}
      </div>
    </div>
  )
})

const NoteView = memo(function NoteView({ lane, seg }: { lane: Lane; seg: NoteData }): ReactElement {
  useSeg(lane, seg)
  const brief = store.firstErrLine(seg.detail, 140) || seg.detail
  const full = seg.detail ? `${seg.label} · ${seg.detail}` : seg.label
  const items = (): Array<{ label: string; fn: () => void }> => [
    { label: t('gui.answer.copy'), fn: () => shell().copyToClip?.(full, t('gui.answer.copied')) },
  ]
  return (
    <div className={'tnote in' + (seg.quiet ? '' : ' bad')} title={full} ref={ctxRef(items)}>
      <span className="tx">{brief ? `${seg.label} · ${brief}` : seg.label}</span>
      {seg.retry ? (
        <button className="rt" onClick={() => {
          const retry = seg.retry
          const i = lane.segs.indexOf(seg)
          if (i >= 0) { lane.segs.splice(i, 1); store.nudge(lane) }
          retry?.()
        }}>{t('gui.retry')}</button>
      ) : null}
    </div>
  )
})

const QA_Q = 62
const QA_A = 34

const QaView = memo(function QaView({ lane, seg }: { lane: Lane; seg: QaData }): ReactElement {
  useSeg(lane, seg)
  const rowRef = useRef<HTMLDivElement | null>(null)
  const overflow = seg.q.length > QA_Q || seg.a.length > QA_A
  const flip = (): void => pinRow(rowRef.current, () => store.toggleQa(lane, seg))
  return (
    <>
      <div ref={rowRef} className={'wrow qa' + (overflow ? ' tog' : '') + (overflow && seg.open ? ' open' : '')}
        {...(overflow ? { tabIndex: 0, onClick: flip, onKeyDown: onKeyToggle(flip) } : {})}>
        <Ico d={ACT_ICO.ask as string} cls="ic" />
        <span className="vb">{t(seg.skipped ? 'gui.qa.skipped' : 'gui.qa.answered')}</span>
        <span className="ar">{store.shortArg(seg.q, QA_Q)}</span>
        {seg.a && !seg.skipped ? <span className="an">{store.shortArg(seg.a, QA_A)}</span> : null}
        {overflow ? <Chev /> : null}
      </div>
      {overflow ? (
        <div className="dtl" hidden={!seg.open}>
          <div className="bd">
            <div className="hd">{t('gui.qa.q')}</div>
            <pre>{seg.q}</pre>
            {seg.a ? (
              <>
                <div className="hd">{t('gui.qa.a')}</div>
                <pre>{seg.a}</pre>
              </>
            ) : null}
          </div>
        </div>
      ) : null}
    </>
  )
})

const StatusView = memo(function StatusView({ lane, seg }: { lane: Lane; seg: StatusData }): ReactElement {
  useSeg(lane, seg)
  return (
    <div className="status in">
      <span className="pip" />
      <span>{seg.text}</span>
    </div>
  )
})

const DeliveredView = memo(function DeliveredView({ lane, seg }: { lane: Lane; seg: DeliveredData }): ReactElement {
  useSeg(lane, seg)
  return (
    <div className={'sdlv' + (seg.err ? ' err' : '')}>
      <Ico d={SDLV_ICO} cls="ic" />
      <button className="nm" onClick={seg.open}>{seg.isDag ? t('gui.deleg.dag_title') : seg.label}</button>
      <span className="tx">{t(seg.err ? 'gui.deleg.delivered_err' : 'gui.deleg.delivered')}</span>
    </div>
  )
})

/* ── the turn's products ───────────────────────────────────────────────
   A turn that wrote files ends with them. As tiles rather than a list of
   names, because four documents out of one turn read as four identical names
   and as four different miniatures -- that difference is what the extra
   height buys. */

const ART_CAP = 6
/* How many of the file's own lines a miniature draws. More than fills the
   tile at this scale; the rest would be rendered and then clipped. */
const ART_HEAD_LINES = 16
/* Above this an image is not worth fetching whole to paint 164 pixels of it,
   and the tile shows its kind instead. There is no thumbnailer to ask -- the
   gateway's /file serves the file itself -- so this ceiling is the only guard
   between a tile and an eight-megabyte screenshot. */
const ART_IMG_MAX = 2 * 1024 * 1024

/* Text draws itself with nothing to wait for: a write tool's hunk already
   carried what it wrote, live and on replay both, so `head` is in memory
   before the tile mounts.

   Markdown goes through the renderer the full-size viewer uses, and the
   result is SCALED by the stylesheet. Not set in a smaller font: shrinking
   the type re-wraps every line and stops a heading from being a heading, so
   what the tile showed would be a different document from the one it names. */
const ArtMini = memo(function ArtMini({ row }: { row: ArtifactRow }): ReactElement {
  if (!row.head) return <span className="pic none"><Ico d={ACT_ICO.doc as string} cls="fi" /></span>
  const head = row.head.split('\n').slice(0, ART_HEAD_LINES).join('\n')
  if (fileKind(row.name) === 'md') {
    return (
      <span className="pic doc">
        <span className="mini prose" dangerouslySetInnerHTML={{ __html: store.mdHtml(head) }} />
      </span>
    )
  }
  return <span className="pic doc"><span className="mini"><span className="raw">{head}</span></span></span>
})

/* An image has to come down the wire, so this is the tile that can be slow.
   It asks for the size first -- a HEAD on the same URL, no RPC of its own --
   and only then decides whether to fetch the picture at all. Both waits are
   covered by the skeleton: a tile that jumps from blank to full reads as the
   page having been broken until that moment. */
const ArtShot = memo(function ArtShot({ row }: { row: ArtifactRow }): ReactElement {
  const [st, setSt] = useState<'probe' | 'load' | 'ok' | 'no'>('probe')
  const url = fileURL(row.path)
  useEffect(() => {
    let alive = true
    /* HEAD on the same URL the picture would come from. The response carries
       Content-Length and no body, so asking costs one round trip and no
       bytes -- and a page that cannot serve the file answers 401 or 404 here
       rather than after a megabyte. */
    fetch(url, { method: 'HEAD', credentials: 'same-origin', cache: 'no-store' })
      .then((r) => {
        if (!alive) return
        const n = Number(r.headers.get('content-length'))
        setSt(!r.ok || (Number.isFinite(n) && n > ART_IMG_MAX) ? 'no' : 'load')
      })
      .catch(() => { if (alive) setSt('no') })
    return () => { alive = false }
  }, [url])
  if (st === 'no') return <span className="pic none"><Ico d={ACT_ICO.image as string} cls="fi" /></span>
  return (
    <span className={'pic shot' + (st === 'ok' ? '' : ' skel')}>
      {st === 'probe' ? null : (
        <img src={url} alt="" loading="lazy" decoding="async"
          onLoad={() => setSt('ok')} onError={() => setSt('no')} />
      )}
      {st === 'ok' ? null : <span className="sk" />}
    </span>
  )
})

const ArtTile = memo(function ArtTile({ row, onOpen }: { row: ArtifactRow; onOpen: () => void }): ReactElement {
  /* Where it opens, said before the click: a kind the page renders opens in
     the viewer, a kind it cannot read leaves for the host. */
  const kind = fileKind(row.name)
  const away = !WS_RENDERED[kind]
  return (
    <div className="atile" title={row.path}>
      {kind === 'img' || kind === 'svg' ? <ArtShot row={row} /> : <ArtMini row={row} />}
      <span className="cap">
        <span className="nm">{row.name}</span>
        <span className="mt">
          {row.ext ? row.ext.toUpperCase() : t('gui.arts.file')}
          {row.lines ? ` \u00b7 ${t('gui.arts.lines', { n: String(row.lines) })}` : ''}
        </span>
        {away ? <span className="away">{t('gui.arts.away')}</span> : null}
      </span>
      {/* The whole tile is the target; a covering button keeps that one
          keyboard stop and one accessible name, and leaves the picture free
          to be block content a <button> may not contain. */}
      <button className="hit" aria-label={t('gui.arts.open', { f: row.name })} onClick={onOpen} />
    </div>
  )
})

const ArtsView = memo(function ArtsView({ lane, seg }: { lane: Lane; seg: ArtsData }): ReactElement | null {
  useSeg(lane, seg)
  const rows = store.artifactsOf(seg.turn)
  /* Read, not stored -- so a turn whose products the source no longer knows
     about draws nothing rather than a bar of dead names. */
  if (!rows.length) return null
  const shown = seg.all ? rows : rows.slice(0, ART_CAP)
  const rest = rows.length - shown.length
  return (
    <div className="arts">
      <div className="ahd">
        <span className="lb">{t('gui.arts.head')}</span>
        <span className="n">{rows.length}</span>
      </div>
      <div className="atiles">
        {shown.map((r) => (
          <ArtTile key={r.path} row={r} onOpen={() => wsOpenPath(r.path)} />
        ))}
        {rest > 0 ? (
          <button className="amore" onClick={() => store.toggleArts(lane, seg)}>
            {t('gui.arts.more', { n: String(rest) })}
          </button>
        ) : null}
      </div>
    </div>
  )
})

const FoldView = memo(function FoldView({ lane, seg }: { lane: Lane; seg: FoldData }): ReactElement {
  useSeg(lane, seg)
  const headRef = useRef<HTMLButtonElement | null>(null)
  const flip = (): void => pinRow(headRef.current, () => store.toggleFold(lane, seg))
  return (
    <div className={'tfold' + (seg.open ? ' open' : '')}>
      <button ref={headRef} className="tfh" aria-label={t('gui.fold.aria')}
        aria-expanded={String(seg.open) as 'true' | 'false'} onClick={flip}>
        <span className="lb">{t('gui.fold.done')}</span>
        <span className="tm">{seg.time || ''}</span>
        <Chev />
      </button>
      <div className="tfb" hidden={!seg.open}>
        {seg.steps.map((s) => <StepView key={s.id} lane={lane} seg={s} />)}
      </div>
    </div>
  )
})

/* ── the lane ──────────────────────────────────────────────────────────── */

function SegView({ lane, seg }: { lane: Lane; seg: Seg }): ReactElement | null {
  switch (seg.kind) {
    case 'ask': return <AskView lane={lane} seg={seg} />
    case 'step': return <StepView lane={lane} seg={seg} />
    case 'answer': return <AnswerView lane={lane} seg={seg} />
    case 'note': return <NoteView lane={lane} seg={seg} />
    case 'qa': return <QaView lane={lane} seg={seg} />
    case 'status': return <StatusView lane={lane} seg={seg} />
    case 'sdlv': return <DeliveredView lane={lane} seg={seg} />
    case 'arts': return <ArtsView lane={lane} seg={seg} />
    case 'fold': return <FoldView lane={lane} seg={seg} />
    default: return null
  }
}

export function StageView({ lane }: { lane: Lane }): ReactElement {
  useSyncExternalStore((cb) => store.subscribe(lane, cb), () => lane.listV)
  const req = useSyncExternalStore((cb) => store.subscribe(lane, cb), () => lane.scrollReq)
  /* Tail-follow after the commit, so the new height is the one measured. */
  useEffect(() => {
    if (lane.main) shell().down?.()
  }, [lane, req])
  return (
    <>
      {lane.segs.map((s) => <SegView key={`${lane.epoch}:${s.id}`} lane={lane} seg={s} />)}
    </>
  )
}

/* The agent stage pane: the same segments, plus the working glyph at the
   tail and the pane's own empty note. */
export function AgentStageView({ lane }: { lane: Lane }): ReactElement {
  useSyncExternalStore((cb) => store.subscribe(lane, cb), () => lane.listV)
  return (
    <>
      {lane.segs.map((s) => <SegView key={`${lane.epoch}:${s.id}`} lane={lane} seg={s} />)}
      {lane.running ? (
        <div className="act in sarun">
          <span className="wkg" aria-hidden="true"><i /><i /><i /></span>
        </div>
      ) : null}
      {!lane.running && !lane.segs.length ? (
        <div className="wsempty">{lane.empty}</div>
      ) : null}
    </>
  )
}
