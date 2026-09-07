/* ---- overrides ----------------------------------------------------- */
async function subscribe(sessionKey) {
  // One subscription per session per socket: re-opening a session reuses its
  // stream, so a parked turn's events and the visible ones never double up.
  if (subBySession[sessionKey]) { claimStream(sessionKey); return; }
  try {
    const r = await rpc.call('turn.subscribe', { session_key: sessionKey });
    subBySession[sessionKey] = r.subscription_id;
    subSession[r.subscription_id] = sessionKey;
    claimStream(sessionKey);
  } catch (e) {
    toast(`订阅失败：${e.message || e}`);
  }
}

/* `live.subId` is one thing: the subscription whose events paint the visible
   stage. So only the conversation actually on screen may hold it -- turn.subscribe
   is a round trip, and the reader can leave before it answers. Claiming it
   anyway routed a background session's stream onto the open one's transcript;
   the events belong in that session's parked buffer instead (see
   rpc.notify.event). */
function claimStream(sessionKey) {
  if (sessionKey !== sessionCurrent()) return;
  live.subId = subBySession[sessionKey] || null;
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
  sessionRows().forEach((s) => { if (s.status === 'run') s.status = null; });
  /* Re-subscribing is not enough: events emitted while the socket was down
     are gone, and if the turn ENDED in that gap the client would keep its
     busy spinner forever. Reload the whole session from disk instead -- the
     transcript is persisted server-side, so a full re-open is lossless, and
     a turn that is genuinely still running keeps streaming into the fresh
     subscription that sessionOpen sets up. */
  const current = sessionCurrent();
  if (current && !draft) {
    turn.dispatch({ type: 'idle' });
    const title = $('#title').textContent;
    await sessionOpen({ id: current, title });
    showStatus(T('gui.reconnected'));
    setTimeout(killStatus, 2500);
  } else if (current) {
    subscribe(current);
  }
  /* The installed skills, plugins and tools are read once at boot into
     module state and served from there, so a socket that was down when boot
     ran leaves all three empty for the life of the tab -- an empty page
     rather than a failed one. Re-read them here: the session reload above
     already treats a reconnect as "refetch what the gap invalidated", and
     these are the only surfaces whose data never asks again on its own. */
  /* Repainted, not just re-read: the island renders on its own `set`, which
     refilling the module state does not call, so the extensions page would
     keep showing the offline note after the reconnect it tells the reader to
     wait for. `drawCaps` is the entry for both tabs (153-plugins.js wraps
     it), guarded the way `redrawAll` guards it -- the page may not be up. */
  loadExt()
    .then(() => {
      drawCapsBadge();
      try { drawCaps(); } catch { /* extensions page not built yet */ }
    })
    .catch(() => {});
};

/* A pending new task is a draft, not a session: nothing is written to disk
   until the first message, so the rail does not fill with empty sessions. */
let draft = false;

/* A model chosen while still a draft is held here, not written: a draft has no
   session to scope the switch to, and writing it would change the global
   default instead. It is applied to the session the first message mints, then
   forgotten. Cleared on any leave of the draft so a stale pick cannot land on
   the next conversation. */
let pendingModel = null;

/* Apply a staged draft pick to the session the first message just minted.
   Awaited before that turn is sent, so the turn runs on the chosen model rather
   than racing the write.

   ``gen`` is the view as it stood when the send began, not when this runs: a
   refusal reconciles the chip to what THAT session actually runs, and the
   reader may have opened another conversation while the write was in flight.
   Its own function so the refusal path is reachable from a test without
   driving the whole send. */
async function applyStagedModel(sessionId, gen) {
  if (!pendingModel) return;
  const pm = pendingModel; pendingModel = null;
  try {
    await rpc.call('config.set', { key: 'model', value: pm.model, provider: pm.provider, session_id: sessionId });
  } catch (e) {
    // Said out loud, not just reversed: the pick was announced as staged, so a
    // silent chip flip back would be an unexplained contradiction.
    toast(`切换失败：${(e && ((e.data && e.data.detail) || e.message)) || e}`);
    void loadProviders(sessionId, gen);
  }
}

/* Which switch the visible page belongs to. `session.resume` is a round trip,
   and a reader who clicks a second session -- or the new-task button -- while
   it is in flight leaves the answer with nowhere to land: the stage it was
   cleared for now holds somebody else's conversation. Every switch takes the
   next ticket, and an answer whose ticket has been spent is dropped rather
   than painted, which is lossless because a re-open reads the same transcript
   back off disk. */
let viewGen = 0;

function resetView() {
  stop_(); turn.dispatch({ type: 'idle' }); queueClear();
  resetTurnState();
  wsReset();
  setWs(false);
  /* The empty state ends when the reader leaves it, not when its replacement
     finishes arriving: holding it across the round trip left the wordmark and
     the centred composer sitting over an empty stage, and dropped them 81px the
     moment the transcript landed. startDraft pitches again right after. */
  unpitch();
  $('#stage').innerHTML = '';
  $('#flash').textContent = '';
  drawMeter(); goState(); drawBanner();
}

function startDraft() {
  viewGen += 1;
  const gen = viewGen;
  parkTurn();
  // The old session's stream must stop routing to the visible stage the
  // moment we leave it -- its events belong to the parked buffer now.
  live.subId = null;
  parkDraft(); loadDraft('new');
  resetView();
  draft = true; sessionSet(null); pendingModel = null;
  // The other half of the pair with openLiveSession: a draft runs the
  // configured default, so leaving a conversation for one has to read that
  // default back or the chip keeps claiming the model of the conversation just
  // left. A null session omits the field, which is the default's own answer.
  void loadProviders(null, gen);
  $('#title').textContent = T('gui.new_task');
  pitch(); sessionDraw(); ta.focus();
}

async function openLiveSession(s) {
  viewGen += 1;
  const gen = viewGen;
  parkTurn();
  // Same as startDraft: while session.resume is in flight the old session
  // may still be streaming, and a stale live.subId would paint its events
  // into the newly opened stage. Route them to the parked buffer instead.
  live.subId = null;
  parkDraft(); loadDraft(s.id);
  draft = false; pendingModel = null;
  // The model is per conversation now, so the chip must follow the one being
  // opened -- otherwise it keeps the model of the session left behind. Keyed to
  // s.id rather than sessionCurrent(): the current session is not switched over
  // until below (and not at all on the parked-turn path). The gen lets the
  // refresh drop itself if a later open overtakes it, since model.options
  // answers off-thread and can land out of order.
  void loadProviders(s.id, gen);
  // Opening it IS reading it. ``s`` can be a bare {id, title} from the
  // reconnect path, so clear the flag on the row in sessionRows(), not on the arg.
  const row = sess(s.id);
  if (row && row.status === 'done') row.status = null;
  markNewCurrent();
  resetView();
  $('#title').textContent = plainTitle(s.title);
  // A parked turn restores in place of a disk reload: the transcript on disk
  // does not have the still-streaming content, the parked DOM does.
  const pk = parkedTurns.get(s.id);
  if (pk) {
    parkedTurns.delete(s.id);
    sessionSet(s.id);
    restoreTurn(pk);
    /* Unconditional, where this used to claim the recorded subscription itself
       and fall back to subscribing when there was none: `subscribe` already
       does exactly that, and one path through it is what keeps the claim rule
       in a single place. Nothing follows the await, so the extra microtask on
       the reuse case changes no ordering. */
    await subscribe(s.id);
    return;
  }
  try {
    const r = await rpc.call('session.resume', { session_id: s.id, session_key: s.id });
    /* Somebody else's page now. Everything below writes what the reader is
       looking at -- the pointer, the rail selection, the context ring, the
       transcript, the panes -- so a spent ticket has to stop here rather than
       repaint over the switch that overtook it. */
    if (gen !== viewGen) return;
    if (r.session_id && r.session_id !== s.id) s.id = r.session_id;
    sessionSet(s.id);
    /* resume hands back the canonical id, so the row rendered from the listed
       id no longer matches the current pointer -- without this redraw the rail shows nothing
       selected until the reader clicks a session themselves. */
    sessionDraw();
    /* The session's working directory, for the path shortener. It rides on
       every init bundle and used to be learned from a directory listing, which
       is a call the page no longer makes. */
    wsSetRoot(r.info && r.info.cwd);
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
    /* After the replay, because the replay is the better answer where it has
       one: a manifest on a stored turn knows which turn delivered the file, and
       the registry only knows that this conversation did. What the registry
       adds is everything the replay cannot carry -- a turn still in flight when
       the socket dropped, and one whose messages a compaction has since
       archived. Not awaited: the shelf fills when it answers. */
    RavenIslands.workspace.loadDeliveries(s.id);
    await subscribe(s.id);
    /* Checked again on this side of the subscribe: the round trip is one more
       place a reader can leave from, and a replayed file window opens on
       whichever desk is on screen. */
    if (gen !== viewGen) return;
    /* Last, and only on this path. The reader may be arriving here after a
       reload -- or after an upgrade replaced the page under them -- in which
       case the graph they were watching and the windows they had open are
       recorded but not on screen. The parked path above returns instead: its
       conversation never left this page, so its sheet and its desk are still in
       the stores and putting a second copy back would replace them.
       Not awaited: it reads the run and the panes back from the gateway, and
       the transcript is already up. */
    RavenIslands.view.resume(s.id);
  } catch (e) {
    /* Same for the failure: a session the reader has already left must not
       empty their stage, and must not raise a toast about a page nobody is on. */
    if (gen !== viewGen) return;
    pitch();
    toast(`打开会话失败：${e.message || e}`);
  }
}

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
  drawMeter(); goState(); sessionDraw();
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
    beginNaming(text);
    /* `=== false`, not falsy: a server too old to carry the field says nothing
       at all, and reading that as "declined" would tear down a placeholder
       while a title really is on its way. */
    rpc.call('turn.send', { session_key: current, content: text, ...mediaOf(text) })
      .then(r => { if (r && r.naming === false) namingDeclined(current); })
      .catch(failed);
    return;
  }
  // The draft becomes a real session here, on its first message.
  (async () => {
    // Taken before the first await: the recovery refresh below reports on THIS
    // send's session, so it needs the view as it stood when the send began --
    // a ticket taken later would read as current and repaint whatever the
    // reader has since opened.
    const sendGen = viewGen;
    const r = await rpc.call('session.create', {});
    wsSetRoot(r.info && r.info.cwd);
    const s = { id: r.session_id, title: T('gui.new_task'), last: rowPreview(text) || T('gui.sess.not_started'),
      when: T('gui.sess.just_now'), at: Math.floor(Date.now() / 1000), run: null, live: true, persisted: false };
    sessionRows().unshift(s); sessionSet(s.id); draft = false;
    turnOwner = sessionCurrent();
    // The composer was owned by 'new' until this point; keep later keystrokes
    // filed under the session that just came into being.
    claimDraft(sessionCurrent());
    await applyStagedModel(s.id, sendGen);
    beginNaming(text);
    sessionDraw();
    await subscribe(s.id);
    const sent = await rpc.call('turn.send', { session_key: sessionCurrent(), content: text, ...mediaOf(text) });
    if (sent && sent.naming === false) namingDeclined(s.id);
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
  RavenIslands.transcript.artifacts(RavenIslands.workspace.currentTurn());
  resetTurnState();
  drawMeter(); goState(); sessionDraw();
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
      if (sessionCurrent() === owner) { drawMeter(); goState(); sessionDraw(); drainQueue(); }
    }, () => {
      transitionTurn(owner, { type: 'idle' });
      if (sessionCurrent() === owner) { drawMeter(); goState(); sessionDraw(); }
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
  const transition = RavenIslands.rail.removeRow(sessionRows(), sessionCurrent(), sessionId);
  sessionReplace(transition.rows);
  if (transition.kind === 'unchanged') { sessionDraw(); return; }
  if (transition.kind === 'open') {
    sessionSet(transition.next.id);
    await sessionOpen(transition.next);
    return;
  }
  $('#ta').value = '';
  startDraft();
}

async function leaveArchivedSession(sessionId) {
  const transition = RavenIslands.rail.removeRow(sessionRows(), sessionCurrent(), sessionId);
  sessionReplace(transition.rows);
  if (transition.kind === 'unchanged') { sessionDraw(); return; }
  if (transition.kind === 'open') {
    sessionSet(transition.next.id);
    await sessionOpen(transition.next);
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
    const at = sessionRows().findIndex(row => row.id === s.id);
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
          if (!sessionRows().some(row => row.id === s.id)) sessionRows().splice(Math.max(0, Math.min(at, sessionRows().length)), 0, s);
          sessionDraw();
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
DS.sessions.renamed = (id, title, previous) => {
  /* A refused rename must not stay quiet -- same reason `DS.sessions.pin` puts
     its flag back. The row moved optimistically, so a name the server rejected
     (too long for the metadata record) looks identical to one it took until the
     page is reloaded and the old name is simply back. */
  rpc.call('session.title', { session_id: id, title }).catch((e) => {
    const s = sess(id);
    if (s) { s.title = previous; sessionDraw(); }
    if (id === sessionCurrent()) {
      const h = $('#title');
      if (h) h.textContent = plainTitle(previous);
    }
    toast(T('gui.sess.rename_failed', { detail: (e && (e.message || e.detail)) || String(e) }));
  });
};
