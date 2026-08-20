import { ds, shell, t } from '../../shell/bridge'

import type { BrowserReply, BrowserSource, BrowserTabRow, ChromiumSource, FrameHead } from './types'

/* Page state, outside React on purpose: the legacy shell drives this view
 * imperatively (drawWs mounts and unmounts it per redraw, frames land from
 * the transport, the transcript's link trap opens pages), so the state lives
 * in a plain store the shims can call, and the component subscribes.
 *
 * This is the legacy BR object (ui/src/live/220-browser.js before the
 * migration) ported field for field; the paint/watch/poll machinery keeps
 * its shape so the two can be diffed.
 */

export interface BrowserState {
  /* -32601 from any browser.* call: the server does not have the surface at
     all, as opposed to `avail === false` (has it, cannot use it right now). */
  absent: boolean
  avail: boolean | null
  reason: string
  started: boolean
  headful: boolean
  url: string
  title: string
  err: string
  loading: boolean
  canBack: boolean
  canFwd: boolean
  tabs: BrowserTabRow[]
  vp: [number, number]
  /* Whether a frame has ever landed on the stage: gates the waiting note. */
  hasFrame: boolean
}

const initial: BrowserState = {
  absent: false,
  avail: null,
  reason: '',
  started: false,
  headful: false,
  url: '',
  title: '',
  err: '',
  loading: false,
  canBack: false,
  canFwd: false,
  tabs: [],
  vp: [1280, 800],
  hasFrame: false,
}

let state: BrowserState = { ...initial }
const listeners = new Set<() => void>()

export const getState = (): BrowserState => state

export function subscribe(l: () => void): () => void {
  listeners.add(l)
  return () => listeners.delete(l)
}

/* patch() mutates without notifying -- the legacy writes a later repaint
   picked up; set() notifies when a field actually moved, which is the
   repaint. Frames with unchanged metadata must cause no render at all. */
function patch(p: Partial<BrowserState>): void {
  state = { ...state, ...p }
  barSync()
}

function set(p: Partial<BrowserState>): void {
  let moved = false
  for (const k of Object.keys(p) as Array<keyof BrowserState>) {
    if (state[k] !== p[k]) {
      moved = true
      break
    }
  }
  state = { ...state, ...p }
  barSync()
  if (moved) for (const l of listeners) l()
}

let busy = false
let noWatch = false
let watching = false
let keepT: ReturnType<typeof setInterval> | null = null
let pollT: ReturnType<typeof setInterval> | null = null
let tabT: ReturnType<typeof setInterval> | null = null
let tabsBusy = false
let lastTried = ''
let lastBlob: Blob | null = null
let frameQ: Blob | null = null
let painting = false
const noFav = new Set<string>()

let host: HTMLElement | null = null
let canvasEl: HTMLCanvasElement | null = null
let stageEl: HTMLElement | null = null
let urlEl: HTMLInputElement | null = null

export const source = (): BrowserSource => ds<BrowserSource>('browser')
const chromium = (): ChromiumSource => source() as ChromiumSource

export function setHost(el: HTMLElement): void {
  host = el
}
export const hostEl = (): HTMLElement | null => host

export function setCanvas(el: HTMLCanvasElement | null): void {
  canvasEl = el
  if (el) {
    el.width = state.vp[0]
    el.height = state.vp[1]
  }
}
export const canvas = (): HTMLCanvasElement | null => canvasEl

export function setStage(el: HTMLElement | null): void {
  stageEl = el
}

export function setUrlEl(el: HTMLInputElement | null): void {
  urlEl = el
}
export const urlInput = (): HTMLInputElement | null => urlEl

/* The bar is uncontrolled, so React never rewrites its value and a state write
   that moves `url` only reaches the screen through this node. Every write goes
   through patch/set, so syncing there covers all of them -- as three call sites
   at the ends of the frame and poll paths did not: a close, an unstarted tabs
   reply and a shape-changed poll each set `url` and returned before one.
   Skipped while the bar has focus: a frame arriving mid-edit must not take the
   address out from under the reader. */
function barSync(): void {
  const bar = urlEl
  if (bar && document.activeElement !== bar && bar.value !== state.url) bar.value = state.url
}

export const noFavHas = (origin: string): boolean => noFav.has(origin)
export const noFavAdd = (origin: string): void => {
  noFav.add(origin)
}

export function showing(): boolean {
  const s = shell()
  return s.wsShows ? s.wsShows('browser') : false
}

const gone = (e: unknown): boolean =>
  Boolean(e) && typeof e === 'object' && (e as { code?: number }).code === -32601

const detail = (e: unknown): string =>
  ((e as { data?: { detail?: string } })?.data?.detail) || (e as Error)?.message || String(e)

function b64Blob(b64: string): Blob {
  const s = atob(b64)
  const u = new Uint8Array(s.length)
  for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i)
  return new Blob([u], { type: 'image/jpeg' })
}

/* Decode-and-draw with backpressure: while one frame is in createImageBitmap,
   arrivals overwrite frameQ instead of queueing, so after a decode stall the
   stage shows the page as it is now -- never a replay of where it has been. */
async function paint(blob: Blob): Promise<void> {
  frameQ = blob
  lastBlob = blob
  if (painting) return
  painting = true
  while (frameQ) {
    const b = frameQ
    frameQ = null
    try {
      const bmp = await createImageBitmap(b)
      const cv = canvasEl
      if (cv) {
        if (cv.width !== bmp.width || cv.height !== bmp.height) {
          cv.width = bmp.width
          cv.height = bmp.height
        }
        cv.getContext('2d')!.drawImage(bmp, 0, 0)
        if (!state.hasFrame) set({ hasFrame: true })
      }
      bmp.close()
    } catch {
      /* a torn frame; the next paint replaces it */
    }
  }
  painting = false
}

/* Repaints a rebuilt stage without waiting for a frame. */
export function repaintLast(): void {
  if (lastBlob) void paint(lastBlob)
}

export function onFrame(head: FrameHead, blob: Blob | null): void {
  if (frameMeta(head) && blob) void paint(blob)
}

function frameMeta(head: FrameHead): boolean {
  const navved = Boolean(head.url) && head.url !== state.url
  const p: Partial<BrowserState> = { avail: true, started: true }
  if (head.vw) patch({ vp: [head.vw, head.vh || state.vp[1]] })
  if (head.url) p.url = head.url
  let stopped = false
  if (typeof head.loading === 'boolean' && head.loading !== state.loading) {
    p.loading = head.loading
    stopped = !head.loading
  }
  /* Hidden: the facts are kept, the repaint is not -- the next draw reads
     them, exactly as the legacy meta handler returned early here. */
  if (!showing()) {
    patch(p)
    return false
  }
  set(p)
  if (stopped) void tabsSync()
  if (navved) void tabsSync()
  return true
}

function stateIn(r: BrowserReply): void {
  const p: Partial<BrowserState> = {}
  if (typeof r.loading === 'boolean') p.loading = r.loading
  if (typeof r.can_back === 'boolean') p.canBack = r.can_back
  if (typeof r.can_forward === 'boolean') p.canFwd = r.can_forward
  set(p)
}

/* Legacy pull path. Still used for: the first "is a browser even possible"
   ask, the idle wait for a page the agent might open, and the whole view on
   a server whose browser surface has no watch. */
export async function poll(force: boolean): Promise<void> {
  if (busy || state.absent) return
  /* The poll cancels its own timer once the panel stops showing, so a page
     left open behind a closed panel is not being screenshotted every second. */
  if (!force && !showing()) {
    tick(false)
    return
  }
  busy = true
  try {
    const r = await chromium().frame({ quality: 70 })
    const wasStarted = state.started
    const wasHeadful = state.headful
    const p: Partial<BrowserState> = {
      absent: false,
      avail: r.available !== false,
      reason: r.reason || r.error || '',
      started: !!r.started,
      headful: !!r.headful,
      url: r.url || '',
      title: r.title || '',
    }
    if (!r.started) {
      lastBlob = null
      p.hasFrame = false
    }
    set(p)
    /* Repaint-only-on-shape-change is the render diff now, but the frame and
       chrome below still belong to the unchanged-shape path alone. */
    if (wasStarted !== state.started || wasHeadful !== state.headful) return
    if (r.jpeg) void paint(b64Blob(r.jpeg))
    stateIn(r)
  } catch (e) {
    /* A server without the surface is not a failed screenshot: say so once
       and stop asking -- `absent` is the gate at the top of this function. */
    if (gone(e)) set({ absent: true, avail: false, reason: t('gui.br.absent'), err: '' })
    else set({ err: detail(e) })
  } finally {
    busy = false
  }
}

export function tick(on: boolean): void {
  if (pollT) {
    clearInterval(pollT)
    pollT = null
  }
  if (on) pollT = setInterval(() => void poll(false), 900)
}

export async function watch(on: boolean): Promise<void> {
  if (noWatch) {
    tick(on && showing())
    return
  }
  if (!on) {
    if (keepT) {
      clearInterval(keepT)
      keepT = null
    }
    if (watching) {
      watching = false
      chromium()
        .watch({ on: false })
        .catch(() => {})
    }
    tabTick(false)
    return
  }
  const r = stageEl && stageEl.getBoundingClientRect()
  const p: { on: boolean; quality: number; width?: number; height?: number } = { on: true, quality: 70 }
  if (r && r.width > 50 && r.height > 50) {
    p.width = Math.round(r.width)
    p.height = Math.round(r.height)
  }
  try {
    const res = await chromium().watch(p)
    watching = !!res.watching
    if (res.vw) patch({ vp: [res.vw, res.vh || state.vp[1]] })
    tick(false)
  } catch (e) {
    if (gone(e)) {
      noWatch = true
      tick(true)
      return
    }
    set({ err: (e as Error).message || String(e) })
  }
  /* Renewing with an unchanged size is a heartbeat, not a restart -- the
     server only reopens the screencast when the size or quality moved. */
  if (!keepT) {
    keepT = setInterval(() => {
      if (showing() && state.started) void watch(true)
      else void watch(false)
    }, 10000)
  }
}

export async function tabsSync(): Promise<void> {
  if (!showing() || !state.started || state.headful || tabsBusy) return
  tabsBusy = true
  try {
    const r = await chromium().tabs({ action: 'list' })
    const tabs = r.tabs || []
    if (JSON.stringify(tabs) !== JSON.stringify(state.tabs)) set({ tabs })
  } catch {
    /* no browser.tabs here: the strip just stays empty */
  }
  tabsBusy = false
}

export async function tabsAct(action: string, extra: { index?: number } = {}): Promise<void> {
  try {
    const r = await chromium().tabs({ action, ...extra })
    const tabs = r.tabs || []
    if (!r.started) {
      lastBlob = null
      set({ started: false, url: '', tabs, hasFrame: false })
      return
    }
    const p: Partial<BrowserState> = { tabs }
    if (r.url !== undefined) p.url = r.url || state.url
    if (typeof r.loading === 'boolean') p.loading = r.loading
    if (typeof r.can_back === 'boolean') p.canBack = r.can_back
    if (typeof r.can_forward === 'boolean') p.canFwd = r.can_forward
    set(p)
    if (action === 'new' && urlEl) {
      urlEl.focus()
      urlEl.select()
    }
    void watch(true)
    /* A static page repaints nothing after a tab switch, so the stream has no
       frame to push -- pull one so the stage shows the tab we just went to. */
    if (action === 'activate' || action === 'new') void poll(true)
  } catch (e) {
    set({ err: (e as Error).message || String(e) })
  }
}

export function tabTick(on: boolean): void {
  if (tabT) {
    clearInterval(tabT)
    tabT = null
  }
  if (on) tabT = setInterval(() => void tabsSync(), 2500)
}

export function input(payload: Record<string, unknown>): void {
  chromium()
    .input(payload)
    .catch((e: unknown) => set({ err: (e as Error).message || String(e) }))
  /* Pushed frames show the result on their own; only the poll fallback needs
     to go and look. */
  if (noWatch) setTimeout(() => void poll(true), 350)
}

export async function open(url: string): Promise<void> {
  set({ err: '' })
  lastTried = url
  try {
    const r = await chromium().open({ url })
    const p: Partial<BrowserState> = { avail: r.available !== false }
    if (r.error) p.err = r.error
    set(p)
    stateIn(r)
  } catch (e) {
    /* A server without the surface is not a failed navigation: say so once,
       stop offering the panel, and let the next link go to the real browser. */
    if (gone(e)) set({ absent: true, avail: false, reason: t('gui.br.absent'), err: '' })
    else set({ err: detail(e) })
  }
  await poll(true)
  void tabsSync()
}

export async function go(action: string): Promise<void> {
  try {
    const r = await chromium().open({ action })
    if (r) {
      set({ err: r.error || '' })
      stateIn(r)
    }
  } catch {
    /* state poll reports it */
  }
  await poll(true)
  void tabsSync()
}

export function retry(): void {
  set({ err: '' })
  if (lastTried) void open(lastTried)
}

export function dismissErr(): void {
  set({ err: '' })
}

export async function popout(): Promise<void> {
  void watch(false)
  try {
    const r = await chromium().mode({ headful: true })
    set({ headful: !!r.headful })
  } catch {
    /* poll reports it */
  }
}

export async function popin(): Promise<void> {
  try {
    const r = await chromium().mode({ headful: false })
    set({ headful: !!r.headful })
  } catch {
    /* poll reports it */
  }
}

export async function closeBrowser(): Promise<void> {
  await chromium().close()
  lastBlob = null
  set({ started: false, url: '', hasFrame: false })
}

/* Leaving the browser view -- another tab, another session, the panel shut --
   must drop the watch; the demo drawWs wrapper calls this on every repaint
   that lands somewhere else. */
export function hidden(): void {
  void watch(false)
  tick(false)
}

/* The island's subscription to the pushed-frame hook. Called once the seam
   exists (the island bundle evaluates before the seam script does). */
export function hook(): void {
  const seam = window.DS
  const src = seam && (seam['browser'] as BrowserSource | undefined)
  if (src && src.embedded) src.onFrame = onFrame
}

/* A link in the transcript opens in the embedded browser -- the panel IS this
   app's browser. Modifier clicks keep the system-browser escape hatch, and a
   server whose browser is absent or unavailable keeps the navigation. */
function trap(e: MouseEvent): void {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
  const seam = window.DS
  const src = seam && (seam['browser'] as BrowserSource | undefined)
  if (!src || !src.embedded) return
  if (state.absent || state.avail !== true) return
  const el = e.target as Element | null
  const a = el && el.closest ? (el.closest('#scroll a[href]') as HTMLAnchorElement | null) : null
  if (!a || !/^https?:/i.test(a.href)) return
  e.preventDefault()
  shell().showWorkspace?.('browser')
  void open(a.href)
}

export function installLinkTrap(): void {
  document.addEventListener('click', trap, true)
  /* Subscribe to pushed frames as soon as the live source exists, so a page
     the agent opens before this view is ever shown is still tracked. */
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', () => hook())
  else hook()
}

/* Test seam only: module-level timers and flags survive between tests. */
export function _resetForTests(): void {
  watching = false
  if (keepT) {
    clearInterval(keepT)
    keepT = null
  }
  tick(false)
  tabTick(false)
  busy = false
  noWatch = false
  tabsBusy = false
  painting = false
  frameQ = null
  lastBlob = null
  lastTried = ''
  noFav.clear()
  host = null
  canvasEl = null
  stageEl = null
  urlEl = null
  state = { ...initial }
}
