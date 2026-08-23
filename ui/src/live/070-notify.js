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
    refreshList();
  }
};

/* Approval wears the ask_user sheet (approveSheet), so a blocked turn always
   interrupts in the same place and shape. Closing it is a denial, never a
   silent drop -- the engine is waiting on an answer either way.

   Filed under the conversation the server says it asked on behalf of, for the
   same reason clarify.request is (below): the request belongs to the turn that
   raised it, not to whichever conversation the reader had open when it landed.
   A frame that names none -- a dispatch with no conversation to name -- keeps
   the old fallback and docks where the reader is. */
rpc.notify['confirm.request'] = (p) => {
  const say = (answer) =>
    rpc.call('confirm.respond', { request_id: p.request_id, answer }).catch(() => {});
  approveSheet(p.prompt || '', () => say(true), () => say(false), p.conversation_id);
};

/* The question the agent asks mid-turn. The sheet is the island's
   (features/composer/clarify.ts); what is left here is the transport and the
   step marking -- it answers with one string, whichever control the reader
   used, including the skip, whose wording is the sheet's copy.

   No echo row: the asking tool's own row renders the full question-to-answer
   exchange in its detail once the tool returns, so a separate answered line
   would say the same thing twice. The step is still marked hasQA so the
   exchange keeps its own step instead of merging into a silent work run. */
rpc.notify['clarify.request'] = (p) => {
  clarifySheet(p, (answer) => {
    rpc.call('clarify.respond', { request_id: p.request_id, answer }).catch(() => {});
    if (live.st) live.st.hasQA = true;
  });
};
