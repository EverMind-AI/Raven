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

