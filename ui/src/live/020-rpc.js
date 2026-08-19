/* ---- rpc client -------------------------------------------------- */
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
        if (!was) { authFail(); resolve(false); return; }
        // In the DOM, not a toast: a silent drop mid-turn reads as the model
        // hanging forever, which is exactly the bug report this line answers.
        try { showStatus(T('gui.reconnecting')); } catch { /* pre-boot */ }
        setTimeout(async () => {
          if (await this.connect()) { if (this.onReconnect) this.onReconnect(); }
        }, 1500);
      };
    });
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
  const bar = mk('div', 'topfail');
  if (askShellReauth()) {
    bar.textContent = T('gui.auth.retry');
    document.body.appendChild(bar);
    return;
  }
  bar.textContent = T(SHELL ? 'gui.auth.dead_app' : 'gui.auth.checking');
  document.body.appendChild(bar);
  if (SHELL) return;
  /* "Not authenticated OR the service stopped" made the reader guess between
     two causes with opposite fixes -- and a restarted `serve` mints a fresh
     cookie, so the common case is a live service that no longer knows this
     tab. /health is unauthenticated precisely so it can answer this: it
     replies to a browser holding a cookie the gateway has already forgotten. */
  fetch('/health', { cache: 'no-store' })
    .then((r) => r.ok && r.json())
    .then((j) => { bar.textContent = T(j && j.service ? 'gui.auth.stale' : 'gui.auth.dead'); })
    .catch(() => { bar.textContent = T('gui.auth.dead'); });
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
  const bar = mk('div', 'topfail');
  bar.textContent = T('gui.boot_fail', { where: 'live boot', err: msg });
  document.body.appendChild(bar);
  // A dead boot must not leave the rail shimmering forever under the banner.
  listReady = true;
  drawList();
  if (window.console) console.error('[live boot]', e);
}

