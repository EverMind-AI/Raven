/* ══ live mode ═════════════════════════════════════════════════════
   Wires the demo shell to a running `raven serve` over the /rpc
   WebSocket. Loaded over http(s) it replaces the canned replay with
   real turn events; opened from disk (file://) or with ?stub=1 the
   demo keeps its mock data untouched. */
(() => {
if (!/^http/.test(location.protocol) || /(^|[?&])stub=1/.test(location.search)) return;

if (/RavenShell/.test(navigator.userAgent)) {
  document.documentElement.dataset.shell = '1';
  /* Inside the app the native overlay is the one and only splash: it covers
     this window from launch until the boot below posts {type:"ready"}. The
     page-level splash would just replay the same scene under it and be caught
     mid-fade when the overlay lifts -- the "two splashes" launch. */
  const s = document.getElementById('splash');
  if (s) s.remove();
}

/* Tells the shell the page has real pixels worth revealing. A no-op in a
   plain browser tab, where the page-level splash handles the same moment. */
const shellReady = () => {
  try { window.webkit.messageHandlers.raven.postMessage({ type: 'ready' }); } catch { /* not the shell */ }
};

/* Claims the splash and the onboarding moment from the demo shell: its load
   handler backs off when this flag is set, and the boot below decides when
   the splash lifts and whether first-run setup is due (setup.status). */
window.__liveBoot = 1;

/* Set before the first paint, cleared once the real counts land: the demo boot
   has already written its mock attention counts into the rail badges by the
   time this runs, and letting them through was the badge that flashed on the
   rail on every refresh. */
(() => { const r = document.querySelector('.rail'); if (r) r.dataset.counts = 'pending'; })();

/* The demo shell has already seeded and painted its mock data by the time this
   runs. In live mode none of it may reach the eye: clear it synchronously (same
   task as the demo paint, so nothing mock survives to the first frame) and hold
   the session rail on skeleton rows until the real list lands. */
SESS = []; cur = null;
SKILLS.length = 0; PLUGINS.length = 0; CRONS.length = 0; TOOLS.length = 0;
$('#stage').innerHTML = '';
$('#flash').textContent = '';
$('#title').textContent = T('gui.new_task');
let listReady = false;
{
  const origDrawList = drawList;
  drawList = function () {
    if (listReady) { origDrawList(); return; }
    const box = $('#list'); box.innerHTML = '';
    for (let i = 0; i < 6; i++) {
      const r = mk('div', 'sess skel');
      const bar = (w, h) => { const b = mk('span', 'sk'); b.style.cssText = `width:${w};height:${h}`; return b; };
      r.append(bar(`${52 + ((i * 17) % 30)}%`, '11px'), bar('28px', '9px'));
      box.appendChild(r);
    }
  };
  drawList();
}

/* No toasts: every state change the user triggers is already visible where
   they made it (the list redraws, the switch moves, the transcript shows the
   error), so a floating strip only repeats it. Failures still reach the
   console for diagnosis. */
toast = function (text) {
  if (window.console) console.info('[raven]', text);
};

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

/* ---- session list ------------------------------------------------ */
const DAY = 86400000;
/* A row's stamp says when the session last answered, so it always carries a
   clock -- a bare date cannot tell two of yesterday's sessions apart. The year
   only appears once it is not this one; inside the current year it is noise. */
/* A clock only earns its place on today's rows: further back, the day is what
   the reader is placing the session by, and "yesterday 22:07" spends four
   characters saying something they did not ask. */
function whenGroup(epochS) {
  const d = new Date(epochS * 1000), now = new Date();
  const day0 = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const t = d.getTime();
  const hm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  const parts = { y: d.getFullYear(), m: d.getMonth() + 1, d: d.getDate() };
  const dated = d.getFullYear() === now.getFullYear()
    ? T('gui.time.md', parts)
    : T('gui.time.ymd', parts);
  if (t >= day0) return { when: hm, g: '今天' };
  if (t >= day0 - DAY) return { when: T('gui.time.yest'), g: '昨天' };
  if (t >= day0 - 2 * DAY) return { when: T('gui.time.dbyest'), g: '更早' };
  if (t >= day0 - 6 * DAY) return { when: dated, g: '本周' };
  return { when: dated, g: '更早' };
}

/* Scheduled runs live in cron:<job_id> sessions with no title of their own;
   the job's name is what the user recognises, so the list borrows it. */
let cronNames = {};

async function loadCronNames() {
  try {
    const r = await rpc.call('cron.list', {});
    cronNames = {};
    (r.jobs || []).forEach((j) => { cronNames[j.id] = j.name; });
  } catch { /* keep whatever we had */ }
}

function rowFrom(it) {
  /* Last activity, not creation: the list is ordered by when a session last
     answered, so labelling rows with their birthday put the clock and the
     order in disagreement. */
  const at = it.updated_at || it.started_at || 0;
  const { when, g } = whenGroup(at);
  const cron = it.source === 'cron';
  const jobId = cron ? String(it.id).split(':').pop() : null;
  /* Cron turns open with the scheduler's "[Scheduled Task] Timer ..." wrapper;
     the task's own words start after "Task '". Rows for deleted jobs (no name
     to borrow) fall back to that inner text rather than the wrapper. */
  let prev = (it.preview || '').trim();
  if (cron) {
    const m = prev.match(/Task '([^']+)'/);
    prev = m ? m[1] : prev.replace(/^\[Scheduled Task\]\s*/, '');
  }
  return {
    id: it.id,
    title: (cron && cronNames[jobId]) || it.title || prev.slice(0, 24)
      || T('gui.sess.fallback_title', { id: String(it.id).split(':').pop().slice(0, 15) }),
    last: it.preview ? it.preview.slice(0, 60) : T('gui.sess.n_messages', { n: it.message_count }),
    when, g, at, run: null, live: true, from: cron ? 'cron' : undefined,
    pin: !!it.pinned,
  };
}

/* The rail clock tracks the LATEST message regardless of author: sending
   stamps the row right away, and the finished reply stamps it again. The
   re-sort keeps the list order agreeing with the clocks it shows. */
function touchSession(id) {
  const s = sess(id);
  if (!s) return;
  s.at = Math.floor(Date.now() / 1000);
  const wg = whenGroup(s.at);
  s.when = wg.when;
  s.g = wg.g;
  SESS.sort((a, b) => (b.at || 0) - (a.at || 0));
  drawList();
}

const SESS_CHANNELS = ['tui', 'cron'];

async function loadSessions() {
  await loadCronNames();
  const r = await rpc.call('session.list', { channels: SESS_CHANNELS });
  SESS = (r.sessions || []).map(rowFrom).sort((a, b) => (b.at || 0) - (a.at || 0));
  if (!SESS.length) {
    SESS = [{ id: 'tui:default', title: T('gui.new_task'), last: T('gui.sess.not_started'),
      when: T('gui.sess.just_now'), g: '今天', run: null, live: true }];
  }
}

/* Tool results arrive wrapped in prompt-injection guards
   ([BEGIN UNTRUSTED ...] / [END UNTRUSTED ...]). Those markers protect the
   model, not the reader — strip them from every preview. */
function cleanPreview(text) {
  return String(text || '')
    .split('\n')
    .filter((l) => !/^\s*\[(BEGIN|END) UNTRUSTED /.test(l))
    .join('\n')
    .trim();
}

/* A completed call's success is guessed from its result text until the wire
   carries a real flag (pending backend change): the registry stamps failed
   calls with its retry hint, error-shaped first lines count, and
   understand_media reports per-file failures inline. */
function okOf(name, preview) {
  if (preview.includes('[Analyze the error above')) return false;
  if (/^\s*(error|traceback|failed)\b/i.test(preview)) return false;
  if (name === 'understand_media' && preview.includes('[could not understand:')) return false;
  return true;
}

/* ---- history rendering ------------------------------------------- */
/* session.resume carries the assistant's tool_calls as [{id, name, arguments}]
   with `arguments` a raw JSON string, and each tool result names the call it
   answers. Indexing them by id is what lets a restored row say which file it
   read instead of just "read". */
function callIndex(messages) {
  const byId = new Map();
  messages.forEach((m) => {
    if (!m || m.role !== 'assistant' || !Array.isArray(m.tool_calls)) return;
    m.tool_calls.forEach((c) => {
      let args = {};
      try { args = JSON.parse(c.arguments || '{}') || {}; } catch (e) { args = {}; }
      byId.set(String(c.id || ''), { name: c.name || '', args });
    });
  });
  return byId;
}

/* The turn's own clock is gone by the time it is restored (nothing stores how
   long a call took), so a restored fold header says only that the work
   happened -- see collapseTurn(null). Wall-clock stamps do survive. */
/* Raven's own words, drawn as Raven's own words. The runtime writes this text
   in English and hands it over as a KIND, so the sentence is chosen here, in
   the reader's language -- the old canned string was pushed down the token
   stream and arrived as the model's answer, in English, in whatever paragraph
   the model happened to be mid-way through. */
function noticeRow(kind, detail) {
  const box = mk('div', 'rnote in');
  const hd = mk('div', 'hd');
  hd.appendChild(ico('M12 8.5v4M12 16h.01M10.3 3.9 2.6 17.2A1.6 1.6 0 0 0 4 19.6h16a1.6 1.6 0 0 0 1.4-2.4L13.7 3.9a1.6 1.6 0 0 0-2.8 0Z'));
  hd.appendChild(mk('span', '', T('gui.notice.by_raven')));
  box.appendChild(hd);
  /* An unknown kind still gets a row: a client one release behind the runtime
     should say something happened rather than drop it on the floor. */
  box.appendChild(mk('div', 'tx', T('gui.notice.' + kind, null, kind)));
  if (detail) box.appendChild(mk('div', 'why', detail));
  return box;
}

function renderHistory(messages) {
  /* Drawing somebody else's run into the panel says nothing about whether
     THIS conversation has started, so the new-task state stays as it was. */
  const ch = stageHost ? null : document.querySelector('.chat');
  if (ch) delete ch.dataset.fresh;
  let toolRun = null;
  const sealTools = () => { if (toolRun) { toolRun.seal(); toolRun = null; } };
  const calls = callIndex(messages);
  /* One footer per turn: only the LAST assistant text before the next user
     message is the turn's answer; the ones before it are the model narrating
     mid-turn and render as quiet prose, exactly like a live turn. */
  const isFinal = messages.map((m, i) => {
    if (!(m && m.role === 'assistant' && m.text && m.text.trim())) return false;
    for (let j = i + 1; j < messages.length; j += 1) {
      const n = messages[j];
      if (n && n.role === 'user' && n.text && n.text.trim()) return true;
      if (n && n.role === 'assistant' && n.text && n.text.trim()) return false;
    }
    return true;
  });
  /* Call durations were never stored, but the wall-clock stamps were: the gap
     between the question and the answer that closed it IS how long the turn
     took, so a restored fold header carries the same time a live one does. */
  let turnAt = 0;
  const msOf = (t) => { const d = new Date(t); const v = d.getTime(); return isNaN(v) ? 0 : v; };
  messages.forEach((m, i) => {
    if (m.role === 'user' && m.text && m.text.trim()) {
      sealTools(); turnAt = msOf(m.timestamp); ask(m.text, stamp(m.timestamp)); return;
    }
    if (m.role === 'assistant' && m.notice) {
      /* Stored as an assistant message because the MODEL has to read it on the
         next turn -- but a reader must not, or the reload contradicts the live
         view about who said it. */
      sealTools();
      const endAt = msOf(m.timestamp);
      collapseTurn(turnAt && endAt && endAt - turnAt >= 1000 ? dur(endAt - turnAt) : null);
      stageBox().appendChild(noticeRow(m.notice.kind || '', m.notice.detail || ''));
      return;
    }
    if (m.role === 'assistant' && m.turn_ended) {
      /* The closing marker of a turn that was stopped or died: everything the
         turn streamed is already drawn above; this renders the same note the
         live stop shows, instead of the marker's model-facing text. */
      sealTools();
      const endAt = msOf(m.timestamp);
      collapseTurn(turnAt && endAt && endAt - turnAt >= 1000 ? dur(endAt - turnAt) : null);
      const r = mk('div', 'act in');
      const note = m.turn_ended.status === 'cancelled'
        ? T('gui.halted')
        : T('gui.turn_died', { e: firstErrLine(m.turn_ended.reason || '') || '' });
      r.append(mk('span', 'g'), mk('span', 'arg', note));
      stageBox().appendChild(r);
      return;
    }
    if (m.role === 'assistant') {
      const thought = String(m.reasoning_content || '').trim();
      const text = String(m.text || '').trim();
      /* The thought opens the step its calls will land in, so a restored turn
         reads in the same order it happened: thought, then work. */
      if (thought) {
        sealTools();
        toolRun = newStep();
        toolRun.hasThink = true;
        toolRun.cot.textContent = thought;
        toolRun.reveal();
        /* 0, not the elapsed time: how long it thought was never stored, and a
           clock counting from page load would be a lie. */
        toolRun.thinkDone(0);
      }
      if (!text) return;
      if (isFinal[i]) {
        sealTools();
        const endAt = msOf(m.timestamp);
        /* Sessions written before the turn-start stamp landed have the whole
           turn on one clock read. A sub-second gap is that artifact, not a
           measurement -- show the bare header rather than a fake "1s". */
        collapseTurn(turnAt && endAt && endAt - turnAt >= 1000 ? dur(endAt - turnAt) : null);
        const body = answerBlock(text, stamp(m.timestamp));
        body.innerHTML = md(text);
      } else if (thought) {
        toolRun.hasSay = true;
        toolRun.say.innerHTML = md(text);
      } else {
        sealTools();
        const stp = mk('div', 'step in');
        const s = mk('div', 'say prose');
        s.innerHTML = md(text);
        stp.appendChild(s);
        stageBox().appendChild(stp);
      }
      return;
    }
    if (m.role === 'tool') {
      if (!toolRun) { toolRun = newStep(); }
      /* The result names the call it answers, so the row can carry the target
         it was given -- and an edit's arguments still yield its diff. */
      const hit = calls.get(String(m.tool_call_id || '')) || {};
      /* An ACP transcript puts a human title where a tool name goes, so the
         program becomes the verb and the command becomes the argument -- the
         same shape every other call in this transcript already has. A raven
         tool name has no colon and comes back unchanged. */
      const parts = callParts(hit.name || m.name || 'tool');
      const h = toolRun.tool(parts.name, hit.args || null, parts.display || null);
      const preview = cleanPreview(m.text).split('\n').slice(0, 8).map((l) => l.slice(0, 160)).join('\n');
      /* The stored diff is the live event's diff, written down: same argument,
         same numbered rows after a reload as before it. */
      h.done(okOf(m.name || '', preview), preview, 0, m.diff);
    }
  });
  sealTools();
  down();
}

/* ---- live turn state machine ------------------------------------- */
const live = { subId: null, st: null, steps: [], say: '', open: new Map(), sawEpisode: false };

function resetTurnState() {
  stopSayPaint();
  live.st = null; live.steps = []; live.say = ''; live.open.clear(); live.sawEpisode = false;
  live.startedAt = Date.now();
  live.answerAt = 0;
}

function ensureStep() {
  if (!live.st) { live.st = newStep(); live.steps.push(live.st); }
  return live.st;
}

/* The prose is re-parsed from the whole buffer on every token, so rendering per
   delta means thousands of full parses and DOM swaps for one answer -- visible
   as scroll jank, and it destroys any text the reader has selected inside the
   answer. One paint per frame is as often as anyone can read. */
let sayJob = null;
function paintSay() {
  if (sayJob) return;
  const run = () => {
    sayJob = null;
    if (live.st) live.st.say.innerHTML = md(live.say);
    down();
  };
  /* A frame callback never fires while the window is hidden, and a turn that
     streams into a backgrounded window still has to land its text -- so a
     hidden window falls back to a timer. */
  sayJob = document.hidden ? { t: setTimeout(run, 120), timer: true } : { t: requestAnimationFrame(run) };
}
function stopSayPaint() {
  if (!sayJob) return;
  if (sayJob.timer) clearTimeout(sayJob.t); else cancelAnimationFrame(sayJob.t);
  sayJob = null;
}

function flushSay() {
  /* Promote the episode's streamed narration: intermediate episodes keep it
     as step prose; handled at message.complete for the final answer. */
  live.say = '';
}

/* How long the turn took: asked, to the last word of the answer. Not until the
   runtime is idle -- the housekeeping that follows a turn (memory deposit) can
   run for another ten seconds, and counting it would make this number disagree
   with the one a reloaded transcript computes from the stamps on disk. Under a
   second the duration floors at 1s, matching what the restored transcript
   shows for the same turn. */
const turnDur = () => {
  const ms = (live.answerAt || Date.now()) - live.startedAt;
  return live.startedAt ? dur(Math.max(ms, 1000)) : null;
};

/* The turn folds ONCE, at message.complete. Folding as soon as prose started
   arriving was tried and reverted: mid-stream nothing can tell the answer from
   another line of narration, so a turn that narrates between calls folded again
   and again, guessing wrong each time. The end of the turn is the first moment
   the answer is known. */

function onEvent(ev) {
  const p = ev.payload || {};
  if (ev.type === 'message.start') {
    /* Read BEFORE busy is set, because `busy` is exactly the discriminator:
       the window that sent this turn set it locally and has already drawn the
       question, while a window that is only watching has not. Drawing it here
       is what makes a turn in flight legible to a second window (or a shared
       link) -- the user entry is not written to the transcript until the turn
       ends, so this event is the only place the question exists yet. */
    if (!busy && p.content) ask(p.content);
    busy = true; goState(); drawMeter();
    WS.turn += 1;
  } else if (ev.type === 'episode.start') {
    if (live.st) { live.st.seal(); }
    flushSay();
    live.st = newStep(); live.steps.push(live.st); live.sawEpisode = true;
    raw.push('episode.start');
  } else if (ev.type === 'notice') {
    killStatus();
    /* Seals the open step first: this ends the turn, so the streamed prose
       above stays where it was said instead of being adopted by whatever
       comes next. */
    if (live.st) { live.st.seal(); live.st = null; }
    flushSay();
    const host = stageBox();
    /* Before the live glyph, like every other row that lands mid-turn -- after
       it the notice would sit under the spinner still claiming to be working. */
    host.insertBefore(noticeRow(p.kind || '', p.detail || ''), host.querySelector(':scope > .turnlive'));
    down();
  } else if (ev.type === 'thinking.delta') {
    killStatus();
    const st = ensureStep();
    st.hasThink = true;
    st.cot.textContent += p.text || '';
    st.reveal();
  } else if (ev.type === 'token.delta') {
    killStatus();
    const st = ensureStep();
    /* Prose means the thought is over -- fold it, or a finished thought keeps
       shimmering above the answer it already produced. */
    st.thinkDone();
    st.hasSay = true;
    live.say += p.text || '';
    live.answerAt = Date.now();
    paintSay();
  } else if (ev.type === 'tool.start') {
    killStatus();
    const st = ensureStep();
    const h = st.tool(p.name || 'tool', p.arguments, p.display);
    live.open.set(p.tool_call_id, { h, st, t0: Date.now(), name: p.name, args: p.arguments });
    raw.push(`tool.start     ${p.name}`);
    /* The workspace panel gets the WHOLE argument object, not the one-line
       display string: edit_file's old_text/new_text is the diff. */
    if (typeof wsOnTool === 'function') wsOnTool(p.name, p.arguments, false);
  } else if (ev.type === 'tool.complete') {
    const o = live.open.get(p.tool_call_id);
    if (!o) return;
    live.open.delete(p.tool_call_id);
    const preview = cleanPreview(p.result_preview).split('\n').map((l) => l.slice(0, 160)).join('\n');
    const ok = okOf(o.name || '', preview);
    const took = Date.now() - o.t0;
    o.h.done(ok, preview, took, null, p.truncated);
    /* p.diff is the real change on disk -- the only place a whole-file write's
       previous content survives, so the panel prefers it over the guess it made
       from the arguments at tool.start. */
    if (typeof wsOnToolDone === 'function') wsOnToolDone(o.name, o.args, ok, preview, took, p.diff);
  } else if (ev.type === 'message.complete') {
    finishTurn(p.usage || {});
  } else if (ev.type === 'error') {
    killStatus();
    /* A cancelled turn is the one "error" a person asked for. The stop button
       already finalised this stage; the event still matters when the cancel
       came from ANOTHER client on the same session -- and either way it is the
       server saying the engine is free, which is what the queue waits on. */
    if (p.reason === 'cancelled_by_client') {
      if (busy) softStop();
      if (!cancelInFlight) setTimeout(drainQueue, 400);
      return;
    }
    busy = false;
    /* A turn that died upstream (a provider timeout, a rate limit) is the one
       error worth offering to re-run, and re-running means the message that
       started it -- not a fresh guess at what the user wanted. */
    errLine(p.message || 'error', p.detail || p.reason || '', lastAsk ? () => send(lastAsk) : null);
    goState(); drawMeter(); drawList();
  } else if (ev.type === 'cron.delivered') {
    toast(T('gui.cron.new_output', { name: p.name }));
  } else if (ev.type === 'subagent.delivered') {
    /* The seam a delegated result re-enters the conversation at. The next
       thing to stream is the main agent retelling that result, and without
       this row it reads as raven speaking unprompted -- the reader deserves
       the "because" one line above the effect. Placed at the tail of the flow
       (before the live glyph), which is exactly where the injection happened
       in time; clicking it opens the run the result came from. */
    const row = mk('div', 'sdlv' + (p.status === 'error' ? ' err' : ''));
    row.appendChild(ico('M5 5v5a4 4 0 0 0 4 4h9M14 10l4 4-4 4', 'ic'));
    const isDag = p.kind === 'dag';
    const b = mk('button', 'nm', isDag ? T('gui.deleg.dag_title') : p.label);
    b.onclick = () => {
      if (isDag && p.run_id) {
        const d = dagFor();
        const first = d && d.run_id === p.run_id ? d.order[d.order.length - 1] : null;
        if (first) { dagOpenNode(p.run_id, { id: first }); return; }
      }
      if (!isDag && delegOpenSpawn) { delegOpenSpawn('', p.label); return; }
      setWs(true, 'agents');
    };
    row.appendChild(b);
    row.appendChild(mk('span', 'tx',
      T(p.status === 'error' ? 'gui.deleg.delivered_err' : 'gui.deleg.delivered')));
    const stage = $('#stage');
    if (stage) {
      const lr = stage.querySelector(':scope > .turnlive');
      stage.insertBefore(row, lr || null);
      down();
    }
  } else if (ev.type === 'cron.started') {
    // Mark (or seed) the job's row so the rail shows the run while it works;
    // the session file may not exist until the turn ends, hence the seed.
    cronNames[p.job_id] = p.name;
    const id = `cron:${p.job_id}`;
    let s = sess(id);
    if (!s) {
      s = { id, title: p.name, last: '', when: T('gui.sess.just_now'), g: '今天',
        at: Date.now() / 1000, run: null, live: true, from: 'cron' };
      SESS.unshift(s);
    }
    s.status = 'run';
    if (p.name) s.title = p.name;
    touchSession(id);
  } else if (ev.type === 'cron.finished') {
    const s = sess(`cron:${p.job_id}`);
    // A cron run finishes with nobody watching by definition, so the row
    // keeps the finished marker until it is opened.
    if (s) { s.status = p.ok ? 'done' : 'err'; touchSession(s.id); }
    refreshList();
    if ($('#cronPage').dataset.open === 'true') reloadCronPage();
  } else if (ev.type === 'dag.run_started') {
    /* The trail's delegation card paints the same events as the sheet below:
       one feed call per branch, before the sheet's own bookkeeping. */
    dagFlowFeed(ev.type, p);
    /* The graph arrives whole, before any node runs -- the server validates the
       whole thing or refuses it -- so the map is drawable from the first event
       and only the colours move after that.

       Filed under the conversation it belongs to. onEvent only ever runs for the
       open session (another session's frames are buffered by rpc.notify.event
       and replayed by restoreTurn, with `cur` restored first), so the current
       key is the owning key on both paths. */
    DAGS.set(sheetSession(), {
      run_id: p.run_id,
      session: sheetSession(),
      order: (p.nodes || []).map((n) => n.id),
      nodes: new Map((p.nodes || []).map((n) => [n.id, {
        id: n.id, subagent: n.subagent, instance: n.instance || null,
        depends_on: n.depends_on || [], status: 'pending', started_at: null, ended_at: null,
      }])),
      summary: null, done: false, folded: false,
    });
    drawDag();
  } else if (ev.type === 'dag.node_updated') {
    dagFlowFeed(ev.type, p);
    const d = dagFor();
    if (d && d.run_id === p.run_id) {
      const n = d.nodes.get(p.node);
      if (n) {
        n.status = p.status;
        n.started_at = p.started_at || n.started_at;
        n.ended_at = p.ended_at || n.ended_at;
        dagTouch(d);
      }
    }
  } else if (ev.type === 'dag.run_completed') {
    dagFlowFeed(ev.type, p);
    const d = dagFor();
    if (d && d.run_id === p.run_id) {
      /* Each file's status is the run's own last word on that node: a node the
         registry never got an update for (a run interrupted mid-flight) would
         otherwise sit at `running` forever. */
      (p.files || []).forEach((f) => {
        const n = d.nodes.get(f.node);
        if (n) { n.status = f.status; n.ended_at = n.ended_at || Date.now(); }
      });
      d.summary = p.summary || null;
      d.dir = p.dir || null;
      d.done = true;
      d.folded = true;
      dagTouch(d);
    }
  }
}

const fmtTok = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));

// Per-turn cost lives under each answer and "a turn is running" is now the
// ticking row above the composer, so the strip under the field stays empty.
drawMeter = function () {
  $('#meter').textContent = '';
  drawTurnLive();
  drawPill();
};

function finishTurn(usage) {
  killStatus();
  /* A queued paint would write the streamed prose back into a step this is
     about to empty. */
  stopSayPaint();
  if (live.st) live.st.seal();
  /* Final answer gets the answer block (copy / retry actions); the streamed
     step prose is replaced by it so the text does not appear twice. */
  if (live.st && live.say.trim()) {
    /* Read before the step can be removed: the answer belongs where the prose
       was streaming, not at the bottom of whatever arrived since. */
    const anchor = live.st.step.nextSibling;
    live.st.say.innerHTML = '';
    live.st.hasSay = false;
    if (!live.st.hasThink && !live.st.calls.length) {
      live.st.step.remove();
      live.steps.pop();
    }
    /* The footer carries when the answer landed; how long it took is on the
       turn's fold header. */
    const body = answerBlock(live.say, stamp(Date.now()), anchor);
    body.innerHTML = md(live.say);
  }
  foldSilentRuns(live.steps);
  /* Whatever is still loose joins the turn's fold (creating it if the turn
     folded nothing early). */
  collapseTurn(turnDur());
  busy = false;
  const inTok = usage.input_tokens || usage.prompt_tokens || 0;
  const outTok = usage.output_tokens || usage.completion_tokens || 0;
  use = { calls: tl.length, in: inTok, out: outTok, cost: usage.cost || 0 };
  /* The window fill is the turn's prompt, not the running total: every turn
     re-sends the conversation, so input tokens ARE what is in the window. */
  setCtx(usage.context_used || inTok, usage.context_max);
  raw.push('message.complete');
  const s = sess(cur);
  if (s && live.say.trim()) s.last = live.say.trim().split('\n')[0].slice(0, 60);
  touchSession(cur);
  // OS notification when the answer lands while the window is in the
  // background; ntfPush itself checks focus and the user's preference.
  ntfPush(T('gui.set.ntf.done'), (s && s.title) || live.say.trim().slice(0, 80));
  resetTurnState();
  drawMeter(); goState(); drawList(); down();
  if (q.length) { const nx = q.shift(); drawQ(); send(nx); }
}

/* The session's title IS the user's first message (first line, capped) — set
   the moment it is sent, not after the turn ends, so the rail and the top bar
   never sit on 新任务 while the agent works. */
function titleFromFirstMessage(text) {
  const s = sess(cur);
  if (!s || (s.title && s.title !== '新任务' && s.title !== T('gui.new_task'))) return;
  const t = text.trim().split('\n')[0].trim().slice(0, 30);
  if (!t) return;
  s.title = t;
  $('#title').textContent = plainTitle(t);
  drawList();
  rpc.call('session.title', { session_id: s.id, title: t }).catch(() => {});
}

/* A refused persist must not stay quiet. The row moves optimistically, but a
   pin the server never accepted looks identical to one it did until the page
   is reloaded and the group is simply gone -- which is exactly how an older
   resident gateway, with no session.pin to call at all, presents itself. Put
   the row back and say so. */
pinPersist = (id, pinned) => rpc.call('session.pin', { session_id: id, pinned: !!pinned })
  .catch((e) => {
    const s = sess(id);
    if (s) { s.pin = !pinned; drawList(); }
    toast(T('gui.sess.pin_failed', { detail: (e && (e.message || e.detail)) || String(e) }));
  });

async function refreshList() {
  try {
    const r = await rpc.call('session.list', { channels: SESS_CHANNELS });
    const rows = (r.sessions || []).map(rowFrom);
    const curRow = sess(cur);
    if (curRow && !rows.find((x) => x.id === cur)) rows.unshift(curRow);
    // Pins come back from the server now (session metadata), so a refresh must
    // NOT overlay the old in-memory flag: that would resurrect an unpin. The
    // running dot is still client state -- set by cron.started, cleared by
    // cron.finished, not by a list refresh.
    SESS.forEach((old) => {
      const nx = rows.find((x) => x.id === old.id);
      if (!nx) return;
      if (old.status) nx.status = old.status;
    });
    SESS = rows;
    drawList();
  } catch { /* keep the stale list */ }
}

/* ---- in-flight turns survive session switches ----------------------
   Server-side subscriptions are additive and never torn down here, so a
   background session's turn keeps streaming over the socket. Leaving a
   session mid-turn parks its transcript DOM (detached nodes keep every
   streamed token, which is persisted nowhere else until the turn ends) and
   buffers the events that arrive while it is away; returning reattaches the
   DOM and replays the buffer, so nothing is lost. */
const parkedTurns = new Map();  // session_key -> parked turn snapshot
const subBySession = {};        // session_key -> subscription_id
const subSession = {};          // subscription_id -> session_key
const PARK_EVENT_CAP = 4000;
/* The session the running turn belongs to. parkTurn must NOT key by `cur`:
   every rail click does `cur = s.id; openSession(s)`, so by the time the
   old turn is parked, `cur` already names the TARGET session — parking
   under it would file the old transcript in the wrong drawer and the
   parked-restore branch would immediately hand it back as the new
   session's content (the "switching still shows the old session" bug). */
let turnOwner = null;
/* The message a retry would re-send. Held here rather than read back off the
   last `.ask` bubble, which is markup and may belong to another session. */
let lastAsk = '';

function parkTurn() {
  if (!turnOwner || !busy) return;
  stopSayPaint();
  const s = sess(turnOwner);
  if (s) s.status = 'run';
  parkedTurns.set(turnOwner, {
    nodes: [...$('#stage').childNodes],
    turn: { st: live.st, steps: live.steps, say: live.say, open: new Map(live.open),
      sawEpisode: live.sawEpisode, startedAt: live.startedAt, answerAt: live.answerAt },
    busy, use, tl, raw, lastRun, q,
    /* The live clock's anchor. It is module state in base.html, and the away
       session's idle drawTurnLive zeroes it -- without carrying it here, a
       turn ten minutes in read "2s" after a round trip through another
       session. */
    liveT0,
    ws: { changes: WS.changes, cmds: WS.cmds, urls: WS.urls, file: WS.file, turn: WS.turn, unseen: WS.unseen },
    wsTab, wsPicked,
    events: [], overflow: false,
  });
}

function restoreTurn(pk) {
  turnOwner = cur;
  const stage = $('#stage');
  stage.innerHTML = '';
  pk.nodes.forEach((n) => stage.appendChild(n));
  Object.assign(live, pk.turn);
  busy = pk.busy; use = pk.use; tl = pk.tl; raw = pk.raw; lastRun = pk.lastRun; q = pk.q;
  /* Before drawMeter below: its drawTurnLive keeps a non-zero anchor, so the
     clock resumes from the turn's real start rather than from the switch. */
  liveT0 = pk.liveT0 || 0;
  Object.assign(WS, pk.ws);
  wsTab = pk.wsTab; wsPicked = pk.wsPicked;
  const s = sess(cur);
  if (s && s.status === 'run') s.status = null;
  pk.events.forEach((ev) => { try { onEvent(ev); } catch { /* one bad frame must not eat the rest */ } });
  paintSay();
  drawQ(); drawMeter(); goState(); drawList(); drawBanner();
  if (typeof drawWs === 'function' && wsOpen) drawWs();
  down();
}

/* ---- notifications ------------------------------------------------ */
rpc.notify.event = (params) => {
  if (live.subId && params.subscription_id === live.subId) { onEvent(params.event || {}); return; }
  const sid = subSession[params.subscription_id];
  const pk = sid && parkedTurns.get(sid);
  if (!pk) return;
  const ev = params.event || {};
  if (pk.events.length >= PARK_EVENT_CAP) pk.overflow = true;
  else pk.events.push(ev);
  if (ev.type === 'message.complete' || ev.type === 'error') {
    const s = sess(sid);
    // This branch only ever runs for a session the reader is NOT looking at
    // (a parked turn), so a clean finish is news: hold the row on 'done'
    // until they open it. openSession is what clears it. A cancel is a stop
    // somebody chose, not a failure -- no red dot for doing what was asked.
    const cancelled = ev.type === 'error' && (ev.payload || {}).reason === 'cancelled_by_client';
    if (s) { s.status = ev.type === 'error' && !cancelled ? 'err' : 'done'; touchSession(sid); }
  }
};

/* Approval wears the ask_user sheet (approveSheet), so a blocked turn always
   interrupts in the same place and shape. Closing it is a denial, never a
   silent drop -- the engine is waiting on an answer either way. */
rpc.notify['confirm.request'] = (p) => {
  const say = (answer) =>
    rpc.call('confirm.respond', { request_id: p.request_id, answer }).catch(() => {});
  approveSheet(p.prompt || '', () => say(true), () => say(false));
};

const SKIP_ANSWER = () => T('gui.clarify.skipped_msg');

rpc.notify['clarify.request'] = (p) => {
  /* The server names the conversation it is asking on behalf of, which is not
     always the one on screen: a question can arrive for a turn the reader
     stepped away from. Its own answer beats "wherever the reader happens to
     be", and the fallback is only for a frame that predates the field. */
  const owner = p.conversation_id || sheetSession();
  sheetDropClass('csheet', owner);
  const sheet = mk('div', 'csheet');
  sheet.setAttribute('role', 'dialog');
  sheet.setAttribute('aria-label', T('gui.clarify.aria'));

  /* No echo row here: the asking tool's own row (询问) renders the full
     question -> answer exchange in its detail once the tool returns, so a
     separate 已回答 line would say the same thing twice. The step is still
     marked hasQA so the exchange keeps its own step instead of merging
     into a silent work run. */
  const markQA = () => { if (live.st) live.st.hasQA = true; };
  const done = (text) => {
    rpc.call('clarify.respond', { request_id: p.request_id, answer: text }).catch(() => {});
    cleanup();
    markQA();
  };
  const skip = () => {
    rpc.call('clarify.respond', { request_id: p.request_id, answer: SKIP_ANSWER() }).catch(() => {});
    cleanup();
    markQA();
  };

  const head = mk('div', 'hd');
  const q = mk('div', 'q', p.question || '');
  const fold = mk('button', 'ic tipdn');
  fold.appendChild(ico('M6.5 10 12 15.5 17.5 10', 'cv'));
  const setFold = (v) => {
    sheet.dataset.fold = String(v);
    const lb = T(v ? 'gui.clarify.unfold' : 'gui.clarify.fold');
    fold.dataset.tip = lb;
    fold.setAttribute('aria-label', lb);
  };
  fold.onclick = () => setFold(sheet.dataset.fold !== 'true');
  setFold(false);
  q.onclick = () => { if (sheet.dataset.fold === 'true') setFold(false); };
  const x = mk('button', 'ic tipdn');
  x.appendChild(ico('M7 7l10 10M17 7 7 17'));
  x.dataset.tip = T('gui.clarify.skip');
  x.setAttribute('aria-label', T('gui.clarify.skip_aria'));
  x.onclick = skip;
  head.append(q, fold, x);
  sheet.appendChild(head);

  const body = mk('div', 'body');
  const choices = p.choices || [];
  choices.forEach((c, i) => {
    const b = mk('button', 'opt');
    b.append(mk('span', 'n', String(i + 1)), mk('span', null, c));
    b.onclick = () => done(c);
    body.appendChild(b);
  });

  const other = mk('div', 'other');
  other.appendChild(mk('span', 'n', String(choices.length + 1)));
  const inp = mk('input');
  inp.placeholder = T(choices.length ? 'gui.clarify.other_ph' : 'gui.clarify.ph');
  other.appendChild(inp);
  body.appendChild(other);
  sheet.appendChild(body);

  const foot = mk('div', 'foot');
  const skipBtn = mk('button', 'btn', T('gui.clarify.skip'));
  skipBtn.onclick = skip;
  const submit = mk('button', 'btn key', T('gui.clarify.submit'));
  submit.disabled = true;
  submit.onclick = () => { if (inp.value.trim()) done(inp.value.trim()); };
  foot.append(skipBtn, submit);
  sheet.appendChild(foot);

  inp.oninput = () => { submit.disabled = !inp.value.trim(); };
  inp.onkeydown = (e) => {
    e.stopPropagation();
    if (composing(e)) return;
    if (e.key === 'Enter' && inp.value.trim()) done(inp.value.trim());
  };

  // Number keys pick an option while focus is outside the input.
  const onKey = (e) => {
    // Parked with another conversation, this sheet is still on the document's
    // keydown; only the mounted one may be answered by number.
    if (!sheet.isConnected || document.activeElement === inp || composing(e)) return;
    const n = Number(e.key);
    if (n >= 1 && n <= choices.length) { e.preventDefault(); done(choices[n - 1]); }
    if (n === choices.length + 1) { e.preventDefault(); setFold(false); inp.focus(); }
  };
  document.addEventListener('keydown', onKey, true);

  /* The sheet is absolutely positioned, so growing it does not change the
     dock's own height and the dock's ResizeObserver never fires -- dockLift()
     has to be called by hand here. It reads the sheet out of the DOM, so
     folding or resizing only needs to re-measure. */
  const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(dockLift) : null;

  function cleanup() {
    document.removeEventListener('keydown', onKey, true);
    if (ro) ro.disconnect();
    sheetRemove(sheet);
  }

  sheetAdd(sheet, owner);
  if (ro) ro.observe(sheet);
  // Only when the question is the one on screen: focusing a field inside a
  // detached element steals the caret out of the composer the reader is using.
  if (sheet.isConnected) inp.focus();
};

/* ---- overrides ----------------------------------------------------- */
async function subscribe(sessionKey) {
  // One subscription per session per socket: re-opening a session reuses its
  // stream, so a parked turn's events and the visible ones never double up.
  if (subBySession[sessionKey]) { live.subId = subBySession[sessionKey]; return; }
  try {
    const r = await rpc.call('turn.subscribe', { session_key: sessionKey });
    live.subId = r.subscription_id;
    subBySession[sessionKey] = r.subscription_id;
    subSession[r.subscription_id] = sessionKey;
  } catch (e) {
    toast(`订阅失败：${e.message || e}`);
  }
}

rpc.onReconnect = async () => {
  await rpc.call('system.hello', { client_version: '0.1.0' }).catch(() => {});
  killStatus();
  // A fresh socket voids every server-side subscription, and the events a
  // parked turn missed while the socket was down are unrecoverable — drop
  // the parked copies and let re-opens rebuild from disk.
  parkedTurns.clear();
  for (const k of Object.keys(subBySession)) delete subBySession[k];
  for (const k of Object.keys(subSession)) delete subSession[k];
  live.subId = null;
  SESS.forEach((s) => { if (s.status === 'run') s.status = null; });
  /* Re-subscribing is not enough: events emitted while the socket was down
     are gone, and if the turn ENDED in that gap the client would keep its
     busy spinner forever. Reload the whole session from disk instead -- the
     transcript is persisted server-side, so a full re-open is lossless, and
     a turn that is genuinely still running keeps streaming into the fresh
     subscription that openSession sets up. */
  if (cur && !draft) {
    busy = false;
    const title = $('#title').textContent;
    await openSession({ id: cur, title });
    showStatus(T('gui.reconnected'));
    setTimeout(killStatus, 2500);
  } else if (cur) {
    subscribe(cur);
  }
};

/* A pending new task is a draft, not a session: nothing is written to disk
   until the first message, so the rail does not fill with empty sessions. */
let draft = false;

function resetView() {
  stop_(); busy = false; q = []; use = null; kids = []; tl = []; raw = []; lastRun = null;
  resetTurnState();
  wsReset();
  setWs(false);
  $('#stage').innerHTML = '';
  $('#flash').textContent = '';
  drawQ(); drawMeter(); goState(); drawBanner();
}

function startDraft() {
  parkTurn();
  // The old session's stream must stop routing to the visible stage the
  // moment we leave it -- its events belong to the parked buffer now.
  live.subId = null;
  parkDraft(); loadDraft('new');
  resetView();
  draft = true; cur = null;
  sheetsSync();
  $('#title').textContent = T('gui.new_task');
  pitch(); drawList(); ta.focus();
}

openSession = async function (s) {
  parkTurn();
  // Same as startDraft: while session.resume is in flight the old session
  // may still be streaming, and a stale live.subId would paint its events
  // into the newly opened stage. Route them to the parked buffer instead.
  live.subId = null;
  /* Twice, deliberately. The rail sets `cur` before calling this, so syncing
     here takes the last conversation's sheets down at once rather than leaving
     them over the composer for as long as session.resume takes; the sync below
     is the one that runs when `cur` was not settled yet (the reconnect path). */
  sheetsSync();
  parkDraft(); loadDraft(s.id);
  draft = false;
  // Opening it IS reading it. ``s`` can be a bare {id, title} from the
  // reconnect path, so clear the flag on the row in SESS, not on the arg.
  const row = sess(s.id);
  if (row && row.status === 'done') row.status = null;
  markNewCurrent();
  stop_(); busy = false; q = []; use = null; kids = []; tl = []; raw = []; lastRun = null;
  resetTurnState();
  wsReset();
  setWs(false);
  $('#title').textContent = plainTitle(s.title);
  $('#stage').innerHTML = '';
  $('#flash').textContent = '';
  drawQ(); drawMeter(); goState(); drawBanner();
  // A parked turn restores in place of a disk reload: the transcript on disk
  // does not have the still-streaming content, the parked DOM does.
  const pk = parkedTurns.get(s.id);
  if (pk) {
    parkedTurns.delete(s.id);
    cur = s.id;
    // With `cur` settled: hand this conversation back the sheets it was raised
    // with, and take away the ones the last conversation was still holding.
    sheetsSync();
    drawDag();
    live.subId = subBySession[s.id] || null;
    restoreTurn(pk);
    if (!live.subId) await subscribe(s.id);
    return;
  }
  try {
    const r = await rpc.call('session.resume', { session_id: s.id, session_key: s.id });
    if (r.session_id && r.session_id !== s.id) { s.id = r.session_id; if (cur !== s.id) cur = s.id; }
    cur = s.id;
    sheetsSync();
    drawDag();
    /* resume hands back the canonical id, so the row rendered from the listed
       id no longer matches `cur` -- without this redraw the rail shows nothing
       selected until the reader clicks a session themselves. */
    drawList();
    const u = (r.info && r.info.usage) || {};
    setCtx(u.context_used, u.context_max, u.context_estimated);
    if (r.messages && r.messages.length) {
      renderHistory(r.messages);
      /* Rebuild what the panel can from the replay. Stored messages keep the
         tool name and its result but not the call arguments, so this recovers
         the changed paths and counts the rest -- see wsOnHistory. */
      wsOnHistory(r.messages);
    } else pitch();
    await subscribe(s.id);
  } catch (e) {
    pitch();
    toast(`打开会话失败：${e.message || e}`);
  }
};

/* The attachment note baked into the message is the record of what was handed
   over -- the reader's own bubble renders its chips from it, and it is what
   survives into session history. So the paths ride to the model as a typed
   `media` field as well, recovered from that same note rather than threaded
   separately: a queued message is a plain string by the time it is drained
   (drawQ lets the reader edit it), and the draft path sends from a second
   place, so deriving here covers all three with one rule. */
const mediaOf = (text) => {
  const paths = splitAtts(String(text)).atts;
  return paths.length ? { media: paths } : {};
};

send = function (text) {
  const pending = atts.filter((a) => a.uploading).length;
  if (pending) { errLine(T('gui.att.pending'), T('gui.att.pending_body', { n: pending })); return; }
  if (atts.length) {
    const list = atts.map((a) => `- ${a.path}`).join('\n');
    /* Handing over a file with nothing typed is a message in itself; the note
       leads on its own rather than trailing a blank line. */
    const note = `${T('gui.att.note')}\n${list}`;
    text = text.trim() ? `${text}\n\n${note}` : `\n\n${note}`;
    atts.length = 0;
    drawAtts();
  }
  if (busy) { q.push(text); drawQ(); return; }
  const p = $('#stage').querySelector('.pitch'); if (p) p.remove();
  /* What a retry re-sends. Recorded after the attachment note is folded in, so
     the second attempt carries the same message as the first. */
  lastAsk = text;
  ask(text);
  busy = true; use = null; tl = []; raw = [];
  resetTurnState();
  drawMeter(); goState(); drawList();
  const failed = (e) => {
    killStatus();
    busy = false;
    errLine(T('gui.err.send'), e.message === 'not connected' ? T('gui.err.disconnected') : (e.message || String(e)),
      () => send(text));
    goState(); drawMeter();
  };
  if (!draft) {
    turnOwner = cur;
    touchSession(cur);
    titleFromFirstMessage(text);
    rpc.call('turn.send', { session_key: cur, content: text, ...mediaOf(text) }).catch(failed);
    return;
  }
  // The draft becomes a real session here, on its first message.
  (async () => {
    const r = await rpc.call('session.create', {});
    const s = { id: r.session_id, title: T('gui.new_task'), last: T('gui.sess.not_started'),
      when: T('gui.sess.just_now'), g: '今天', at: Math.floor(Date.now() / 1000), run: null, live: true };
    SESS.unshift(s); cur = s.id; draft = false;
    turnOwner = cur;
    // The composer was owned by 'new' until this point; keep later keystrokes
    // filed under the session that just came into being.
    if (draftOwner === 'new') draftOwner = cur;
    titleFromFirstMessage(text);
    drawList();
    await subscribe(s.id);
    await rpc.call('turn.send', { session_key: cur, content: text, ...mediaOf(text) });
  })().catch(failed);
};

/* A stop is not a failure: everything already streamed stays on the stage, and
   the only new line is the note that a person asked for the stop. Shared by
   the button and by the cancelled event another client can cause. */
function softStop() {
  killStatus();
  stopSayPaint();
  if (live.st) live.st.seal();
  collapseTurn(turnDur());
  stop_(); busy = false;
  const r = mk('div', 'act in');
  r.append(mk('span', 'g'), mk('span', 'arg', T('gui.halted')));
  $('#stage').appendChild(r);
  resetTurnState();
  drawMeter(); goState(); drawList(); down();
}

/* Queued messages were waiting for the engine, and a stop is the engine coming
   free -- so the queue drains into it, same as after a finished turn. */
function drainQueue() {
  if (busy || !q.length) return;
  const nx = q.shift();
  drawQ();
  send(nx);
}

/* True between our own turn.cancel and its response. The cancelled EVENT is
   emitted mid-cancel, before the server has fully unwound the turn -- a drain
   fired off the event raced the dying turn into a -32003 refusal (observed).
   Our own cancel drains off the response instead, which the server sends only
   after the turn is provably gone; the event-side drain stays for a cancel
   made by another client on the same session. */
let cancelInFlight = false;

halt = function () {
  const owner = cur;
  cancelInFlight = true;
  rpc.call('turn.cancel', { session_key: cur })
    .then(() => { cancelInFlight = false; if (cur === owner) drainQueue(); },
      () => { cancelInFlight = false; });
  softStop();
};

removeSession = function (s) {
  confirmAsk(T('gui.sess.delete_title'), T('gui.sess.delete_body', { title: s.title }), T('gui.sess.delete'), async () => {
    try {
      await rpc.call('session.delete', { session_id: s.id });
      dropDraft(s.id);
      parkedTurns.delete(s.id);
      // A deleted conversation's pending question has nothing left to answer,
      // and its graph nothing left to describe.
      sheetsForget(s.id);
      DAGS.delete(s.id);
      SESS = SESS.filter((x) => x.id !== s.id);
      if (cur === s.id && SESS[0]) { cur = SESS[0].id; openSession(SESS[0]); }
      drawList();
      toast(T('gui.sess.deleted_x', { title: s.title }));
    } catch (e) { toast(`删除失败：${e.message || e}`); }
  });
};

$('#newBtn').onclick = () => { showPage(null); startDraft(); };

/* Persist a manual rename made through the title editor. */
{
  const origRename = renameTitle;
  renameTitle = function () {
    const before = sess(cur) ? sess(cur).title : null;
    origRename();
    const inp = $('#title').querySelector('input');
    if (!inp) return;
    inp.addEventListener('blur', () => {
      const s = sess(cur);
      if (s && s.title !== before) rpc.call('session.title', { session_id: s.id, title: s.title }).catch(() => {});
    });
  };
}

/* ---- module pages: real data ---------------------------------------
   Real lists are loaded on page open and written into the demo's data
   arrays IN PLACE, so every existing renderer keeps working. Mutations
   the demo makes locally (t.on = ..., c.state = ...) are intercepted
   with property setters that persist through settings.set. */

const fmt2 = (n) => String(n).padStart(2, '0');
function fmtStamp(ms) {
  if (!ms) return '—';
  const d = new Date(ms), now = new Date();
  const day0 = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const hm = `${fmt2(d.getHours())}:${fmt2(d.getMinutes())}`;
  if (d.getTime() >= day0 && d.getTime() < day0 + 86400000) return T('gui.time.today', { hm });
  if (d.getTime() >= day0 - 86400000 && d.getTime() < day0) return T('gui.time.yesterday', { hm });
  if (d.getTime() >= day0 + 86400000 && d.getTime() < day0 + 2 * 86400000) return T('gui.time.tomorrow', { hm });
  return T('gui.time.md_hm', { m: d.getMonth() + 1, d: d.getDate(), hm });
}
function fmtEvery(ms) {
  if (ms % 3600000 === 0) return T('gui.dur.h', { n: ms / 3600000 });
  if (ms % 60000 === 0) return T('gui.dur.m', { n: ms / 60000 });
  return T('gui.dur.s', { n: Math.round(ms / 1000) });
}

/* -- extensions ----------------------------------------------------- */
// Display names come from the catalogue; an unknown tool keeps its raw id.
const toolLabel = (n) => T('tool.' + n, null, n);
const TOOL_GROUP_OF = (n) => /file|dir|grep|glob|sheet|pdf/.test(n) ? 'file'
  : /web|fetch|search|research|browser/.test(n) ? 'net'
  : /ask|message|clarify/.test(n) ? 'ask' : 'run';
const TOOL_DANGER = new Set(['write_file', 'edit_file', 'exec']);

let disabledToolsLive = [];
let pluginsDisabledLive = [];

function persistDisabledTools() {
  rpc.call('settings.set', { key: 'tools.disabledTools', value: disabledToolsLive })
    .then(() => toast('已保存 · 重启引擎后生效'))
    .catch((e) => toast(`保存失败：${e.message || e}`));
}

function mkToolRow(t) {
  const id = t.name;
  const o = { id, name: toolLabel(id), group: TOOL_GROUP_OF(id),
    reach: TOOL_GROUP_OF(id) === 'net' ? 'net' : 'local',
    danger: TOOL_DANGER.has(id), one: t.description || '',
    /* Present but withheld for want of a key. The row exists so the reader
       learns the tool exists and what it wants -- before this, a key-gated
       tool was simply absent, which reads as removed. */
    needs: t.needs || null };
  if (t.needs) {
    o.on = false;
    return o;
  }
  Object.defineProperty(o, 'on', {
    get: () => !disabledToolsLive.includes(id),
    set: (v) => {
      disabledToolsLive = v ? disabledToolsLive.filter((x) => x !== id) : [...new Set([...disabledToolsLive, id])];
      persistDisabledTools();
    },
  });
  return o;
}

function mkSkillRow(s) {
  const o = { id: s.name, name: s.name, reach: 'local', glyph: (s.name[0] || 'S').toUpperCase(),
    ver: '—', src: s.source, one: s.description || '', cat: s.source, hub: !!s.hub, hubId: s.hub_id || '' };
  Object.defineProperty(o, 'state', {
    get: () => 'on',
    set: () => toast(T('gui.hub.auto_use')),
  });
  return o;
}

function mkPluginRow(p) {
  const id = p.id;
  const o = { id, name: p.display_name || id, reach: 'local', glyph: '◧', ver: p.version,
    src: T(p.bundled ? 'gui.ext.builtin_plugin' : 'gui.ext.plugin'), one: '',
    cat: T('gui.ext.plugin'), tools: [], perms: [] };
  Object.defineProperty(o, 'state', {
    get: () => (pluginsDisabledLive.includes(id) ? 'off' : p.enabled ? 'on' : 'off'),
    set: (v) => {
      pluginsDisabledLive = v === 'on'
        ? pluginsDisabledLive.filter((x) => x !== id)
        : [...new Set([...pluginsDisabledLive, id])];
      rpc.call('settings.set', { key: 'plugins.disabled', value: pluginsDisabledLive })
        .then(() => toast('已保存 · 重启引擎后生效'))
        .catch((e) => toast(`保存失败：${e.message || e}`));
    },
  });
  return o;
}

/* Legacy-state mapping keeps the rail badge (needsAttn counts 'need'/'fail')
   working; the plugin page itself renders from the raw snapshot in o.m. */
const MCP_LEGACY = { connected: 'on', connecting: 'on', auth_required: 'need', error: 'fail', disconnected: 'off' };

function mkMcpRow(m) {
  const o = { id: `mcp:${m.name}`, name: m.name, reach: m.transport === 'stdio' ? 'local' : 'net',
    glyph: '◇', ver: '—', src: `${m.transport} · MCP`, cat: 'MCP', m,
    one: m.connected ? T('gui.ext.mcp_tools', { n: m.tool_count }) : '', tools: [], perms: [] };
  Object.defineProperty(o, 'state', {
    get: () => (!m.enabled ? 'off' : MCP_LEGACY[m.state] || 'off'),
    set: (v) => pmToggle(m.name, v === 'on'),
  });
  return o;
}

async function loadExt() {
  const [ext, cfg] = await Promise.all([rpc.call('ext.list', {}), rpc.call('settings.get', {})]);
  const raw = cfg.settings || {};
  disabledToolsLive = (raw.tools && raw.tools.disabledTools) || [];
  pluginsDisabledLive = (raw.plugins && raw.plugins.disabled) || [];
  TOOLS.length = 0;
  ext.tools.filter((t) => !t.mcp_server).forEach((t) => TOOLS.push(mkToolRow(t)));
  SKILLS.length = 0;
  ext.skills.forEach((s) => SKILLS.push(mkSkillRow(s)));
  PLUGINS.length = 0;
  ext.plugins.forEach((p) => PLUGINS.push(mkPluginRow(p)));
  ext.mcp.forEach((m) => PLUGINS.push(mkMcpRow(m)));
}

let extLoaded = false;
openCaps = async function (tab) {
  /* Page first, data second: the old await-first shape left the previous
     tab's cards on screen until the RPC returned -- the "stale flash". */
  extSet(tab);
  showPage('capsPage');
  if (extLoaded) { drawCaps(); drawCapsBadge(); }
  else {
    const box = $('#capsBody'); box.innerHTML = '';
    const g = mk('div', 'hubgrid');
    for (let i = 0; i < 6; i++) g.appendChild(hubSkeleton());
    box.appendChild(g);
  }
  try { await loadExt(); extLoaded = true; } catch (e) { toast(`加载失败：${e.message || e}`); }
  drawCaps(); drawCapsBadge();
};

/* -- schedules ------------------------------------------------------- */
function cronToRow(j) {
  const when = j.kind === 'cron' ? cronExprHuman(j.expr)
    : j.kind === 'every' ? T('gui.cron.every', { every: fmtEvery(j.every_ms) })
    : T('gui.cron.once', { at: fmtStamp(j.at_ms) });
  const runs = j.last_run_at_ms
    ? [{ at: fmtStamp(j.last_run_at_ms), ok: j.last_status !== 'error', ms: 0,
        note: j.last_error || (j.last_status === 'ok' ? T('gui.cron.ok') : j.last_status || ''), sid: null }]
    : [];
  /* `at` is the server's third kind, and mapping it to 'day' is what let the
     editor rewrite a one-shot into a daily job. It has its own frequency now,
     and carries its instant in the shape the datetime input reads. */
  /* The offset of the instant being converted, not of today: `new Date()` with
     no argument is now, so a job on the other side of a DST boundary displayed
     -- and re-saved -- an hour off. Same shape as the bug above it, one layer
     down: a value re-derived through a conversion that does not know which
     instant it is converting. */
  const local = j.kind === 'at' && j.at_ms
    ? new Date(j.at_ms - new Date(j.at_ms).getTimezoneOffset() * 60000).toISOString().slice(0, 16) : '';
  return { id: j.id, name: j.name, on: j.enabled, what: j.message,
    freq: j.kind === 'cron' ? 'cron' : j.kind === 'every' ? 'hour' : 'once',
    at: j.kind === 'cron' ? j.expr : '', at_local: local,
    when, next: j.enabled ? fmtStamp(j.next_run_at_ms) : T('gui.cron.paused'),
    deliver: 'app', runs, kind: j.kind, every_ms: j.every_ms, at_ms: j.at_ms, tzv: j.tz };
}

async function loadCrons() {
  const r = await rpc.call('cron.list', {});
  CRONS.length = 0;
  r.jobs.map(cronToRow).forEach((x) => CRONS.push(x));
}

let cronLoaded = false;
openCron = async function () {
  cronView = null;
  showPage('cronPage');
  if (cronLoaded) { drawCron(); drawCronBdg(); }
  else $('#cronBody').innerHTML = '';
  try { await loadCrons(); cronLoaded = true; } catch (e) { toast(`加载失败：${e.message || e}`); }
  drawCron(); drawCronBdg();
};
const reloadCronPage = () => loadCrons().then(() => { drawCron(); drawCronBdg(); }).catch(() => {});

runNow = function (j) {
  rpc.call('cron.run_now', { id: j.id })
    .then(() => { toast(T('gui.cron.triggered_x', { name: j.name })); reloadCronPage(); })
    .catch((e) => toast(`触发失败：${e.message || e}`));
};

openRun = function (j) {
  closeCron();
  const s = { id: `cron:${j.id}`, title: j.name, last: '', when: '', g: '今天', run: null, live: true, from: 'cron' };
  if (!sess(s.id)) SESS.unshift(s);
  cur = s.id; drawList(); openSession(s);
};

cronToggle = (j) => {
  rpc.call('cron.set_enabled', { id: j.id, enabled: !j.on })
    .then(() => { toast(!j.on ? `已恢复「${j.name}」` : `已暂停「${j.name}」`); reloadCronPage(); })
    .catch((e) => toast(`操作失败：${e.message || e}`));
};
cronDelete = (j) => {
  rpc.call('cron.delete', { id: j.id })
    .then(() => { cronView = null; toast(T('gui.cron.deleted_x', { name: j.name })); reloadCronPage(); })
    .catch((err) => toast(`删除失败：${err.message || err}`));
};
cronRunsLoad = (j) => rpc.call('cron.runs', { id: j.id })
  .then((r) => (r.runs || []).map((x) => ({
    at: x.at_ms ? fmtStamp(x.at_ms) : '—', ok: !!x.ok, note: x.preview || '',
  })));
cronPersist = (draft, done) => {
  let payload;
  try { payload = jobToSave(draft); } catch (e) { jobRefuse(draft, e); return; }
  rpc.call('cron.save', payload)
    .then((r) => loadCrons().then(() => {
      if (done) done(CRONS.find((x) => x.id === r.job.id) || cronToRow(r.job));
      drawCronBdg();
    }))
    .catch((e) => toast(`保存失败：${(e.data && e.data.detail) || e.message || e}`));
};

cronRow = function (j) {
  const last = j.runs[0];
  const r = mk('div', 'cronjob' + (j.on && last && !last.ok ? ' bad' : ''));
  r.style.cursor = 'pointer';
  r.onclick = (e) => { if (e.target.closest('button')) return; openCronDetail(j); };
  const nm = mk('div', 'nm');
  nm.appendChild(mk('span', 'dot' + (!j.on ? '' : last && !last.ok ? ' err' : ' run')));
  nm.appendChild(mk('span', null, j.name));
  nm.appendChild(mk('span', 'when', j.when));
  r.appendChild(nm);
  const meta = mk('div', 'mo');
  meta.textContent = j.on ? T('gui.cron.next_only', { next: j.next }) : T('gui.cron.paused');
  r.appendChild(meta);
  const foot = mk('div', 'foot');
  if (last) {
    const chip = mk('button', 'mini ghost');
    chip.textContent = T('gui.cron.last_run_short',
      { at: last.at, state: T(last.ok ? 'gui.cron.ok' : 'gui.cron.failed') });
    if (!last.ok) chip.classList.add('danger');
    chip.onclick = () => openRun(j, last);
    foot.appendChild(chip);
    foot.appendChild(mk('span', 'mono', last.note)).style.cssText =
      'font-size:11px;color:var(--faint);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:1';
  } else {
    foot.appendChild(mk('span', 'mono', T('gui.cron.never'))).style.cssText = 'font-size:11px;color:var(--faint)';
  }
  r.appendChild(foot);
  const ctl = mk('div', 'ctl');
  const s = mk('button', 'swi');
  s.setAttribute('role', 'switch');
  s.setAttribute('aria-checked', String(j.on));
  s.setAttribute('aria-label', T('gui.caps.toggle_aria', { name: j.name }));
  s.onclick = () => cronToggle(j);
  const more = mk('button', 'mini ghost', '⋯');
  more.setAttribute('aria-label', T('gui.cron.menu_aria', { name: j.name }));
  more.onclick = (e) => {
    const b = e.currentTarget.getBoundingClientRect();
    menuAt(b.right - 150, b.bottom + 6, [
      { label: T('gui.cron.run_now'), fn: () => runNow(j) },
      { label: T('gui.cron.history'), fn: () => openCronDetail(j) },
      { label: T('gui.cron.open_session'), fn: () => openRun(j) },
      '-',
      { label: T('gui.cron.delete'), bad: true, fn: () => confirmAsk(T('gui.cron.delete_title'),
          T('gui.cron.delete_body', { name: j.name }), T('gui.cron.delete'), () => cronDelete(j)) },
    ]);
  };
  ctl.append(s, more);
  r.appendChild(ctl);
  return r;
};

/* Which message a refusal shows, keyed by what jobToSave could not read. The
   sheet is redrawn so the note lands under the control the reader has to fix;
   a toast would not, because live mode sends those to the console. */
const JOB_BAD = { 'no instant': 'gui.job.need_instant', 'bad weekday': 'gui.job.bad_weekday' };
function jobRefuse(draft, err, redraw) {
  draft.bad = JOB_BAD[err && err.message] || 'gui.job.bad_time';
  if (redraw) redraw();
  else if (typeof reloadCronPage === 'function') reloadCronPage();
}

function jobToSave(j) {
  const base = { name: j.name.trim(), message: j.what.trim(), deliver: true };
  if (j.id && !j.fresh) base.id = j.id;
  if (j.freq === 'hour') {
    return { ...base, kind: 'every', every_seconds: j.every_ms ? Math.round(j.every_ms / 1000) : 3600 };
  }
  if (j.freq === 'cron') return { ...base, kind: 'cron', expr: j.at.trim() };
  if (j.freq === 'once') {
    if (!j.at_local) throw new Error('no instant');
    return { ...base, kind: 'at', at_iso: j.at_local };
  }
  /* A time the reader typed, and nothing else read back out of prose: the
     weekday is a number the control produced. */
  const hm = j.at.match(/^\s*(\d{1,2}):(\d{2})\s*$/);
  if (!hm) throw new Error('bad time');
  const [h, m] = [Number(hm[1]), Number(hm[2])];
  if (h > 23 || m > 59) throw new Error('bad time');
  if (j.freq === 'week') {
    const wd = Number(j.wd);
    if (!Number.isInteger(wd) || wd < 0 || wd > 6) throw new Error('bad weekday');
    return { ...base, kind: 'cron', expr: `${m} ${h} * * ${wd}` };
  }
  return { ...base, kind: 'cron', expr: `${m} ${h} * * *` };
}

$('#jobYes').onclick = () => {
  const j = jobDraft;
  if (!j || !j.name.trim() || !j.what.trim()) { j.blank = true; openJobSheet(j); return; }
  let payload;
  try { payload = jobToSave(j); } catch (e) { jobRefuse(j, e, () => openJobSheet(j)); return; }
  rpc.call('cron.save', payload)
    .then((r) => {
      $('#jobVeil').dataset.open = 'false'; jobDraft = null;
      toast(T('gui.job.saved_x', { name: r.job.name }),
        { label: T('gui.job.run_once'), fn: () => runNow({ id: r.job.id, name: r.job.name }) });
      reloadCronPage();
    })
    .catch((e) => toast(`保存失败：${(e.data && e.data.detail) || e.message || e}`));
};

/* -- connections (channels) ------------------------------------------ */
async function loadChannels() {
  const r = await rpc.call('channels.status', {});
  const byName = Object.fromEntries(r.channels.map((c) => [c.name, c]));
  CHANNELS.forEach((c) => {
    const s = byName[c.id];
    if (!s) return;
    /* No prose state line: the LED and the switch say on/off, and a missing
       credential says 未配置 through c.missing. `who` is reserved for a real
       identity (the account the channel signs in as), which no backend
       supplies yet -- so live rows keep their sub line empty. */
    c.who = '';
    /* The schema-declared field list rides the status row; the page's
       configure form is drawn from it, so the form and the config can't drift. */
    c.fields = s.fields || [];
    c.missing = s.missing || [];
    c._on = s.enabled;
    /* Three separate facts, kept separate. `on` is what the config asks for;
       `running` is whether the adapter came up; `connected` is whether the
       account is paired, which only the QR channels report. Absent means the
       gateway could not be asked -- not "no". */
    c.running = s.running;
    c.connected = s.connected;
    c.qrLogin = !!s.qr_login;
    if (!c._live) {
      c._live = true;
      Object.defineProperty(c, 'on', {
        get: () => c._on,
        set: (v) => {
          c._on = v;
          rpc.call('settings.set', { key: `channels.${c.id}.enabled`, value: v })
            .then(() => toast(T('gui.conn.toggled', { name: chanName(c), state: T(v ? 'gui.conn.enabled' : 'gui.conn.disabled') })))
            .catch((e) => { c._on = !v; toast(`保存失败：${e.message || e}`); drawConn(); });
        },
      });
    }
  });
  gatewayRunningLive = r.gateway_running;
}
let gatewayRunningLive = false;

/* Credentials and the switch travel together, and the server applies them in
   that order, so a channel is never on without the values it was turned on
   for. `enable` used to be a local `c.on = true` that `loadChannels()` then
   overwrote from the server -- which made connect a no-op that looked like it
   worked, and disconnect a no-op with nothing to show for it at all. */
connApply = async (c, patch, enable) => {
  try {
    const fields = patch && Object.keys(patch).length ? patch : {};
    await rpc.call('channels.configure', { name: c.id, fields, enabled: !!enable });
    if (Object.keys(fields).length) toast(T('gui.conn.saved_x', { name: chanName(c) }));
    await loadChannels();
  } catch (e) {
    toast(`保存失败：${(e.data && e.data.detail) || e.message || e}`);
  }
  drawConn();
};

let chanLoaded = false;
openConn = async function () {
  showPage('connPage');
  if (chanLoaded) drawConn();
  else $('#connBody').innerHTML = '';
  try { await loadChannels(); chanLoaded = true; } catch (e) { toast(`加载失败：${e.message || e}`); }
  drawConn();
  if (!gatewayRunningLive && CHANNELS.some((c) => c.on)) {
    toast('已启用的入口还没在收消息 — 重新打开 Raven App 即可生效');
  }
};

/* -- settings --------------------------------------------------------- */
/* No standing notice banners: a config gap belongs in the settings page, not
   as a strip above every conversation. */
drawBanner = function () {
  $('#bannerHost').innerHTML = '';
};

/* The config settings.get returned, kept so the pages can display real values.
   Keys arrive camelCased (agents.defaults.reasoningEffort), one level per dot. */
let RAW = {};
V = (path, fallback) => {
  const v = String(path).split('.').reduce((o, k) => (o == null ? o : o[k]), RAW);
  return v == null || v === '' ? fallback : v;
};
/* Like V but null is a value: memory.backend stores null for "plugin memory
   off", and painting the schema default over it would misreport the choice. */
Vnull = (path, fallback) => {
  let node = RAW;
  for (const k of String(path).split('.')) {
    if (node == null || typeof node !== 'object' || !(k in node)) return fallback;
    node = node[k];
  }
  return node;
};

/* The write half of the settings dialog: one whitelisted dotted key per
   control. Reload-then-redraw keeps every V() read honest after a write. */
settingsWrite = (key, value, el) => rpc.call('settings.set', { key, value })
  .then(() => loadSettings())
  .then(() => { if (setIsOpen()) drawSettings(); toast(T('gui.set.saved')); })
  .catch((e) => {
    toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e }));
    if (setIsOpen()) drawSettings();
  });

/* EverOS model roles: read on settings open, written per section. A null
   fields object means "clear the section" (optional roles only). */
everosWrite = (section, fields, el) => {
  const p = fields ? { section, fields } : { section, clear: true };
  return rpc.call('settings.everosSet', p)
    .then(() => loadEveros())
    .then(() => {
      memEdit = null;
      if (setIsOpen()) drawSettings();
      toast(T('gui.set.mem.saved'));
    })
    .catch((e) => {
      toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e }));
      if (setIsOpen()) drawSettings();
    });
};

let usageBusy = false;
let usageAt = 0;
usageLoad = () => {
  /* The page asks on every redraw and a fresh reply causes one, so without the
     floor the two would spin. It also bounds how often the tab polls itself. */
  if (usageBusy || Date.now() - usageAt < 3000) return;
  usageBusy = true;
  rpc.call('settings.usage', {})
    .then((r) => { USAGE = r; })
    /* A failed refresh keeps the numbers it already has: zeroing them would
       report "no usage" for what is really a dropped call. */
    .catch(() => {
      if (!USAGE) USAGE = { days: 30, llm: { total: { calls: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0 }, models: [] }, tools: { total: 0, counts: [] } };
    })
    .then(() => {
      usageBusy = false; usageAt = Date.now();
      if (setIsOpen() && sTab === 'usage') drawSettings();
    });
};
/* Counters that only move when the dialog is reopened read as broken. The page
   keeps itself current for as long as it is the one on screen. */
setInterval(() => { if (setIsOpen() && sTab === 'usage') usageLoad(); }, 15000);

async function loadEveros() {
  try {
    EVEROS = await rpc.call('settings.everos', {});
  } catch { /* section rows render as unset; writes still surface their error */ }
}

/* config.set's whitelisted keys are readable only through config.get;
   settings.get reports the effective agent defaults instead, which are a
   different number (agents.defaults.temperature 0.1 vs agent.temperature 1).
   These three are display-only -- see the note on `wire` below. */
const HOT_KEYS = ['agent.temperature', 'agent.thinking_budget', 'tui.show_token_usage'];

async function loadSettings() {
  const r = await rpc.call('settings.get', {});
  const raw = r.settings || {};
  RAW = raw;
  CONFIG_PATH = r.config_path || CONFIG_PATH;
  drawBanner();
  const defaults = (raw.agents && raw.agents.defaults) || {};
  if (!CFG._live) {
    CFG._live = true;
    let tempVal = CFG.temp, budgetVal = CFG.budget;
    /* Read-only on purpose. config.set takes both keys and then writes a
       top-level `agent` section that the config schema forbids, so the next
       load of config.json fails and every config-reading RPC goes down with
       it. Until the writer maps them onto agents.defaults, the settings page
       shows the value and refuses the write. */
    const wire = (prop, get, set) => Object.defineProperty(CFG, prop, { get, set });
    wire('temp', () => tempVal, (v) => { tempVal = v; });
    wire('budget', () => budgetVal, (v) => { budgetVal = v; });
    CFG._setRaw = (t, b) => { if (typeof t === 'number') tempVal = t; if (typeof b === 'number') budgetVal = b; };
  }
  if (defaults.workspace) CFG.cwd = defaults.workspace;
  CFG.endpoint = r.config_path;
  try {
    const w = await rpc.call('config.get', { keys: HOT_KEYS });
    const c = (w && w.config) || {};
    CFG._setRaw(c['agent.temperature'], c['agent.thinking_budget']);
    if (typeof c['tui.show_token_usage'] === 'boolean') SHOW_TOKENS = c['tui.show_token_usage'];
  } catch { /* leave the sliders where they are rather than show a guess */ }
  if (defaults.model) { model = defaults.model; setModelLabel(); }
  try { await loadProviders(); } catch { /* model options unavailable — keep demo providers */ }
}

let curProvider = '';

async function loadProviders() {
  const mo = await rpc.call('model.options', {});
  PROVIDERS.length = 0;
  (mo.providers || []).forEach((p) => PROVIDERS.push({
    id: p.slug, name: p.name, models: p.models || [], on: p.authenticated,
    kind: p.auth_type || 'api_key', needsBase: !!p.needs_api_base,
    env: p.key_env || '', warn: p.warning || '',
    key: p.authenticated ? '已配置' : '',
  }));
  curProvider = mo.provider || '';
  if (mo.model) { model = mo.model; setModelLabel(); }
}

openSettings = async function () {
  /* loadExt too: the agent's tool inventory is a settings page now, and a
     panel drawn from the demo list would flip switches that do not exist. */
  try { await Promise.all([loadSettings(), loadExt(), loadEveros()]); } catch (e) { toast(`加载失败：${e.message || e}`); }
  drawSettings(); openSet();
};

/* -- language ---------------------------------------------------------
   One key, both front ends: config.language also drives the TUI (which
   polls it) and the language the agent replies in. */
applyLang = async function (next, { persist } = {}) {
  if (next === LANG) return;
  const prev = LANG;
  LANG = next;
  applyI18n();
  redrawAll();
  if (!persist) return;
  try {
    await rpc.call('config.set', { key: 'language', value: next });
  } catch (e) {
    LANG = prev; applyI18n(); redrawAll();
    toast(`切换语言失败：${(e.data && e.data.detail) || e.message || e}`);
  }
};

/* Everything the catalogue reaches that is drawn rather than written in
   the markup. Cheap enough to run wholesale on a language flip. */
function redrawAll() {
  drawList();
  drawFoot();
  drawCapsBadge();
  setModelLabel();
  drawPerm();
  drawCtx();
  // Every module page, not just the open one: a hidden page keeps its old
  // DOM, so it would still be in the previous language when reopened.
  drawSettings();
  try { drawCaps(); } catch { /* extensions not loaded yet */ }
  try { drawConn(); } catch { /* channels not loaded yet */ }
  try { drawCron(); } catch { /* schedules not loaded yet */ }
  const p = $('#stage').querySelector('.pitch');
  if (p) { p.remove(); pitch(); }
  /* The transcript writes its words into the DOM as it renders -- fold headers,
     work phrases, answer footers -- so a flip has to rebuild it, not just
     re-run the catalogue over the markup. Skipped while a turn is streaming:
     re-opening the session mid-turn would cut the stream off. */
  if (!draft && cur && !busy) openSession(sess(cur));
}

async function loadLang() {
  try {
    const r = await rpc.call('config.get', { keys: ['language'] });
    const v = r && r.config && r.config.language;
    if (v === 'en' || v === 'zh') { LANG = v; applyI18n(); redrawAll(); }
  } catch { /* stay on the built-in default */ }
}

/* A model id is provider-qualified (openrouter/anthropic/claude-opus-4.6);
   the chip only has room for the part that identifies the model. */
const shortModel = (m) => String(m || '').split('/').pop();
const setModelLabel = () => { $('#modelName').textContent = shortModel(model); $('#modelChip').title = model; };

/* Anchored to the composer chip by default; the settings panel passes its own
   button and a callback so both places pick a model the same way. */
function openModelPicker(anchor, after) {
  document.querySelectorAll('.mpick').forEach((n) => n.remove());
  const host = anchor || $('#modelChip');
  const authed = PROVIDERS.filter((p) => p.on && p.models.length);
  if (!authed.length) { toast(T('gui.picker.no_account')); return; }

  const pick = mk('div', 'mpick');
  pick.setAttribute('role', 'dialog');
  let provIdx = Math.max(0, authed.findIndex((p) => p.models.includes(model)));
  let query = '';

  const find = mk('div', 'find');
  find.appendChild(mk('span', null, '⌕')).style.cssText = 'color:var(--faint)';
  const q = mk('input');
  q.placeholder = T('gui.picker.search_ph');
  find.appendChild(q);
  pick.appendChild(find);

  const cols = mk('div', 'cols');
  const provs = mk('div', 'provs');
  const models = mk('div', 'models');
  cols.append(provs, models);
  pick.appendChild(cols);

  if (!anchor) {
    const foot = mk('div', 'foot');
    const manage = mk('button', null, T('gui.picker.manage'));
    manage.onclick = () => { close(); openSettings(); };
    foot.appendChild(manage);
    pick.appendChild(foot);
  }

  const choose = (m) => {
    close();
    const prev = model;
    model = m; setModelLabel();
    if (after) after();
    rpc.call('config.set', { key: 'model', value: m })
      .then(() => toast(`已切换到 ${shortModel(m)}`))
      .catch((e) => {
        model = prev; setModelLabel();
        if (after) after();
        toast(`切换失败：${(e.data && e.data.detail) || e.message || e}`);
      });
  };

  const modelRow = (m, subtitle) => {
    const b = mk('button', 'row');
    const nm = mk('span', 'nm', shortModel(m));
    b.appendChild(nm);
    if (subtitle) b.appendChild(mk('span', 'sub', subtitle));
    if (m === model) b.appendChild(mk('span', 'tick', '✓'));
    b.onclick = () => choose(m);
    return b;
  };

  function render() {
    provs.innerHTML = '';
    models.innerHTML = '';
    // Search narrows each provider's list in place: the provider column stays,
    // its count turns into a hit count, and empty providers dim out.
    const hits = authed.map((p) => (query
      ? p.models.filter((m) => shortModel(m).toLowerCase().includes(query))
      : p.models));
    if (query && !hits[provIdx].length) {
      const first = hits.findIndex((h) => h.length);
      if (first >= 0) provIdx = first;
    }
    authed.forEach((p, i) => {
      const b = mk('button', 'row' + (hits[i].length ? '' : ' dim'));
      b.setAttribute('aria-selected', String(i === provIdx && hits[i].length > 0));
      b.append(mk('span', 'nm', p.name), mk('span', 'ct', String(hits[i].length)));
      if (hits[i].includes(model)) b.appendChild(mk('span', 'tick', '•'));
      b.onclick = () => { if (!hits[i].length) return; provIdx = i; render(); };
      provs.appendChild(b);
    });
    const list = hits[provIdx] || [];
    if (!list.length) {
      models.appendChild(mk('div', 'empty', query ? T('gui.picker.no_match') : T('gui.picker.empty_provider')));
      return;
    }
    list.forEach((m) => models.appendChild(modelRow(m)));
    const active = models.querySelector('.tick');
    if (active) active.parentElement.scrollIntoView({ block: 'nearest' });
  }

  q.oninput = () => { query = q.value.trim().toLowerCase(); render(); };
  q.onkeydown = (e) => {
    e.stopPropagation();
    if (composing(e)) return;
    if (e.key === 'Escape') { e.preventDefault(); close(); }
    if (e.key === 'Enter') {
      const first = models.querySelector('.row');
      if (first) first.click();
    }
  };

  const onDown = (e) => { if (!e.target.closest('.mpick') && !host.contains(e.target)) close(); };
  function close() {
    document.removeEventListener('pointerdown', onDown, true);
    pick.remove();
  }

  render();
  document.body.appendChild(pick);
  // documentElement metrics, not window.innerWidth: the latter reads 0 inside
  // some embedded webviews and would push the popover into the corner.
  const vw = document.documentElement.clientWidth;
  const vh = document.documentElement.clientHeight;
  const r = host.getBoundingClientRect();
  const box = pick.getBoundingClientRect();
  const above = r.top - box.height - 8;
  pick.style.left = `${Math.max(12, Math.min(r.left, vw - box.width - 12))}px`;
  pick.style.top = `${above >= 12 ? above : Math.min(r.bottom + 8, Math.max(12, vh - box.height - 12))}px`;
  document.addEventListener('pointerdown', onDown, true);
  q.focus();
}

$('#modelChip').onclick = () => openModelPicker();

/* -- settings page: model & accounts ---------------------------------
   Replaces the demo panel wholesale. Every control here writes through a
   model.* method, so what the page shows is what is on disk. */
const kindLabel = (kind) => T('gui.model.kind.' + (kind || 'key'), null, T('gui.model.kind.key'));
const loginCmd = (slug) => `raven provider login ${String(slug).replace(/_/g, '-')}`;
const OFF_HEAD = 5;

let provOpen = null;
let provAll = false;
let provErr = '';
let provBusy = false;
let provFocus = false;

async function provWrite(fn) {
  if (provBusy) return;
  provBusy = true; provErr = '';
  try {
    await fn();
    await loadProviders();
  } catch (e) {
    provErr = (e.data && e.data.detail) || e.message || String(e);
  }
  provBusy = false;
  drawSettings();
}

function modelChips(pv) {
  const box = mk('div');
  const head = mk('div', 'pnote', T('gui.model.count_pick', { n: pv.models.length }));
  box.appendChild(head);
  if (!pv.models.length) return box;
  const chips = mk('div', 'chips');
  pv.models.slice(0, 10).forEach((m) => {
    const c = mk('span', 'chipm');
    c.appendChild(mk('span', null, shortModel(m)));
    const x = mk('button', null, '✕');
    x.title = T('gui.model.remove');
    x.setAttribute('aria-label', `${T('gui.model.remove')} ${m}`);
    x.onclick = () => provWrite(() => rpc.call('model.remove_model', { slug: pv.id, model: m }));
    c.appendChild(x);
    chips.appendChild(c);
  });
  if (pv.models.length > 10) chips.appendChild(mk('span', 'pnote', T('gui.model.count_more', { n: pv.models.length - 10 })));
  box.appendChild(chips);
  return box;
}

function provForm(pv) {
  const f = mk('div', 'pform');

  if (pv.kind === 'oauth') {
    f.appendChild(mk('div', 'pnote', T('gui.model.oauth_hint', { name: pv.name })));
    const cmd = mk('div', 'cmd');
    cmd.appendChild(mk('code', null, loginCmd(pv.id)));
    const cp = mk('button', 'mini ghost', T('gui.model.copy'));
    cp.onclick = () => {
      navigator.clipboard && navigator.clipboard.writeText(loginCmd(pv.id));
      cp.textContent = T('gui.model.copied');
      setTimeout(() => { cp.textContent = T('gui.model.copy'); }, 1200);
    };
    cmd.appendChild(cp);
    f.appendChild(cmd);
  } else {
    const base = mk('input');
    base.type = 'text';
    base.placeholder = pv.kind === 'local' ? T('gui.model.base_ph_local') : T('gui.model.base_ph');
    const key = mk('input');
    key.type = 'password';
    key.placeholder = pv.on ? T('gui.model.key_ph_update') : T('gui.model.key_ph', { name: pv.name });
    key.setAttribute('aria-label', `${pv.name} API Key`);

    const save = mk('button', 'mini', pv.on ? T('gui.model.update') : T('gui.model.connect'));
    save.onclick = () => {
      const params = { slug: pv.id };
      if (pv.kind !== 'local') params.api_key = key.value.trim();
      if (base.value.trim()) params.api_base = base.value.trim();
      if (pv.kind === 'local' && !params.api_base) { provErr = T('gui.model.need_base'); drawSettings(); return; }
      if (pv.kind !== 'local' && !params.api_key) { provErr = T('gui.model.need_key'); drawSettings(); return; }
      provWrite(() => rpc.call('model.save_key', params));
    };

    if (pv.needsBase || pv.kind === 'endpoint') {
      const r0 = mk('div', 'keyrow'); r0.appendChild(base); f.appendChild(r0);
    }
    const r1 = mk('div', 'keyrow');
    if (pv.kind !== 'local') r1.appendChild(key);
    r1.appendChild(save);
    f.appendChild(r1);
    if (pv.env) f.appendChild(mk('div', 'pnote', T('gui.model.env_hint', { env: pv.env })));
  }

  const add = mk('div', 'keyrow');
  const ai = mk('input');
  ai.type = 'text';
  ai.placeholder = T('gui.model.add_ph');
  const ab = mk('button', 'mini ghost', T('gui.add'));
  ab.onclick = () => {
    const v = ai.value.trim();
    if (!v) return;
    provWrite(() => rpc.call('model.add_model', { slug: pv.id, model: v }));
  };
  add.append(ai, ab);
  f.appendChild(add);
  f.appendChild(modelChips(pv));

  if (pv.on) {
    const cut = mk('button', 'mini ghost danger', T('gui.model.disconnect'));
    cut.style.justifySelf = 'start';
    cut.onclick = () => confirmAsk(T('gui.model.disconnect_title'),
      T('gui.model.disconnect_body', { name: pv.name }), T('gui.model.disconnect_title'),
      () => provWrite(() => rpc.call('model.disconnect', { slug: pv.id })));
    f.appendChild(cut);
  }
  if (provErr) f.appendChild(mk('div', 'perr', provErr));
  return f;
}

function provCard(pv) {
  const open = provOpen === pv.id;
  const c = mk('div', 'pcard' + (open ? ' open' : ''));
  const nm = mk('div', 'nm');
  nm.append(mk('span', 'led' + (pv.on ? '' : ' warn')), mk('span', null, pv.name));
  if (pv.id === curProvider) nm.appendChild(mk('span', 'tagm', T('gui.model.is_default')));
  c.appendChild(nm);
  const bits = [pv.on ? T('gui.model.state.connected') : kindLabel(pv.kind)];
  if (pv.models.length) bits.push(T('gui.model.count', { n: pv.models.length }));
  if (!pv.on && pv.kind === 'oauth') bits.push(T('gui.model.needs_login'));
  c.appendChild(mk('div', 'mo', bits.join(' · ')));
  const ctl = mk('div', 'ctl');
  const go = mk('button', 'mini' + (pv.on || open ? ' ghost' : ''),
    open ? T('gui.model.collapse') : (pv.on ? T('gui.model.manage') : T('gui.model.connect')));
  go.setAttribute('aria-expanded', String(open));
  go.onclick = () => { provOpen = open ? null : pv.id; provErr = ''; provFocus = !open; drawSettings(); };
  ctl.appendChild(go);
  c.appendChild(ctl);
  if (open) c.appendChild(provForm(pv));
  return c;
}

function drawModelPanel() {
  const host = $('#spanels');
  // Expanding a card redraws the panel; keeping the scroll offset and skipping
  // the panel's entry animation makes that read as an expand, not a reload.
  const keep = host.scrollTop;
  host.innerHTML = '';
  const p = mk('div', 'panel'); p.dataset.on = 'true';
  p.style.animation = 'none';

  const home = PROVIDERS.find((x) => x.id === curProvider);
  const d = scard(p, T('gui.model.default'));
  const pick = mk('button', 'mini ghost pickm');
  pick.append(mk('span', 'mono', shortModel(model) || T('gui.model.unset')), mk('span', 'car', '⌄'));
  pick.onclick = () => openModelPicker(pick, drawSettings);
  d.appendChild(pick);

  const on = PROVIDERS.filter((x) => x.on);
  const off = PROVIDERS.filter((x) => !x.on);

  /* Connected first and in its own card: which providers are live is the one
     thing this panel is asked, and the long tail of unconnected ones must not
     be the first thing the eye lands on. */
  const cOn = scard(p, T('gui.model.connected', { n: on.length }));
  if (!on.length) {
    cOn.appendChild(mk('div', 'pnote', T('gui.model.none_connected')));
  } else {
    const s = mk('div', 'fset');
    on.forEach((pv) => s.appendChild(provCard(pv)));
    cOn.appendChild(s);
  }

  const cOff = scard(p, T('gui.model.others', { n: off.length }));
  const s2 = mk('div', 'fset');
  (provAll ? off : off.slice(0, OFF_HEAD)).forEach((pv) => s2.appendChild(provCard(pv)));
  cOff.appendChild(s2);
  if (off.length > OFF_HEAD) {
    const more = mk('button', 'mini ghost',
      provAll ? T('gui.model.collapse') : T('gui.model.expand_rest', { n: off.length - OFF_HEAD }));
    more.setAttribute('aria-expanded', String(provAll));
    more.onclick = () => { provAll = !provAll; drawSettings(); };
    cOff.appendChild(more);
  }

  host.appendChild(p);
  host.scrollTop = keep;
  if (provFocus) {
    provFocus = false;
    const card = p.querySelector('.pcard.open');
    if (card) {
      const field = card.querySelector('.pform input');
      if (field) field.focus();
      card.scrollIntoView({ block: 'nearest' });
    }
  }
}

{
  const origDrawSettings = drawSettings;
  drawSettings = function () {
    origDrawSettings();
    if (sTab !== 'model') return;
    drawModelPanel();
    const p = $('#spanels').querySelector('.panel');
    if (p) modelTuning(p);
  };
}

/* ---- the writes the live layer can actually make --------------------- */

/* One delete per session, and a session that refuses stays in the list -- the
   rail must never claim something is gone while the file is still on disk. */
deleteAllSessions = async () => {
  const gone = [];
  for (const s of SESS.slice()) {
    try {
      await rpc.call('session.delete', { session_id: s.id });
      gone.push(s.id); dropDraft(s.id);
    } catch { /* counted by what is left below */ }
  }
  SESS = SESS.filter((s) => !gone.includes(s.id));
  cur = null;
  startDraft();
  drawSettings();
  toast(SESS.length
    ? T('gui.set.dat.del_partial', { n: gone.length, left: SESS.length })
    : T('gui.set.dat.del_done', { n: gone.length }));
};

/* The version check the rail-foot notice already does, on demand. No new
   backend: system.version carries the answer. */
checkUpdate = async (btn) => {
  const was = btn.textContent;
  btn.textContent = T('gui.set.checking'); btn.disabled = true;
  try {
    /* check:true = fetch now, not the daily cache: the button says 检查更新,
       and a person who just clicked it is asking about now. */
    const v = await rpc.call('system.version', { check: true });
    if (v.raven_version) APP_VERSION = v.raven_version;
    if (v.update_available) {
      showUpNote('ver', v.latest_version);
      drawSettings();
      askUpgrade();
      return;
    }
    /* The answer has to land on the button: this layer sends toasts to the
       console, and "nothing happened" is indistinguishable from a broken
       check. */
    btn.disabled = false;
    btn.textContent = T('gui.set.abt.latest');
    setTimeout(() => { btn.textContent = was; }, 2200);
    return;
  } catch (e) {
    btn.textContent = T('gui.set.abt.check_fail');
    setTimeout(() => { btn.textContent = was; }, 2600);
    if (window.console) console.error('[update check]', e);
  }
  btn.disabled = false;
};

/* -- skill market: browse, search and install from SkillHub -----------
   The skill tab mirrors the plugin tab exactly: the market IS the page,
   what you already have lives one level in (the 已安装 button top-right,
   back arrow to return), a category chip row filters, and every card
   opens the shared detail drawer.

   Everything comes from skillhub.search / skillhub.detail — the hub's
   /skills/search, 93k skills, paged, filterable by category. */
const HUB_CATS = [
  ['', 'gui.hubcat.all'],
  ['DEV', 'gui.hubcat.DEV'],
  ['FRONTEND-UI', 'gui.hubcat.FRONTEND-UI'],
  ['DEVOPS-INFRA', 'gui.hubcat.DEVOPS-INFRA'],
  ['DATA', 'gui.hubcat.DATA'],
  ['AI-ML', 'gui.hubcat.AI-ML'],
  ['TESTING', 'gui.hubcat.TESTING'],
  ['SECURITY', 'gui.hubcat.SECURITY'],
  ['AUTH', 'gui.hubcat.AUTH'],
  ['MULTIMEDIA', 'gui.hubcat.MULTIMEDIA'],
  ['WRITING', 'gui.hubcat.WRITING'],
  ['DOC-PROC', 'gui.hubcat.DOC-PROC'],
  ['COMMS', 'gui.hubcat.COMMS'],
  ['WORKFLOW', 'gui.hubcat.WORKFLOW'],
  ['PRODUCTIVITY', 'gui.hubcat.PRODUCTIVITY'],
  ['META', 'gui.hubcat.META'],
  ['OTHER', 'gui.hubcat.OTHER'],
];
const HUB_PAGE = 24;

let skView = 'market';   // market | installed — same shape as plugView
let hubQuery = '';
let hubCat = '';
let hubPage = 1;
let hubItems = [];
let hubTotal = 0;
let hubState = 'idle';   // idle | loading | done | error
let hubErr = '';
let hubDetails = {};     // id -> skillhub.detail result
let hubBusy = null;      // id of the skill being installed / removed
let hubDebounce = null;
let skDrawer = null;     // { kind: 'market'|'inst', id } while the drawer shows a skill

const hubCatLabel = (key) => { const c = HUB_CATS.find((x) => x[0] === key); return c ? T(c[1]) : key; };
const hubNum = (n) => (n >= 10000 ? `${(n / 1000).toFixed(0)}k` : n.toLocaleString('en-US'));

const skRedraw = () => {
  if ($('#capsPage').dataset.open === 'true' && extTab === 'skill') drawCaps();
  if (skDrawer) skOpenDetail(skDrawer.kind, skDrawer.id);
};

/* The hub search round-trip takes seconds, so page/category flips are
   cached for the session and served instantly; only a genuinely new
   (query, category, page) hits the network — behind a skeleton grid,
   never a frozen page. A request seq drops stale responses when the
   user outclicks the network. */
const hubCache = new Map();
let hubSeq = 0;

async function hubSearch(page) {
  hubPage = page || 1;
  const key = `${hubQuery}\0${hubCat}\0${hubPage}`;
  const hit = hubCache.get(key);
  if (hit) {
    hubItems = hit.items; hubTotal = hit.total;
    hubState = 'done'; hubErr = '';
    drawCaps();
    return;
  }
  const seq = ++hubSeq;
  hubState = 'loading'; hubErr = '';
  drawCaps();
  try {
    const r = await rpc.call('skillhub.search', {
      query: hubQuery, category: hubCat, page: hubPage, limit: HUB_PAGE,
    });
    if (seq !== hubSeq) return;
    hubItems = r.items || [];
    hubTotal = r.total || hubItems.length;
    hubState = 'done';
    hubCache.set(key, { items: hubItems, total: hubTotal });
    if (hubCache.size > 60) hubCache.delete(hubCache.keys().next().value);
  } catch (e) {
    if (seq !== hubSeq) return;
    hubItems = [];
    hubErr = (e.data && e.data.detail) || e.message || String(e);
    hubState = 'error';
  }
  drawCaps();
}

const hubSearchSoon = () => {
  clearTimeout(hubDebounce);
  hubDebounce = setTimeout(() => hubSearch(1), 320);
};

function hubInstall(it) {
  hubBusy = it.id; skRedraw();
  rpc.call('skillhub.install', { id: it.id })
    .then(() => loadExt().catch(() => {}))
    .then(() => { it.installed = true; it.installed_name = it.name; })
    .catch((e) => toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e })))
    .finally(() => { hubBusy = null; skRedraw(); });
}

function hubRemove(it) {
  hubBusy = it.id; skRedraw();
  rpc.call('skillhub.remove', { name: it.installed_name || it.name })
    .then(() => loadExt().catch(() => {}))
    .then(() => { it.installed = false; it.installed_name = ''; })
    .catch((e) => toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e })))
    .finally(() => { hubBusy = null; skRedraw(); });
}

/* Installed rows come from ext.list, so the market row (if any) is found
   by name; removal goes through the same skillhub.remove. */
function skRemoveInstalled(c) {
  hubBusy = c.id; skRedraw();
  rpc.call('skillhub.remove', { name: c.name })
    .then(() => {
      const it = hubItems.find((x) => x.name === c.name || x.installed_name === c.name);
      if (it) { it.installed = false; it.installed_name = ''; }
      toast(T('gui.caps.removed_x', { name: c.name }));
      skCloseDetail();
      return loadExt();
    })
    .catch((e) => toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e })))
    .finally(() => { hubBusy = null; skRedraw(); });
}

/* 去使用 = start a task with the capability already named: a fresh session
   whose composer opens pre-filled, cursor at the end, ready to complete. */
function useInTask(promptKey, name) {
  closeDetail();
  $('#newBtn').click();
  const ta = $('#ta');
  ta.value = T(promptKey, { name });
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

/* quality_score is 0-1; the site shows it out of five, so do the same. */
function hubStars(score) {
  const val = Math.round((score || 0) * 5 * 10) / 10;
  const box = mk('div', 'stars');
  for (let i = 1; i <= 5; i++) {
    const fill = Math.max(0, Math.min(1, val - i + 1));
    const s = mk('span', 'st');
    s.style.setProperty('--f', `${Math.round(fill * 100)}%`);
    s.textContent = '★';
    box.appendChild(s);
  }
  box.appendChild(mk('span', 'sv', val.toFixed(1)));
  return box;
}

/* ── market view: same card grammar as pmMarketCard ─────────────── */

function hubCard(it) {
  const c = mk('div', 'hubcard pmcard' + (it.installed ? ' dim' : ''));

  const top = mk('div', 'top');
  const nmwrap = mk('div', 'pmhead');
  nmwrap.appendChild(pmTile(it.name));
  const id = mk('div', 'pmid');
  const nm = mk('div', 'pmnm');
  nm.appendChild(mk('span', null, it.name));
  id.appendChild(nm);
  const pub = it.source || hubCatLabel(it.category);
  if (pub) id.appendChild(mk('div', 'pmpub', pub));
  nmwrap.appendChild(id);
  top.appendChild(nmwrap);
  top.appendChild(hubStars(it.quality_score));
  c.appendChild(top);

  c.appendChild(mk('p', 'one', it.description || ''));

  const foot = mk('div', 'foot');
  const act = mk('div', 'act');
  if (hubBusy === it.id) act.appendChild(mk('span', 'pnote', T('gui.hub.working')));
  else if (it.installed) act.appendChild(mk('span', 'okpill', T('gui.hub.installed')));
  else {
    // Two hit zones: the button installs right away, the card opens the sheet.
    const b = mk('button', 'mini gold', T('gui.hub.install'));
    b.onclick = (e) => { e.stopPropagation(); hubInstall(it); };
    act.appendChild(b);
  }
  foot.appendChild(act);
  c.appendChild(foot);

  c.tabIndex = 0;
  c.setAttribute('role', 'button');
  c.onclick = () => skOpenDetail('market', it.id);
  c.onkeydown = (e) => { if (e.key === 'Enter') skOpenDetail('market', it.id); };
  return c;
}

function drawSkillMarket(box) {
  const chips = mk('div', 'pmchips');
  HUB_CATS.forEach(([key, label]) => {
    const b = mk('button', 'pill', T(label));
    b.setAttribute('aria-pressed', String(hubCat === key));
    b.onclick = () => { if (key === hubCat) return; hubCat = key; hubSearch(1); };
    chips.appendChild(b);
  });
  box.appendChild(chips);

  if (hubErr) {
    const wrap = mk('div', 'empty-note');
    wrap.append(mk('div', null, T('gui.hub.err', { err: hubErr })), document.createElement('br'));
    const retry = mk('button', 'mini ghost', T('gui.plug.retry'));
    retry.onclick = () => hubSearch(hubPage);
    wrap.appendChild(retry);
    box.appendChild(wrap);
    return;
  }
  if (hubState === 'loading') {
    const g = mk('div', 'hubgrid');
    for (let i = 0; i < 9; i++) g.appendChild(hubSkeleton());
    box.appendChild(g);
    return;
  }
  if (!hubItems.length) { box.appendChild(mk('div', 'empty-note', T('gui.hub.empty_filter'))); return; }

  const g = mk('div', 'hubgrid');
  hubItems.forEach((it) => g.appendChild(hubCard(it)));
  box.appendChild(g);
  const pager = hubPager();
  if (pager) box.appendChild(pager);
}

/* Placeholder card shown the instant a chip / query flips, so the page
   answers the click immediately instead of freezing on stale results. */
function hubSkeleton() {
  const c = mk('div', 'hubcard skel');
  c.setAttribute('aria-hidden', 'true');
  const bar = (w, h, extra) => {
    const b = mk('span', 'sk');
    b.style.cssText = `width:${w};height:${h};${extra || ''}`;
    return b;
  };
  const top = mk('div', 'top');
  const head = mk('div', 'pmhead');
  head.appendChild(bar('34px', '34px', 'border-radius:10px;flex:none'));
  const id = mk('div', 'pmid');
  id.style.cssText = 'display:grid;gap:6px';
  id.append(bar('110px', '12px'), bar('70px', '9px'));
  head.appendChild(id);
  top.appendChild(head);
  c.appendChild(top);
  const one = mk('div');
  one.style.cssText = 'display:grid;gap:7px';
  one.append(bar('100%', '10px'), bar('72%', '10px'));
  c.appendChild(one);
  const foot = mk('div', 'foot');
  foot.append(bar('58px', '22px', 'margin-left:auto;border-radius:8px'));
  c.appendChild(foot);
  return c;
}

function hubPager() {
  const pages = Math.max(1, Math.ceil(hubTotal / HUB_PAGE));
  if (pages <= 1) return null;
  const bar = mk('div', 'hubpage');
  const prev = mk('button', 'mini ghost', T('gui.hub.prev'));
  prev.disabled = hubPage <= 1;
  prev.onclick = () => hubSearch(hubPage - 1);
  const next = mk('button', 'mini ghost', T('gui.hub.next'));
  next.disabled = hubPage >= pages;
  next.onclick = () => hubSearch(hubPage + 1);
  bar.append(prev, mk('span', 'pnote', T('gui.hub.page', { p: hubPage, n: hubNum(pages) })), next);
  return bar;
}

/* ── installed view: same shape as drawPlugInstalled ────────────── */

function skInstCard(c) {
  const card = mk('div', 'hubcard pmcard');
  const top = mk('div', 'top');
  const nmwrap = mk('div', 'pmhead');
  nmwrap.appendChild(pmTile(c.name));
  const id = mk('div', 'pmid');
  const nm = mk('div', 'pmnm');
  nm.appendChild(mk('span', null, c.name));
  id.appendChild(nm);
  if (c.src) id.appendChild(mk('div', 'pmpub', c.src));
  nmwrap.appendChild(id);
  top.appendChild(nmwrap);
  card.appendChild(top);

  card.appendChild(mk('p', 'one', c.one || ''));

  const foot = mk('div', 'foot');
  foot.appendChild(mk('span'));
  const act = mk('div', 'act');
  const use = mk('button', 'mini gold', T('gui.hub.use'));
  use.onclick = (e) => { e.stopPropagation(); useInTask('gui.hub.use_prompt', c.name); };
  act.appendChild(use);
  foot.appendChild(act);
  card.appendChild(foot);

  card.tabIndex = 0;
  card.setAttribute('role', 'button');
  card.onclick = () => skOpenDetail('inst', c.id);
  card.onkeydown = (e) => { if (e.key === 'Enter') skOpenDetail('inst', c.id); };
  return card;
}

function drawSkillInstalled(box) {
  const back = mk('div', 'pmback');
  const b = mk('button', 'mini ghost');
  b.textContent = '← ' + T('gui.plug.back');
  b.onclick = () => { skView = 'market'; skCloseDetail(); drawCaps(); };
  back.append(b, mk('b', null, T('gui.plug.installed_title')));
  box.appendChild(back);

  if (!SKILLS.length) { box.appendChild(mk('div', 'empty-note', T('gui.hub.empty_installed'))); return; }
  const g = mk('div', 'hubgrid');
  SKILLS.forEach((c) => g.appendChild(skInstCard(c)));
  box.appendChild(g);
}

/* ── detail drawer (market entries + installed skills) ──────────── */

function skCloseDetail() { skDrawer = null; closeDetail(); }

/* One renderer for both entry points: an installed skill opens the exact
   hub detail the market shows (fetched via the id its marker recorded);
   the only delta is the action set — 去使用 in the header, 卸载 under 管理.
   Hand-written/builtin skills have no hub entry to show, so they get the
   same head/about/manage skeleton without the hub sections. */
function skOpenDetail(kind, id) {
  skDrawer = { kind, id };
  if ($('#capsPage').dataset.open !== 'true') openCaps('skill');
  const body = $('#dBody'); body.innerHTML = '';
  $('#dTitle').textContent = '';
  $('#detail').dataset.open = 'true';

  const sec = (label, el) => {
    const s = mk('div', 'pmsec');
    if (label) s.appendChild(mk('div', 'cap', label));
    s.appendChild(el);
    body.appendChild(s);
  };

  const inst = kind === 'inst' ? SKILLS.find((x) => x.id === id) : null;
  if (kind === 'inst' && !inst) return;
  const it = kind === 'market'
    ? (hubItems.find((x) => x.id === id) || {})
    : (hubItems.find((x) => x.name === inst.name || x.installed_name === inst.name) || {});
  const hubId = kind === 'market' ? id : (inst.hubId || it.id || '');
  const installed = !!(inst || it.installed);
  const busy = hubBusy != null && (hubBusy === it.id || (inst && hubBusy === inst.id));
  const name = (inst && inst.name) || it.name || String(id);

  const header = (metaBits, sourceUrl) => {
    const head = mk('div', 'pmdhead');
    head.appendChild(pmTile(name));
    const meta = mk('div', 'pmdmeta');
    const l1 = mk('div', 'l1');
    l1.appendChild(mk('b', null, name));
    meta.appendChild(l1);
    const l2 = mk('div', 'l2', metaBits.filter(Boolean).join(' · ') + (sourceUrl ? ' · ' : ''));
    if (sourceUrl) {
      const a = mk('a', null, T('gui.plug.homepage') + ' ↗');
      a.href = sourceUrl; a.target = '_blank'; a.rel = 'noreferrer';
      l2.appendChild(a);
    }
    meta.appendChild(l2);
    head.appendChild(meta);
    const hact = mk('div', 'dact');
    if (busy) hact.appendChild(mk('span', 'pnote', T('gui.hub.working')));
    else if (installed) {
      const use = mk('button', 'mini gold', T('gui.hub.use'));
      use.onclick = () => useInTask('gui.hub.use_prompt', name);
      hact.appendChild(use);
    } else {
      const b = mk('button', 'mini gold', T('gui.hub.install'));
      b.onclick = () => hubInstall(it);
      hact.appendChild(b);
    }
    head.appendChild(hact);
    body.appendChild(head);
  };

  const manage = (removable, fn) => {
    if (!removable || busy) return;
    const man = mk('div');
    man.appendChild(mk('div', 'pnote', T('gui.hub.uninstall_note')));
    const rm = mk('button', 'mini ghost', T('gui.plug.uninstall'));
    rm.style.marginTop = '8px';
    pmArm(rm, fn);
    man.appendChild(rm);
    sec(T('gui.plug.sec_manage'), man);
  };

  if (!hubId) {
    header([inst.src, reachText(inst.reach)]);
    if (inst.one) sec(T('gui.plug.sec_about'), mk('div', 'pmdesc', inst.one));
    manage(inst.hub, () => skRemoveInstalled(inst));
    return;
  }

  const render = (d) => {
    if (!skDrawer || skDrawer.id !== id) return;
    body.innerHTML = '';

    header([hubCatLabel(d.category) || d.category, d.source || it.source, d.license], it.source_url);

    const about = mk('div');
    about.appendChild(mk('div', 'pmdesc', d.description || it.description || (inst && inst.one) || ''));
    const tags = (d.tags && d.tags.length ? d.tags : it.tags) || [];
    if (tags.length) {
      const tg = mk('div', 'tags');
      tg.style.marginTop = '8px';
      tags.forEach((t) => tg.appendChild(mk('span', 'tag', t)));
      about.appendChild(tg);
    }
    sec(T('gui.plug.sec_about'), about);

    const rate = mk('div');
    rate.appendChild(hubStars(d.quality_score != null ? d.quality_score : it.quality_score));
    const s = d.subscores || {};
    if (s.utility || s.robustness || s.safety) {
      rate.appendChild(mk('div', 'pnote', T('gui.hub.rating', { u: s.utility, r: s.robustness, s: s.safety })));
    }
    if (s.flags && s.flags.length) rate.appendChild(mk('div', 'perr', T('gui.hub.flags', { flags: s.flags.join(', ') })));
    sec(T('gui.hub.sec_rating'), rate);

    if (d.files && d.files.length) {
      const fb = mk('div');
      fb.appendChild(mk('div', 'pnote', d.files.slice(0, 12).join('  ·  ')
        + (d.files.length > 12 ? '  ·  ' + T('gui.hub.more_files', { n: d.files.length - 12 }) : '')));
      if (d.body_tokens) fb.appendChild(mk('div', 'pnote', `${hubNum(d.body_tokens)} tokens`));
      sec(T('gui.hub.n_files', { n: d.files.length }), fb);
    }
    if (d.skill_md) sec(T('gui.hub.sec_preview'), mk('pre', 'mdprev', d.skill_md.slice(0, 1600)));

    if (installed) manage(true, () => (inst ? skRemoveInstalled(inst) : hubRemove(it)));
  };

  const cached = hubDetails[hubId];
  if (cached) { render(cached); return; }
  body.appendChild(mk('div', 'pnote', T('gui.hub.reading')));
  rpc.call('skillhub.detail', { id: hubId })
    .then((d) => { hubDetails[hubId] = d; render(d); })
    .catch((e) => toast(T('gui.hub.err', { err: (e.data && e.data.detail) || e.message || e })));
}

/* ── page assembly: mirrors the plugin tab's wrapper ────────────── */

/* The 已安装 entry point rides in the filter bar, exactly like the
   plugin tab's — created once, shown only on the skill market. */
const skInstBtn = (() => {
  const b = mk('button', 'pminstbtn');
  b.onclick = () => { skView = skView === 'installed' ? 'market' : 'installed'; skCloseDetail(); drawCaps(); };
  $('.cbar').appendChild(b);
  return { sync() {
    b.hidden = extTab !== 'skill' || skView === 'installed';
    b.innerHTML = '';
    b.append(mk('span', null, T('gui.plug.installed_n', { n: SKILLS.length })));
  } };
})();

{
  const origDrawCaps = drawCaps;
  drawCaps = function () {
    if (extTab !== 'skill') { origDrawCaps(); skInstBtn.sync(); return; }

    const box = $('#capsBody'); box.innerHTML = '';
    const title = T('gui.tab.skills');
    $('#capsTitle').textContent = skView === 'installed' ? T('gui.plug.installed_title') : title;
    $('#capsPage').setAttribute('aria-label', title);
    $('#cKind').hidden = true;
    $('#advAdd').hidden = true;
    $('#cq').placeholder = T('gui.hub.search_ph');
    $('.cbar').style.display = skView === 'installed' ? 'none' : '';

    if (skView === 'installed') drawSkillInstalled(box);
    else drawSkillMarket(box);
    skInstBtn.sync();
    drawCapsBadge();
  };

  const origInput = $('#cq').oninput;
  $('#cq').oninput = () => {
    if (extTab === 'skill') { hubQuery = $('#cq').value.trim(); hubSearchSoon(); return; }
    if (origInput) origInput();
  };
  $('#cq').onkeydown = (e) => {
    if (composing(e)) return;
    if (e.key !== 'Enter' || extTab !== 'skill') return;
    e.preventDefault();
    clearTimeout(hubDebounce);
    hubQuery = $('#cq').value.trim();
    hubSearch(1);
  };

  const prevExtSet = extSet;
  extSet = function (tab) {
    const was = extTab;
    prevExtSet(tab);
    if (extTab !== was) { skView = 'market'; skDrawer = null; hubQuery = ''; hubCat = ''; }
  };

  const prevShowPage = showPage;
  showPage = function (id) {
    prevShowPage(id);
    if (id !== 'capsPage') skDrawer = null;
  };

  const prevOpenCaps = openCaps;
  openCaps = async function (tab) {
    await prevOpenCaps(tab);
    if (extTab === 'skill' && skView === 'market' && hubState === 'idle') hubSearch(1);
  };

  const prevCloseDetail = closeDetail;
  closeDetail = function () { skDrawer = null; prevCloseDetail(); };
  $('#dClose').onclick = () => closeDetail();
}

/* -- plugin market ----------------------------------------------------
   The plugin tab is market-first: the page IS the catalog, and what you
   already have lives one level in (the 已安装 button top-right, back
   arrow to return). Both views share the hub grid so they read as the
   same place. Install is a disk transaction server-side; everything
   slower than ~8s (an OAuth browser round-trip) streams back over
   mcp.status / oauth.pending / oauth.done notifications.               */

let plugView = 'market';    // market | installed
let pmItems = [], pmCats = [], pmState = 'idle', pmErr = '';
let pmQuery = '', pmCat = '', pmBusy = null, pmDebounce = null;
let pmDrawer = null;        // { kind: 'market'|'inst', id } while the drawer shows a plugin
let pmForm = false;         // drawer: apikey form unfolded
let pmConfirm = false;      // drawer: stdio run-locally confirm unfolded
const pmAuthWait = Object.create(null);  // server -> auth url while the browser round-trip is pending
const pmAuthEnd = Object.create(null);   // server -> epoch ms the authorization window closes at
let pmAuthClock = null;

const pmAuthLeft = (id) => {
  const end = pmAuthEnd[id];
  if (!end) return '';
  const s = Math.max(0, Math.round((end - Date.now()) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};

/* Writes the remaining time into whatever is showing it, rather than redrawing
   the panel once a second: a countdown that rebuilds its own surroundings would
   drop the focus and the scroll of a reader who is mid-decision. */
function pmAuthTick(on) {
  if (on && !pmAuthClock) {
    pmAuthClock = setInterval(() => {
      const live = Object.keys(pmAuthEnd);
      if (!live.length) { pmAuthTick(false); return; }
      document.querySelectorAll('[data-authcd]').forEach((el) => {
        el.textContent = pmAuthLeft(el.dataset.authcd);
      });
    }, 1000);
  } else if (!on && pmAuthClock && !Object.keys(pmAuthEnd).length) {
    clearInterval(pmAuthClock);
    pmAuthClock = null;
  }
}
/* Installs whose authentication hasn't been proven yet. An auth plugin only
   counts as installed once its connection authenticates: while an id is in
   here the entry stays out of every "installed" surface, and the pending
   install resolves from events — connected -> installed, a settled auth
   failure -> the install is rolled back (plug.remove). */
const pmPending = new Set();
const pmMarketIds = new Set();

const pmRedraw = () => {
  if ($('#capsPage').dataset.open === 'true' && extTab === 'plugin') drawCaps();
  if (pmDrawer) pmOpenDetail(pmDrawer.kind, pmDrawer.id, true);
  drawCapsBadge();
};

async function pmSearch() {
  pmState = 'loading'; pmErr = '';
  drawCaps();
  try {
    const r = await rpc.call('plughub.search', { q: pmQuery, category: pmCat });
    pmItems = r.items || [];
    pmCats = r.categories || [];
    pmItems.forEach((it) => pmMarketIds.add(it.id));
    pmState = 'done';
  } catch (e) {
    pmItems = [];
    pmErr = (e.data && e.data.detail) || e.message || String(e);
    pmState = 'error';
  }
  drawCaps();
}

const pmSearchSoon = () => { clearTimeout(pmDebounce); pmDebounce = setTimeout(pmSearch, 320); };

const pmMcpRows = () => PLUGINS.filter((p) => p.m);
const pmPyRows = () => PLUGINS.filter((p) => !p.m);
const pmText = (v) => (v && typeof v === 'object' ? v[LANG] || v.en || '' : String(v || ''));
const pmHost = (url) => { try { return new URL(url).host; } catch { return url || ''; } };
const pmEntryMcp = (entry) => (entry.contributes || []).find((c) => c.kind === 'mcp') || null;

/* Status vocabulary: a dot plus a word, never color alone. */
const PM_ST = {
  off:           { k: 'gui.plug.st_off',  cls: 'off' },
  connecting:    { k: 'gui.plug.st_conn', cls: 'busy' },
  connected:     { k: 'gui.plug.st_on',   cls: 'ok' },
  auth_required: { k: 'gui.plug.st_auth', cls: 'bad' },
  error:         { k: 'gui.plug.st_err',  cls: 'bad' },
  disconnected:  { k: 'gui.plug.st_off',  cls: 'off' },
};
function pmStatus(m) {
  if (pmAuthWait[m.name]) return { k: 'gui.plug.st_wait', cls: 'busy' };
  if (!m.enabled) return PM_ST.off;
  return PM_ST[m.state] || PM_ST.off;
}

function pmTile(name) {
  // Stable per-name hue: same plugin, same colour, every render and page.
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  const t = mk('span', 'pmtile th' + (h % 8));
  t.textContent = (name[0] || '?').toUpperCase();
  return t;
}

const PM_VFD = '<svg class="pmvfd" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
  + '<path d="M12 2l2.4 2.4 3.4-.5 1 3.3 3.2 1.4-1.3 3.4 1.3 3.4-3.2 1.4-1 3.3-3.4-.5L12 22l-2.4-2.4-3.4.5-1-3.3-3.2-1.4L3.3 12 2 8.6l3.2-1.4 1-3.3 3.4.5z"/>'
  + '<path d="M9.5 12.2l1.8 1.8 3.6-3.8" stroke="var(--ink)" stroke-width="2" fill="none"/></svg>';

/* ── actions ─────────────────────────────────────────────────────── */

function pmToggle(name, on) {
  const row = pmMcpRows().find((p) => p.m.name === name);
  if (row) row.m.enabled = on;
  pmRedraw();
  rpc.call('plug.toggle', { name, enabled: on })
    .then((r) => { if (r.mcp && row) Object.assign(row.m, r.mcp); pmRedraw(); })
    .catch((e) => { toast(T('gui.plug.op_failed', { err: e.message || e })); loadExt().then(pmRedraw).catch(() => {}); });
}

/* Install progress sheet: the install drives the detail modal through the
   whole transaction -- write config -> connect -> (browser authorization)
   -> done -- so the user sees where it stands and what to do next. State
   lives here; the drawer kind 'progress' renders it and events advance it.
   Closing the sheet (X) leaves the install running in the background, and
   pmProg survives with it: clicking the plugin while the install still runs
   reopens this sheet (pmOpenDetail redirects). The state is released when
   the story ends -- done/fail acknowledged or resolved off-screen, cancel,
   uninstall. */
let pmProg = null;  // { id, name, mode, host, step, state: run|done|fail, err, tools }

function pmProgOpen(entry) {
  const mcp = pmEntryMcp(entry);
  pmProg = {
    id: entry.id,
    name: pmText(entry.name),
    mode: mcp ? (mcp.auth || {}).mode || 'none' : 'none',
    host: mcp ? pmHost((mcp.connection || {}).url) : '',
    step: 0, state: 'run', err: '', tools: 0,
  };
  pmOpenDetail('progress', entry.id);
}

/* The sheet is authoritative while it shows this id: toasts for the same
   outcome would just repeat it. */
const pmProgShows = (id) => !!(pmProg && pmProg.id === id && pmDrawer && pmDrawer.kind === 'progress');

function pmProgStep(n) {
  if (pmProg && pmProg.state === 'run' && n > pmProg.step) { pmProg.step = n; pmRedraw(); }
}

/* tools === null means "installed, still connecting" (a no-auth entry whose
   connect outlived the RPC window): the sheet closes the story as installed
   and a later connected event fills the tool count in. */
function pmProgDone(tools) {
  if (!pmProg) return;
  if (pmProg.state !== 'run' && !(pmProg.state === 'done' && pmProg.tools == null)) return;
  pmProg.state = 'done';
  pmProg.tools = tools;
  // Resolved while the sheet is closed: the toast carries the news and the
  // plugin goes back to opening its normal detail.
  if (!pmProgShows(pmProg.id)) pmProg = null;
  pmRedraw();
}

function pmProgFail(err) {
  if (!pmProg || pmProg.state !== 'run') return;
  pmProg.state = 'fail';
  pmProg.err = err || '';
  if (!pmProgShows(pmProg.id)) pmProg = null;
  pmRedraw();
}

function pmProgRender(body) {
  const pg = pmProg;
  if (!pg) return;
  const head = mk('div', 'pmdhead');
  head.appendChild(pmTile(pg.name));
  const meta = mk('div', 'pmdmeta');
  const l1 = mk('div', 'l1');
  l1.appendChild(mk('b', null, pg.name));
  meta.appendChild(l1);
  meta.appendChild(mk('div', 'l2', T('gui.plug.prog_cap')));
  head.appendChild(meta);
  const hact = mk('div', 'dact');
  const authPhase = pg.state === 'run' && pg.mode === 'oauth' && pg.step >= 2;
  if (authPhase) {
    if (pmAuthWait[pg.id]) {
      const re = mk('button', 'mini', T('gui.plug.reopen'));
      re.onclick = () => window.open(pmAuthWait[pg.id], '_blank');
      hact.appendChild(re);
    }
    const c = mk('button', 'mini ghost', T('gui.plug.cancel'));
    c.onclick = () => { pmPending.delete(pg.id); pmRemove(pg.id, pg.name); };
    hact.appendChild(c);
  } else if (pg.state === 'run') {
    hact.appendChild(mk('span', 'pnote', T('gui.hub.working')));
  } else {
    if (pg.state === 'done') {
      const use = mk('button', 'mini gold', T('gui.hub.use'));
      use.onclick = () => useInTask('gui.plug.use_prompt', pg.name);
      hact.appendChild(use);
    }
    const x = mk('button', 'mini ghost', T('gui.close'));
    x.onclick = () => closeDetail();
    hact.appendChild(x);
  }
  head.appendChild(hact);
  body.appendChild(head);

  const keys = ['prog_write', 'prog_conn'].concat(pg.mode === 'oauth' ? ['prog_auth'] : [], ['prog_finish']);
  const list = mk('div', 'pmsteps');
  keys.forEach((k, i) => {
    const done = pg.state === 'done' || i < pg.step;
    const cur = pg.state !== 'done' && !done && i === Math.min(pg.step, keys.length - 1);
    const cls = done ? ' done' : cur ? (pg.state === 'fail' ? ' fail' : ' cur') : '';
    const row = mk('div', 'pstep' + cls);
    const ic = mk('i', 'ic');
    if (done) ic.textContent = '✓';
    else if (cur && pg.state === 'fail') ic.textContent = '✕';
    row.append(ic, mk('span', null, T('gui.plug.' + k)));
    list.appendChild(row);
  });
  body.appendChild(list);

  if (pg.state === 'done') {
    body.appendChild(mk('div', 'pnote', pg.tools == null
      ? T('gui.plug.installed_conn', { name: pg.name })
      : T('gui.plug.prog_ok', { n: pg.tools })));
  } else if (pg.state === 'fail') {
    body.appendChild(mk('div', 'pnote', T('gui.plug.auth_fail_rm', { name: pg.name })));
    if (pg.err) body.appendChild(mk('div', 'perr', pg.err));
  } else if (authPhase) {
    const note = mk('div', 'pnote',
      T(pmAuthWait[pg.id] ? 'gui.plug.prog_auth_hint' : 'gui.plug.prog_auth_soon', { host: pg.host }));
    if (pmAuthEnd[pg.id]) {
      note.append(' ', mk('span', 'pcd', T('gui.plug.auth_left') + ' '));
      const cd = mk('span', 'pcd b', pmAuthLeft(pg.id));
      cd.dataset.authcd = pg.id;
      note.lastChild.appendChild(cd);
    }
    body.appendChild(note);
  } else if (pg.mode === 'oauth') {
    body.appendChild(mk('div', 'pnote', T('gui.plug.prog_auth_soon', { host: pg.host })));
  }
}

function pmInstall(entry, form) {
  // Pending from the first moment: the ledger lands on disk mid-call, and the
  // entry must not read as installed anywhere before its auth is proven.
  pmBusy = entry.id; pmPending.add(entry.id);
  pmProgOpen(entry);
  pmRedraw();
  rpc.call('plug.install', { id: entry.id, form: form || {} })
    .then((r) => {
      const it = pmItems.find((x) => x.id === entry.id);
      if (r.pending) {
        // Guard on the pending mark: a cancel mid-call already rolled the
        // install back, and marking it installed here would resurrect it.
        if (pmPending.has(entry.id)) {
          if (it) it.installed = false;
          if (!pmAuthWait[entry.id] && !pmProgShows(entry.id)) {
            toast(T('gui.plug.wait_auth', { name: pmText(entry.name) }));
          }
        }
      } else {
        pmPending.delete(entry.id);
        if (it) it.installed = true;
        const st = r.mcp && r.mcp.state;
        if (st === 'connected') {
          pmProgDone(r.mcp.tool_count || 0);
          if (!pmProgShows(entry.id)) toast(T('gui.plug.installed_ok', { name: pmText(entry.name), n: r.mcp.tool_count }));
        } else {
          if (pmProg && pmProg.id === entry.id) pmProgDone(null);
          if (!pmProgShows(entry.id)) toast(T('gui.plug.installed_conn', { name: pmText(entry.name) }));
        }
      }
      pmForm = false; pmConfirm = false;
      return loadExt();
    })
    .catch((e) => {
      // Includes the backend's own rollback (auth settled as failed inside
      // the connect window): nothing is installed, clear the pending mark.
      pmPending.delete(entry.id);
      const err = (e.data && e.data.detail) || e.message || e;
      if (pmProg && pmProg.id === entry.id) pmProgFail(String(err));
      if (!pmProgShows(entry.id)) toast(T('gui.plug.op_failed', { err }));
      return loadExt().catch(() => {});
    })
    .finally(() => { pmBusy = null; pmRedraw(); });
}

/* Card-level install: fetch the manifest, install straight away when nothing
   needs input; anything with a key form or a run-locally confirm opens the
   sheet already unfolded at that step. */
function pmQuickInstall(it) {
  pmBusy = it.id; pmRedraw();
  rpc.call('plughub.detail', { id: it.id })
    .then((r) => {
      const entry = r.item;
      const mcp = pmEntryMcp(entry);
      const mode = mcp ? (mcp.auth || {}).mode || 'none' : 'none';
      const stdio = !!(mcp && (mcp.connection || {}).command);
      if (mode === 'apikey' || stdio) {
        pmBusy = null;
        if (mode === 'apikey') pmForm = true; else pmConfirm = true;
        pmOpenDetail('market', entry.id, true);
        return;
      }
      pmInstall(entry, {});
    })
    .catch((e) => {
      pmBusy = null;
      toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e }));
      pmRedraw();
    });
}

/* No window.confirm (the WKWebView shell has no JS-panel delegate): a
   destructive button arms on first click and fires on the second. */
function pmArm(btn, fn) {
  let armed = false;
  const label = btn.textContent;
  btn.onclick = (e) => {
    e.stopPropagation();
    if (!armed) {
      armed = true;
      btn.textContent = T('gui.plug.confirm_remove');
      btn.classList.add('bad');
      setTimeout(() => { armed = false; btn.textContent = label; btn.classList.remove('bad'); }, 4000);
      return;
    }
    fn();
  };
}

/* A pending install failed authentication: the plugin never counted as
   installed, so undo the disk transaction and put the market card back. */
function pmPendingFail(name, why) {
  if (!pmPending.has(name)) return;
  pmPending.delete(name);
  delete pmAuthWait[name];
  const it = pmItems.find((x) => x.id === name);
  if (it) it.installed = false;
  /* The cause travels with the failure. A rollback that only says "removed"
     reads as raven losing the plugin; "the authorization window closed" tells
     the reader what to do differently on the retry. */
  if (pmProg && pmProg.id === name) pmProgFail(why || '');
  if (!pmProgShows(name)) toast(T('gui.plug.auth_fail_rm', { name: it ? it.name : name }));
  rpc.call('plug.remove', { name }).then(loadExt).catch(() => {}).then(pmRedraw);
}

function pmRemove(name, label) {
  pmBusy = name; pmRedraw();
  rpc.call('plug.remove', { name })
    .then(() => {
      const it = pmItems.find((x) => x.id === name);
      if (it) it.installed = false;
      pmPending.delete(name);
      delete pmAuthWait[name];
      // Cancel/uninstall ends the install story outright.
      if (pmProg && pmProg.id === name) pmProg = null;
      toast(T('gui.caps.removed_x', { name: label || name }));
      pmCloseDetail();
      return loadExt();
    })
    .catch((e) => toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e })))
    .finally(() => { pmBusy = null; pmRedraw(); });
}

function pmAuth(name) {
  pmBusy = name; pmRedraw();
  rpc.call('plug.auth', { name })
    .then((r) => {
      const row = pmMcpRows().find((p) => p.m.name === name);
      if (r.mcp && row) Object.assign(row.m, r.mcp);
    })
    .catch((e) => toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e })))
    .finally(() => { pmBusy = null; pmRedraw(); });
}

/* ── events from the gateway ─────────────────────────────────────── */

/* The gateway announces a newer build the moment its periodic check finds one,
   so a tab that has been open for days hears about it without a reload. Same
   banner as the boot-time system.version path. */
rpc.notify['system.update_available'] = (p) => {
  if (p && p.latest_version) showUpNote('ver', p.latest_version);
};

let pmExtSoon = null;
rpc.notify['mcp.status'] = (p) => {
  // A settled status for the busy server means nothing is processing any
  // more — belt-and-braces against a busy flag that outlives its install.
  const inFlight = pmBusy === p.name;
  if (inFlight && p.state !== 'connecting') pmBusy = null;
  if (pmProg && pmProg.id === p.name) {
    if (p.state === 'connecting') pmProgStep(1);
    else if (p.state === 'connected') pmProgDone(p.tool_count || 0);
  }
  if (pmPending.has(p.name)) {
    if (p.state === 'connected') {
      pmPending.delete(p.name);
      const it = pmItems.find((x) => x.id === p.name);
      if (it) it.installed = true;
      // The install RPC reports its own outcome; only a later async
      // connect (the OAuth round-trip) announces from here.
      if (!inFlight && !pmProgShows(p.name)) {
        toast(T('gui.plug.installed_ok', { name: it ? it.name : p.name, n: p.tool_count || 0 }));
      }
    } else if ((p.state === 'auth_required' || p.state === 'error') && !inFlight) {
      // While the install RPC is in flight the backend rolls back itself
      // and the call rejects; acting here too would remove twice.
      pmPendingFail(p.name, p.error || '');
      return;
    }
  }
  const row = pmMcpRows().find((x) => x.m.name === p.name);
  if (row) Object.assign(row.m, p);
  else {
    // Unknown server (fresh install, or events arriving before the first
    // ext.list) — coalesce the reload; startup syncs fire one event per server.
    clearTimeout(pmExtSoon);
    pmExtSoon = setTimeout(() => loadExt().then(pmRedraw).catch(() => {}), 250);
  }
  if (p.state !== 'connecting') delete pmAuthWait[p.name];
  pmRedraw();
};

/* Long-term memory stopped writing, or started again. Broadcast like the other
   per-server events, because a backend that cannot store is not part of any one
   conversation's turn. */
rpc.notify['memory.health'] = (p) => {
  memFault = p && p.ok === false ? (p.error || T('gui.mem.down')) : null;
  drawBanner();
};

rpc.notify['oauth.pending'] = (p) => {
  pmAuthWait[p.server] = p.url;
  /* The window has an end, so the page shows one. Without it the sheet sits on
     "waiting for authorization" with nothing to distinguish a flow still worth
     finishing from one that expired minutes ago -- and the reader only learns
     which it was when the install disappears. */
  if (p.expires_in) pmAuthEnd[p.server] = Date.now() + Number(p.expires_in) * 1000;
  pmAuthTick(true);
  if (pmProg && pmProg.id === p.server) pmProgStep(2);
  /* A background connect found this server unauthorized; nobody asked for it,
     and the host deliberately did not open a browser. Say so where the reader
     can act on it, rather than reporting a page that never opened. The rows
     already grow a "reopen" button off pmAuthWait, which is now the way in. */
  if (!pmProgShows(p.server)) {
    toast(T(p.interactive === false ? 'gui.plug.auth_needed' : 'gui.plug.auth_opened',
      { host: pmHost(p.url), name: p.server }));
  }
  pmRedraw();
};

rpc.notify['oauth.done'] = (p) => {
  delete pmAuthWait[p.server];
  delete pmAuthEnd[p.server];
  pmAuthTick(false);
  if (p.ok && pmProg && pmProg.id === p.server) pmProgStep(3);
  if (!p.ok) {
    const why = p.error === 'timeout' ? T('gui.plug.auth_expired') : (p.error || '');
    // A failed authorization on a pending install rolls the install back;
    // on an already-installed server (re-auth) it just reports. While the
    // install RPC is in flight the backend rolls back itself.
    if (pmPending.has(p.server)) {
      if (pmBusy !== p.server) pmPendingFail(p.server, why);
      return;
    }
    toast(why || T('gui.plug.auth_fail', { name: p.server }));
  }
  pmRedraw();
};

/* ── market view ─────────────────────────────────────────────────── */

function pmMarketCard(it) {
  const c = mk('div', 'hubcard pmcard' + (it.installed ? ' dim' : ''));
  const top = mk('div', 'top');
  const nmwrap = mk('div', 'pmhead');
  nmwrap.appendChild(pmTile(it.name));
  const id = mk('div', 'pmid');
  const nm = mk('div', 'pmnm');
  nm.appendChild(mk('span', null, it.name));
  if (it.verified) { const v = mk('span'); v.innerHTML = PM_VFD; v.title = T('gui.plug.verified'); nm.appendChild(v); }
  id.appendChild(nm);
  if (it.publisher) id.appendChild(mk('div', 'pmpub', it.publisher));
  nmwrap.appendChild(id);
  top.appendChild(nmwrap);
  c.appendChild(top);
  c.appendChild(mk('p', 'one', it.summary || ''));

  const foot = mk('div', 'foot');
  const cnt = mk('span', 'pmcnt');
  const bits = [];
  if (it.tool_preview_count) bits.push(T('gui.plug.tools_n', { n: it.tool_preview_count }));
  if (it.skill_count) bits.push(T('gui.plug.skills_n', { n: it.skill_count }));
  cnt.textContent = bits.join(' · ');
  if (it.risk_tier === 2) cnt.appendChild(mk('span', 'pmsign gold', T('gui.plug.local_run')));
  if (it.risk_tier >= 3) cnt.appendChild(mk('span', 'pmsign faint', T('gui.plug.needs_restart')));
  foot.appendChild(cnt);

  const act = mk('div', 'act');
  if (pmBusy === it.id) act.appendChild(mk('span', 'pnote', T('gui.hub.working')));
  else if (pmPending.has(it.id)) act.appendChild(mk('span', 'pnote', T('gui.plug.st_wait')));
  else if (it.installed) act.appendChild(mk('span', 'okpill', T('gui.hub.installed')));
  else {
    // Two hit zones: the button installs right away (or lands on the form
    // when one is needed), the card opens the sheet.
    const b = mk('button', 'mini gold', T('gui.plug.install'));
    b.onclick = (e) => { e.stopPropagation(); pmQuickInstall(it); };
    act.appendChild(b);
  }
  foot.appendChild(act);
  c.appendChild(foot);
  c.tabIndex = 0;
  c.setAttribute('role', 'button');
  c.onclick = () => pmOpenDetail('market', it.id);
  c.onkeydown = (e) => { if (e.key === 'Enter') pmOpenDetail('market', it.id); };
  return c;
}

function drawPlugMarket(box) {
  const chips = mk('div', 'pmchips');
  const chip = (label, val) => {
    const b = mk('button', 'pill', label);
    b.setAttribute('aria-pressed', String(pmCat === val));
    b.onclick = () => { pmCat = val; pmSearch(); };
    chips.appendChild(b);
  };
  chip(T('gui.filter.all'), '');
  pmCats.forEach((cat) => chip(T('gui.plug.cat_' + cat, null, cat), cat));
  box.appendChild(chips);

  if (pmErr) {
    const wrap = mk('div', 'empty-note');
    wrap.append(mk('div', null, T('gui.plug.market_down')), document.createElement('br'));
    const retry = mk('button', 'mini ghost', T('gui.plug.retry'));
    retry.onclick = pmSearch;
    wrap.appendChild(retry);
    box.appendChild(wrap);
    return;
  }
  if (pmState === 'loading' && !pmItems.length) { box.appendChild(mk('div', 'empty-note', T('gui.hub.reading'))); return; }
  if (!pmItems.length) { box.appendChild(mk('div', 'empty-note', T('gui.plug.none_found', { q: pmQuery }))); return; }

  const g = mk('div', 'hubgrid');
  pmItems.forEach((it) => g.appendChild(pmMarketCard(it)));
  box.appendChild(g);
}

/* ── installed view ──────────────────────────────────────────────── */

function pmInstMcpCard(row) {
  const m = row.m;
  const st = pmStatus(m);
  const c = mk('div', 'hubcard pmcard' + (st.cls === 'bad' ? ' pmbad' : ''));
  const top = mk('div', 'top');
  const nmwrap = mk('div', 'pmhead');
  nmwrap.appendChild(pmTile(m.name));
  const nm = mk('div', 'pmnm');
  nm.appendChild(mk('span', null, m.name));
  nmwrap.appendChild(nm);
  top.appendChild(nmwrap);
  const sw = mk('button', 'swi');
  sw.setAttribute('role', 'switch');
  sw.setAttribute('aria-checked', String(m.enabled));
  sw.setAttribute('aria-label', T('gui.caps.toggle_aria', { name: m.name }));
  sw.onclick = (e) => { e.stopPropagation(); pmToggle(m.name, !m.enabled); };
  top.appendChild(sw);
  c.appendChild(top);

  const sub = mk('p', 'one');
  const bits = [pmMarketIds.has(m.name) ? T('gui.plug.from_market') : T('gui.plug.manual'), m.transport];
  if (m.tool_count) bits.push(T('gui.plug.tools_n', { n: m.tool_count }));
  sub.textContent = bits.join(' · ');
  c.appendChild(sub);

  const foot = mk('div', 'foot');
  const stEl = mk('span', 'pmst ' + st.cls);
  stEl.append(mk('i', 'pmdot'), mk('span', null, T(st.k)));
  if (st.cls === 'bad' && m.error) stEl.title = m.error;
  foot.appendChild(stEl);
  const act = mk('div', 'act');
  // The card keeps only the high-frequency verbs (the enable switch and one
  // context action); 卸载 lives in the detail's manage section.
  if (pmAuthWait[m.name]) {
    const re = mk('button', 'mini', T('gui.plug.reopen'));
    re.onclick = (e) => { e.stopPropagation(); window.open(pmAuthWait[m.name], '_blank'); };
    act.appendChild(re);
  } else if (m.state === 'auth_required' || m.state === 'error') {
    const fix = mk('button', 'mini', T(m.state === 'auth_required' ? 'gui.plug.reauth' : 'gui.caps.repair'));
    fix.onclick = (e) => { e.stopPropagation(); pmAuth(m.name); };
    act.appendChild(fix);
  } else if (m.enabled) {
    const use = mk('button', 'mini gold', T('gui.hub.use'));
    use.onclick = (e) => { e.stopPropagation(); useInTask('gui.plug.use_prompt', m.name); };
    act.appendChild(use);
  }
  foot.appendChild(act);
  c.appendChild(foot);
  c.tabIndex = 0;
  c.setAttribute('role', 'button');
  c.onclick = () => pmOpenDetail('inst', m.name);
  c.onkeydown = (e) => { if (e.key === 'Enter') pmOpenDetail('inst', m.name); };
  return c;
}

function pmInstPyCard(row) {
  const c = mk('div', 'hubcard pmcard');
  const top = mk('div', 'top');
  const nmwrap = mk('div', 'pmhead');
  nmwrap.appendChild(pmTile(row.name));
  const nm = mk('div', 'pmnm');
  nm.appendChild(mk('span', null, row.name));
  nmwrap.appendChild(nm);
  top.appendChild(nmwrap);
  const sw = mk('button', 'swi');
  sw.setAttribute('role', 'switch');
  sw.setAttribute('aria-checked', String(row.state === 'on'));
  sw.setAttribute('aria-label', T('gui.caps.toggle_aria', { name: row.name }));
  sw.onclick = (e) => { e.stopPropagation(); row.state = row.state === 'on' ? 'off' : 'on'; pmRedraw(); };
  top.appendChild(sw);
  c.appendChild(top);
  c.appendChild(mk('p', 'one', [row.src, row.ver !== '—' ? 'v' + row.ver : ''].filter(Boolean).join(' · ')));
  const foot = mk('div', 'foot');
  const st = row.state === 'on' ? PM_ST.connected : PM_ST.off;
  const stEl = mk('span', 'pmst ' + st.cls);
  stEl.append(mk('i', 'pmdot'), mk('span', null, T(st.k)));
  foot.appendChild(stEl);
  const cnt = mk('span', 'pmcnt');
  cnt.appendChild(mk('span', 'pmsign faint', T('gui.plug.needs_restart')));
  foot.appendChild(cnt);
  c.appendChild(foot);
  return c;
}

function drawPlugInstalled(box) {
  const back = mk('div', 'pmback');
  const b = mk('button', 'mini ghost');
  b.textContent = '← ' + T('gui.plug.back');
  b.onclick = () => { plugView = 'market'; pmCloseDetail(); drawCaps(); };
  back.append(b, mk('b', null, T('gui.plug.installed_title')));
  box.appendChild(back);

  const section = (label, cards) => {
    if (!cards.length) return;
    const s = mk('div', 'csec');
    const hd = mk('div', 'hd');
    hd.append(mk('b', null, label), mk('span', 'n', String(cards.length)));
    s.appendChild(hd);
    const g = mk('div', 'hubgrid');
    cards.forEach((el) => g.appendChild(el));
    s.appendChild(g);
    box.appendChild(s);
  };

  // A pending-auth install is not installed yet: it stays a market-side
  // waiting card until its authentication settles.
  const py = pmPyRows();
  const mcps = pmMcpRows().filter((p) => !pmPending.has(p.m.name));
  section(T('gui.plug.grp_builtin'), py.map(pmInstPyCard));
  section(T('gui.plug.grp_market'), mcps.map(pmInstMcpCard));

  if (!py.length && !mcps.length) box.appendChild(mk('div', 'empty-note', T('gui.plug.empty_installed')));
}

/* ── detail drawer (market entries + installed servers) ──────────── */

/* A running install keeps its pmProg across a close so the sheet can be
   reentered from the card; a settled one is acknowledged by closing. */
function pmCloseDetail() {
  pmDrawer = null;
  if (pmProg && pmProg.state !== 'run') pmProg = null;
  pmForm = false; pmConfirm = false;
  closeDetail();
}

function pmDrawerPerms(entry) {
  const mcp = pmEntryMcp(entry);
  const box = mk('div', 'pmperm');
  const li = (txt) => { const r = mk('div', 'row'); r.append(mk('i', 'pmdot'), mk('span', null, txt)); box.appendChild(r); };
  if (mcp) {
    const mode = (mcp.auth || {}).mode || 'none';
    const local = !!(mcp.connection || {}).command;
    if (mode === 'oauth') li(T('gui.plug.perm_oauth', { host: pmHost(mcp.connection.url) }));
    if (mode === 'apikey') li(T('gui.plug.perm_key'));
    if (local) li(T('gui.plug.perm_local'));
    // Where the data goes is already stated by the oauth line for oauth servers.
    else if (mode !== 'oauth') li(T('gui.plug.perm_net', { host: pmHost((mcp.connection || {}).url) }));
  }
  const skills = (entry.contributes || []).filter((c) => c.kind === 'skill').length;
  if (skills) li(T('gui.plug.perm_skill', { n: skills }));
  return box;
}

function pmInstallControls(entry, host) {
  const mcp = pmEntryMcp(entry);
  const mode = mcp ? (mcp.auth || {}).mode || 'none' : 'none';
  const stdio = !!(mcp && (mcp.connection || {}).command);
  const box = mk('div');

  const start = () => {
    if (mode === 'apikey' && !pmForm) { pmForm = true; pmOpenDetail('market', entry.id, true); return; }
    if (stdio && !pmConfirm && mode !== 'apikey') { pmConfirm = true; pmOpenDetail('market', entry.id, true); return; }
    pmInstall(entry, {});
  };

  if (pmForm && mode === 'apikey') {
    const fields = (mcp.auth || {}).fields || [];
    const inputs = new Map();
    fields.forEach((f) => {
      const lab = mk('label', 'pmlab');
      lab.append(mk('span', null, pmText(f.label) || f.key));
      if (f.help_url) {
        const a = mk('a', 'pmhelp', T('gui.plug.how_get'));
        a.href = f.help_url; a.target = '_blank'; a.rel = 'noreferrer';
        lab.appendChild(a);
      }
      box.appendChild(lab);
      const inp = document.createElement('input');
      inp.type = f.secret ? 'password' : 'text';
      inp.className = 'pminp';
      inp.autocomplete = 'off';
      inputs.set(f.key, inp);
      box.appendChild(inp);
    });
    if (stdio) {
      // The key form and the run-locally warning show together: one scroll,
      // one decision — the warning block owns the single action row.
      box.appendChild(pmStdioWarn(entry, inputs));
      return box;
    }
    const row = mk('div', 'pmrow');
    const cancel = mk('button', 'mini ghost', T('gui.plug.cancel'));
    cancel.onclick = () => { pmForm = false; pmOpenDetail('market', entry.id, true); };
    const go = mk('button', 'mini gold', T('gui.plug.connect'));
    go.onclick = () => {
      const form = {};
      let missing = false;
      inputs.forEach((inp, k) => { form[k] = inp.value.trim(); if (!form[k]) missing = true; });
      if (missing) { toast(T('gui.plug.need_key')); return; }
      pmInstall(entry, form);
    };
    row.append(cancel, go);
    box.appendChild(row);
    return box;
  }

  if (pmConfirm && stdio) {
    box.appendChild(pmStdioWarn(entry, null));
    return box;
  }

  const b = mk('button', 'mini gold', pmBusy === entry.id ? T('gui.hub.working') : T('gui.plug.install'));
  b.disabled = pmBusy === entry.id;
  b.onclick = start;
  box.appendChild(b);
  if (host) box.appendChild(mk('div', 'pnote', T('gui.plug.oauth_note', { host })));
  return box;
}

function pmStdioWarn(entry, inputs) {
  const mcp = pmEntryMcp(entry);
  const cmd = [mcp.connection.command, ...(mcp.connection.args || [])].join(' ');
  const w = mk('div', 'pmwarn');
  w.appendChild(mk('div', 'wt', T('gui.plug.stdio_warn')));
  w.appendChild(mk('pre', 'pmcmd', cmd));
  w.appendChild(mk('div', 'pnote', T('gui.plug.stdio_trust')));
  const row = mk('div', 'pmrow');
  const cancel = mk('button', 'mini ghost', T('gui.plug.cancel'));
  cancel.onclick = () => { pmConfirm = false; pmForm = false; pmOpenDetail('market', entry.id, true); };
  const go = mk('button', 'mini', T('gui.plug.still_install'));
  go.onclick = () => {
    const form = {};
    if (inputs) {
      let missing = false;
      inputs.forEach((inp, k) => { form[k] = inp.value.trim(); if (!form[k]) missing = true; });
      if (missing) { toast(T('gui.plug.need_key')); return; }
    }
    pmInstall(entry, form);
  };
  row.append(cancel, go);
  w.appendChild(row);
  return w;
}

function pmOpenDetail(kind, id, keep) {
  // An installed market plugin opens the same detail the market shows —
  // full package content — with connection state and manage actions layered
  // in. Only manually-configured servers (no catalog entry) fall through to
  // the slim config drawer below.
  // While this plugin's install is still running, every entry point lands
  // on the progress sheet -- closing it must never strand the install.
  if (kind !== 'progress' && pmProg && pmProg.id === id && pmProg.state === 'run') kind = 'progress';
  if (kind === 'inst' && pmMarketIds.has(id)) kind = 'market';
  pmDrawer = { kind, id };
  if (!keep) { pmForm = false; pmConfirm = false; }
  if ($('#capsPage').dataset.open !== 'true') openCaps('plugin');
  const body = $('#dBody'); body.innerHTML = '';

  if (kind === 'progress') {
    $('#dTitle').textContent = '';
    pmProgRender(body);
    $('#detail').dataset.open = 'true';
    return;
  }

  if (kind === 'market') {
    const it = pmItems.find((x) => x.id === id);
    rpc.call('plughub.detail', { id }).then((r) => {
      if (!pmDrawer || pmDrawer.id !== id) return;
      const entry = r.item;
      $('#dTitle').textContent = '';
      body.innerHTML = '';

      const mcp = pmEntryMcp(entry);
      const mode = mcp ? (mcp.auth || {}).mode || 'none' : 'none';
      const stdio = !!(mcp && (mcp.connection || {}).command);
      // A pending-auth install never reads as installed: the ledger entry
      // exists on disk, but the install completes (or rolls back) with auth.
      const pending = pmPending.has(entry.id);
      const installed = !pending && (r.installed || (it && it.installed));
      const lrow = (installed || pending) ? pmMcpRows().find((p) => p.m.name === entry.id) : null;
      const lm = lrow && lrow.m;

      const head = mk('div', 'pmdhead');
      head.appendChild(pmTile(pmText(entry.name)));
      const meta = mk('div', 'pmdmeta');
      const l1 = mk('div', 'l1');
      l1.appendChild(mk('b', null, pmText(entry.name)));
      if ((entry.publisher || {}).verified) { const v = mk('span'); v.innerHTML = PM_VFD; l1.appendChild(v); }
      meta.appendChild(l1);
      const bits = [(entry.publisher || {}).name, 'v' + entry.version].filter(Boolean);
      const l2 = mk('div', 'l2', bits.join(' · ') + (entry.homepage ? ' · ' : ''));
      if (entry.homepage) {
        const a = mk('a', null, T('gui.plug.homepage') + ' ↗');
        a.href = entry.homepage; a.target = '_blank'; a.rel = 'noreferrer';
        l2.appendChild(a);
      }
      meta.appendChild(l2);
      head.appendChild(meta);
      // The decisive control rides the identity row; forms unfold just below.
      const hact = mk('div', 'dact');
      if (pmBusy === entry.id) hact.appendChild(mk('span', 'pnote', T('gui.hub.working')));
      else if (pending) {
        if (pmAuthWait[entry.id]) {
          const re = mk('button', 'mini', T('gui.plug.reopen'));
          re.onclick = () => window.open(pmAuthWait[entry.id], '_blank');
          hact.appendChild(re);
        }
        const c = mk('button', 'mini ghost', T('gui.plug.cancel'));
        c.onclick = () => { pmPending.delete(entry.id); pmRemove(entry.id, pmText(entry.name)); };
        hact.appendChild(c);
      }
      else if (installed) {
        if (pmAuthWait[entry.id]) {
          const re = mk('button', 'mini', T('gui.plug.reopen'));
          re.onclick = () => window.open(pmAuthWait[entry.id], '_blank');
          hact.appendChild(re);
        } else if (lm && (lm.state === 'auth_required' || lm.state === 'error')) {
          const fix = mk('button', 'mini', T(lm.state === 'auth_required' ? 'gui.plug.reauth' : 'gui.caps.repair'));
          fix.onclick = () => pmAuth(entry.id);
          hact.appendChild(fix);
        } else if (!lm || lm.enabled) {
          const use = mk('button', 'mini gold', T('gui.hub.use'));
          use.onclick = () => useInTask('gui.plug.use_prompt', pmText(entry.name));
          hact.appendChild(use);
        } else hact.appendChild(mk('span', 'okpill', T('gui.hub.installed')));
      } else if (!pmForm && !pmConfirm) {
        const b = mk('button', 'mini gold', T('gui.plug.install'));
        b.onclick = () => {
          if (mode === 'apikey') { pmForm = true; pmOpenDetail('market', entry.id, true); return; }
          if (stdio) { pmConfirm = true; pmOpenDetail('market', entry.id, true); return; }
          pmInstall(entry, {});
        };
        hact.appendChild(b);
      }
      head.appendChild(hact);
      body.appendChild(head);

      const sec = (label, el) => {
        const s = mk('div', 'pmsec');
        s.appendChild(mk('div', 'cap', label));
        s.appendChild(el);
        body.appendChild(s);
      };
      if (pending) {
        body.appendChild(mk('div', 'pnote', T('gui.plug.wait_auth', { name: pmText(entry.name) })));
      } else if (!installed && (pmForm || pmConfirm)) {
        sec(T(pmForm ? 'gui.plug.sec_cred' : 'gui.plug.sec_confirm'),
          pmInstallControls(entry, mode === 'oauth' ? pmHost(mcp.connection.url) : ''));
      } else if (!installed && mode === 'oauth') {
        body.appendChild(mk('div', 'pnote', T('gui.plug.oauth_note', { host: pmHost(mcp.connection.url) })));
      }
      if (lm) {
        const st = pmStatus(lm);
        const conn = mk('div');
        const stEl = mk('span', 'pmst ' + st.cls);
        stEl.append(mk('i', 'pmdot'), mk('span', null, T(st.k)));
        conn.appendChild(stEl);
        if (lm.error && st.cls === 'bad') conn.appendChild(mk('div', 'perr', lm.error));
        if (lm.tool_count) conn.appendChild(mk('div', 'pnote', T('gui.plug.tools_n', { n: lm.tool_count })));
        sec(T('gui.plug.sec_conn'), conn);
      }
      sec(T('gui.plug.sec_perm'), pmDrawerPerms(entry));
      const desc = mk('div', 'pmdesc', pmText(entry.description) || pmText(entry.summary));
      sec(T('gui.plug.sec_about'), desc);
      // A plugin is a package: one section lists everything installing it
      // adds, each piece with its own detail nested under it (an MCP server
      // owns its tool list; a skill shows its hub id). No sibling sections.
      // A plugin is a package of contributions (today: MCP servers and
      // skills; the catalog is currently MCP-only). Each piece renders as
      // name line -> connection meta -> what it brings (an MCP server
      // contributes its tools; that is the surface raven consumes).
      const cts = entry.contributes || [];
      if (cts.length) {
        const box = mk('div');
        cts.forEach((co, i) => {
          const piece = mk('div', 'pmperm');
          if (i) piece.style.marginTop = '14px';
          const line = mk('div', 'row');
          line.appendChild(mk('span', 'kd', co.kind === 'skill' ? T('gui.plug.ct_skill') : 'MCP'));
          line.appendChild(mk('b', null,
            co.kind === 'mcp' ? entry.id : (co.name || co.skillhub_id || entry.id)));
          piece.appendChild(line);
          if (co.kind === 'mcp') {
            const conn = co.connection || {};
            const meta = mk('div', 'pnote',
              (conn.command ? ['stdio', [conn.command].concat(conn.args || []).join(' ').slice(0, 60)]
                : [conn.type || 'http', pmHost(conn.url)]).filter(Boolean).join(' · '));
            meta.style.margin = '4px 0 0 2px';
            piece.appendChild(meta);
            const tools = co.tools_preview || [];
            if (tools.length) {
              const cap = mk('div', 'cap', T('gui.plug.tools_n', { n: tools.length }));
              cap.style.margin = '8px 0 4px 2px';
              piece.appendChild(cap);
              const ul = mk('div');
              ul.style.cssText = 'margin: 0 0 0 2px';
              tools.forEach((t) => ul.appendChild(mk('div', 'pmtool', t)));
              piece.appendChild(ul);
            }
          }
          box.appendChild(piece);
        });
        sec(T('gui.plug.sec_contents'), box);
      }
      if (installed && pmBusy !== entry.id) {
        const man = mk('div');
        man.appendChild(mk('div', 'pnote', T('gui.plug.uninstall_note')));
        const rm = mk('button', 'mini ghost', T('gui.plug.uninstall'));
        rm.style.marginTop = '8px';
        pmArm(rm, () => pmRemove(entry.id, pmText(entry.name)));
        man.appendChild(rm);
        sec(T('gui.plug.sec_manage'), man);
      }
      $('#detail').dataset.open = 'true';
    }).catch((e) => { toast(T('gui.plug.op_failed', { err: e.message || e })); });
    $('#dTitle').textContent = '';
    $('#detail').dataset.open = 'true';
    body.appendChild(mk('div', 'pnote', T('gui.hub.reading')));
    return;
  }

  // installed server drawer
  const row = pmMcpRows().find((p) => p.m.name === id);
  if (!row) return;
  const m = row.m;
  $('#dTitle').textContent = '';
  const st = pmStatus(m);
  const head = mk('div', 'pmdhead');
  head.appendChild(pmTile(m.name));
  const meta = mk('div', 'pmdmeta');
  const l1 = mk('div', 'l1');
  l1.appendChild(mk('b', null, m.name));
  meta.appendChild(l1);
  meta.appendChild(mk('div', 'l2', [pmMarketIds.has(m.name) ? T('gui.plug.from_market') : T('gui.plug.manual'), m.transport].join(' · ')));
  head.appendChild(meta);
  const hact = mk('div', 'dact');
  if (pmAuthWait[m.name]) {
    const re = mk('button', 'mini', T('gui.plug.reopen'));
    re.onclick = () => window.open(pmAuthWait[m.name], '_blank');
    hact.appendChild(re);
  } else if (m.state === 'auth_required' || m.state === 'error') {
    const fix = mk('button', 'mini', T(m.state === 'auth_required' ? 'gui.plug.reauth' : 'gui.caps.repair'));
    fix.onclick = () => pmAuth(m.name);
    hact.appendChild(fix);
  } else if (m.enabled) {
    const use = mk('button', 'mini gold', T('gui.hub.use'));
    use.onclick = () => useInTask('gui.plug.use_prompt', m.name);
    hact.appendChild(use);
  }
  head.appendChild(hact);
  body.appendChild(head);

  const conn = mk('div');
  const stEl = mk('span', 'pmst ' + st.cls);
  stEl.append(mk('i', 'pmdot'), mk('span', null, T(st.k)));
  conn.appendChild(stEl);
  if (m.error && st.cls === 'bad') conn.appendChild(mk('div', 'perr', m.error));
  if (m.tool_count) conn.appendChild(mk('div', 'pnote', T('gui.plug.tools_n', { n: m.tool_count })));
  const s1 = mk('div', 'pmsec');
  s1.appendChild(mk('div', 'cap', T('gui.plug.sec_conn')));
  s1.appendChild(conn);
  body.appendChild(s1);

  const man = mk('div');
  man.appendChild(mk('div', 'pnote', T('gui.plug.uninstall_note')));
  const rm = mk('button', 'mini ghost', T('gui.plug.uninstall'));
  rm.style.marginTop = '8px';
  pmArm(rm, () => pmRemove(m.name, m.name));
  man.appendChild(rm);
  const s2 = mk('div', 'pmsec');
  s2.appendChild(mk('div', 'cap', T('gui.plug.sec_manage')));
  s2.appendChild(man);
  body.appendChild(s2);
  $('#detail').dataset.open = 'true';
}

/* ── page assembly ───────────────────────────────────────────────── */

/* The 已安装 entry point rides in the filter bar, like the skills view
   switch — created once, shown only on the plugin tab. */
const pmInstBtn = (() => {
  const b = mk('button', 'pminstbtn');
  b.onclick = () => { plugView = plugView === 'installed' ? 'market' : 'installed'; pmCloseDetail(); drawCaps(); };
  $('.cbar').appendChild(b);
  return { el: b, sync() {
    b.hidden = extTab !== 'plugin' || plugView === 'installed';
    const n = PLUGINS.filter((p) => !p.m || !pmPending.has(p.m.name)).length;
    const attn = attnCount();
    b.innerHTML = '';
    b.append(mk('span', null, T('gui.plug.installed_n', { n })));
    if (attn) b.appendChild(mk('span', 'pmbdg', String(attn)));
  } };
})();

{
  /* The hero sits above the search bar, so it lives outside #capsBody --
     one node, repopulated on every draw for whichever view is up. */
  const pageHero = () => {
    let h = $('#pageHero');
    if (!h) {
      h = mk('div', 'pmhero');
      h.id = 'pageHero';
      const bar = document.querySelector('#capsPage .cbar');
      bar.parentNode.insertBefore(h, bar);
    }
    return h;
  };
  /* Title only — no tagline under it; that copy read as marketing, not UI. */
  const syncHero = () => {
    const h = pageHero();
    h.innerHTML = '';
    let title = '';
    if (extTab === 'plugin' && plugView === 'market') title = T('gui.plug.hero');
    else if (extTab === 'skill' && skView === 'market') title = T('gui.hub.hero');
    h.hidden = !title;
    if (title) h.appendChild(mk('h3', null, title));
  };

  const prevDrawCaps = drawCaps;
  drawCaps = function () {
    if (extTab !== 'plugin') {
      // Undo this tab's chrome before handing back: the plugin view hid the
      // status pills, and the skill view re-hides them for itself.
      $('#cKind').hidden = false;
      $('.cbar').style.display = '';
      prevDrawCaps();
      pmInstBtn.sync();
      syncHero();
      return;
    }

    const box = $('#capsBody'); box.innerHTML = '';
    const title = T('gui.tab.plugins');
    $('#capsTitle').textContent = plugView === 'installed' ? T('gui.plug.installed_title') : title;
    $('#capsPage').setAttribute('aria-label', title);
    $('#cKind').hidden = true;
    $('#advAdd').hidden = plugView !== 'market';
    $('#cq').placeholder = T('gui.plug.search_ph');
    $('.cbar').style.display = plugView === 'installed' ? 'none' : '';
    skInstBtn.sync();  // the skills 已安装 button must not linger on this tab

    if (plugView === 'installed') drawPlugInstalled(box);
    else drawPlugMarket(box);
    pmInstBtn.sync();
    syncHero();
    drawCapsBadge();
  };

  const prevShowPage = showPage;
  showPage = function (id) {
    prevShowPage(id);
    if (id !== 'capsPage') { pmDrawer = null; $('.cbar').style.display = ''; }
  };

  const prevExtSet = extSet;
  extSet = function (tab) {
    const was = extTab;
    prevExtSet(tab);
    if (extTab !== was) { plugView = 'market'; pmDrawer = null; pmQuery = ''; pmCat = ''; $('.cbar').style.display = ''; }
  };

  const prevInput = $('#cq').oninput;
  $('#cq').oninput = () => {
    if (extTab === 'plugin') { pmQuery = $('#cq').value.trim(); pmSearchSoon(); return; }
    if (prevInput) prevInput();
  };

  const prevOpenCaps = openCaps;
  openCaps = async function (tab) {
    await prevOpenCaps(tab);
    if (extTab === 'plugin' && pmState === 'idle') pmSearch();
  };

  const prevCloseDetail = closeDetail;
  // Dismissing the sheet mid-install keeps pmProg so the card reopens the
  // progress view; a settled install is acknowledged by the close.
  closeDetail = function () {
    pmDrawer = null;
    if (pmProg && pmProg.state !== 'run') pmProg = null;
    prevCloseDetail();
  };
  // The X button captured the original closeDetail reference at base load.
  $('#dClose').onclick = () => closeDetail();
}

/* -- data & memory ----------------------------------------------------
   Four EverOS memory kinds behind one page: a stat band that doubles as
   the kind switch, a semantic search box, and a detail drawer carrying
   the one mutation memory supports today (delete, two-click armed).
   Reads go through memory.stats / memory.list; the page never talks to
   EverOS directly. */

const MEM_KINDS = [
  { kind: 'episode', tab: 'gui.mem.tab_episode', hint: 'gui.mem.hint_episode', stat: 'episodes' },
  { kind: 'profile', tab: 'gui.mem.tab_profile', hint: 'gui.mem.hint_profile', stat: 'profiles' },
  { kind: 'agent_case', tab: 'gui.mem.tab_case', hint: 'gui.mem.hint_case', stat: 'agent_cases' },
  { kind: 'agent_skill', tab: 'gui.mem.tab_skill', hint: 'gui.mem.hint_skill', stat: 'agent_skills' },
];
const MEM_PAGE_SIZE = 20;
let memKind = 'episode', memPageNo = 1, memQ = '', memItems = [], memTotal = 0;
let memStats = null, memState = 'idle', memErr = '', memDebounce = null, memBusy = false;

function memWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const two = (n) => String(n).padStart(2, '0');
  return LANG === 'zh'
    ? `${d.getMonth() + 1}月${d.getDate()}日 ${two(d.getHours())}:${two(d.getMinutes())}`
    : `${d.toLocaleString('en-US', { month: 'short' })} ${d.getDate()} ${two(d.getHours())}:${two(d.getMinutes())}`;
}

const memPct = (v) => (v == null ? '' : v <= 1 ? `${Math.round(v * 100)}%` : String(v));

function memMeter(v) {
  const m = mk('span', 'mmeter');
  const bar = mk('i');
  bar.style.width = `${Math.round(Math.min(1, Math.max(0, Number(v) || 0)) * 100)}%`;
  m.appendChild(bar);
  return m;
}

async function memLoadStats() {
  try { memStats = await rpc.call('memory.stats', {}); } catch { memStats = null; }
}

async function memLoad() {
  memState = 'loading';
  memErr = '';
  try {
    const r = await rpc.call('memory.list', {
      kind: memKind, page: memPageNo, page_size: MEM_PAGE_SIZE, q: memQ || null,
    });
    memItems = r.items || [];
    memTotal = r.total || 0;
    memState = 'ready';
  } catch (e) {
    memState = 'error';
    memErr = (e.data && e.data.detail) || e.message || String(e);
  }
  drawMem();
}

openMem = async function () {
  showPage('memPage');
  drawMem();
  memLoadStats().then(drawMem);
  memLoad();
};

function memArm(btn, fn) {
  let armed = false;
  const label = btn.textContent;
  btn.onclick = (e) => {
    e.stopPropagation();
    if (!armed) {
      armed = true;
      btn.textContent = T('gui.mem.confirm_del');
      btn.classList.add('danger');
      setTimeout(() => { armed = false; btn.textContent = label; btn.classList.remove('danger'); }, 4000);
      return;
    }
    fn();
  };
}

function memDelete(it) {
  if (memBusy) return;
  memBusy = true;
  rpc.call('memory.delete', { kind: it.kind, id: it.id })
    .then(() => {
      toast(T('gui.mem.deleted'));
      closeDetail();
      memLoadStats().then(drawMem);
      return memLoad();
    })
    .catch((e) => toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e })))
    .finally(() => { memBusy = false; });
}

function memOpenDetail(it) {
  const body = $('#dBody'); body.innerHTML = '';
  const kindDef = MEM_KINDS.find((k) => k.kind === it.kind);
  const title = it.subject || T(kindDef.tab);
  $('#dTitle').textContent = '';

  // Same drawer opener as the plugin / skill pages: tile, bold name, meta line.
  const head = mk('div', 'pmdhead');
  head.appendChild(pmTile(title));
  const hm = mk('div', 'pmdmeta');
  const l1 = mk('div', 'l1');
  l1.appendChild(mk('b', null, title));
  hm.appendChild(l1);
  hm.appendChild(mk('div', 'l2', [T(kindDef.tab), memWhen(it.timestamp)].filter(Boolean).join(' · ')));
  head.appendChild(hm);
  body.appendChild(head);

  const metaBox = mk('div');
  const mrow = (k, v) => {
    if (!v) return;
    metaBox.appendChild(mk('div', 'pnote', `${k} · ${v}`));
  };
  mrow(T('gui.mem.meta_session'), it.session_id);
  if (it.quality_score != null) mrow(T('gui.mem.meta_quality'), memPct(it.quality_score));
  if (it.confidence != null) mrow(T('gui.mem.meta_confidence'), memPct(it.confidence));
  if (it.maturity_score != null) mrow(T('gui.mem.meta_maturity'), memPct(it.maturity_score));
  if (metaBox.children.length) body.appendChild(metaBox);

  const sec = (label, el) => {
    const s = mk('div', 'pmsec');
    s.appendChild(mk('div', 'cap', label));
    s.appendChild(el);
    body.appendChild(s);
  };
  if (it.kind === 'agent_case') {
    if (it.body) sec(T('gui.mem.sec_approach'), mk('div', 'mempre', it.body));
    if (it.key_insight) sec(T('gui.mem.sec_insight'), mk('div', 'mempre', it.key_insight));
  } else {
    sec(T('gui.mem.sec_detail'), mk('div', 'mempre', it.body || it.summary || ''));
  }

  const delWrap = mk('div', 'pmsec');
  const del = mk('button', 'mini ghost', T('gui.mem.delete'));
  memArm(del, () => memDelete(it));
  delWrap.appendChild(del);
  if (it.kind === 'episode') delWrap.appendChild(mk('div', 'memnote', T('gui.mem.del_episode_note')));
  body.appendChild(delWrap);
  $('#detail').dataset.open = 'true';
}

function memRow(it) {
  const r = mk('div', 'memrow');
  r.tabIndex = 0;
  r.setAttribute('role', 'button');
  const t = mk('div', 't');
  t.appendChild(mk('b', null, it.subject || it.summary || it.id));
  if (it.timestamp) t.appendChild(mk('span', 'when', memWhen(it.timestamp)));
  r.appendChild(t);
  const sub = it.kind === 'agent_case' ? (it.key_insight || it.body) : (it.summary || it.body);
  if (sub) r.appendChild(mk('div', 's', sub));
  const meta = mk('div', 'meta');
  if (typeof it.score === 'number') meta.appendChild(mk('span', 'pmsign faint', it.score.toFixed(2)));
  if (it.kind === 'agent_skill') {
    meta.appendChild(mk('span', 'pmcnt', T('gui.mem.meta_confidence')));
    meta.appendChild(memMeter(it.confidence));
    meta.appendChild(mk('span', 'pmcnt', T('gui.mem.meta_maturity')));
    meta.appendChild(memMeter(it.maturity_score));
  }
  if (it.kind === 'agent_case' && it.quality_score != null) {
    meta.appendChild(mk('span', 'pmsign faint', `${T('gui.mem.meta_quality')} ${memPct(it.quality_score)}`));
  }
  if (meta.children.length) r.appendChild(meta);
  r.onclick = () => memOpenDetail(it);
  r.onkeydown = (e) => { if (e.key === 'Enter') memOpenDetail(it); };
  return r;
}

/* profile_data values are engine-shaped: strings, arrays of objects,
   nested dicts, epoch stamps. Render all of them as prose lines. */
function memVal(v) {
  if (v == null) return '';
  if (Array.isArray(v)) return v.map(memVal).filter(Boolean).join('\n');
  if (typeof v === 'object') {
    return Object.entries(v)
      .map(([k, x]) => `${k}: ${memVal(x)}`)
      .join(' · ');
  }
  return String(v);
}

function memProfileCard(it) {
  const wrap = mk('div');
  const kv = mk('div', 'memkv');
  const data = it.profile_data || {};
  Object.keys(data).forEach((k) => {
    const row = mk('div', 'row');
    row.appendChild(mk('div', 'k', k));
    const isStamp = /_ms$/i.test(k) && Number(data[k]) > 1e12;
    row.appendChild(mk('div', 'v', isStamp ? memWhen(new Date(Number(data[k])).toISOString()) : memVal(data[k])));
    kv.appendChild(row);
  });
  wrap.appendChild(kv);
  const del = mk('button', 'mini ghost', T('gui.mem.delete'));
  del.style.marginTop = '14px';
  memArm(del, () => memDelete(it));
  wrap.appendChild(del);
  wrap.appendChild(mk('div', 'memnote', T('gui.mem.del_profile_note')));
  return wrap;
}

function memPager() {
  const pages = Math.max(1, Math.ceil(memTotal / MEM_PAGE_SIZE));
  if (memQ || pages <= 1) return null;
  const bar = mk('div', 'hubpage');
  const prev = mk('button', 'mini ghost', T('gui.mem.prev'));
  prev.disabled = memPageNo <= 1;
  prev.onclick = () => { memPageNo -= 1; memLoad(); };
  const next = mk('button', 'mini ghost', T('gui.mem.next'));
  next.disabled = memPageNo >= pages;
  next.onclick = () => { memPageNo += 1; memLoad(); };
  bar.append(prev, mk('span', 'pnote', T('gui.mem.page', { p: memPageNo, n: pages })), next);
  return bar;
}

drawMem = function () {
  const box = $('#memBody');
  // A redraw mid-typing must not eat the search field's focus.
  const refocus = document.activeElement && box.contains(document.activeElement)
    && document.activeElement.tagName === 'INPUT';
  box.innerHTML = '';

  const hero = mk('div', 'pmhero');
  hero.appendChild(mk('h3', null, T('gui.mem.hero')));
  box.appendChild(hero);

  const band = mk('div', 'memstats');
  MEM_KINDS.forEach((k) => {
    const b = mk('button', 'mstat');
    b.setAttribute('aria-pressed', String(memKind === k.kind));
    b.appendChild(mk('div', 'k', T(k.tab)));
    b.appendChild(mk('div', 'v', memStats ? String(memStats[k.stat]) : '—'));
    b.appendChild(mk('div', 'h', T(k.hint)));
    b.onclick = () => {
      if (memKind === k.kind) return;
      memKind = k.kind; memPageNo = 1; memQ = ''; memItems = [];
      closeDetail();
      drawMem();
      memLoad();
    };
    band.appendChild(b);
  });
  box.appendChild(band);

  let inp = null;
  if (memKind !== 'profile') {
    const tools = mk('div', 'memtools');
    const find = mk('div', 'cfind');
    find.innerHTML = '<svg class="ic" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" '
      + 'stroke-width="2" aria-hidden="true"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4.3-4.3"/></svg>';
    inp = document.createElement('input');
    inp.placeholder = T('gui.mem.search_ph');
    inp.value = memQ;
    inp.oninput = () => {
      memQ = inp.value.trim();
      clearTimeout(memDebounce);
      memDebounce = setTimeout(() => { memPageNo = 1; memLoad(); }, 350);
    };
    find.appendChild(inp);
    tools.appendChild(find);
    if (memState === 'ready') {
      tools.appendChild(mk('span', 'n', T(memQ ? 'gui.mem.n_hits' : 'gui.mem.n_total', { n: memTotal })));
    }
    box.appendChild(tools);
  }
  if (refocus && inp) {
    inp.focus();
    inp.setSelectionRange(inp.value.length, inp.value.length);
  }

  if (memState === 'error') {
    box.appendChild(mk('div', 'errline-lite', `${T('gui.mem.down')} · ${memErr}`));
    const retry = mk('button', 'mini ghost', T('gui.plug.retry'));
    retry.onclick = memLoad;
    box.appendChild(retry);
    return;
  }
  if (memState !== 'ready' && !memItems.length) {
    box.appendChild(mk('div', 'empty-note', T('gui.hub.reading')));
    return;
  }
  if (!memItems.length) {
    box.appendChild(mk('div', 'empty-note', memQ ? T('gui.mem.none_found', { q: memQ }) : T('gui.mem.empty')));
    return;
  }

  if (memKind === 'profile') {
    box.appendChild(memProfileCard(memItems[0]));
  } else {
    const list = mk('div', 'memlist');
    memItems.forEach((it) => list.appendChild(memRow(it)));
    box.appendChild(list);
    const pager = memPager();
    if (pager) box.appendChild(pager);
  }
};

/* -- workspace: real file browsing + live file view ------------------ */

function relToWorkspace(p) {
  const s = String(p || '');
  const m = s.match(/(?:^|\/)(?:\.raven\/)?workspace\/(.+)$/);
  if (m) return m[1];
  if (s.startsWith('/') || s.startsWith('~')) return null;
  return s.replace(/^\.\//, '');
}

/* "Looks like a path" is not enough to make one clickable: most repo-shaped
   strings in an answer are not files, and a link that opens onto an error is
   worse than plain text. Two cases are provable without a round trip: the text
   names the workspace, or Raven touched that file this session -- and since the
   viewer is no longer confined to the workspace, the second case now counts
   wherever the file lives. Everything else stays plain text. */
wsPathOf = (s) => {
  const t = String(s).trim().replace(/:\d+(?::\d+)?$/, '');
  if (!t || /\s/.test(t)) return null;
  if (/(?:^|\/)(?:\.raven\/)?workspace\/./.test(t)) return relToWorkspace(t) || t;
  const hit = WS.changes.find((c) => c.key === t || wsShortPath(c.key) === t);
  return hit ? hit.key : null;
};
pathOpen = (rel) => showFile(rel);

/* An explicit markdown link is the author handing something over, so the live
   resolver is broader than the bare-span one above: absolute paths, ~ paths
   and workspace-relative shapes all count, extension decides file-or-folder.
   file:// is stripped rather than rejected -- models write it out of habit. */
linkTargetOf = (u) => {
  let t = String(u).trim().replace(/^file:\/\//, '');
  if (!t || /\s|[<>"']/.test(t)) return null;
  const dirMark = /\/$/.test(t);
  t = t.replace(/\/+$/, '');
  if (!/\//.test(t)) return null;
  /* Segments take any non-separator character: deliverables are routinely
     named in the user's language (HANDOFF-开发交接.md), and \w-only segments
     silently dropped every one of those back to plain text. */
  const shaped = /^(?:\/|~\/)/.test(t) || /^[^\s/\\]+(?:\/[^\s/\\]+)+$/.test(t);
  if (!shaped) return null;
  return { p: t, dir: dirMark || !/\.\w{1,8}$/.test(t) };
};

/* Open a folder where folders live: the file tab's tree, unfolded down to it.
   The root listing is fetched first because tree entries are root-relative and
   the root is only learned from fs.list's answer. */
dirOpen = (p) => {
  wsPicked = true;
  if (!wsOpen) setWs(true, 'file'); else { wsTab = 'file'; drawWs(); }
  ftFetch('').then(() => {
    const rel = relToRoot(p);
    ftReveal(rel != null ? rel : String(p).replace(/^\/+/, ''), true);
  }).catch(() => {});
};

/* The viewer reads over HTTP rather than through fs.read: an image or a PDF
   needs a URL a tag can point at, and the endpoint resolves exactly what the
   agent may read, so a file outside the workspace opens too. */
const fileURL = (p) => '/file?path=' + encodeURIComponent(String(p));

const TEXT_EXT = new Set(['c', 'cfg', 'conf', 'cpp', 'css', 'diff', 'env', 'go', 'h', 'ini', 'java',
  'js', 'json', 'jsonl', 'jsx', 'kt', 'log', 'lua', 'patch', 'php', 'pl', 'py', 'pyi', 'rb', 'rs',
  'sh', 'sql', 'swift', 'toml', 'ts', 'tsx', 'txt', 'vue', 'yaml', 'yml', 'zsh']);
const IMG_EXT = new Set(['png', 'jpg', 'jpeg', 'gif', 'webp', 'bmp', 'ico', 'avif']);

/* What to render, decided from the name alone: the endpoint's content type says
   the same thing, but the view has to be laid out before the bytes arrive. */
function fileKind(p) {
  const ext = (String(p).split('.').pop() || '').toLowerCase();
  if (ext === 'md' || ext === 'mdx' || ext === 'markdown') return 'md';
  if (IMG_EXT.has(ext)) return 'img';
  if (ext === 'svg') return 'svg';
  if (ext === 'pdf') return 'pdf';
  if (ext === 'html' || ext === 'htm') return 'html';
  if (ext === 'csv' || ext === 'tsv') return 'csv';
  if (ext === 'json') return 'json';
  if (ext === 'diff' || ext === 'patch') return 'diff';
  if (TEXT_EXT.has(ext)) return 'code';
  return 'bin';
}
const RENDERED = { md: 1, img: 1, svg: 1, pdf: 1, html: 1, csv: 1, json: 1 };

async function showFile(p) {
  const kind = fileKind(p);
  WS.file = { path: String(p), kind, raw: false, text: null, err: null, size: null, loading: false };
  /* Asking for one named file IS picking the file view -- without this the
     panel opens on its launcher and the file that was just read is nowhere. */
  wsPicked = true;
  if (!wsOpen) setWs(true, 'file'); else { wsTab = 'file'; drawWs(); }
}

/* Only the kinds the page itself parses are fetched as text; an image or a PDF
   is handed to the tag that knows how to draw it. */
async function loadFileText(f) {
  const epoch = wsEpoch;
  f.loading = true;
  try {
    const r = await fetch(fileURL(f.path), { credentials: 'same-origin' });
    if (!r.ok) throw new Error(r.status === 403 ? T('gui.ws.file_denied')
      : r.status === 404 ? T('gui.ws.file_gone')
        : r.status === 413 ? T('gui.ws.file_big') : `HTTP ${r.status}`);
    f.text = await r.text();
  } catch (e) {
    f.err = e.message || String(e);
  } finally {
    f.loading = false;
    if (WS.file === f && !wsStale(epoch)) drawWs();
  }
}

function csvTable(text, tab) {
  const rows = text.replace(/\r/g, '').split('\n').filter((l) => l.length).slice(0, 500)
    .map((l) => l.split(tab ? '\t' : ','));
  const t = mk('table', 'csvt');
  rows.forEach((cells, i) => {
    const tr = mk('tr');
    cells.forEach((c) => tr.appendChild(mk(i === 0 ? 'th' : 'td', null, c)));
    t.appendChild(tr);
  });
  const w = mk('div', 'csvw');
  w.appendChild(t);
  return w;
}

/* The browser's PDF viewer is script-driven and draws nothing in a frame with
   scripts denied, so a PDF gets allow-scripts. The origin stays opaque either
   way -- allow-same-origin is never granted -- so nothing in the frame can
   reach this page's cookie or its RPC socket. HTML stays script-free. */
function frameFor(p, kind) {
  const f = mk('iframe');
  f.setAttribute('sandbox', kind === 'pdf' ? 'allow-scripts' : '');
  f.setAttribute('referrerpolicy', 'no-referrer');
  f.src = fileURL(p);
  return f;
}

function fileBody(f) {
  const v = mk('div', 'fview');
  if (f.err) { v.appendChild(mk('div', 'verr', f.err)); return v; }
  const asSource = f.raw || !RENDERED[f.kind];
  if (f.kind === 'img' || (f.kind === 'svg' && !asSource)) {
    const shot = mk('div', 'shot');
    const img = mk('img');
    img.src = fileURL(f.path);
    img.alt = f.path;
    img.onerror = () => { shot.innerHTML = ''; shot.appendChild(mk('div', 'verr', T('gui.ws.file_gone'))); };
    shot.appendChild(img);
    v.appendChild(shot);
    return v;
  }
  if ((f.kind === 'pdf' || f.kind === 'html') && !asSource) {
    v.appendChild(frameFor(f.path, f.kind));
    return v;
  }
  if (f.kind === 'bin') {
    const n = mk('div', 'binote');
    n.append(mk('div', 'h', T('gui.ws.file_binary')), mk('div', 'w', f.path));
    const b = mk('button', 'mini ghost', T('gui.ws.copy_path_do'));
    b.onclick = () => navigator.clipboard && navigator.clipboard.writeText(f.path);
    n.appendChild(b);
    v.appendChild(n);
    return v;
  }
  if (f.text == null) {
    if (!f.loading) loadFileText(f);
    v.appendChild(mk('div', 'vspin', T('gui.ws.file_loading')));
    return v;
  }
  if (f.kind === 'md' && !asSource) {
    const pr = mk('div', 'prose');
    pr.innerHTML = md(f.text);
    v.appendChild(pr);
    return v;
  }
  if (f.kind === 'csv' && !asSource) {
    v.appendChild(csvTable(f.text, /\.tsv$/i.test(f.path)));
    return v;
  }
  if (f.kind === 'json' && !asSource) {
    const tree = jsonTree(f.text);
    /* A file that does not parse is still a file: fall through to the plain
       numbered lines rather than showing an error for something readable. */
    if (tree) { v.appendChild(tree); return v; }
  }
  const code = mk('div', 'code');
  f.text.split('\n').forEach((line, i) => {
    const row = mk('div', 'ln' + (f.kind === 'diff' ? diffLineCls(line) : ''));
    row.append(mk('i', null, String(i + 1)), mk('span', null, line || ' '));
    code.appendChild(row);
  });
  v.appendChild(code);
  return v;
}

/* A patch is one of the few formats whose lines carry their meaning in the
   first character; without colour it reads as noise with plus signs. */
function diffLineCls(line) {
  if (/^(\+\+\+|---)/.test(line)) return ' dmeta';
  if (line[0] === '+') return ' dadd';
  if (line[0] === '-') return ' ddel';
  if (line.startsWith('@@')) return ' dhunk';
  if (/^(diff |index |new file|deleted file|similarity |rename |Binary )/.test(line)) return ' dmeta';
  return '';
}

/* ── json: a foldable tree ────────────────────────────────────────────
   Native <details> does the folding, so there is no open-state to manage and
   the keyboard works for free. A node budget keeps a machine-dumped megabyte
   from freezing the tab: past it, the rest of a container is summarised and
   the source toggle is the way to read that far. */
const JSON_NODE_BUDGET = 4000;

function jsonTree(text) {
  if (text.length > 2 * 1024 * 1024) return null;
  let val;
  try { val = JSON.parse(text); } catch { return null; }
  const box = mk('div', 'jsonv');
  const state = { left: JSON_NODE_BUDGET };
  box.appendChild(jsonNode(val, null, state, 0));
  return box;
}

const jsonLeaf = (v) => {
  const s = mk('span', 'jv ' + (v === null ? 'jnull' : typeof v === 'string' ? 'jstr'
    : typeof v === 'number' ? 'jnum' : 'jbool'));
  s.textContent = v === null ? 'null' : typeof v === 'string' ? JSON.stringify(v) : String(v);
  return s;
};

function jsonNode(v, key, state, depth) {
  state.left -= 1;
  const keySpan = () => {
    const k = mk('span', 'jk');
    k.textContent = typeof key === 'number' ? String(key) : JSON.stringify(key);
    return k;
  };
  if (v === null || typeof v !== 'object') {
    const row = mk('div', 'jrow');
    if (key !== null) { row.appendChild(keySpan()); row.appendChild(mk('span', 'jc', ':')); }
    row.appendChild(jsonLeaf(v));
    return row;
  }
  const isArr = Array.isArray(v);
  const entries = isArr ? v.map((x, i) => [i, x]) : Object.entries(v);
  const d = document.createElement('details');
  d.className = 'jnode';
  /* The first two levels open by default: that is the shape of the file. Below
     that the reader opens what they are looking for. */
  if (depth < 2) d.open = true;
  const sum = document.createElement('summary');
  if (key !== null) { sum.appendChild(keySpan()); sum.appendChild(mk('span', 'jc', ':')); }
  sum.appendChild(mk('span', 'jb', isArr ? '[' : '{'));
  sum.appendChild(mk('span', 'jn', T('gui.ws.json_items', { n: String(entries.length) })));
  sum.appendChild(mk('span', 'jb', isArr ? ']' : '}'));
  d.appendChild(sum);
  const kids = mk('div', 'jkids');
  for (const [k, child] of entries) {
    if (state.left <= 0) {
      kids.appendChild(mk('div', 'jrow jmore', T('gui.ws.json_capped')));
      break;
    }
    kids.appendChild(jsonNode(child, k, state, depth + 1));
  }
  if (!entries.length) kids.appendChild(mk('div', 'jrow jmore', isArr ? '[]' : '{}'));
  d.appendChild(kids);
  return d;
}

/* ── file tree ────────────────────────────────────────────────────────
   One tree over the whole workspace instead of a one-folder-at-a-time drill:
   folders open in place, so where a file sits stays visible while you read it.
   Listings are fetched per folder on first open and kept -- fs.list is a round
   trip, and a tree that re-fetched on every keystroke would flicker. */
const FT = { open: new Set(['']), kids: new Map(), q: '', w: 208, hide: false,
  list: null, run: 0, crawling: false, capped: false, root: '' };
const FTW_KEY = 'raven.gui.ftw';
try { FT.w = Math.max(150, Math.min(460, parseFloat(localStorage.getItem(FTW_KEY)) || FT.w)); } catch {}

/* A width and a collapsed tree are how this reader likes to work, not part of
   the workspace, so they survive a session switch. */
function ftReset() {
  FT.open = new Set(['']); FT.kids.clear();
  FT.q = ''; FT.run += 1; FT.crawling = false; FT.capped = false;
  FT.root = '';
}

/* Tree entries are root-relative; the viewer and the reveal RPC take real
   paths, so the join happens at the moment a row leaves the tree. */
const ftAbs = (full) => (FT.root ? `${FT.root}/${full}` : full);
const relToRoot = (p) => {
  const s = String(p || '');
  return FT.root && s.startsWith(FT.root + '/') ? s.slice(FT.root.length + 1) : null;
};

/* A different session is a different workspace state: keep no listing that was
   read before the switch. */
const wsResetBase = wsReset;
wsReset = function () { wsResetBase(); ftReset(); };

/* One promise per directory, shared by the tree and the search crawler, so a
   folder the crawler already asked for is never fetched again when it is
   opened by hand -- and vice versa. */
const ftPending = new Map();
function ftFetch(dir) {
  if (FT.kids.has(dir)) return Promise.resolve(FT.kids.get(dir));
  if (ftPending.has(dir)) return ftPending.get(dir);
  const p = rpc.call('fs.list', { path: dir, session: cur || '' })
    .then((r) => {
      /* The root rides on every answer: it is the session's working directory,
         which the server resolves and this side has no way to guess. */
      if (r.root) FT.root = r.root;
      /* Directories first, each group alphabetical -- the order every file
         manager has trained people to expect. */
      const kids = [...r.entries].sort((a, b) =>
        (b.dir ? 1 : 0) - (a.dir ? 1 : 0) || a.name.localeCompare(b.name));
      FT.kids.set(dir, kids);
      return kids;
    })
    .catch((e) => {
      const bad = { err: e.message || String(e) };
      FT.kids.set(dir, bad);
      return bad;
    })
    .finally(() => ftPending.delete(dir));
  ftPending.set(dir, p);
  return p;
}

function ftLoad(dir) {
  if (FT.kids.has(dir)) return;
  ftFetch(dir).then(() => ftDraw());
}

/* Redraws only the row list: the search field above it must survive every
   redraw, or a listing that lands mid-word steals the focus and the caret. */
function ftDraw() {
  const list = FT.list;
  if (!list || !list.isConnected) {
    if (wsTab === 'file' && wsOpen) drawWs();
    return;
  }
  const y = list.scrollTop;
  list.innerHTML = '';
  if (FT.q) ftResults(list); else ftRows(list, '', 0);
  list.scrollTop = y;
}

const ftJoin = (dir, name) => (dir ? `${dir}/${name}` : name);
const ftKind = (name) => {
  const k = fileKind(name);
  if (k === 'md') return 'md';
  if (k === 'img' || k === 'svg' || k === 'pdf') return 'img';
  if (/\.(json|ya?ml|toml|ini|cfg|conf|lock|env)$/i.test(name)) return 'cfg';
  return k === 'code' || k === 'html' || k === 'csv' ? 'code' : 'doc';
};

/* ── workspace search ─────────────────────────────────────────────────
   A query walks the whole workspace, not just the folders that happen to be
   open: fs.list is the only primitive the host offers, so the client crawls --
   breadth-first, a few folders at a time, against a budget. Every listing it
   pulls lands in the same cache the tree reads, so the first search warms the
   tree and the second search is instant. */
const FT_SKIP = new Set(['.git', 'node_modules', '.venv', 'venv', '__pycache__',
  '.mypy_cache', '.ruff_cache', '.pytest_cache', '.cache', 'dist', 'build', 'target', '.next']);
const FT_CRAWL_DIRS = 600;
const FT_CRAWL_PAR = 4;
const FT_SHOW_MAX = 120;

async function ftCrawl(run) {
  FT.crawling = true;
  FT.capped = false;
  let budget = FT_CRAWL_DIRS;
  const queue = [''];
  const seen = new Set(queue);
  const worker = async () => {
    while (queue.length) {
      if (FT.run !== run) return;
      const dir = queue.shift();
      let kids = FT.kids.get(dir);
      if (kids === undefined) {
        if (budget <= 0) { FT.capped = true; continue; }
        budget -= 1;
        kids = await ftFetch(dir);
        if (FT.run === run) ftDrawSoon();
      }
      if (!kids || kids.err) continue;
      kids.forEach((e) => {
        if (!e.dir || FT_SKIP.has(e.name)) return;
        const full = ftJoin(dir, e.name);
        if (!seen.has(full)) { seen.add(full); queue.push(full); }
      });
    }
  };
  await Promise.all(Array.from({ length: FT_CRAWL_PAR }, worker));
  if (FT.run !== run) return;
  FT.crawling = false;
  ftDraw();
}

/* Streams land many at a time; one repaint per frame is enough. */
let ftDrawQueued = false;
function ftDrawSoon() {
  if (ftDrawQueued) return;
  ftDrawQueued = true;
  requestAnimationFrame(() => { ftDrawQueued = false; ftDraw(); });
}

function ftMatches() {
  const out = [];
  const walk = (dir) => {
    const kids = FT.kids.get(dir);
    if (!kids || kids.err) return;
    kids.forEach((e) => {
      if (e.dir && FT_SKIP.has(e.name)) return;
      const full = ftJoin(dir, e.name);
      const at = e.name.toLowerCase().indexOf(FT.q);
      if (at >= 0) out.push({ e, full, at });
      if (e.dir) walk(full);
    });
  };
  walk('');
  /* Name-starts-with beats name-contains, files beat folders (opening one is
     the usual intent), shallow beats deep. */
  const depth = (p) => p.split('/').length;
  out.sort((a, b) => (a.at === 0 ? 0 : 1) - (b.at === 0 ? 0 : 1)
    || (a.e.dir ? 1 : 0) - (b.e.dir ? 1 : 0)
    || depth(a.full) - depth(b.full)
    || a.e.name.localeCompare(b.e.name));
  return out;
}

/* Open every folder above the path, so the file has a row in the tree when the
   query is cleared -- or right now, when revealing is the point. */
function ftOpenTo(full) {
  let acc = '';
  full.split('/').slice(0, -1).forEach((p) => {
    acc = ftJoin(acc, p);
    FT.open.add(acc);
    ftLoad(acc);
  });
}

function ftReveal(full, isDir) {
  ftOpenTo(full);
  if (isDir) { FT.open.add(full); ftLoad(full); }
  FT.q = '';
  FT.run += 1;
  FT.crawling = false;
  const inp = FT.list && FT.list.parentElement && FT.list.parentElement.querySelector('.ftq input');
  if (inp) inp.value = '';
  ftDraw();
  requestAnimationFrame(() => {
    const row = FT.list && FT.list.querySelector(`[data-p="${CSS.escape(full)}"]`);
    if (row) row.scrollIntoView({ block: 'center' });
  });
}

function ftName(e, at) {
  const nm = mk('span', 'nm');
  nm.append(e.name.slice(0, at), mk('b', 'hl', e.name.slice(at, at + FT.q.length)), e.name.slice(at + FT.q.length));
  return nm;
}

function ftResults(box) {
  const hits = ftMatches();
  hits.slice(0, FT_SHOW_MAX).forEach(({ e, full, at }) => {
    const row = mk('button', 'ftrow');
    row.style.paddingLeft = '10px';
    row.appendChild(ico(e.dir ? ICO.file : ICO.doc, 'fi ' + (e.dir ? 'k-dir' : 'k-' + ftKind(e.name))));
    row.appendChild(ftName(e, at));
    const cut = full.lastIndexOf('/');
    if (cut > 0) {
      const pth = mk('span', 'pth', full.slice(0, cut));
      pth.title = full;
      row.appendChild(pth);
    }
    row.onclick = () => {
      if (e.dir) { ftReveal(full, true); return; }
      ftOpenTo(full);
      showFile(ftAbs(full));
    };
    ctxMenu(row, () => (e.dir ? [] : [{ label: T('gui.ws.open'), fn: () => { ftOpenTo(full); showFile(ftAbs(full)); } }])
      .concat([
        { label: T('gui.ws.reveal'), fn: () => ftReveal(full, e.dir) },
        { label: T('gui.ws.copy_path_do'), fn: () => copyToClip(ftAbs(full), T('gui.ws.copy_path')) },
        { label: T('gui.ws.copy_name'), fn: () => copyToClip(e.name, T('gui.ws.copied_name')) },
      ]));
    box.appendChild(row);
  });
  if (!hits.length && !FT.crawling) box.appendChild(ftLeaf(T('gui.ws.no_match'), 0));
  if (hits.length > FT_SHOW_MAX) {
    box.appendChild(ftLeaf(T('gui.ws.search_more', { n: hits.length - FT_SHOW_MAX }), 0));
  }
  if (FT.crawling) box.appendChild(ftLeaf(T('gui.ws.searching'), 0));
  else if (FT.capped) box.appendChild(ftLeaf(T('gui.ws.search_capped'), 0));
}

function ftRows(box, dir, depth) {
  const kids = FT.kids.get(dir);
  if (kids === undefined) {
    ftLoad(dir);
    box.appendChild(ftLeaf(T('gui.ws.file_loading'), depth));
    return;
  }
  if (kids.err) { box.appendChild(ftLeaf(T('gui.ws.read_fail', { err: kids.err }), depth)); return; }
  if (!kids.length) { box.appendChild(ftLeaf(T('gui.ws.dir_empty'), depth)); return; }
  kids.forEach((e) => {
    const full = ftJoin(dir, e.name);
    const open = e.dir && FT.open.has(full);
    const row = mk('button', 'ftrow' + (!e.dir && WS.file && WS.file.path === ftAbs(full) ? ' on' : ''));
    row.dataset.p = full;
    row.style.paddingLeft = (10 + depth * 13) + 'px';
    if (depth) {
      /* Guide lines, one per level, each under its ancestor's chevron. Inline
         because the count is the row's depth; hover keeps working because the
         states set background-color, never the shorthand. */
      row.style.backgroundImage = 'repeating-linear-gradient(to right, var(--line) 0 1px, transparent 1px 13px)';
      row.style.backgroundSize = (depth * 13) + 'px 100%';
      row.style.backgroundPosition = '16px 0';
      row.style.backgroundRepeat = 'no-repeat';
    }
    if (e.dir) {
      row.setAttribute('aria-expanded', String(open));
      row.appendChild(ico(ACT_ICO.chev, 'cv'));
      row.appendChild(ico(ICO.file, 'fi k-dir'));
    } else {
      row.appendChild(mk('span', 'sp'));
      row.appendChild(ico(ICO.doc, 'fi k-' + ftKind(e.name)));
    }
    row.appendChild(mk('span', 'nm', e.name));
    if (!e.dir) row.appendChild(mk('span', 'sz', fmtSize(e.size)));
    const flip = () => {
      if (FT.open.has(full)) FT.open.delete(full); else { FT.open.add(full); ftLoad(full); }
      ftDraw();
    };
    row.onclick = () => { if (e.dir) flip(); else showFile(ftAbs(full)); };
    ctxMenu(row, () => (e.dir
      ? [{ label: T(open ? 'gui.ws.dir_collapse' : 'gui.ws.dir_expand'), fn: flip }]
      : [{ label: T('gui.ws.open'), fn: () => showFile(ftAbs(full)) }]
    ).concat([
      { label: T('gui.ws.copy_path_do'), fn: () => copyToClip(ftAbs(full), T('gui.ws.copy_path')) },
      { label: T('gui.ws.copy_name'), fn: () => copyToClip(e.name, T('gui.ws.copied_name')) },
    ]));
    box.appendChild(row);
    if (e.dir && open) ftRows(box, full, depth + 1);
  });
}

function ftLeaf(text, depth) {
  const d = mk('div', 'ftleaf', text);
  d.style.paddingLeft = (23 + depth * 13) + 'px';
  return d;
}

let ftDebounce = 0;
function ftPane() {
  const tree = mk('div', 'ftree');
  const q = mk('div', 'ftq');
  q.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.9"'
    + ' aria-hidden="true"><circle cx="11" cy="11" r="6.5"/><path d="M16 16l4.5 4.5"/></svg>';
  const inp = mk('input');
  inp.type = 'search';
  inp.placeholder = T('gui.ws.search');
  inp.setAttribute('aria-label', T('gui.ws.search'));
  inp.value = FT.q;
  inp.oninput = () => {
    FT.q = inp.value.trim().toLowerCase();
    FT.run += 1;
    clearTimeout(ftDebounce);
    /* What is already cached answers immediately; the crawl for the rest waits
       out the keystroke burst. */
    ftDraw();
    if (FT.q) ftDebounce = setTimeout(() => ftCrawl(FT.run), 220);
    else FT.crawling = false;
  };
  inp.onkeydown = (e) => {
    if (e.key === 'Escape' && inp.value) {
      e.stopPropagation();
      inp.value = '';
      inp.oninput();
    }
  };
  q.appendChild(inp);
  const list = mk('div', 'ftlist');
  FT.list = list;
  tree.append(q, list);
  if (FT.q) ftResults(list); else ftRows(list, '', 0);
  return tree;
}

/* The seam between tree and file. It writes the width straight onto the wrap
   rather than through a redraw: a drag repaints on every pointer move, and
   rebuilding the tree at that rate would drop frames on a deep folder. */
function ftGrip(wrap) {
  const g = mk('button', 'grip');
  g.type = 'button';
  g.setAttribute('role', 'separator');
  g.setAttribute('aria-orientation', 'vertical');
  g.title = T('gui.ws.tree_resize');
  g.setAttribute('aria-label', T('gui.ws.tree_resize'));
  const put = (px, persist) => {
    const max = Math.max(150, (wrap.offsetWidth || 600) - 220);
    FT.w = Math.round(Math.max(150, Math.min(max, px)));
    wrap.style.setProperty('--ftw', FT.w + 'px');
    if (persist) { try { localStorage.setItem(FTW_KEY, String(FT.w)); } catch {} }
  };
  g.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    const x0 = e.clientX;
    const w0 = FT.hide ? 0 : FT.w;
    let opened = false;
    g.dataset.drag = 'true';
    g.setPointerCapture(e.pointerId);
    document.body.style.userSelect = 'none';
    const move = (ev) => {
      const d = ev.clientX - x0;
      /* Dragging the shut seam to the right is how the tree comes back, without
         going up to the folder for it. */
      if (FT.hide) {
        if (d < 60) return;
        FT.hide = false;
        opened = true;
        wrap.dataset.tree = 'on';
      }
      put(w0 + d, false);
    };
    const up = () => {
      g.removeEventListener('pointermove', move);
      g.removeEventListener('pointerup', up);
      g.removeEventListener('pointercancel', up);
      delete g.dataset.drag;
      document.body.style.userSelect = '';
      if (opened) { drawWs(); return; }
      /* Shoved against its own floor: the intent was to get rid of it. */
      if (!FT.hide && FT.w <= 150 && e.clientX - x0 < -40) {
        FT.hide = true;
        drawWs();
        return;
      }
      put(FT.w, true);
    };
    g.addEventListener('pointermove', move);
    g.addEventListener('pointerup', up);
    g.addEventListener('pointercancel', up);
  });
  g.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 40 : 10;
    if (FT.hide) return;
    if (e.key === 'ArrowLeft') { e.preventDefault(); put(FT.w - step, true); }
    if (e.key === 'ArrowRight') { e.preventDefault(); put(FT.w + step, true); }
  });
  return g;
}

const FT_ICO = {
  folder: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"'
    + ' stroke-linejoin="round" aria-hidden="true"><path d="M3 6.6a2 2 0 0 1 2-2h3.6l1.8 2.2H19a2 2 0 0 1 2 2v8.6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>',
  page: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"'
    + ' aria-hidden="true"><path d="M7 2.8h7L19 8v13.2H7z"/><path d="M13.5 2.8V8H19"/></svg>'
};

/* Where the file sits, as its own line of text: the folders in faint, the file
   itself in the text colour, so the name still reads first. */
function fbarPath(path) {
  const nm = mk('span', 'nm');
  const rel = wsShortPath(path);
  const cut = rel.lastIndexOf('/');
  if (cut > 0) nm.appendChild(mk('i', null, rel.slice(0, cut + 1)));
  nm.appendChild(mk('b', null, cut > 0 ? rel.slice(cut + 1) : rel));
  nm.title = path;
  return nm;
}

drawWsFile = function (box) {
  box.dataset.view = 'file';
  const wrap = mk('div', 'fwrap');
  wrap.dataset.tree = FT.hide ? 'off' : 'on';
  wrap.style.setProperty('--ftw', FT.w + 'px');

  const f = WS.file;
  /* One bar across the whole view, and the folder in it is what opens and shuts
     the tree -- the control belongs next to the path it is about. */
  const bar = mk('div', 'fbar');
  const tog = mk('button', 'ghost-ic ftog tipdn');
  tog.innerHTML = FT_ICO.folder;
  tog.dataset.tip = T(FT.hide ? 'gui.ws.tree_show' : 'gui.ws.tree_hide');
  tog.setAttribute('aria-label', tog.dataset.tip);
  tog.setAttribute('aria-pressed', String(!FT.hide));
  tog.onclick = () => { FT.hide = !FT.hide; drawWs(); };
  bar.appendChild(tog);
  if (f) {
    const nm = fbarPath(f.path);
    ctxMenu(nm, () => [
      { label: T('gui.ws.copy_path_do'), fn: () => copyToClip(f.path, T('gui.ws.copy_path')) },
      { label: T('gui.ws.reveal'), fn: () => ftReveal(relToRoot(f.path) || relToWorkspace(f.path) || f.path, false) },
    ]);
    bar.appendChild(nm);
    /* Copy rides right behind the path it copies -- an icon at the far end of
       the bar read as a generic control for the whole view, not for this line. */
    const cp = mk('button', 'ghost-ic fcopy tipdn');
    cp.appendChild(ico(ICO.doc));
    cp.dataset.tip = T('gui.ws.copy_path_do');
    cp.setAttribute('aria-label', T('gui.ws.copy_path_do'));
    cp.onclick = () => navigator.clipboard && navigator.clipboard.writeText(f.path)
      .then(() => toast(T('gui.ws.copy_path')), () => {});
    bar.appendChild(cp);
  } else {
    bar.appendChild(mk('span', 'nm', T('gui.ws.file_none')));
  }
  bar.appendChild(mk('span', 'fsp'));

  const row = mk('div', 'frow');
  row.appendChild(ftPane());
  row.appendChild(ftGrip(wrap));
  const pane = mk('div', 'fpane');
  if (!f) {
    const empty = mk('div', 'fempty');
    const g = mk('div');
    g.innerHTML = FT_ICO.page;
    empty.append(g, mk('div', 't', T('gui.ws.file_none')));
    pane.appendChild(empty);
    row.appendChild(pane);
    wrap.append(bar, row);
    box.appendChild(wrap);
    return;
  }
  /* Only offered where there are two forms to choose between: a .py has no
     rendered form, and a toggle that cannot change anything is noise. */
  if (RENDERED[f.kind]) {
    const seg = mk('div', 'kseg');
    [[false, 'gui.ws.file_rendered'], [true, 'gui.ws.file_source']].forEach(([raw, key]) => {
      const b = mk('button', null, T(key));
      b.setAttribute('aria-pressed', String(!!f.raw === raw));
      b.onclick = () => { f.raw = raw; drawWs(); };
      seg.appendChild(b);
    });
    bar.appendChild(seg);
  }
  /* A framed document depends on the host's own viewer -- WKWebView renders a
     PDF inline, a stripped Chromium may render nothing -- and the sandbox that
     keeps an artifact off this origin is not negotiable. So the frame stays,
     and this is the way out when the host draws nothing in it. */
  if (f.kind === 'pdf' || f.kind === 'html') {
    const ext = mk('button', 'ghost-ic tipdn');
    ext.appendChild(ico(ICO.ext));
    ext.dataset.tip = T('gui.ws.file_newtab');
    ext.setAttribute('aria-label', T('gui.ws.file_newtab'));
    ext.onclick = () => window.open(fileURL(f.path), '_blank', 'noopener');
    bar.appendChild(ext);
  }
  /* The host-side action lives at the end of the bar: it opens a window on the
     gateway's own desktop, worded for that machine's file manager. */
  const rv = mk('button', 'ghost-ic tipdn');
  rv.appendChild(ico(ICO.reveal));
  rv.dataset.tip = T(HOST_PLATFORM === 'mac' ? 'gui.ws.reveal_finder'
    : HOST_PLATFORM === 'windows' ? 'gui.ws.reveal_explorer' : 'gui.ws.reveal_folder');
  rv.setAttribute('aria-label', rv.dataset.tip);
  rv.onclick = () => rpc.call('fs.reveal', { path: f.path, session: cur || '' })
    .then(() => {}, (e) => toast((e && e.message) || String(e)));
  bar.appendChild(rv);
  /* No close: picking another file replaces this one, and an empty pane is not
     a state anyone asks for. */
  const body = mk('div', 'fbody');
  body.appendChild(fileBody(f));
  pane.appendChild(body);
  row.appendChild(pane);
  wrap.append(bar, row);
  box.appendChild(wrap);
};

wsShortPath = function (p) {
  const rel = relToRoot(p) || relToWorkspace(p);
  return rel || String(p).replace(/^\/Users\/[^/]+\//, '~/');
};

/* Host action for the Changes view: the viewer reads whatever the agent may
   read, so a path outside the workspace opens the same way. */
wsOpenPath = function (p) { showFile(p); };
wsOpenUrl = function (u) {
  if (/^https?:\/\//.test(u)) { window.open(u, '_blank', 'noopener'); return; }
  navigator.clipboard.writeText(String(u)).then(
    () => toast(T('gui.ws.copy_path')),
    () => toast(String(u))
  );
};

/* -- attachments -----------------------------------------------------
   Files are uploaded into <workspace>/uploads and handed to the agent as
   paths: every file tool is already workspace-scoped, so a path is all it
   needs. Bytes never ride inside the message. */
const atts = [];
const fmtSize = (n) => (n >= 1048576 ? `${(n / 1048576).toFixed(1)} MB`
  : n >= 1024 ? `${Math.round(n / 1024)} KB` : `${n} B`);

function drawAtts() {
  let box = $('#atts');
  if (!box) {
    box = mk('div', 'atts');
    box.id = 'atts';
    const dock = $('#ta').closest('.dock-in');
    dock.insertBefore(box, dock.querySelector('.field'));
  }
  box.innerHTML = '';
  box.hidden = !atts.length;
  atts.forEach((a, i) => {
    const isImg = !!a.url;
    const chip = mk('div', 'att' + (isImg ? ' img' : '') + (a.uploading ? ' up' : ''));
    if (isImg) {
      const img = mk('img');
      img.src = a.url;
      img.alt = a.name;
      img.title = `${a.name} · ${a.uploading ? T('gui.att.uploading') : fmtSize(a.size)}`;
      /* the square crops the image, so a click has to be able to show all of it */
      img.onclick = () => openImage(a.url, a.name);
      chip.appendChild(img);
    } else {
      chip.append(mk('span', 'nm', a.name), mk('span', 'sz', a.uploading ? T('gui.att.uploading') : fmtSize(a.size)));
    }
    const rm = mk('button', 'rm', '✕');
    rm.setAttribute('aria-label', T('gui.att.remove', { name: a.name }));
    rm.onclick = (e) => { e.stopPropagation(); atts.splice(i, 1); drawAtts(); };
    chip.appendChild(rm);
    box.appendChild(chip);
  });
  /* A staged file is enough to send, so the button's enabled state follows the
     tray, not just the field. */
  goState();
}

hasAtts = () => atts.length > 0;

function addFiles(fileList) {
  [...fileList].forEach((file) => {
    const entry = { name: file.name, size: file.size, uploading: true, path: null, url: null };
    atts.push(entry);
    drawAtts();
    const reader = new FileReader();
    reader.onload = () => {
      const dataUrl = String(reader.result);
      const b64 = dataUrl.split(',')[1] || '';
      /* Keep the bytes for display only: an image renders as itself in the
         composer, and once uploaded, keyed by path, in the sent bubble. */
      if (/^image\//.test(file.type || '')) entry.url = dataUrl;
      rpc.call('fs.upload', { name: file.name, content_b64: b64, session: cur || '' })
        .then((r) => {
          entry.path = r.path; entry.size = r.size; entry.uploading = false;
          if (entry.url) ATT_IMG.set(r.path, entry.url);
          drawAtts();
        })
        .catch((e) => {
          const i = atts.indexOf(entry);
          if (i >= 0) atts.splice(i, 1);
          drawAtts();
          errLine(T('gui.att.fail', { name: file.name }), (e.data && e.data.detail) || e.message || String(e));
        });
    };
    reader.onerror = () => {
      const i = atts.indexOf(entry);
      if (i >= 0) atts.splice(i, 1);
      drawAtts();
    };
    reader.readAsDataURL(file);
  });
}

{
  const picker = mk('input');
  picker.type = 'file';
  picker.multiple = true;
  picker.style.display = 'none';
  picker.onchange = () => { addFiles(picker.files); picker.value = ''; };
  document.body.appendChild(picker);
  $('#attBtn').onclick = () => picker.click();

  const field = $('#ta').closest('.field');
  let dragDepth = 0;
  const over = (e) => { e.preventDefault(); };
  field.addEventListener('dragenter', (e) => { over(e); dragDepth++; field.classList.add('drop'); });
  field.addEventListener('dragover', over);
  field.addEventListener('dragleave', () => { if (--dragDepth <= 0) field.classList.remove('drop'); });
  field.addEventListener('drop', (e) => {
    e.preventDefault(); dragDepth = 0; field.classList.remove('drop');
    if (e.dataTransfer && e.dataTransfer.files.length) addFiles(e.dataTransfer.files);
  });
  $('#ta').addEventListener('paste', (e) => {
    const files = [...(e.clipboardData ? e.clipboardData.files : [])];
    if (files.length) { e.preventDefault(); addFiles(files); }
  });
}

/* -- real session actions: branch / clear ---------------------------- */
answerBlock = function (text, meta, anchor) {
  const box = mk('div', 'answer in');
  const body = mk('div', 'prose');
  const acts = mk('div', 'acts');
  const add = (label, path, fn) => {
    const b = mk('button');
    b.dataset.tip = label;
    b.dataset.label = label;
    b.setAttribute('aria-label', label);
    b.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
    b.onclick = fn;
    acts.appendChild(b);
    return b;
  };
  // Toasts are gone, so an action reports back through its own hover label.
  const flash = (b, word) => {
    b.dataset.tip = word;
    setTimeout(() => { b.dataset.tip = b.dataset.label; }, 1400);
  };
  const copy = add(T('gui.answer.copy'), '<rect x="9" y="9" width="11" height="11" rx="2.5"/>'
    + '<path d="M5.5 15H5a1.5 1.5 0 0 1-1.5-1.5v-8A1.5 1.5 0 0 1 5 4h8A1.5 1.5 0 0 1 14.5 5.5V6"/>', () => {
    navigator.clipboard && navigator.clipboard.writeText(text);
    flash(copy, T('gui.answer.copied'));
  });
  /* Branching acts on `cur` -- the OPEN conversation -- so inside a sub-agent
     transcript (stageHost points the renderer at the panel) the button would
     fork the parent session while claiming to fork the run. A run has no
     session to fork; the row shows its clock instead, which the record kept. */
  const canBranch = !stageHost;
  if (canBranch) {
    add(T('gui.answer.branch'), '<circle cx="7" cy="6" r="2.2"/><circle cx="7" cy="18" r="2.2"/>'
      + '<circle cx="17" cy="8" r="2.2"/><path d="M7 8.2v7.6"/>'
      + '<path d="M17 10.2v1.3a3.5 3.5 0 0 1-3.5 3.5H10"/>', () => {
      rpc.call('session.branch', { session_id: cur })
        .then((r) => {
          if (!r.session_id) { toast('这个会话还没有内容，无法分叉'); return; }
          const s = { id: r.session_id, title: r.title || T('gui.sess.branch_title'),
            last: T('gui.sess.branched'), when: T('gui.sess.just_now'), g: '今天', run: null, live: true };
          SESS.unshift(s); cur = s.id; drawList(); openSession(s);
          toast(`已分叉，带上了 ${r.message_count || 0} 条消息`);
        })
        .catch((e) => toast(`分叉失败：${e.message || e}`));
    });
  }
  // Copy and branch only — the footer stays a quiet two-icon row per the
  // maintainer's explicit call. Undo/regenerate live in session commands.
  const foot = mk('div', 'ansfoot');
  foot.appendChild(acts);
  if (meta) foot.appendChild(mk('span', 'turnmeta', meta));
  /* Same two actions as the footer: right-clicking an answer should not offer
     less than hovering it does. Selected text keeps the host menu instead --
     copying a quote is what a selection is for. */
  ctxMenu(box, () => [
    { label: T('gui.answer.copy'), fn: () => copyToClip(text, T('gui.answer.copied')) },
  ].concat(canBranch ? [{ label: T('gui.answer.branch'), fn: () => acts.children[1].click() }] : []));
  box.append(body, foot);
  /* `anchor` is where the streamed prose stood. Appending instead was a real
     reordering: anything that landed mid-turn -- a delegated result's delivery
     row, a runtime notice -- was drawn at the tail while the text was still
     streaming, and then the finished answer jumped BELOW it, so the page
     claimed the result came back before the model said it was dispatching one. */
  stageBox().insertBefore(box, anchor || null);
  return body;
};

SLASH.forEach((x) => {
  if (x.id === 'gui.clear') {
    x.fn = () => confirmAsk(T('gui.clear_title'), T('gui.clear_body'), T('gui.clear_yes'), () => {
      rpc.call('session.clear', { session_id: cur })
        .then(() => {
          $('#stage').innerHTML = ''; pitch();
          const s = sess(cur); if (s) s.last = T('gui.sess.cleared');
          use = null; drawMeter(); drawList();
        })
        .catch((e) => errLine(T('gui.clear_title'), (e.data && e.data.detail) || e.message || String(e)));
    });
  }
  if (x.id === 'gui.compress') x.fn = compressNow;
});

/* Manual compaction. The runtime already compacts when a prompt outgrows the
   window; this forces the same pass early, which is what you want once the
   earlier half of a session has stopped being useful. */
async function compressNow() {
  if (!cur || draft) return;
  const line = mk('div', 'act in');
  line.append(mk('span', 'g'), mk('span', 'arg', T('gui.compress.running')));
  $('#stage').appendChild(line); down();
  try {
    const r = await rpc.call('session.compress', { session_id: cur });
    const arg = line.querySelector('.arg');
    arg.textContent = r.removed
      ? T('gui.compress.done', { n: r.removed, before: fmtTok(r.before_tokens), after: fmtTok(r.after_tokens) })
      : T('gui.compress.noop');
  } catch (e) {
    line.remove();
    errLine(T('gui.compress.fail', { err: '' }).replace(/[:：]\s*$/, ''),
      (e.data && e.data.detail) || e.message || String(e));
  }
  down();
}

// Dev-only hook: lets a design pass preview the clarify sheet without
// spending a model turn (window.__clarify({question, choices})).
window.__clarify = (p) => rpc.notify['clarify.request'](p || { request_id: 'dev', question: '预览', choices: ['A', 'B'] });

// Same reason: the update row's version state only appears when a release is
// actually newer, which never happens on a dev checkout
// (window.__upnote('ver', '0.1.11')).
window.__upnote = (kind, latest) => showUpNote(kind || 'ver', latest);

/* ---- boot ---------------------------------------------------------- */
(async () => {
  if (!(await rpc.connect())) return;
  try {
    const hello = await rpc.call('system.hello', { client_version: '0.1.0' });
    if (hello && hello.platform) HOST_PLATFORM = hello.platform;
    // Before the first paint of anything data-driven: config.language decides
    // what every label below says.
    await loadLang();
    const v = await rpc.call('system.version', {});
    if (v.raven_version) APP_VERSION = v.raven_version;
    drawFoot();
    /* Absent until system.version carries them; the row simply stays hidden,
       so an older server degrades to no notice rather than a broken one. */
    if (v.update_available) showUpNote('ver', v.latest_version);
    await loadSessions();
    listReady = true;
    drawList();
    /* Home is the new-task screen, never the last session: opening straight
       into someone else's half-finished transcript is a worse first frame
       than an empty composer, and the rail is one click away. A draft writes
       nothing to disk, so this costs no empty session either. */
    startDraft();
    /* First run: the same gate the TUI boots through. setup.status decides;
       the flow itself drives model.options / model.save_key / config.set --
       one server-side setup logic, two faces. Not awaited: the overlay
       resolves on its own while the rest of boot continues underneath.
       ?onboard=1 forces the flow for a design pass on a configured machine.
       Errors leave the gate open (v0.1 fallback, same as the TUI). */
    try {
      const setup = await rpc.call('setup.status', {});
      if (setup.provider_configured === false || /[?&]onboard=1/.test(location.search)) {
        showOnboard({
          options: () => rpc.call('model.options', {}),
          saveKey: (slug, api_key, api_base) => rpc.call('model.save_key', {
            slug,
            ...(api_key ? { api_key } : {}),
            ...(api_base ? { api_base } : {}),
          }),
          setModel: (value, provider) => rpc.call('config.set', { key: 'model', value, ...(provider ? { provider } : {}) }),
          recheck: async () => {
            try { return (await rpc.call('setup.status', {})).provider_configured !== false; }
            catch { return true; }
          },
        });
      }
    } catch (e) {
      if (window.console) console.warn('[live boot] setup.status failed; skipping onboarding gate', e);
    }
    hideSplash();
    shellReady();
    loadSettings().catch(() => {});
    /* Refresh the rail badges from real data right away — until these resolve
       the badges stay suppressed (data-counts="pending") rather than showing
       the demo mock's phantom counts. */
    Promise.allSettled([
      loadExt().then(() => drawCapsBadge()),
      loadCrons().then(() => drawCronBdg()),
    ]).then(() => {
      const r = document.querySelector('.rail');
      if (r) delete r.dataset.counts;
    });
    watchForUpdates();
    resumeUpgrade();
  } catch (e) {
    bootFail(e);
  }
})();

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
  const hd = mk('div', 'hd');
  const pip = mk('span', 'pip');
  const title = mk('span', 't');
  hd.append(pip, title);
  const sub = mk('div', 'sub');
  const el = mk('div', 'el');
  el.hidden = true;
  card.append(hd, sub, el);
  shade.appendChild(card);
  document.body.appendChild(shade);
  let clockTimer = null;
  const stopClock = () => { if (clockTimer) { clearInterval(clockTimer); clockTimer = null; } };
  return {
    /* An install with no visible progress reads as a hang, and this one can
       run for minutes. A running count is the honest signal available: the
       helper reports nothing back until it relaunches serve. */
    clock(t0) {
      stopClock();
      el.hidden = false;
      const paint = () => {
        const s = Math.max(0, Math.round((Date.now() - t0) / 1000));
        const mm = Math.floor(s / 60);
        el.textContent = T('gui.upg.elapsed', { t: `${mm}:${String(s % 60).padStart(2, '0')}` });
      };
      paint();
      clockTimer = setInterval(paint, 1000);
    },
    say(text, detail) {
      title.textContent = text;
      sub.textContent = detail || '';
    },
    /* A failure must leave the reader a way forward, so it always ends with the
       command they can run themselves. */
    fail(text, detail) {
      stopClock();
      el.hidden = true;
      pip.style.animation = 'none';
      pip.style.background = 'var(--clay)';
      title.textContent = text;
      sub.textContent = detail ? `${detail}\n${T('gui.upg.manual')}` : T('gui.upg.manual');
      const cmd = mk('div', 'cmd');
      const code = mk('code', null, 'raven upgrade');
      const cp = mk('button', null, T('gui.dtl.copy'));
      cp.onclick = () => { if (navigator.clipboard) navigator.clipboard.writeText('raven upgrade'); };
      cmd.append(code, cp);
      const foot = mk('div', 'foot');
      const close = mk('button', 'btn', T('gui.upg.close'));
      close.onclick = () => shade.remove();
      foot.appendChild(close);
      card.append(cmd, foot);
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
/* Past this, stop implying it is nearly done and say what is taking so long. */
const UPG_PATIENCE_MS = 90000;

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
  shade.say(T('gui.upg.installing', { v: `v${upLatest || '?'}` }));
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
  shade.say(T('gui.upg.waiting'));
  shade.clock(t0);
  let saidLong = false;
  const tick = async () => {
    const waited = Date.now() - t0;
    if (waited > UPG_CEILING_MS) {
      upMarkClear();
      shade.fail(T('gui.upg.failed'), T('gui.upg.gave_up'));
      return;
    }
    if (waited > UPG_PATIENCE_MS && !saidLong) {
      saidLong = true;
      shade.say(T('gui.upg.waiting'), T('gui.upg.waiting_long'));
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
    shade.say(T('gui.upg.done'));
    setTimeout(() => window.location.reload(), 600);
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

/* -- tabs -------------------------------------------------------------- */

const brNoFav = new Set();  // origins whose /favicon.ico 404'd -- don't re-ask every strip redraw

function brTabStrip() {
  const strip = document.querySelector('#wsBody .btabs');
  if (!strip) return;
  strip.innerHTML = '';
  BR.tabs.forEach((t) => {
    const tab = mk('div', 'btab');
    tab.setAttribute('role', 'tab');
    tab.setAttribute('aria-current', String(!!t.active));
    tab.title = t.title || t.url;
    if (t.loading) tab.appendChild(mk('span', 'ld'));
    else {
      let host = '', origin = '';
      try { const u = new URL(t.url); host = u.host; origin = u.origin; } catch { /* about:blank */ }
      if (host && !brNoFav.has(origin)) {
        const fav = document.createElement('img');
        fav.className = 'fav';
        fav.alt = '';
        fav.src = `${origin}/favicon.ico`;
        fav.onerror = () => {
          brNoFav.add(origin);
          const l = mk('span', 'fav ltr', (host[0] || '?').toUpperCase());
          fav.replaceWith(l);
        };
        tab.appendChild(fav);
      } else if (host) {
        tab.appendChild(mk('span', 'fav ltr', (host[0] || '?').toUpperCase()));
      } else {
        tab.appendChild(mk('span', 'fav ltr', '·'));
      }
    }
    const label = t.title || t.url.replace(/^https?:\/\//, '') || T('gui.br.tab_blank');
    tab.appendChild(mk('span', 'tt', label));
    const x = mk('button', 'bx', '✕');
    x.title = T('gui.br.tab_close');
    x.setAttribute('aria-label', T('gui.br.tab_close'));
    x.onclick = (e) => { e.stopPropagation(); brTabs('close', { index: t.index }); };
    tab.appendChild(x);
    tab.onclick = () => { if (!t.active) brTabs('activate', { index: t.index }); };
    tab.onauxclick = (e) => { if (e.button === 1) { e.preventDefault(); brTabs('close', { index: t.index }); } };
    strip.appendChild(tab);
  });
  const add = mk('button', 'btab-new', '+');
  add.title = T('gui.br.tab_new');
  add.setAttribute('aria-label', T('gui.br.tab_new'));
  add.onclick = () => brTabs('new', {});
  strip.appendChild(add);
}

let brTabsBusy = false;
async function brTabsSync() {
  if (!brShowing() || !BR.started || BR.headful || brTabsBusy) return;
  brTabsBusy = true;
  try {
    const r = await rpc.call('browser.tabs', { action: 'list' });
    BR.tabs = r.tabs || [];
    brTabStrip();
  } catch { /* no browser.tabs here: the strip just stays empty */ }
  brTabsBusy = false;
}

async function brTabs(action, extra) {
  try {
    const r = await rpc.call('browser.tabs', Object.assign({ action }, extra));
    BR.tabs = r.tabs || [];
    if (!r.started) { BR.started = false; BR.lastBlob = null; BR.url = ''; drawWs(); return; }
    if (r.url !== undefined) BR.url = r.url || BR.url;
    brStateIn(r);
    brTabStrip();
    const bar = document.querySelector('#wsBody .bbar .url');
    if (bar && document.activeElement !== bar) bar.value = BR.url;
    if (action === 'new') {
      const u = document.querySelector('#wsBody .bbar .url');
      if (u) { u.focus(); u.select(); }
    }
    brWatch(true);
    /* A static page repaints nothing after a tab switch, so the stream has no
       frame to push -- pull one so the stage shows the tab we just went to. */
    if (action === 'activate' || action === 'new') brPoll(true);
  } catch (e) { BR.err = e.message || String(e); }
}

function brTabTick(on) {
  if (BR.tabTimer) { clearInterval(BR.tabTimer); BR.tabTimer = null; }
  if (on) BR.tabTimer = setInterval(brTabsSync, 2500);
}

/* The viewport tracks the stage, so this is normally 1:1 -- the math only
   earns its keep in the beat between a resize and the restream, when the
   frame is letterboxed (object-fit: contain) inside the new stage. */
function brToPage(stage, e) {
  const r = stage.getBoundingClientRect();
  const vw = BR.vp[0] || r.width || 1;
  const vh = BR.vp[1] || r.height || 1;
  const s = Math.min(r.width / vw, r.height / vh) || 1;
  const ox = r.left + (r.width - vw * s) / 2;
  const oy = r.top + (r.height - vh * s) / 2;
  return {
    x: Math.round(Math.min(vw, Math.max(0, (e.clientX - ox) / s))),
    y: Math.round(Math.min(vh, Math.max(0, (e.clientY - oy) / s))),
  };
}

function brInput(payload) {
  rpc.call('browser.input', payload).catch((e) => { BR.err = e.message || String(e); });
  /* Pushed frames show the result on their own; only the poll fallback needs
     to go and look. */
  if (BR.noWatch) setTimeout(() => brPoll(true), 350);
}

drawWsWeb = function (box) {
  if (BR.avail === null) { brPoll(true).then(() => { if (brShowing()) drawWs(); }); }

  /* Absent is not the same as uninstalled, and the difference is the whole
     point of telling the reader anything: chromium is worth an install command,
     a server without the surface is not -- following that command would change
     nothing. */
  if (!rpcHas('browser')) {
    const n = mk('div', 'bnote');
    n.append(mk('div', 'h', T('gui.br.absent_h')), mk('div', 'w', T('gui.br.absent')));
    box.appendChild(n);
    return;
  }

  if (BR.avail === false) {
    const n = mk('div', 'bnote');
    n.append(mk('div', 'h', T('gui.br.unavail')), mk('div', 'w', T('gui.br.unavail_w')));
    n.appendChild(mk('code', null, 'uv sync --extra browser && uv run playwright install chromium'));
    if (BR.reason) n.appendChild(mk('div', 'w', BR.reason));
    box.appendChild(n);
    return;
  }

  if (BR.started && !BR.headful) {
    box.appendChild(mk('div', 'btabs'));
    brTabStrip();
    brTabsSync();
    brTabTick(true);
  } else {
    brTabTick(false);
  }

  const bar = mk('div', 'bbar');
  const nav = mk('div', 'nav');
  const navBtn = (cls, glyph, key, fn, rotate) => {
    const b = mk('button', 'ghost-ic ' + cls);
    const g = ico(glyph);
    if (rotate) g.style.transform = 'rotate(180deg)';
    b.appendChild(g);
    b.title = T(key);
    b.setAttribute('aria-label', T(key));
    b.onclick = fn;
    nav.appendChild(b);
    return b;
  };
  const back = navBtn('bk', ICO.up, 'gui.br.back', () => brGo('back'));
  const fwd = navBtn('fw', ICO.up, 'gui.br.forward', () => brGo('forward'), true);
  back.disabled = !BR.started || !BR.canBack;
  fwd.disabled = !BR.started || !BR.canFwd;
  const rl = navBtn(
    'brl',
    BR.loading ? 'M6 6l12 12M18 6L6 18' : 'M4.5 12a7.5 7.5 0 1 0 2.6-5.7M4.5 5.5V10h4.5',
    BR.loading ? 'gui.br.stop' : 'gui.br.reload',
    () => brGo(BR.loading ? 'stop' : 'reload'),
  );
  rl.disabled = !BR.started;
  bar.appendChild(nav);

  /* Address field: security glyph inside, select-all on focus, Esc restores,
     and Enter routes -- URL-shaped input navigates, anything else searches. */
  const wrap = mk('span', 'burl');
  const sec = mk('span', 'sec');
  wrap.appendChild(sec);
  const url = mk('input', 'url');
  url.type = 'text';
  url.value = BR.url;
  url.placeholder = T('gui.br.url_ph');
  url.setAttribute('aria-label', T('gui.br.url_ph'));
  url.autocomplete = 'off';
  url.spellcheck = false;
  url.onfocus = () => url.select();
  url.onkeydown = (e) => {
    if (e.key === 'Escape') { url.value = BR.url; url.blur(); return; }
    if (e.key !== 'Enter') return;
    e.preventDefault();
    const v = url.value.trim();
    if (!v) return;
    const urlish = /^[a-z][a-z0-9+.-]*:\/\//i.test(v)
      || (!/\s/.test(v) && (/^localhost(:\d+)?([/?#]|$)/i.test(v) || /^[\w-]+(\.[\w-]+)+/.test(v) || /^\d{1,3}(\.\d{1,3}){3}/.test(v)));
    const search = LANG === 'zh'
      ? `https://www.baidu.com/s?wd=${encodeURIComponent(v)}`
      : `https://duckduckgo.com/?q=${encodeURIComponent(v)}`;
    brOpen(urlish ? v : search);
    url.blur();
  };
  wrap.appendChild(url);
  bar.appendChild(wrap);

  const prog = mk('div', 'bprog');
  prog.appendChild(mk('i'));
  prog.hidden = !BR.loading;
  bar.appendChild(prog);

  if (BR.started && !BR.headful) {
    const pop = mk('button', 'ghost-ic');
    pop.appendChild(ico('M9 5H5v14h14v-4M14 4h6v6M20 4l-9 9'));
    pop.title = T('gui.br.popout');
    pop.setAttribute('aria-label', T('gui.br.popout'));
    pop.onclick = async () => {
      brWatch(false);
      try { const r = await rpc.call('browser.mode', { headful: true }); BR.headful = !!r.headful; } catch { /* poll reports it */ }
      drawWs();
    };
    bar.appendChild(pop);
  }
  if (BR.started) {
    const x = mk('button', 'ghost-ic');
    x.appendChild(ico('M6 6l12 12M18 6L6 18'));
    x.title = T('gui.br.close');
    x.setAttribute('aria-label', T('gui.br.close'));
    x.onclick = async () => {
      await rpc.call('browser.close', {});
      BR.started = false; BR.lastBlob = null; BR.url = '';
      drawWs();
    };
    bar.appendChild(x);
  }
  box.appendChild(bar);

  /* Popped out: the page lives in a real Chromium window. The panel keeps the
     address bar working and offers the way back; streaming a window the
     reader can already see would just be a second, worse copy of it. */
  if (BR.started && BR.headful) {
    brTick(true);
    const n = mk('div', 'bnote');
    n.append(mk('div', 'h', T('gui.br.popped')), mk('div', 'w', T('gui.br.popped_w')));
    const back = mk('button', 'btn');
    back.textContent = T('gui.br.popin');
    back.onclick = async () => {
      try { const r = await rpc.call('browser.mode', { headful: false }); BR.headful = !!r.headful; } catch { /* poll reports it */ }
      drawWs();
    };
    n.appendChild(back);
    box.appendChild(n);
    return;
  }

  if (!BR.started) {
    /* Poll cheaply until a page exists -- the agent may open one any moment,
       and the flip to started is what swaps this note for the live stage. */
    brTick(true);
    const n = mk('div', 'bnote');
    n.append(mk('div', 'h', T('gui.br.idle')), mk('div', 'w', T('gui.br.idle_w')));
    if (BR.err) n.appendChild(mk('div', 'w', BR.err));
    /* The pages the agent already fetched are the likeliest thing to want
       open, so the old link list stays -- as a starting point, not the view. */
    if (WS.urls.length) {
      const list = mk('div', 'urls');
      WS.urls.slice(0, 8).forEach((u) => {
        const r = mk('button', 'urow');
        r.appendChild(ico(ICO.web));
        r.appendChild(mk('span', 'u', u.url.replace(/^https?:\/\//, '')));
        r.title = u.url;
        r.onclick = () => brOpen(u.url);
        list.appendChild(r);
      });
      n.appendChild(list);
    }
    box.appendChild(n);
    return;
  }

  /* Live: the stage owns the rest of the panel (flex column, no scroll) and
     the page's viewport is resized to the stage, so pointer geometry is 1:1. */
  box.dataset.view = 'web';
  const stage = mk('div', 'bstage');
  const img = mk('canvas', 'shot');
  img.width = BR.vp[0]; img.height = BR.vp[1];
  img.setAttribute('role', 'img');
  img.setAttribute('aria-label', BR.title || BR.url);

  /* Keystrokes land in an invisible input, not on the stage div: that is what
     lets an IME compose (Chinese input has no keydown spelling), and the
     composed string is forwarded whole. */
  const kb = mk('input', 'kbsink');
  kb.type = 'text';
  kb.autocapitalize = 'off';
  kb.autocomplete = 'off';
  kb.spellcheck = false;
  kb.setAttribute('aria-hidden', 'true');
  kb.tabIndex = -1;
  let composing = false;
  kb.oncompositionstart = () => { composing = true; };
  kb.oncompositionend = (e) => {
    composing = false; kb.value = '';
    if (e.data) brInput({ kind: 'text', text: e.data });
  };
  kb.oninput = () => {
    if (composing) return;
    const v = kb.value; kb.value = '';
    if (v) brInput({ kind: 'text', text: v });
  };
  kb.onkeydown = (e) => {
    if (composing) return;
    if (e.metaKey || e.ctrlKey) {
      const k = e.key.toLowerCase();
      /* The browser's own chrome shortcuts, same keys as the native thing:
         L focuses the address bar, R reloads, T/W manage tabs, [ ] walk
         history. Everything else editing-shaped belongs to the page. */
      if (k === 'l') {
        e.preventDefault();
        const u = document.querySelector('#wsBody .bbar .url');
        if (u) { u.focus(); u.select(); }
        return;
      }
      if (k === 'r') { e.preventDefault(); brGo(BR.loading ? 'stop' : 'reload'); return; }
      if (k === 't') { e.preventDefault(); brTabs('new', {}); return; }
      if (k === 'w') {
        e.preventDefault();
        const act = BR.tabs.find((t) => t.active);
        if (act) brTabs('close', { index: act.index });
        return;
      }
      if (k === '[') { e.preventDefault(); brGo('back'); return; }
      if (k === ']') { e.preventDefault(); brGo('forward'); return; }
      /* Editing shortcuts belong to the page (select-all in its text field,
         not ours). Paste is the exception: letting Cmd+V land in the sink
         turns it into an input event, which is already the IME text path. */
      if (k.length === 1 && 'aczyx'.includes(k)) {
        e.preventDefault();
        brInput({ kind: 'key', key: 'ControlOrMeta+' + (e.shiftKey ? 'Shift+' : '') + k.toUpperCase() });
      }
      return;
    }
    if (e.key.length === 1) return;
    e.preventDefault();
    const mods = [];
    if (e.shiftKey) mods.push('Shift');
    if (e.altKey) mods.push('Alt');
    brInput({ kind: 'key', key: mods.concat(e.key).join('+') });
  };
  kb.onfocus = () => stage.classList.add('hot');
  kb.onblur = () => stage.classList.remove('hot');

  const btnOf = (e) => (e.button === 2 ? 'right' : e.button === 1 ? 'middle' : 'left');
  stage.onpointerdown = (e) => {
    e.preventDefault();
    try { stage.setPointerCapture(e.pointerId); } catch { /* gone mid-gesture */ }
    kb.focus({ preventScroll: true });
    brInput(Object.assign({ kind: 'down', button: btnOf(e), count: e.detail || 1 }, brToPage(stage, e)));
  };
  stage.onpointerup = (e) => {
    brInput(Object.assign({ kind: 'up', button: btnOf(e), count: e.detail || 1 }, brToPage(stage, e)));
  };
  /* Moves coalesce per animation frame -- the newest position wins -- instead
     of a fixed 30ms gate that adds up to a visible rubber-band on drags. */
  let mvPend = null, mvRaf = 0;
  stage.onpointermove = (e) => {
    mvPend = brToPage(stage, e);
    if (mvRaf) return;
    mvRaf = requestAnimationFrame(() => {
      mvRaf = 0;
      if (mvPend) { brInput(Object.assign({ kind: 'move' }, mvPend)); mvPend = null; }
    });
  };
  /* The remote page's own menu is not reachable from here, and the window's
     right-click rule already opens nothing over a canvas that declares no
     actions -- so this surface needs no handler of its own. */
  /* Raw deltas, not rounded: trackpad momentum is made of fractional steps,
     and rounding them is what makes scrolling feel notchy.

     The canvas also shifts locally on the spot -- the exposed band is a smear
     of the edge row until Chromium's next frame lands and repaints the truth.
     That round-trip is exactly the lag that makes a streamed page feel like
     it is dragging; scrolling is the one gesture whose visual result is
     predictable enough to fake for a frame or two. */
  stage.onwheel = (e) => {
    e.preventDefault();
    brInput({ kind: 'wheel', dx: e.deltaX, dy: e.deltaY });
    const cv = stage.querySelector('canvas.shot');
    if (!cv || !cv.width || !BR.vp[1]) return;
    const d = Math.max(-cv.height, Math.min(cv.height, Math.round(e.deltaY * (cv.height / BR.vp[1]))));
    if (!d) return;
    const c2 = cv.getContext('2d'); const w = cv.width; const h = cv.height; const a = Math.abs(d);
    if (d > 0) {
      c2.drawImage(cv, 0, a, w, h - a, 0, 0, w, h - a);
      c2.drawImage(cv, 0, h - 1, w, 1, 0, h - a, w, a);
    } else {
      c2.drawImage(cv, 0, 0, w, h - a, 0, a, w, h - a);
      c2.drawImage(cv, 0, 0, w, 1, 0, 0, w, a);
    }
  };

  stage.appendChild(img);
  stage.appendChild(kb);
  if (BR.lastBlob) brPaint(BR.lastBlob);
  else stage.appendChild(mk('div', 'waiting', T('gui.br.waiting')));
  /* A navigation that failed gets a page, not a status-line mumble. */
  if (BR.err) {
    const err = mk('div', 'berr');
    err.append(mk('div', 'h', T('gui.br.error')), mk('div', 'w', BR.err));
    const retry = mk('button', 'mini gold', T('gui.plug.retry'));
    retry.onclick = () => { BR.err = ''; if (BR.lastTried) brOpen(BR.lastTried); else drawWs(); };
    const dismiss = mk('button', 'mini ghost', T('gui.cancel'));
    dismiss.onclick = () => { BR.err = ''; drawWs(); };
    const row = mk('div');
    row.style.cssText = 'display:flex;gap:8px';
    row.append(retry, dismiss);
    err.appendChild(row);
    stage.appendChild(err);
  }
  box.appendChild(stage);
  brSecSync();

  brWatch(true);
  /* A dragged seam or window resize means a new viewport: re-lease with the
     new size once it settles, and the server restreams at that size. */
  if (!BR.ro) {
    BR.ro = new ResizeObserver(() => {
      clearTimeout(BR.rsz);
      BR.rsz = setTimeout(() => { if (brShowing() && BR.started) brWatch(true); }, 250);
    });
  }
  BR.ro.disconnect();
  BR.ro.observe(stage);
};

/* A link in the transcript opens in the embedded browser -- the panel IS this
   app's browser, same as the built-in viewer is its file opener. Modifier
   clicks (Cmd/Ctrl/Shift/Alt) keep the system-browser escape hatch.

   Only when this server has a browser to open it with. Cancelling the
   navigation first and finding out afterwards is how a plain click on a link
   in a reply became a click that does nothing: the panel opened, `browser.open`
   answered -32601, and the reader was left with a parked error. Unknown counts
   as absent here -- a link opening in the real browser is a worse outcome than
   nothing only if you already know the panel works. */
document.addEventListener('click', (e) => {
  if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  if (!rpcHas('browser') || BR.avail !== true) return;
  const a = e.target && e.target.closest ? e.target.closest('#scroll a[href]') : null;
  if (!a || !/^https?:/i.test(a.href)) return;
  e.preventDefault();
  if (!wsOpen) setWs(true);
  wsPick('browser');
  brOpen(a.href);
}, true);

/* ── subagents: what this conversation handed off ──────────────────────
   Scoped to the open session on every call, so background work from another
   conversation can never surface here. */
let agentsBusy = false;
let agentsAt = 0;
/* One fingerprint per drawn list, for the same reason the detail keeps one: the
   poll answers every few seconds whether or not anything moved, and drawWs()
   wipes the panel body to rebuild it. Redrawing an unchanged list is a list that
   flickers on a timer and loses the reader's scroll every time. */
let agentsDrawn = '';
agentsLoad = () => {
  /* The panel asks on each redraw and a fresh answer causes one; the floor
     keeps that from spinning, and doubles as the poll's rate limit. */
  if (!cur || agentsBusy || Date.now() - agentsAt < 2500) return;
  /* A server without the surface answers -32601 every five seconds forever
     otherwise, and the panel sits empty with no way to tell an empty list from
     a missing feature. */
  if (!rpcHas('subagent')) return;
  agentsBusy = true;
  const asked = cur;
  rpc.call('subagent.list', { session_id: asked })
    .then((r) => {
      /* Answer for a conversation the reader already left: dropping it is the
         difference between a stale list and somebody else's list. */
      if (asked !== cur) return;
      AGENTS = (r && r.items) || [];
      const dot = $('#wsAgentRun');
      if (dot) dot.hidden = !AGENTS.some((a) => a.status === 'run');
      /* Keyed by conversation as well as content, so switching between two
         sessions that have listed the same thing still repaints. */
      const drawn = `${asked}|${JSON.stringify(AGENTS)}`;
      if (drawn === agentsDrawn) return;
      agentsDrawn = drawn;
      if (wsOpen && wsTab === 'agents' && !agentOpen && !dagNode) drawWs();
    })
    .catch((e) => { rpcGone('subagent', e); /* an empty list is not a broken one */ })
    .then(() => { agentsBusy = false; agentsAt = Date.now(); });
};

/* One fingerprint per drawn detail: the poll repaints only when the answer
   actually changed, because a redraw every tick reads as flicker and eats any
   text selection the reader had. */
/* What has already been drawn for the open run. `drawn` counts *messages*, not
   nodes: a poll appends the ones that arrived since the last, and never touches
   what is on screen. Rebuilding the whole transcript every five seconds -- which
   is what a running record's poll used to do -- tore out whatever the reader was
   in the middle of, closed every fold they had opened, and dropped the selection.
   The scroll survived it and nothing else did. */
const agentDrawn = { key: null, drawn: 0, status: null };

function agentPaint(box, r, opts) {
  const msgs = (r && r.messages) || [];
  const running = r && r.status === 'run';
  const key = (opts && opts.key) || `sp:${agentOpen}`;
  const fresh = agentDrawn.key !== key;
  if (fresh) { box.innerHTML = ''; agentDrawn.key = key; agentDrawn.drawn = 0; }
  agentDrawn.status = r && r.status;

  /* An answer being streamed is held back until it settles: drawing it is what
     would force a redraw of it a moment later, and it is why an answer in
     flight carries no footer here, exactly as a live turn in the transcript
     carries none until it settles.
     Only an *assistant* message though. A user prompt is never rewritten, and
     for most of a run it is the only message there is -- holding it back drew
     the working glyph over an empty panel, so a reader watching a run could
     not tell which run they were watching. */
  const last = msgs[msgs.length - 1];
  const streaming = running && last && last.role === 'assistant';
  const commit = streaming ? msgs.length - 1 : msgs.length;
  const tail = msgs.slice(agentDrawn.drawn, commit);

  const sc = box;
  const atEnd = sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 4;
  const top = sc.scrollTop;

  /* The working glyph is the tail of the box, so it is kept aside while
     messages are appended and put back after -- kept, not rebuilt: re-creating
     it restarts its CSS animation, and this paint runs on every poll, so a
     fresh glyph each time is a glyph that never finishes a cycle. The turn's
     own live row learned this first; see `drawTurnLive`. */
  const glyph = box.querySelector(':scope > .sarun');
  const empty = box.querySelector(':scope > .wsempty');
  if (empty) empty.remove();

  /* What it did, between what was asked and what came back -- only for the
     lanes whose record holds call names and nothing else; a real transcript
     carries its own rows. It is a whole-record shape, so it is drawn once, on
     the first paint. */
  const did = fresh ? agentFlatCalls(r) : [];
  if (tail.length || did.length) {
    /* The one line that makes this a subagent view rather than a second
       renderer: point the transcript's write target at this box, draw with
       the ordinary one, then put it back. Synchronous, so no live event can
       land in between. */
    const prev = stageHost;
    stageHost = box;
    try {
      if (did.length) {
        renderHistory(tail.filter((m) => m && m.role === 'user'));
        agentDidStep(did);
        renderHistory(tail.filter((m) => !(m && m.role === 'user')));
        agentFoldTime(box, r);
      } else {
        renderHistory(tail);
      }
    } finally { stageHost = prev; }
    agentDrawn.drawn = commit;
  }

  if (running) {
    /* The glyph alone, as the reader's own turn wears it. The word beside it
       ("Working") named the one thing the moving glyph already says, in a panel
       whose header carries the status too -- three ways of saying running, and
       the only one that survives a glance is the movement. */
    let w = glyph;
    if (!w) {
      w = mk('div', 'act in sarun');
      w.append(workGlyph());
    }
    if (box.lastElementChild !== w) box.appendChild(w);
  } else if (glyph) {
    glyph.remove();
  }
  if (!box.childElementCount) {
    box.appendChild(mk('div', 'wsempty', (opts && opts.empty) || T('gui.ws.agents_none')));
  }
  box.scrollTop = atEnd ? box.scrollHeight : top;
}

agentRender = (box, id) => {
  /* Which panel asked. The header below is reached through the document rather
     than through `box`, so an answer for a run the reader has already left
     would find whichever header is open now and rename it. The rest of this
     callback is safe on its own -- it writes into a detached box -- but a
     write to the live document has to know it is still the right document. */
  const epoch = wsEpoch;
  /* A call is addressed by conversation and call, not by call alone: its record
     lives inside that conversation's own directory. */
  agentDrawn.key = null;
  rpc.call('subagent.context', { id, session_id: cur })
    .then((r) => {
      if (wsStale(epoch)) return;
      /* The header drew from the listed row. Opened without one -- a reopened
         panel, a run that has aged out of the list -- it fell back to raven's
         own sub-agent, which would quietly mislabel an openclaw run. The answer
         carries the truth, so correct it on arrival. */
      const who = document.querySelector('.sahd .trow .who');
      if (who && r) who.textContent = (r.agent || 'raven');
      agentPaint(box, r);
    })
    .catch((e) => {
      box.innerHTML = '';
      box.appendChild(mk('div', 'wsempty', (e && e.message) || String(e)));
    });
};

/* A run in flight has to move on screen without being reopened, and its header
   has to change the moment the run does -- a detail page still saying "working"
   over a run the list already knows failed is the panel lying. */
setInterval(() => {
  if (!wsOpen || wsTab !== 'agents') return;
  if (!agentOpen && !dagNode) { agentsLoad(); return; }
  agentsAt = 0;
  agentsLoad();
  /* A graph node is a row like any other now, so the same rule applies. It is
     found by (run, node) rather than by id because that is how the list
     addresses it. */
  const it = dagNode
    ? AGENTS.find((a) => a.kind === 'dag' && a.run_id === dagNode.run_id && a.node === dagNode.node)
    : AGENTS.find((a) => a.id === agentOpen);
  const stNow = it ? it.status : null;
  if (agentDrawn.status && stNow && agentDrawn.status !== stNow) {
    /* Status flipped under an open page: redraw the whole view once, so the
       header mark and the transcript land on the final state together. */
    agentDrawn.status = stNow;
    drawWs();
    return;
  }
  if (!it || it.status !== 'run') return;
  const box = document.querySelector('.satx');
  if (!box) return;
  if (dagNode) { dagNodePaint(box, dagNode); return; }
  rpc.call('subagent.context', { id: agentOpen, session_id: cur })
    .then((r) => {
      if (!document.body.contains(box)) return;
      agentPaint(box, r);
    })
    .catch(() => {});
}, 2000);

/* Leaving the browser view -- another tab, another session, the panel shut --
   must drop the watch, and every one of those paths repaints the panel. */
const drawWsBase = drawWs;
drawWs = function () {
  const body = document.querySelector('#wsBody');
  if (body) delete body.dataset.view;
  drawWsBase();
  if (!brShowing()) { brWatch(false); brTick(false); }
};

/* -- external agents --------------------------------------------------
   `subagents.*` is one surface shared with the TUI and the web UI: the rows,
   the install grouping and the write path all live server-side, so this layer
   only maps a row into what the page draws and sends the mutation back.

   The list is re-fetched after every mutation rather than patched locally: the
   handler recomputes `group`, `enabled` and the probe verdict together, and a
   client that guesses any one of them is how the page starts disagreeing with
   the config on disk. `probe: false` on that follow-up call skips the
   availability check, which can cost up to ten seconds per entry and would only
   re-measure what the write just changed. */
function xaRowOf(r) {
  return {
    name: r.name,
    preset: r.preset,
    kind: r.kind || 'cli',
    configured: !!r.configured,
    enabled: !!r.enabled,
    probe_status: r.probe_status || 'unknown',
    upgrade_to: r.upgrade_to || null,
    probe_detail: r.probe_detail || '',
    has_api_key: !!r.has_api_key,
    description: r.description || '',
    last_test_ok: r.last_test_ok,
    last_test_detail: r.last_test_detail || '',
    last_test_at_ms: r.last_test_at_ms || null,
    test_running: !!r.test_running,
  };
}

async function xaFetch(probe) {
  const res = await rpc.call('subagents.list', { probe: !!probe });
  /* A probe-less list reports every row as "unknown", which would blank the
     health line of a row that was ready a second ago -- connecting an agent
     would look like it broke it. The verdict cannot have changed by writing
     config, so the last known one is carried over. */
  const was = new Map(XAGENTS.map((a) => [a.name, a]));
  XAGENTS.length = 0;
  (res.rows || []).forEach((r) => {
    const row = xaRowOf(r);
    const prev = was.get(row.name);
    if (row.probe_status === 'unknown' && prev && prev.probe_status !== 'unknown') {
      row.probe_status = prev.probe_status;
      row.probe_detail = prev.probe_detail;
    }
    XAGENTS.push(row);
  });
  return XAGENTS;
}

xaLoad = () => xaFetch(true);

xaAct = async function (op, row, args) {
  const a = args || {};
  if (op === 'probe') {
    xaProbing = true;
    drawXa();
    try { await xaFetch(true); } finally { xaProbing = false; }
    return;
  }
  if (op === 'connect') {
    /* Only name / description / key travel: every execution field comes from
       the preset server-side. A page that could post a command line would make
       "which agent is this" unanswerable. */
    await rpc.call('subagents.add', {
      preset: row.preset || row.name,
      name: a.new_name || undefined,
      description: a.description || undefined,
      api_key: a.api_key || undefined,
    });
  } else if (op === 'update') {
    await rpc.call('subagents.update', {
      name: row.name,
      new_name: a.new_name && a.new_name !== row.name ? a.new_name : undefined,
      description: a.description,
      api_key: a.api_key || undefined,
    });
  } else if (op === 'toggle') {
    await rpc.call('subagents.toggle', { name: row.name, enabled: !!a.enabled });
  } else if (op === 'remove') {
    await rpc.call('subagents.remove', { name: row.name });
  } else if (op === 'upgrade') {
    /* There is no "change the transport" write: `subagents.update` touches name,
       description and key only, on purpose. So the switch is a remove plus an add
       from the preset, which is also what makes it visible in config as one
       entry replaced rather than an entry mutated underneath its session handles. */
    await rpc.call('subagents.remove', { name: row.name });
    await rpc.call('subagents.add', {
      preset: row.preset || row.name,
      name: row.name,
      description: row.description || undefined,
    });
  } else if (op === 'test_cancel') {
    await rpc.call('subagents.test_cancel', { name: row.name });
  } else if (op === 'test') {
    /* The call runs the agent for real and does not return until it answers, so
       the row is marked running first and the page redrawn -- otherwise the
       button looks dead for the length of a model turn. */
    row.test_running = true;
    drawXa();
    try {
      const res = await rpc.call('subagents.test', { name: row.name, source: row.configured ? 'config' : 'preset' });
      row.last_test_ok = res.cancelled ? row.last_test_ok : !!res.ok;
      row.last_test_detail = res.detail || '';
      if (!res.cancelled) row.last_test_at_ms = Date.now();
      if (!res.ok && res.detail && !res.cancelled) toast(`${row.name}: ${res.detail}`);
    } finally {
      row.test_running = false;
    }
    /* probe:true here, unlike every other mutation: a test is the one write that
       changes the probe verdict. An acp test records the capability snapshot the
       probe reads, so carrying the old "not recorded yet" over would leave the
       row telling the user to run the test they just ran. */
    await xaFetch(true);
    return;
  }
  await xaFetch(false);
};

/* ── the dag sheet: what a `run_subagent_dag` call is orchestrating ────────
   The transcript only ever shows this call as one tool row with a clamped
   result, and the three `dag.*` events that describe the graph arrived here and
   were dropped on the floor. They are the whole picture of the work, so they get
   the same place the clarify sheet gets: above the composer, on the turn being
   worked on rather than buried in the scrollback. */
const DAGS = new Map();  // session key -> the graph that conversation is running
const dagFor = (key) => DAGS.get(key || sheetSession()) || null;

/* Wider and taller than the first pass: the box now carries a status mark as
   well as the node's id, the agent under it and a clock, and 108x38 had them
   touching each other. GAP_X leaves 46px of edge between columns, which is
   enough for a curve to read as a curve rather than as a kink. */
const DAG_GAP_X = 182;
const DAG_GAP_Y = 60;
const DAG_W = 136;
const DAG_H = 44;
const DAG_PAD = 10;

/* Depth by longest path, which is what puts a node in the column after the last
   thing it waits for. Memoised, and guarded against a cycle it should never see:
   the server rejects a cyclic graph before running it, but a panel that hangs is
   a worse way to find that out than a panel that draws something odd. */
function dagDepths(nodes) {
  const by = new Map(nodes.map((n) => [n.id, n]));
  const depth = new Map();
  const walking = new Set();
  const of = (id) => {
    if (depth.has(id)) return depth.get(id);
    const n = by.get(id);
    const deps = (n && n.depends_on) || [];
    if (!n || !deps.length || walking.has(id)) { depth.set(id, 0); return 0; }
    walking.add(id);
    let d = 0;
    deps.forEach((p) => { if (by.has(p)) d = Math.max(d, of(p) + 1); });
    walking.delete(id);
    depth.set(id, d);
    return d;
  };
  nodes.forEach((n) => of(n.id));
  return depth;
}

/* One sentence for a reader who does not want to read a graph: how much work,
   how deep, who is doing it, and whether anything actually runs side by side. */
function dagGist(d) {
  const nodes = d.order.map((id) => d.nodes.get(id));
  const depth = dagDepths(nodes);
  const layers = new Map();
  nodes.forEach((n) => {
    const k = depth.get(n.id) || 0;
    layers.set(k, (layers.get(k) || 0) + 1);
  });
  const widest = Math.max(...layers.values(), 1);
  const agents = [...new Set(nodes.map((n) => n.subagent).filter(Boolean))];
  const bits = [T('gui.dag.count').replace('{n}', String(nodes.length)).replace('{d}', String(layers.size))];
  bits.push(widest > 1 ? T('gui.dag.parallel').replace('{n}', String(widest)) : T('gui.dag.serial'));
  if (agents.length) bits.push(agents.join(' · '));
  return bits.join(' · ');
}

const SVG_NS = 'http://www.w3.org/2000/svg';

function svgEl(tag, attrs, cls) {
  const e = document.createElementNS(SVG_NS, tag);
  Object.entries(attrs || {}).forEach(([k, v]) => e.setAttribute(k, String(v)));
  if (cls) e.setAttribute('class', cls);
  return e;
}

/* The one mark a node wears, and the whole of how status is drawn. It used to be
   the box's own stroke colour plus a pulse of the entire node's opacity: with
   nothing but colour to go by, a reader had to already know the palette, and a
   node fading in and out as a whole read as an error rather than as work. Colour
   stays, quietly, on the border; this is what a reader actually looks at.

   Running is the same three bars the turn's own row and a sub-agent row wear
   (workGlyphSvg), which is the point -- one glyph for work in progress, whatever
   is doing the work. */
function dagMark(status, cx, cy) {
  if (status === 'running') return workGlyphSvg(cx - 5, cy + 4);
  if (status === 'completed') return svgEl('path', { d: `M${cx - 5} ${cy}l3.6 3.8 6.4 -7.6` }, 'mk ok');
  if (status === 'failed') return svgEl('path', { d: `M${cx - 4} ${cy - 4}l8 8M${cx + 4} ${cy - 4}l-8 8` }, 'mk bad');
  if (status === 'skipped' || status === 'interrupted') {
    return svgEl('path', { d: `M${cx - 4.5} ${cy}h9` }, 'mk skip');
  }
  return svgEl('circle', { cx, cy, r: 3.6 }, 'mk wait');
}

/* Column x, row y, and the sizes that follow from them. Each column is centred
   on the graph's own midline rather than stacked from the top: a fan-out into
   three and a fan-in back to one then reads as the diamond it is, instead of a
   staircase whose single nodes sit against the ceiling with their edges cutting
   diagonally down. */
function dagLayout(nodes) {
  const depth = dagDepths(nodes);
  const cols = new Map();
  nodes.forEach((n) => {
    const c = depth.get(n.id) || 0;
    if (!cols.has(c)) cols.set(c, []);
    cols.get(c).push(n);
  });
  const tallest = Math.max(...[...cols.values()].map((c) => c.length), 1);
  const height = DAG_PAD * 2 + tallest * DAG_H + (tallest - 1) * (DAG_GAP_Y - DAG_H);
  const width = DAG_PAD * 2 + (cols.size - 1) * DAG_GAP_X + DAG_W;
  const at = new Map();
  cols.forEach((column, c) => {
    const span = column.length * DAG_H + (column.length - 1) * (DAG_GAP_Y - DAG_H);
    const top = (height - span) / 2;
    column.forEach((n, i) => {
      at.set(n.id, { x: DAG_PAD + c * DAG_GAP_X, y: top + i * DAG_GAP_Y });
    });
  });
  return { at, width, height };
}

function dagSvg(d) {
  const nodes = d.order.map((id) => d.nodes.get(id)).filter(Boolean);
  const { at, width, height } = dagLayout(nodes);
  const svg = svgEl('svg', { width, height, viewBox: `0 0 ${width} ${height}` });

  /* Edges first so a box always sits on top of the line reaching it. An edge out
     of a node that has not finished is drawn faint: what has actually flowed
     through the graph so far is the thing a reader is trying to see. */
  nodes.forEach((n) => {
    (n.depends_on || []).forEach((pid) => {
      const a = at.get(pid);
      const b = at.get(n.id);
      if (!a || !b) return;
      const x1 = a.x + DAG_W;
      const y1 = a.y + DAG_H / 2;
      const x2 = b.x - 5;
      const y2 = b.y + DAG_H / 2;
      const mid = (x1 + x2) / 2;
      const done = (d.nodes.get(pid) || {}).status === 'completed';
      // Which node this edge leaves, so a later status change can darken it in
      // place instead of costing the graph a redraw.
      svg.appendChild(svgEl(
        'path',
        { d: `M${x1} ${y1} C${mid} ${y1} ${mid} ${y2} ${x2} ${y2}`, 'data-from': pid },
        'edge' + (done ? ' flowed' : ''),
      ));
      // The head, drawn separately: a marker-end would inherit the path's own
      // stroke width and end up heavier than the line it caps.
      svg.appendChild(svgEl(
        'path',
        { d: `M${x2 - 3.5} ${y2 - 3}L${x2 + 1} ${y2}l-4.5 3`, 'data-from': pid },
        'tip' + (done ? ' flowed' : ''),
      ));
    });
  });

  d.els = new Map();
  nodes.forEach((n) => {
    const p = at.get(n.id);
    const g = svgEl('g', { transform: `translate(${p.x} ${p.y})`, role: 'button', tabindex: '0' }, 'nd');
    g.dataset.st = n.status || 'pending';
    g.dataset.node = n.id;
    if (dagNode && dagNode.run_id === d.run_id && dagNode.node === n.id) g.dataset.sel = '1';
    g.appendChild(svgEl('rect', { width: DAG_W, height: DAG_H, rx: 9 }));
    const mark = dagMark(n.status, 17, DAG_H / 2);
    g.appendChild(mark);
    const id = svgEl('text', { x: 31, y: 19 }, 'id');
    id.textContent = n.id;
    const ag = svgEl('text', { x: 31, y: 32 }, 'ag');
    ag.textContent = n.subagent + (n.instance ? ' @' + n.instance : '');
    // Always present, even while empty: the clock below writes into it every
    // second, and a node that starts running must not have to be redrawn to
    // grow somewhere to put its time.
    const tm = svgEl('text', { x: DAG_W - 11, y: 19, 'text-anchor': 'end' }, 'tm');
    tm.textContent = dagTook(n);
    g.append(id, ag, tm);
    d.els.set(n.id, { g, tm, mark });
    const open = () => dagOpenNode(d.run_id, n);
    g.onclick = open;
    g.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } };
    // The graph is a map, not a table: what a node is and who is running it are
    // in the box, and how long is on the title for the one being pointed at.
    const title = svgEl('title');
    title.textContent = `${n.id} · ${n.subagent}${n.instance ? ' @' + n.instance : ''}`;
    g.appendChild(title);
    svg.appendChild(g);
  });
  return svg;
}

function dagTook(n) {
  if (!n.started_at) return '';
  const end = n.ended_at || Date.now();
  return dur(Math.max(end - n.started_at, 1000));
}

/* ── the clock on a running node ──────────────────────────────────────────
   Only two events ever arrive for a node: it started, and it ended. Between
   them nothing is sent, so a number drawn once sat frozen for exactly the
   interval a reader is watching it for -- a node that took four minutes showed
   "1.0s" for all four of them and then jumped.

   One interval for the whole panel, holding no state of its own: it re-reads the
   graph each second and stops itself as soon as nothing is running, so a
   finished run leaves no timer behind. */
let dagTicker = null;

function dagStopClock() {
  if (dagTicker) { clearInterval(dagTicker); dagTicker = null; }
}

function dagStartClock(d) {
  dagStopClock();
  const anyRunning = () => [...d.nodes.values()].some((n) => n.status === 'running');
  if (!anyRunning()) return;
  dagTicker = setInterval(() => {
    if (!anyRunning()) { dagStopClock(); return; }
    d.nodes.forEach((n) => {
      if (n.status !== 'running') return;
      const slot = d.els && d.els.get(n.id);
      if (slot) slot.tm.textContent = dagTook(n);
    });
  }, 1000);
}

function drawDag() {
  const key = sheetSession();
  sheetDropClass('dsheet', key);
  dagStopClock();
  const d = dagFor(key);
  if (!d) return;

  const sheet = mk('div', 'dsheet');
  sheet.setAttribute('role', 'group');
  sheet.setAttribute('aria-label', T('gui.dag.aria'));
  sheet.dataset.fold = String(!!d.folded);

  const head = mk('div', 'hd');
  head.appendChild(mk('span', 'ttl', T('gui.dag.title')));
  const gist = mk('div', d.done ? 'sum' : 'gist', d.done ? dagSummary(d) : dagGist(d));
  head.appendChild(gist);
  d.gist = gist;

  const fold = mk('button', 'ic tipdn');
  fold.appendChild(ico('M6.5 10 12 15.5 17.5 10', 'cv'));
  const setFold = (v) => {
    d.folded = v;
    sheet.dataset.fold = String(v);
    const lb = T(v ? 'gui.dag.unfold' : 'gui.dag.fold');
    fold.dataset.tip = lb;
    fold.setAttribute('aria-label', lb);
  };
  fold.onclick = () => setFold(!d.folded);
  /* The one writer of the fold, so an in-place update folds through the same
     three writes the button does rather than setting the flag and leaving the
     sheet showing the opposite. */
  d.setFold = setFold;

  const x = mk('button', 'ic tipdn');
  x.appendChild(ico('M7 7l10 10M17 7 7 17'));
  x.dataset.tip = T('gui.dag.close');
  x.setAttribute('aria-label', T('gui.dag.close'));
  x.onclick = () => { DAGS.delete(key); drawDag(); };

  head.append(fold, x);
  sheet.appendChild(head);
  setFold(!!d.folded);

  const canvas = mk('div', 'canvas');
  canvas.appendChild(dagSvg(d));
  sheet.appendChild(canvas);

  sheetAdd(sheet, key);
  /* Text can only be measured once it is rendered, so the id fit runs after
     the sheet is in the document: a long node id used to run under the clock
     in its own corner. The clock's column is reserved whether or not a time
     is showing yet -- a node that starts running must not need a re-fit. */
  sheet.querySelectorAll('.nd .id').forEach((el) => {
    const max = DAG_W - 31 - 40;
    let s = el.textContent;
    while (s.length > 1 && el.getComputedTextLength() > max) {
      s = s.slice(0, -1);
      el.textContent = s + '…';
    }
  });
  d.sheet = sheet;
  dagStartClock(d);
}

/* ── keeping a drawn sheet current, without drawing it again ──────────────
   Every `dag.*` event used to land on drawDag, which drops the sheet and builds
   a new one: a five-node run rebuilt it twelve times, and each rebuild slid it
   back in from the bottom, reset the canvas scroll on a graph wider than the
   pane, and dropped whatever the reader had focused. Nothing about a node
   update needs a new sheet -- the layout is fixed at run_started, since the
   server sends the whole graph before any node runs, and only colours, marks,
   times and the one-line gist move after that. So they move, and the sheet
   stays where it is. Falls back to a full draw when there is no live sheet to
   touch (first event, or a reader who closed and reopened the page). */
const dagLive = (d) => !!(d && d.sheet && d.els && document.body.contains(d.sheet));

function dagTouch(d) {
  if (!dagLive(d)) { drawDag(); return; }
  if (d.setFold && d.sheet.dataset.fold !== String(!!d.folded)) d.setFold(!!d.folded);
  if (d.gist) {
    d.gist.className = d.done ? 'sum' : 'gist';
    d.gist.textContent = d.done ? dagSummary(d) : dagGist(d);
  }
  d.nodes.forEach((n) => {
    const slot = d.els.get(n.id);
    if (!slot) return;
    const st = n.status || 'pending';
    if (slot.g.dataset.st !== st) {
      slot.g.dataset.st = st;
      const next = dagMark(n.status, 17, DAG_H / 2);
      slot.mark.replaceWith(next);
      slot.mark = next;
    }
    slot.tm.textContent = dagTook(n);
  });
  /* Edges darken as their source completes, and that is a class on a path
     rather than a shape, so it can be reapplied without a relayout. */
  const done = new Set();
  d.nodes.forEach((n) => { if (n.status === 'completed') done.add(n.id); });
  d.sheet.querySelectorAll('.edge, .tip').forEach((el) => {
    const from = el.dataset.from;
    if (from) el.classList.toggle('flowed', done.has(from));
  });
  dagStartClock(d);
}

/* The selection is one attribute on one node; moving it does not need the
   graph rebuilt around it. */
function dagSelect(d) {
  if (!dagLive(d)) return;
  d.els.forEach((slot, id) => {
    const on = dagNode && dagNode.run_id === d.run_id && dagNode.node === id;
    if (on) slot.g.dataset.sel = '1';
    else delete slot.g.dataset.sel;
  });
}

function dagSummary(d) {
  const s = d.summary || {};
  const bits = [T('gui.dag.done').replace('{n}', String(s.completed || 0)).replace('{t}', String(s.total || d.order.length))];
  if (s.failed) bits.push(T('gui.dag.failed').replace('{n}', String(s.failed)));
  if (s.skipped) bits.push(T('gui.dag.skipped').replace('{n}', String(s.skipped)));
  return bits.join(' · ');
}

/* A node opens where a sub-agent's work already lives, rather than growing a
   second transcript view inside the sheet: same panel, same renderer, and the
   sheet stays the map rather than becoming the territory. */
function dagOpenNode(runId, n) {
  dagNode = { run_id: runId, node: n.id, agent: n.subagent, label: n.id };
  agentOpen = null;
  if (!wsOpen) setWs(true);
  wsPick('agents');
  drawWs();
  /* The sheet marks the node whose transcript is open. Moving one attribute,
     not rebuilding the graph: every click used to drop the sheet and animate a
     new one in, so picking a second node to compare against the first meant
     watching the first one leave. */
  dagSelect(dagFor());
}
/* The trail's dag card opens a node through the same reader. */
delegOpenNode = (runId, nodeId) => dagOpenNode(runId, { id: nodeId });

/* Per-node status for a card whose events are long gone: `dag.get` reads the
   run back off disk, reconciled against the registry, so a graph reopened from
   history shows what actually happened rather than a row of pending dots. */
delegReadDag = (runId) => rpc.call('dag.get', { run_id: runId, session_key: cur })
  .then((r) => ((r && r.run && r.run.files) || []).map((f) => ({ node: f.node, status: f.status })));

/* "View in workspace" on a spawn row: open the panel on the run's own record,
   not just on the list. The list may not have caught the new run yet, so a
   couple of short retries cover the gap between the call and its row. */
delegOpenSpawn = (agent, label) => {
  setWs(true, 'agents');
  const match = () => AGENTS.find((x) => x.kind !== 'dag'
    && (!label || plainTitle(x.label) === plainTitle(label))
    && (!agent || (x.agent || 'raven') === (agent || 'raven')));
  const attempt = (n) => {
    const it = match();
    if (it) { agentOpenRow(it); return; }
    if (n >= 4) return;
    agentsAt = 0; agentsLoad();
    setTimeout(() => attempt(n + 1), 700);
  };
  attempt(0);
};

/* One dag node's own transcript: what it was asked, what it did on the way, and
   what it answered. Painted by the same function a spawned call's transcript is,
   so the two kinds of delegated work read the same way and only one of them has
   to be kept append-only -- a node polled while it runs used to be torn down and
   rebuilt on every tick, which closed every fold the reader had opened. */
function dagNodePaint(box, it) {
  const row = AGENTS.find((a) => a.kind === 'dag' && a.run_id === it.run_id && a.node === it.node);
  return rpc.call('dag.node', { run_id: it.run_id, node: it.node, session_key: cur })
    .then((r) => {
      if (!document.body.contains(box)) return;
      const n = (r && r.node) || {};
      agentPaint(
        box,
        { messages: n.messages || [], status: row ? row.status : null },
        { key: `dag:${it.run_id}:${it.node}`, empty: T('gui.dag.node_empty') },
      );
      /* Said plainly rather than left to a reader wondering where the rest went:
         the file on disk is whole, this is its head. Once, not once per poll. */
      if (n.output_truncated && !box.querySelector(':scope > .wsnote')) {
        box.appendChild(mk('div', 'wsnote', T('gui.dag.truncated')));
      }
    })
    .catch((e) => {
      if (!document.body.contains(box)) return;
      agentDrawn.key = null;
      box.innerHTML = '';
      box.appendChild(mk('div', 'wsempty', (e && e.message) || String(e)));
    });
}

dagNodeRender = (box, it) => {
  /* A reopened panel draws into a new box, so the paint has to start over even
     though the node it is drawing has not changed. */
  agentDrawn.key = null;
  dagNodePaint(box, it);
};

/* Dev-only hook, beside __clarify and __approve and for the same reason: the
   graph is only reachable by configuring third-party sub-agents and spending a
   multi-agent run, which is too long a loop to design a layout in.
   `window.__dag()` feeds the same three events the server sends. */
window.__dag = (ev) => onEvent(ev && ev.type ? ev : {
  type: 'dag.run_started',
  payload: {
    run_id: '20260812T120000Z-deadbeef',
    nodes: [
      { id: 'survey', subagent: 'Researcher', depends_on: [] },
      { id: 'read_a', subagent: 'Researcher', depends_on: ['survey'] },
      { id: 'read_b', subagent: 'Coder', depends_on: ['survey'] },
      { id: 'read_c', subagent: 'Coder', instance: 'w2', depends_on: ['survey'] },
      { id: 'merge', subagent: 'Writer', depends_on: ['read_a', 'read_b', 'read_c'] },
      { id: 'review', subagent: 'Critic', depends_on: ['merge'] },
    ],
  },
});

})();
