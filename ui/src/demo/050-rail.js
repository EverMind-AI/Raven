/* ══ module 1a: session rail ══════════════════════════════════════ */
const listOpen = new Set();
/* Collapsed rail groups survive reloads; expanded is the default. */
const grpFold = new Set((() => {
  try { return JSON.parse(localStorage.getItem('raven.gui.grpfold') || '[]'); } catch { return []; }
})());
const saveGrpFold = () => {
  try { localStorage.setItem('raven.gui.grpfold', JSON.stringify([...grpFold])); } catch { /* private mode */ }
};

/* One writer for the whole rail, because the surfaces stack: the settings dialog
   layers over a module page, which layers over the chat, and every one of them
   has a row that would claim to be current on its own. Deciding it in one place
   from the topmost surface is what makes two selected rows impossible, rather
   than three call sites each remembering to clear the others.

   The new-task row stands for a draft -- a draft has no session id, so an empty
   `cur` is its state -- but only while nothing covers it. */
function markNewCurrent() {
  const app = document.querySelector('.app');
  const pageUp = app && app.dataset.page === 'on' ? Object.keys(NAV_OF).find(
    (p) => $('#' + p) && $('#' + p).dataset.open === 'true') : null;
  /* A NAV_OF entry may be a function: the capabilities section is one page
     shared by two rail modules, so which button it belongs to is a question
     about the page's current content. */
  const btnOf = (p) => (typeof NAV_OF[p] === 'function' ? NAV_OF[p]() : NAV_OF[p]);
  let top = pageUp ? btnOf(pageUp) : (!cur ? 'newBtn' : null);
  /* While the 更多 group stands open its rows are rail rows, and the current
     one wears the mark itself; the parent lights up only when the group is
     folded and has to stand in for whichever of its pages is open. */
  const moreOpen = $('#moreFly').dataset.open === 'true';
  if (moreOpen) {
    $('#moreFly').querySelectorAll('.mrow').forEach((b, i) => {
      const page = MORE_ROWS[i] && MORE_ROWS[i][0];
      b.setAttribute('aria-current', String(!!page && $('#' + page).dataset.open === 'true'));
    });
    if (top === 'moreBtn') top = null;
  }
  ['newBtn', 'skillBtn', 'plugBtn', 'memBtn', 'moreBtn'].forEach((id) => {
    const b = $('#' + id);
    if (b) b.setAttribute('aria-current', String(id === top));
  });
}

function drawList() {
  markNewCurrent();
  const box = $('#list'); box.innerHTML = '';
  const hit = (s) => !query
    || s.title.toLowerCase().includes(query) || (s.last || '').toLowerCase().includes(query);
  const rows = SESS.filter(hit);

  if (query && !rows.length) {
    box.appendChild(mk('div', 'empty-note', T('gui.rail.no_hits', { q: query })));
    return;
  }

  if (query) {
    box.appendChild(mk('span', 'lab', T('gui.rail.search_hits', { n: rows.length })));
    rows.forEach((s) => box.appendChild(sessRow(s)));
    return;
  }

  // Every group gets the same collapsible eyebrow: caret + label + count +
  // hairline. 定时任务 and 最近 are permanent fixtures of the rail (rendered
  // even when empty); 置顶 only exists while something is pinned.
  const put = (label, items, action, cap, key, always) => {
    if (!items.length && !always) return;
    const gid = key || label;
    const folded = grpFold.has(gid);
    const h = mk('div', 'grp');
    h.setAttribute('role', 'button');
    h.tabIndex = 0;
    h.setAttribute('aria-expanded', String(!folded));
    const car = mk('span', 'car');
    car.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M8.5 5.5 15 12l-6.5 6.5"/></svg>';
    h.append(car, mk('span', 'lab', label), mk('span', 'n', String(items.length)), mk('span', 'rule'));
    if (action) {
      const go = mk('button', 'grp-go', T('gui.rail.manage'));
      go.onclick = (e) => { e.stopPropagation(); action(e); };
      h.appendChild(go);
    }
    const flip = () => {
      if (folded) grpFold.delete(gid); else grpFold.add(gid);
      saveGrpFold(); drawList();
    };
    h.onclick = flip;
    h.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); flip(); } };
    box.appendChild(h);
    if (folded) return;
    if (!items.length) { box.appendChild(mk('div', 'grp-empty', T('gui.rail.none'))); return; }
    // A long tail of old sessions buries the rail's other groups, so a group
    // with a cap shows its head and folds the rest behind one row.
    const open = listOpen.has(key);
    const shown = cap && !open ? items.slice(0, cap) : items;
    shown.forEach((s) => box.appendChild(sessRow(s)));
    if (!cap || items.length <= cap) return;
    const more = mk('button', 'grp-more',
      open ? T('gui.rail.collapse') : T('gui.rail.expand_rest', { n: items.length - cap }));
    more.setAttribute('aria-expanded', String(open));
    more.onclick = () => { if (open) listOpen.delete(key); else listOpen.add(key); drawList(); };
    box.appendChild(more);
  };

  put(T('gui.rail.pinned'), rows.filter((s) => s.pin), null, null, 'pin');
  put(T('gui.rail.from_cron'), rows.filter((s) => !s.pin && s.from === 'cron'), openCron, 3, 'cron', true);
  const rest = rows.filter((s) => !s.pin && s.from !== 'cron');
  // Straight through, in the order SESS already holds: newest last activity
  // first, which is the same value each row's clock shows. This used to be
  // re-bucketed by a coarse day group, and since the day before yesterday
  // shared its bucket with everything older than a week, a session answered
  // two days ago sorted below one answered six days ago -- while its own
  // clock said otherwise.
  put(T('gui.rail.recent'), rest, null, 15, 'recent', true);
}

/* A title is whatever the prompt or the job name started with, and a leading
   emoji turns a dense column of rows into a column of stickers. Display only:
   the stored title keeps its icon, and a title that is nothing BUT an icon
   stays as it is rather than rendering an empty row. */
const LEAD_ICO = /^(?:[\s\u00A0]*[\p{Extended_Pictographic}\p{Emoji_Modifier}\p{Regional_Indicator}\u{1F3FB}-\u{1F3FF}\uFE0F\u200D]+)+[\s\u00A0]*/u;
function plainTitle(t) {
  const raw = t == null ? '' : String(t);
  return raw.replace(LEAD_ICO, '').trim() || raw;
}

function sessRow(s) {
  const row = mk('div', 'sess');
  row.setAttribute('role', 'button');
  row.tabIndex = 0;
  row.setAttribute('aria-current', String(s.id === cur));

  const t = mk('div', 't');
  const live = s.id === cur && busy ? 'run' : s.status;
  // run/done speak from the tail slot instead (see .sess .sig); err and que
  // stay a leading dot -- they are conditions of the session, not of a turn
  // the reader is waiting on.
  const tail = live === 'run' || live === 'done' ? live : null;
  if (live && !tail) t.appendChild(mk('span', 'dot ' + live));
  t.appendChild(mk('span', null, plainTitle(s.title)));
  // The stamp is always rendered -- it is what gives the tail its width. A
  // marker hides the text in place rather than replacing the element, so the
  // row does not resize when a turn starts or ends.
  const w = mk('span', 'w');
  w.appendChild(mk('span', 'wt', s.when));
  if (tail) {
    w.dataset.sig = tail;
    // The state is only colour and motion otherwise, and the stamp behind it
    // is visibility:hidden, so name it for a reader who gets the row as text.
    const label = T(tail === 'run' ? 'gui.sess.running' : 'gui.sess.finished');
    w.setAttribute('aria-label', label);
    w.title = label;
    w.appendChild(mk('i'));
  }
  row.append(t, w);

  const more = mk('button', 'more', '⋯');
  more.setAttribute('aria-label', T('gui.cron.menu_aria', { name: plainTitle(s.title) }));
  more.onclick = (e) => { e.stopPropagation(); const r = more.getBoundingClientRect(); openSessMenu(s, r.right, r.bottom + 4); };
  row.appendChild(more);

  const go = () => { showPage(null); if (s.id !== cur) { cur = s.id; drawList(); openSession(s); } };
  row.onclick = go;
  row.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } };
  ctxMenu(row, () => sessItems(s));
  return row;
}

function sessItems(s) {
  return [
    { label: T('gui.sess.rename'), fn: () => { if (s.id !== cur) { cur = s.id; drawList(); openSession(s); } renameTitle(); } },
    { label: T(s.pin ? 'gui.sess.unpin' : 'gui.sess.pin'),
      fn: () => {
        s.pin = !s.pin; drawList(); toast(T(s.pin ? 'gui.pinned_ok' : 'gui.unpinned_ok'));
        /* Optimistic: the row moved already; live mode persists the flag in
           session metadata so it survives a reload. The demo has no server,
           so the hook stays null there and the toast promised nothing more. */
        if (pinPersist) pinPersist(s.id, s.pin);
      } },
    '-',
    { label: T('gui.sess.delete'), bad: true, fn: () => removeSession(s) }
  ];
}
function openSessMenu(s, x, y) { menuAt(x, y, sessItems(s)); }

function removeSession(s) {
  const at = SESS.indexOf(s);
  dropDraft(s.id);
  SESS = SESS.filter((x) => x.id !== s.id);
  undoBin = { s, at };
  if (cur === s.id) { const nx = SESS[0]; if (nx) { cur = nx.id; openSession(nx); } }
  drawList();
  toast(T('gui.sess.deleted_x', { title: s.title }), { label: T('gui.undo'), fn: () => {
    SESS.splice(undoBin.at, 0, undoBin.s); undoBin = null; drawList();
  } });
}

/* inline rename in the top bar; the list follows */
function renameTitle() {
  const h = $('#title'); if (!h) return;
  const s = sess(cur); if (!s) return;
  const inp = mk('input', 'titin');
  inp.value = s.title;
  h.replaceWith(inp);
  $('#renameBtn').hidden = true;
  inp.focus(); inp.select();
  const finish = (commit) => {
    const v = inp.value.trim();
    const t = commit && v ? v : s.title;
    s.title = t;
    const nh = mk('h1', null, plainTitle(t)); nh.id = 'title';
    inp.replaceWith(nh);
    $('#renameBtn').hidden = false;
    drawList();
  };
  inp.onblur = () => finish(true);
  inp.onkeydown = (e) => {
    if (composing(e)) return;
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') { e.preventDefault(); inp.onblur = null; finish(false); }
  };
}

