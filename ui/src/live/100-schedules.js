/* -- schedules ------------------------------------------------------- */
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

async function loadCrons() {
  const r = await rpc.call('cron.list', {});
  CRONS.length = 0;
  r.jobs.map(cronToRow).forEach((x) => CRONS.push(x));
}

let cronLoaded = false;
openCron = async function () {
  cronView = null;
  showPage('cronPage');
  if (cronLoaded) { drawCron(); drawCronBdg(); }
  else $('#cronBody').innerHTML = '';
  try { await loadCrons(); cronLoaded = true; } catch (e) { toast(`加载失败：${e.message || e}`); }
  drawCron(); drawCronBdg();
};
const reloadCronPage = () => loadCrons().then(() => { drawCron(); drawCronBdg(); }).catch(() => {});

runNow = function (j) {
  rpc.call('cron.run_now', { id: j.id })
    .then(() => { toast(T('gui.cron.triggered_x', { name: j.name })); reloadCronPage(); })
    .catch((e) => toast(`触发失败：${e.message || e}`));
};

openRun = function (j) {
  closeCron();
  const s = { id: `cron:${j.id}`, title: j.name, last: '', when: '',
    at: Math.floor(Date.now() / 1000), run: null, live: true, from: 'cron' };
  if (!sess(s.id)) SESS.unshift(s);
  cur = s.id; drawList(); openSession(s);
};

cronToggle = (j) => {
  rpc.call('cron.set_enabled', { id: j.id, enabled: !j.on })
    .then(() => { toast(!j.on ? `已恢复「${j.name}」` : `已暂停「${j.name}」`); reloadCronPage(); })
    .catch((e) => toast(`操作失败：${e.message || e}`));
};
cronDelete = (j) => {
  rpc.call('cron.delete', { id: j.id })
    .then(() => { cronView = null; toast(T('gui.cron.deleted_x', { name: j.name })); reloadCronPage(); })
    .catch((err) => toast(`删除失败：${err.message || err}`));
};
cronRunsLoad = (j) => rpc.call('cron.runs', { id: j.id })
  .then((r) => (r.runs || []).map((x) => ({
    at: x.at_ms ? fmtStamp(x.at_ms) : '—', ok: !!x.ok, note: x.preview || '',
  })));
cronPersist = (draft, done) => {
  let payload;
  try { payload = jobToSave(draft); } catch (e) { jobRefuse(draft, e); return; }
  rpc.call('cron.save', payload)
    .then((r) => loadCrons().then(() => {
      if (done) done(CRONS.find((x) => x.id === r.job.id) || cronToRow(r.job));
      drawCronBdg();
    }))
    .catch((e) => toast(`保存失败：${(e.data && e.data.detail) || e.message || e}`));
};

cronRow = function (j) {
  const last = j.runs[0];
  const r = mk('div', 'cronjob' + (j.on && last && !last.ok ? ' bad' : ''));
  r.style.cursor = 'pointer';
  r.onclick = (e) => { if (e.target.closest('button')) return; openCronDetail(j); };
  const nm = mk('div', 'nm');
  nm.appendChild(mk('span', 'dot' + (!j.on ? '' : last && !last.ok ? ' err' : ' run')));
  nm.appendChild(mk('span', null, j.name));
  nm.appendChild(mk('span', 'when', j.when));
  r.appendChild(nm);
  const meta = mk('div', 'mo');
  meta.textContent = j.on ? T('gui.cron.next_only', { next: j.next }) : T('gui.cron.paused');
  r.appendChild(meta);
  const foot = mk('div', 'foot');
  if (last) {
    const chip = mk('button', 'mini ghost');
    chip.textContent = T('gui.cron.last_run_short',
      { at: last.at, state: T(last.ok ? 'gui.cron.ok' : 'gui.cron.failed') });
    if (!last.ok) chip.classList.add('danger');
    chip.onclick = () => openRun(j, last);
    foot.appendChild(chip);
    foot.appendChild(mk('span', 'mono', last.note)).style.cssText =
      'font-size:11px;color:var(--faint);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0;flex:1';
  } else {
    foot.appendChild(mk('span', 'mono', T('gui.cron.never'))).style.cssText = 'font-size:11px;color:var(--faint)';
  }
  r.appendChild(foot);
  const ctl = mk('div', 'ctl');
  const s = mk('button', 'swi');
  s.setAttribute('role', 'switch');
  s.setAttribute('aria-checked', String(j.on));
  s.setAttribute('aria-label', T('gui.caps.toggle_aria', { name: j.name }));
  s.onclick = () => cronToggle(j);
  const more = mk('button', 'mini ghost', '⋯');
  more.setAttribute('aria-label', T('gui.cron.menu_aria', { name: j.name }));
  more.onclick = (e) => {
    const b = e.currentTarget.getBoundingClientRect();
    menuAt(b.right - 150, b.bottom + 6, [
      { label: T('gui.cron.run_now'), fn: () => runNow(j) },
      { label: T('gui.cron.history'), fn: () => openCronDetail(j) },
      { label: T('gui.cron.open_session'), fn: () => openRun(j) },
      '-',
      { label: T('gui.cron.delete'), bad: true, fn: () => confirmAsk(T('gui.cron.delete_title'),
          T('gui.cron.delete_body', { name: j.name }), T('gui.cron.delete'), () => cronDelete(j)) },
    ]);
  };
  ctl.append(s, more);
  r.appendChild(ctl);
  return r;
};

/* Which message a refusal shows, keyed by what jobToSave could not read. The
   sheet is redrawn so the note lands under the control the reader has to fix;
   a toast would not, because live mode sends those to the console. */
const JOB_BAD = { 'no instant': 'gui.job.need_instant', 'bad weekday': 'gui.job.bad_weekday' };
function jobRefuse(draft, err, redraw) {
  draft.bad = JOB_BAD[err && err.message] || 'gui.job.bad_time';
  if (redraw) redraw();
  else if (typeof reloadCronPage === 'function') reloadCronPage();
}

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

$('#jobYes').onclick = () => {
  const j = jobDraft;
  if (!j || !j.name.trim() || !j.what.trim()) { j.blank = true; openJobSheet(j); return; }
  let payload;
  try { payload = jobToSave(j); } catch (e) { jobRefuse(j, e, () => openJobSheet(j)); return; }
  rpc.call('cron.save', payload)
    .then((r) => {
      $('#jobVeil').dataset.open = 'false'; jobDraft = null;
      toast(T('gui.job.saved_x', { name: r.job.name }),
        { label: T('gui.job.run_once'), fn: () => runNow({ id: r.job.id, name: r.job.name }) });
      reloadCronPage();
    })
    .catch((e) => toast(`保存失败：${(e.data && e.data.detail) || e.message || e}`));
};

