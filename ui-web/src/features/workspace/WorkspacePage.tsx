import { useEffect, useState, useSyncExternalStore } from 'react'

import { shell, t } from '../../shell/bridge'
import { show as menuAt } from '../../shell/menu'
import { show as toast } from '../../shell/toast'
import {
  RENDERED, appFor, canOpenInApp, copyToClip, extOf, fileURL,
  hostPlatform, mdHtml, openInApp, setAppFor,
} from './store'
import * as deliveries from './deliveries'
import * as store from './store'

import type { MenuItem } from '../../shell/menu'
import type { WsChange, WsFile, WsShared } from './types'
import type { JSX, PointerEvent as ReactPointerEvent } from 'react'

/* Copies of the icon paths the legacy renderers drew with (ICO in
   demo/100-workspace.js, ACT_ICO.chev in demo/070-transcript.js): those
   tables stay in the demo shell for the views that never left it, and the
   island carries its own strings the same way the cron island carries its
   FREQ table. */
const ICO = {
  diff: 'M4 4h16v16H4zM12 8.5v7M8.5 12h7',
  file: 'M4 7.5c0-1.1.9-2 2-2h3.5l2 2.5H18c1.1 0 2 .9 2 2v7c0 1.1-.9 2-2 2H6c-1.1 0-2-.9-2-2v-9.5Z',
  web: 'M4.5 12h15M12 4.5c-4.5 4.5-4.5 10.5 0 15M12 4.5c4.5 4.5 4.5 10.5 0 15',
  ext: 'M10 6H6.5A2.5 2.5 0 0 0 4 8.5v9A2.5 2.5 0 0 0 6.5 20h9a2.5 2.5 0 0 0 2.5-2.5V14M14 4h6v6M20 4l-9 9',
  doc: 'M7 3.5h7L18.5 8v10.5a2 2 0 0 1-2 2h-9a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2ZM13.5 3.5V8h4.5',
  reveal: 'M4 7.5c0-1.1.9-2 2-2h3.5l2 2.5H18c1.1 0 2 .9 2 2v7c0 1.1-.9 2-2 2H6c-1.1 0-2-.9-2-2v-9.5Z'
    + 'M9.5 16l5-4.5M14.5 15V11.5H11',
  chev: 'M9.5 6.5 15 12l-5.5 5.5',
}

const FT_ICO = {
  page: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"'
    + ' aria-hidden="true"><path d="M7 2.8h7L19 8v13.2H7z"/><path d="M13.5 2.8V8H19"/></svg>',
}

function Ico({ d, cls }: { d: string; cls?: string }): JSX.Element {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" aria-hidden="true" className={cls}>
      <path d={d} />
    </svg>
  )
}

/* The app-wide context menu is delegated: a surface declares its actions by
   carrying a `_ctx` items provider and the document-level listener in the
   demo shell does the rest. */
type CtxHost = HTMLElement & { _ctx?: () => MenuItem[] }
const ctxRef = (items: () => MenuItem[]) => (el: HTMLElement | null): void => {
  if (el) (el as CtxHost)._ctx = items
}

export function WsApp(): JSX.Element {
  const s = useSyncExternalStore(store.subscribe, store.getState)
  const ws = store.shared()
  if (s.route === 'launch') return <Launch />
  if (s.route === 'file') return <FileView ws={ws} />
  return <Changes ws={ws} />
}

const LAUNCH_CARDS: Array<[string, string, string, string]> = [
  ['diff', ICO.diff, 'gui.ws.changes', 'gui.ws.sub.changes'],
  ['browser', ICO.web, 'gui.ws.browser', 'gui.ws.sub.browser'],
]

function Launch(): JSX.Element {
  return (
    <div className="wslaunch">
      {LAUNCH_CARDS.map(([tab, d, name, sub]) => (
        <button key={tab} className="lcard" onClick={() => store.pick(tab)}>
          <Ico d={d} />
          <span className="t">
            {t(name)}
            <small>{t(sub)}</small>
          </span>
        </button>
      ))}
    </div>
  )
}

function Changes({ ws }: { ws: WsShared }): JSX.Element {
  /* The flash class rides one render and the flag drops once painted, so
     the next arrival can raise it again -- the legacy renderer cleared it
     while rebuilding the row. */
  useEffect(() => {
    ws.changes.forEach((c) => { c.flash = false })
  })
  if (!ws.changes.length) return <div className="wsnote">{t('gui.ws.no_changes')}</div>
  let group: string | null = null
  const out: JSX.Element[] = []
  ws.changes.forEach((c) => {
    const g = c.turn === ws.turn ? 'gui.ws.turn_now' : 'gui.ws.turn_earlier'
    if (g !== group) {
      group = g
      out.push(<div key={`grp:${g}:${c.key}`} className="wsgrp">{t(g)}</div>)
    }
    out.push(<ChgRow key={`${c.key}:${c.turn}`} c={c} />)
  })
  return <>{out}</>
}

function chgItems(c: WsChange): MenuItem[] {
  return [
    { label: t('gui.ws.open'), fn: () => store.openPath(c.key) },
    { label: t('gui.ws.copy_path_do'), fn: () => copyToClip(c.key, t('gui.ws.copy_path')) },
  ]
}

function ChgRow({ c }: { c: WsChange }): JSX.Element {
  const toggle = (): void => {
    c.open = !c.open
    c.auto = false
    store.redraw()
  }
  return (
    <div className={'chg' + (c.flash ? ' flash' : '')}>
      <div
        className="chghd"
        role="button"
        tabIndex={0}
        aria-expanded={c.open}
        onClick={toggle}
        onKeyDown={(e) => {
          if (e.key !== 'Enter' && e.key !== ' ') return
          e.preventDefault()
          toggle()
        }}
        ref={ctxRef(() => chgItems(c))}
      >
        <i className={'chgc ' + c.kind} title={t('gui.ws.chip.' + c.kind + '_t')}>{t('gui.ws.chip.' + c.kind)}</i>
        <span className="chgp" title={c.key}>
          {c.dir ? <span className="dir">{c.dir}</span> : null}
          {c.name}
        </span>
        <span className="chgs">
          {c.add ? <span className="a">{`+${c.add}`}</span> : null}
          {c.del ? <span className="d">{`−${c.del}`}</span> : null}
        </span>
        <button
          className="chgm"
          aria-label={t('gui.ws.actions')}
          onClick={(e) => {
            e.stopPropagation()
            const r = e.currentTarget.getBoundingClientRect()
            menuAt(r.left, r.bottom + 6, chgItems(c))
          }}
        >
          {'⋯'}
        </button>
      </div>
      {c.open ? <ChgDiff c={c} /> : null}
    </div>
  )
}

function DLine({ numbered, kind, text, oldNo, newNo }: {
  numbered: boolean
  kind: string
  text: string
  oldNo?: number | null
  newNo?: number | null
}): JSX.Element {
  if (!numbered) return <div className={'dl ' + kind}>{text === '' ? ' ' : text}</div>
  /* The line's number in the file it still exists in: the new one for added
     and unchanged lines, the old one for a line that was deleted. */
  const no = newNo == null ? oldNo : newNo
  return (
    <div className={'dl ' + kind + ' num'}>
      <i className="lno">{no == null ? '' : String(no)}</i>
      <b className="sg">{kind === 'add' ? '+' : kind === 'del' ? '−' : ''}</b>
      <span>{text === '' ? ' ' : text}</span>
    </div>
  )
}

export function ChgDiff({ c }: { c: WsChange }): JSX.Element {
  /* One gutter decision per file, not per hunk: a mixed card must not
     zigzag its left edge between the two layouts. */
  const numbered = c.hunks.some((h) => h.rows.some((r) => r.length > 2))
  const out: JSX.Element[] = []
  c.hunks.forEach((h, hi) => {
    if (hi) out.push(<div key={`hs${hi}`} className="hsep" />)
    h.rows.forEach((r, ri) => {
      const key = `${hi}:${ri}`
      if (r[0] === 'gap') {
        const lines = r[1] as string[]
        out.push(
          <button
            key={key}
            className="gap"
            onClick={(e) => {
              e.stopPropagation()
              r.open = !r.open
              store.redraw()
            }}
          >
            {r.open
              ? `··· ${t('gui.ws.fold_lines', { n: lines.length })} ···`
              : `··· ${t('gui.ws.expand_lines', { n: lines.length })} ···`}
          </button>,
        )
        if (r.open) lines.forEach((l, li) => out.push(<DLine key={`${key}:${li}`} numbered={numbered} kind="ctx" text={l} />))
        return
      }
      if (r[0] === 'hunk') {
        if (ri) out.push(<div key={key} className="hsep" />)
        return
      }
      out.push(<DLine key={key} numbered={numbered} kind={r[0]} text={r[1] as string} oldNo={r[2]} newNo={r[3]} />)
    })
  })
  return <div className="diff">{out}</div>
}

/* ── the file view ─────────────────────────────────────────────────── */

/* Applications whose names the host knows, as a starting list rather than a
   registry: `open -a` and a bare Linux launcher both want a name, and a name
   this list does not have has nowhere to be typed yet -- that belongs with the
   settings row, and is not in this change. Windows has no portable way to name
   an application, so it gets the host default only, which is what fs.open
   sends there. */
const OPEN_WITH: Record<string, string[]> = {
  mac: ['Cursor', 'Visual Studio Code', 'Xcode', 'Keynote', 'Numbers', 'Pages', 'Preview', 'TextEdit'],
  linux: ['cursor', 'code', 'libreoffice', 'gedit'],
  windows: [],
}

/* A file the page cannot render. It says so, and offers the two things that
   can still be done with it: hand it to an application, or show it in the file
   manager. Both of those run where the GATEWAY runs, so the first is withheld
   unless that host is this desktop -- a remote serve would start a program on
   somebody else's screen. Reveal was already here and keeps its own behaviour. */
function BinNote({ f }: { f: WsFile }): JSX.Element {
  const chosen = appFor(f.path)
  const canApp = canOpenInApp()
  /* Subscribed, not read once. `loadDeliveries()` is fire-and-forget on a
     session reopen while `resume()` can restore this pane first, so the row
     this note needs can arrive AFTER it mounts -- and `seed()` notifies only
     this store's listeners. Reading `byPath` once leaves the save link absent
     for good in that order, which strands exactly the reader it exists for:
     a remote gateway, where the open-with offer is withheld and the path is
     on somebody else's machine. */
  useSyncExternalStore(deliveries.subscribe, deliveries.getVersion)
  const saveAt = deliveries.byPath(f.path)?.downloadPath || ''
  const hand = (app: string | null): void => {
    void openInApp(f.path, app).then(
      () => {},
      (e: unknown) => toast(((e as Error) && (e as Error).message) || String(e)),
    )
  }
  const pick = (e: ReactPointerEvent<HTMLButtonElement>): void => {
    const r = (e.currentTarget as HTMLElement).getBoundingClientRect()
    const names = OPEN_WITH[hostPlatform()] || []
    const items: Array<MenuItem | '-'> = names.map((name) => ({
      label: name === chosen ? `${name} \u00b7 ${t('gui.ws.open_with_now')}` : name,
      /* Remember the choice for this kind, then act on it -- the reader picked
         an application for pptx, not for this one file. */
      fn: () => { setAppFor(f.path, name); hand(name) },
    }))
    if (names.length) items.push('-')
    items.push({
      label: t('gui.ws.open_with_default'),
      fn: () => { setAppFor(f.path, null); hand(null) },
    })
    menuAt(r.left, r.bottom + 6, items)
  }
  return (
    <div className="binote">
      <div className="h">{t('gui.ws.file_binary')}</div>
      <div className="w">{f.path}</div>
      {canApp ? (
        <div className="binacts">
          <button className="mini ghost" onClick={() => hand(chosen)}>
            {chosen
              ? t('gui.ws.open_with_app', { a: chosen })
              : t('gui.ws.open_with_host', { k: extOf(f.path).toUpperCase() })}
          </button>
          <button className="mini ghost" onPointerUp={pick}>{t('gui.ws.open_with_pick')}</button>
        </div>
      ) : null}
      {/* The one place a download still belongs. This note is what a file the
          viewer cannot render gets, and on a gateway that is not this desktop
          the offer above is withheld -- so without this the reader is left with
          a path on somebody else's machine and no way to reach the bytes. Read
          from the registry by path rather than carried on the pane: the same
          file is one pane whichever door opened it, and only a file this
          session delivered has a URL to serve it from. */}
      {saveAt ? (
        <a className="mini ghost" href={saveAt} download={f.path.split('/').pop() || ''}>
          {t('gui.ws.save_copy')}
        </a>
      ) : null}
      <button
        className="mini ghost"
        onClick={() => {
          if (navigator.clipboard) void navigator.clipboard.writeText(f.path)
        }}
      >
        {t('gui.ws.copy_path_do')}
      </button>
    </div>
  )
}

/* One file, no tree beside it. What the viewer opens comes from the transcript,
   the diff list or the deliverables shelf -- nothing navigates from inside it,
   which is why there is nothing left here to navigate with. */
export function FileView({ ws, file = ws.file }: { ws: WsShared; file?: WsFile | null }): JSX.Element {
  if (!store.source().canBrowse) return <div className="wsnote">{t('gui.ws.file_unreadable')}</div>
  const f = file
  return (
    <div className="fwrap">
      <Fbar f={f} />
      <div className="fpane">
        {!f ? (
          <div className="fempty">
            <div dangerouslySetInnerHTML={{ __html: FT_ICO.page }} />
            <div className="t">{t('gui.ws.file_none')}</div>
          </div>
        ) : (
          <div className="fbody">
            <FileBody key={f.seq ?? f.path} f={f} />
          </div>
        )}
      </div>
    </div>
  )
}

function Fbar({ f }: { f: WsFile | null }): JSX.Element {
  const platform = hostPlatform()
  const revealTip = t(platform === 'mac' ? 'gui.ws.reveal_finder'
    : platform === 'windows' ? 'gui.ws.reveal_explorer' : 'gui.ws.reveal_folder')
  const rel = f ? store.source().shortPath(f.path) : ''
  const cut = rel.lastIndexOf('/')
  return (
    <div className="fbar">
      {f ? (
        <>
          <span
            className="nm"
            title={f.path}
            ref={ctxRef(() => [
              { label: t('gui.ws.copy_path_do'), fn: () => copyToClip(f.path, t('gui.ws.copy_path')) },
            ])}
          >
            {cut > 0 ? <i>{rel.slice(0, cut + 1)}</i> : null}
            <b>{cut > 0 ? rel.slice(cut + 1) : rel}</b>
          </span>
          <button
            className="ghost-ic fcopy tipdn"
            data-tip={t('gui.ws.copy_path_do')}
            aria-label={t('gui.ws.copy_path_do')}
            onClick={() => {
              if (navigator.clipboard) {
                navigator.clipboard.writeText(f.path).then(() => toast(t('gui.ws.copy_path')), () => {})
              }
            }}
          >
            <Ico d={ICO.doc} />
          </button>
        </>
      ) : (
        <span className="nm">{t('gui.ws.file_none')}</span>
      )}
      <span className="fsp" />
      {f && RENDERED[f.kind] ? (
        <div className="kseg">
          {([[false, 'gui.ws.file_rendered'], [true, 'gui.ws.file_source']] as Array<[boolean, string]>).map(([raw, key]) => (
            <button
              key={key}
              aria-pressed={Boolean(f.raw) === raw}
              onClick={() => {
                f.raw = raw
                store.redraw()
              }}
            >
              {t(key)}
            </button>
          ))}
        </div>
      ) : null}
      {f && (f.kind === 'pdf' || f.kind === 'html') ? (
        <button
          className="ghost-ic tipdn"
          data-tip={t('gui.ws.file_newtab')}
          aria-label={t('gui.ws.file_newtab')}
          onClick={() => window.open(fileURL(f.path), '_blank', 'noopener')}
        >
          <Ico d={ICO.ext} />
        </button>
      ) : null}
      {f ? (
        <button
          className="ghost-ic tipdn"
          data-tip={revealTip}
          aria-label={revealTip}
          onClick={() => {
            store.source().reveal?.(f.path).then(() => {}, (e: unknown) =>
              toast(((e as Error) && (e as Error).message) || String(e)))
          }}
        >
          <Ico d={ICO.reveal} />
        </button>
      ) : null}
    </div>
  )
}

/* ── the file body ─────────────────────────────────────────────────── */

/* A patch is one of the few formats whose lines carry their meaning in the
   first character; without colour it reads as noise with plus signs. */
function diffLineCls(line: string): string {
  if (/^(\+\+\+|---)/.test(line)) return ' dmeta'
  if (line[0] === '+') return ' dadd'
  if (line[0] === '-') return ' ddel'
  if (line.startsWith('@@')) return ' dhunk'
  if (/^(diff |index |new file|deleted file|similarity |rename |Binary )/.test(line)) return ' dmeta'
  return ''
}

function FileBody({ f }: { f: WsFile }): JSX.Element {
  const [broken, setBroken] = useState(false)
  const asSource = f.raw || !RENDERED[f.kind]
  const asImage = f.kind === 'img' || (f.kind === 'svg' && !asSource)
  const asFrame = (f.kind === 'pdf' || f.kind === 'html') && !asSource
  const wantsText = !asImage && !asFrame && f.kind !== 'bin'
  useEffect(() => {
    if (wantsText && !f.err && f.text == null && !f.loading) void store.loadFileText(f)
  })
  let body: JSX.Element
  if (f.err) {
    body = <div className="verr">{f.err}</div>
  } else if (asImage) {
    body = (
      <div className="shot">
        {broken
          ? <div className="verr">{t('gui.ws.file_gone')}</div>
          : <img src={fileURL(f.path)} alt={f.path}
            /* The picture kinds never read text, so this is the only place they
               can find out the file is gone -- but the error itself does not say
               that, which is why the probe asks for a status first. */
            onError={() => { setBroken(true); void store.probeDeliveryMissing(f.path) }} />}
      </div>
    )
  } else if (asFrame) {
    /* No sandbox attribute on a PDF frame: Chromium refuses its PDF viewer
       inside any sandboxed frame -- the request is answered 200 and then
       blocked by the client, so the pane stayed a grey box with a sad face,
       whatever tokens the attribute granted (allow-scripts included). The
       viewer runs in its own extension origin, so the frame being same-origin
       hands the document nothing of the page's. HTML keeps the empty sandbox:
       a report the agent wrote is readable without running its scripts. */
    body = f.kind === 'pdf'
      ? <iframe referrerPolicy="no-referrer" src={fileURL(f.path)} />
      : <iframe sandbox="" referrerPolicy="no-referrer" src={fileURL(f.path)} />
  } else if (f.kind === 'bin') {
    body = <BinNote f={f} />
  } else if (f.text == null) {
    body = <div className="vspin">{t('gui.ws.file_loading')}</div>
  } else if (f.kind === 'md' && !asSource) {
    body = <div className="prose" dangerouslySetInnerHTML={{ __html: mdHtml(f.text) }} />
  } else if (f.kind === 'csv' && !asSource) {
    body = <CsvTable text={f.text} tab={/\.tsv$/i.test(f.path)} />
  } else {
    const parsed = f.kind === 'json' && !asSource ? parseJsonCapped(f.text) : null
    body = parsed ? <JsonView v={parsed.v} /> : <CodeLines text={f.text} kind={f.kind} />
  }
  return <div className="fview">{body}</div>
}

function CodeLines({ text, kind }: { text: string; kind: string }): JSX.Element {
  return (
    <div className="code">
      {text.split('\n').map((line, i) => (
        <div key={i} className={'ln' + (kind === 'diff' ? diffLineCls(line) : '')}>
          <i>{String(i + 1)}</i>
          <span>{line || ' '}</span>
        </div>
      ))}
    </div>
  )
}

function CsvTable({ text, tab }: { text: string; tab: boolean }): JSX.Element {
  const rows = text.replace(/\r/g, '').split('\n').filter((l) => l.length).slice(0, 500)
    .map((l) => l.split(tab ? '\t' : ','))
  return (
    <div className="csvw">
      <table className="csvt">
        <tbody>
          {rows.map((cells, i) => (
            <tr key={i}>
              {cells.map((c, j) => (i === 0 ? <th key={j}>{c}</th> : <td key={j}>{c}</td>))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/* ── json: a foldable tree ────────────────────────────────────────────
   Native <details> does the folding, so there is no open-state to manage
   and the keyboard works for free. A node budget keeps a machine-dumped
   megabyte from freezing the tab. */
const JSON_NODE_BUDGET = 4000

function parseJsonCapped(text: string): { v: unknown } | null {
  if (text.length > 2 * 1024 * 1024) return null
  try {
    return { v: JSON.parse(text) }
  } catch {
    /* A file that does not parse is still a file: fall through to the plain
       numbered lines rather than showing an error for something readable. */
    return null
  }
}

function JsonLeaf({ v }: { v: unknown }): JSX.Element {
  const cls = v === null ? 'jnull' : typeof v === 'string' ? 'jstr' : typeof v === 'number' ? 'jnum' : 'jbool'
  const text = v === null ? 'null' : typeof v === 'string' ? JSON.stringify(v) : String(v)
  return <span className={'jv ' + cls}>{text}</span>
}

function jsonNodeEl(v: unknown, key: string | number | null, state: { left: number }, depth: number, k: string): JSX.Element {
  state.left -= 1
  const keyBit = key !== null
    ? (
        <>
          <span className="jk">{typeof key === 'number' ? String(key) : JSON.stringify(key)}</span>
          <span className="jc">:</span>
        </>
      )
    : null
  if (v === null || typeof v !== 'object') {
    return (
      <div key={k} className="jrow">
        {keyBit}
        <JsonLeaf v={v} />
      </div>
    )
  }
  const isArr = Array.isArray(v)
  const entries: Array<[string | number, unknown]> = isArr
    ? (v as unknown[]).map((x, i) => [i, x] as [number, unknown])
    : Object.entries(v as Record<string, unknown>)
  const kidRows: JSX.Element[] = []
  for (const [ck, child] of entries) {
    if (state.left <= 0) {
      kidRows.push(<div key="cap" className="jrow jmore">{t('gui.ws.json_capped')}</div>)
      break
    }
    kidRows.push(jsonNodeEl(child, ck, state, depth + 1, String(ck)))
  }
  if (!entries.length) kidRows.push(<div key="empty" className="jrow jmore">{isArr ? '[]' : '{}'}</div>)
  return (
    /* The first two levels open by default: that is the shape of the file.
       Below that the reader opens what they are looking for. */
    <details key={k} className="jnode" open={depth < 2}>
      <summary>
        {keyBit}
        <span className="jb">{isArr ? '[' : '{'}</span>
        <span className="jn">{t('gui.ws.json_items', { n: String(entries.length) })}</span>
        <span className="jb">{isArr ? ']' : '}'}</span>
      </summary>
      <div className="jkids">{kidRows}</div>
    </details>
  )
}

function JsonView({ v }: { v: unknown }): JSX.Element {
  const state = { left: JSON_NODE_BUDGET }
  return <div className="jsonv">{jsonNodeEl(v, null, state, 0, 'root')}</div>
}
