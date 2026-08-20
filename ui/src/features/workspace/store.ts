import { createRoot } from 'react-dom/client'

import * as browser from '../browser/mount'
import * as agents from '../subagents/mount'
import { ds, shell, t } from '../../shell/bridge'
import { md } from '../../shell/prose'

import type { FtEntry, FtKids, WorkspaceSource, WsFile, WsShared } from './types'
import type { ReactElement } from 'react'
import type { Root } from 'react-dom/client'
import type { Shell } from '../../shell/bridge'

/* Page state, outside React on purpose: the legacy shell drives this panel
 * imperatively (the tab bar, the open/close buttons and the tool hooks all
 * live in legacy parts and call drawWs), so the state lives where the shims
 * can reach it and the component subscribes. The workspace record itself
 * (WS) stays a demo-shell global that every layer mutates; the island reads
 * it through the shell and re-renders when poked.
 */

export type WsRoute = 'launch' | 'diff' | 'file'

export interface WsIslandState {
  route: WsRoute
}

let state: WsIslandState = { route: 'launch' }
const listeners = new Set<() => void>()

export const getState = (): WsIslandState => state

export function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => listeners.delete(l)
}

function set(patch: Partial<WsIslandState>): void {
  state = { ...state, ...patch }
  for (const l of listeners) l()
}

export const source = (): WorkspaceSource => ds<WorkspaceSource>('workspace')

function verb<K extends keyof Shell>(name: K): NonNullable<Shell[K]> {
  const v = shell()[name]
  if (!v) throw new Error(`RavenShell.${String(name)} is not wired`)
  return v as NonNullable<Shell[K]>
}

export const shared = (): WsShared => verb('wsState')() as WsShared

export function copyToClip(text: string, done: string): void {
  verb('copyToClip')(text, done)
}

/* Straight to the renderer, not out through the shell and back: prose.ts is a
   pure function in this same bundle, so a bridge verb here would round-trip
   window.RavenShell.md -> window.md -> this module for nothing, and would hide
   the file viewer from anyone auditing md()'s callers. */
export const mdHtml = (src: string): string => md(src)
export const hostPlatform = (): string => verb('hostPlatform')()

/* A language flip changes nothing in this state, but every visible string
   comes from t(), so a re-render is the whole redraw. */
export function redraw(): void {
  set({})
}

/* The routing the legacy drawWs kept, minus the two tabs with islands of
   their own: agents and browser are dispatched before this runs. Marking
   the changes seen happens HERE, synchronously, because the legacy callers
   run bumpWs() right after drawWs() and count on the render having marked
   them -- the React render itself lands later. */
export function sync(): void {
  const view = verb('wsView')()
  const ws = shared()
  const bare = !ws.changes.length && !ws.urls.length
  const route: WsRoute = bare && !view.picked ? 'launch' : view.tab === 'file' ? 'file' : 'diff'
  if (route === 'diff') ws.changes.forEach((c) => { c.seen = true })
  set({ route })
}

let renderEl: (() => ReactElement) | null = null
export function setRenderer(f: () => ReactElement): void {
  renderEl = f
}

let root: Root | null = null

/* What the drawWs shim calls. The island owns #wsBody for its own views;
   the agents tab belongs to the subagents island and the browser tab to the
   browser island, so this root steps aside (unmounts) and hands them the
   cleared box -- the same box, no wrapper, because .ws-body[data-view]
   styles its direct children. */
export function draw(): void {
  const host = document.getElementById('wsBody')
  if (!host) return
  const view = verb('wsView')()
  /* Each tab island's root must leave while the DOM it owns is intact --
     BEFORE any wipe -- and the browser's frame watch must drop whenever this
     draw lands anywhere but a visible browser view: what its own drawWs
     wrapper did while the dispatch was legacy. */
  browser.detach()
  agents.detach()
  if (view.tab === 'browser' || view.tab === 'agents') {
    if (root) {
      root.unmount()
      root = null
    }
    host.innerHTML = ''
    delete host.dataset.view
    if (view.tab === 'agents') {
      host.dataset.view = 'agents'
      agents.draw(host)
      browser.hidden()
    } else {
      browser.draw(host)
      if (!view.open) browser.hidden()
    }
    return
  }
  browser.hidden()
  sync()
  if (state.route === 'file' && source().canBrowse) host.dataset.view = 'file'
  else delete host.dataset.view
  if (!root && renderEl) {
    host.innerHTML = ''
    root = createRoot(host)
    root.render(renderEl())
  }
}

export function pick(tab: string): void {
  verb('wsPick')(tab)
}

/* ── file viewing ──────────────────────────────────────────────────── */

export const fileURL = (p: string): string => '/file?path=' + encodeURIComponent(String(p))

const TEXT_EXT = new Set(['c', 'cfg', 'conf', 'cpp', 'css', 'diff', 'env', 'go', 'h', 'ini', 'java',
  'js', 'json', 'jsonl', 'jsx', 'kt', 'log', 'lua', 'patch', 'php', 'pl', 'py', 'pyi', 'rb', 'rs',
  'sh', 'sql', 'swift', 'toml', 'ts', 'tsx', 'txt', 'vue', 'yaml', 'yml', 'zsh'])
const IMG_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'avif'])

export function fileKind(p: string): string {
  const ext = (String(p).split('.').pop() || '').toLowerCase()
  if (ext === 'md' || ext === 'mdx' || ext === 'markdown') return 'md'
  if (IMG_EXT.has(ext)) return 'img'
  if (ext === 'svg') return 'svg'
  if (ext === 'pdf') return 'pdf'
  if (ext === 'html' || ext === 'htm') return 'html'
  if (ext === 'csv' || ext === 'tsv') return 'csv'
  if (ext === 'json') return 'json'
  if (ext === 'diff' || ext === 'patch') return 'diff'
  if (TEXT_EXT.has(ext)) return 'code'
  return 'bin'
}
export const RENDERED: Record<string, 1> = { md: 1, img: 1, svg: 1, pdf: 1, html: 1, csv: 1, json: 1 }

/* Which icon colour a tree row gets, from the name alone. */
export const ftKindOf = (name: string): string => {
  const k = fileKind(name)
  if (k === 'md') return 'md'
  if (k === 'img' || k === 'svg' || k === 'pdf') return 'img'
  if (/\.(json|ya?ml|toml|ini|cfg|conf|lock|env)$/i.test(name)) return 'cfg'
  return k === 'code' || k === 'html' || k === 'csv' ? 'code' : 'doc'
}

export function relToWorkspace(p: string): string | null {
  const s = String(p || '')
  const m = s.match(/(?:^|\/)(?:\.raven\/)?workspace\/(.+)$/)
  if (m) return m[1] ?? null
  if (s.startsWith('/') || s.startsWith('~')) return null
  return s.replace(/^\.\//, '')
}

let fileSeq = 0

export function showFile(p: string): void {
  const ws = shared()
  ws.file = {
    path: String(p), kind: fileKind(p), raw: false, text: null,
    err: null, size: null, loading: false, seq: ++fileSeq,
  }
  verb('showWorkspace')('file')
}

/* Everything the island opens goes through the real viewer when the source
   can browse; the fixture source keeps the demo's honest toast instead. */
export function openPath(p: string): void {
  const src = source()
  if (src.canBrowse) showFile(p)
  else src.openPath?.(p)
}

export async function loadFileText(f: WsFile): Promise<void> {
  f.loading = true
  try {
    const r = await fetch(fileURL(f.path), { credentials: 'same-origin' })
    if (!r.ok) {
      throw new Error(r.status === 403 ? t('gui.ws.file_denied')
        : r.status === 404 ? t('gui.ws.file_gone')
          : r.status === 413 ? t('gui.ws.file_big') : `HTTP ${r.status}`)
    }
    f.text = await r.text()
  } catch (e) {
    f.err = (e as Error).message || String(e)
  } finally {
    f.loading = false
    if (shared().file === f) redraw()
  }
}

/* ── file tree ─────────────────────────────────────────────────────────
   The legacy live layer's tree, state and crawler intact: listings are
   fetched per folder on first open and kept, one promise per directory is
   shared by the tree and the search crawler, and the whole thing resets
   with the session. */
export const FT: {
  open: Set<string>
  kids: Map<string, FtKids>
  q: string
  w: number
  hide: boolean
  run: number
  crawling: boolean
  capped: boolean
  root: string
} = {
  open: new Set(['']), kids: new Map(), q: '', w: 208, hide: false,
  run: 0, crawling: false, capped: false, root: '',
}
export const FTW_KEY = 'raven.gui.ftw'
try {
  FT.w = Math.max(150, Math.min(460, parseFloat(localStorage.getItem(FTW_KEY) || '') || FT.w))
} catch { /* storage denied: keep the default width */ }

export const isErr = (k: FtKids): k is { err: string } => !Array.isArray(k)

function ftReset(): void {
  FT.open = new Set([''])
  FT.kids.clear()
  FT.q = ''
  FT.run += 1
  FT.crawling = false
  FT.capped = false
  FT.root = ''
}

/* A different session is a different workspace state: keep no listing that
   was read before the switch. Called by the demo shell's wsReset. */
export function reset(): void {
  ftReset()
}

export const ftJoin = (dir: string, name: string): string => (dir ? `${dir}/${name}` : name)
export const ftAbs = (full: string): string => (FT.root ? `${FT.root}/${full}` : full)
export const relToRoot = (p: string): string | null => {
  const s = String(p || '')
  return FT.root && s.startsWith(FT.root + '/') ? s.slice(FT.root.length + 1) : null
}

const ftPending = new Map<string, Promise<FtKids>>()
export function ftFetch(dir: string): Promise<FtKids> {
  const hit = FT.kids.get(dir)
  if (hit !== undefined) return Promise.resolve(hit)
  const pending = ftPending.get(dir)
  if (pending) return pending
  const list = source().list
  if (!list) return Promise.resolve({ err: 'no source' })
  const p = list(dir)
    .then((r) => {
      if (r.root) FT.root = r.root
      const kids = [...r.entries].sort((a, b) =>
        (b.dir ? 1 : 0) - (a.dir ? 1 : 0) || a.name.localeCompare(b.name))
      FT.kids.set(dir, kids)
      return kids as FtKids
    })
    .catch((e: unknown) => {
      const bad = { err: (e as Error).message || String(e) }
      FT.kids.set(dir, bad)
      return bad
    })
    .finally(() => ftPending.delete(dir))
  ftPending.set(dir, p)
  return p
}

export function ftLoad(dir: string): void {
  if (FT.kids.has(dir)) return
  void ftFetch(dir).then(() => redraw())
}

/* Rendering shows a loading leaf for a folder whose listing has not landed;
   this effect (run by the tree pane after every render) asks for each of
   them -- the render itself stays free of fetches. */
export function ftLoadVisible(): void {
  const walk = (dir: string): void => {
    const kids = FT.kids.get(dir)
    if (kids === undefined) {
      ftLoad(dir)
      return
    }
    if (isErr(kids)) return
    for (const e of kids) {
      if (!e.dir) continue
      const full = ftJoin(dir, e.name)
      if (FT.open.has(full)) walk(full)
    }
  }
  walk('')
}

const FT_SKIP = new Set(['.git', 'node_modules', '.venv', 'venv', '__pycache__',
  '.mypy_cache', '.ruff_cache', '.pytest_cache', '.cache', 'dist', 'build', 'target', '.next'])
const FT_CRAWL_DIRS = 600
const FT_CRAWL_PAR = 4
export const FT_SHOW_MAX = 120

/* Streams land many at a time; one repaint per frame is enough. */
let drawQueued = false
function redrawSoon(): void {
  if (drawQueued) return
  drawQueued = true
  requestAnimationFrame(() => {
    drawQueued = false
    redraw()
  })
}

export async function ftCrawl(run: number): Promise<void> {
  FT.crawling = true
  FT.capped = false
  let budget = FT_CRAWL_DIRS
  const queue = ['']
  const seen = new Set(queue)
  const worker = async (): Promise<void> => {
    while (queue.length) {
      if (FT.run !== run) return
      const dir = queue.shift() as string
      let kids = FT.kids.get(dir)
      if (kids === undefined) {
        if (budget <= 0) {
          FT.capped = true
          continue
        }
        budget -= 1
        kids = await ftFetch(dir)
        if (FT.run === run) redrawSoon()
      }
      if (!kids || isErr(kids)) continue
      kids.forEach((e) => {
        if (!e.dir || FT_SKIP.has(e.name)) return
        const full = ftJoin(dir, e.name)
        if (!seen.has(full)) {
          seen.add(full)
          queue.push(full)
        }
      })
    }
  }
  await Promise.all(Array.from({ length: FT_CRAWL_PAR }, worker))
  if (FT.run !== run) return
  FT.crawling = false
  redraw()
}

export interface FtHit {
  e: FtEntry
  full: string
  at: number
}

export function ftMatches(): FtHit[] {
  const out: FtHit[] = []
  const walk = (dir: string): void => {
    const kids = FT.kids.get(dir)
    if (!kids || isErr(kids)) return
    kids.forEach((e) => {
      if (e.dir && FT_SKIP.has(e.name)) return
      const full = ftJoin(dir, e.name)
      const at = e.name.toLowerCase().indexOf(FT.q)
      if (at >= 0) out.push({ e, full, at })
      if (e.dir) walk(full)
    })
  }
  walk('')
  /* Name-starts-with beats name-contains, files beat folders (opening one is
     the usual intent), shallow beats deep. */
  const depth = (p: string): number => p.split('/').length
  out.sort((a, b) => (a.at === 0 ? 0 : 1) - (b.at === 0 ? 0 : 1)
    || (a.e.dir ? 1 : 0) - (b.e.dir ? 1 : 0)
    || depth(a.full) - depth(b.full)
    || a.e.name.localeCompare(b.e.name))
  return out
}

export function ftOpenTo(full: string): void {
  let acc = ''
  full.split('/').slice(0, -1).forEach((p) => {
    acc = ftJoin(acc, p)
    FT.open.add(acc)
    ftLoad(acc)
  })
}

export function ftReveal(full: string, isDir: boolean): void {
  ftOpenTo(full)
  if (isDir) {
    FT.open.add(full)
    ftLoad(full)
  }
  FT.q = ''
  FT.run += 1
  FT.crawling = false
  const inp = document.querySelector<HTMLInputElement>('#wsBody .ftq input')
  if (inp) inp.value = ''
  redraw()
  requestAnimationFrame(() => {
    const row = document.querySelector(`#wsBody .ftlist [data-p="${CSS.escape(full)}"]`)
    if (row) row.scrollIntoView({ block: 'center' })
  })
}

let ftDebounce: ReturnType<typeof setTimeout> | undefined
export function ftQuery(v: string): void {
  FT.q = v.trim().toLowerCase()
  FT.run += 1
  clearTimeout(ftDebounce)
  /* What is already cached answers immediately; the crawl for the rest waits
     out the keystroke burst. */
  redraw()
  if (FT.q) ftDebounce = setTimeout(() => void ftCrawl(FT.run), 220)
  else FT.crawling = false
}

/* Open a folder where folders live: the file tab's tree, unfolded down to
   it. The root listing is fetched first because tree entries are
   root-relative and the root is only learned from fs.list's answer. */
export function openDir(p: string): void {
  verb('showWorkspace')('file')
  ftFetch('').then(() => {
    const rel = relToRoot(p)
    ftReveal(rel != null ? rel : String(p).replace(/^\/+/, ''), true)
  }).catch(() => {})
}
