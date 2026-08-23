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
      SESS.unshift(s); sessionSet(s.id); drawList(); openSession(s);
      toast(`已分叉，带上了 ${r.message_count || 0} 条消息`);
    })
    .catch((e) => toast(`分叉失败：${e.message || e}`));
};

DS.composer.slash.forEach((x) => {
  if (x.id === 'gui.clear') {
    x.fn = () => confirmAsk(T('gui.clear_title'), T('gui.clear_body'), T('gui.clear_yes'), () => {
      rpc.call('session.clear', { session_id: sessionCurrent() })
        .then(() => {
          $('#stage').innerHTML = ''; pitch();
          const s = sess(sessionCurrent()); if (s) s.last = T('gui.sess.cleared');
          drawMeter(); drawList();
        })
        .catch((e) => noteRow(T('gui.clear_title'), (e.data && e.data.detail) || e.message || String(e)));
    });
  }
  if (x.id === 'gui.compress') x.fn = compressNow;
});

/* Manual compaction. The runtime already compacts when a prompt outgrows the
   window; this forces the same pass early, which is what you want once the
   earlier half of a session has stopped being useful. */
async function compressNow() {
  if (!sessionCurrent() || draft) return;
  const line = noteRow(T('gui.compress.running'), '', { quiet: true, host: $('#stage') });
  try {
    const r = await rpc.call('session.compress', { session_id: sessionCurrent() });
    noteSay(line, r.removed
      ? T('gui.compress.done', { n: r.removed, before: fmtTok(r.before_tokens), after: fmtTok(r.after_tokens) })
      : T('gui.compress.noop'), '');
  } catch (e) {
    line.remove();
    noteRow(T('gui.compress.fail', { err: '' }).replace(/[:：]\s*$/, ''),
      (e.data && e.data.detail) || e.message || String(e));
  }
  down();
}

// Dev-only hook: lets a design pass preview the clarify sheet without
// spending a model turn (window.__clarify({question, choices})).
window.__clarify = (p) => rpc.notify['clarify.request'](p || { request_id: 'dev', question: '预览', choices: ['A', 'B'] });

// Same reason: the update row's version state only appears when a release is
// actually newer, which never happens on a dev checkout
// (window.__upnote('ver', '0.1.11')).
window.__upnote = (kind, latest) => showUpNote(kind || 'ver', latest);
