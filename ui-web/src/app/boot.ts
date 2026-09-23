/* What the page does between "the bundle has run" and "the reader can work".
 *
 * One sequence, in one place, called once from main.tsx: claim the splash and
 * hold the rail, then ask the gateway everything a first frame needs.
 *
 * Which transport answers the calls is decided before any of this runs
 * (src/rpc/chooseTransport.ts): a page opened from disk or with ?stub=1 reads the
 * offline fixture library through the very same sequence.
 *
 * The two elements it reaches for are the ones no component renders: the
 * pre-JavaScript splash, removed outright inside the desktop shell, and
 * `.rail`, which carries the pending-counts flag from before the first paint
 * until the real counts land.
 */

import { load as paneLoad } from '../chrome/behaviour/panes'
import { goPaint } from '../features/composer/mount'
import { warm as warmCron } from '../features/cron/store'
import { refresh as refreshImportSync } from '../features/importSync/store'
import { loadExt } from '../features/installed/source'
import { setupState } from '../features/model/source'
import { open as openOnboard } from '../features/onboard/store'
import { loadSessions, sessionsSource } from '../features/rail/source'
import { draw as sessionDraw, hold as holdRail, release as releaseRail } from '../features/rail/store'
import { loadSettingsWithProviders, pushPermMode } from '../features/settings/source'
import { redraw as redrawSettings } from '../features/settings/store'
import { hostPlatformSet } from '../lib/platform'
import { setCurrent as sessionSet } from '../lib/session'
import { hasUpdateFlag } from '../rpc/capabilities'
import { gateway } from '../rpc/gateway'
import { draw as drawCtx } from '../state/ctxChip'
import { setRuntime } from '../state/envChip'
import { bootError } from '../state/failureBar'
import { load as loadLang } from '../state/lang/pick'
import { load as lookLoad } from '../state/look'
import { draw as drawPerm } from '../state/perm'
import { set as setRail } from '../state/rail'
import { replayPendingApprovals } from '../state/session/pipeline'
import { switchTo, switchToDraft } from '../state/session/registry'
import { landing, watch as watchSessionNote } from '../state/session/resume'
import { open as sessionOpen, rows as sessionRows, sess } from '../state/session/rows'
import { sources } from '../state/sources'
import { load as loadTier } from '../state/tier'
import { bump as bumpWs } from '../state/ws'
import { authFail, bootFail, shellReady, surface } from './connection'
import { installPage } from './install'
import { hideSplash } from './splash'
import { appVersionSet, resumeUpgrade, showUpNote, watchForUpdates } from './updates'

import type { SessRow } from '../features/rail/types'

/* `update_available` and `latest_version` ride along with the version answer
   without being in the contract's result schema: the gateway adds them from
   its own update cache, so a server too old to have one omits both and the
   notice row simply stays hidden (see rpc/capabilities.hasUpdateFlag). */
const latestOf = (v: unknown): string | undefined => (v as { latest_version?: string }).latest_version

/* The page's own claim on the first frame: the shell's markers, the splash,
   and the rail held on skeleton rows until the first list lands. Everything
   below the source install reads it, so the source goes on the seam first. */
function claimFirstFrame(): void {
  if (/RavenShell/.test(navigator.userAgent)) {
    document.documentElement.dataset.shell = '1'
    /* Inside the app the native overlay is the one and only splash: it covers
       this window from launch until the boot below posts {type:"ready"}. The
       page-level splash would just replay the same scene under it and be caught
       mid-fade when the overlay lifts -- the "two splashes" launch. */
    const s = document.getElementById('splash')
    if (s) s.remove()
  }

  /* Set before the deferred first paint, cleared once the real counts land. */
  const rail = document.querySelector('.rail') as HTMLElement | null
  if (rail) rail.dataset.counts = 'pending'
  sources.rail = sessionsSource
  holdRail()
  sessionSet(null)
  /* From here on the pointer is the page's own, so what it says can be recorded
     for the next reload. Started after the line above on purpose: the demo
     chrome has already opened its canned session on this page, and both that
     and the clear above are fixture noise the note must not carry (see
     state/session/resume.ts). */
  watchSessionNote()
}

/* Everything a first frame needs from the gateway, in the order it needs it.
 *
 * The language is not a step here any more: the store resolves it as it loads,
 * from the remembered pick, else the reader's own languages, else what the
 * document declares (state/lang/store.ts), so the notice that explains a page
 * which cannot connect is in the reader's language before this runs. The middle
 * step is what makes that true for a reader the gateway has never answered for,
 * the connect below returning before `loadLang` is reached. */
async function sequence(): Promise<void> {
  /* The first connect is the one place where a socket that never opened really
     does mean the session is not welcome: nothing has been served to this page
     yet that could have come from a gateway which then went away. The rejoin
     path decides differently, and has to -- see the transport's rejoin and the
     reconnect UI in app/connection.ts. */
  if (!(await gateway().connect())) { authFail(); return }
  try {
    const hello = await gateway().call('system.hello', { client_version: '0.1.0', surface: surface() })
    if (hello && hello.platform) hostPlatformSet(hello.platform)
    // Before the first paint of anything data-driven: config.language decides
    // what every label below says.
    await loadLang()
    const v = await gateway().call('system.version', {})
    if (v.raven_version) appVersionSet(v.raven_version)
    /* Absent until system.version carries them; the row simply stays hidden,
       so an older server degrades to no notice rather than a broken one. */
    if (hasUpdateFlag(v)) showUpNote('ver', latestOf(v))
    /* That answer came from the update cache, which the gateway refreshes on a
       poll -- so between a publish and the next poll it names a version that is
       already superseded, and the banner promises one build while the button
       installs whatever is newest at click time. Ask again with `check: true`,
       after the paint and deliberately not awaited, so the number the reader is
       shown is the number they will get. */
    gateway().call('system.version', { check: true })
      .then((fresh) => { if (hasUpdateFlag(fresh)) showUpNote('ver', latestOf(fresh)) })
      .catch(() => {})
    await loadSessions()
    releaseRail()
    /* Home is the new-task screen, never the last session: opening straight
       into someone else's half-finished transcript is a worse first frame than
       an empty composer, and the rail is one click away. A draft writes nothing
       to disk, so this costs no empty session either.

       A RELOAD is not a first frame, though. The reader was already in a
       conversation and did not ask to leave it -- the page was replaced under
       them, by a refresh or by an upgrade -- so the tab's own note is what
       decides here, and it exists only for a tab that was already somewhere
       (state/session/resume.ts). Asked of the list rather than opened blind: a
       conversation deleted since is a note for something that is not there any
       more, and the new-task screen is the right answer for it. */
    const back = landing(sessionRows().map((s: SessRow) => s.id))
    if (back) await switchTo(sess(back) as SessRow)
    else switchToDraft()
    /* After the conversation is up and the dock with it, so a question waiting
       in another conversation lights the line above the composer. */
    void replayPendingApprovals()
    /* A first run opens on the wizard: nothing can answer a turn until a
       provider is set, and the wizard is where one gets set. `setupState` is
       what the task actions read to send the reader to Models instead, for
       the page a reader closes the wizard on without finishing; the wizard
       re-reads it when it closes. ?onboard=1 reopens the wizard on a
       configured install, for a design or support pass. */
    try {
      const setup = await gateway().call('setup.status', {})
      const firstRun = setup.provider_configured === false
      setupState.providerConfigured = !firstRun
      /* ?onboard=demo opens the same wizard from onLoad() over the canned
         transport, whose setup.status answers "first run" until a model is
         picked; opening it here as well would open it twice. */
      const cannedInstead = /[?&]onboard=demo/.test(location.search)
      if (!cannedInstead && (firstRun || /[?&]onboard=1/.test(location.search))) {
        openOnboard()
      }
    } catch (e) {
      if (window.console) console.warn('[live boot] setup.status failed; skipping onboarding gate', e)
    }
    hideSplash()
    shellReady()
    /* pushPermMode after the load, same as the settings island's two callers:
       the chip must reflect the server mode on cold boot, not the localStorage
       cache -- the gate enforces the server's answer either way. */
    loadSettingsWithProviders().then(pushPermMode).catch(() => {})
    /* The tier chip's first read. `session.onChange` covers every switch after
       this, but not the state the page boots into: a page with no conversation
       restored never changes session, so the chip would stay hidden on the one
       screen where the reader is about to start a conversation. */
    loadTier()
    /* The import row's first read. Here rather than with the seam installers:
       those run before the socket opens, and a read there is refused as "not
       connected" -- which left a run the gateway lost to a restart, still in
       the importer's file, off the rail until the wizard next closed. */
    void refreshImportSync()
    /* Refresh the rail badges from real data right away -- until these resolve
       the badges stay suppressed (data-counts="pending") rather than showing
       the demo mock's phantom counts. */
    Promise.allSettled([
      loadExt(),
      warmCron(),
    ]).then(() => {
      const rail = document.querySelector('.rail') as HTMLElement | null
      if (rail) delete rail.dataset.counts
    })
    watchForUpdates()
    resumeUpgrade()
  } catch (e) {
    bootFail(e)
  }
}

/* The first data-driven paint: everything a page can draw from what it already
   holds, queued by `boot` below after every synchronous source installer so
   that none of it observes a half-wired seam.
 *
 * Each step is isolated so one failure stays visible and the rest still
 * renders. The order is the order the concatenated page ran them in. */
export function bootPage(): void {
  ;([
    ['lookLoad', (): void => lookLoad()],
    ['paneLoad', (): void => paneLoad()],
    ['setRail', (): void => setRail(true)],
    ['sessionDraw', (): void => sessionDraw()],
    /* A live boot deliberately starts with an empty source and chooses a draft
       after the real list lands; an offline page has a fixture row to open. */
    ['sessionOpen', (): void => { const first = sessionRows()[0]; if (first) void sessionOpen(first) }],
    ['drawPerm', (): void => drawPerm()],
    ['loadTier', (): void => { void loadTier() }],
    ['drawCtx', (): void => drawCtx()],
    ['bumpWs', (): void => bumpWs()],
    ['drawSettings', (): void => redrawSettings()],
    ['setRuntime', (): void => setRuntime()],
    ['goState', (): void => goPaint()],
  ] as Array<[string, () => void]>).forEach(([where, step]) => {
    try { step() } catch (e) { bootError(where, e) }
  })
}

/* The loaded page. The splash is the boot sequence's to lift, so what is left
   on this event is the one URL flag that asks for the canned onboarding pass.
   Registered with the page's other window listeners
   (src/state/globalListeners.ts). */
export function onLoad(): void {
  if (/[?&]onboard=demo/.test(location.search)) openOnboard()
}

/**
 * The page's boot, in order. main.tsx calls this once, after the chrome has
 * installed itself.
 *
 * The three steps are load-bearing in this order: the seam has to answer before
 * the rail is held (the hold paints), every source has to be installed before
 * the first data-driven paint is queued, and the queue is the last thing here
 * for exactly that reason.
 */
export function boot(): void {
  claimFirstFrame()
  installPage()
  void sequence()
  /* Every synchronous source installer is in place, so the first data-driven
     paint cannot observe a half-wired seam. */
  queueMicrotask(bootPage)
}
