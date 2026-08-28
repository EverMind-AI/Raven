/* -- hot-update notice ------------------------------------------------
   `raven serve` streams dist straight from disk and stamps static
   responses with an mtime+size ETag, so a rebuilt dist is detectable
   with a HEAD probe -- no backend support needed. The page never
   reloads itself: a reload mid-turn would drop the live transcript,
   so the amber row in the rail foot waits for a click. */
/* Two different things can be newer than what this window is running: the built
   page on disk, and the released version of Raven itself. They share the
   rail-foot row because to the reader they are one sentence — something newer
   exists — and they differ only in what the click does. */
let upKind = null;
let upLatest = null;

function showUpNote(kind, latest) {
  const note = $('#upnote');
  if (!note) return;
  /* a pending version upgrade outranks a rebuilt page: upgrading reloads anyway */
  if (upKind === 'ver' && kind === 'ui') return;
  upKind = kind;
  if (latest) upLatest = latest;
  const t = note.querySelector('.t');
  const rl = note.querySelector('.rl');
  if (kind === 'ver') {
    t.textContent = upLatest ? T('gui.upg.note', { v: `v${upLatest}` }) : T('gui.upg.note_bare');
    rl.textContent = T('gui.upg.go');
  } else {
    t.textContent = T('gui.update.note');
    rl.textContent = T('gui.update.reload');
  }
  note.hidden = false;
}

/* An upgrade outlives the page that started it: serve exits, a detached helper
   installs, and what comes back is a fresh load. The marker is how any load
   tells "an upgrade is running" from "no upgrade has been asked for" -- without
   it, a second click starts a second upgrade against a half-removed install,
   and the reader is shown the raw failure of a doomed call. */
const UPG_KEY = 'raven.upgrade';
const UPG_CEILING_MS = 1200000;

function upMark(to) {
  try { localStorage.setItem(UPG_KEY, JSON.stringify({ to: to || null, t0: Date.now() })); } catch { /* private mode */ }
}

function upMarkRead() {
  try {
    const m = JSON.parse(localStorage.getItem(UPG_KEY) || 'null');
    if (!m || !m.t0 || Date.now() - m.t0 > UPG_CEILING_MS) return null;
    return m;
  } catch { return null; }
}

function upMarkClear() {
  try { localStorage.removeItem(UPG_KEY); } catch { /* private mode */ }
}

function askUpgrade() {
  /* Already running: re-enter the progress dialog rather than offering to
     start it again. Closing that dialog must not strand the reader. */
  const running = upMarkRead();
  if (running) { watchUpgrade(upShade(), running.t0); return; }
  if (turn.busy()) {
    confirmAsk(T('gui.upg.title'), T('gui.upg.body_busy'), T('gui.upg.close'), () => {});
    return;
  }
  confirmAsk(T('gui.upg.title'),
    T('gui.upg.body', { from: `v${APP_VERSION || '?'}`, to: `v${upLatest || '?'}` }),
    T('gui.upg.go'), runUpgrade);
}

/* Called at boot: a page that loads while an install is in flight re-attaches
   to it, instead of coming up as if nothing were happening. */
function resumeUpgrade() {
  const running = upMarkRead();
  if (running) watchUpgrade(upShade(), running.t0);
}

/* `system.upgrade` hands the install to a detached helper and then lets serve
   exit, so this page has to survive a gap with no backend: it polls until serve
   answers again and only then reloads (the new dist needs a reload anyway). The
   cookie survives the restart because the relaunched server reuses the same
   session token. */
async function runUpgrade() {
  const shade = upShade();
  shade.say(T('gui.upg.working'));
  try {
    await rpc.call('system.upgrade', {});
  } catch (e) {
    /* The server saw an install already in flight. That is the dialog the
       reader wanted, not an error -- adopt the run instead of reporting it. */
    if (e.data && e.data.reason === 'in_progress') {
      upMark(upLatest);
      watchUpgrade(shade);
      return;
    }
    upMarkClear();
    shade.fail(T('gui.upg.failed'), (e.data && e.data.detail) || e.message || String(e));
    return;
  }
  upMark(upLatest);
  watchUpgrade(shade);
}

/* Poll until serve answers again, then reload -- the new dist needs one anyway,
   and the cookie survives because the relaunched server reuses the session
   token. The ceiling is 20 minutes because a first upgrade resolves and
   byte-compiles every dependency: one measured cold run took nine. The old
   three-minute ceiling declared failure over a install that was still running,
   which is what taught the reader to click upgrade a second time. */
function watchUpgrade(shade, since) {
  const t0 = since || Date.now();
  shade.say(T('gui.upg.working'));
  const tick = async () => {
    if (Date.now() - t0 > UPG_CEILING_MS) {
      upMarkClear();
      shade.fail(T('gui.upg.failed'), T('gui.upg.gave_up'));
      return;
    }
    let r = null;
    try {
      r = await fetch('/', { method: 'HEAD', cache: 'no-store' });
    } catch {
      setTimeout(tick, 1500);
      return;
    }
    if (r.status === 401 || r.status === 403) { upMarkClear(); shade.fail(T('gui.upg.reauth'), ''); return; }
    if (!r.ok) { setTimeout(tick, 1500); return; }
    upMarkClear();
    window.location.reload();
  };
  /* wait out the handoff: probing too early answers from the process that is
     about to exit, and the page would reload onto a dying server */
  setTimeout(tick, 2500);
}

/* The validator for the build this page was loaded from. Kept here rather than
   inside the watcher below because the rpc client's rejoin asks for it too: a
   gateway that comes back on a different build cannot be lived with, and that
   is the same question this watcher asks on a timer. */
let distBase = null;

const distProbe = async () => {
  try {
    const r = await fetch('/', { method: 'HEAD', cache: 'no-store' });
    if (!r.ok) return null;
    return r.headers.get('etag') || r.headers.get('last-modified') || null;
  } catch { return null; }
};

/* True only when the build is known to have CHANGED. An unreachable server, a
   server that sends no validator, or a first look with nothing to compare
   against all answer false -- reloading on a maybe would throw a live
   transcript away for nothing. */
async function distMoved() {
  const tag = await distProbe();
  if (tag === null || distBase === null) return false;
  return tag !== distBase;
}

function watchForUpdates() {
  const note = $('#upnote');
  if (!note) return;
  const probe = async () => {
    /* Deliberately NOT skipped while a notice is already showing. It used to
       be, and that is what made this watcher blind exactly when it mattered:
       the version notice is up precisely when an upgrade is about to land, so
       the one moment the built page really does change was the one moment
       nothing was watching for it. showUpNote already arbitrates which notice
       wins, so the ranking does not need a second gate here. */
    const tag = await distProbe();
    if (tag === null) return;
    if (distBase === null) { distBase = tag; return; }
    if (tag !== distBase) showUpNote('ui');
  };
  note.onclick = () => { if (upKind === 'ver') askUpgrade(); else window.location.reload(); };
  probe();
  setInterval(probe, 30000);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') probe();
  });
}
