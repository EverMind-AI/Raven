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

function upShade() {
  document.querySelectorAll('.upshade').forEach((n) => n.remove());
  const shade = mk('div', 'upshade');
  const card = mk('div', 'upcard');
  /* The waiting state is a bar and one line. An install the reader cannot
     hurry does not become shorter by being described, and the staged copy this
     replaced -- a stage name, a running clock, and a paragraph after ninety
     seconds explaining itself -- spent the whole wait telling someone with
     nothing to decide that there was still nothing to decide. */
  const bar = mk('div', 'upbar');
  bar.appendChild(mk('i'));
  const title = mk('div', 't');
  card.append(bar, title);
  shade.appendChild(card);
  document.body.appendChild(shade);
  return {
    say(text) {
      title.textContent = text;
    },
    /* A failure is the one state with something to decide, so it is the one
       state that gets more than a line: what went wrong, and the command that
       does it by hand. */
    fail(text, detail) {
      bar.hidden = true;
      title.textContent = text;
      const sub = mk('div', 'sub', detail ? `${detail}\n${T('gui.upg.manual')}` : T('gui.upg.manual'));
      const cmd = mk('div', 'cmd');
      const code = mk('code', null, 'raven upgrade');
      const cp = mk('button', null, T('gui.dtl.copy'));
      cp.onclick = () => { if (navigator.clipboard) navigator.clipboard.writeText('raven upgrade'); };
      cmd.append(code, cp);
      const foot = mk('div', 'foot');
      const close = mk('button', 'btn', T('gui.upg.close'));
      close.onclick = () => shade.remove();
      foot.appendChild(close);
      card.append(sub, cmd, foot);
    },
  };
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
  if (busy) {
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

function watchForUpdates() {
  const note = $('#upnote');
  if (!note) return;
  let base = null;
  const probe = async () => {
    if (!note.hidden) return;
    try {
      const r = await fetch('/', { method: 'HEAD', cache: 'no-store' });
      if (!r.ok) return;
      const tag = r.headers.get('etag') || r.headers.get('last-modified');
      if (!tag) return;
      if (base === null) { base = tag; return; }
      if (tag !== base) showUpNote('ui');
    } catch { /* serve unreachable; the connection banner already covers it */ }
  };
  note.onclick = () => { if (upKind === 'ver') askUpgrade(); else window.location.reload(); };
  probe();
  setInterval(probe, 30000);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') probe();
  });
}

