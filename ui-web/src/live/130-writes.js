/* ---- the writes the live layer can actually make --------------------- */

/* One delete per session, and a session that refuses stays in the list -- the
   rail must never claim something is gone while the file is still on disk. */
DS.sessions.deleteAll = async () => {
  const gone = [];
  for (const s of sessionRows().slice()) {
    try {
      /* A refusal is a SUCCESSFUL response, not a rejection: `session.delete`
         answers `{deleted: null, still_on_disk: true}` for a removal the
         filesystem refused. Awaiting alone caught only the transport failures,
         so a refusal counted as a removal -- which is exactly what the note
         above forbids, and the row came back on the next reload.

         The predicate is `DS.sessions.remove`'s, character for character, because
         the two must not disagree about one answer: drop the row when the file
         was removed, and when there was nothing to remove; keep it when the
         file survived, or when a server too old to carry the field leaves the
         question open. */
      const r = await rpc.call('session.delete', { session_id: s.id });
      if (r.deleted !== s.id && r.still_on_disk !== false) continue;
      gone.push(s.id); dropDraft(s.id);
    } catch { /* counted by what is left below */ }
  }
  sessionReplace(sessionRows().filter((s) => !gone.includes(s.id)));
  sessionSet(null);
  startDraft();
  drawSettings();
  toast(sessionRows().length
    ? T('gui.set.dat.del_partial', { n: gone.length, left: sessionRows().length })
    : T('gui.set.dat.del_done', { n: gone.length }));
};
