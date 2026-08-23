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
  await rpc.call('system.hello', { client_version: '0.1.0', surface: SURFACE }).catch(() => {});
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
  const current = sessionCurrent();
  if (current && !draft) {
    turn.dispatch({ type: 'idle' });
    const title = $('#title').textContent;
    await openSession({ id: current, title });
    showStatus(T('gui.reconnected'));
    setTimeout(killStatus, 2500);
  } else if (current) {
    subscribe(current);
  }
};

/* A pending new task is a draft, not a session: nothing is written to disk
   until the first message, so the rail does not fill with empty sessions. */
let draft = false;

function resetView() {
  stop_(); turn.dispatch({ type: 'idle' }); queueClear();
  resetTurnState();
  wsReset();
  setWs(false);
  $('#stage').innerHTML = '';
  $('#flash').textContent = '';
  drawMeter(); goState(); drawBanner();
}

function startDraft() {
  parkTurn();
  // The old session's stream must stop routing to the visible stage the
  // moment we leave it -- its events belong to the parked buffer now.
  live.subId = null;
  parkDraft(); loadDraft('new');
  resetView();
  draft = true; sessionSet(null);
  $('#title').textContent = T('gui.new_task');
  pitch(); drawList(); ta.focus();
}

openSession = async function (s) {
  parkTurn();
  // Same as startDraft: while session.resume is in flight the old session
  // may still be streaming, and a stale live.subId would paint its events
  // into the newly opened stage. Route them to the parked buffer instead.
  live.subId = null;
  parkDraft(); loadDraft(s.id);
  draft = false;
  // Opening it IS reading it. ``s`` can be a bare {id, title} from the
  // reconnect path, so clear the flag on the row in SESS, not on the arg.
  const row = sess(s.id);
  if (row && row.status === 'done') row.status = null;
  markNewCurrent();
  stop_(); turn.dispatch({ type: 'idle' }); queueClear();
  resetTurnState();
  wsReset();
  setWs(false);
  $('#title').textContent = plainTitle(s.title);
  $('#stage').innerHTML = '';
  $('#flash').textContent = '';
  drawMeter(); goState(); drawBanner();
  // A parked turn restores in place of a disk reload: the transcript on disk
  // does not have the still-streaming content, the parked DOM does.
  const pk = parkedTurns.get(s.id);
  if (pk) {
    parkedTurns.delete(s.id);
    sessionSet(s.id);
    live.subId = subBySession[s.id] || null;
    restoreTurn(pk);
    if (!live.subId) await subscribe(s.id);
    return;
  }
  try {
    const r = await rpc.call('session.resume', { session_id: s.id, session_key: s.id });
    if (r.session_id && r.session_id !== s.id) s.id = r.session_id;
    sessionSet(s.id);
    /* resume hands back the canonical id, so the row rendered from the listed
       id no longer matches the current pointer -- without this redraw the rail shows nothing
       selected until the reader clicks a session themselves. */
    drawList();
    const u = (r.info && r.info.usage) || {};
    /* context_estimated rides along in this payload and is not passed on: the
       ring has nowhere to say an estimate, so the writer takes two numbers.
       See shell/ctxchip.ts. */
    setCtx(u.context_used, u.context_max);
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
   separately: a queued message is a plain string by the time it is drained,
   and the draft path sends from a second place, so deriving here covers all
   three with one rule. */
const mediaOf = (text) => {
  const paths = splitAtts(String(text)).atts;
  return paths.length ? { media: paths } : {};
};

/* Named, not anonymous, and that is the whole reason it has a name at all:
   the retry affordance and the queue drain call it from two other files in this
   layer, and they can no longer reach it as `send` -- that binding belongs to
   the demo replay, which live mode must never run.
   The attachment tray is not here any more. It is the composer island's, and so
   is the note a staged file becomes; the island builds the message and hands it
   over already folded. */
function liveSend(text) {
  if (turn.busy()) { queuePush(text); return; }
  const p = $('#stage').querySelector('.pitch'); if (p) p.remove();
  /* What a retry re-sends. Recorded after the attachment note is folded in, so
     the second attempt carries the same message as the first. */
  lastAsk = text;
  ask(text);
  turn.dispatch({ type: 'send' });
  resetTurnState();
  drawMeter(); goState(); drawList();
  const failed = (e) => {
    killStatus();
    turn.dispatch({ type: 'idle' });
    noteRow(T('gui.err.send'), e.message === 'not connected' ? T('gui.err.disconnected') : (e.message || String(e)),
      { retry: () => liveSend(text) });
    goState(); drawMeter();
  };
  if (!draft) {
    const current = sessionCurrent();
    turnOwner = current;
    touchSession(current, text);
    titleFromFirstMessage(text);
    rpc.call('turn.send', { session_key: current, content: text, ...mediaOf(text) }).catch(failed);
    return;
  }
  // The draft becomes a real session here, on its first message.
  (async () => {
    const r = await rpc.call('session.create', {});
    const s = { id: r.session_id, title: T('gui.new_task'), last: rowPreview(text) || T('gui.sess.not_started'),
      when: T('gui.sess.just_now'), at: Math.floor(Date.now() / 1000), run: null, live: true, persisted: false };
    SESS.unshift(s); sessionSet(s.id); draft = false;
    turnOwner = sessionCurrent();
    // The composer was owned by 'new' until this point; keep later keystrokes
    // filed under the session that just came into being.
    if (draftOwner === 'new') draftOwner = sessionCurrent();
    titleFromFirstMessage(text);
    drawList();
    await subscribe(s.id);
    await rpc.call('turn.send', { session_key: sessionCurrent(), content: text, ...mediaOf(text) });
  })().catch(failed);
};

/* A stop is not a failure: everything already streamed stays on the stage, and
   the only new line is the note that a person asked for the stop. Shared by
   the button and by the cancelled event another client can cause.

   The turn ends the same way a finished one does -- finishTurn promotes the
   prose that streamed into the answer block. Sealing the open step instead
   left that prose as narration, which the fold then closed over: the reader
   pressed stop and watched the half-written answer disappear behind
   "done", under a note saying the output was kept. */
function softStop(keepCancelling) {
  killStatus();
  RavenIslands.transcript.finishTurn(live.st, live.steps, turnDur());
  stop_();
  if (!keepCancelling) turn.dispatch({ type: 'idle' });
  /* Only promise the output was kept when there is output above to keep. */
  noteRow(T(RavenIslands.transcript.turnKept() ? 'gui.halted' : 'gui.halted_bare'), '',
    { quiet: true, host: $('#stage') });
  /* A stopped turn still produced what it produced. */
  RavenIslands.transcript.artifacts(WS.turn);
  resetTurnState();
  drawMeter(); goState(); drawList();
}

/* Queued messages were waiting for the engine, and a stop is the engine coming
   free -- so the queue drains into it, same as after a finished turn. */
function drainQueue() {
  if (turn.busy()) return;
  const nx = queueShift();
  if (nx !== undefined) liveSend(nx);
}

/* The two actions, installed on the source the composer already asks. `stop`
   is the go button's other half and the Escape key's; `send` is what the island
   hands a folded message to. */
DS.composer.send = liveSend;
DS.composer.stop = function () {
  /* A runtime turn (a delegated result re-entering) is NOT cancellable:
     turn.cancel resolves only handles turn.send registered, and the stop
     button claiming the UI here would reset the stage while the delegated
     deltas are still streaming into it. The reader's stop does nothing until
     the turn is one they can stop. */
  if (!turn.cancellable()) return;
  const owner = sessionCurrent();
  turn.dispatch({ type: 'cancel' });
  rpc.call('turn.cancel', { session_key: owner })
    .then(() => {
      transitionTurn(owner, { type: 'idle' });
      if (sessionCurrent() === owner) { drawMeter(); goState(); drawList(); drainQueue(); }
    }, () => {
      transitionTurn(owner, { type: 'idle' });
      if (sessionCurrent() === owner) { drawMeter(); goState(); drawList(); }
    });
  softStop(true);
};

/* Deleting for real. Installed on the session source rather than replacing the
   rail's local name: the island asks the source whether there is anywhere to
   delete from, and here there is. */
function forgetSubscription(sessionId) {
  const subId = subBySession[sessionId];
  if (!subId) return;
  delete subBySession[sessionId];
  delete subSession[subId];
  if (live.subId === subId) live.subId = null;
  rpc.call('turn.unsubscribe', { subscription_id: subId }).catch(() => {});
}

async function leaveDeletedSession(sessionId) {
  dropDraft(sessionId);
  parkedTurns.delete(sessionId);
  forgetSubscription(sessionId);
  sheetsForget(sessionId);
  RavenIslands.dag.forget(sessionId);
  const transition = RavenIslands.rail.removeRow(SESS, sessionCurrent(), sessionId);
  SESS = transition.rows;
  if (transition.kind === 'unchanged') { drawList(); return; }
  if (transition.kind === 'open') {
    sessionSet(transition.next.id);
    await openSession(transition.next);
    return;
  }
  $('#ta').value = '';
  startDraft();
}

async function leaveArchivedSession(sessionId) {
  const transition = RavenIslands.rail.removeRow(SESS, sessionCurrent(), sessionId);
  SESS = transition.rows;
  if (transition.kind === 'unchanged') { drawList(); return; }
  if (transition.kind === 'open') {
    sessionSet(transition.next.id);
    await openSession(transition.next);
    return;
  }
  $('#ta').value = '';
  startDraft();
}

DS.sessions.remove = function (s) {
  confirmAsk(T('gui.sess.delete_title'), T('gui.sess.delete_body', { title: s.title }), T('gui.sess.delete'), async () => {
    try {
      const r = await rpc.call('session.delete', { session_id: s.id });
      if (r.deleted !== s.id) throw new Error(`session ${s.id} no longer exists`);
      await leaveDeletedSession(s.id);
      toast(T('gui.sess.deleted_x', { title: s.title }));
    } catch (e) { toast(`删除失败：${e.message || e}`); }
  });
};

DS.sessions.archive = async function (s) {
  try {
    const at = SESS.findIndex(row => row.id === s.id);
    const result = await rpc.call('session.archive', { session_id: s.id, archived: true });
    if (!result.archived || result.session_key !== s.id) throw new Error(`session ${s.id} was not archived`);
    await leaveArchivedSession(s.id);
    toast(T('gui.sess.archived', { title: s.title }), {
      label: T('gui.undo'),
      fn: async () => {
        try {
          const restored = await rpc.call('session.archive', { session_id: s.id, archived: false });
          if (restored.archived || restored.session_key !== s.id) {
            throw new Error(`session ${s.id} was not restored`);
          }
          if (!SESS.some(row => row.id === s.id)) SESS.splice(Math.max(0, Math.min(at, SESS.length)), 0, s);
          drawList();
        } catch (e) {
          toast(T('gui.sess.restore_failed', { detail: (e && (e.message || e.detail)) || String(e) }));
        }
      }
    });
  } catch (e) {
    toast(T('gui.sess.archive_failed', { detail: (e && (e.message || e.detail)) || String(e) }));
  }
};

$('#newBtn').onclick = () => { showPage(null); startDraft(); };

/* Persist a manual rename made through the title editor. The editor is the
   rail island's, and this used to wrap its entry point to hang a blur listener
   off the input it had just created -- reaching into another layer's DOM, and
   missing an Enter, which replaces that input while it still has focus. The
   island tells us instead. */
DS.sessions.renamed = (id, title) => {
  rpc.call('session.title', { session_id: id, title }).catch(() => {});
};
