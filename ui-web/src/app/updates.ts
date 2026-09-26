/* Something newer than what this window is running.
 *
 * Three different things can be: the built page on disk, the released version
 * of Raven itself, and -- on a source checkout -- the sources the page was
 * built from. They share the rail-foot row because to the reader they are one
 * sentence -- something newer exists -- and they differ only in what the click
 * does: reload, upgrade, or be told how to rebuild.
 *
 * `raven serve` streams dist straight from disk and stamps static responses
 * with an mtime+size ETag, so a rebuilt dist is detectable with a HEAD probe --
 * no backend support needed. The page never reloads itself: a reload mid-turn
 * would drop the live transcript, so the amber row in the rail foot waits for a
 * click.
 *
 * That row is `#upnote` and its two spans, reached for by id here: the rail
 * renders the element (src/chrome/Rail.tsx) and what it says, what it offers
 * and its click are this module's.
 */

import { turn } from '../features/composer/mount'
import { t } from '../i18n/t'
import { gateway } from '../rpc/gateway'
import { ask as confirmAsk } from '../state/confirm'
import { open as upShade } from '../state/upgradeShade'

import type { UpgradeShade } from '../state/upgradeShade'

type UpKind = 'ver' | 'ui' | 'behind'

/* Which notice wins the one row. A pending release replaces the install, page
   included, so it outranks both (a checkout that also sits behind its sources
   still has the terminal's warning). A page behind its sources outranks a
   rebuilt one: the reload the latter offers would come back behind as well, so
   the row asks for the rebuild first, and the probe that sees it land takes
   this notice down and lets the reload's take the row. */
const RANK: Record<UpKind, number> = { ui: 0, behind: 1, ver: 2 }

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

/** Which notice is up, for the reconnect that has to explain itself. */
export const upgradeKind = (): UpKind | null => upKind

/* The version the last notice named, retained here since `showUpNote` wrote
   it: the settings About row reads it to offer the upgrade, and it has to
   outlive that row, which the settings panel replaces on every redraw. */
export const upgradeLatest = (): string | null => upLatest

export function showUpNote(kind: UpKind, latest?: string | null): void {
  const note = document.getElementById('upnote')
  if (!note) return
  if (upKind && RANK[kind] < RANK[upKind]) return
  upKind = kind
  if (latest) upLatest = latest
  const [said, action] = wording(kind)
  ;(note.querySelector('.t') as HTMLElement).textContent = said
  ;(note.querySelector('.rl') as HTMLElement).textContent = action
  note.hidden = false
}

/* The row's line and its action word, per notice. */
function wording(kind: UpKind): [string, string] {
  if (kind === 'ver') return [upLatest ? t('gui.upg.note', { v: `v${upLatest}` }) : t('gui.upg.note_bare'), t('gui.upg.go')]
  if (kind === 'behind') return [t('gui.update.behind'), t('gui.update.behind_how')]
  return [t('gui.update.note'), t('gui.update.reload')]
}

/* The row, as the watch below found it. Only the behind-sources notice is ever
   taken back down -- its reason is a fact about the disk that the next build
   removes, and the probe that saw it go says so -- and that probe only runs
   once the watch has the row, so this is the one reach it needs. */
let noteRow: HTMLElement | null = null

function hideBehindNote(): void {
  if (upKind !== 'behind') return
  if (noteRow) noteRow.hidden = true
  upKind = null
}

/* The click for a page behind its sources. The page cannot rebuild itself, so
   what it can do is say where and how. */
function explainBehind(): void {
  confirmAsk(t('gui.update.behind_title'), t('gui.update.behind_body'), t('gui.close'), () => {}, 'notice')
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
    confirmAsk(t('gui.upg.title'), t('gui.upg.body_busy'), t('gui.upg.close'), () => {}, 'notice')
    return
  }
  confirmAsk(t('gui.upg.title'),
    t('gui.upg.body', { from: `v${APP_VERSION() || '?'}`, to: `v${upLatest || '?'}` }),
    t('gui.upg.go'), runUpgrade, 'primary')
}

/* Called at boot: a page that loads while an install is in flight re-attaches
   to it, instead of coming up as if nothing were happening. */
export function resumeUpgrade(): void {
  const running = upMarkRead()
  if (running) watchUpgrade(upShade(), running.t0)
}

/* A refusal the page can name in the reader's language. The server's `detail`
   is one English sentence for every surface that calls it; showing it raw
   dropped an English paragraph into the middle of a Chinese card. Reasons whose
   detail carries run-specific facts (the uv error, the failed lookup) keep it,
   because translating the frame would throw the facts away. */
const REFUSALS: Record<string, string> = {
  unsupervised: 'gui.upg.why.unsupervised',
  not_serving: 'gui.upg.why.not_serving',
}

export function refusalText(err: { data?: { reason?: string; detail?: string }; message?: string }): string {
  const key = err.data && err.data.reason ? REFUSALS[err.data.reason] : undefined
  if (key) return t(key)
  return (err.data && err.data.detail) || err.message || String(err)
}

/* `system.upgrade` hands the install to a detached helper and then lets serve
   exit, so this page has to survive a gap with no backend: it polls until serve
   answers again and only then reloads (the new dist needs a reload anyway). The
   cookie survives the restart because the relaunched server reuses the same
   session token. */
export async function runUpgrade(): Promise<void> {
  const shade = upShade()
  shade.say(t('gui.upg.working'))
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
    /* Work is running that the page cannot see -- an IM turn, a sub-agent.
       Same answer as the page's own busy turn: a notice to wait. The failure
       card would offer the terminal command, which is not the fix. */
    if (err.data && err.data.reason === 'busy') {
      upMarkClear()
      shade.close()
      confirmAsk(t('gui.upg.title'), t('gui.upg.why.busy'), t('gui.upg.close'), () => {}, 'notice')
      return
    }
    upMarkClear()
    shade.fail(t('gui.upg.failed'), refusalText(err))
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
/* What the upgrade helper reports while it is the only thing on this page's
   port: from the moment the old Raven exits until the new one is started.
   Anything without `upgrading` is not the helper -- the real server may well
   answer this path with the page itself. */
export interface UpStatus {
  upgrading: true
  phase: string
  done: number
  total: number
  rate: number
  message: string | null
}

async function readStatus(): Promise<UpStatus | null> {
  try {
    const r = await fetch('/upgrade/status', { cache: 'no-store' })
    if (!r.ok) return null
    const body = (await r.json()) as Partial<UpStatus> | null
    return body && body.upgrading === true ? (body as UpStatus) : null
  } catch { return null }
}

const mib = (n: number): string => (n / 1048576).toFixed(1)
const speed = (n: number): string => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB/s` : `${Math.round(n / 1024)} KB/s`)

/* The line and the bar for one report. Numbers and units are the same in every
   language, so only the frame around them comes from the catalogue. */
export function describeStatus(s: UpStatus): { text: string; fraction: number | null } {
  if (s.phase === 'downloading') {
    const amount = s.total > 0 ? `${mib(s.done)} / ${mib(s.total)} MiB` : `${mib(s.done)} MiB`
    const rate = s.rate > 0 ? ` · ${speed(s.rate)}` : ''
    return { text: t('gui.upg.phase.download', { progress: amount + rate }), fraction: s.total > 0 ? s.done / s.total : null }
  }
  if (s.phase === 'installing') return { text: t('gui.upg.phase.install'), fraction: null }
  return { text: t('gui.upg.working'), fraction: null }
}

export function watchUpgrade(shade: UpgradeShade, since?: number): void {
  const t0 = since || Date.now()
  /* The ceiling counts time without progress, not the whole upgrade. A slow
     link can take longer than the ceiling to download a release, and giving up
     on one that is visibly still moving would be the wrong answer. But the
     helper's status server is its own thread and keeps answering while a
     download or uv is stuck, so an answer alone is not progress: only a new
     phase or more bytes move the deadline. Otherwise a stalled helper holds
     the reader under a card with no way out, and a reload resumes it. */
  let moved: number | null = null
  let last: { phase: string; done: number } | null = null
  let helperSeen = false
  shade.say(t('gui.upg.working'))
  const tick = async (): Promise<void> => {
    if (Date.now() - (moved ?? t0) > UPG_CEILING_MS) {
      upMarkClear()
      shade.fail(t('gui.upg.failed'), t('gui.upg.gave_up'))
      return
    }
    const status = await readStatus()
    if (status) {
      helperSeen = true
      if (!last || status.phase !== last.phase || status.done > last.done) {
        moved = Date.now()
        last = { phase: status.phase, done: status.done }
      }
      if (status.phase === 'failed') {
        /* The helper holds this until it has been read, then brings the old
           Raven back; without the card the page would reload onto it and the
           failed upgrade would read as one that did nothing. */
        upMarkClear()
        shade.fail(t('gui.upg.failed'), status.message || '')
        return
      }
      const seen = describeStatus(status)
      shade.measure(seen.text, seen.fraction)
      setTimeout(tick, 1000)
      return
    }
    /* The helper answered and now does not: it has let the port go to the new
       Raven, which is starting. */
    if (helperSeen) shade.measure(t('gui.upg.phase.restart'), null)
    let r: Response
    try {
      r = await fetch('/', { method: 'HEAD', cache: 'no-store' })
    } catch {
      setTimeout(tick, 1500)
      return
    }
    if (r.status === 401 || r.status === 403) { upMarkClear(); shade.fail(t('gui.upg.reauth'), ''); return }
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

/* One look at the served page: the validator of the build on disk, and
   whether the server judged that build older than the sources beside it (the
   X-Raven-Page-Behind header, which only a source checkout ever sends). */
interface DistSeen { tag: string | null; behind: boolean }

const distProbe = async (): Promise<DistSeen | null> => {
  /* Only the built page has a dist to watch. Under `vite dev` the dev server
     owns '/' and answers with a fresh validator after every edit it hot-reloads,
     so this probe would read the developer's own typing as a new build. */
  if (!import.meta.env.PROD) return null
  try {
    const r = await fetch('/', { method: 'HEAD', cache: 'no-store' })
    if (!r.ok) return null
    return {
      tag: r.headers.get('etag') || r.headers.get('last-modified') || null,
      behind: r.headers.has('x-raven-page-behind'),
    }
  } catch { return null }
}

/* True only when the build is known to have CHANGED. An unreachable server, a
   server that sends no validator, or a first look with nothing to compare
   against all answer false -- reloading on a maybe would throw a live
   transcript away for nothing. */
export async function distMoved(): Promise<boolean> {
  const seen = await distProbe()
  if (seen === null || seen.tag === null || distBase === null) return false
  return seen.tag !== distBase
}

/* Deliberately NOT skipped while a notice is already showing. It used to be,
   and that is what made this watcher blind exactly when it mattered: the
   version notice is up precisely when an upgrade is about to land, so the one
   moment the built page really does change was the one moment nothing was
   watching for it. showUpNote already arbitrates which notice wins, so the
   ranking does not need a second gate here. */
const probe = async (): Promise<void> => {
  const seen = await distProbe()
  if (seen === null) return
  if (seen.behind) showUpNote('behind')
  else hideBehindNote()
  if (seen.tag === null) return
  if (distBase === null) { distBase = seen.tag; return }
  if (seen.tag !== distBase) showUpNote('ui')
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
  noteRow = note
  note.onclick = () => {
    if (upKind === 'ver') askUpgrade()
    else if (upKind === 'behind') explainBehind()
    else window.location.reload()
  }
  /* The click stays wired either way -- the version notice this row also
     carries comes from the gateway, which the dev server proxies. Only the
     dist watch is built-page-only; see distProbe. */
  if (!import.meta.env.PROD) return
  watching = true
  void probe()
  setInterval(probe, 30000)
}
