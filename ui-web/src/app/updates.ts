/* Something newer than what this window is running.
 *
 * Two different things can be: the built page on disk, and the released
 * version of Raven itself. They share the rail-foot row because to the reader
 * they are one sentence -- something newer exists -- and they differ only in
 * what the click does.
 *
 * `raven serve` streams dist straight from disk and stamps static responses
 * with an mtime+size ETag, so a rebuilt dist is detectable with a HEAD probe --
 * no backend support needed. The page never reloads itself: a reload mid-turn
 * would drop the live transcript, so the amber row in the rail foot waits for a
 * click.
 */

import type { UpgradeShade } from '../state/upgradeShade'

import { turn } from '../features/composer/mount'
import { T } from '../i18n/t'
import { gateway } from '../rpc/gateway'
import { ask as confirmAsk } from '../state/confirm'
import { open as upShade } from '../state/upgradeShade'

type UpKind = 'ver' | 'ui'

/* What build this window is running. Filled in from `system.version` once the
   socket is up, and unknown until then: the running install is the only thing
   that knows its version, so the rail foot and the About card render "--"
   rather than a guess. Here because this module is what compares it with what
   the gateway says is newest. */
let appVersion: string | null = null

export const APP_VERSION = (): string | null => appVersion

export function appVersionSet(v: string): void {
  appVersion = v
}

let upKind: UpKind | null = null
let upLatest: string | null = null

/** Which of the two notices is up, for the reconnect that has to explain itself. */
export const upgradeKind = (): UpKind | null => upKind

export function showUpNote(kind: UpKind, latest?: string | null): void {
  const note = document.getElementById('upnote')
  if (!note) return
  /* a pending version upgrade outranks a rebuilt page: upgrading reloads anyway */
  if (upKind === 'ver' && kind === 'ui') return
  upKind = kind
  if (latest) upLatest = latest
  const t = note.querySelector('.t') as HTMLElement
  const rl = note.querySelector('.rl') as HTMLElement
  if (kind === 'ver') {
    t.textContent = upLatest ? T('gui.upg.note', { v: `v${upLatest}` }) : T('gui.upg.note_bare')
    rl.textContent = T('gui.upg.go')
  } else {
    t.textContent = T('gui.update.note')
    rl.textContent = T('gui.update.reload')
  }
  note.hidden = false
}

/* An upgrade outlives the page that started it: serve exits, a detached helper
   installs, and what comes back is a fresh load. The marker is how any load
   tells "an upgrade is running" from "no upgrade has been asked for" -- without
   it, a second click starts a second upgrade against a half-removed install,
   and the reader is shown the raw failure of a doomed call. */
const UPG_KEY = 'raven.upgrade'
const UPG_CEILING_MS = 1200000

interface UpgradeMark {
  to: string | null
  t0: number
}

export function upMark(to?: string | null): void {
  try { localStorage.setItem(UPG_KEY, JSON.stringify({ to: to || null, t0: Date.now() })) } catch { /* private mode */ }
}

export function upMarkRead(): UpgradeMark | null {
  try {
    const m = JSON.parse(localStorage.getItem(UPG_KEY) || 'null') as UpgradeMark | null
    if (!m || !m.t0 || Date.now() - m.t0 > UPG_CEILING_MS) return null
    return m
  } catch { return null }
}

export function upMarkClear(): void {
  try { localStorage.removeItem(UPG_KEY) } catch { /* private mode */ }
}

export function askUpgrade(): void {
  /* Already running: re-enter the progress dialog rather than offering to
     start it again. Closing that dialog must not strand the reader. */
  const running = upMarkRead()
  if (running) { watchUpgrade(upShade(), running.t0); return }
  if (turn.busy()) {
    confirmAsk(T('gui.upg.title'), T('gui.upg.body_busy'), T('gui.upg.close'), () => {})
    return
  }
  confirmAsk(T('gui.upg.title'),
    T('gui.upg.body', { from: `v${APP_VERSION() || '?'}`, to: `v${upLatest || '?'}` }),
    T('gui.upg.go'), runUpgrade)
}

/* Called at boot: a page that loads while an install is in flight re-attaches
   to it, instead of coming up as if nothing were happening. */
export function resumeUpgrade(): void {
  const running = upMarkRead()
  if (running) watchUpgrade(upShade(), running.t0)
}

/* `system.upgrade` hands the install to a detached helper and then lets serve
   exit, so this page has to survive a gap with no backend: it polls until serve
   answers again and only then reloads (the new dist needs a reload anyway). The
   cookie survives the restart because the relaunched server reuses the same
   session token. */
export async function runUpgrade(): Promise<void> {
  const shade = upShade()
  shade.say(T('gui.upg.working'))
  try {
    await gateway().call('system.upgrade', {})
  } catch (e) {
    /* The server saw an install already in flight. That is the dialog the
       reader wanted, not an error -- adopt the run instead of reporting it. */
    const err = e as { data?: { reason?: string; detail?: string }; message?: string }
    if (err.data && err.data.reason === 'in_progress') {
      upMark(upLatest)
      watchUpgrade(shade)
      return
    }
    upMarkClear()
    shade.fail(T('gui.upg.failed'), (err.data && err.data.detail) || err.message || String(e))
    return
  }
  upMark(upLatest)
  watchUpgrade(shade)
}

/* Poll until serve answers again, then reload -- the new dist needs one anyway,
   and the cookie survives because the relaunched server reuses the session
   token. The ceiling is 20 minutes because a first upgrade resolves and
   byte-compiles every dependency: one measured cold run took nine. The old
   three-minute ceiling declared failure over a install that was still running,
   which is what taught the reader to click upgrade a second time. */
export function watchUpgrade(shade: UpgradeShade, since?: number): void {
  const t0 = since || Date.now()
  shade.say(T('gui.upg.working'))
  const tick = async (): Promise<void> => {
    if (Date.now() - t0 > UPG_CEILING_MS) {
      upMarkClear()
      shade.fail(T('gui.upg.failed'), T('gui.upg.gave_up'))
      return
    }
    let r: Response
    try {
      r = await fetch('/', { method: 'HEAD', cache: 'no-store' })
    } catch {
      setTimeout(tick, 1500)
      return
    }
    if (r.status === 401 || r.status === 403) { upMarkClear(); shade.fail(T('gui.upg.reauth'), ''); return }
    if (!r.ok) { setTimeout(tick, 1500); return }
    upMarkClear()
    window.location.reload()
  }
  /* wait out the handoff: probing too early answers from the process that is
     about to exit, and the page would reload onto a dying server */
  setTimeout(tick, 2500)
}

/* The validator for the build this page was loaded from. Kept here rather than
   inside the watcher below because the reconnect asks for it too: a gateway
   that comes back on a different build cannot be lived with, and that is the
   same question this watcher asks on a timer. */
let distBase: string | null = null

const distProbe = async (): Promise<string | null> => {
  /* Only the built page has a dist to watch. Under `vite dev` the dev server
     owns '/' and answers with a fresh validator after every edit it hot-reloads,
     so this probe would read the developer's own typing as a new build. */
  if (!import.meta.env.PROD) return null
  try {
    const r = await fetch('/', { method: 'HEAD', cache: 'no-store' })
    if (!r.ok) return null
    return r.headers.get('etag') || r.headers.get('last-modified') || null
  } catch { return null }
}

/* True only when the build is known to have CHANGED. An unreachable server, a
   server that sends no validator, or a first look with nothing to compare
   against all answer false -- reloading on a maybe would throw a live
   transcript away for nothing. */
export async function distMoved(): Promise<boolean> {
  const tag = await distProbe()
  if (tag === null || distBase === null) return false
  return tag !== distBase
}

/* Deliberately NOT skipped while a notice is already showing. It used to be,
   and that is what made this watcher blind exactly when it mattered: the
   version notice is up precisely when an upgrade is about to land, so the one
   moment the built page really does change was the one moment nothing was
   watching for it. showUpNote already arbitrates which notice wins, so the
   ranking does not need a second gate here. */
const probe = async (): Promise<void> => {
  const tag = await distProbe()
  if (tag === null) return
  if (distBase === null) { distBase = tag; return }
  if (tag !== distBase) showUpNote('ui')
}

/* Whether the watch below ever started. The tab-visible probe is registered
   once with the page's other document listeners (state/globalListeners.ts),
   which is earlier than the boot reaches this module and happens in every mode;
   this flag is what keeps it answering nothing until there is a watch. */
let watching = false

/** A tab coming back to the front, for a page that is watching its build. */
export function onVisible(): void {
  if (!watching) return
  if (document.visibilityState === 'visible') void probe()
}

export function watchForUpdates(): void {
  const note = document.getElementById('upnote')
  if (!note) return
  note.onclick = () => { if (upKind === 'ver') askUpgrade(); else window.location.reload() }
  /* The click stays wired either way -- the version notice this row also
     carries comes from the gateway, which the dev server proxies. Only the
     dist watch is built-page-only; see distProbe. */
  if (!import.meta.env.PROD) return
  watching = true
  void probe()
  setInterval(probe, 30000)
}
