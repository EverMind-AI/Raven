import { Fragment, memo, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { flushSync } from 'react-dom'

import { shell, t } from '../../shell/bridge'
import { openPath as wsOpenPath } from '../workspace/store'
import * as store from './store'

import type {
  AnswerData, AskData, CallData, DeliveredData, FoldData, Lane, NoteData, QaData, Seg, StatusData, StepData,
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
      /* `.pth` is a contract: the document-level handler opens `dataset.p`. */
      out.push(
        <button key={i} className="pth" data-p={k.path}
          onClick={(e) => { e.stopPropagation(); shell().pathOpen?.(k.path as string) }}>{piece}</button>,
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
      {openPath ? (
        <button className="nm pth" data-p={openPath} title={openPath}
          onClick={(e) => { e.stopPropagation(); shell().pathOpen?.(openPath) }}>{name}</button>
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
  const who = a.agent ? String(a.agent) + (a.instance ? ' @' + a.instance : '') : t('gui.deleg.self')
  let extra = ''
  if (c.kind === 'dag' && c.done && c.ok) {
    const n = { ok: 0, bad: 0, skip: 0 }
    c.chips.forEach((ch) => {
      const d = store.DOT_OF[ch.st]
      if (d === 'ok') n.ok += 1
      else if (d === 'bad') n.bad += 1
      else if (d === 'skip') n.skip += 1
    })
    const bits: string[] = []
    if (n.ok || n.bad || n.skip) {
      bits.push(t('gui.deleg.dag_done', { ok: String(n.ok) }))
      if (n.bad) bits.push(t('gui.deleg.dag_bad', { n: String(n.bad) }))
      if (n.skip) bits.push(t('gui.deleg.dag_skip', { n: String(n.skip) }))
    }
    extra = bits.join(' · ')
  }
  const openTask = (): void => {
    if (c.kind === 'spawn') store.openSpawn(a.agent ? String(a.agent) : '', c.label || '')
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
  if (c.kind === 'spawn') {
    kv('task', t('gui.deleg.d_task'), c.label || String(a.task || '').slice(0, 160), true)
    kv('agent', t('gui.deleg.d_agent'), <span className="who">{who}</span>)
  } else {
    kv('scale', t('gui.deleg.d_scale'), c.label || t('gui.deleg.dag_title'))
  }
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
        <span className="ar">{c.rowLabel || ''}</span>
        <Chev />
      </div>
      <div className="dtl dlg" hidden={!c.open}>
        <div className="bd">
          <div className="dgr">{grid}</div>
          {c.kind === 'dag' && c.chips.length ? (
            <div className="nds" {...(c.chipsLive ? { 'data-live': '1' } : {})}>
              {c.chips.map((n) => {
                const dotCls = store.DOT_OF[n.st] || ''
                return (
                  <button key={n.id} className={'nd' + (dotCls ? ' act' : '')} data-st={n.st || 'pending'}
                    title={n.subagent ? `${n.id} · ${n.subagent}` : n.id}
                    onClick={(e) => {
                      e.stopPropagation()
                      if (c.runId) store.openDagNode(c.runId, n.id)
                    }}>
                    <span className={'dot' + (dotCls ? ' ' + dotCls : '')} />
                    <span className="tx">
                      <span className="id">{n.id}</span>
                      {n.subagent ? <span className="ag">{n.subagent + (n.instance ? ' @' + n.instance : '')}</span> : null}
                    </span>
                  </button>
                )
              })}
            </div>
          ) : null}
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
