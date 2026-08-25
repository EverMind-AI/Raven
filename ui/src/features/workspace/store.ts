import { createRoot } from 'react-dom/client'

import * as browser from '../browser/mount'
import * as agents from '../subagents/mount'
import * as deliveries from './deliveries'
import { ds, shell, t } from '../../shell/bridge'
import { copy } from '../../shell/clipboard'
import { md } from '../../shell/prose'

import type { WorkspaceSnapshot, WorkspaceSource, WsFile, WsShared } from './types'
import type { ReactElement } from 'react'
import type { Root } from 'react-dom/client'
import type { Shell } from '../../shell/bridge'

/* Page state, outside React on purpose: the legacy shell drives this panel
 * imperatively (the tab bar, the open/close buttons and the tool hooks all
 * live in legacy parts and call drawWs), so the state lives where the shims
 * can reach it and the component subscribes. The workspace record lives here
 * as well; the legacy fixture adapter reaches the stable record through the
 * island bag, while the live layer uses the narrow accessors below.
 */

export type WsRoute = 'launch' | 'diff' | 'file'

export interface WsIslandState {
  route: WsRoute
}

let state: WsIslandState = { route: 'launch' }
const listeners = new Set<() => void>()

const workspace: WsShared = { changes: [], urls: [], file: null, turn: 0, unseen: 0 }

export const shared = (): WsShared => workspace
export const currentTurn = (): number => workspace.turn
export const changes = (): WsShared['changes'] => workspace.changes
export const urls = (): WsShared['urls'] => workspace.urls

export function advanceTurn(): number {
  workspace.turn += 1
  return workspace.turn
}

export function snapshot(): WorkspaceSnapshot {
  return {
    changes: workspace.changes,
    urls: workspace.urls,
    file: workspace.file,
    turn: workspace.turn,
    unseen: workspace.unseen,
    deliveries: deliveries.snapshot(),
  }
}

export function restore(next: WorkspaceSnapshot): void {
  workspace.changes = next.changes
  workspace.urls = next.urls
  workspace.file = next.file
  workspace.turn = next.turn
  workspace.unseen = next.unseen
  deliveries.restore(next.deliveries || [])
}

function resetShared(): void {
  restore({ changes: [], urls: [], file: null, turn: 0, unseen: 0, deliveries: [] })
}

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

export function copyToClip(text: string, done: string): void {
  copy(text, done)
}

/* Straight to the renderer, not out through the shell and back: prose.ts is a
   pure function in this same bundle, so a bridge verb here would round-trip
   window.RavenShell.md -> window.md -> this module for nothing, and would hide
   the file viewer from anyone auditing md()'s callers. */
export const mdHtml = (src: string): string => md(src)
export const hostPlatform = (): string => source().hostPlatform()

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

/* ── which application gets a file the page cannot render ──────────────
   Per EXTENSION, not one global default, because that is the shape of the
   want: code in an editor, a deck in a presentation app. Front-end state
   mirrored to localStorage -- the gateway is told the name on each call
   rather than holding the map, so nothing about this preference has to be
   valid on another machine.

   An application NAME only, matching the server's own rule. The check is
   here as well as there because a value read back out of storage is input
   too: a page that saved something else, or somebody editing the key by
   hand, must not put it on a command line. */
const APP_KEY = 'raven.openWith'
const APP_NAME = /^[A-Za-z0-9][A-Za-z0-9 ._+-]{0,63}$/

function readApps(): Record<string, string> {
  let raw = ''
  try {
    raw = localStorage.getItem(APP_KEY) || ''
  } catch {
    return {}
  }
  if (!raw) return {}
  try {
    const parsed: unknown = JSON.parse(raw)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {}
    const out: Record<string, string> = {}
    for (const [ext, app] of Object.entries(parsed as Record<string, unknown>)) {
      if (typeof app === 'string' && APP_NAME.test(app)) out[ext.toLowerCase()] = app
    }
    return out
  } catch {
    return {}
  }
}

let apps: Record<string, string> | null = null
const appMap = (): Record<string, string> => (apps ??= readApps())

export const extOf = (path: string): string => {
  const name = String(path).split('/').pop() || ''
  const dot = name.lastIndexOf('.')
  return dot > 0 ? name.slice(dot + 1).toLowerCase() : ''
}

/* The application chosen for this file's kind, or null for the host default. */
export const appFor = (path: string): string | null => appMap()[extOf(path)] || null

export function setAppFor(path: string, app: string | null): void {
  const ext = extOf(path)
  if (!ext) return
  const next = { ...appMap() }
  if (app && APP_NAME.test(app)) next[ext] = app
  else delete next[ext]
  apps = next
  try {
    localStorage.setItem(APP_KEY, JSON.stringify(next))
  } catch {
    /* Private mode throws on write. Losing the preference is the whole cost. */
  }
}

/* Whether handing a file to an application is worth offering at all: the
   source has the verb, and the host it would run on is this desktop. A remote
   serve would start the program on somebody else's screen, which is worse than
   not offering -- so an unknown answer counts as no. */
export const canOpenInApp = (): boolean => {
  const src = source()
  return !!src.openIn && !!src.hostIsLocal && src.hostIsLocal()
}

export function openInApp(path: string, app?: string | null): Promise<unknown> {
  const src = source()
  if (!src.openIn) return Promise.resolve(null)
  return src.openIn(path, app || undefined)
}

export function _resetAppsForTests(): void {
  apps = null
}

export function relToWorkspace(p: string): string | null {
  const s = String(p || '')
  const m = s.match(/(?:^|\/)(?:\.raven\/)?workspace\/(.+)$/)
  if (m) return m[1] ?? null
  if (s.startsWith('/') || s.startsWith('~')) return null
  return s.replace(/^\.\//, '')
}

let fileSeq = 0

export function makeFile(p: string, downloadPath?: string): WsFile {
  return {
    path: String(p), ...(downloadPath ? { downloadPath } : {}), kind: fileKind(p), raw: false, text: null,
    err: null, size: null, loading: false, seq: ++fileSeq,
  }
}

export function showFile(p: string, downloadPath?: string): void {
  const desk = window.RavenIslands?.workspace as { openFile?: (path: string, downloadPath?: string) => void } | undefined
  if (desk?.openFile) {
    desk.openFile(p, downloadPath)
    return
  }
  const ws = shared()
  ws.file = makeFile(p, downloadPath)
  verb('showWorkspace')('file')
}

/* Everything the island opens goes through the real viewer when the source
   can browse; the fixture source keeps the demo's honest toast instead. */
export function openPath(p: string): void {
  const src = source()
  if (src.canBrowse) showFile(p)
  else src.openPath?.(p)
}

export function openDelivery(p: string, downloadPath: string): void {
  const src = source()
  if (src.canBrowse) showFile(p, downloadPath)
  else src.openPath?.(p)
}

/* Re-exported so the viewer does not have to know where the registry lives;
   the store is what it already talks to. */
export const markDeliveryMissing = (path: string): void => deliveries.markMissing(path)

/* For the kinds that never read text: an <img> or a frame that failed to load
   says nothing about WHY, and "gone" is one of several answers -- the viewer
   caps a render at 25 MB and refuses a path outside what the agent may read,
   and a file served 200 that the browser cannot decode fails here too. So the
   status is asked for, and only 404 marks the shelf. The transcript's tile
   probes the same way. */
export async function probeDeliveryMissing(path: string): Promise<void> {
  try {
    const r = await fetch(fileURL(path), { method: 'HEAD', credentials: 'same-origin', cache: 'no-store' })
    if (r.status === 404) deliveries.markMissing(path)
  } catch {
    /* No answer at all is not an answer about the file. */
  }
}

export async function loadFileText(f: WsFile): Promise<void> {
  f.loading = true
  try {
    const r = await fetch(fileURL(f.path), { credentials: 'same-origin' })
    if (!r.ok) {
      /* Only 404 says the file is gone. A refusal (403) or a file too big to
         render (413) is the viewer's limit, not the file's absence, and marking
         a deliverable missing for either would put a lie on the shelf. */
      if (r.status === 404) deliveries.markMissing(f.path)
      throw new Error(r.status === 403 ? t('gui.ws.file_denied')
        : r.status === 404 ? t('gui.ws.file_gone')
          : r.status === 413 ? t('gui.ws.file_big') : `HTTP ${r.status}`)
    }
    f.text = await r.text()
  } catch (e) {
    f.err = (e as Error).message || String(e)
  } finally {
    f.loading = false
    redraw()
  }
}

/* A different session is a different workspace state. Called by the demo
   shell's wsReset. */
export function reset(): void {
  resetShared()
}
