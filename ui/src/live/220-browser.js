/* -- the embedded browser -------------------------------------------------
   One Chromium behind the RPC, driven from two sides: the agent's browser_*
   tools and this panel. While the panel is showing it holds a watch lease:
   the page's viewport is resized to the stage's own CSS size and Chromium
   pushes a frame per paint (browser.frame notifications), so what is on
   screen IS the page -- 1:1 pixels, no thumbnail, no poll latency. The lease
   is renewed while showing and dropped when hidden, and the server expires
   it on its own if the panel dies without saying goodbye. */
/* Absent, not merely unusable. `avail: false` means the server has the surface
   and cannot use it right now (no chromium); this means the server does not
   have it at all -- a -32601 from any browser.* call. The two want different
   answers: the first is worth a reason and a retry, the second means the page
   must stop offering the feature, including the transcript's link handler.

   Kept separate because this page ships ahead of those handlers on purpose:
   `browser.*` and `subagent.*` arrive with the MRs that implement them, and
   until then a client that behaves as though they exist is a client that looks
   like it works. */
/* Two surfaces this page is deliberately ahead of: browser.{open,close,frame,
   input,mode,tabs,watch} arrive with the browser MR, and subagent.{list,context}
   with the one that writes the transcripts they read. On this revision every
   one of them answers -32601, so the page has to look like a client whose
   server does not have them -- not like a broken client. */
const RPC_ABSENT = new Set();
const rpcGone = (name, e) => {
  if (e && e.code === -32601) RPC_ABSENT.add(name);
  return RPC_ABSENT.has(name);
};
const rpcHas = (name) => !RPC_ABSENT.has(name);
/* The shell draws the empty states, and it is outside this closure. */
rpcAbsent = (name) => RPC_ABSENT.has(name);

const BR = {
  avail: null,      // null = not asked yet
  reason: '',
  started: false,
  url: '',
  title: '',
  busy: false,
  err: '',
  timer: null,
  vp: [1280, 800],  // the page's CSS viewport, from the last watch/frame
  watching: false,
  noWatch: false,   // no browser.watch here -> legacy frame polling
  keep: null,
  ro: null,
  rsz: 0,
  headful: false,   // popped out into a real window: no stage, no watch
  frameQ: null,     // newest undecoded frame; older ones are simply dropped
  painting: false,
  lastBlob: null,   // repaints a rebuilt stage without waiting for a frame
  loading: false,   // active tab is between navigation commit and load
  canBack: false,
  canFwd: false,
  tabs: [],         // [{index,url,title,active,loading}] from browser.tabs
  tabTimer: null,   // strip refresh while the panel is showing
  lastTried: '',    // last URL the reader asked for; the error page retries it
};

const brShowing = () => wsOpen && wsTab === 'browser';

/* Decode-and-draw with backpressure: while one frame is in createImageBitmap,
   arrivals overwrite frameQ instead of queueing, so after a decode stall the
   stage shows the page as it is now -- never a replay of where it has been.
   Drawing into a canvas (not an <img> src swap) keeps WKWebKit from running
   layout + data-URI churn on every frame. */
async function brPaint(blob) {
  BR.frameQ = blob;
  BR.lastBlob = blob;
  if (BR.painting) return;
  BR.painting = true;
  while (BR.frameQ) {
    const b = BR.frameQ;
    BR.frameQ = null;
    try {
      const bmp = await createImageBitmap(b);
      const cv = document.querySelector('#wsBody .bstage canvas.shot');
      if (cv) {
        if (cv.width !== bmp.width || cv.height !== bmp.height) { cv.width = bmp.width; cv.height = bmp.height; }
        cv.getContext('2d').drawImage(bmp, 0, 0);
        const w = cv.parentElement.querySelector('.waiting');
        if (w) w.remove();
      }
      bmp.close();
    } catch { /* a torn frame; the next paint replaces it */ }
  }
  BR.painting = false;
}

const brB64Blob = (b64) => {
  const s = atob(b64);
  const u = new Uint8Array(s.length);
  for (let i = 0; i < s.length; i++) u[i] = s.charCodeAt(i);
  return new Blob([u], { type: 'image/jpeg' });
};

function brFrameMeta(head) {
  BR.avail = true;
  const was = BR.started;
  BR.started = true;
  if (head.vw) BR.vp = [head.vw, head.vh];
  const navved = head.url && head.url !== BR.url;
  if (head.url) BR.url = head.url;
  if (typeof head.loading === 'boolean' && head.loading !== BR.loading) {
    BR.loading = head.loading;
    brSyncChrome();
    if (!BR.loading) brTabsSync();
  }
  if (!brShowing()) return false;
  if (!was || !document.querySelector('#wsBody .bstage')) { drawWs(); }
  const bar = document.querySelector('#wsBody .bbar .url');
  if (bar && document.activeElement !== bar) bar.value = BR.url;
  if (navved) { brSecSync(); brTabsSync(); }
  return true;
}

/* In-place chrome updates: a frame must never rebuild the panel (that is what
   interrupts typing), so loading/lock/tab changes patch the DOM they own. */
function brSyncChrome() {
  const bar = document.querySelector('#wsBody .bbar');
  if (!bar) return;
  const prog = bar.querySelector('.bprog');
  if (prog) prog.hidden = !BR.loading;
  const rl = bar.querySelector('.brl');
  if (rl) {
    rl.innerHTML = '';
    rl.appendChild(ico(BR.loading ? 'M6 6l12 12M18 6L6 18' : 'M4.5 12a7.5 7.5 0 1 0 2.6-5.7M4.5 5.5V10h4.5'));
    rl.title = T(BR.loading ? 'gui.br.stop' : 'gui.br.reload');
  }
  const back = bar.querySelector('.bk');
  if (back) back.disabled = !BR.started || !BR.canBack;
  const fwd = bar.querySelector('.fw');
  if (fwd) fwd.disabled = !BR.started || !BR.canFwd;
}

function brSecSync() {
  const sec = document.querySelector('#wsBody .burl .sec');
  if (!sec) return;
  const https = /^https:/i.test(BR.url);
  const http = /^http:/i.test(BR.url);
  sec.classList.toggle('ok', https);
  sec.classList.toggle('warn', http);
  sec.innerHTML = https
    ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M8 11V8a4 4 0 0 1 8 0v3M6 11h12v9H6z"/></svg>'
    : http
      ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M12 5v8M12 17.5v.5"/></svg>'
      : '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4.3-4.3"/></svg>';
}

function brStateIn(r) {
  if (typeof r.loading === 'boolean') BR.loading = r.loading;
  if (typeof r.can_back === 'boolean') BR.canBack = r.can_back;
  if (typeof r.can_forward === 'boolean') BR.canFwd = r.can_forward;
  brSyncChrome();
  brSecSync();
}

/* Screencast frames arrive as binary WS messages:
   "RVF1" + u32 header length + JSON header + raw JPEG. */
rpc.binary = (buf) => {
  const u8 = new Uint8Array(buf);
  if (u8.length < 8 || u8[0] !== 0x52 || u8[1] !== 0x56 || u8[2] !== 0x46 || u8[3] !== 0x31) return;
  const hl = new DataView(buf).getUint32(4);
  let head;
  try { head = JSON.parse(new TextDecoder().decode(u8.subarray(8, 8 + hl))); } catch { return; }
  if (brFrameMeta(head)) brPaint(new Blob([u8.subarray(8 + hl)], { type: 'image/jpeg' }));
};

/* Old servers still notify frames as base64 JSON; same pipeline after decode. */
rpc.notify['browser.frame'] = (p) => {
  if (brFrameMeta(p) && p.jpeg) brPaint(brB64Blob(p.jpeg));
};

async function brWatch(on) {
  if (BR.noWatch) { brTick(on && brShowing()); return; }
  if (!on) {
    if (BR.keep) { clearInterval(BR.keep); BR.keep = null; }
    if (BR.watching) { BR.watching = false; rpc.call('browser.watch', { on: false }).catch(() => {}); }
    brTabTick(false);
    return;
  }
  const stage = document.querySelector('#wsBody .bstage');
  const r = stage && stage.getBoundingClientRect();
  const p = { on: true, quality: 70 };
  if (r && r.width > 50 && r.height > 50) { p.width = Math.round(r.width); p.height = Math.round(r.height); }
  try {
    const res = await rpc.call('browser.watch', p);
    BR.watching = !!res.watching;
    if (res.vw) BR.vp = [res.vw, res.vh];
    brTick(false);
  } catch (e) {
    if (e && e.code === -32601) { BR.noWatch = true; brTick(true); return; }
    BR.err = e.message || String(e);
  }
  /* Renewing with an unchanged size is a heartbeat, not a restart -- the
     server only reopens the screencast when the size or quality moved. */
  if (!BR.keep) {
    BR.keep = setInterval(() => {
      if (brShowing() && BR.started) brWatch(true);
      else brWatch(false);
    }, 10000);
  }
}

/* Legacy pull path. Still used for: the first "is a browser even possible"
   ask, the idle wait for a page the agent might open, and the whole view on
   a server whose browser surface has no watch. */
async function brPoll(force) {
  if (BR.busy || !rpcHas('browser')) return;
  /* The poll cancels its own timer once the panel stops showing, so a page left
     open behind a closed panel is not being screenshotted every second. */
  if (!force && !brShowing()) { brTick(false); return; }
  BR.busy = true;
  try {
    const r = await rpc.call('browser.frame', { quality: 70 });
    BR.avail = r.available !== false;
    RPC_ABSENT.delete('browser');
    BR.reason = r.reason || r.error || '';
    const wasStarted = BR.started;
    const wasHeadful = BR.headful;
    BR.started = !!r.started;
    BR.headful = !!r.headful;
    BR.url = r.url || '';
    BR.title = r.title || '';
    if (!BR.started) BR.lastBlob = null;
    /* Repaint the whole panel only when the shape changed; a new frame draws
       onto the canvas in place, so typing in the address bar is not interrupted
       every time a frame lands. */
    if (wasStarted !== BR.started || wasHeadful !== BR.headful) { if (brShowing()) drawWs(); return; }
    if (r.jpeg) brPaint(brB64Blob(r.jpeg));
    brStateIn(r);
    const bar = document.querySelector('#wsBody .bbar .url');
    if (bar && document.activeElement !== bar) bar.value = BR.url;
  } catch (e) {
    /* The write side of the same gate the top of this function reads. Without
       it `browser` never entered the absent set -- `brOpen`'s catch was the only
       other route, and gating the transcript's click handler closed that one --
       so `BR.avail` stayed null, and the panel's own `avail === null` branch
       called back into here through drawWs as fast as the gateway could answer
       -32601. A read side with no write side is a loop, not a guard. */
    if (rpcGone('browser', e)) { BR.avail = false; BR.reason = T('gui.br.absent'); BR.err = ''; }
    else BR.err = (e.data && e.data.detail) || e.message || String(e);
  } finally {
    BR.busy = false;
  }
}

function brTick(on) {
  if (BR.timer) { clearInterval(BR.timer); BR.timer = null; }
  if (on) BR.timer = setInterval(() => brPoll(false), 900);
}

async function brOpen(url) {
  BR.err = '';
  BR.lastTried = url;
  try {
    const r = await rpc.call('browser.open', { url });
    if (r.error) BR.err = r.error;
    BR.avail = r.available !== false;
    brStateIn(r);
  } catch (e) {
    /* A server without the surface is not a failed navigation: say so once,
       stop offering the panel, and let the next link go to the real browser. */
    if (rpcGone('browser', e)) { BR.avail = false; BR.reason = T('gui.br.absent'); BR.err = ''; }
    else BR.err = (e.data && e.data.detail) || e.message || String(e);
  }
  await brPoll(true);
  drawWs();
  brTabsSync();
}

async function brGo(action) {
  try {
    const r = await rpc.call('browser.open', { action });
    if (r) { BR.err = r.error || ''; brStateIn(r); }
  } catch { /* state poll reports it */ }
  await brPoll(true);
  brTabsSync();
}

