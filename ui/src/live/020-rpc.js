/* ---- rpc client -------------------------------------------------- */

/* How long a rejoin keeps trying, and how long it ever sleeps between tries.
   The ceiling matches the upgrade watcher's (20 minutes) on purpose: the reason
   the gateway is away this long is almost always an upgrade, and the two should
   not disagree about when to stop hoping. The 8s cap keeps a page left open
   overnight from hammering a machine that is simply off. */
const REJOIN_CEILING_MS = 1200000;
const REJOIN_MAX_WAIT_MS = 8000;
const rpc = {
  ws: null, next: 1, pending: new Map(), notify: {}, open: false,
  onReconnect: null,
  binary: null,  // sink for binary WS messages (screencast frames)
  connect() {
    return new Promise((resolve) => {
      const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/rpc`);
      ws.binaryType = 'arraybuffer';
      this.ws = ws;
      ws.onopen = () => { this.open = true; resolve(true); };
      ws.onmessage = (m) => {
        if (m.data instanceof ArrayBuffer) { if (this.binary) this.binary(m.data); return; }
        let f; try { f = JSON.parse(m.data); } catch { return; }
        if (f.id != null && this.pending.has(f.id)) {
          const p = this.pending.get(f.id); this.pending.delete(f.id);
          f.error ? p.reject(f.error) : p.resolve(f.result);
        } else if (f.method && this.notify[f.method]) {
          this.notify[f.method](f.params || {});
        }
      };
      ws.onclose = () => {
        const was = this.open; this.open = false;
        this.pending.forEach((p) => p.reject({ code: -1, message: 'connection closed' }));
        this.pending.clear();
        /* A socket that never opened is reported, not interpreted. It means one
           of two very different things -- the gateway refused this session, or
           there is no gateway right now -- and only the caller has the context
           to tell them apart. Deciding here is what made an upgrade look like a
           sign-in failure. */
        if (!was) { resolve(false); return; }
        // In the DOM, not a toast: a silent drop mid-turn reads as the model
        // hanging forever, which is exactly the bug report this line answers.
        try { showStatus(T('gui.reconnecting')); } catch { /* pre-boot */ }
        this.rejoin();
      };
    });
  },
  /* Keep trying, with backoff, instead of the single 1.5s attempt this
     replaces. The one thing that reliably takes the gateway away is an upgrade
     replacing the installation under it, and an upgrade is minutes -- a cold
     one measured nine. So the old retry was guaranteed to fire while the
     backend was still absent, fail, and fall through to authFail(), which told
     the reader their sign-in had expired and left the page there for good.

     `alive()` is what separates absent from refused: the page is served by the
     same process as /rpc, so an HTTP answer means the gateway is back and the
     socket closing anyway is a real auth refusal. No answer means keep waiting.
     Only when the first is true does this give up and say so. */
  rejoin() {
    if (this.rejoining) return;
    this.rejoining = true;
    const t0 = Date.now();
    let wait = 1500;
    let shade = null;
    const alive = async () => {
      try {
        const r = await fetch('/', { method: 'HEAD', cache: 'no-store' });
        return r.status !== 401 && r.status !== 403 ? r : null;
      } catch { return null; }
    };
    /* Takes down a card this rejoin raised, and only that one. Both give-up
       exits below end in authFail(), which paints a red bar and nothing that
       clears a full-window shade -- the card has no dismiss affordance unless
       something calls fail() on it, and nothing here does. A shade left behind
       is therefore the same dead end this function exists to remove, with a
       blur over the rest of the window. */
    const drop = () => { if (shade) { shade.close(); shade = null; } };
    const tick = async () => {
      if (Date.now() - t0 > REJOIN_CEILING_MS) { this.rejoining = false; drop(); authFail(); return; }
      if (await this.connect()) {
        this.rejoining = false;
        /* The gateway that came back may be serving a different build than the
           one this page was loaded from -- that is exactly the upgrade case --
           and the running scripts cannot be swapped in place. Reload onto it,
           and only then; a plain drop and recover must not throw the transcript
           away. */
        if (await distMoved()) {
          /* Clear the marker before reloading, because this reload races the
             upgrade watcher's own. Whichever poller loses would otherwise come
             back up, read a marker that is still live, and drop the upgrade
             card over a page that is already healthy on the new build -- then
             reload a second time to clear it. */
          upMarkClear();
          window.location.reload();
          return;
        }
        /* Same build after all -- the gateway just restarted. Take the card
           back down, since there is nothing left to wait for and no reload
           coming to remove it. */
        drop();
        if (this.onReconnect) this.onReconnect();
        return;
      }
      /* The socket refused while HTTP answers: the gateway is there and this
         session is not welcome. Retrying cannot fix that. */
      if (await alive()) { this.rejoining = false; drop(); authFail(); return; }
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
      wait = Math.min(Math.round(wait * 1.6), REJOIN_MAX_WAIT_MS);
      setTimeout(tick, wait);
    };
    setTimeout(tick, wait);
  },
  call(method, params) {
    if (!this.open) return Promise.reject({ code: -1, message: 'not connected' });
    const id = this.next++;
    this.ws.send(JSON.stringify({ jsonrpc: '2.0', id, method, params: params || {} }));
    return new Promise((resolve, reject) => this.pending.set(id, { resolve, reject }));
  },
};

const SHELL = /RavenShell/.test(navigator.userAgent);

/* What this connection calls itself in system.hello, so a trace can tell the
   GUI shell from the browser page on one gateway. Identity only — both still
   share the tui session pool. */
const SURFACE = SHELL ? 'shell' : 'page';

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
  RavenIslands.rail.release();
  if (window.console) console.error('[live boot]', e);
}
