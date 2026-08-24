import { Fragment, useEffect, useRef, useState, useSyncExternalStore } from 'react'

import { shell, t } from '../../shell/bridge'
import { show as menuAt } from '../../shell/menu'
import { show as toast } from '../../shell/toast'
import {
  FT, FT_SHOW_MAX, FTW_KEY, RENDERED, appFor, canOpenInApp, copyToClip, extOf, fileURL,
  ftAbs, ftJoin, ftKindOf, ftLoad, ftLoadVisible, ftMatches, ftOpenTo, ftQuery, ftReveal,
  hostPlatform, isErr, mdHtml, openInApp, relToRoot, relToWorkspace, setAppFor,
} from './store'
import * as store from './store'

import type { MenuItem } from '../../shell/menu'
import type { WsChange, WsFile, WsShared } from './types'
import type { CSSProperties, JSX, PointerEvent as ReactPointerEvent, RefObject } from 'react'

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
  download: 'M12 4v11M7.5 10.5 12 15l4.5-4.5M5 19.5h14',
  doc: 'M7 3.5h7L18.5 8v10.5a2 2 0 0 1-2 2h-9a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2ZM13.5 3.5V8h4.5',
  reveal: 'M4 7.5c0-1.1.9-2 2-2h3.5l2 2.5H18c1.1 0 2 .9 2 2v7c0 1.1-.9 2-2 2H6c-1.1 0-2-.9-2-2v-9.5Z'
    + 'M9.5 16l5-4.5M14.5 15V11.5H11',
  chev: 'M9.5 6.5 15 12l-5.5 5.5',
}

const FT_ICO = {
  folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"'
    + ' stroke-linejoin="round" aria-hidden="true"><path d="M3 6.6a2 2 0 0 1 2-2h3.6l1.8 2.2H19a2 2 0 0 1 2 2v8.6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  page: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"'
    + ' aria-hidden="true"><path d="M7 2.8h7L19 8v13.2H7z"/><path d="M13.5 2.8V8H19"/></svg>',
}

/* The attachments part's byte formatter, verbatim -- it lives inside the
   live layer's IIFE where the island cannot reach it. */
const fmtSize = (n?: number): string => (n !== undefined && n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB`
  : n !== undefined && n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`)

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
  ['file', ICO.file, 'gui.ws.files', 'gui.ws.sub.files'],
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
        <div className="acts">
          <button className="mini ghost" onClick={() => hand(chosen)}>
            {chosen
              ? t('gui.ws.open_with_app', { a: chosen })
              : t('gui.ws.open_with_host', { k: extOf(f.path).toUpperCase() })}
          </button>
          <button className="mini ghost" onPointerUp={pick}>{t('gui.ws.open_with_pick')}</button>
        </div>
      ) : null}
      {f.downloadPath ? <a className="mini ghost" href={f.downloadPath}>{t('gui.ws.download')}</a> : null}
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

export function FileView({
  ws,
  file = ws.file,
  onOpen = store.showFile,
}: {
  ws: WsShared
  file?: WsFile | null
  onOpen?: (path: string) => void
}): JSX.Element {
  const wrapRef = useRef<HTMLDivElement>(null)
  if (!store.source().canBrowse) return <div className="wsnote">{t('gui.ws.dir_empty')}</div>
  const f = file
  return (
    <div
      className="fwrap"
      ref={wrapRef}
      data-tree={FT.hide ? 'off' : 'on'}
      style={{ '--ftw': FT.w + 'px' } as CSSProperties}
    >
      <Fbar f={f} />
      <div className="frow">
        <FtPane selectedPath={f?.path || ''} onOpen={onOpen} />
        <Grip wrapRef={wrapRef} />
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
    </div>
  )
}

function Fbar({ f }: { f: WsFile | null }): JSX.Element {
  const treeTip = t(FT.hide ? 'gui.ws.tree_show' : 'gui.ws.tree_hide')
  const platform = hostPlatform()
  const revealTip = t(platform === 'mac' ? 'gui.ws.reveal_finder'
    : platform === 'windows' ? 'gui.ws.reveal_explorer' : 'gui.ws.reveal_folder')
  const rel = f ? store.source().shortPath(f.path) : ''
  const cut = rel.lastIndexOf('/')
  return (
    <div className="fbar">
      <button
        className="ghost-ic ftog tipdn"
        data-tip={treeTip}
        aria-label={treeTip}
        aria-pressed={!FT.hide}
        onClick={() => {
          FT.hide = !FT.hide
          store.redraw()
        }}
        dangerouslySetInnerHTML={{ __html: FT_ICO.folder }}
      />
      {f ? (
        <>
          <span
            className="nm"
            title={f.path}
            ref={ctxRef(() => [
              { label: t('gui.ws.copy_path_do'), fn: () => copyToClip(f.path, t('gui.ws.copy_path')) },
              { label: t('gui.ws.reveal'), fn: () => ftReveal(relToRoot(f.path) || relToWorkspace(f.path) || f.path, false) },
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
      {f?.downloadPath ? (
        <a className="ghost-ic tipdn" data-tip={t('gui.ws.download')} aria-label={t('gui.ws.download')}
          href={f.downloadPath}>
          <Ico d={ICO.download} />
        </a>
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

function FtPane({ selectedPath, onOpen }: { selectedPath: string; onOpen: (path: string) => void }): JSX.Element {
  const list = useRef<HTMLDivElement>(null)
  useEffect(() => {
    ftLoadVisible()
  }, [])
  useEffect(() => {
    if (!selectedPath) return
    const full = relToRoot(selectedPath) || relToWorkspace(selectedPath) || selectedPath
    ftOpenTo(full)
  }, [selectedPath])
  useEffect(() => {
    if (!selectedPath) return
    const full = relToRoot(selectedPath) || relToWorkspace(selectedPath) || selectedPath
    requestAnimationFrame(() => {
      const row = list.current?.querySelector<HTMLElement>(`.ftrow[data-p="${CSS.escape(full)}"]`)
      row?.scrollIntoView({ block: 'nearest' })
    })
  })
  return (
    <div className="ftree">
      <div className="ftq">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9" aria-hidden="true">
          <circle cx="11" cy="11" r="6.5" />
          <path d="M16 16l4.5 4.5" />
        </svg>
        <input
          type="search"
          placeholder={t('gui.ws.search')}
          aria-label={t('gui.ws.search')}
          defaultValue={FT.q}
          onInput={(e) => ftQuery(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === 'Escape' && e.currentTarget.value) {
              e.stopPropagation()
              e.currentTarget.value = ''
              ftQuery('')
            }
          }}
        />
      </div>
      <div className="ftlist" ref={list}>
        {FT.q ? <FtResults onOpen={onOpen} /> : <FtRows dir="" depth={0} selectedPath={selectedPath} onOpen={onOpen} />}
      </div>
    </div>
  )
}

function FtLeaf({ text, depth }: { text: string; depth: number }): JSX.Element {
  return <div className="ftleaf" style={{ paddingLeft: `${23 + depth * 13}px` }}>{text}</div>
}

function FtResults({ onOpen }: { onOpen: (path: string) => void }): JSX.Element {
  const hits = ftMatches()
  return (
    <>
      {hits.slice(0, FT_SHOW_MAX).map(({ e, full, at }) => {
        const open = (): void => {
          if (e.dir) {
            ftReveal(full, true)
            return
          }
          ftOpenTo(full)
          onOpen(ftAbs(full))
        }
        const items = (): MenuItem[] => (e.dir ? [] : [{ label: t('gui.ws.open'), fn: open }]).concat([
          { label: t('gui.ws.reveal'), fn: () => ftReveal(full, Boolean(e.dir)) },
          { label: t('gui.ws.copy_path_do'), fn: () => copyToClip(ftAbs(full), t('gui.ws.copy_path')) },
          { label: t('gui.ws.copy_name'), fn: () => copyToClip(e.name, t('gui.ws.copied_name')) },
        ])
        const cut = full.lastIndexOf('/')
        return (
          <button key={full} className="ftrow" style={{ paddingLeft: '10px' }} onClick={open} ref={ctxRef(items)}>
            <Ico d={e.dir ? ICO.file : ICO.doc} cls={'fi ' + (e.dir ? 'k-dir' : 'k-' + ftKindOf(e.name))} />
            <span className="nm">
              {e.name.slice(0, at)}
              <b className="hl">{e.name.slice(at, at + FT.q.length)}</b>
              {e.name.slice(at + FT.q.length)}
            </span>
            {cut > 0 ? <span className="pth" title={full}>{full.slice(0, cut)}</span> : null}
          </button>
        )
      })}
      {!hits.length && !FT.crawling ? <FtLeaf text={t('gui.ws.no_match')} depth={0} /> : null}
      {hits.length > FT_SHOW_MAX ? <FtLeaf text={t('gui.ws.search_more', { n: hits.length - FT_SHOW_MAX })} depth={0} /> : null}
      {FT.crawling ? <FtLeaf text={t('gui.ws.searching')} depth={0} />
        : FT.capped ? <FtLeaf text={t('gui.ws.search_capped')} depth={0} /> : null}
    </>
  )
}

function FtRows({ dir, depth, selectedPath, onOpen }: {
  dir: string
  depth: number
  selectedPath: string
  onOpen: (path: string) => void
}): JSX.Element {
  const kids = FT.kids.get(dir)
  if (kids === undefined) return <FtLeaf text={t('gui.ws.file_loading')} depth={depth} />
  if (isErr(kids)) return <FtLeaf text={t('gui.ws.read_fail', { err: kids.err })} depth={depth} />
  if (!kids.length) return <FtLeaf text={t('gui.ws.dir_empty')} depth={depth} />
  return (
    <>
      {kids.map((e) => {
        const full = ftJoin(dir, e.name)
        const open = Boolean(e.dir) && FT.open.has(full)
        const on = !e.dir && selectedPath === ftAbs(full)
        /* Guide lines, one per level, each under its ancestor's chevron.
           Inline because the count is the row's depth; hover keeps working
           because the states set background-color, never the shorthand. */
        const guides = depth
          ? {
              backgroundImage: 'repeating-linear-gradient(to right, var(--line) 0 1px, transparent 1px 13px)',
              backgroundSize: `${depth * 13}px 100%`,
              backgroundPosition: '16px 0',
              backgroundRepeat: 'no-repeat',
            }
          : null
        const flip = (): void => {
          if (FT.open.has(full)) FT.open.delete(full)
          else {
            FT.open.add(full)
            ftLoad(full)
          }
          store.redraw()
        }
        const items = (): MenuItem[] => (e.dir
          ? [{ label: t(open ? 'gui.ws.dir_collapse' : 'gui.ws.dir_expand'), fn: flip }]
          : [{ label: t('gui.ws.open'), fn: () => onOpen(ftAbs(full)) }]
        ).concat([
          { label: t('gui.ws.copy_path_do'), fn: () => copyToClip(ftAbs(full), t('gui.ws.copy_path')) },
          { label: t('gui.ws.copy_name'), fn: () => copyToClip(e.name, t('gui.ws.copied_name')) },
        ])
        return (
          <Fragment key={full}>
            <button
              className={'ftrow' + (on ? ' on' : '')}
              data-p={full}
              style={{ paddingLeft: `${10 + depth * 13}px`, ...(guides || {}) }}
              aria-expanded={e.dir ? open : undefined}
              onClick={() => {
                if (e.dir) flip()
                else onOpen(ftAbs(full))
              }}
              ref={ctxRef(items)}
            >
              {e.dir ? (
                <>
                  <Ico d={ICO.chev} cls="cv" />
                  <Ico d={ICO.file} cls="fi k-dir" />
                </>
              ) : (
                <>
                  <span className="sp" />
                  <Ico d={ICO.doc} cls={'fi k-' + ftKindOf(e.name)} />
                </>
              )}
              <span className="nm">{e.name}</span>
              {!e.dir ? <span className="sz">{fmtSize(e.size)}</span> : null}
            </button>
            {e.dir && open
              ? <FtRows dir={full} depth={depth + 1} selectedPath={selectedPath} onOpen={onOpen} />
              : null}
          </Fragment>
        )
      })}
    </>
  )
}

/* The seam between tree and file. It writes the width straight onto the wrap
   rather than through a redraw: a drag repaints on every pointer move, and
   rebuilding the tree at that rate would drop frames on a deep folder. */
function Grip({ wrapRef }: { wrapRef: RefObject<HTMLDivElement | null> }): JSX.Element {
  const put = (px: number, persist: boolean): void => {
    const wrap = wrapRef.current
    if (!wrap) return
    const max = Math.max(150, (wrap.offsetWidth || 600) - 220)
    FT.w = Math.round(Math.max(150, Math.min(max, px)))
    wrap.style.setProperty('--ftw', FT.w + 'px')
    if (persist) {
      try { localStorage.setItem(FTW_KEY, String(FT.w)) } catch { /* storage denied */ }
    }
  }
  const down = (e: ReactPointerEvent<HTMLButtonElement>): void => {
    e.preventDefault()
    const g = e.currentTarget
    const wrap = wrapRef.current
    if (!wrap) return
    const x0 = e.clientX
    const w0 = FT.hide ? 0 : FT.w
    let opened = false
    /* Where the pointer got to, for the release to read. The legacy handler
       asked its own pointerdown event for this, so the distance it compared
       was always zero and the shove-it-shut gesture below never fired. */
    let lastX = x0
    g.dataset.drag = 'true'
    g.setPointerCapture(e.pointerId)
    document.body.style.userSelect = 'none'
    const move = (ev: globalThis.PointerEvent): void => {
      lastX = ev.clientX
      const d = ev.clientX - x0
      /* Dragging the shut seam to the right is how the tree comes back,
         without going up to the folder for it. */
      if (FT.hide) {
        if (d < 60) return
        FT.hide = false
        opened = true
        wrap.dataset.tree = 'on'
      }
      put(w0 + d, false)
    }
    const up = (): void => {
      g.removeEventListener('pointermove', move)
      g.removeEventListener('pointerup', up)
      g.removeEventListener('pointercancel', up)
      delete g.dataset.drag
      document.body.style.userSelect = ''
      if (opened) {
        store.redraw()
        return
      }
      /* Shoved against its own floor: the intent was to get rid of it. */
      if (!FT.hide && FT.w <= 150 && lastX - x0 < -40) {
        FT.hide = true
        store.redraw()
        return
      }
      put(FT.w, true)
    }
    g.addEventListener('pointermove', move)
    g.addEventListener('pointerup', up)
    g.addEventListener('pointercancel', up)
  }
  return (
    <button
      type="button"
      className="grip"
      role="separator"
      aria-orientation="vertical"
      title={t('gui.ws.tree_resize')}
      aria-label={t('gui.ws.tree_resize')}
      onPointerDown={down}
      onKeyDown={(e) => {
        const step = e.shiftKey ? 40 : 10
        if (FT.hide) return
        if (e.key === 'ArrowLeft') {
          e.preventDefault()
          put(FT.w - step, true)
        }
        if (e.key === 'ArrowRight') {
          e.preventDefault()
          put(FT.w + step, true)
        }
      }}
    />
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
          : <img src={fileURL(f.path)} alt={f.path} onError={() => setBroken(true)} />}
      </div>
    )
  } else if (asFrame) {
    /* The browser's PDF viewer is script-driven and draws nothing in a frame
       with scripts denied, so a PDF gets allow-scripts. The origin stays
       opaque either way -- allow-same-origin is never granted. */
    body = <iframe sandbox={f.kind === 'pdf' ? 'allow-scripts' : ''} referrerPolicy="no-referrer" src={fileURL(f.path)} />
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
