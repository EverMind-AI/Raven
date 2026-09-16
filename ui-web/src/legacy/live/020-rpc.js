/* ---- what a connection looks like to the reader --------------------
   The transport is src/rpc/wsTransport.ts now: the socket, the pending calls,
   the rejoin and its backoff all live there, and main.tsx installs it as the
   page's gateway before this layer is. What stayed here is everything that
   paints -- the reconnect status line, the upgrade shade, the auth banner and
   the desktop shell's reauth handshake -- driven off the transport's own
   connection state. */

import { islands } from '../../islands'
import { gateway } from '../../state/gateway'
import { T } from '../demo/010-kernel.js'
import { failureBar, upShade } from '../demo/040-state.js'
import { showStatus } from '../demo/070-transcript.js'
import { hideSplash } from '../demo/160-boot.js'
import { shellReady } from './010-boot-guard.js'
import { distMoved, upKind, upMarkClear } from './210-update-notice.js'

let SHELL;

/* What this connection calls itself in system.hello, so a trace can tell the
   GUI shell from the browser page on one gateway. Identity only -- both still
   share the tui session pool. Derived in install() beside SHELL, because that
   is where the user agent is read. */
let SURFACE = 'page';

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
let reauthTries = 0;
function askShellReauth() {
  if (!SHELL || reauthTries >= 2) return false;
  try {
    window.webkit.messageHandlers.raven.postMessage({ type: 'reauth' });
  } catch {
    return false;
  }
  reauthTries++;
  return true;
}
function authFail() {
  // The banner paints under the splash (z 99 < 120); a splash that stays up
  // would turn a readable failure into an endless loading screen. Same for
  // the shell's native overlay -- the failure must be readable there too.
  hideSplash(0);
  shellReady();
  if (askShellReauth()) {
    failureBar(T('gui.auth.retry'));
    return;
  }
  const bar = failureBar(T(SHELL ? 'gui.auth.dead_app' : 'gui.auth.checking'));
  if (SHELL) return;
  /* "Not authenticated OR the service stopped" made the reader guess between
     two causes with opposite fixes -- and a restarted `serve` mints a fresh
     cookie, so the common case is a live service that no longer knows this
     tab. /health is unauthenticated precisely so it can answer this: it
     replies to a browser holding a cookie the gateway has already forgotten. */
  fetch('/health', { cache: 'no-store' })
    .then((r) => r.ok && r.json())
    .then((j) => { bar.say(T(j && j.service ? 'gui.auth.stale' : 'gui.auth.dead')); })
    .catch(() => { bar.say(T('gui.auth.dead')); });
}

/* Anything that breaks after the socket is up is NOT an auth failure. Blaming
   auth for it sends the reader to restart a service that is running fine while
   the real cause (a config the loader rejects, an engine that failed to build)
   stays invisible. */
function bootFail(e) {
  hideSplash(0);
  shellReady();
  const detail = (e && e.data && (e.data.detail || e.data.reason)) || '';
  const msg = [(e && e.message) || String(e), detail].filter(Boolean).join(' - ');
  failureBar(T('gui.boot_fail', { where: 'live boot', err: msg }));
  // A dead boot must not leave the rail shimmering forever under the banner.
  islands.rail.release();
  if (window.console) console.error('[live boot]', e);
}

/* What has to happen again once a dropped connection is back. A registry
   rather than one slot, because the transport reports a reconnect to whoever
   is listening and this layer is what decides the order things are refetched
   in; live/080-overrides.js is the one registrar today. */
const reconnectHandlers = new Set();
function onReconnect(fn) {
  reconnectHandlers.add(fn);
  return () => reconnectHandlers.delete(fn);
}

/* The upgrade card this reconnect raised, and only this one.

   Both give-up exits below end in authFail(), which paints a red bar and
   nothing that clears a full-window shade -- the card has no dismiss
   affordance unless something calls fail() on it, and nothing here does. A
   shade left behind is therefore the same dead end this function exists to
   remove, with a blur over the rest of the window. */
let shade = null;
const dropShade = () => { if (shade) { shade.close(); shade = null; } };

/* The reconnect as the reader sees it. The transport says what it is doing;
   every line below is what the old rejoin loop painted while it did.

   `attempt` is how many tries have already failed, so 0 is the moment of the
   drop itself and anything above it is a retry that came back empty. */
async function onConnectionState(state, info) {
  const attempt = (info && info.attempt) || 0;
  if (state === 'reconnecting' && attempt === 0) {
    // In the DOM, not a toast: a silent drop mid-turn reads as the model
    // hanging forever, which is exactly the bug report this line answers.
    try { showStatus(T('gui.reconnecting')); } catch { /* pre-boot */ }
    return;
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
    if (!shade && !document.querySelector('.upshade')
        && typeof upKind !== 'undefined' && upKind === 'ver') {
      shade = upShade();
      shade.say(T('gui.upg.working'));
    }
    return;
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
      upMarkClear();
      window.location.reload();
      return;
    }
    /* Same build after all -- the gateway just restarted. Take the card back
       down, since there is nothing left to wait for and no reload coming to
       remove it. */
    dropShade();
    for (const fn of reconnectHandlers) fn();
    return;
  }
  if (state === 'auth-failed') {
    /* The socket refused while HTTP answers, or twenty minutes went by: the
       transport has stopped trying and only the reader can move this on. */
    dropShade();
    authFail();
  }
}

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  SHELL = /RavenShell/.test(navigator.userAgent);
  SURFACE = SHELL ? 'shell' : 'page';
  gateway().onState(onConnectionState);
}

export { SHELL, SURFACE, reauthTries, askShellReauth, authFail, bootFail, reconnectHandlers, onReconnect, onConnectionState }
