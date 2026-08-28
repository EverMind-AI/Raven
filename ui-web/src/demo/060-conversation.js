/* ══ module 1b: the conversation ══════════════════════════════════ */
function openDemoSession(s) {
  parkDraft(); loadDraft(s.id);
  // Opening it IS reading it: the finished marker has done its job and the
  // row goes back to carrying its timestamp.
  if (s.status === 'done') s.status = null;
  markNewCurrent();
  stop_(); turn.dispatch({ type: 'idle' }); queueClear(); use = null;
  wsReset();
  setWs(false);
  $('#title').textContent = plainTitle(s.title);
  $('#stage').innerHTML = '';
  $('#flash').textContent = '';
  drawMeter(); goState(); drawBanner();
  if (s.run) {
    const r = RUNS[s.run];
    use = r.use;
    setCtx(((r.use && r.use.in) || 0) + ((r.use && r.use.out) || 0), 200000);
    ask(r.ask); replay(r, true);
  } else if (s.status === 'err') {
    ask('把支付回调那块拆成两个 handler');
    noteRow('找不到模块 stripe（internal/pay/callback.go:12）', '装上依赖或改用内置 http 客户端后重试。');
  } else {
    pitch();
  }
}

/* The fixture half of DS.banner, and only that half. `cap` reads the capability
   list, which the live layer does fill in place -- but live mode does not use
   this reading of it: live/120-settings.js installs a source that refuses the
   suggestion outright, because a config gap belongs in the settings page, not
   as a strip over every conversation.
   Live mode installs its source before the shared deferred boot, so this
   fixture source is never consulted for a live page's first paint. */
DS.banner ??= {
  websearchNeeds: () => { const c = cap('websearch'); return !!c && c.state === 'need'; },
};

function pitch() {
  /* The empty state is the composer itself, moved to the visual centre --
     no mark, no facts, no title. unpitch() lifts the flag again. */
  const c = document.querySelector('.chat');
  if (c) c.dataset.fresh = '1';
}

/* Leaving the empty state. Its own element, because the flag drives a whole
   layout -- the wordmark, the crew, a centred composer 81px above where the
   dock sits, and an opaque card instead of the glass one -- so every path out
   of it has to lift the flag at the same moment, and there are four of them
   (content arriving, a stored conversation replaying, and either kind of
   switch through resetView). */
function unpitch() {
  const c = document.querySelector('.chat');
  if (c) delete c.dataset.fresh;
}

/* The composer appends an "[attachments]" note plus "- path" bullets for the
   model; the reader gets chips instead. Parsed against both language variants
   of the note, since history may have been written under the other one. */
function splitAtts(text) {
  const notes = Object.values(I18N.ui['gui.att.note'] || {});
  for (const note of notes) {
    if (!note) continue;
    const ix = text.lastIndexOf('\n\n' + note + '\n');
    if (ix < 0) continue;
    const tail = text.slice(ix + note.length + 3).split('\n');
    if (!tail.length || !tail.every((l) => !l.trim() || /^- /.test(l))) continue;
    return {
      body: text.slice(0, ix),
      atts: tail.filter((l) => /^- /.test(l)).map((l) => l.slice(2).trim()),
    };
  }
  return { body: text, atts: [] };
}

function ask(text, when) {
  unpitch();
  RavenIslands.transcript.setStuck(true);
  /* The bubble, its attachment chips and its footer are the island's. */
  RavenIslands.transcript.ask(text, when);
}

/* Writes what the row shows and what it can give back, together; `row` is
   the island handle noteRow returned. */
function noteSay(row, label, detail) {
  row.set(label, detail);
}

/* The one row for everything raven says about itself in the transcript, so a
   new kind of notice cannot arrive wearing its own shape. `label` leads, the
   detail follows after a `·`, and the whole thing is one line: a failure and a
   framework note differ only in colour. Options: `quiet` for a framework note
   (no failure happened, so no red) and `retry`, which is the caller's -- this
   row cannot know what should happen next. Omit it and no action appears. */
function noteRow(label, detail, opts) {
  const o = opts || {};
  /* The row is an island segment now; the handle keeps the two verbs the
     shell still uses on it (noteSay's set, compressNow's remove). `host`
     needs no forwarding: the island's main lane IS the #stage transcript,
     and a delegated pane draws its own notes from its own record. */
  return RavenIslands.transcript.note(label, detail,
    { quiet: !!o.quiet, retry: typeof o.retry === 'function' ? o.retry : null });
}
