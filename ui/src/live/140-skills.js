/* -- skill market: browse, search and install from SkillHub -----------
   The skill tab mirrors the plugin tab exactly: the market IS the page,
   what you already have lives one level in (the 已安装 button top-right,
   back arrow to return), a category chip row filters, and every card
   opens the shared detail drawer.

   Everything comes from skillhub.search / skillhub.detail — the hub's
   /skills/search, 93k skills, paged, filterable by category. */
const HUB_CATS = [
  ['', 'gui.hubcat.all'],
  ['DEV', 'gui.hubcat.DEV'],
  ['FRONTEND-UI', 'gui.hubcat.FRONTEND-UI'],
  ['DEVOPS-INFRA', 'gui.hubcat.DEVOPS-INFRA'],
  ['DATA', 'gui.hubcat.DATA'],
  ['AI-ML', 'gui.hubcat.AI-ML'],
  ['TESTING', 'gui.hubcat.TESTING'],
  ['SECURITY', 'gui.hubcat.SECURITY'],
  ['AUTH', 'gui.hubcat.AUTH'],
  ['MULTIMEDIA', 'gui.hubcat.MULTIMEDIA'],
  ['WRITING', 'gui.hubcat.WRITING'],
  ['DOC-PROC', 'gui.hubcat.DOC-PROC'],
  ['COMMS', 'gui.hubcat.COMMS'],
  ['WORKFLOW', 'gui.hubcat.WORKFLOW'],
  ['PRODUCTIVITY', 'gui.hubcat.PRODUCTIVITY'],
  ['META', 'gui.hubcat.META'],
  ['OTHER', 'gui.hubcat.OTHER'],
];
const HUB_PAGE = 24;

let skView = 'market';   // market | installed — same shape as plugView
let hubQuery = '';
let hubCat = '';
let hubPage = 1;
let hubItems = [];
let hubTotal = 0;
let hubState = 'idle';   // idle | loading | done | error
let hubErr = '';
let hubDetails = {};     // id -> skillhub.detail result
let hubBusy = null;      // id of the skill being installed / removed
let hubDebounce = null;
let skDrawer = null;     // { kind: 'market'|'inst', id } while the drawer shows a skill

const hubCatLabel = (key) => { const c = HUB_CATS.find((x) => x[0] === key); return c ? T(c[1]) : key; };
const hubNum = (n) => (n >= 10000 ? `${(n / 1000).toFixed(0)}k` : n.toLocaleString('en-US'));

const skRedraw = () => {
  if ($('#capsPage').dataset.open === 'true' && extTab === 'skill') drawCaps();
  if (skDrawer) skOpenDetail(skDrawer.kind, skDrawer.id);
};

/* The hub search round-trip takes seconds, so page/category flips are
   cached for the session and served instantly; only a genuinely new
   (query, category, page) hits the network — behind a skeleton grid,
   never a frozen page. A request seq drops stale responses when the
   user outclicks the network. */
const hubCache = new Map();
let hubSeq = 0;

async function hubSearch(page) {
  hubPage = page || 1;
  const key = `${hubQuery}\0${hubCat}\0${hubPage}`;
  const hit = hubCache.get(key);
  if (hit) {
    hubItems = hit.items; hubTotal = hit.total;
    hubState = 'done'; hubErr = '';
    drawCaps();
    return;
  }
  const seq = ++hubSeq;
  hubState = 'loading'; hubErr = '';
  drawCaps();
  try {
    const r = await rpc.call('skillhub.search', {
      query: hubQuery, category: hubCat, page: hubPage, limit: HUB_PAGE,
    });
    if (seq !== hubSeq) return;
    hubItems = r.items || [];
    hubTotal = r.total || hubItems.length;
    hubState = 'done';
    hubCache.set(key, { items: hubItems, total: hubTotal });
    if (hubCache.size > 60) hubCache.delete(hubCache.keys().next().value);
  } catch (e) {
    if (seq !== hubSeq) return;
    hubItems = [];
    hubErr = (e.data && e.data.detail) || e.message || String(e);
    hubState = 'error';
  }
  drawCaps();
}

const hubSearchSoon = () => {
  clearTimeout(hubDebounce);
  hubDebounce = setTimeout(() => hubSearch(1), 320);
};

function hubInstall(it) {
  hubBusy = it.id; skRedraw();
  rpc.call('skillhub.install', { id: it.id })
    .then(() => loadExt().catch(() => {}))
    .then(() => { it.installed = true; it.installed_name = it.name; })
    .catch((e) => toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e })))
    .finally(() => { hubBusy = null; skRedraw(); });
}

function hubRemove(it) {
  hubBusy = it.id; skRedraw();
  rpc.call('skillhub.remove', { name: it.installed_name || it.name })
    .then(() => loadExt().catch(() => {}))
    .then(() => { it.installed = false; it.installed_name = ''; })
    .catch((e) => toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e })))
    .finally(() => { hubBusy = null; skRedraw(); });
}

/* Installed rows come from ext.list, so the market row (if any) is found
   by name; removal goes through the same skillhub.remove. */
function skRemoveInstalled(c) {
  hubBusy = c.id; skRedraw();
  rpc.call('skillhub.remove', { name: c.name })
    .then(() => {
      const it = hubItems.find((x) => x.name === c.name || x.installed_name === c.name);
      if (it) { it.installed = false; it.installed_name = ''; }
      toast(T('gui.caps.removed_x', { name: c.name }));
      skCloseDetail();
      return loadExt();
    })
    .catch((e) => toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e })))
    .finally(() => { hubBusy = null; skRedraw(); });
}

/* 去使用 = start a task with the capability already named: a fresh session
   whose composer opens pre-filled, cursor at the end, ready to complete. */
function useInTask(promptKey, name) {
  closeDetail();
  $('#newBtn').click();
  const ta = $('#ta');
  ta.value = T(promptKey, { name });
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

/* quality_score is 0-1; the site shows it out of five, so do the same. */
function hubStars(score) {
  const val = Math.round((score || 0) * 5 * 10) / 10;
  const box = mk('div', 'stars');
  for (let i = 1; i <= 5; i++) {
    const fill = Math.max(0, Math.min(1, val - i + 1));
    const s = mk('span', 'st');
    s.style.setProperty('--f', `${Math.round(fill * 100)}%`);
    s.textContent = '★';
    box.appendChild(s);
  }
  box.appendChild(mk('span', 'sv', val.toFixed(1)));
  return box;
}

/* ── market view: same card grammar as pmMarketCard ─────────────── */

function hubCard(it) {
  const c = mk('div', 'hubcard pmcard' + (it.installed ? ' dim' : ''));

  const top = mk('div', 'top');
  const nmwrap = mk('div', 'pmhead');
  nmwrap.appendChild(pmTile(it.name));
  const id = mk('div', 'pmid');
  const nm = mk('div', 'pmnm');
  nm.appendChild(mk('span', null, it.name));
  id.appendChild(nm);
  const pub = it.source || hubCatLabel(it.category);
  if (pub) id.appendChild(mk('div', 'pmpub', pub));
  nmwrap.appendChild(id);
  top.appendChild(nmwrap);
  top.appendChild(hubStars(it.quality_score));
  c.appendChild(top);

  c.appendChild(mk('p', 'one', it.description || ''));

  const foot = mk('div', 'foot');
  const act = mk('div', 'act');
  if (hubBusy === it.id) act.appendChild(mk('span', 'pnote', T('gui.hub.working')));
  else if (it.installed) act.appendChild(mk('span', 'okpill', T('gui.hub.installed')));
  else {
    // Two hit zones: the button installs right away, the card opens the sheet.
    const b = mk('button', 'mini gold', T('gui.hub.install'));
    b.onclick = (e) => { e.stopPropagation(); hubInstall(it); };
    act.appendChild(b);
  }
  foot.appendChild(act);
  c.appendChild(foot);

  c.tabIndex = 0;
  c.setAttribute('role', 'button');
  c.onclick = () => skOpenDetail('market', it.id);
  c.onkeydown = (e) => { if (e.key === 'Enter') skOpenDetail('market', it.id); };
  return c;
}

function drawSkillMarket(box) {
  const chips = mk('div', 'pmchips');
  HUB_CATS.forEach(([key, label]) => {
    const b = mk('button', 'pill', T(label));
    b.setAttribute('aria-pressed', String(hubCat === key));
    b.onclick = () => { if (key === hubCat) return; hubCat = key; hubSearch(1); };
    chips.appendChild(b);
  });
  box.appendChild(chips);

  if (hubErr) {
    const wrap = mk('div', 'empty-note');
    wrap.append(mk('div', null, T('gui.hub.err', { err: hubErr })), document.createElement('br'));
    const retry = mk('button', 'mini ghost', T('gui.plug.retry'));
    retry.onclick = () => hubSearch(hubPage);
    wrap.appendChild(retry);
    box.appendChild(wrap);
    return;
  }
  if (hubState === 'loading') {
    const g = mk('div', 'hubgrid');
    for (let i = 0; i < 9; i++) g.appendChild(hubSkeleton());
    box.appendChild(g);
    return;
  }
  if (!hubItems.length) { box.appendChild(mk('div', 'empty-note', T('gui.hub.empty_filter'))); return; }

  const g = mk('div', 'hubgrid');
  hubItems.forEach((it) => g.appendChild(hubCard(it)));
  box.appendChild(g);
  const pager = hubPager();
  if (pager) box.appendChild(pager);
}

/* Placeholder card shown the instant a chip / query flips, so the page
   answers the click immediately instead of freezing on stale results. */
function hubSkeleton() {
  const c = mk('div', 'hubcard skel');
  c.setAttribute('aria-hidden', 'true');
  const bar = (w, h, extra) => {
    const b = mk('span', 'sk');
    b.style.cssText = `width:${w};height:${h};${extra || ''}`;
    return b;
  };
  const top = mk('div', 'top');
  const head = mk('div', 'pmhead');
  head.appendChild(bar('34px', '34px', 'border-radius:10px;flex:none'));
  const id = mk('div', 'pmid');
  id.style.cssText = 'display:grid;gap:6px';
  id.append(bar('110px', '12px'), bar('70px', '9px'));
  head.appendChild(id);
  top.appendChild(head);
  c.appendChild(top);
  const one = mk('div');
  one.style.cssText = 'display:grid;gap:7px';
  one.append(bar('100%', '10px'), bar('72%', '10px'));
  c.appendChild(one);
  const foot = mk('div', 'foot');
  foot.append(bar('58px', '22px', 'margin-left:auto;border-radius:8px'));
  c.appendChild(foot);
  return c;
}

function hubPager() {
  const pages = Math.max(1, Math.ceil(hubTotal / HUB_PAGE));
  if (pages <= 1) return null;
  const bar = mk('div', 'hubpage');
  const prev = mk('button', 'mini ghost', T('gui.hub.prev'));
  prev.disabled = hubPage <= 1;
  prev.onclick = () => hubSearch(hubPage - 1);
  const next = mk('button', 'mini ghost', T('gui.hub.next'));
  next.disabled = hubPage >= pages;
  next.onclick = () => hubSearch(hubPage + 1);
  bar.append(prev, mk('span', 'pnote', T('gui.hub.page', { p: hubPage, n: hubNum(pages) })), next);
  return bar;
}

/* ── installed view: same shape as drawPlugInstalled ────────────── */

function skInstCard(c) {
  const card = mk('div', 'hubcard pmcard');
  const top = mk('div', 'top');
  const nmwrap = mk('div', 'pmhead');
  nmwrap.appendChild(pmTile(c.name));
  const id = mk('div', 'pmid');
  const nm = mk('div', 'pmnm');
  nm.appendChild(mk('span', null, c.name));
  id.appendChild(nm);
  if (c.src) id.appendChild(mk('div', 'pmpub', c.src));
  nmwrap.appendChild(id);
  top.appendChild(nmwrap);
  card.appendChild(top);

  card.appendChild(mk('p', 'one', c.one || ''));

  const foot = mk('div', 'foot');
  foot.appendChild(mk('span'));
  const act = mk('div', 'act');
  const use = mk('button', 'mini gold', T('gui.hub.use'));
  use.onclick = (e) => { e.stopPropagation(); useInTask('gui.hub.use_prompt', c.name); };
  act.appendChild(use);
  foot.appendChild(act);
  card.appendChild(foot);

  card.tabIndex = 0;
  card.setAttribute('role', 'button');
  card.onclick = () => skOpenDetail('inst', c.id);
  card.onkeydown = (e) => { if (e.key === 'Enter') skOpenDetail('inst', c.id); };
  return card;
}

function drawSkillInstalled(box) {
  const back = mk('div', 'pmback');
  const b = mk('button', 'mini ghost');
  b.textContent = '← ' + T('gui.plug.back');
  b.onclick = () => { skView = 'market'; skCloseDetail(); drawCaps(); };
  back.append(b, mk('b', null, T('gui.plug.installed_title')));
  box.appendChild(back);

  if (!SKILLS.length) { box.appendChild(mk('div', 'empty-note', T('gui.hub.empty_installed'))); return; }
  const g = mk('div', 'hubgrid');
  SKILLS.forEach((c) => g.appendChild(skInstCard(c)));
  box.appendChild(g);
}

/* ── detail drawer (market entries + installed skills) ──────────── */

function skCloseDetail() { skDrawer = null; closeDetail(); }

/* One renderer for both entry points: an installed skill opens the exact
   hub detail the market shows (fetched via the id its marker recorded);
   the only delta is the action set — 去使用 in the header, 卸载 under 管理.
   Hand-written/builtin skills have no hub entry to show, so they get the
   same head/about/manage skeleton without the hub sections. */
function skOpenDetail(kind, id) {
  skDrawer = { kind, id };
  if ($('#capsPage').dataset.open !== 'true') openCaps('skill');
  const body = $('#dBody'); body.innerHTML = '';
  $('#dTitle').textContent = '';
  $('#detail').dataset.open = 'true';

  const sec = (label, el) => {
    const s = mk('div', 'pmsec');
    if (label) s.appendChild(mk('div', 'cap', label));
    s.appendChild(el);
    body.appendChild(s);
  };

  const inst = kind === 'inst' ? SKILLS.find((x) => x.id === id) : null;
  if (kind === 'inst' && !inst) return;
  const it = kind === 'market'
    ? (hubItems.find((x) => x.id === id) || {})
    : (hubItems.find((x) => x.name === inst.name || x.installed_name === inst.name) || {});
  const hubId = kind === 'market' ? id : (inst.hubId || it.id || '');
  const installed = !!(inst || it.installed);
  const busy = hubBusy != null && (hubBusy === it.id || (inst && hubBusy === inst.id));
  const name = (inst && inst.name) || it.name || String(id);

  const header = (metaBits, sourceUrl) => {
    const head = mk('div', 'pmdhead');
    head.appendChild(pmTile(name));
    const meta = mk('div', 'pmdmeta');
    const l1 = mk('div', 'l1');
    l1.appendChild(mk('b', null, name));
    meta.appendChild(l1);
    const l2 = mk('div', 'l2', metaBits.filter(Boolean).join(' · ') + (sourceUrl ? ' · ' : ''));
    if (sourceUrl) {
      const a = mk('a', null, T('gui.plug.homepage') + ' ↗');
      a.href = sourceUrl; a.target = '_blank'; a.rel = 'noreferrer';
      l2.appendChild(a);
    }
    meta.appendChild(l2);
    head.appendChild(meta);
    const hact = mk('div', 'dact');
    if (busy) hact.appendChild(mk('span', 'pnote', T('gui.hub.working')));
    else if (installed) {
      const use = mk('button', 'mini gold', T('gui.hub.use'));
      use.onclick = () => useInTask('gui.hub.use_prompt', name);
      hact.appendChild(use);
    } else {
      const b = mk('button', 'mini gold', T('gui.hub.install'));
      b.onclick = () => hubInstall(it);
      hact.appendChild(b);
    }
    head.appendChild(hact);
    body.appendChild(head);
  };

  const manage = (removable, fn) => {
    if (!removable || busy) return;
    const man = mk('div');
    man.appendChild(mk('div', 'pnote', T('gui.hub.uninstall_note')));
    const rm = mk('button', 'mini ghost', T('gui.plug.uninstall'));
    rm.style.marginTop = '8px';
    pmArm(rm, fn);
    man.appendChild(rm);
    sec(T('gui.plug.sec_manage'), man);
  };

  if (!hubId) {
    header([inst.src, reachText(inst.reach)]);
    if (inst.one) sec(T('gui.plug.sec_about'), mk('div', 'pmdesc', inst.one));
    manage(inst.hub, () => skRemoveInstalled(inst));
    return;
  }

  const render = (d) => {
    if (!skDrawer || skDrawer.id !== id) return;
    body.innerHTML = '';

    header([hubCatLabel(d.category) || d.category, d.source || it.source, d.license], it.source_url);

    const about = mk('div');
    about.appendChild(mk('div', 'pmdesc', d.description || it.description || (inst && inst.one) || ''));
    const tags = (d.tags && d.tags.length ? d.tags : it.tags) || [];
    if (tags.length) {
      const tg = mk('div', 'tags');
      tg.style.marginTop = '8px';
      tags.forEach((t) => tg.appendChild(mk('span', 'tag', t)));
      about.appendChild(tg);
    }
    sec(T('gui.plug.sec_about'), about);

    const rate = mk('div');
    rate.appendChild(hubStars(d.quality_score != null ? d.quality_score : it.quality_score));
    const s = d.subscores || {};
    if (s.utility || s.robustness || s.safety) {
      rate.appendChild(mk('div', 'pnote', T('gui.hub.rating', { u: s.utility, r: s.robustness, s: s.safety })));
    }
    if (s.flags && s.flags.length) rate.appendChild(mk('div', 'perr', T('gui.hub.flags', { flags: s.flags.join(', ') })));
    sec(T('gui.hub.sec_rating'), rate);

    if (d.files && d.files.length) {
      const fb = mk('div');
      fb.appendChild(mk('div', 'pnote', d.files.slice(0, 12).join('  ·  ')
        + (d.files.length > 12 ? '  ·  ' + T('gui.hub.more_files', { n: d.files.length - 12 }) : '')));
      if (d.body_tokens) fb.appendChild(mk('div', 'pnote', `${hubNum(d.body_tokens)} tokens`));
      sec(T('gui.hub.n_files', { n: d.files.length }), fb);
    }
    if (d.skill_md) sec(T('gui.hub.sec_preview'), mk('pre', 'mdprev', d.skill_md.slice(0, 1600)));

    if (installed) manage(true, () => (inst ? skRemoveInstalled(inst) : hubRemove(it)));
  };

  const cached = hubDetails[hubId];
  if (cached) { render(cached); return; }
  body.appendChild(mk('div', 'pnote', T('gui.hub.reading')));
  rpc.call('skillhub.detail', { id: hubId })
    .then((d) => { hubDetails[hubId] = d; render(d); })
    .catch((e) => toast(T('gui.hub.err', { err: (e.data && e.data.detail) || e.message || e })));
}

/* ── page assembly: mirrors the plugin tab's wrapper ────────────── */

/* The 已安装 entry point rides in the filter bar, exactly like the
   plugin tab's — created once, shown only on the skill market. */
const skInstBtn = (() => {
  const b = mk('button', 'pminstbtn');
  b.onclick = () => { skView = skView === 'installed' ? 'market' : 'installed'; skCloseDetail(); drawCaps(); };
  $('.cbar').appendChild(b);
  return { sync() {
    b.hidden = extTab !== 'skill' || skView === 'installed';
    b.innerHTML = '';
    b.append(mk('span', null, T('gui.plug.installed_n', { n: SKILLS.length })));
  } };
})();

{
  const origDrawCaps = drawCaps;
  drawCaps = function () {
    if (extTab !== 'skill') { origDrawCaps(); skInstBtn.sync(); return; }

    const box = $('#capsBody'); box.innerHTML = '';
    const title = T('gui.tab.skills');
    $('#capsTitle').textContent = skView === 'installed' ? T('gui.plug.installed_title') : title;
    $('#capsPage').setAttribute('aria-label', title);
    $('#cKind').hidden = true;
    $('#advAdd').hidden = true;
    $('#cq').placeholder = T('gui.hub.search_ph');
    $('.cbar').style.display = skView === 'installed' ? 'none' : '';

    if (skView === 'installed') drawSkillInstalled(box);
    else drawSkillMarket(box);
    skInstBtn.sync();
    drawCapsBadge();
  };

  const origInput = $('#cq').oninput;
  $('#cq').oninput = () => {
    if (extTab === 'skill') { hubQuery = $('#cq').value.trim(); hubSearchSoon(); return; }
    if (origInput) origInput();
  };
  $('#cq').onkeydown = (e) => {
    if (composing(e)) return;
    if (e.key !== 'Enter' || extTab !== 'skill') return;
    e.preventDefault();
    clearTimeout(hubDebounce);
    hubQuery = $('#cq').value.trim();
    hubSearch(1);
  };

  const prevExtSet = extSet;
  extSet = function (tab) {
    const was = extTab;
    prevExtSet(tab);
    if (extTab !== was) { skView = 'market'; skDrawer = null; hubQuery = ''; hubCat = ''; }
  };

  const prevShowPage = showPage;
  showPage = function (id) {
    prevShowPage(id);
    if (id !== 'capsPage') skDrawer = null;
  };

  const prevOpenCaps = openCaps;
  openCaps = async function (tab) {
    await prevOpenCaps(tab);
    if (extTab === 'skill' && skView === 'market' && hubState === 'idle') hubSearch(1);
  };

  const prevCloseDetail = closeDetail;
  closeDetail = function () { skDrawer = null; prevCloseDetail(); };
  $('#dClose').onclick = () => closeDetail();
}

