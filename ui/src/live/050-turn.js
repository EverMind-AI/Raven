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
    noticeRow(p.kind || '', p.detail || '');
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
    noteRow(p.message || 'error', p.detail || p.reason || '',
      lastAsk ? { retry: () => send(lastAsk) } : null);
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
      s = { id, title: p.name, last: '', when: T('gui.sess.just_now'),
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

