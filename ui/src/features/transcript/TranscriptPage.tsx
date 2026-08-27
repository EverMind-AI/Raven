import { Fragment, memo, useEffect, useRef, useState, useSyncExternalStore } from 'react'
import { flushSync } from 'react-dom'

import * as dag from '../dag/graph'
import { DagGraph } from '../dag/DagGraph'
import * as attachmentCache from '../../shell/attachment-cache'
import { shell, t } from '../../shell/bridge'
import { copy } from '../../shell/clipboard'
import { open as openChip } from '../../shell/chips'
import { humanSize } from '../workspace/deliveries'
import {
  fileKind, fileURL, openDelivery as wsOpenDelivery, openPath as wsOpenPath,
} from '../workspace/store'
import * as store from './store'
import * as tail from './tail'

import type { DagNode } from '../dag/types'
import type { DeliveryRow } from '../workspace/types'
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
      {/* Built on opening, not merely hidden when shut. `hidden` spares the
          layout and the paint but not the nodes, and a shut detail is up to
          120 diff rows or a whole command's output -- for a resumed session,
          the same again for every call it ever made. Nothing outside this
          island reads a shut body, and `display: none` was never findable by
          the browser's own search nor part of a selection, so there is no
          reader this takes anything from. The same rule holds for the work
          list and the fold body below, which are the two that carry this one.
          Bounded bodies (a delegation's four labelled cells, a dag card) stay
          eager: they do not grow with the conversation. */}
      {withDtl && c.open ? <Dtl c={c} open={c.open} /> : null}
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
  /* Only when there is something to say. Both clocks here can come up empty --
     a call that finished carrying no duration, and a graph that stopped
     without leaving an end stamp -- and a labelled row with an empty value
     cell reads as a broken render rather than as an absent number. */
  if (cost) kv('cost', t('gui.deleg.d_cost'), cost)
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
  /* Only once the header stopped being it. The id is what a dependency names,
     what the run dir is keyed by and what a reader types into a search -- it did
     not stop mattering when it stopped being the title. */
  if (n.node_summary) kv('nid', t('gui.dag.node_id'), <span className="nid">{n.id}</span>)
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
        {/* What the node was dispatched to do, which the model is required to
            write. The id stays -- it is what a dependency names and what the run
            dir is keyed by -- but one row down, among the machine-readable
            fields, rather than standing in for a title it never was. */}
        <span className="nm">{n.node_summary || n.id}</span>
        <span className="st">{t('gui.dag.st_' + st, undefined, st)}{n.started_at ? ' · ' + dag.took(n, Date.now()) : ''}</span>
        {c.runId ? (
          <button type="button" className="orun"
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
  const flip = (): void => pinRow(rowRef.current, () => store.toggleCall(lane, c))
  const state = c.done ? (c.ok ? 'ok' : 'bad') : 'run'
  const nodes = c.nodes
  /* The graph's clock, not the call's. A backgrounded graph -- which is the
     default -- returns as soon as it is submitted, so `c.ms` is that submit: a
     number near zero, frozen there while the nodes run for minutes. The span
     comes off the nodes, and ticks for as long as one of them has not stopped. */
  const span = store.dagSpan(nodes)
  const live = !!span && span.open
  const running = useTick(live, span ? span.from : c.t0)
  const cost = span
    ? (live
      ? (running >= 1000 ? store.durText(running) : '…')
      /* A closed span with no end stamp is a cancel seen over the wire: every
         node stopped and none of them said when. Blank rather than a made-up
         number -- one reload reads the manifest, which does carry the stamps. */
      : (span.to ? store.durText(span.to - span.from) : ''))
    /* Before any node has started there is no graph clock yet, so this falls
       back to the call's -- which is the right answer for exactly that window. */
    : (c.done ? (c.ms ? store.durText(c.ms) : '') : '…')
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
  const shape = nodes.length ? dag.shape(nodes) : ''
  /* The line the graph was dispatched with, and nothing else. The shape used to
     ride along here and now only sits one row down, under `scale`, where it
     already was: a model writes a summary that names its own steps, so the row
     read "...four-node DAG - 4 nodes - 3 layers - at most 2 at once" and said
     the same thing twice before running out of room.

     `c.runTitle` over `c.label` where they differ: a playbook load's label is
     the playbook's directory name, and the graph's own line is what running it
     dispatched. They are the same string for a model-composed graph. */
  const rowLabel = c.runTitle || c.label || shape || t('gui.deleg.dag_title')
  /* In full, because the row above truncates it. `.wrow .ar` is a one-line
     ellipsis with no `title` attribute, so a summary longer than the row is
     readable nowhere else on the card. Unguarded on purpose: the row IS
     `c.runTitle` whenever there is one, so any comparison against it is
     vacuous, and the duplication is the point -- one copy is legible. */
  if (c.runTitle) kv('task', t('gui.deleg.d_task'), c.runTitle)
  kv('scale', t('gui.deleg.d_scale'),
    [shape, ...(agents.length ? [agents.join(' · ')] : [])].filter(Boolean).join(' · ')
    || c.runTitle || c.label || t('gui.deleg.dag_title'))
  /* Which playbook ran, once the row above it stopped being the place for it.
     A playbook's name is its directory -- how it is addressed, edited and
     re-run -- so it is worth being able to read; what it is not is a
     description of what running it dispatched, which is what the row says. */
  if (c.name === 'load_playbook' && c.label && c.label !== c.runTitle) {
    kv('book', t('gui.dag.playbook'), <span className="nid">{c.label}</span>)
  }
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
  /* Only when there is something to say. Both clocks here can come up empty --
     a call that finished carrying no duration, and a graph that stopped
     without leaving an end stamp -- and a labelled row with an empty value
     cell reads as a broken render rather than as an absent number. */
  if (cost) kv('cost', t('gui.deleg.d_cost'), cost)
  /* The run's own id, which is what `dag.get` is keyed by, what a node's record
     lives under and what a later graph names to depend on this one. Last,
     because it is the one field here nobody reads unless they went looking. */
  if (c.runId) kv('run', t('gui.dag.run_id'), <span className="nid">{c.runId}</span>)
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
          {drawn ? <DagGraph active={c.open} dims={dag.CARD} nodes={c.nodes} now={Date.now()}
            surface="card" selectedId={selId} stopPropagation
            onPick={(n) => store.pickDagNode(lane, c, n.id)} /> : null}
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
          {wkinHidden ? null : seg.calls.map((c) => <CallRow key={c.id} lane={lane} seg={seg} c={c} />)}
        </div>
      </div>
    </div>
  )
})

/* ── the other segments ────────────────────────────────────────────────── */

const AskView = memo(function AskView({ lane, seg }: { lane: Lane; seg: AskData }): ReactElement {
  useSeg(lane, seg)
  const bRef = useRef<HTMLDivElement | null>(null)
  const imgs = seg.atts.filter((p) => attachmentCache.get(String(p)))
  const docs = seg.atts.filter((p) => !attachmentCache.get(String(p)))
  const openAll = (): void => store.expandAskAtts(lane, seg)
  const thumb = (p: string, liveImg: boolean): ReactElement => {
    const src = attachmentCache.get(String(p)) as string
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

const AnswerFoot = memo(function AnswerFoot({ lane, seg }: { lane: Lane; seg: AnswerData }): ReactElement {
  const branch = store.branchOf(lane)
  return (
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
  )
})

const AnswerView = memo(function AnswerView({ lane, seg, showFoot = true }: {
  lane: Lane; seg: AnswerData; showFoot?: boolean
}): ReactElement {
  useSeg(lane, seg)
  const branch = store.branchOf(lane)
  const typing = seg.shown != null && seg.shown < seg.text.length
  const html = seg.shown != null
    ? store.mdHtml(seg.text.slice(0, seg.shown)) + (typing ? '<span class="caret"></span>' : '')
    : store.mdHtml(seg.text)
  const items = (): Array<{ label: string; fn: () => void }> => {
    const list = [{
      label: t('gui.answer.copy'),
      fn: () => copy(seg.text, t('gui.answer.copied')),
    }]
    if (branch) list.push({ label: t('gui.answer.branch'), fn: () => branch(seg.text) })
    return list
  }
  return (
    <div className="answer in" ref={ctxRef(items)}>
      <div className="prose" dangerouslySetInnerHTML={{ __html: html }} />
      {showFoot ? <AnswerFoot lane={lane} seg={seg} /> : null}
    </div>
  )
})

const NoteView = memo(function NoteView({ lane, seg }: { lane: Lane; seg: NoteData }): ReactElement {
  useSeg(lane, seg)
  const brief = store.firstErrLine(seg.detail, 140) || seg.detail
  const full = seg.detail ? `${seg.label} · ${seg.detail}` : seg.label
  const items = (): Array<{ label: string; fn: () => void }> => [
    { label: t('gui.answer.copy'), fn: () => copy(full, t('gui.answer.copied')) },
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
  /* The row says a result came back and opens the run it came from; the fold
     holds what came back. Folded, because the retelling right below it is what
     the reader is meant to read -- the delivered text is the receipt, there to
     be checked against, and it is the sub-agent's words rather than Raven's. */
  const headRef = useRef<HTMLButtonElement | null>(null)
  const flip = (): void => pinRow(headRef.current, () => store.toggleDelivered(lane, seg))
  return (
    <div className={'sdlv' + (seg.err ? ' err' : '') + (seg.shown ? ' open' : '')}>
      <div className="sdhd">
        <Ico d={SDLV_ICO} cls="ic" />
        <button className="nm" onClick={seg.open}>{seg.isDag ? t('gui.deleg.dag_title') : seg.label}</button>
        <span className="tx">{t(seg.err ? 'gui.deleg.delivered_err' : 'gui.deleg.delivered')}</span>
        {seg.body ? (
          <button ref={headRef} className="sdcv" onClick={flip}
            aria-label={t('gui.deleg.body_aria')}
            aria-expanded={String(seg.shown) as 'true' | 'false'}>
            <span className="lb">{t('gui.deleg.body')}</span>
            <Chev />
          </button>
        ) : null}
      </div>
      {seg.body ? (
        <div className="sdbd" hidden={!seg.shown}>
          <div className="prose" dangerouslySetInnerHTML={{ __html: store.mdHtml(seg.body) }} />
        </div>
      ) : null}
    </div>
  )
})

/* ── the turn's products ───────────────────────────────────────────────
   A turn that wrote files ends with them. As tiles rather than a list of
   names, because four documents out of one turn read as four identical names
   and as four different miniatures -- that difference is what the extra
   height buys. */

const CHANGE_CAP = 4
/* Deliveries are capped by a STATED count, like the changes below them. It
   used to be whatever fitted one row, measured by an observer that starts at
   one -- so a full-width card could sit claiming it had no space for a second
   tile until something happened to resize it, which is how three deliveries
   came to show two. Three is one row at the reading column and three rows in a
   desk pane; past that the card asks before it grows. */
const DELIVERY_CAP = 3
/* How many of the file's own lines a miniature draws. More than fills the
   tile at this scale; the rest would be rendered and then clipped. */
const ART_HEAD_LINES = 16
/* Enough of the file to fill the miniature, for the files whose opening lines
   have to be READ rather than remembered (see useDeliveryHead). */
const HEAD_BYTES = 4096
/* Which kinds draw themselves as text. `bin` and the ones with their own
   picture (an image, a shot) are not on it, and `html` is deliberately absent:
   its source is markup, and a miniature of the markup is not a miniature of
   the page. */
const HEAD_KINDS = new Set(['md', 'code', 'csv', 'json', 'diff'])

/* The file's own first lines, read from the same URL the tile already probes.

   The workspace record answers this for free when the MAIN agent wrote the
   file -- a write tool's hunk IS what it wrote. Nothing answers it for a file
   a playbook or a sub-agent produced: that write happened on another lane, so
   this session's workspace holds no change for it and the tile fell back to a
   grey "MD" square for the one product the turn was about. One ranged GET
   costs less than the HEAD probe it rides beside. */
function useDeliveryHead(url: string, want: boolean): string | null {
  const [head, setHead] = useState<string | null>(null)
  useEffect(() => {
    if (!want) {
      setHead(null)
      return
    }
    let alive = true
    fetch(url, {
      credentials: 'same-origin',
      cache: 'no-store',
      headers: { Range: `bytes=0-${HEAD_BYTES - 1}` },
    })
      .then((res) => (res.ok ? res.text() : ''))
      /* Sliced again on this side: a server that ignores Range answers with
         the whole file, and the miniature wants sixteen lines of it. */
      .then((text) => { if (alive) setHead(text.slice(0, HEAD_BYTES) || null) })
      .catch(() => { if (alive) setHead(null) })
    return () => { alive = false }
  }, [url, want])
  return head
}

/* Markdown goes through the renderer the full-size viewer uses, and the
   result is SCALED by the stylesheet. Not set in a smaller font: shrinking
   the type re-wraps every line and stops a heading from being a heading, so
   what the tile showed would be a different document from the one it names. */
const ArtMini = memo(function ArtMini({ name, head }: { name: string; head: string | null }): ReactElement {
  if (!head) return <span className="pic none"><Ico d={ACT_ICO.doc as string} cls="fi" /></span>
  const text = head.split('\n').slice(0, ART_HEAD_LINES).join('\n')
  if (fileKind(name) === 'md') {
    return (
      <span className="pic doc">
        <span className="amini prose" dangerouslySetInnerHTML={{ __html: store.mdHtml(text) }} />
      </span>
    )
  }
  return <span className="pic doc"><span className="amini"><span className="raw">{text}</span></span></span>
})

const DeliveryShot = memo(function DeliveryShot({ row, missing }: {
  row: DeliveryRow; missing: () => void
}): ReactElement {
  const [ready, setReady] = useState(false)
  return (
    <span className={'pic shot' + (ready ? '' : ' skel')}>
      <img src={row.downloadPath} alt="" loading="lazy" decoding="async"
        onLoad={() => setReady(true)} onError={missing} />
      {ready ? null : <span className="sk" />}
    </span>
  )
})

const DeliveryTile = memo(function DeliveryTile({ row, preview, single }: {
  row: DeliveryRow; preview: ArtifactRow | null; single: boolean
}): ReactElement {
  const [state, setState] = useState<'probe' | 'ready' | 'missing'>(row.missing ? 'missing' : 'probe')
  const url = row.downloadPath
  useEffect(() => {
    let alive = true
    if (row.missing) {
      setState('missing')
      return () => { alive = false }
    }
    setState('probe')
    fetch(url, { method: 'HEAD', credentials: 'same-origin', cache: 'no-store' })
      .then((res) => { if (alive) setState(res.ok ? 'ready' : 'missing') })
      .catch(() => { if (alive) setState('missing') })
    return () => { alive = false }
  }, [row.missing, url])
  const kind = fileKind(row.name)
  /* Only when the workspace has no hunk to draw from, and only once the probe
     says the file is there: a miss would just be a second 404. */
  const fetched = useDeliveryHead(url, state === 'ready' && !preview?.head && HEAD_KINDS.has(kind))
  const type = row.ext ? row.ext.toUpperCase() : t('gui.arts.file')
  const fallback = (
    <span className="pic none">
      <Ico d={ACT_ICO.doc as string} cls="fi" />
      <span className="ft">{type}</span>
    </span>
  )
  const picture = state === 'probe' ? (
    <span className="pic none skel"><span className="sk" /></span>
  ) : state === 'missing' ? (
    fallback
  ) : kind === 'img' || kind === 'svg' ? (
    <DeliveryShot row={row} missing={() => setState('missing')} />
  ) : preview?.head ? <ArtMini name={row.name} head={preview.head} />
    : fetched ? <ArtMini name={row.name} head={fetched} /> : (
      fallback
    )
  const meta = state === 'missing'
    ? t('gui.arts.missing')
    : [type, humanSize(row.size)].filter(Boolean).join(' \u00b7 ')
  return (
    <div className={'atile' + (state === 'missing' ? ' missing' : '')} title={row.path}>
      {picture}
      <span className="cap">
        <span className="nm">{row.title}</span>
        {row.description ? <span className="ds">{row.description}</span> : null}
        <span className="mt">{meta}</span>
      </span>
      <button className="hit" disabled={state !== 'ready'}
        aria-label={t('gui.arts.open', { f: row.name })}
        onClick={() => wsOpenDelivery(row.path, row.downloadPath)} />
    </div>
  )
})


const ArtsView = memo(function ArtsView({ lane, seg }: { lane: Lane; seg: ArtsData }): ReactElement | null {
  useSeg(lane, seg)
  const changes = store.artifactsOf(lane, seg.turn)
  const deliveries = store.deliveriesOf(lane, seg.turn)
  if (!changes.length && !deliveries.length) return null
  const shownDeliveries = seg.deliveriesOpen ? deliveries : deliveries.slice(0, DELIVERY_CAP)
  const deliveryRest = deliveries.length - shownDeliveries.length
  const shownChanges = seg.changesOpen ? changes : changes.slice(0, CHANGE_CAP)
  const changeRest = changes.length - shownChanges.length
  const previews = new Map(changes.map((row) => [row.path, row]))
  return (
    <div className="arts">
      {deliveries.length ? <section className="asec deliveries">
        <div className="ahd">
          <span className="ahm"><span className="lb">{t('gui.arts.delivered')}</span><span className="n">{deliveries.length}</span></span>
        </div>
        <div className={'atiles' + (deliveries.length === 1 ? ' single' : '')}>
          {shownDeliveries.map((row) => <DeliveryTile key={row.path} row={row}
            preview={previews.get(row.path) || null} single={deliveries.length === 1} />)}
        </div>
        {deliveryRest > 0 || seg.deliveriesOpen ? <button className="amore"
          aria-expanded={seg.deliveriesOpen} onClick={() => store.toggleArts(lane, seg, 'deliveries')}>
          {seg.deliveriesOpen ? t('gui.arts.less') : t('gui.arts.more', { n: String(deliveryRest) })}
        </button> : null}
      </section> : null}
      {changes.length ? <section className="asec changes">
        <div className="ahd">
          <span className="ahm"><span className="lb">{t('gui.arts.changed')}</span><span className="n">{changes.length}</span></span>
        </div>
        <div className="achanges">
          {shownChanges.map((row) => <button key={row.path} className="achange" onClick={() => wsOpenPath(row.path)}>
            <span className={'ck ' + row.change}>{t(row.change === 'new' ? 'gui.arts.new' : 'gui.arts.edit')}</span>
            <span className="cn">{row.name}</span>
            <span className="ct">{row.ext ? row.ext.toUpperCase() : t('gui.arts.file')}</span>
            <span className="ca">+{row.lines}</span>
            <span className="cd">{'\u2212'}{row.deleted}</span>
          </button>)}
        </div>
        {changeRest > 0 || seg.changesOpen ? <button className="amore"
          aria-expanded={seg.changesOpen} onClick={() => store.toggleArts(lane, seg, 'changes')}>
          {seg.changesOpen ? t('gui.arts.less') : t('gui.arts.more', { n: String(changeRest) })}
        </button> : null}
      </section> : null}
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
      {/* Every turn of a resumed conversation arrives shut, so this is where
          the weight was: a forty-turn session built 7361 nodes of which 6400
          sat in shut fold bodies. */}
      <div className="tfb" hidden={!seg.open}>
        {seg.open ? seg.steps.map((s) => <StepView key={s.id} lane={lane} seg={s} />) : null}
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

function stageRows(lane: Lane): ReactElement[] {
  const rows: ReactElement[] = []
  for (let i = 0; i < lane.segs.length; i += 1) {
    const seg = lane.segs[i] as Seg
    if (seg.kind === 'answer') {
      let end = i + 1
      while (end < lane.segs.length && !['ask', 'answer', 'arts'].includes(lane.segs[end]!.kind)) end += 1
      const close = lane.segs[end]
      if (close?.kind === 'arts') {
        rows.push(
          <div className="answer-turn" key={`${lane.epoch}:${seg.id}`}>
            <AnswerView lane={lane} seg={seg} showFoot={false} />
            {lane.segs.slice(i + 1, end).map((middle) => (
              <SegView key={`${lane.epoch}:${middle.id}`} lane={lane} seg={middle} />
            ))}
            <ArtsView lane={lane} seg={close} />
            <AnswerFoot lane={lane} seg={seg} />
          </div>,
        )
        i = end
        continue
      }
    }
    rows.push(<SegView key={`${lane.epoch}:${seg.id}`} lane={lane} seg={seg} />)
  }
  return rows
}

export function StageView({ lane }: { lane: Lane }): ReactElement {
  useSyncExternalStore((cb) => store.subscribe(lane, cb), () => lane.listV)
  const req = useSyncExternalStore((cb) => store.subscribe(lane, cb), () => lane.scrollReq)
  /* Tail-follow after the commit, so the new height is the one measured. */
  useEffect(() => {
    if (lane.main) tail.down()
  }, [lane, req])
  return (
    <>
      {stageRows(lane)}
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
