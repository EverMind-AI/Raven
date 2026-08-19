/* ---- the writes the live layer can actually make --------------------- */

/* One delete per session, and a session that refuses stays in the list -- the
   rail must never claim something is gone while the file is still on disk. */
deleteAllSessions = async () => {
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

/* The version check the rail-foot notice already does, on demand. No new
   backend: system.version carries the answer. */
checkUpdate = async (btn) => {
  const was = btn.textContent;
  btn.textContent = T('gui.set.checking'); btn.disabled = true;
  try {
    /* check:true = fetch now, not the daily cache: the button says 检查更新,
       and a person who just clicked it is asking about now. */
    const v = await rpc.call('system.version', { check: true });
    if (v.raven_version) APP_VERSION = v.raven_version;
    if (v.update_available) {
      showUpNote('ver', v.latest_version);
      drawSettings();
      askUpgrade();
      return;
    }
    /* The answer has to land on the button: this layer sends toasts to the
       console, and "nothing happened" is indistinguishable from a broken
       check. */
    btn.disabled = false;
    btn.textContent = T('gui.set.abt.latest');
    setTimeout(() => { btn.textContent = was; }, 2200);
    return;
  } catch (e) {
    btn.textContent = T('gui.set.abt.check_fail');
    setTimeout(() => { btn.textContent = was; }, 2600);
    if (window.console) console.error('[update check]', e);
  }
  btn.disabled = false;
};

