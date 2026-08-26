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

/* How long the turn took. The runtime measures it and sends it on
   message.complete, so that number is the one to draw: timing it here measures
   when the events reached this page, which is a different span and one a
   reload cannot reproduce -- a page that was not open for the turn never saw
   its start, so the same turn came out one number live and another on replay.

   The local subtraction stays as the fallback, for a server too old to send
   the field and for a stop, which ends the turn without a message.complete. It
   stops at the last word of the answer, which is NOT what a reloaded
   transcript computes: the stamps on disk are written by `_save_turn` after
   the turn is fully unwound, so post-turn housekeeping is inside their span
   and outside this one. Hence a fallback, not a second opinion. */
const turnDur = (serverMs) => {
  if (serverMs != null) return dur(Math.max(serverMs, 1000));
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
  if (p.target) { RavenIslands.subagents.directEvent(p.target, ev.type, p); return; }
  if (ev.type === 'message.start') {
    /* Read BEFORE the phase is set: the window that sent this turn has already
       drawn the question; a window that is only watching has not. */
    if (!turn.busy() && p.content) ask(p.content);
    if (p.content) touchSession(sessionCurrent(), p.content);
    turnOwner = sessionCurrent();
    turn.dispatch({ type: 'stream', cancellable: true }); goState(); drawMeter();
    RavenIslands.workspace.advanceTurn();
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
    RavenIslands.workspace.advanceTurn();
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
    /* Re-anchor the fallback clock. Nobody typed this turn, so no liveSend ran
       to move it, and it was last set when the PARENT turn ended -- with the
       sub-agent's whole run sitting in between. The server's `duration_ms`
       covers the number that gets drawn; this covers the paths that fall back
       to timing it here, a stop being the one that always does. */
    live.startedAt = Date.now();
    live.answerAt = 0;
    turnOwner = sessionCurrent();
    turn.dispatch({ type: 'stream', cancellable: false }); goState(); drawMeter();
  } else if (ev.type === 'episode.start') {
    if (live.st) { live.st.seal(); }
    flushSay();
    live.st = newStep(); live.steps.push(live.st); live.sawEpisode = true;
  } else if (ev.type === 'session.titled') {
    /* The server named the session. Replaces whatever the row shows without
       comparing: the event is emitted only when the title actually changed. */
    settleNaming(p.session_id, p.title);
  } else if (ev.type === 'session.naming_ended') {
    /* No title is coming after all: the model answered without calling the
       naming tool, the call outran its budget, this code raised, or a person
       named the session while it ran. `namingEnded` decides what that means for
       the row -- the last of those is not settled onto the opening line. The
       timer is only a backstop for a server that says neither of these. */
    namingEnded(p.session_id, p.reason);
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
    /* The call id travels with the row: a `run_subagent_dag` names it on every
       progress event, and it is what binds the graph to this card rather than to
       whichever dag card happened to be the newest. */
    const h = st.tool(p.name || 'tool', p.arguments, p.display, p.tool_call_id);
    live.open.set(p.tool_call_id, { h, st, t0: Date.now(), name: p.name, args: p.arguments });
    /* The workspace panel gets the WHOLE argument object, not the one-line
       display string: edit_file's old_text/new_text is the diff. */
    if (typeof wsOnTool === 'function') wsOnTool(p.name, p.arguments, false);
  } else if (ev.type === 'tool.complete') {
    if (p.metadata) RavenIslands.transcript.delivery(RavenIslands.workspace.currentTurn(), p.metadata);
    const o = live.open.get(p.tool_call_id);
    if (!o) return;
    live.open.delete(p.tool_call_id);
    const preview = cleanPreview(p.result_preview).split('\n').map((l) => l.slice(0, 160)).join('\n');
    // The emit site's verdict is authoritative; the text heuristic survived
    // only as the backstop for an old server that does not send the field.
    const ok = typeof p.ok === 'boolean' ? p.ok : okOf(o.name || '', preview);
    const took = Date.now() - o.t0;
    o.h.done(ok, preview, took, null, p.truncated);
    /* p.diff is the real change on disk -- the only place a whole-file write's
       previous content survives. */
    if (typeof wsOnToolDone === 'function') wsOnToolDone(o.name, o.args, ok, preview, took, p.diff);
  } else if (ev.type === 'message.complete') {
    /* Our own cancel already folded and reset the visible turn. The server can
       finish unwinding before turn.cancel replies; only that reply may release
       the queued send. */
    if (turn.phase() === 'cancelling') return;
    finishTurn(p);
  } else if (ev.type === 'error') {
    killStatus();
    /* A cancelled turn is the one "error" a person asked for; the event still
       matters when the cancel came from ANOTHER client on the same session. */
    if (p.reason === 'cancelled_by_client') {
      if (turn.phase() === 'cancelling') return;
      if (turn.busy()) softStop();
      setTimeout(drainQueue, 400);
      return;
    }
    turn.dispatch({ type: 'idle' });
    noteRow(p.message || 'error', p.detail || p.reason || '',
      lastAsk ? { retry: () => liveSend(lastAsk) } : null);
    goState(); drawMeter(); sessionDraw();
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
      sessionRows().unshift(s);
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
      task_summary: p.task_summary || null,
    });
  } else if (ev.type === 'dag.node_updated') {
    dagFlowFeed(ev.type, p);
    /* Through the island rather than into the run's node map from here: what a
       report means to a node is one definition, next to the model it moves, and
       the copy that lived here had drifted into inventing a clock. */
    RavenIslands.dag.advance(sheetSession(), p);
  } else if (ev.type === 'dag.run_completed') {
    dagFlowFeed(ev.type, p);
    RavenIslands.dag.settle(sheetSession(), p);
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

function finishTurn(p) {
  const usage = p.usage || {};
  killStatus();
  /* The island promotes the streamed prose into the answer block where the
     prose stood, merges the silent stretches and folds the turn. */
  RavenIslands.transcript.finishTurn(live.st, live.steps, turnDur(p.duration_ms));
  /* The turn's products close it, after the answer and after any note: the
     bar is the last line of a turn, and it is only drawn once the turn is
     over -- nothing grows it mid-flight. */
  RavenIslands.transcript.artifacts(RavenIslands.workspace.currentTurn());
  turn.dispatch({ type: 'idle' });
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
  drawMeter(); goState(); sessionDraw(); down();
  const nx = queueShift();
  if (nx !== undefined) liveSend(nx);
}

/* Naming a new session belongs to the server now: it reads the opening message
   and answers with `session.titled`. Until that lands the row and the top bar
   hold a placeholder, which is what the animation is for -- this used to write
   the truncated first line here and then rewrite it a second later, and no
   client-side cap is left to disagree with the server's.

   The server now says when naming ends without a title
   (`session.naming_ended`), so the ordinary quiet endings settle as fast as a
   success does. This timer is the backstop for the case no message covers -- a
   server too old to send either event, or a connection that drops mid-turn --
   and it outlives the server's own timeout on the call
   (`session_title.timeout_seconds`, 8s by default) so it cannot fire while an
   answer is still legitimately on its way. A placeholder waiting on something
   that is not coming is the one state a reader cannot leave by waiting.

   It gives up onto the opening line held here, NOT onto the stored title: the
   session's auto-name is written by `SessionManager.save`, which runs at turn
   END (agent/loop/main.py), so a turn still working at the 12s mark has no
   stored title to read and the row would sit on the default name until a
   reload -- nothing re-reads it, since `refreshList` fires only for sessions
   the reader is not looking at. The stored read is still tried first, because
   a turn that has already ended has the better text. */
const NAMING_GRACE_MS = 12000;
const namingTimers = new Map();

function titlePlaceholder(on) {
  const h = $('#title');
  if (!h) return;
  h.textContent = '';
  h.classList.toggle('skel', !!on);
  if (!on) return;
  const bar = document.createElement('span');
  bar.className = 'sk';
  bar.style.width = '180px';
  bar.style.height = '14px';
  bar.setAttribute('aria-label', T('gui.sess.naming'));
  h.appendChild(bar);
}

/* Stop waiting on `id` and show `title`, or what the row already had. Looks the
   row up rather than holding one: `refreshList` replaces the row objects, so a
   row captured when the wait started can be off the list by the time it ends. */
function settleNaming(id, title) {
  const pending = namingTimers.get(id);
  if (pending) clearTimeout(pending.timer);
  namingTimers.delete(id);
  const s = sess(id);
  if (!s) return;
  s.naming = false;
  if (title) s.title = title;
  if (id === sessionCurrent()) {
    titlePlaceholder(false);
    const h = $('#title');
    if (h) h.textContent = plainTitle(s.title);
  }
  sessionDraw();
}

/* The grace period ran out. Prefer the stored title -- a turn that has ended
   has an auto-name on disk and it is the one every other client shows -- and
   otherwise use the opening line captured when the wait started. A read that
   succeeds and answers null is the ordinary case here, not an error: the turn
   is still running. */
async function namingGaveUp(id) {
  const pending = namingTimers.get(id);
  let title = '';
  try {
    const r = await rpc.call('session.title', { session_id: id });
    title = (r && r.title) || '';
  } catch { /* fall through to the captured line */ }
  settleNaming(id, title || (pending && pending.fallback) || '');
}

/* The server told us no name is coming for this one -- the opening line was
   too short to name after, or the session already had a name, or the feature is
   off. Settle now on the line we captured: waiting the full grace period for an
   event that will never arrive is what made a two-character "hi" the SLOWEST
   thing you could send, since a long message actually generates and lands in a
   second or two while a short one always burned the whole timeout.

   Only when a wait is actually open: `beginNaming` declines to start one for a
   session that is already named, and this must not then blank its title. */
function namingDeclined(id) {
  const pending = namingTimers.get(id);
  if (!pending) return;
  settleNaming(id, pending.fallback || '');
}

/* A name arrived from a person while the model was still writing one, so the
   session is already better named than anything we hold. End the wait WITHOUT
   the captured opening line: settling on it here overwrote the name that had
   just been typed with the message it was typed over, for as long as the page
   stayed open.

   The stored title is read rather than assumed, because the rename may have
   been typed in another client -- this row would still be showing the default
   and clearing the placeholder alone would leave it there. If that read fails,
   keep whatever the row has; it is at worst the default, and never the wrong
   name. */
async function namingSuperseded(id) {
  if (!namingTimers.get(id)) return;
  let title = '';
  try {
    const r = await rpc.call('session.title', { session_id: id });
    title = (r && r.title) || '';
  } catch { /* keep what the row shows */ }
  settleNaming(id, title);
}

/* One place to decide what a `session.naming_ended` reason means for the row,
   so the dispatcher carries no policy and this is reachable from a test. Three
   of the four reasons mean "nothing better than the opening line exists"; the
   fourth means the opposite. */
function namingEnded(id, reason) {
  if (reason === 'renamed') return namingSuperseded(id);
  return namingDeclined(id);
}

function beginNaming(text) {
  const s = sess(sessionCurrent());
  if (!s || (s.title && s.title !== '新任务' && s.title !== T('gui.new_task'))) return;
  if (!String(text || '').trim()) return;
  const id = s.id;
  /* Not capped here: how a title fits a row is the front end's own business and
     both places that draw one already ellipsise. */
  const fallback = String(text).trim().split('\n')[0].trim();
  s.naming = true;
  if (id === sessionCurrent()) titlePlaceholder(true);
  sessionDraw();
  namingTimers.set(id, { fallback, timer: setTimeout(() => { namingGaveUp(id); }, NAMING_GRACE_MS) });
}

/* A refused persist must not stay quiet. The row moves optimistically, but a
   pin the server never accepted looks identical to one it did until the page
   is reloaded and the group is simply gone -- which is exactly how an older
   resident gateway, with no session.pin to call at all, presents itself. Put
   the row back and say so. */
DS.sessions.pin = (id, pinned) => rpc.call('session.pin', { session_id: id, pinned: !!pinned })
  .catch((e) => {
    const s = sess(id);
    if (s) { s.pin = !pinned; sessionDraw(); }
    toast(T('gui.sess.pin_failed', { detail: (e && (e.message || e.detail)) || String(e) }));
  });

async function refreshList() {
  try {
    const r = await rpc.call('session.list', { channels: SESS_CHANNELS });
    const rows = (r.sessions || []).map(rowFrom);
    // Pins and persisted fields come from the server. Only the running/done
    // marker is client state; a not-yet-saved current row also survives until
    // the first list response that contains it.
    const reconciled = RavenIslands.rail.reconcile(sessionRows(), rows, sessionCurrent());
    const currentMissing = reconciled.currentMissing;
    sessionReplace(reconciled.rows);
    if (currentMissing) await leaveDeletedSession(sessionCurrent());
    else sessionDraw();
  } catch { /* keep the stale list */ }
}
