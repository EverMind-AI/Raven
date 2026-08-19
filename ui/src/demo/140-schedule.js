/* ══ module 3b: data & memory — live.js supplies the real loader ══ */
function openMem() { showPage('memPage'); drawMem(); }
function closeMem() { showPage(null); }
function drawMem() {
  const box = $('#memBody'); box.innerHTML = '';
  const hero = mk('div', 'pmhero');
  hero.appendChild(mk('h3', null, T('gui.mem.hero')));
  box.appendChild(hero);
  box.appendChild(mk('div', 'empty-note', T('gui.mem.down')));
}

/* ══ module 4: scheduled work ═════════════════════════════════════
   Each run produces a session, so every row links to what it actually
   did. A schedule you cannot inspect is a schedule you stop trusting. */
function openCron() { cronView = null; showPage('cronPage'); drawCron(); }
function closeCron() { showPage(null); }

/* Detail state: which job the page is showing, and the edit draft that
   survives redraws while the reader types. */
let cronView = null;
let cronDraft = null;
function openCronDetail(j) { cronView = j.id; cronDraft = { ...j }; drawCron(); }

/* layer hooks: the demo mutates CRONS in place; live.js repoints these
   at the cron.* RPCs. */
let cronToggle = (j) => {
  j.on = !j.on;
  j.next = j.on ? (j.at.match(/\d{2}:\d{2}/) || ['08:00'])[0] : T('gui.cron.paused');
  drawCron(); drawCronBdg();
  toast(T(j.on ? 'gui.cron.resumed_x' : 'gui.cron.paused_x', { name: j.name }));
};
let cronDelete = (j) => {
  const i = CRONS.indexOf(j); if (i >= 0) CRONS.splice(i, 1);
  cronView = null; drawCron(); drawCronBdg();
  toast(T('gui.cron.deleted_x', { name: j.name }));
};
let cronRunsLoad = async (j) => (j.runs || []);
let cronPersist = (draft, done) => {
  const j = CRONS.find((x) => x.id === draft.id);
  if (j) Object.assign(j, draft, { when: cronWhen(draft) });
  if (done) done(j || draft);
};

/* What a schedule means, in words. The raw five-field expression stays in
   the editor for whoever writes one, but nobody should have to read it. */
function cronExprHuman(expr) {
  const p = String(expr || '').trim().split(/\s+/);
  const raw = `cron ${expr}`;
  if (p.length !== 5) return raw;
  const [m, h, dom, mon, dow] = p;
  if (dom !== '*' || mon !== '*') return raw;
  const pad = (n) => String(n).padStart(2, '0');
  let day;
  if (dow === '*') day = T('gui.cron.h.daily');
  else if (dow === '1-5') day = T('gui.cron.h.weekdays');
  else if (dow === '0,6' || dow === '6,0') day = T('gui.cron.h.weekend');
  else if (/^[0-6](,[0-6])*$/.test(dow)) {
    day = dow.split(',').map((d) => T('gui.cron.h.dow' + d)).join('、');
  } else return raw;
  let time;
  const mN = /^\d+$/.test(m), hN = /^\d+$/.test(h);
  const hRange = h.match(/^(\d+)-(\d+)$/);
  const mStep = m.match(/^\*\/(\d+)$/), hStep = h.match(/^\*\/(\d+)$/);
  const mList = /^\d+(,\d+)+$/.test(m) ? m.split(',').map(Number) : null;
  if (mN && hN) time = `${pad(h)}:${pad(m)}`;
  else if (mN && /^\d+(,\d+)+$/.test(h)) time = h.split(',').map((x) => `${pad(x)}:${pad(m)}`).join('、');
  else if (hStep && mN) time = T('gui.cron.h.every_h', { n: hStep[1] });
  else if (mStep && h === '*') time = T('gui.cron.h.every_m', { n: mStep[1] });
  else {
    const span = hRange ? `${pad(hRange[1])}:00–${pad(hRange[2])}:59`
      : h === '*' ? T('gui.cron.h.allday') : null;
    let mm = null;
    if (mList) {
      mm = mList.length === 2 && mList[0] === 0 && mList[1] === 30
        ? T('gui.cron.h.half') : T('gui.cron.h.at_min', { m: mList.map(pad).join('/') });
    } else if (mStep) mm = T('gui.cron.h.every_m', { n: mStep[1] });
    else if (mN && hRange) mm = T('gui.cron.h.at_min', { m: pad(m) });
    if (span == null || mm == null) return raw;
    time = `${span} ${mm}`;
  }
  return `${day} ${time}`;
}
/* Demo rows carry a hand-written `when`; a real cron expr goes through the
   translator. live.js routes every kind through here via cronToRow. */
function cronWhen(j) {
  return j.freq === 'cron' && j.at ? cronExprHuman(j.at) : j.when;
}

function drawCron() {
  const box = $('#cronBody'); box.innerHTML = '';

  if (cronView) {
    const j = CRONS.find((x) => x.id === cronView);
    if (j) { drawCronDetail(box, j); return; }
    cronView = null;
  }

  const row = mk('div', 'herorow');
  const hero = mk('div', 'pmhero');
  hero.appendChild(mk('h3', null, T('gui.cron.hero')));
  row.appendChild(hero);
  const add = mk('button', 'mini gold', T('gui.cron_new'));
  add.onclick = () => openJobSheet();
  row.appendChild(add);
  box.appendChild(row);

  const fail = CRONS.filter((j) => j.on && j.runs[0] && !j.runs[0].ok);
  if (fail.length) {
    const b = mk('div', 'banner'); b.style.margin = '0 0 18px';
    b.append(mk('b', null, T('gui.cron.failing', { n: fail.length })));
    b.appendChild(mk('span', null, fail.map((j) => j.name).join(', ')));
    const go = mk('button', null, T('gui.cron.why'));
    go.onclick = () => openRun(fail[0], fail[0].runs[0]);
    b.appendChild(go);
    box.appendChild(b);
  }

  if (!CRONS.length) {
    box.appendChild(mk('div', 'empty-note', T('gui.cron.none')));
    return;
  }

  const set = mk('div', 'cronlist');
  CRONS.forEach((j) => set.appendChild(cronRow(j)));
  box.appendChild(set);
}

function cronRow(j) {
  const last = j.runs[0];
  const r = mk('div', 'cronjob' + (j.on && last && !last.ok ? ' bad' : ''));
  r.style.cursor = 'pointer';
  r.onclick = (e) => { if (e.target.closest('button')) return; openCronDetail(j); };

  const nm = mk('div', 'nm');
  nm.appendChild(mk('span', 'dot' + (!j.on ? '' : last && !last.ok ? ' err' : ' run')));
  nm.appendChild(mk('span', null, j.name));
  nm.appendChild(mk('span', 'when', T(DELIVER[j.deliver] || 'gui.deliver.app')));
  r.appendChild(nm);

  const meta = mk('div', 'mo');
  meta.textContent = j.on ? T('gui.cron.next', { when: j.when, next: j.next })
    : T('gui.cron.paused_meta', { when: j.when });
  r.appendChild(meta);

  const foot = mk('div', 'foot');
  if (last) {
    const chip = mk('button', 'mini ghost');
    chip.textContent = T('gui.cron.last_run',
      { at: last.at, state: T(last.ok ? 'gui.cron.ok' : 'gui.cron.failed'), dur: dur(last.ms) });
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
      '-',
      { label: T('gui.cron.delete'), bad: true, fn: () => confirmAsk(T('gui.cron.delete_title'),
          T('gui.cron.delete_body', { name: j.name }), T('gui.cron.delete'), () => cronDelete(j)) }
    ]);
  };
  ctl.append(s, more);
  r.appendChild(ctl);
  return r;
}

/* A run is a session. Opening one leaves the schedule and lands in the
   transcript, which is the whole point of scheduling anything. */
function openRun(j, run) {
  closeCron();
  const s = { id: 'n' + Date.now(), title: j.name, last: run.note,
    when: run.at, run: run.ok ? 'gtm' : null, status: run.ok ? null : 'err',
    from: 'cron', job: j.id };
  SESS.unshift(s); cur = s.id; drawList(); openSession(s);
  if (!run.ok) {
    $('#stage').innerHTML = '';
    ask(j.what);
    noteRow(run.note, T('gui.cron.rerun_note'));
  }
  toast(`打开了 ${run.at} 那次运行`);
}

function runNow(j) {
  closeCron();
  const s = { id: 'n' + Date.now(), title: j.name, last: T('gui.cron.manual_run'), when: T('gui.sess.just_now'),
    run: null, from: 'cron', job: j.id };
  SESS.unshift(s); cur = s.id; drawList(); openSession(s);
  send(j.what);
  toast(T('gui.cron.running_x', { name: j.name }));
}

/* The job's own page: header actions, the editable schedule, and every run
   it has made -- each row opens the transcript that run wrote. */
function drawCronDetail(box, j) {
  /* Same return shape as every secondary page: the back button alone at the
     top-left, then the job's own title line with its actions, then a status
     line saying what the schedule means and when it fires next. */
  const hd = mk('div', 'pmback');
  const back = mk('button', 'mini ghost', '← ' + T('gui.cron.back'));
  back.onclick = () => { cronView = null; cronDraft = null; drawCron(); };
  hd.appendChild(back);
  const trow = mk('div', 'trow');
  trow.appendChild(mk('b', null, j.name));
  const acts = mk('div', 'cdacts');
  const run = mk('button', 'mini gold', T('gui.cron.run_now'));
  run.onclick = () => runNow(j);
  const del = mk('button', 'mini ghost danger', T('gui.cron.delete'));
  del.onclick = () => confirmAsk(T('gui.cron.delete_title'),
    T('gui.cron.delete_body', { name: j.name }), T('gui.cron.delete'), () => cronDelete(j));
  acts.append(run, del, swi(j.on, () => cronToggle(j), T('gui.caps.toggle_aria', { name: j.name })));
  trow.appendChild(acts);
  hd.appendChild(trow);
  const st = mk('div', 'cdstat');
  st.appendChild(mk('span', 'dot' + (j.on ? ' on' : '')));
  st.appendChild(mk('span', null, j.on
    ? T('gui.cron.next', { when: cronWhen(j), next: j.next })
    : `${cronWhen(j)} · ${T('gui.cron.paused')}`));
  hd.appendChild(st);
  box.appendChild(hd);

  const cfg = mk('div', 'cdcard');
  cfg.appendChild(mk('div', 't', T('gui.cron.cfg')));
  if (!cronDraft || cronDraft.id !== j.id) cronDraft = { ...j };
  const fb = mk('div');
  buildJobForm(fb, cronDraft, drawCron);
  cfg.appendChild(fb);
  const save = mk('div', 'cdfoot');
  const sv = mk('button', 'mini gold', T('gui.cron.save'));
  sv.onclick = () => {
    if (!cronDraft.name.trim() || !cronDraft.what.trim()) { cronDraft.blank = true; drawCron(); return; }
    cronPersist(cronDraft, (saved) => {
      cronView = saved ? saved.id : null; cronDraft = null;
      drawCron(); drawCronBdg();
      toast(T('gui.cron.saved'));
    });
  };
  save.appendChild(sv);
  cfg.appendChild(save);
  box.appendChild(cfg);

  const hs = mk('div', 'cdcard');
  const ht = mk('div', 't', T('gui.cron.hist_title2'));
  hs.appendChild(ht);
  const list = mk('div', 'cdruns');
  hs.appendChild(list);
  box.appendChild(hs);
  cronRunsLoad(j).then((rows) => {
    if (cronView !== j.id) return;
    list.innerHTML = '';
    if (!rows.length) { list.appendChild(mk('div', 'empty-note', T('gui.cron.hist_none'))); return; }
    ht.appendChild(mk('span', 'n', String(rows.length)));
    rows.forEach((run) => {
      const row = mk('button', 'cdrun');
      row.appendChild(mk('span', 'st' + (run.ok ? ' ok' : ' bad')));
      row.appendChild(mk('span', 'at', run.at));
      row.appendChild(mk('span', 'note', run.note || ''));
      row.appendChild(mk('span', 'chev', '›'));
      row.onclick = () => openRun(j, run);
      list.appendChild(row);
    });
  }).catch(() => { list.innerHTML = ''; list.appendChild(mk('div', 'empty-note', T('gui.cron.hist_none'))); });
}

function drawCronBdg() {}

/* create / edit -- one form, two hosts: the new-job sheet and the detail
   page's config card both build from the same draft. */
let jobDraft = null;
function buildJobForm(b, draft, redraw) {
  const field = (label, hint, node) => {
    const w = mk('div', 'ff');
    w.appendChild(mk('label', null, label));
    w.appendChild(node);
    if (hint) w.appendChild(mk('span', 'hint', hint));
    b.appendChild(w);
    return node;
  };

  const nameIn = mk('input'); nameIn.type = 'text'; nameIn.value = draft.name;
  nameIn.placeholder = T('gui.job.name_ph');
  nameIn.oninput = () => { draft.name = nameIn.value; draft.blank = null; };
  /* Same rule as the schedule row: a refusal is the one state change with
     nothing for the reader to look at, so it is marked where it happened. */
  /* Marked per field, so "both are required" does not appear under the one the
     reader already filled in. */
  const blankName = draft.blank && !draft.name.trim();
  const blankWhat = draft.blank && !draft.what.trim();
  if (blankName) nameIn.dataset.bad = 'true';
  field(T('gui.job.name'), blankName ? T('gui.job.need_name') : null, nameIn);

  const what = mk('textarea');
  what.rows = 3; what.value = draft.what;
  what.placeholder = T('gui.job.what_ph');
  what.style.cssText = 'background:var(--ink);border:1px solid var(--line);border-radius:7px;padding:9px 11px;outline:0;resize:vertical;font-size:13px;line-height:1.6';
  what.oninput = () => { draft.what = what.value; draft.blank = null; };
  /* Inline, because this field's border is inline: a stylesheet rule cannot
     reach past `style.cssText` above, so the mark has to be set where the
     border was. */
  if (blankWhat) { what.dataset.bad = 'true'; what.style.borderColor = 'var(--amber)'; }
  field(T('gui.job.what'), blankWhat ? T('gui.job.need_what') : null, what);

  const row = mk('div'); row.style.cssText = 'display:flex;gap:9px;align-items:center;flex-wrap:wrap';
  const fseg = seg(FREQ.map((f) => [f.id, T(f.label)]), draft.freq, (v) => {
    draft.freq = v; redraw();
  });
  row.appendChild(fseg);
  if (draft.freq === 'week') {
    /* A control, not free text. The weekday used to be read back out of the
       localized placeholder the field taught the reader to type, so an English
       "Fri 17:00" missed a Chinese-only pattern and fell through to Monday --
       with the hour still correct, so nothing looked wrong. */
    const wd = mk('select');
    for (let d = 0; d < 7; d++) {
      const o = mk('option', null, T('gui.cron.h.dow' + d));
      o.value = String(d);
      if (String(d) === String(draft.wd ?? 1)) o.selected = true;
      wd.appendChild(o);
    }
    wd.onchange = () => { draft.wd = Number(wd.value); draft.bad = null; };
    if (draft.wd == null) draft.wd = 1;
    row.appendChild(wd);
  }
  if (draft.freq === 'once') {
    /* The one-shot's own instant, round-tripped as an ISO string rather than
       re-derived from prose: `cron.save` takes `at_iso` and recomputes the
       delete-after-run flag from the kind, so the reminder stays a reminder. */
    const when = mk('input'); when.type = 'datetime-local'; when.style.width = '190px';
    when.value = draft.at_local || '';
    when.oninput = () => { draft.at_local = when.value; draft.bad = null; };
    row.appendChild(when);
  }
  if (draft.freq !== 'hour' && draft.freq !== 'once' && draft.freq !== 'week') {
    const at = mk('input'); at.type = 'text'; at.value = draft.at; at.style.width = '140px';
    at.placeholder = draft.freq === 'cron' ? '0 8 * * *' : '08:00';
    at.oninput = () => { draft.at = at.value; draft.bad = null; };
    row.appendChild(at);
    if (draft.freq === 'cron') {
      const hu = mk('span', 'hint');
      const refresh = () => { hu.textContent = cronExprHuman(at.value); };
      at.addEventListener('input', refresh); refresh();
      row.appendChild(hu);
    }
  }
  if (draft.freq === 'week') {
    const at = mk('input'); at.type = 'text'; at.value = draft.at; at.style.width = '90px';
    at.placeholder = '09:30';
    at.oninput = () => { draft.at = at.value; draft.bad = null; };
    row.appendChild(at);
  }
  /* A refused save is the one state change that leaves nothing to look at, so
     the reason goes on the control rather than into a toast -- which live mode
     redirects to the console, where the reader is not. Cleared by the redraw
     that any edit triggers. */
  field(T('gui.job.freq'), draft.bad ? T(draft.bad) : null, row);
  if (draft.bad) row.dataset.bad = 'true';

  const sel = mk('select');
  Object.entries(DELIVER).forEach(([k, v]) => {
    const o = mk('option', null, T(v)); o.value = k;
    if (k === draft.deliver) o.selected = true;
    sel.appendChild(o);
  });
  sel.onchange = () => { draft.deliver = sel.value; };
  field(T('gui.job.deliver'), null, sel);
  return nameIn;
}

function openJobSheet(j) {
  jobDraft = j || { id: 'j' + Date.now(), name: '', what: '', freq: 'day', at: '08:00',
    on: true, deliver: 'app', runs: [], fresh: true };
  $('#jobTitle').textContent = T(j ? 'gui.job.edit_title' : 'gui.cron_new');
  const b = $('#jobBody'); b.innerHTML = '';
  const nameIn = buildJobForm(b, jobDraft, () => openJobSheet(jobDraft));
  $('#jobVeil').dataset.open = 'true';
  nameIn.focus();
}

$('#jobNo').onclick = () => { $('#jobVeil').dataset.open = 'false'; jobDraft = null; };
$('#jobYes').onclick = () => {
  const j = jobDraft;
  if (!j.name.trim() || !j.what.trim()) { toast('名称和要做什么都得填'); return; }
  const label = FREQ.find((f) => f.id === j.freq).label;
  j.when = j.freq === 'hour' ? '每小时' : j.freq === 'cron' ? `cron ${j.at}` : `${label} ${j.at}`;
  j.next = j.on ? (j.freq === 'day' ? `明天 ${j.at}` : j.at) : '已暂停';
  if (j.fresh) { delete j.fresh; CRONS.push(j); }
  $('#jobVeil').dataset.open = 'false';
  drawCron(); drawCronBdg();
  toast(`已保存「${j.name}」`, { label: '立即跑一次', fn: () => runNow(j) });
  jobDraft = null;
};
$('#jobVeil').onclick = (e) => { if (e.target === $('#jobVeil')) $('#jobNo').click(); };

