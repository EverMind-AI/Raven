/* -- schedules: the rpc source ---------------------------------------
   The page (demo/140-schedule.js) owns the one renderer and all chrome;
   this file only knows how to speak cron.* over /rpc. Installing onto the
   seam replaces the fixture source before the first paint. */
function cronToRow(j) {
  const when = j.kind === 'cron' ? cronExprHuman(j.expr)
    : j.kind === 'every' ? T('gui.cron.every', { every: fmtEvery(j.every_ms) })
    : T('gui.cron.once', { at: fmtStamp(j.at_ms) });
  const runs = j.last_run_at_ms
    ? [{ at: fmtStamp(j.last_run_at_ms), ok: j.last_status !== 'error', ms: 0,
        note: j.last_error || (j.last_status === 'ok' ? T('gui.cron.ok') : j.last_status || ''), sid: null }]
    : [];
  /* `at` is the server's third kind, and mapping it to 'day' is what let the
     editor rewrite a one-shot into a daily job. It has its own frequency now,
     and carries its instant in the shape the datetime input reads. */
  /* The offset of the instant being converted, not of today: `new Date()` with
     no argument is now, so a job on the other side of a DST boundary displayed
     -- and re-saved -- an hour off. Same shape as the bug above it, one layer
     down: a value re-derived through a conversion that does not know which
     instant it is converting. */
  const local = j.kind === 'at' && j.at_ms
    ? new Date(j.at_ms - new Date(j.at_ms).getTimezoneOffset() * 60000).toISOString().slice(0, 16) : '';
  return { id: j.id, name: j.name, on: j.enabled, what: j.message,
    freq: j.kind === 'cron' ? 'cron' : j.kind === 'every' ? 'hour' : 'once',
    at: j.kind === 'cron' ? j.expr : '', at_local: local,
    when, next: j.enabled ? fmtStamp(j.next_run_at_ms) : T('gui.cron.paused'),
    deliver: 'app', runs, kind: j.kind, every_ms: j.every_ms, at_ms: j.at_ms, tzv: j.tz };
}

DS.cron = {
  rows: () => rpc.call('cron.list', {}).then((r) => r.jobs.map(cronToRow)),
  toggle: (j) => rpc.call('cron.set_enabled', { id: j.id, enabled: !j.on })
    .then(() => toast(T(!j.on ? 'gui.cron.resumed_x' : 'gui.cron.paused_x', { name: j.name })))
    .catch((e) => toast(`操作失败：${e.message || e}`)),
  /* Toasted here, and still rejected: the caller's success branch closes the
     job's page, so resolving after a failed delete would bounce the reader
     back to a list where the row they just deleted is still there. */
  remove: (j) => rpc.call('cron.delete', { id: j.id })
    .then(() => toast(T('gui.cron.deleted_x', { name: j.name })))
    .catch((err) => {
      toast(`删除失败：${err.message || err}`);
      throw { handled: true };
    }),
  /* `async` is load bearing, not decoration: `jobToSave` reports a bad draft by
     throwing, and a plain arrow would throw it before the caller's
     `.then(...).catch(...)` chain exists -- so the refusal never reaches
     `jobRefuse` and the reader gets a dead button instead of the note that
     says which field is wrong. An async function turns that into a rejection
     the existing catch already handles. */
  save: async (draft) => {
    const payload = jobToSave(draft);
    return rpc.call('cron.save', payload).then((r) => cronToRow(r.job)).catch((e) => {
      toast(`保存失败：${(e.data && e.data.detail) || e.message || e}`);
      throw { handled: true };
    });
  },
  runs: (j) => rpc.call('cron.runs', { id: j.id })
    .then((r) => (r.runs || []).map((x) => ({
      at: x.at_ms ? fmtStamp(x.at_ms) : '—', ok: !!x.ok, note: x.preview || '',
    }))),
  runNow: (j) => rpc.call('cron.run_now', { id: j.id })
    .then(() => toast(T('gui.cron.triggered_x', { name: j.name })))
    .catch((e) => toast(`触发失败：${e.message || e}`)),
  openRun: async (j) => {
    closeCron();
    const s = { id: `cron:${j.id}`, title: j.name, last: '', when: '',
      at: Math.floor(Date.now() / 1000), run: null, live: true, from: 'cron' };
    if (!sess(s.id)) SESS.unshift(s);
    sessionSet(s.id); drawList(); openSession(s);
  },
};

function jobToSave(j) {
  const base = { name: j.name.trim(), message: j.what.trim() };
  if (j.id && !j.fresh) base.id = j.id;
  if (j.freq === 'hour') {
    return { ...base, kind: 'every', every_seconds: j.every_ms ? Math.round(j.every_ms / 1000) : 3600 };
  }
  if (j.freq === 'cron') return { ...base, kind: 'cron', expr: j.at.trim() };
  if (j.freq === 'once') {
    if (!j.at_local) throw new Error('no instant');
    return { ...base, kind: 'at', at_iso: j.at_local };
  }
  /* A time the reader typed, and nothing else read back out of prose: the
     weekday is a number the control produced. */
  const hm = j.at.match(/^\s*(\d{1,2}):(\d{2})\s*$/);
  if (!hm) throw new Error('bad time');
  const [h, m] = [Number(hm[1]), Number(hm[2])];
  if (h > 23 || m > 59) throw new Error('bad time');
  if (j.freq === 'week') {
    const wd = Number(j.wd);
    if (!Number.isInteger(wd) || wd < 0 || wd > 6) throw new Error('bad weekday');
    return { ...base, kind: 'cron', expr: `${m} ${h} * * ${wd}` };
  }
  return { ...base, kind: 'cron', expr: `${m} ${h} * * *` };
}
