/* ---- live turn state machine ------------------------------------- */
/* The DOM half of a turn is the transcript island's now; this machine keeps
   what is genuinely live-side: which step is open, which calls are in
   flight, the say buffer the session row's preview reads, and the clocks. */
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

/* Token deltas land in the island's per-step buffer and paint once per
   frame there; these two names survive for the parked-turn machinery. */
function paintSay() { RavenIslands.transcript.nudge(); }
function stopSayPaint() { RavenIslands.transcript.stopStream(); }

function flushSay() {
  /* The step keeps its streamed narration; only the local buffer resets. */
  live.say = '';
}

/* How long the turn took: asked, to the last word of the answer. Not until
   the runtime is idle -- post-turn housekeeping would make this disagree with
   what a reloaded transcript computes from the stamps on disk. */
const turnDur = () => {
  const ms = (live.answerAt || Date.now()) - live.startedAt;
  return live.startedAt ? dur(Math.max(ms, 1000)) : null;
};

/* The turn folds ONCE, at message.complete: mid-stream nothing can tell the
   answer from another line of narration. */

function onEvent(ev) {
  const p = ev.payload || {};
  /* A turn addressed to a sub-agent instance, not to this conversation. The
     client holds ONE subscription per session, and the lane stamps every event
     of a direct chat with its `target` precisely so the two can be told apart
     here (see `_subscription` in raven/rpc/spine.py); an untagged frame is the
     main agent's.

     Dropping them was already the effect -- a delta arriving with no turn of
     ours open finds no slot to land in -- but only by accident. Send a direct
     turn while the main agent is answering and that slot exists, and one
     instance's words get typed into the conversation as if raven had said them.
     These belong on the instance's own page, which reads them back through
     `subagents.instance.history`. */
  if (p.target) { RavenIslands.subagents.directEvent(p.target, ev.type); return; }
  if (ev.type === 'message.start') {
    /* Read BEFORE busy is set: the window that sent this turn has already
       drawn the question; a window that is only watching has not. */
    if (!busy && p.content) ask(p.content);
    if (p.content) touchSession(sessionCurrent(), p.content);
    busy = true; busyCancellable = true; goState(); drawMeter();
    WS.turn += 1;
  } else if (ev.type === 'turn.started') {
    /* A turn the RUNTIME opened (a delegated result re-entering) has begun.
       The spine suppresses message.start for these, so this event is the whole
       opening: the workspace turn advances, the client enters the busy state
       (a queued send must wait for this turn's message.complete), and the
       delivery row is drawn HERE -- this is the moment the result is actually
       visible, not the moment it was submitted while its parent still owned
       the lane. `delegated` carries the identity AND the injected text, the
       same identity a stored entry carries on replay, so the two views draw
       the same row at the same place. */
    WS.turn += 1;
    if (p.delegated) {
      const d = p.delegated;
      const isDag = d.kind === 'dag';
      RavenIslands.transcript.delivered({
        label: d.label || '',
        isDag,
        err: d.status === 'error',
        body: d.content || '',
        open: () => {
          if (isDag) { DS.transcript.openDagRun(d.run_id || d.label || ''); return; }
          DS.transcript.openSpawn('', d.label || '');
        },
      });
    }
    busy = true; busyCancellable = false; goState(); drawMeter();
  } else if (ev.type === 'episode.start') {
    if (live.st) { live.st.seal(); }
    flushSay();
    live.st = newStep(); live.steps.push(live.st); live.sawEpisode = true;
  } else if (ev.type === 'notice') {
    killStatus();
    /* Seals the open step first: this ends the turn, so the streamed prose
       above stays where it was said. */
    if (live.st) { live.st.seal(); live.st = null; }
    flushSay();
    noteRow(T('gui.notice.' + (p.kind || ''), null, p.kind || ''), p.detail || '', { quiet: true });
  } else if (ev.type === 'thinking.delta') {
    killStatus();
    ensureStep().thinkAppend(p.text || '');
  } else if (ev.type === 'token.delta') {
    killStatus();
    const st = ensureStep();
    /* sayDelta folds a finished thought before the prose lands. */
    st.sayDelta(p.text || '');
    live.say += p.text || '';
    live.answerAt = Date.now();
  } else if (ev.type === 'tool.start') {
    killStatus();
    const st = ensureStep();
    const h = st.tool(p.name || 'tool', p.arguments, p.display);
    live.open.set(p.tool_call_id, { h, st, t0: Date.now(), name: p.name, args: p.arguments });
    /* The workspace panel gets the WHOLE argument object, not the one-line
       display string: edit_file's old_text/new_text is the diff. */
    if (typeof wsOnTool === 'function') wsOnTool(p.name, p.arguments, false);
  } else if (ev.type === 'tool.complete') {
    if (p.metadata) RavenIslands.transcript.delivery(WS.turn, p.metadata);
    const o = live.open.get(p.tool_call_id);
    if (!o) return;
    live.open.delete(p.tool_call_id);
    const preview = cleanPreview(p.result_preview).split('\n').map((l) => l.slice(0, 160)).join('\n');
    const ok = okOf(o.name || '', preview);
    const took = Date.now() - o.t0;
    o.h.done(ok, preview, took, null, p.truncated);
    /* p.diff is the real change on disk -- the only place a whole-file write's
       previous content survives. */
    if (typeof wsOnToolDone === 'function') wsOnToolDone(o.name, o.args, ok, preview, took, p.diff);
  } else if (ev.type === 'message.complete') {
    finishTurn(p.usage || {});
  } else if (ev.type === 'error') {
    killStatus();
    /* A cancelled turn is the one "error" a person asked for; the event still
       matters when the cancel came from ANOTHER client on the same session. */
    if (p.reason === 'cancelled_by_client') {
      if (busy) softStop();
      if (!cancelInFlight) setTimeout(drainQueue, 400);
      return;
    }
    busy = false;
    noteRow(p.message || 'error', p.detail || p.reason || '',
      lastAsk ? { retry: () => liveSend(lastAsk) } : null);
    goState(); drawMeter(); drawList();
  } else if (ev.type === 'cron.delivered') {
    toast(T('gui.cron.new_output', { name: p.name }));
  } else if (ev.type === 'cron.missed') {
    /* One-shot reminders whose time passed while the backend was down. Queued
       at bring-up and flushed to the first subscription, so this arrives once
       per restart rather than per job -- the count is the payload's own. */
    toast(T('gui.cron.missed_x', { count: p.count }));
  } else if (ev.type === 'subagent.delivered') {
    /* A result was submitted, not yet visible: the turn it opens is still
       queued behind its parent, so the row does NOT belong here. It arrives
       with the turn's own opening (turn.started, carrying the same identity)
       -- until then this event is a no-op, kept for older servers that still
       send it. */
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
    if ($('#cronPage').dataset.open === 'true') refreshCron();
  } else if (ev.type === 'dag.run_started') {
    /* The trail's delegation card paints the same events as the sheet below:
       one feed call per branch, before the sheet's own bookkeeping. */
    dagFlowFeed(ev.type, p);
    /* The graph arrives whole, before any node runs. Filed under the
       conversation it belongs to: onEvent only ever runs for the open
       session, so the current key is the owning key on both paths. */
    /* Through the same adapter the transcript's dag card reads (the bundle's
       features/dag/nodes.ts): the payload was being unpacked field by field here
       as well, so "what a node is" had two definitions that only happened to
       agree. */
    const started = RavenIslands.dag.fromStarted(p);
    const key = sheetSession();
    RavenIslands.dag.start(key, {
      run_id: p.run_id,
      session: key,
      order: started.map((n) => n.id),
      nodes: new Map(started.map((n) => [n.id, n])),
      summary: null, done: false, folded: false,
    });
  } else if (ev.type === 'dag.node_updated') {
    dagFlowFeed(ev.type, p);
    const d = RavenIslands.dag.run(sheetSession());
    if (d && d.run_id === p.run_id) {
      const n = d.nodes.get(p.node);
      if (n) {
        n.status = p.status;
        n.started_at = p.started_at || n.started_at;
        n.ended_at = p.ended_at || n.ended_at;
        RavenIslands.dag.touch();
      }
    }
  } else if (ev.type === 'dag.run_completed') {
    dagFlowFeed(ev.type, p);
    const d = RavenIslands.dag.run(sheetSession());
    if (d && d.run_id === p.run_id) {
      /* Each file's status is the run's own last word on that node. */
      (p.files || []).forEach((f) => {
        const n = d.nodes.get(f.node);
        if (n) { n.status = f.status; n.ended_at = n.ended_at || Date.now(); }
      });
      d.summary = p.summary || null;
      d.dir = p.dir || null;
      d.done = true;
      d.folded = true;
      RavenIslands.dag.touch();
    }
  }
}

const fmtTok = (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n));

// Per-turn cost lives under each answer and "a turn is running" is now the
// ticking row above the composer, so the strip under the field stays empty.
DS.composer.meter = () => '';

/* Opening a delegated graph: the last node if this page already holds the run's
   own record, the agents panel otherwise. Installed as a source verb rather
   than written inline in the delivered handler, because the row a RELOAD draws
   has to open the same thing the live row does. */
DS.transcript.openDagRun = function (runId) {
  const id = String(runId || '');
  if (id) {
    const d = RavenIslands.dag.run(sheetSession());
    const last = d && d.run_id === id ? d.order[d.order.length - 1] : null;
    if (last) { dagOpenNode(id, { id: last }); return; }
  }
  setWs(true, 'agents');
};

function finishTurn(usage) {
  busyCancellable = false;
  killStatus();
  /* The island promotes the streamed prose into the answer block where the
     prose stood, merges the silent stretches and folds the turn. */
  RavenIslands.transcript.finishTurn(live.st, live.steps, turnDur());
  /* The turn's products close it, after the answer and after any note: the
     bar is the last line of a turn, and it is only drawn once the turn is
     over -- nothing grows it mid-flight. */
  RavenIslands.transcript.artifacts(WS.turn);
  busy = false;
  const inTok = usage.input_tokens || usage.prompt_tokens || 0;
  /* The window fill is the turn's prompt, not the running total. */
  setCtx(usage.context_used || inTok, usage.context_max);
  const s = sess(sessionCurrent());
  if (s && live.say.trim()) s.last = live.say.trim().split('\n')[0].slice(0, 60);
  touchSession(sessionCurrent(), live.say);
  // OS notification when the answer lands while the window is in the
  // background; ntfPush itself checks focus and the user's preference.
  ntfPush(T('gui.set.ntf.done'), (s && s.title) || live.say.trim().slice(0, 80));
  resetTurnState();
  drawMeter(); goState(); drawList(); down();
  const nx = queueShift();
  if (nx !== undefined) liveSend(nx);
}

/* The session's title IS the user's first message (first line, capped) — set
   the moment it is sent, not after the turn ends, so the rail and the top bar
   never sit on 新任务 while the agent works. */
function titleFromFirstMessage(text) {
  const s = sess(sessionCurrent());
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
DS.sessions.pin = (id, pinned) => rpc.call('session.pin', { session_id: id, pinned: !!pinned })
  .catch((e) => {
    const s = sess(id);
    if (s) { s.pin = !pinned; drawList(); }
    toast(T('gui.sess.pin_failed', { detail: (e && (e.message || e.detail)) || String(e) }));
  });

async function refreshList() {
  try {
    const r = await rpc.call('session.list', { channels: SESS_CHANNELS });
    const rows = (r.sessions || []).map(rowFrom);
    // Pins and persisted fields come from the server. Only the running/done
    // marker is client state; a not-yet-saved current row also survives until
    // the first list response that contains it.
    const reconciled = RavenIslands.rail.reconcile(SESS, rows, sessionCurrent());
    const currentMissing = reconciled.currentMissing;
    SESS = reconciled.rows;
    if (currentMissing) await leaveDeletedSession(sessionCurrent());
    else drawList();
  } catch { /* keep the stale list */ }
}
