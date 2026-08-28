/* -- real session actions: branch / clear ---------------------------- */
/* The answer block is the transcript island's; what stays here is the real
   branch action its footer offers. Branching acts on the current session -- the OPEN
   conversation -- and the island only offers it on the main lane, so a
   delegated run's pane never claims to fork a session it does not have. */
DS.transcript.branch = () => {
  rpc.call('session.branch', { session_id: sessionCurrent() })
    .then((r) => {
      if (!r.session_id) { toast('这个会话还没有内容，无法分叉'); return; }
      const s = { id: r.session_id, title: r.title || T('gui.sess.branch_title'),
        last: T('gui.sess.branched'), when: T('gui.sess.just_now'),
        at: Math.floor(Date.now() / 1000), run: null, live: true };
      sessionRows().unshift(s); sessionSet(s.id); sessionDraw(); sessionOpen(s);
      toast(`已分叉，带上了 ${r.message_count || 0} 条消息`);
    })
    .catch((e) => toast(`分叉失败：${e.message || e}`));
};

DS.composer.slash.forEach((x) => {
  if (x.id === 'gui.clear') {
    x.fn = () => confirmAsk(T('gui.clear_title'), T('gui.clear_body'), T('gui.clear_yes'), () => {
      /* Which conversation was cleared, read once. The reply used to ask for the
         pointer again, and by then it can name a different one: clearing A and
         clicking B mid-flight wiped B's stage, gave B the new-task layout, and
         stamped B's row "cleared" while B's transcript sat untouched on disk. */
      const key = sessionCurrent();
      rpc.call('session.clear', { session_id: key })
        .then(() => {
          /* The row belongs to the conversation that was cleared, wherever the
             reader is now -- it really is empty, and a list that says otherwise
             is wrong until the next reload. */
          const s = sess(key); if (s) s.last = T('gui.sess.cleared');
          sessionDraw();
          /* The stage and the meter are the open conversation's, so they are
             only this reply's to touch while it IS the open one. */
          if (key !== sessionCurrent()) return;
          $('#stage').innerHTML = ''; pitch();
          drawMeter();
        })
        /* A failure is news for the conversation it happened to. Posted on
           whatever is open, it reads as that conversation refusing to clear;
           dropped, the reader walks away believing a session was wiped when its
           transcript is still on disk, which is the one direction where being
           wrong costs something. So it goes where the other session actions put
           theirs -- a toast naming the conversation, same as delete, archive and
           rename in this file. Not the row's `status = 'err'` channel: nothing
           clears that (openLiveSession only clears 'done'), so it would pin a
           failure marker on a row whose conversation is fine once opened. */
        .catch((e) => {
          const detail = (e.data && e.data.detail) || e.message || String(e);
          if (key !== sessionCurrent()) {
            const s = sess(key);
            toast(T('gui.sess.clear_failed', { title: plainTitle((s && s.title) || key), detail }));
            return;
          }
          noteRow(T('gui.clear_title'), detail);
        });
    });
  }
  if (x.id === 'gui.compress') x.fn = compressNow;
});

/* Manual compaction. The runtime already compacts when a prompt outgrows the
   window; this forces the same pass early, which is what you want once the
   earlier half of a session has stopped being useful. */
async function compressNow() {
  const key = sessionCurrent();
  if (!key || draft) return;
  const line = noteRow(T('gui.compress.running'), '', { quiet: true, host: $('#stage') });
  try {
    const r = await rpc.call('session.compress', { session_id: key });
    noteSay(line, r.removed
      ? T('gui.compress.done', { n: r.removed, before: fmtTok(r.before_tokens), after: fmtTok(r.after_tokens) })
      : T('gui.compress.noop'), '');
  } catch (e) {
    line.remove();
    /* Same rule as the clear handler, and here it is the failure path that
       needed it: `line` is a segment in the lane this started in, so a switch
       has already dropped it and writing to it lands nowhere -- but a bare
       noteRow asks for the CURRENT lane, so a compaction that failed for the
       conversation being left posted its error over the one being read. */
    const detail = (e.data && e.data.detail) || e.message || String(e);
    if (key !== sessionCurrent()) {
      const s = sess(key);
      toast(T('gui.sess.compress_failed', { title: plainTitle((s && s.title) || key), detail }));
      return;
    }
    noteRow(T('gui.compress.fail', { err: '' }).replace(/[:：]\s*$/, ''), detail);
  }
  /* The tail this scrolls is the open conversation's. */
  if (key !== sessionCurrent()) return;
  down();
}

// Dev-only hook: lets a design pass preview the clarify sheet without
// spending a model turn (window.__clarify({question, choices})).
window.__clarify = (p) => rpc.notify['clarify.request'](p || { request_id: 'dev', question: '预览', choices: ['A', 'B'] });

// Same reason: the update row's version state only appears when a release is
// actually newer, which never happens on a dev checkout
// (window.__upnote('ver', '0.1.11')).
window.__upnote = (kind, latest) => showUpNote(kind || 'ver', latest);
