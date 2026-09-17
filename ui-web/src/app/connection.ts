/* The gateway connection as the reader sees it, and the two handshakes this
 * page has with whatever is hosting it.
 *
 * The socket, the pending calls, the rejoin and its backoff all live in the
 * transport (src/rpc/wsTransport.ts), and main.tsx installs it as the page's
 * gateway before anything asks for one. What is here is everything that
 * PAINTS -- the reconnect status line, the upgrade shade, the auth banner --
 * driven off the transport's own connection state, plus the desktop shell's
 * `ready` and `reauth` messages, because the shell is the other end of both
 * the splash and the credential this page cannot mint.
 */

import { islands } from '../features/registry'
import { T } from '../i18n/t'
import { gateway } from '../rpc/gateway'
import { show as failureBar } from '../state/failureBar'
import { open as upShade } from '../state/upgradeShade'
import { hideSplash } from './splash'
import { distMoved, upgradeKind, upMarkClear } from './updates'

import type { ConnectionState, StateInfo } from '../rpc/transport'
import type { UpgradeShade } from '../state/upgradeShade'

/* Whether this page is the desktop shell's own window. Read on demand rather
   than latched at boot: the user agent cannot change under a loaded page, and
   a module-scope read would fire in every test that so much as imports this. */
const isShell = (): boolean => /RavenShell/.test(navigator.userAgent)

/* What this connection calls itself in system.hello, so a trace can tell the
   GUI shell from the browser page on one gateway. Identity only -- both still
   share the tui session pool. */
export const surface = (): 'shell' | 'page' => (isShell() ? 'shell' : 'page')

/* Tells the shell the page has real pixels worth revealing. A no-op in a
   plain browser tab, where the page-level splash handles the same moment. */
export function shellReady(): void {
  try {
    (window as unknown as { webkit: { messageHandlers: { raven: { postMessage(m: unknown): void } } } })
      .webkit.messageHandlers.raven.postMessage({ type: 'ready' })
  } catch { /* not the shell */ }
}

/* Only for a socket that never opened: the cookie no longer matches the running
   gateway's token (a serve restarted without RAVEN_SERVE_TOKEN mints a fresh
   one), or the browser dropped the session cookie.

   The page cannot fix this by itself and that is deliberate -- minting a nonce
   takes the shared secret in an X-Raven-Token header and is never
   cookie-callable, so no page script can issue its own credential. The desktop
   shell can: the secret is in ~/.raven/serve.json, which it reads and the page
   cannot. So in the app we ask the shell to redo the launch handshake; in a
   browser tab there is nobody to ask, and the banner stands.

   Capped at two tries because the shell reloads the page on success, which
   resets this counter -- the shell throttles its own side as well. */
let reauthTries = 0
export function askShellReauth(): boolean {
  if (!isShell() || reauthTries >= 2) return false
  try {
    (window as unknown as { webkit: { messageHandlers: { raven: { postMessage(m: unknown): void } } } })
      .webkit.messageHandlers.raven.postMessage({ type: 'reauth' })
  } catch {
    return false
  }
  reauthTries++
  return true
}

export function authFail(): void {
  // The banner paints under the splash (z 99 < 120); a splash that stays up
  // would turn a readable failure into an endless loading screen. Same for
  // the shell's native overlay -- the failure must be readable there too.
  hideSplash(0)
  shellReady()
  if (askShellReauth()) {
    failureBar(T('gui.auth.retry'))
    return
  }
  const bar = failureBar(T(isShell() ? 'gui.auth.dead_app' : 'gui.auth.checking'))
  if (isShell()) return
  /* "Not authenticated OR the service stopped" made the reader guess between
     two causes with opposite fixes -- and a restarted `serve` mints a fresh
     cookie, so the common case is a live service that no longer knows this
     tab. /health is unauthenticated precisely so it can answer this: it
     replies to a browser holding a cookie the gateway has already forgotten. */
  fetch('/health', { cache: 'no-store' })
    .then((r) => r.ok && r.json())
    .then((j: { service?: unknown } | false) => { bar.say(T(j && j.service ? 'gui.auth.stale' : 'gui.auth.dead')) })
    .catch(() => { bar.say(T('gui.auth.dead')) })
}

/* Anything that breaks after the socket is up is NOT an auth failure. Blaming
   auth for it sends the reader to restart a service that is running fine while
   the real cause (a config the loader rejects, an engine that failed to build)
   stays invisible. */
export function bootFail(e: unknown): void {
  hideSplash(0)
  shellReady()
  const err = e as { data?: { detail?: string; reason?: string }; message?: string } | null
  const detail = (err && err.data && (err.data.detail || err.data.reason)) || ''
  const msg = [(err && err.message) || String(e), detail].filter(Boolean).join(' - ')
  failureBar(T('gui.boot_fail', { where: 'live boot', err: msg }))
  // A dead boot must not leave the rail shimmering forever under the banner.
  islands.rail.release()
  if (window.console) console.error('[live boot]', e)
}

/* What has to happen again once a dropped connection is back. A registry
   rather than one slot, because the transport reports a reconnect to whoever
   is listening and this module is what decides the order things are refetched
   in; the page's wiring (app/install.ts) is the one registrar today. */
export const reconnectHandlers = new Set<() => void>()
export function onReconnect(fn: () => void): () => void {
  reconnectHandlers.add(fn)
  return () => reconnectHandlers.delete(fn)
}

/* The upgrade card this reconnect raised, and only this one.

   Both give-up exits below end in authFail(), which paints a red bar and
   nothing that clears a full-window shade -- the card has no dismiss
   affordance unless something calls fail() on it, and nothing here does. A
   shade left behind is therefore the same dead end this function exists to
   remove, with a blur over the rest of the window. */
let shade: UpgradeShade | null = null
const dropShade = (): void => { if (shade) { shade.close(); shade = null } }

/* The reconnect as the reader sees it. The transport says what it is doing;
   every line below is what the old rejoin loop painted while it did.

   `attempt` is how many tries have already failed, so 0 is the moment of the
   drop itself and anything above it is a retry that came back empty. */
export async function onConnectionState(state: ConnectionState, info?: StateInfo): Promise<void> {
  const attempt = (info && info.attempt) || 0
  if (state === 'reconnecting' && attempt === 0) {
    // In the DOM, not a toast: a silent drop mid-turn reads as the model
    // hanging forever, which is exactly the bug report this line answers.
    try { islands.transcript.status(T('gui.reconnecting')) } catch { /* pre-boot */ }
    return
  }
  if (state === 'reconnecting') {
    /* Still absent, and the page was already told a newer version exists --
       so the overwhelmingly likely reason it went away is that version
       landing. Say so with the same card the page shows for an upgrade it
       started itself, animated bar and all. The reader's complaint that
       started this was that an upgrade begun from the app or the terminal
       showed them nothing at all while the page sat dead.
       Guarded on the notice rather than shown for every drop: a shade over
       the whole window is the wrong answer to a two-second blip, and only a
       pending version makes an absence explainable.
       Guarded on there being no card up yet for a second reason, and this one
       is about an upgrade this page started itself: that card belongs to
       watchUpgrade, which is still writing into it and still owes the reader
       the two answers only it has -- what failed, and the command to run by
       hand. Minting one here would take that card out of the document
       (upShade clears them before building) while the watcher went on
       addressing the detached node, so the reader would lose the message on
       exactly the paths that had one. Every page-initiated upgrade reaches
       here: serve exits about a second after `system.upgrade` replies, and
       the close drives the socket into this rejoin. */
    if (!shade && !document.querySelector('.upshade') && upgradeKind() === 'ver') {
      shade = upShade()
      shade.say(T('gui.upg.working'))
    }
    return
  }
  if (state === 'reconnected') {
    /* The gateway that came back may be serving a different build than the
       one this page was loaded from -- that is exactly the upgrade case --
       and the running scripts cannot be swapped in place. Reload onto it, and
       only then; a plain drop and recover must not throw the transcript away. */
    if (await distMoved()) {
      /* Clear the marker before reloading, because this reload races the
         upgrade watcher's own. Whichever poller loses would otherwise come
         back up, read a marker that is still live, and drop the upgrade card
         over a page that is already healthy on the new build -- then reload a
         second time to clear it. */
      upMarkClear()
      window.location.reload()
      return
    }
    /* Same build after all -- the gateway just restarted. Take the card back
       down, since there is nothing left to wait for and no reload coming to
       remove it. */
    dropShade()
    for (const fn of reconnectHandlers) fn()
    return
  }
  if (state === 'auth-failed') {
    /* The socket refused while HTTP answers, or twenty minutes went by: the
       transport has stopped trying and only the reader can move this on. */
    dropShade()
    authFail()
  }
}

/** The one registration this module needs on the page's transport. */
export function installConnectionUI(): void {
  gateway().onState(onConnectionState)
}
