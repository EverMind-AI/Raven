/* ---- notifications ------------------------------------------------ */
const notifyTurn = (owner, event) => {
  transitionTurn(owner, event);
  if (owner === sessionCurrent()) { drawMeter(); goState(); sessionDraw(); }
};

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
    // until they open it. sessionOpen is what clears it. A cancel is a stop
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
  const owner = p.conversation_id || sessionCurrent();
  notifyTurn(owner, { type: 'wait' });
  const say = (answer) => {
    notifyTurn(owner, { type: 'resume' });
    rpc.call('confirm.respond', { request_id: p.request_id, answer }).catch(() => {});
  };
  approveSheet(p.prompt || '', () => say(true), () => say(false), owner);
};

/* The permission gate's ask. Same docking rules as confirm above; what an
   answer is differs: allow once, deny (the agent reads the refusal and keeps
   going), or deny and stop the turn, with an optional note that rides to the
   model as the refusal's reason. The engine fails closed on its own deadline,
   and approval.closed below is how this sheet learns the question is over. */
rpc.notify['approval.request'] = (p) => {
  const owner = p.conversation_id || sessionCurrent();
  notifyTurn(owner, { type: 'wait' });
  approvalSheet(
    { approvalId: p.approval_id, command: p.command || '', description: p.description || '' },
    (choice, feedback) => {
      notifyTurn(owner, { type: 'resume' });
      const params = { approval_id: p.approval_id, choice, session_id: owner };
      if (feedback) params.feedback = feedback;
      rpc.call('approval.respond', params).catch(() => {});
    },
    owner,
  );
};

rpc.notify['approval.closed'] = (p) => {
  notifyTurn(p.conversation_id || sessionCurrent(), { type: 'resume' });
  approvalClose(p.approval_id);
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
  const owner = p.conversation_id || sessionCurrent();
  notifyTurn(owner, { type: 'wait' });
  clarifySheet(p, (answer) => {
    notifyTurn(owner, { type: 'resume' });
    rpc.call('clarify.respond', { request_id: p.request_id, answer }).catch(() => {});
    if (live.st) live.st.hasQA = true;
  });
};

/* The question is over and nobody answered it: it timed out, its turn was
   interrupted, or a later question replaced it. Only the server knows -- a sheet
   cannot tell "still waiting" from "waited out" -- so until it said so the sheet
   stayed up offering an answer that had nowhere to go. The turn resumes for the
   same reason it resumes on an answer: it is no longer blocked on the reader. */
rpc.notify['clarify.closed'] = (p) => {
  notifyTurn(p.conversation_id || sessionCurrent(), { type: 'resume' });
  clarifyClose(p.request_id);
};
