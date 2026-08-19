/* -- plugin market ----------------------------------------------------
   The plugin tab is market-first: the page IS the catalog, and what you
   already have lives one level in (the 已安装 button top-right, back
   arrow to return). Both views share the hub grid so they read as the
   same place. Install is a disk transaction server-side; everything
   slower than ~8s (an OAuth browser round-trip) streams back over
   mcp.status / oauth.pending / oauth.done notifications.               */

let plugView = 'market';    // market | installed
let pmItems = [], pmCats = [], pmState = 'idle', pmErr = '';
let pmQuery = '', pmCat = '', pmBusy = null, pmDebounce = null;
let pmDrawer = null;        // { kind: 'market'|'inst', id } while the drawer shows a plugin
let pmForm = false;         // drawer: apikey form unfolded
let pmConfirm = false;      // drawer: stdio run-locally confirm unfolded
const pmAuthWait = Object.create(null);  // server -> auth url while the browser round-trip is pending
const pmAuthEnd = Object.create(null);   // server -> epoch ms the authorization window closes at
let pmAuthClock = null;

const pmAuthLeft = (id) => {
  const end = pmAuthEnd[id];
  if (!end) return '';
  const s = Math.max(0, Math.round((end - Date.now()) / 1000));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
};

/* Writes the remaining time into whatever is showing it, rather than redrawing
   the panel once a second: a countdown that rebuilds its own surroundings would
   drop the focus and the scroll of a reader who is mid-decision. */
function pmAuthTick(on) {
  if (on && !pmAuthClock) {
    pmAuthClock = setInterval(() => {
      const live = Object.keys(pmAuthEnd);
      if (!live.length) { pmAuthTick(false); return; }
      document.querySelectorAll('[data-authcd]').forEach((el) => {
        el.textContent = pmAuthLeft(el.dataset.authcd);
      });
    }, 1000);
  } else if (!on && pmAuthClock && !Object.keys(pmAuthEnd).length) {
    clearInterval(pmAuthClock);
    pmAuthClock = null;
  }
}
/* Installs whose authentication hasn't been proven yet. An auth plugin only
   counts as installed once its connection authenticates: while an id is in
   here the entry stays out of every "installed" surface, and the pending
   install resolves from events — connected -> installed, a settled auth
   failure -> the install is rolled back (plug.remove). */
const pmPending = new Set();
const pmMarketIds = new Set();

const pmRedraw = () => {
  if ($('#capsPage').dataset.open === 'true' && extTab === 'plugin') drawCaps();
  if (pmDrawer) pmOpenDetail(pmDrawer.kind, pmDrawer.id, true);
  drawCapsBadge();
};

async function pmSearch() {
  pmState = 'loading'; pmErr = '';
  drawCaps();
  try {
    const r = await rpc.call('plughub.search', { q: pmQuery, category: pmCat });
    pmItems = r.items || [];
    pmCats = r.categories || [];
    pmItems.forEach((it) => pmMarketIds.add(it.id));
    pmState = 'done';
  } catch (e) {
    pmItems = [];
    pmErr = (e.data && e.data.detail) || e.message || String(e);
    pmState = 'error';
  }
  drawCaps();
}

const pmSearchSoon = () => { clearTimeout(pmDebounce); pmDebounce = setTimeout(pmSearch, 320); };

const pmMcpRows = () => PLUGINS.filter((p) => p.m);
const pmPyRows = () => PLUGINS.filter((p) => !p.m);
const pmText = (v) => (v && typeof v === 'object' ? v[LANG] || v.en || '' : String(v || ''));
const pmHost = (url) => { try { return new URL(url).host; } catch { return url || ''; } };
const pmEntryMcp = (entry) => (entry.contributes || []).find((c) => c.kind === 'mcp') || null;

/* Status vocabulary: a dot plus a word, never color alone. */
const PM_ST = {
  off:           { k: 'gui.plug.st_off',  cls: 'off' },
  connecting:    { k: 'gui.plug.st_conn', cls: 'busy' },
  connected:     { k: 'gui.plug.st_on',   cls: 'ok' },
  auth_required: { k: 'gui.plug.st_auth', cls: 'bad' },
  error:         { k: 'gui.plug.st_err',  cls: 'bad' },
  disconnected:  { k: 'gui.plug.st_off',  cls: 'off' },
};
function pmStatus(m) {
  if (pmAuthWait[m.name]) return { k: 'gui.plug.st_wait', cls: 'busy' };
  if (!m.enabled) return PM_ST.off;
  return PM_ST[m.state] || PM_ST.off;
}

function pmTile(name) {
  // Stable per-name hue: same plugin, same colour, every render and page.
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  const t = mk('span', 'pmtile th' + (h % 8));
  t.textContent = (name[0] || '?').toUpperCase();
  return t;
}

const PM_VFD = '<svg class="pmvfd" viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
  + '<path d="M12 2l2.4 2.4 3.4-.5 1 3.3 3.2 1.4-1.3 3.4 1.3 3.4-3.2 1.4-1 3.3-3.4-.5L12 22l-2.4-2.4-3.4.5-1-3.3-3.2-1.4L3.3 12 2 8.6l3.2-1.4 1-3.3 3.4.5z"/>'
  + '<path d="M9.5 12.2l1.8 1.8 3.6-3.8" stroke="var(--ink)" stroke-width="2" fill="none"/></svg>';

/* ── actions ─────────────────────────────────────────────────────── */

function pmToggle(name, on) {
  const row = pmMcpRows().find((p) => p.m.name === name);
  if (row) row.m.enabled = on;
  pmRedraw();
  rpc.call('plug.toggle', { name, enabled: on })
    .then((r) => { if (r.mcp && row) Object.assign(row.m, r.mcp); pmRedraw(); })
    .catch((e) => { toast(T('gui.plug.op_failed', { err: e.message || e })); loadExt().then(pmRedraw).catch(() => {}); });
}

/* Install progress sheet: the install drives the detail modal through the
   whole transaction -- write config -> connect -> (browser authorization)
   -> done -- so the user sees where it stands and what to do next. State
   lives here; the drawer kind 'progress' renders it and events advance it.
   Closing the sheet (X) leaves the install running in the background, and
   pmProg survives with it: clicking the plugin while the install still runs
   reopens this sheet (pmOpenDetail redirects). The state is released when
   the story ends -- done/fail acknowledged or resolved off-screen, cancel,
   uninstall. */
let pmProg = null;  // { id, name, mode, host, step, state: run|done|fail, err, tools }

function pmProgOpen(entry) {
  const mcp = pmEntryMcp(entry);
  pmProg = {
    id: entry.id,
    name: pmText(entry.name),
    mode: mcp ? (mcp.auth || {}).mode || 'none' : 'none',
    host: mcp ? pmHost((mcp.connection || {}).url) : '',
    step: 0, state: 'run', err: '', tools: 0,
  };
  pmOpenDetail('progress', entry.id);
}

/* The sheet is authoritative while it shows this id: toasts for the same
   outcome would just repeat it. */
const pmProgShows = (id) => !!(pmProg && pmProg.id === id && pmDrawer && pmDrawer.kind === 'progress');

function pmProgStep(n) {
  if (pmProg && pmProg.state === 'run' && n > pmProg.step) { pmProg.step = n; pmRedraw(); }
}

/* tools === null means "installed, still connecting" (a no-auth entry whose
   connect outlived the RPC window): the sheet closes the story as installed
   and a later connected event fills the tool count in. */
function pmProgDone(tools) {
  if (!pmProg) return;
  if (pmProg.state !== 'run' && !(pmProg.state === 'done' && pmProg.tools == null)) return;
  pmProg.state = 'done';
  pmProg.tools = tools;
  // Resolved while the sheet is closed: the toast carries the news and the
  // plugin goes back to opening its normal detail.
  if (!pmProgShows(pmProg.id)) pmProg = null;
  pmRedraw();
}

function pmProgFail(err) {
  if (!pmProg || pmProg.state !== 'run') return;
  pmProg.state = 'fail';
  pmProg.err = err || '';
  if (!pmProgShows(pmProg.id)) pmProg = null;
  pmRedraw();
}

function pmProgRender(body) {
  const pg = pmProg;
  if (!pg) return;
  const head = mk('div', 'pmdhead');
  head.appendChild(pmTile(pg.name));
  const meta = mk('div', 'pmdmeta');
  const l1 = mk('div', 'l1');
  l1.appendChild(mk('b', null, pg.name));
  meta.appendChild(l1);
  meta.appendChild(mk('div', 'l2', T('gui.plug.prog_cap')));
  head.appendChild(meta);
  const hact = mk('div', 'dact');
  const authPhase = pg.state === 'run' && pg.mode === 'oauth' && pg.step >= 2;
  if (authPhase) {
    if (pmAuthWait[pg.id]) {
      const re = mk('button', 'mini', T('gui.plug.reopen'));
      re.onclick = () => window.open(pmAuthWait[pg.id], '_blank');
      hact.appendChild(re);
    }
    const c = mk('button', 'mini ghost', T('gui.plug.cancel'));
    c.onclick = () => { pmPending.delete(pg.id); pmRemove(pg.id, pg.name); };
    hact.appendChild(c);
  } else if (pg.state === 'run') {
    hact.appendChild(mk('span', 'pnote', T('gui.hub.working')));
  } else {
    if (pg.state === 'done') {
      const use = mk('button', 'mini gold', T('gui.hub.use'));
      use.onclick = () => useInTask('gui.plug.use_prompt', pg.name);
      hact.appendChild(use);
    }
    const x = mk('button', 'mini ghost', T('gui.close'));
    x.onclick = () => closeDetail();
    hact.appendChild(x);
  }
  head.appendChild(hact);
  body.appendChild(head);

  const keys = ['prog_write', 'prog_conn'].concat(pg.mode === 'oauth' ? ['prog_auth'] : [], ['prog_finish']);
  const list = mk('div', 'pmsteps');
  keys.forEach((k, i) => {
    const done = pg.state === 'done' || i < pg.step;
    const cur = pg.state !== 'done' && !done && i === Math.min(pg.step, keys.length - 1);
    const cls = done ? ' done' : cur ? (pg.state === 'fail' ? ' fail' : ' cur') : '';
    const row = mk('div', 'pstep' + cls);
    const ic = mk('i', 'ic');
    if (done) ic.textContent = '✓';
    else if (cur && pg.state === 'fail') ic.textContent = '✕';
    row.append(ic, mk('span', null, T('gui.plug.' + k)));
    list.appendChild(row);
  });
  body.appendChild(list);

  if (pg.state === 'done') {
    body.appendChild(mk('div', 'pnote', pg.tools == null
      ? T('gui.plug.installed_conn', { name: pg.name })
      : T('gui.plug.prog_ok', { n: pg.tools })));
  } else if (pg.state === 'fail') {
    body.appendChild(mk('div', 'pnote', T('gui.plug.auth_fail_rm', { name: pg.name })));
    if (pg.err) body.appendChild(mk('div', 'perr', pg.err));
  } else if (authPhase) {
    const note = mk('div', 'pnote',
      T(pmAuthWait[pg.id] ? 'gui.plug.prog_auth_hint' : 'gui.plug.prog_auth_soon', { host: pg.host }));
    if (pmAuthEnd[pg.id]) {
      note.append(' ', mk('span', 'pcd', T('gui.plug.auth_left') + ' '));
      const cd = mk('span', 'pcd b', pmAuthLeft(pg.id));
      cd.dataset.authcd = pg.id;
      note.lastChild.appendChild(cd);
    }
    body.appendChild(note);
  } else if (pg.mode === 'oauth') {
    body.appendChild(mk('div', 'pnote', T('gui.plug.prog_auth_soon', { host: pg.host })));
  }
}

function pmInstall(entry, form) {
  // Pending from the first moment: the ledger lands on disk mid-call, and the
  // entry must not read as installed anywhere before its auth is proven.
  pmBusy = entry.id; pmPending.add(entry.id);
  pmProgOpen(entry);
  pmRedraw();
  rpc.call('plug.install', { id: entry.id, form: form || {} })
    .then((r) => {
      const it = pmItems.find((x) => x.id === entry.id);
      if (r.pending) {
        // Guard on the pending mark: a cancel mid-call already rolled the
        // install back, and marking it installed here would resurrect it.
        if (pmPending.has(entry.id)) {
          if (it) it.installed = false;
          if (!pmAuthWait[entry.id] && !pmProgShows(entry.id)) {
            toast(T('gui.plug.wait_auth', { name: pmText(entry.name) }));
          }
        }
      } else {
        pmPending.delete(entry.id);
        if (it) it.installed = true;
        const st = r.mcp && r.mcp.state;
        if (st === 'connected') {
          pmProgDone(r.mcp.tool_count || 0);
          if (!pmProgShows(entry.id)) toast(T('gui.plug.installed_ok', { name: pmText(entry.name), n: r.mcp.tool_count }));
        } else {
          if (pmProg && pmProg.id === entry.id) pmProgDone(null);
          if (!pmProgShows(entry.id)) toast(T('gui.plug.installed_conn', { name: pmText(entry.name) }));
        }
      }
      pmForm = false; pmConfirm = false;
      return loadExt();
    })
    .catch((e) => {
      // Includes the backend's own rollback (auth settled as failed inside
      // the connect window): nothing is installed, clear the pending mark.
      pmPending.delete(entry.id);
      const err = (e.data && e.data.detail) || e.message || e;
      if (pmProg && pmProg.id === entry.id) pmProgFail(String(err));
      if (!pmProgShows(entry.id)) toast(T('gui.plug.op_failed', { err }));
      return loadExt().catch(() => {});
    })
    .finally(() => { pmBusy = null; pmRedraw(); });
}

/* Card-level install: fetch the manifest, install straight away when nothing
   needs input; anything with a key form or a run-locally confirm opens the
   sheet already unfolded at that step. */
function pmQuickInstall(it) {
  pmBusy = it.id; pmRedraw();
  rpc.call('plughub.detail', { id: it.id })
    .then((r) => {
      const entry = r.item;
      const mcp = pmEntryMcp(entry);
      const mode = mcp ? (mcp.auth || {}).mode || 'none' : 'none';
      const stdio = !!(mcp && (mcp.connection || {}).command);
      if (mode === 'apikey' || stdio) {
        pmBusy = null;
        if (mode === 'apikey') pmForm = true; else pmConfirm = true;
        pmOpenDetail('market', entry.id, true);
        return;
      }
      pmInstall(entry, {});
    })
    .catch((e) => {
      pmBusy = null;
      toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e }));
      pmRedraw();
    });
}

/* No window.confirm (the WKWebView shell has no JS-panel delegate): a
   destructive button arms on first click and fires on the second. */
function pmArm(btn, fn) {
  let armed = false;
  const label = btn.textContent;
  btn.onclick = (e) => {
    e.stopPropagation();
    if (!armed) {
      armed = true;
      btn.textContent = T('gui.plug.confirm_remove');
      btn.classList.add('bad');
      setTimeout(() => { armed = false; btn.textContent = label; btn.classList.remove('bad'); }, 4000);
      return;
    }
    fn();
  };
}

/* A pending install failed authentication: the plugin never counted as
   installed, so undo the disk transaction and put the market card back. */
function pmPendingFail(name, why) {
  if (!pmPending.has(name)) return;
  pmPending.delete(name);
  delete pmAuthWait[name];
  const it = pmItems.find((x) => x.id === name);
  if (it) it.installed = false;
  /* The cause travels with the failure. A rollback that only says "removed"
     reads as raven losing the plugin; "the authorization window closed" tells
     the reader what to do differently on the retry. */
  if (pmProg && pmProg.id === name) pmProgFail(why || '');
  if (!pmProgShows(name)) toast(T('gui.plug.auth_fail_rm', { name: it ? it.name : name }));
  rpc.call('plug.remove', { name }).then(loadExt).catch(() => {}).then(pmRedraw);
}

function pmRemove(name, label) {
  pmBusy = name; pmRedraw();
  rpc.call('plug.remove', { name })
    .then(() => {
      const it = pmItems.find((x) => x.id === name);
      if (it) it.installed = false;
      pmPending.delete(name);
      delete pmAuthWait[name];
      // Cancel/uninstall ends the install story outright.
      if (pmProg && pmProg.id === name) pmProg = null;
      toast(T('gui.caps.removed_x', { name: label || name }));
      pmCloseDetail();
      return loadExt();
    })
    .catch((e) => toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e })))
    .finally(() => { pmBusy = null; pmRedraw(); });
}

function pmAuth(name) {
  pmBusy = name; pmRedraw();
  rpc.call('plug.auth', { name })
    .then((r) => {
      const row = pmMcpRows().find((p) => p.m.name === name);
      if (r.mcp && row) Object.assign(row.m, r.mcp);
    })
    .catch((e) => toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e })))
    .finally(() => { pmBusy = null; pmRedraw(); });
}

/* ── events from the gateway ─────────────────────────────────────── */

/* The gateway announces a newer build the moment its periodic check finds one,
   so a tab that has been open for days hears about it without a reload. Same
   banner as the boot-time system.version path. */
rpc.notify['system.update_available'] = (p) => {
  if (p && p.latest_version) showUpNote('ver', p.latest_version);
};

let pmExtSoon = null;
rpc.notify['mcp.status'] = (p) => {
  // A settled status for the busy server means nothing is processing any
  // more — belt-and-braces against a busy flag that outlives its install.
  const inFlight = pmBusy === p.name;
  if (inFlight && p.state !== 'connecting') pmBusy = null;
  if (pmProg && pmProg.id === p.name) {
    if (p.state === 'connecting') pmProgStep(1);
    else if (p.state === 'connected') pmProgDone(p.tool_count || 0);
  }
  if (pmPending.has(p.name)) {
    if (p.state === 'connected') {
      pmPending.delete(p.name);
      const it = pmItems.find((x) => x.id === p.name);
      if (it) it.installed = true;
      // The install RPC reports its own outcome; only a later async
      // connect (the OAuth round-trip) announces from here.
      if (!inFlight && !pmProgShows(p.name)) {
        toast(T('gui.plug.installed_ok', { name: it ? it.name : p.name, n: p.tool_count || 0 }));
      }
    } else if ((p.state === 'auth_required' || p.state === 'error') && !inFlight) {
      // While the install RPC is in flight the backend rolls back itself
      // and the call rejects; acting here too would remove twice.
      pmPendingFail(p.name, p.error || '');
      return;
    }
  }
  const row = pmMcpRows().find((x) => x.m.name === p.name);
  if (row) Object.assign(row.m, p);
  else {
    // Unknown server (fresh install, or events arriving before the first
    // ext.list) — coalesce the reload; startup syncs fire one event per server.
    clearTimeout(pmExtSoon);
    pmExtSoon = setTimeout(() => loadExt().then(pmRedraw).catch(() => {}), 250);
  }
  if (p.state !== 'connecting') delete pmAuthWait[p.name];
  pmRedraw();
};

/* Long-term memory stopped writing, or started again. Broadcast like the other
   per-server events, because a backend that cannot store is not part of any one
   conversation's turn. */
rpc.notify['memory.health'] = (p) => {
  memFault = p && p.ok === false ? (p.error || T('gui.mem.down')) : null;
  drawBanner();
};

rpc.notify['oauth.pending'] = (p) => {
  pmAuthWait[p.server] = p.url;
  /* The window has an end, so the page shows one. Without it the sheet sits on
     "waiting for authorization" with nothing to distinguish a flow still worth
     finishing from one that expired minutes ago -- and the reader only learns
     which it was when the install disappears. */
  if (p.expires_in) pmAuthEnd[p.server] = Date.now() + Number(p.expires_in) * 1000;
  pmAuthTick(true);
  if (pmProg && pmProg.id === p.server) pmProgStep(2);
  /* A background connect found this server unauthorized; nobody asked for it,
     and the host deliberately did not open a browser. Say so where the reader
     can act on it, rather than reporting a page that never opened. The rows
     already grow a "reopen" button off pmAuthWait, which is now the way in. */
  if (!pmProgShows(p.server)) {
    toast(T(p.interactive === false ? 'gui.plug.auth_needed' : 'gui.plug.auth_opened',
      { host: pmHost(p.url), name: p.server }));
  }
  pmRedraw();
};

rpc.notify['oauth.done'] = (p) => {
  delete pmAuthWait[p.server];
  delete pmAuthEnd[p.server];
  pmAuthTick(false);
  if (p.ok && pmProg && pmProg.id === p.server) pmProgStep(3);
  if (!p.ok) {
    const why = p.error === 'timeout' ? T('gui.plug.auth_expired') : (p.error || '');
    // A failed authorization on a pending install rolls the install back;
    // on an already-installed server (re-auth) it just reports. While the
    // install RPC is in flight the backend rolls back itself.
    if (pmPending.has(p.server)) {
      if (pmBusy !== p.server) pmPendingFail(p.server, why);
      return;
    }
    toast(why || T('gui.plug.auth_fail', { name: p.server }));
  }
  pmRedraw();
};

/* ── market view ─────────────────────────────────────────────────── */

function pmMarketCard(it) {
  const c = mk('div', 'hubcard pmcard' + (it.installed ? ' dim' : ''));
  const top = mk('div', 'top');
  const nmwrap = mk('div', 'pmhead');
  nmwrap.appendChild(pmTile(it.name));
  const id = mk('div', 'pmid');
  const nm = mk('div', 'pmnm');
  nm.appendChild(mk('span', null, it.name));
  if (it.verified) { const v = mk('span'); v.innerHTML = PM_VFD; v.title = T('gui.plug.verified'); nm.appendChild(v); }
  id.appendChild(nm);
  if (it.publisher) id.appendChild(mk('div', 'pmpub', it.publisher));
  nmwrap.appendChild(id);
  top.appendChild(nmwrap);
  c.appendChild(top);
  c.appendChild(mk('p', 'one', it.summary || ''));

  const foot = mk('div', 'foot');
  const cnt = mk('span', 'pmcnt');
  const bits = [];
  if (it.tool_preview_count) bits.push(T('gui.plug.tools_n', { n: it.tool_preview_count }));
  if (it.skill_count) bits.push(T('gui.plug.skills_n', { n: it.skill_count }));
  cnt.textContent = bits.join(' · ');
  if (it.risk_tier === 2) cnt.appendChild(mk('span', 'pmsign gold', T('gui.plug.local_run')));
  if (it.risk_tier >= 3) cnt.appendChild(mk('span', 'pmsign faint', T('gui.plug.needs_restart')));
  foot.appendChild(cnt);

  const act = mk('div', 'act');
  if (pmBusy === it.id) act.appendChild(mk('span', 'pnote', T('gui.hub.working')));
  else if (pmPending.has(it.id)) act.appendChild(mk('span', 'pnote', T('gui.plug.st_wait')));
  else if (it.installed) act.appendChild(mk('span', 'okpill', T('gui.hub.installed')));
  else {
    // Two hit zones: the button installs right away (or lands on the form
    // when one is needed), the card opens the sheet.
    const b = mk('button', 'mini gold', T('gui.plug.install'));
    b.onclick = (e) => { e.stopPropagation(); pmQuickInstall(it); };
    act.appendChild(b);
  }
  foot.appendChild(act);
  c.appendChild(foot);
  c.tabIndex = 0;
  c.setAttribute('role', 'button');
  c.onclick = () => pmOpenDetail('market', it.id);
  c.onkeydown = (e) => { if (e.key === 'Enter') pmOpenDetail('market', it.id); };
  return c;
}

function drawPlugMarket(box) {
  const chips = mk('div', 'pmchips');
  const chip = (label, val) => {
    const b = mk('button', 'pill', label);
    b.setAttribute('aria-pressed', String(pmCat === val));
    b.onclick = () => { pmCat = val; pmSearch(); };
    chips.appendChild(b);
  };
  chip(T('gui.filter.all'), '');
  pmCats.forEach((cat) => chip(T('gui.plug.cat_' + cat, null, cat), cat));
  box.appendChild(chips);

  if (pmErr) {
    const wrap = mk('div', 'empty-note');
    wrap.append(mk('div', null, T('gui.plug.market_down')), document.createElement('br'));
    const retry = mk('button', 'mini ghost', T('gui.plug.retry'));
    retry.onclick = pmSearch;
    wrap.appendChild(retry);
    box.appendChild(wrap);
    return;
  }
  if (pmState === 'loading' && !pmItems.length) { box.appendChild(mk('div', 'empty-note', T('gui.hub.reading'))); return; }
  if (!pmItems.length) { box.appendChild(mk('div', 'empty-note', T('gui.plug.none_found', { q: pmQuery }))); return; }

  const g = mk('div', 'hubgrid');
  pmItems.forEach((it) => g.appendChild(pmMarketCard(it)));
  box.appendChild(g);
}

/* ── installed view ──────────────────────────────────────────────── */

function pmInstMcpCard(row) {
  const m = row.m;
  const st = pmStatus(m);
  const c = mk('div', 'hubcard pmcard' + (st.cls === 'bad' ? ' pmbad' : ''));
  const top = mk('div', 'top');
  const nmwrap = mk('div', 'pmhead');
  nmwrap.appendChild(pmTile(m.name));
  const nm = mk('div', 'pmnm');
  nm.appendChild(mk('span', null, m.name));
  nmwrap.appendChild(nm);
  top.appendChild(nmwrap);
  const sw = mk('button', 'swi');
  sw.setAttribute('role', 'switch');
  sw.setAttribute('aria-checked', String(m.enabled));
  sw.setAttribute('aria-label', T('gui.caps.toggle_aria', { name: m.name }));
  sw.onclick = (e) => { e.stopPropagation(); pmToggle(m.name, !m.enabled); };
  top.appendChild(sw);
  c.appendChild(top);

  const sub = mk('p', 'one');
  const bits = [pmMarketIds.has(m.name) ? T('gui.plug.from_market') : T('gui.plug.manual'), m.transport];
  if (m.tool_count) bits.push(T('gui.plug.tools_n', { n: m.tool_count }));
  sub.textContent = bits.join(' · ');
  c.appendChild(sub);

  const foot = mk('div', 'foot');
  const stEl = mk('span', 'pmst ' + st.cls);
  stEl.append(mk('i', 'pmdot'), mk('span', null, T(st.k)));
  if (st.cls === 'bad' && m.error) stEl.title = m.error;
  foot.appendChild(stEl);
  const act = mk('div', 'act');
  // The card keeps only the high-frequency verbs (the enable switch and one
  // context action); 卸载 lives in the detail's manage section.
  if (pmAuthWait[m.name]) {
    const re = mk('button', 'mini', T('gui.plug.reopen'));
    re.onclick = (e) => { e.stopPropagation(); window.open(pmAuthWait[m.name], '_blank'); };
    act.appendChild(re);
  } else if (m.state === 'auth_required' || m.state === 'error') {
    const fix = mk('button', 'mini', T(m.state === 'auth_required' ? 'gui.plug.reauth' : 'gui.caps.repair'));
    fix.onclick = (e) => { e.stopPropagation(); pmAuth(m.name); };
    act.appendChild(fix);
  } else if (m.enabled) {
    const use = mk('button', 'mini gold', T('gui.hub.use'));
    use.onclick = (e) => { e.stopPropagation(); useInTask('gui.plug.use_prompt', m.name); };
    act.appendChild(use);
  }
  foot.appendChild(act);
  c.appendChild(foot);
  c.tabIndex = 0;
  c.setAttribute('role', 'button');
  c.onclick = () => pmOpenDetail('inst', m.name);
  c.onkeydown = (e) => { if (e.key === 'Enter') pmOpenDetail('inst', m.name); };
  return c;
}

function pmInstPyCard(row) {
  const c = mk('div', 'hubcard pmcard');
  const top = mk('div', 'top');
  const nmwrap = mk('div', 'pmhead');
  nmwrap.appendChild(pmTile(row.name));
  const nm = mk('div', 'pmnm');
  nm.appendChild(mk('span', null, row.name));
  nmwrap.appendChild(nm);
  top.appendChild(nmwrap);
  const sw = mk('button', 'swi');
  sw.setAttribute('role', 'switch');
  sw.setAttribute('aria-checked', String(row.state === 'on'));
  sw.setAttribute('aria-label', T('gui.caps.toggle_aria', { name: row.name }));
  sw.onclick = (e) => { e.stopPropagation(); row.state = row.state === 'on' ? 'off' : 'on'; pmRedraw(); };
  top.appendChild(sw);
  c.appendChild(top);
  c.appendChild(mk('p', 'one', [row.src, row.ver !== '—' ? 'v' + row.ver : ''].filter(Boolean).join(' · ')));
  const foot = mk('div', 'foot');
  const st = row.state === 'on' ? PM_ST.connected : PM_ST.off;
  const stEl = mk('span', 'pmst ' + st.cls);
  stEl.append(mk('i', 'pmdot'), mk('span', null, T(st.k)));
  foot.appendChild(stEl);
  const cnt = mk('span', 'pmcnt');
  cnt.appendChild(mk('span', 'pmsign faint', T('gui.plug.needs_restart')));
  foot.appendChild(cnt);
  c.appendChild(foot);
  return c;
}

function drawPlugInstalled(box) {
  const back = mk('div', 'pmback');
  const b = mk('button', 'mini ghost');
  b.textContent = '← ' + T('gui.plug.back');
  b.onclick = () => { plugView = 'market'; pmCloseDetail(); drawCaps(); };
  back.append(b, mk('b', null, T('gui.plug.installed_title')));
  box.appendChild(back);

  const section = (label, cards) => {
    if (!cards.length) return;
    const s = mk('div', 'csec');
    const hd = mk('div', 'hd');
    hd.append(mk('b', null, label), mk('span', 'n', String(cards.length)));
    s.appendChild(hd);
    const g = mk('div', 'hubgrid');
    cards.forEach((el) => g.appendChild(el));
    s.appendChild(g);
    box.appendChild(s);
  };

  // A pending-auth install is not installed yet: it stays a market-side
  // waiting card until its authentication settles.
  const py = pmPyRows();
  const mcps = pmMcpRows().filter((p) => !pmPending.has(p.m.name));
  section(T('gui.plug.grp_builtin'), py.map(pmInstPyCard));
  section(T('gui.plug.grp_market'), mcps.map(pmInstMcpCard));

  if (!py.length && !mcps.length) box.appendChild(mk('div', 'empty-note', T('gui.plug.empty_installed')));
}

/* ── detail drawer (market entries + installed servers) ──────────── */

/* A running install keeps its pmProg across a close so the sheet can be
   reentered from the card; a settled one is acknowledged by closing. */
function pmCloseDetail() {
  pmDrawer = null;
  if (pmProg && pmProg.state !== 'run') pmProg = null;
  pmForm = false; pmConfirm = false;
  closeDetail();
}

function pmDrawerPerms(entry) {
  const mcp = pmEntryMcp(entry);
  const box = mk('div', 'pmperm');
  const li = (txt) => { const r = mk('div', 'row'); r.append(mk('i', 'pmdot'), mk('span', null, txt)); box.appendChild(r); };
  if (mcp) {
    const mode = (mcp.auth || {}).mode || 'none';
    const local = !!(mcp.connection || {}).command;
    if (mode === 'oauth') li(T('gui.plug.perm_oauth', { host: pmHost(mcp.connection.url) }));
    if (mode === 'apikey') li(T('gui.plug.perm_key'));
    if (local) li(T('gui.plug.perm_local'));
    // Where the data goes is already stated by the oauth line for oauth servers.
    else if (mode !== 'oauth') li(T('gui.plug.perm_net', { host: pmHost((mcp.connection || {}).url) }));
  }
  const skills = (entry.contributes || []).filter((c) => c.kind === 'skill').length;
  if (skills) li(T('gui.plug.perm_skill', { n: skills }));
  return box;
}

function pmInstallControls(entry, host) {
  const mcp = pmEntryMcp(entry);
  const mode = mcp ? (mcp.auth || {}).mode || 'none' : 'none';
  const stdio = !!(mcp && (mcp.connection || {}).command);
  const box = mk('div');

  const start = () => {
    if (mode === 'apikey' && !pmForm) { pmForm = true; pmOpenDetail('market', entry.id, true); return; }
    if (stdio && !pmConfirm && mode !== 'apikey') { pmConfirm = true; pmOpenDetail('market', entry.id, true); return; }
    pmInstall(entry, {});
  };

  if (pmForm && mode === 'apikey') {
    const fields = (mcp.auth || {}).fields || [];
    const inputs = new Map();
    fields.forEach((f) => {
      const lab = mk('label', 'pmlab');
      lab.append(mk('span', null, pmText(f.label) || f.key));
      if (f.help_url) {
        const a = mk('a', 'pmhelp', T('gui.plug.how_get'));
        a.href = f.help_url; a.target = '_blank'; a.rel = 'noreferrer';
        lab.appendChild(a);
      }
      box.appendChild(lab);
      const inp = document.createElement('input');
      inp.type = f.secret ? 'password' : 'text';
      inp.className = 'pminp';
      inp.autocomplete = 'off';
      inputs.set(f.key, inp);
      box.appendChild(inp);
    });
    if (stdio) {
      // The key form and the run-locally warning show together: one scroll,
      // one decision — the warning block owns the single action row.
      box.appendChild(pmStdioWarn(entry, inputs));
      return box;
    }
    const row = mk('div', 'pmrow');
    const cancel = mk('button', 'mini ghost', T('gui.plug.cancel'));
    cancel.onclick = () => { pmForm = false; pmOpenDetail('market', entry.id, true); };
    const go = mk('button', 'mini gold', T('gui.plug.connect'));
    go.onclick = () => {
      const form = {};
      let missing = false;
      inputs.forEach((inp, k) => { form[k] = inp.value.trim(); if (!form[k]) missing = true; });
      if (missing) { toast(T('gui.plug.need_key')); return; }
      pmInstall(entry, form);
    };
    row.append(cancel, go);
    box.appendChild(row);
    return box;
  }

  if (pmConfirm && stdio) {
    box.appendChild(pmStdioWarn(entry, null));
    return box;
  }

  const b = mk('button', 'mini gold', pmBusy === entry.id ? T('gui.hub.working') : T('gui.plug.install'));
  b.disabled = pmBusy === entry.id;
  b.onclick = start;
  box.appendChild(b);
  if (host) box.appendChild(mk('div', 'pnote', T('gui.plug.oauth_note', { host })));
  return box;
}

function pmStdioWarn(entry, inputs) {
  const mcp = pmEntryMcp(entry);
  const cmd = [mcp.connection.command, ...(mcp.connection.args || [])].join(' ');
  const w = mk('div', 'pmwarn');
  w.appendChild(mk('div', 'wt', T('gui.plug.stdio_warn')));
  w.appendChild(mk('pre', 'pmcmd', cmd));
  w.appendChild(mk('div', 'pnote', T('gui.plug.stdio_trust')));
  const row = mk('div', 'pmrow');
  const cancel = mk('button', 'mini ghost', T('gui.plug.cancel'));
  cancel.onclick = () => { pmConfirm = false; pmForm = false; pmOpenDetail('market', entry.id, true); };
  const go = mk('button', 'mini', T('gui.plug.still_install'));
  go.onclick = () => {
    const form = {};
    if (inputs) {
      let missing = false;
      inputs.forEach((inp, k) => { form[k] = inp.value.trim(); if (!form[k]) missing = true; });
      if (missing) { toast(T('gui.plug.need_key')); return; }
    }
    pmInstall(entry, form);
  };
  row.append(cancel, go);
  w.appendChild(row);
  return w;
}

function pmOpenDetail(kind, id, keep) {
  // An installed market plugin opens the same detail the market shows —
  // full package content — with connection state and manage actions layered
  // in. Only manually-configured servers (no catalog entry) fall through to
  // the slim config drawer below.
  // While this plugin's install is still running, every entry point lands
  // on the progress sheet -- closing it must never strand the install.
  if (kind !== 'progress' && pmProg && pmProg.id === id && pmProg.state === 'run') kind = 'progress';
  if (kind === 'inst' && pmMarketIds.has(id)) kind = 'market';
  pmDrawer = { kind, id };
  if (!keep) { pmForm = false; pmConfirm = false; }
  if ($('#capsPage').dataset.open !== 'true') openCaps('plugin');
  const body = $('#dBody'); body.innerHTML = '';

  if (kind === 'progress') {
    $('#dTitle').textContent = '';
    pmProgRender(body);
    $('#detail').dataset.open = 'true';
    return;
  }

  if (kind === 'market') {
    const it = pmItems.find((x) => x.id === id);
    rpc.call('plughub.detail', { id }).then((r) => {
      if (!pmDrawer || pmDrawer.id !== id) return;
      const entry = r.item;
      $('#dTitle').textContent = '';
      body.innerHTML = '';

      const mcp = pmEntryMcp(entry);
      const mode = mcp ? (mcp.auth || {}).mode || 'none' : 'none';
      const stdio = !!(mcp && (mcp.connection || {}).command);
      // A pending-auth install never reads as installed: the ledger entry
      // exists on disk, but the install completes (or rolls back) with auth.
      const pending = pmPending.has(entry.id);
      const installed = !pending && (r.installed || (it && it.installed));
      const lrow = (installed || pending) ? pmMcpRows().find((p) => p.m.name === entry.id) : null;
      const lm = lrow && lrow.m;

      const head = mk('div', 'pmdhead');
      head.appendChild(pmTile(pmText(entry.name)));
      const meta = mk('div', 'pmdmeta');
      const l1 = mk('div', 'l1');
      l1.appendChild(mk('b', null, pmText(entry.name)));
      if ((entry.publisher || {}).verified) { const v = mk('span'); v.innerHTML = PM_VFD; l1.appendChild(v); }
      meta.appendChild(l1);
      const bits = [(entry.publisher || {}).name, 'v' + entry.version].filter(Boolean);
      const l2 = mk('div', 'l2', bits.join(' · ') + (entry.homepage ? ' · ' : ''));
      if (entry.homepage) {
        const a = mk('a', null, T('gui.plug.homepage') + ' ↗');
        a.href = entry.homepage; a.target = '_blank'; a.rel = 'noreferrer';
        l2.appendChild(a);
      }
      meta.appendChild(l2);
      head.appendChild(meta);
      // The decisive control rides the identity row; forms unfold just below.
      const hact = mk('div', 'dact');
      if (pmBusy === entry.id) hact.appendChild(mk('span', 'pnote', T('gui.hub.working')));
      else if (pending) {
        if (pmAuthWait[entry.id]) {
          const re = mk('button', 'mini', T('gui.plug.reopen'));
          re.onclick = () => window.open(pmAuthWait[entry.id], '_blank');
          hact.appendChild(re);
        }
        const c = mk('button', 'mini ghost', T('gui.plug.cancel'));
        c.onclick = () => { pmPending.delete(entry.id); pmRemove(entry.id, pmText(entry.name)); };
        hact.appendChild(c);
      }
      else if (installed) {
        if (pmAuthWait[entry.id]) {
          const re = mk('button', 'mini', T('gui.plug.reopen'));
          re.onclick = () => window.open(pmAuthWait[entry.id], '_blank');
          hact.appendChild(re);
        } else if (lm && (lm.state === 'auth_required' || lm.state === 'error')) {
          const fix = mk('button', 'mini', T(lm.state === 'auth_required' ? 'gui.plug.reauth' : 'gui.caps.repair'));
          fix.onclick = () => pmAuth(entry.id);
          hact.appendChild(fix);
        } else if (!lm || lm.enabled) {
          const use = mk('button', 'mini gold', T('gui.hub.use'));
          use.onclick = () => useInTask('gui.plug.use_prompt', pmText(entry.name));
          hact.appendChild(use);
        } else hact.appendChild(mk('span', 'okpill', T('gui.hub.installed')));
      } else if (!pmForm && !pmConfirm) {
        const b = mk('button', 'mini gold', T('gui.plug.install'));
        b.onclick = () => {
          if (mode === 'apikey') { pmForm = true; pmOpenDetail('market', entry.id, true); return; }
          if (stdio) { pmConfirm = true; pmOpenDetail('market', entry.id, true); return; }
          pmInstall(entry, {});
        };
        hact.appendChild(b);
      }
      head.appendChild(hact);
      body.appendChild(head);

      const sec = (label, el) => {
        const s = mk('div', 'pmsec');
        s.appendChild(mk('div', 'cap', label));
        s.appendChild(el);
        body.appendChild(s);
      };
      if (pending) {
        body.appendChild(mk('div', 'pnote', T('gui.plug.wait_auth', { name: pmText(entry.name) })));
      } else if (!installed && (pmForm || pmConfirm)) {
        sec(T(pmForm ? 'gui.plug.sec_cred' : 'gui.plug.sec_confirm'),
          pmInstallControls(entry, mode === 'oauth' ? pmHost(mcp.connection.url) : ''));
      } else if (!installed && mode === 'oauth') {
        body.appendChild(mk('div', 'pnote', T('gui.plug.oauth_note', { host: pmHost(mcp.connection.url) })));
      }
      if (lm) {
        const st = pmStatus(lm);
        const conn = mk('div');
        const stEl = mk('span', 'pmst ' + st.cls);
        stEl.append(mk('i', 'pmdot'), mk('span', null, T(st.k)));
        conn.appendChild(stEl);
        if (lm.error && st.cls === 'bad') conn.appendChild(mk('div', 'perr', lm.error));
        if (lm.tool_count) conn.appendChild(mk('div', 'pnote', T('gui.plug.tools_n', { n: lm.tool_count })));
        sec(T('gui.plug.sec_conn'), conn);
      }
      sec(T('gui.plug.sec_perm'), pmDrawerPerms(entry));
      const desc = mk('div', 'pmdesc', pmText(entry.description) || pmText(entry.summary));
      sec(T('gui.plug.sec_about'), desc);
      // A plugin is a package: one section lists everything installing it
      // adds, each piece with its own detail nested under it (an MCP server
      // owns its tool list; a skill shows its hub id). No sibling sections.
      // A plugin is a package of contributions (today: MCP servers and
      // skills; the catalog is currently MCP-only). Each piece renders as
      // name line -> connection meta -> what it brings (an MCP server
      // contributes its tools; that is the surface raven consumes).
      const cts = entry.contributes || [];
      if (cts.length) {
        const box = mk('div');
        cts.forEach((co, i) => {
          const piece = mk('div', 'pmperm');
          if (i) piece.style.marginTop = '14px';
          const line = mk('div', 'row');
          line.appendChild(mk('span', 'kd', co.kind === 'skill' ? T('gui.plug.ct_skill') : 'MCP'));
          line.appendChild(mk('b', null,
            co.kind === 'mcp' ? entry.id : (co.name || co.skillhub_id || entry.id)));
          piece.appendChild(line);
          if (co.kind === 'mcp') {
            const conn = co.connection || {};
            const meta = mk('div', 'pnote',
              (conn.command ? ['stdio', [conn.command].concat(conn.args || []).join(' ').slice(0, 60)]
                : [conn.type || 'http', pmHost(conn.url)]).filter(Boolean).join(' · '));
            meta.style.margin = '4px 0 0 2px';
            piece.appendChild(meta);
            const tools = co.tools_preview || [];
            if (tools.length) {
              const cap = mk('div', 'cap', T('gui.plug.tools_n', { n: tools.length }));
              cap.style.margin = '8px 0 4px 2px';
              piece.appendChild(cap);
              const ul = mk('div');
              ul.style.cssText = 'margin: 0 0 0 2px';
              tools.forEach((t) => ul.appendChild(mk('div', 'pmtool', t)));
              piece.appendChild(ul);
            }
          }
          box.appendChild(piece);
        });
        sec(T('gui.plug.sec_contents'), box);
      }
      if (installed && pmBusy !== entry.id) {
        const man = mk('div');
        man.appendChild(mk('div', 'pnote', T('gui.plug.uninstall_note')));
        const rm = mk('button', 'mini ghost', T('gui.plug.uninstall'));
        rm.style.marginTop = '8px';
        pmArm(rm, () => pmRemove(entry.id, pmText(entry.name)));
        man.appendChild(rm);
        sec(T('gui.plug.sec_manage'), man);
      }
      $('#detail').dataset.open = 'true';
    }).catch((e) => { toast(T('gui.plug.op_failed', { err: e.message || e })); });
    $('#dTitle').textContent = '';
    $('#detail').dataset.open = 'true';
    body.appendChild(mk('div', 'pnote', T('gui.hub.reading')));
    return;
  }

  // installed server drawer
  const row = pmMcpRows().find((p) => p.m.name === id);
  if (!row) return;
  const m = row.m;
  $('#dTitle').textContent = '';
  const st = pmStatus(m);
  const head = mk('div', 'pmdhead');
  head.appendChild(pmTile(m.name));
  const meta = mk('div', 'pmdmeta');
  const l1 = mk('div', 'l1');
  l1.appendChild(mk('b', null, m.name));
  meta.appendChild(l1);
  meta.appendChild(mk('div', 'l2', [pmMarketIds.has(m.name) ? T('gui.plug.from_market') : T('gui.plug.manual'), m.transport].join(' · ')));
  head.appendChild(meta);
  const hact = mk('div', 'dact');
  if (pmAuthWait[m.name]) {
    const re = mk('button', 'mini', T('gui.plug.reopen'));
    re.onclick = () => window.open(pmAuthWait[m.name], '_blank');
    hact.appendChild(re);
  } else if (m.state === 'auth_required' || m.state === 'error') {
    const fix = mk('button', 'mini', T(m.state === 'auth_required' ? 'gui.plug.reauth' : 'gui.caps.repair'));
    fix.onclick = () => pmAuth(m.name);
    hact.appendChild(fix);
  } else if (m.enabled) {
    const use = mk('button', 'mini gold', T('gui.hub.use'));
    use.onclick = () => useInTask('gui.plug.use_prompt', m.name);
    hact.appendChild(use);
  }
  head.appendChild(hact);
  body.appendChild(head);

  const conn = mk('div');
  const stEl = mk('span', 'pmst ' + st.cls);
  stEl.append(mk('i', 'pmdot'), mk('span', null, T(st.k)));
  conn.appendChild(stEl);
  if (m.error && st.cls === 'bad') conn.appendChild(mk('div', 'perr', m.error));
  if (m.tool_count) conn.appendChild(mk('div', 'pnote', T('gui.plug.tools_n', { n: m.tool_count })));
  const s1 = mk('div', 'pmsec');
  s1.appendChild(mk('div', 'cap', T('gui.plug.sec_conn')));
  s1.appendChild(conn);
  body.appendChild(s1);

  const man = mk('div');
  man.appendChild(mk('div', 'pnote', T('gui.plug.uninstall_note')));
  const rm = mk('button', 'mini ghost', T('gui.plug.uninstall'));
  rm.style.marginTop = '8px';
  pmArm(rm, () => pmRemove(m.name, m.name));
  man.appendChild(rm);
  const s2 = mk('div', 'pmsec');
  s2.appendChild(mk('div', 'cap', T('gui.plug.sec_manage')));
  s2.appendChild(man);
  body.appendChild(s2);
  $('#detail').dataset.open = 'true';
}

/* ── page assembly ───────────────────────────────────────────────── */

/* The 已安装 entry point rides in the filter bar, like the skills view
   switch — created once, shown only on the plugin tab. */
const pmInstBtn = (() => {
  const b = mk('button', 'pminstbtn');
  b.onclick = () => { plugView = plugView === 'installed' ? 'market' : 'installed'; pmCloseDetail(); drawCaps(); };
  $('.cbar').appendChild(b);
  return { el: b, sync() {
    b.hidden = extTab !== 'plugin' || plugView === 'installed';
    const n = PLUGINS.filter((p) => !p.m || !pmPending.has(p.m.name)).length;
    const attn = attnCount();
    b.innerHTML = '';
    b.append(mk('span', null, T('gui.plug.installed_n', { n })));
    if (attn) b.appendChild(mk('span', 'pmbdg', String(attn)));
  } };
})();

{
  /* The hero sits above the search bar, so it lives outside #capsBody --
     one node, repopulated on every draw for whichever view is up. */
  const pageHero = () => {
    let h = $('#pageHero');
    if (!h) {
      h = mk('div', 'pmhero');
      h.id = 'pageHero';
      const bar = document.querySelector('#capsPage .cbar');
      bar.parentNode.insertBefore(h, bar);
    }
    return h;
  };
  /* Title only — no tagline under it; that copy read as marketing, not UI. */
  const syncHero = () => {
    const h = pageHero();
    h.innerHTML = '';
    let title = '';
    if (extTab === 'plugin' && plugView === 'market') title = T('gui.plug.hero');
    else if (extTab === 'skill' && skView === 'market') title = T('gui.hub.hero');
    h.hidden = !title;
    if (title) h.appendChild(mk('h3', null, title));
  };

  const prevDrawCaps = drawCaps;
  drawCaps = function () {
    if (extTab !== 'plugin') {
      // Undo this tab's chrome before handing back: the plugin view hid the
      // status pills, and the skill view re-hides them for itself.
      $('#cKind').hidden = false;
      $('.cbar').style.display = '';
      prevDrawCaps();
      pmInstBtn.sync();
      syncHero();
      return;
    }

    const box = $('#capsBody'); box.innerHTML = '';
    const title = T('gui.tab.plugins');
    $('#capsTitle').textContent = plugView === 'installed' ? T('gui.plug.installed_title') : title;
    $('#capsPage').setAttribute('aria-label', title);
    $('#cKind').hidden = true;
    $('#advAdd').hidden = plugView !== 'market';
    $('#cq').placeholder = T('gui.plug.search_ph');
    $('.cbar').style.display = plugView === 'installed' ? 'none' : '';
    skInstBtn.sync();  // the skills 已安装 button must not linger on this tab

    if (plugView === 'installed') drawPlugInstalled(box);
    else drawPlugMarket(box);
    pmInstBtn.sync();
    syncHero();
    drawCapsBadge();
  };

  const prevShowPage = showPage;
  showPage = function (id) {
    prevShowPage(id);
    if (id !== 'capsPage') { pmDrawer = null; $('.cbar').style.display = ''; }
  };

  const prevExtSet = extSet;
  extSet = function (tab) {
    const was = extTab;
    prevExtSet(tab);
    if (extTab !== was) { plugView = 'market'; pmDrawer = null; pmQuery = ''; pmCat = ''; $('.cbar').style.display = ''; }
  };

  const prevInput = $('#cq').oninput;
  $('#cq').oninput = () => {
    if (extTab === 'plugin') { pmQuery = $('#cq').value.trim(); pmSearchSoon(); return; }
    if (prevInput) prevInput();
  };

  const prevOpenCaps = openCaps;
  openCaps = async function (tab) {
    await prevOpenCaps(tab);
    if (extTab === 'plugin' && pmState === 'idle') pmSearch();
  };

  const prevCloseDetail = closeDetail;
  // Dismissing the sheet mid-install keeps pmProg so the card reopens the
  // progress view; a settled install is acknowledged by the close.
  closeDetail = function () {
    pmDrawer = null;
    if (pmProg && pmProg.state !== 'run') pmProg = null;
    prevCloseDetail();
  };
  // The X button captured the original closeDetail reference at base load.
  $('#dClose').onclick = () => closeDetail();
}

