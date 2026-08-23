/* ---- the writes the live layer can actually make --------------------- */

/* One delete per session, and a session that refuses stays in the list -- the
   rail must never claim something is gone while the file is still on disk. */
DS.sessions.deleteAll = async () => {
  const gone = [];
  for (const s of SESS.slice()) {
    try {
      await rpc.call('session.delete', { session_id: s.id });
      gone.push(s.id); dropDraft(s.id);
    } catch { /* counted by what is left below */ }
  }
  SESS = SESS.filter((s) => !gone.includes(s.id));
  cur = null;
  startDraft();
  drawSettings();
  toast(SESS.length
    ? T('gui.set.dat.del_partial', { n: gone.length, left: SESS.length })
    : T('gui.set.dat.del_done', { n: gone.length }));
};
