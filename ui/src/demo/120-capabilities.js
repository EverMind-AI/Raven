/* ══ module 2: capabilities page ══════════════════════════════════ */
let extTab = 'skill', cKind = 'all', cQuery = '', dCur = null;

/* Only one module page at a time. They used to cover the whole window, so
   two open at once was invisible; now that the rail stays put, the one behind
   shows through. */
const NAV_OF = {
  capsPage: () => (extTab === 'plugin' ? 'plugBtn' : 'skillBtn'),
  xaPage: 'moreBtn',
  connPage: 'moreBtn',
  memPage: 'memBtn',
  cronPage: 'moreBtn',
};

function showPage(id) {
  Object.keys(NAV_OF).forEach((p) => { $('#' + p).dataset.open = String(p === id); });
  /* From the top, every time: the scroller keeps its position across a close
     and reopen, so a page could greet the reader halfway down its own list. */
  if (id) {
    const sc = $('#' + id).querySelector('.work');
    if (sc) sc.scrollTop = 0;
  }
  /* Read by the rail: while a page is up it owns the selected state, so the
     session behind it stops claiming one too. */
  document.querySelector('.app').dataset.page = id ? 'on' : 'off';
  markNewCurrent();
  /* caps and memory both use the shared detail drawer */
  if (id !== 'capsPage' && id !== 'memPage') closeDetail();
}

/* Switching module resets the filters: a query typed while browsing skills is
   not a question about plugins. */
function extSet(tab) {
  if (!tab || tab === extTab) return;
  extTab = tab; cKind = 'all'; cQuery = '';
  $('#cq').value = '';
  [...$('#cKind').children].forEach((c, i) => c.setAttribute('aria-pressed', String(i === 0)));
  closeDetail();
}

function openCaps(tab) { extSet(tab); showPage('capsPage'); drawCaps(); }
const openSkills = () => openCaps('skill');
const openPlugins = () => openCaps('plugin');
function closeCaps() { showPage(null); closeDetail(); }
/* A dialog, so it layers over whatever you were reading rather than replacing
   it -- but the rail still marks itself, since that is where you came from. */
/* Called from markNewCurrent, which runs during the first draw -- so it has to
   tolerate being asked before the dialog's markup is in the document. */
function setIsOpen() {
  const v = $('#setVeil');
  return !!v && v.dataset.open === 'true';
}
function openSet() {
  $('#setVeil').dataset.open = 'true';
  markNewCurrent();
  $('#setModal').focus();
}
function closeSet() {
  $('#setVeil').dataset.open = 'false';
  markNewCurrent();
}

function drawCaps() {
  const box = $('#capsBody'); box.innerHTML = '';
  const title = T(extTab === 'plugin' ? 'gui.tab.plugins' : 'gui.tab.skills');
  $('#capsTitle').textContent = title;
  $('#capsPage').setAttribute('aria-label', title);
  $('#advAdd').hidden = extTab !== 'plugin';
  $('#cq').placeholder = T('gui.caps.search_' + extTab);

  const list = extTab === 'skill' ? SKILLS : PLUGINS;
  const byState = (c) => cKind === 'all'
    || (cKind === 'on' && c.state === 'on' && !needsAttn(c))
    || (cKind === 'attn' && needsAttn(c))
    || (cKind === 'add' && c.state === 'add');
  const rows = list.filter((c) => byState(c)
    && (!cQuery || (c.name + c.one + (c.cat || '') + reachText(c.reach)).toLowerCase().includes(cQuery)));

  const section = (label, items, cls, hint) => {
    if (!items.length) return;
    const s = mk('div', 'csec' + (cls ? ' ' + cls : ''));
    const hd = mk('div', 'hd');
    hd.append(mk('b', null, label), mk('span', 'n', String(items.length)));
    if (hint) hd.appendChild(mk('span', 'n', '· ' + hint));
    s.appendChild(hd);
    const g = mk('div', 'grid');
    items.forEach((c) => g.appendChild(capCard(c)));
    s.appendChild(g);
    box.appendChild(s);
  };

  // Blocked items pin above everything: a missing key is a to-do, not
  // something the user should have to discover by browsing.
  section(T('gui.caps.sec_attn'), rows.filter(needsAttn), 'attn', T('gui.caps.sec_attn_h'));
  section(T('gui.caps.sec_on'), rows.filter((c) => (c.state === 'on' || c.state === 'off') && !needsAttn(c)));
  section(T('gui.caps.sec_add'), rows.filter((c) => c.state === 'add'), '',
    T(extTab === 'skill' ? 'gui.caps.from_skills' : 'gui.caps.from_market'));

  if (!rows.length) box.appendChild(mk('div', 'empty-note', T('gui.caps.none')));

  drawCapsBadge();
}

/* Tools are a fixed inventory the agent ships with, not a store -- which is
   why they live in Settings under the agent, not in a module that exists for
   adding and removing things. Rendered by SET_PAGE.toolset. */

/* Which tools take a credential, and where it is stored. These used to be a
   separate settings page of four unexplained key fields; a key belongs on the
   tool it unlocks, where "set / not set" reads next to the switch it gates. */
const TOOL_CRED = {
  web_search: 'tools.web.search.apiKey',
  web_fetch: 'tools.web.jinaApiKey',
  image_generate: 'tools.media.image.apiKey',
  deep_research: 'tools.deepResearch.apiKey',
};
let toolKeyEdit = null;   // tool id whose credential editor is unfolded

function toolCredRow(t, path) {
  const row = mk('div', 'tkrow tkey');
  const on = !!V(path, '');
  const chip = mk('span', 'kchip' + (on ? '' : ' off'));
  chip.append(mk('span', 'led'), mk('span', null, T(on ? 'gui.set.tls.key_set' : 'gui.set.tls.key_unset')));
  row.appendChild(chip);
  const i = mk('input');
  i.type = 'password';
  i.placeholder = 'API Key';
  i.autocomplete = 'off';
  const b = mk('button', 'mini', T(on ? 'gui.model.update' : 'gui.plug.connect'));
  b.onclick = () => {
    if (!i.value.trim()) { i.focus(); return; }
    if (!settingsWrite) { nlSay(b); return; }
    settingsWrite(path, i.value.trim(), b);
  };
  row.append(i, b);
  if (on) {
    const x = mk('button', 'mini ghost', T('gui.set.tls.clear'));
    x.onclick = () => (settingsWrite ? settingsWrite(path, '', x) : nlSay(x));
    row.appendChild(x);
  }
  return row;
}

function toolLine(t) {
  const r = mk('div', 'trow' + (t.on ? '' : ' off'));
  const nm = mk('div', 'nm');
  nm.appendChild(mk('span', null, t.name));
  if (t.danger) {
    const d = mk('span', 'tag warn', T('gui.caps.mutates'));
    d.style.fontSize = '10px';
    nm.appendChild(d);
  }
  const cred = TOOL_CRED[t.id];
  if (cred) {
    /* The key's state rides on the row itself: a switched-on tool with no key
       is the gap this chip exists to make visible. */
    const on = !!V(cred, '');
    const chip = mk('span', 'kchip' + (on ? '' : ' off'));
    chip.style.fontSize = '10px';
    chip.append(mk('span', 'led'), mk('span', null, T(on ? 'gui.set.tls.key_set' : 'gui.set.tls.key_unset')));
    nm.appendChild(chip);
  }
  const one = mk('div', 'one', t.one);
  one.title = t.one;
  r.append(nm, one);
  const b = mk('div', 'bdgs');
  b.appendChild(reachBadge(t.reach));
  r.appendChild(b);
  const ctl = mk('div', 'ctl');
  if (cred) {
    const cfg = mk('button', 'mini ghost', T(toolKeyEdit === t.id ? 'gui.set.mem.fold' : 'gui.caps.configure'));
    cfg.onclick = () => { toolKeyEdit = toolKeyEdit === t.id ? null : t.id; drawSettings(); };
    ctl.appendChild(cfg);
  }
  if (t.needs) {
    /* No switch: the tool is withheld for want of a key, and a toggle here
       would promise something the flip cannot deliver. The key is the switch --
       fill it and the tool registers itself on the next start. */
    ctl.appendChild(mk('span', 'pnote', T('gui.caps.needs_key')));
    r.appendChild(ctl);
    return r;
  }
  const s = mk('button', 'swi');
  s.setAttribute('role', 'switch');
  s.setAttribute('aria-checked', String(t.on));
  s.setAttribute('aria-label', T('gui.caps.toggle_aria', { name: t.name }));
  s.onclick = () => {
    t.on = !t.on;
    drawSettings();
    toast(T(t.on ? 'gui.caps.enabled_x' : 'gui.caps.disabled_x', { name: t.name }));
  };
  ctl.appendChild(s);
  r.appendChild(ctl);
  return r;
}

function reachBadge(reach) {
  const b = mk('span', 'kd' + (reach === 'auth' ? ' auth' : ''), reachText(reach));
  b.title = reachHint(reach);
  return b;
}

function capCard(c) {
  // div+role, not <button>: the card holds real buttons (switch, 配置) and a
  // button may not contain interactive descendants
  const card = mk('div', 'card' + (c.state === 'fail' ? ' dead' : needsAttn(c) ? ' attn' : ''));
  card.setAttribute('role', 'button');
  card.tabIndex = 0;
  card.onkeydown = (e) => { if (e.key === 'Enter') openDetail(c.id); };
  // Same stable-hue letter tile as the plugin market (hash duplicated so the
  // base shell keeps working without live.js).
  let th = 0;
  for (let i = 0; i < c.name.length; i++) th = (th * 31 + c.name.charCodeAt(i)) >>> 0;
  card.appendChild(mk('span', 'pmtile th' + (th % 8), (c.name[0] || '?').toUpperCase()));
  const nm = mk('div', 'nm');
  // The badge answers "where does my data go", which "built-in vs external"
  // does NOT answer — that is why it rides on every row in all three tabs.
  nm.append(mk('span', null, c.name), reachBadge(c.reach));
  card.append(nm, mk('div', 'one', c.one));

  const st = STATE_TXT[c.state];
  const line = mk('div', 'st ' + (c.update && c.state === 'on' ? 'new' : st.cls));
  line.textContent = c.state === 'fail' ? c.err
    : c.update && c.state === 'on' ? T('gui.caps.update_to', { from: c.ver, to: c.update })
    : c.state === 'need' ? T('gui.caps.needs_cfg')
    : T(st.t);
  card.appendChild(line);

  const ctl = mk('div', 'ctl');
  if (c.state === 'need' || c.state === 'fail') {
    const b = mk('button', 'mini', T(c.state === 'fail' ? 'gui.caps.repair' : 'gui.caps.configure'));
    b.onclick = (e) => { e.stopPropagation(); openDetail(c.id); };
    ctl.appendChild(b);
  } else if (c.state === 'add') {
    const b = mk('button', 'mini ghost', T('gui.add'));
    b.onclick = (e) => { e.stopPropagation(); addCap(c); };
    ctl.appendChild(b);
  } else {
    ctl.appendChild(switchFor(c));
  }
  card.appendChild(ctl);
  card.onclick = () => openDetail(c.id);
  return card;
}

function switchFor(c) {
  const s = mk('button', 'swi');
  s.setAttribute('role', 'switch');
  s.setAttribute('aria-checked', String(c.state === 'on'));
  s.setAttribute('aria-label', T('gui.caps.toggle_aria', { name: c.name }));
  s.onclick = (e) => {
    e.stopPropagation();
    c.state = c.state === 'on' ? 'off' : 'on';
    afterCapChange(c, T(c.state === 'on' ? 'gui.caps.enabled_x' : 'gui.caps.disabled_x', { name: c.name }));
  };
  return s;
}

function addCap(c) {
  c.state = c.fields && c.fields.length ? 'need' : 'on';
  afterCapChange(c, T(c.state === 'on' ? 'gui.caps.added_on' : 'gui.caps.added_need', { name: c.name }));
  if (c.state === 'need') openDetail(c.id);
}

function afterCapChange(c, msg) {
  drawCaps(); drawCapsBadge(); drawBanner();
  if (dCur === c.id) openDetail(c.id);
  toast(msg, c.state === 'on'
    ? { label: T('gui.caps.back_to_chat'), fn: () => { closeCaps(); $('#ta').focus(); } }
    : null);
}

function openDetail(id) {
  const c = cap(id); if (!c) return;
  dCur = id;
  if ($('#capsPage').dataset.open !== 'true') openCaps();
  $('#dTitle').textContent = c.name;
  const b = $('#dBody'); b.innerHTML = '';

  const st = STATE_TXT[c.state];
  const head = mk('div');
  const badge = mk('span', 'tag' + (st.cls === 'warn' || st.cls === 'bad' ? ' warn' : ''), T(st.t));
  head.appendChild(badge);
  if (c.update && c.state === 'on') {
    const up = mk('button', 'mini', T('gui.detail.update_to_v', { v: c.update }));
    up.style.marginLeft = '8px';
    up.onclick = () => {
      c.ver = c.update; delete c.update;
      afterCapChange(c, T('gui.detail.updated_v', { v: c.ver }));
    };
    head.appendChild(up);
  }
  b.appendChild(head);

  b.appendChild(mk('p', null, c.one)).style.cssText = 'margin:0;color:var(--muted);font-size:13px';
  if (c.why) b.appendChild(mk('div', 'note', c.why)).style.borderTop = '0';

  if (c.state === 'fail') {
    const e = mk('div', 'probe bad', '⚠ ' + c.err);
    b.appendChild(e);
  }

  const dl = mk('dl', 'kv');
  const kv = (k, v) => { dl.append(mk('dt', null, k), mk('dd', null, v)); };
  kv(T('gui.detail.kind'), T(SKILLS.includes(c) ? 'gui.detail.kind_skill' : 'gui.detail.kind_plugin'));
  kv(T('gui.detail.reach'), `${reachText(c.reach)} · ${reachHint(c.reach)}`);
  if (c.cat) kv(T('gui.detail.category'), c.cat);
  kv(T('gui.detail.source'), c.src);
  kv(T('gui.detail.version'), c.ver);
  if (c.account) kv(T('gui.detail.account'), c.account);
  b.appendChild(dl);

  if (c.fields && c.fields.length) {
    b.appendChild(mk('span', 'lab', T('gui.detail.config')));
    const box = mk('div'); box.style.cssText = 'display:grid;gap:9px';
    const inputs = [];
    c.fields.forEach((f) => {
      const w = mk('div'); w.style.cssText = 'display:grid;gap:5px';
      w.appendChild(mk('label', null, f.k)).style.cssText = 'font-size:12.5px;color:var(--muted)';
      const inp = mk('input');
      inp.type = f.pw ? 'password' : 'text';
      inp.placeholder = f.ph || '';
      if (f.val) inp.value = f.val;
      w.appendChild(inp); box.appendChild(w); inputs.push([f, inp]);
    });
    b.appendChild(box);

    const row = mk('div'); row.style.cssText = 'display:flex;gap:8px;margin-top:4px';
    const save = mk('button', 'mini', T(c.state === 'on' ? 'gui.detail.save' : 'gui.detail.save_on'));
    save.onclick = () => {
      const missing = inputs.some(([f, i]) => !i.value.trim() && f.pw);
      if (missing) { toast(T('gui.detail.need_cred')); inputs[0][1].focus(); return; }
      inputs.forEach(([f, i]) => { f.val = i.value; });
      c.state = 'on'; delete c.err;
      afterCapChange(c, T('gui.caps.enabled_x', { name: c.name }));
    };
    row.appendChild(save);
    b.appendChild(row);
  }

  if (PLUGINS.includes(c)) {
    const probe = mk('div');
    const t = mk('button', 'mini ghost', T('gui.detail.test'));
    const out = mk('div', 'probe');
    t.onclick = () => {
      out.className = 'probe'; out.textContent = T('gui.detail.testing');
      setTimeout(() => {
        if (c.state === 'fail') { out.className = 'probe bad'; out.textContent = '✗ ' + c.err; }
        else if (c.state === 'on') {
          out.className = 'probe ok';
          out.textContent = '✓ ' + T('gui.detail.test_ok', { n: c.tools.length });
        } else { out.className = 'probe'; out.textContent = T('gui.detail.test_off'); }
      }, 900);
    };
    probe.append(t, out);
    b.appendChild(probe);
  }

  if (c.tools && c.tools.length) {
    b.appendChild(mk('span', 'lab', T('gui.detail.tools')));
    const g = mk('div', 'chips');
    c.tools.forEach((t) => g.appendChild(mk('span', 'tag', t)));
    b.appendChild(g);
  }

  b.appendChild(mk('span', 'lab', T('gui.detail.can_do')));
  const pg = mk('div', 'chips');
  (c.perms || []).forEach((p) => pg.appendChild(mk('span', 'tag warn', p)));
  if (!c.perms || !c.perms.length) pg.appendChild(mk('span', 'tag', T('gui.detail.no_ext')));
  b.appendChild(pg);

  if (c.state === 'on' || c.state === 'off' || c.state === 'fail' || c.state === 'need') {
    const foot = mk('div'); foot.style.cssText = 'border-top:1px solid var(--line-soft);padding-top:14px';
    const rm = mk('button', 'mini ghost danger', T('gui.detail.remove'));
    rm.onclick = () => confirmAsk(T('gui.detail.remove_title'),
      T('gui.detail.remove_body', { name: c.name }), T('gui.detail.remove_yes'), () => {
        c.state = 'add'; delete c.err; (c.fields || []).forEach((f) => delete f.val);
        closeDetail(); afterCapChange(c, T('gui.caps.removed_x', { name: c.name }));
      });
    foot.appendChild(rm);
    b.appendChild(foot);
  }

  $('#detail').dataset.open = 'true';
}
function closeDetail() { $('#detail').dataset.open = 'false'; dCur = null; xaSheet = null; }

/* ══ entrances: where you reach it ════════════════════════════════
   NOT a capability. A plugin is "what it can touch"; an entrance is
   "where you find it". Same brand can be both (Slack plugin vs Slack
   entrance) and the two point in opposite directions.              */
/* ══ module 2b: external agents ════════════════════════════════════
   Connect the agents already installed on this machine, so Raven can hand work
   to them. One row per agent; the rows are whatever `DS.xa` answers -- the
   fixture source below with no gateway behind the page, the `subagents.*`
   source in the live layer.

   A row is (name, kind, configured, enabled, probe_status, last test). Three
   facts, deliberately kept apart: `configured` is "Raven knows about it",
   `enabled` is "Raven may dispatch to it", and `probe_status` is "the machine
   can actually run it". Collapsing them is how a disabled agent reads as
   broken, or a missing binary reads as switched off. */
const XA_FIXTURE = [
  { name: 'claude_code', preset: 'claude_code', kind: 'cli', configured: true, enabled: true,
    probe_status: 'ready', probe_detail: '', has_api_key: false, test_running: false,
    last_test_ok: true, last_test_at_ms: Date.now() - 3600e3, last_test_detail: '',
    upgrade_to: null, description: 'Claude Code CLI - strong general coding / agent tasks.' },
  { name: 'codex', preset: 'codex', kind: 'cli', configured: false, enabled: false,
    probe_status: 'missing', probe_detail: 'codex: command not found', has_api_key: false,
    test_running: false, last_test_ok: null, last_test_at_ms: null, last_test_detail: '',
    description: 'OpenAI Codex CLI - coding tasks.' },
  { name: 'hermes', preset: 'hermes', kind: 'cli', configured: false, enabled: false,
    probe_status: 'ready', probe_detail: '', has_api_key: false, test_running: false,
    last_test_ok: null, last_test_at_ms: null, last_test_detail: '',
    description: 'Hermes Agent CLI - general assistant with tool calling.' },
  { name: 'mirothinker', preset: 'mirothinker', kind: 'openai', configured: false, enabled: false,
    probe_status: 'unknown', probe_detail: '', has_api_key: false, test_running: false,
    last_test_ok: null, last_test_at_ms: null, last_test_detail: '',
    description: 'MiroMind deep-research (OpenAI-compatible HTTP).' },
];

/* The rows on screen, and the two flags only the page can answer: which sheet
   is unfolded, and whether a probe is in flight. Both are about what is drawn,
   so neither belongs to whichever source is answering. */
let XAGENTS = [];
let xaSheet = null;  /* name whose detail sheet is open */
let xaProbing = false;

/* The fixture source: mutate the row in place so the page is still explorable
   with no gateway behind it, and answer with the same array every time, which
   is what makes those edits stick across a redraw. */
DS.xa ??= {
  load: async () => XA_FIXTURE,
  act: async (op, row, args) => {
    const a = args || {};
    if (op === 'connect') { row.configured = true; row.enabled = row.kind !== 'openai'; }
    if (op === 'remove') { row.configured = false; row.enabled = false; }
    if (op === 'upgrade') { row.kind = row.upgrade_to || row.kind; row.upgrade_to = null; }
    if (op === 'toggle') row.enabled = !!a.enabled;
    if (op === 'update') {
      if (a.new_name) row.name = a.new_name;
      if (a.description != null) row.description = a.description || row.description;
      if (a.api_key) { row.has_api_key = true; row.enabled = true; }
    }
    if (op === 'test') {
      row.last_test_ok = row.probe_status === 'ready';
      row.last_test_at_ms = Date.now();
    }
    return XA_FIXTURE;
  },
};

function xaKind(row) {
  return T(row.kind === 'openai' ? 'gui.agent.kind_openai'
    : row.kind === 'acp' ? 'gui.agent.kind_acp' : 'gui.agent.kind_cli');
}

/* The one-line health summary. Order matters: the reason it cannot run beats
   the fact that it is switched off, because that is the one the user has to act
   on. */
function xaState(row) {
  if (row.kind === 'openai' && !row.has_api_key) return { cls: 'bad', text: T('gui.agent.needs_key') };
  if (row.configured && !row.enabled && row.probe_status !== 'missing') {
    return { cls: 'off', text: T('gui.agent.disabled') };
  }
  /* Four probe verdicts, and they are not two. `attention` means the binary is
     there but nothing has verified it can do a task -- an amber nudge, not a
     failure; calling it broken would have the user reinstalling something that
     is installed. `unknown` is "not measured", which earns no colour at all. */
  if (row.probe_status === 'ready') return { cls: 'ok', text: T('gui.agent.ready') };
  if (row.probe_status === 'attention') return { cls: 'warn', text: row.probe_detail || T('gui.agent.unverified') };
  if (row.probe_status === 'unknown') return { cls: 'off', text: row.probe_detail || '' };
  return { cls: 'bad', text: row.probe_detail || T('gui.agent.missing') };
}

function xaTestLine(row) {
  if (row.test_running) return T('gui.agent.testing');
  if (row.last_test_ok == null) return T('gui.agent.test_never');
  /* fmtStamp comes from live.js, which the build appends into this same script;
     the standalone demo has no live layer, hence the guard. */
  const ago = !row.last_test_at_ms ? ''
    : typeof fmtStamp === 'function' ? fmtStamp(row.last_test_at_ms)
      : new Date(row.last_test_at_ms).toLocaleString();
  return T(row.last_test_ok ? 'gui.agent.test_ok' : 'gui.agent.test_bad', { ago });
}

/* Every mutation goes through here, and the two long-running ones say so on
   screen before they hand over: a probe re-measures every entry and a test runs
   the agent for real, either of which can outlast a model turn, and a button
   that neither moves nor disables reads as a dead button. The flags are the
   page's, so they are set here rather than inside whichever source answers. */
async function xaRun(op, row, args) {
  try {
    if (op === 'probe') {
      xaProbing = true;
      drawXa();
      try { XAGENTS = await DS.xa.load(true); } finally { xaProbing = false; }
    } else {
      if (op === 'test') { row.test_running = true; drawXa(); }
      try {
        XAGENTS = await DS.xa.act(op, row, args || {});
      } finally {
        if (op === 'test') row.test_running = false;
      }
    }
  } catch (e) {
    toast(T('gui.agent.failed', { detail: (e && (e.message || e.detail)) || String(e) }));
  }
  drawXa();
  drawXaBadge();
  if (xaSheet) {
    const cur = XAGENTS.find((x) => x.name === xaSheet);
    if (cur) xaSheetDraw(cur); else closeDetail();
  }
}

/* The agent's detail sheet: everything about one row in one place, drawn into
   the same #detail dialog the plugin and skill details use -- identity block
   up top, one decisive action beside it, then sections. Editing happens HERE,
   never squeezed into the list row: the list answers "which agents, are they
   alive", the sheet answers everything else.

   Configure means name, description, and -- only where one is needed -- the
   api key. Never the command line. That comes from the preset, which is
   version verified; letting the page edit it is how a working agent turns
   into a command nobody can account for. */
function xaSheetOpen(row) {
  xaSheet = row.name;
  xaSheetDraw(row);
  $('#detail').dataset.open = 'true';
}

function xaSheetDraw(row) {
  const st = xaState(row);
  $('#dTitle').textContent = '';
  const b = $('#dBody'); b.innerHTML = '';

  const head = mk('div', 'pmdhead');
  let th = 0;
  for (let i = 0; i < row.name.length; i++) th = (th * 31 + row.name.charCodeAt(i)) >>> 0;
  head.appendChild(mk('span', 'pmtile th' + (th % 8), row.name[0].toUpperCase()));
  const meta = mk('div', 'pmdmeta');
  const l1 = mk('div', 'l1');
  l1.append(mk('span', 'led' + (st.cls === 'ok' ? '' : ' ' + st.cls)),
    mk('b', null, row.name), mk('span', 'kd', xaKind(row)));
  meta.appendChild(l1);
  meta.appendChild(mk('div', 'l2', row.description || ''));
  head.appendChild(meta);
  const act = mk('div', 'dact');
  if (!row.configured) {
    /* Connecting an HTTP agent needs its key first, so the head action defers
       to the form's save; every other kind connects in one click. */
    if (row.kind !== 'openai') {
      const go = mk('button', 'mini', T('gui.agent.connect'));
      go.onclick = () => xaRun('connect', row, {});
      act.appendChild(go);
    }
  } else if (row.test_running) {
    const stop = mk('button', 'mini ghost', T('gui.agent.test_cancel'));
    stop.onclick = () => xaRun('test_cancel', row);
    act.appendChild(stop);
  } else {
    const test = mk('button', 'mini', T('gui.agent.test'));
    test.dataset.tip = T('gui.agent.test_spend', { name: row.name });
    test.onclick = () => xaRun('test', row);
    act.appendChild(test);
  }
  head.appendChild(act);
  b.appendChild(head);

  const s1 = mk('div', 'pmsec');
  s1.appendChild(mk('span', 'cap', T('gui.agent.sec_status')));
  const line = mk('div', 'probe' + (st.cls === 'ok' ? ' ok' : st.cls === 'bad' ? ' bad' : ''),
    row.test_running ? T('gui.agent.testing') : (st.text || '—'));
  line.style.marginTop = '0';
  s1.appendChild(line);
  const dl = mk('dl', 'kv');
  dl.style.marginTop = '10px';
  dl.append(mk('dt', null, T('gui.agent.sec_transport')), mk('dd', null, xaKind(row)));
  dl.append(mk('dt', null, T('gui.agent.sec_last_test')), mk('dd', null, xaTestLine(row)));
  s1.appendChild(dl);
  if (row.last_test_detail && !String(st.text || '').includes(row.last_test_detail)) {
    s1.appendChild(mk('div', 'pnote', row.last_test_detail));
  }
  if (row.upgrade_to) {
    const up = mk('div', 'keyrow');
    up.appendChild(mk('span', 'pnote', T('gui.agent.upgrade', { to: xaKind({ kind: row.upgrade_to }) })));
    const go = mk('button', 'mini', T('gui.agent.upgrade_do'));
    go.onclick = () => confirmAsk(T('gui.agent.upgrade_do'),
      T('gui.agent.upgrade_body', { name: row.name }), T('gui.agent.upgrade_do'),
      () => xaRun('upgrade', row));
    up.appendChild(go);
    s1.appendChild(up);
  }
  b.appendChild(s1);

  const s2 = mk('div', 'pmsec');
  s2.appendChild(mk('span', 'cap', T('gui.agent.sec_config')));
  const form = mk('div', 'pform');
  const nameIn = mk('input'); nameIn.type = 'text'; nameIn.value = row.name;
  const descIn = mk('textarea'); descIn.rows = 2; descIn.value = row.description || '';
  const keyIn = mk('input'); keyIn.type = 'password'; keyIn.autocomplete = 'off';
  keyIn.placeholder = row.has_api_key ? T('gui.agent.key_set') : '';
  const field = (labelKey, hintKey, input) => {
    const f = mk('div', 'agf');
    f.append(mk('label', null, T(labelKey)), input, mk('div', 'hint', T(hintKey)));
    return f;
  };
  form.appendChild(field('gui.agent.name', 'gui.agent.name_hint', nameIn));
  form.appendChild(field('gui.agent.desc', 'gui.agent.desc_hint', descIn));
  if (row.kind === 'openai') form.appendChild(field('gui.agent.key', 'gui.agent.key_hint', keyIn));
  const ctl = mk('div', 'ctl');
  const save = mk('button', 'mini', T(row.configured ? 'gui.agent.save' : 'gui.agent.connect'));
  save.onclick = () => {
    const patch = { new_name: nameIn.value.trim(), description: descIn.value, api_key: keyIn.value };
    if (patch.new_name) xaSheet = patch.new_name;
    xaRun(row.configured ? 'update' : 'connect', row, patch);
  };
  ctl.appendChild(save);
  form.appendChild(ctl);
  s2.appendChild(form);
  b.appendChild(s2);

  if (row.configured) {
    const foot = mk('div', 'pmsec');
    const acts = mk('div', 'ctl');
    acts.style.justifyContent = 'flex-start';
    const tog = mk('button', 'mini ghost', T(row.enabled ? 'gui.agent.disable' : 'gui.agent.enable'));
    tog.onclick = () => xaRun('toggle', row, { enabled: !row.enabled });
    const rm = mk('button', 'mini ghost danger', T('gui.agent.disconnect'));
    rm.onclick = () => confirmAsk(T('gui.agent.disconnect'),
      T('gui.agent.disc_body', { name: row.name }), T('gui.agent.disconnect'),
      () => { closeDetail(); xaRun('remove', row); });
    acts.append(tog, rm);
    foot.appendChild(acts);
    b.appendChild(foot);
  }
}

function drawXa() {
  const box = $('#xaBody');
  if (!box) return;
  box.innerHTML = '';

  const hero = mk('div', 'pmhero');
  hero.append(mk('h3', null, T('gui.agent.hero')), mk('p', null, T('gui.agent.hero_sub')));
  box.appendChild(hero);

  const on = XAGENTS.filter((a) => a.configured);
  const off = XAGENTS.filter((a) => !a.configured);

  const s1 = mk('div', 'csec');
  const hd = mk('div', 'hd');
  hd.append(mk('b', null, T('gui.agent.on')), mk('span', 'n', String(on.length)));
  const probe = mk('button', 'mini ghost', T(xaProbing ? 'gui.agent.probing' : 'gui.agent.probe'));
  probe.disabled = xaProbing;
  probe.onclick = () => xaRun('probe');
  hd.appendChild(probe);
  s1.appendChild(hd);

  if (!on.length) {
    s1.appendChild(mk('div', 'empty-note', T('gui.agent.none')));
  } else {
    const set = mk('div', 'fset');
    on.forEach((row) => {
      const st = xaState(row);
      const r = mk('div', 'pcard');
      const nm = mk('div', 'nm');
      const led = mk('span', 'led' + (st.cls === 'ok' ? '' : ' ' + st.cls));
      nm.append(led, mk('span', null, row.name), mk('span', 'kd', xaKind(row)));
      r.append(nm);
      const mo = mk('div', 'mo' + (st.cls === 'ok' || st.cls === 'off' ? '' : ' ' + st.cls),
        [st.text, xaTestLine(row)].filter(Boolean).join(' · '));
      r.appendChild(mo);
      /* What the verdict actually proved. It differs by transport -- an acp test
         completes a handshake and reads the agent's own capability list, a cli
         test dispatches a real turn -- so "passed" on its own would flatten two
         quite different claims into one word. Skipped when the status line
         above already says the same sentence: a failed handshake used to print
         itself twice, once red and once grey. */
      if (row.last_test_detail && !String(st.text || '').includes(row.last_test_detail)) {
        const note = mk('div', 'pnote clamp', row.last_test_detail);
        note.onclick = () => note.classList.toggle('clamp');
        r.appendChild(note);
      }
      /* The preset moved to another transport since this entry was written.
         Rewriting it silently would change its command line and invalidate every
         session handle bound to it, so the switch is offered, never applied. */
      if (row.upgrade_to) {
        const up = mk('div', 'keyrow');
        up.appendChild(mk('span', 'pnote', T('gui.agent.upgrade', { to: xaKind({ kind: row.upgrade_to }) })));
        const go = mk('button', 'mini', T('gui.agent.upgrade_do'));
        go.onclick = () => confirmAsk(T('gui.agent.upgrade_do'),
          T('gui.agent.upgrade_body', { name: row.name }), T('gui.agent.upgrade_do'),
          () => xaRun('upgrade', row));
        up.appendChild(go);
        r.appendChild(up);
      }

      /* Two verbs on the row, everything else in the detail sheet: the list
         answers "which agents, are they alive"; disable, disconnect and the
         form live behind 配置. The whole row is a door to the same place. */
      const ctl = mk('div', 'ctl');
      if (row.test_running) {
        const stop = mk('button', 'mini ghost', T('gui.agent.test_cancel'));
        stop.onclick = (e) => { e.stopPropagation(); xaRun('test_cancel', row); };
        ctl.appendChild(stop);
      } else {
        const test = mk('button', 'mini ghost', T('gui.agent.test'));
        test.dataset.tip = T('gui.agent.test_spend', { name: row.name });
        test.onclick = (e) => { e.stopPropagation(); xaRun('test', row); };
        ctl.appendChild(test);
      }
      const cfg = mk('button', 'mini ghost', T('gui.agent.configure'));
      cfg.onclick = (e) => { e.stopPropagation(); xaSheetOpen(row); };
      ctl.appendChild(cfg);
      r.appendChild(ctl);
      r.setAttribute('role', 'button');
      r.tabIndex = 0;
      r.onclick = () => xaSheetOpen(row);
      r.onkeydown = (e) => { if (e.key === 'Enter') xaSheetOpen(row); };
      set.appendChild(r);
    });
    s1.appendChild(set);
  }
  box.appendChild(s1);

  const s2 = mk('div', 'csec');
  const hd2 = mk('div', 'hd');
  hd2.append(mk('b', null, T('gui.agent.off')), mk('span', 'n', String(off.length)));
  s2.appendChild(hd2);
  const g = mk('div', 'grid');
  off.forEach((row) => {
    const st = xaState(row);
    const card = mk('div', 'card');
    let th = 0;
    for (let i = 0; i < row.name.length; i++) th = (th * 31 + row.name.charCodeAt(i)) >>> 0;
    card.appendChild(mk('span', 'pmtile th' + (th % 8), row.name[0].toUpperCase()));
    const nm = mk('div', 'nm');
    nm.append(mk('span', null, row.name), mk('span', 'kd', xaKind(row)));
    card.append(nm);
    card.appendChild(mk('div', 'one', row.description || ''));
    if (st.text) card.appendChild(mk('div', 'st' + (st.cls === 'ok' || st.cls === 'off' ? '' : ' ' + st.cls), st.text));
    const ctl = mk('div', 'ctl');
    const b = mk('button', 'mini', T('gui.agent.connect'));
    b.onclick = (e) => {
      e.stopPropagation();
      /* An HTTP agent cannot answer without its key, so connecting it opens
         the sheet at the form rather than a one-click add: the alternative
         lands a disabled row and leaves the user hunting for why. */
      if (row.kind === 'openai') { xaSheetOpen(row); return; }
      xaRun('connect', row, {});
    };
    ctl.appendChild(b);
    card.appendChild(ctl);
    card.onclick = () => xaSheetOpen(row);
    g.appendChild(card);
  });
  s2.appendChild(g);
  box.appendChild(s2);
}

/* No rail badge: the maintainer's call is that the set-up-once modules do not
   nag from the rail -- their state is spoken inside the page, where the fix is. */
function drawXaBadge() {}

async function openXa() {
  showPage('xaPage');
  drawXa();
  try { XAGENTS = await DS.xa.load(true); } catch (e) { toast(T('gui.agent.failed', { detail: String(e) })); }
  drawXa();
  drawXaBadge();
}
function closeXa() { showPage(null); }

function openConn() { showPage('connPage'); connEdit = null; drawConn(); }
function closeConn() { showPage(null); }

/* Which channel's credential form is unfolded, and the writer behind it. The
   demo's writer only refuses politely; live.js swaps in channels.configure. */
let connEdit = null;
let connApply = (c, patch, enable) => { nlSay(null); drawConn(); };

/* One form per channel, built from the fields its own schema declares (they
   ride on channels.status). Secrets never echo back: a set field shows a
   placeholder, and a box left blank means "keep", never "erase".

   Only the required fields show. The schema declares up to a dozen knobs per
   channel and the old form drew every one of them, which made "connect Slack"
   look like filling in a tax return -- when all it takes is the two tokens.
   Everything optional folds behind one line, closed, with its count. */
function connForm(c, onDone) {
  const box = mk('div', 'pform');
  const inputs = new Map();
  const fieldRow = (f) => {
    const w = mk('div', 'agf');
    /* The human sentence is the label; the config key is the fine print.
       Key-as-label asked the user to read `app_secret` and guess, while the
       words that explain it hid in a hover. The catalogue speaks first so the
       label follows the reader's language; the schema's own description backs
       it up, and the raw key is the floor, never the face. */
    const human = T('gui.connf.' + f.key, null, f.label && f.label !== f.key ? f.label : f.key);
    const lb = mk('label', null, human);
    lb.title = human;
    w.appendChild(lb);
    const i = mk('input');
    i.type = f.secret ? 'password' : 'text';
    i.autocomplete = 'off';
    i.placeholder = f.set ? T('gui.conn.field_set') : '';
    w.appendChild(i);
    if (human !== f.key) w.appendChild(mk('div', 'hint mono', f.key));
    inputs.set(f.key, i);
    return w;
  };
  const required = (c.fields || []).filter((f) => f.required);
  const optional = (c.fields || []).filter((f) => !f.required);
  required.forEach((f) => box.appendChild(fieldRow(f)));
  if (optional.length) {
    const fold = mk('button', 'mini ghost fold', T('gui.conn.advanced', { n: optional.length }));
    fold.setAttribute('aria-expanded', 'false');
    const adv = mk('div', 'adv');
    adv.hidden = true;
    optional.forEach((f) => adv.appendChild(fieldRow(f)));
    fold.onclick = () => {
      adv.hidden = !adv.hidden;
      fold.setAttribute('aria-expanded', String(!adv.hidden));
    };
    box.append(fold, adv);
  }
  const ctl = mk('div', 'ctl');
  const save = mk('button', 'mini', T(c.on ? 'gui.agent.save' : 'gui.conn.connect'));
  save.onclick = () => {
    const patch = {};
    inputs.forEach((i, k) => { if (i.value.trim()) patch[k] = i.value.trim(); });
    /* Always "on". `enable` used to be `!c.on`, which read as a toggle: saving
       a correction to a connected channel turned it off. Disconnecting is its
       own control in the dialog now, so this one only ever connects. */
    const enable = true;
    connEdit = null;
    if (onDone) onDone();
    connApply(c, patch, enable);
  };
  const cancel = mk('button', 'mini ghost', T('gui.agent.cancel'));
  cancel.onclick = () => {
    connEdit = null;
    if (onDone) onDone(); else drawConn();
  };
  ctl.append(save, cancel);
  box.appendChild(ctl);
  return box;
}

/* The configuration dialog. The form used to unfold inside the card, which is
   why the page had to be a grid of tall cards in the first place: every tile
   reserved the height its form would need. Lifting the form into a dialog frees
   the page to be a list -- one line per entry, all twelve legible at once --
   and gives the form the width its fields actually want. */
function connDialog(c) {
  const veil = $('#connVeil');
  const body = $('#connDlgBody');
  $('#connDlgTitle').textContent = chanName(c);
  const gap = (c.missing || []).length > 0;
  /* Identity or nothing. The old subtitle recited what the channel is, which
     the reader knew before they clicked its name. */
  const sub = c.on && c.who ? T('gui.conn.as_you', { who: c.who }) : '';
  $('#connDlgSub').textContent = sub;
  $('#connDlgSub').hidden = !sub;
  body.innerHTML = '';
  connQrStop();
  /* Scanning is how these channels sign in, and it only exists while the
     adapter is up and unpaired -- so it sits above the form, where the reader
     is already looking, rather than behind a second click. */
  if (c.qrLogin && c.on) body.appendChild(connQrPanel(c));
  body.appendChild(connForm(c, () => { connCloseDialog(); }));
  if (c.on) {
    /* Disconnecting belongs with configuring, not on the row: the row is a
       list of twelve, and a destructive control repeated twelve times down a
       page is one mis-click waiting to happen. */
    const off = mk('button', 'mini ghost danger', T('gui.conn.disconnect'));
    off.onclick = () => {
      connCloseDialog();
      confirmAsk(T('gui.conn.disconnect'),
        T('gui.conn.disc_body', { name: chanName(c) }), T('gui.conn.disconnect'), () => {
          connApply(c, {}, false);
        });
    };
    const foot = mk('div', 'dngrow');
    foot.appendChild(off);
    body.appendChild(foot);
  }
  veil.dataset.open = 'true';
  const first = body.querySelector('input');
  if (first) first.focus();
  else if (gap) body.querySelector('button').focus();
}

function connCloseDialog() {
  $('#connVeil').dataset.open = 'false';
  connEdit = null;
  /* Every close path lands here -- Esc, the veil, the form's own cancel -- so
     this is the one place the scan poll can be stopped without leaving a timer
     running against a dialog nobody is looking at. */
  connQrStop();
}
$('#connVeil').onclick = (e) => { if (e.target === $('#connVeil')) connCloseDialog(); };

/* One row per entry, one state machine: an unconfigured entry offers 配置 and
   nothing else, a configured one carries the on/off switch, and clicking the
   row body opens its dialog either way. Two states, two controls -- a switch
   on an entry with no credentials would promise something the flip cannot
   deliver, which is the same rule the key-gated tool rows follow. */
/* The scan panel. `channels.qr` is a live read off the adapter, so it is polled
   while the dialog is open and stopped the moment it is not: the code rotates,
   and a poll left running after the dialog closed would keep a socket busy for
   a picture nobody is looking at. */
let connQrTimer = null;

function connQrStop() {
  if (connQrTimer) { clearInterval(connQrTimer); connQrTimer = null; }
}

function connQrPanel(c) {
  const box = mk('div', 'qrbox');
  const shot = mk('div', 'qrshot');
  const say = mk('div', 'qrsay', T('gui.conn.qr_wait'));
  box.append(shot, say);
  if (typeof rpc === 'undefined' || (typeof rpcHas === 'function' && !rpcHas('channels'))) return box;

  const paint = async () => {
    let r = null;
    try {
      r = await rpc.call('channels.qr', { name: c.id });
    } catch {
      return;  /* the connection banner already covers an unreachable gateway */
    }
    if (!r) return;
    if (r.connected) {
      connQrStop();
      shot.innerHTML = '';
      say.textContent = T('gui.conn.qr_done');
      say.className = 'qrsay ok';
      loadChannels().then(drawConn).catch(() => {});
      return;
    }
    if (r.qr) {
      const img = mk('img');
      img.src = r.qr;
      img.alt = T('gui.conn.qr_alt');
      shot.innerHTML = '';
      shot.appendChild(img);
      say.textContent = T('gui.conn.qr_scan');
      return;
    }
    /* A payload with no picture: the server could not rasterise it. Say which
       install is missing rather than showing an empty frame -- the reader
       cannot scan a URL, and has no way to guess why the box is blank. */
    shot.innerHTML = '';
    say.textContent = r.qr_text ? T('gui.conn.qr_noenc') : T('gui.conn.qr_wait');
  };
  paint();
  connQrTimer = setInterval(paint, 3000);
  return box;
}

/* What this row may honestly claim. The config flag alone used to drive the
   dot, so a channel whose adapter never came up still read as connected -- the
   flag says what was asked for, not what happened. `running` and `connected`
   come from the live gateway, and `undefined` means nobody could be asked,
   which is its own answer and not a negative one. */
function connState(c) {
  if (!c.on) return 'off';
  if (c.running === undefined || c.running === null) return 'unknown';
  if (!c.running) return 'down';
  /* Null for every channel that does not report a pairing: running is all it
     claims and all this may draw. */
  if (c.connected === false) return 'unpaired';
  return 'live';
}

function connStateText(state) {
  if (state === 'down') return T('gui.conn.st_down');
  if (state === 'unpaired') return T('gui.conn.st_unpaired');
  if (state === 'unknown') return T('gui.conn.st_unknown');
  return '';
}

function connRow(c) {
  /* Configured means the schema's required fields are all set. Entries whose
     schema declares no required fields count as configured out of the box. */
  const configured = (c.fields || []).length ? (c.missing || []).length === 0 : true;
  const r = mk('div', 'chrow');
  r.dataset.on = String(!!c.on);
  let th = 0;
  const cn = chanName(c);
  for (let i = 0; i < cn.length; i++) th = (th * 31 + cn.charCodeAt(i)) >>> 0;
  r.appendChild(mk('span', 'pmtile th' + (th % 8), cn[0]));
  const mid = mk('div', 'bd');
  const nm = mk('div', 'nm');
  const live = connState(c);
  if (live !== 'off') nm.appendChild(mk('span', 'led' + (live === 'live' ? '' : live === 'unknown' ? ' bad' : ' warn')));
  nm.appendChild(mk('span', 'tt', cn));
  mid.appendChild(nm);
  /* The sub line answers one question -- can this receive messages, and as
     whom -- or stays empty. It no longer introduces the channel to its user. */
  const sub = !configured
    ? T('gui.conn.unset')
    : live === 'live' && c.who
      ? T('gui.conn.as_you', { who: c.who })
      : connStateText(live);
  if (sub) mid.appendChild(mk('div', 'sub' + (configured && live !== 'unknown' ? '' : ' warn'), sub));
  r.appendChild(mid);
  const open = () => { connEdit = c.id; connDialog(c); };
  if (configured) {
    /* Editing stays reachable after connecting -- a rotated secret has to go
       somewhere. Ghost, so the switch stays the row's loudest control. */
    const b = mk('button', 'mini ghost', T('gui.conn.configure'));
    b.onclick = (e) => { e.stopPropagation(); open(); };
    r.appendChild(b);
    const s = mk('button', 'swi');
    s.setAttribute('role', 'switch');
    s.setAttribute('aria-checked', String(!!c.on));
    s.setAttribute('aria-label', cn);
    s.onclick = (e) => {
      e.stopPropagation();
      c.on = !c.on;
      s.setAttribute('aria-checked', String(!!c.on));
      drawConn();
    };
    r.appendChild(s);
  } else {
    const b = mk('button', 'mini', T('gui.conn.configure'));
    b.onclick = (e) => { e.stopPropagation(); open(); };
    r.appendChild(b);
  }
  /* The whole row is the target, not just the control -- a list of twelve
     reads as a list of twelve things to click, and hitting the label works. */
  r.setAttribute('role', 'button');
  r.tabIndex = 0;
  r.onclick = open;
  r.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open(); } };
  return r;
}

/* Flat, catalogue order. The connected/available split spent two headers and
   a count to say what each row's own switch already says. */
function drawConn() {
  const box = $('#connBody'); box.innerHTML = '';
  /* The strip at the top of every page hides its own h2 (the hero below says it
     bigger), and this was the one page with no hero to say it -- so it opened
     with a bare list and no name on it. */
  const hero = mk('div', 'pmhero');
  hero.append(mk('h3', null, T('gui.page.conn')), mk('p', null, T('gui.conn.hero_sub')));
  box.appendChild(hero);
  const list = mk('div', 'clist');
  CHANNELS.forEach((c) => list.appendChild(connRow(c)));
  box.appendChild(list);
}

/* The rail badge for capabilities that need attention. The composer used to
   carry a chip counting active capabilities too; it is gone -- how many tools
   are wired is a setup question, answered on the extensions page, not something
   to read while typing. What belongs under the field is the state of THIS
   session: how much context is left, and what the agent may do unasked. */
/* Same call as drawXaBadge: no counters on the rail's module rows. */
function drawCapsBadge() {}

/* ---- context meter ------------------------------------------------
   Filled from the turn's own usage (message.complete carries context_used /
   context_max); hidden until a real window is known, since a ring drawn from a
   guessed denominator is worse than no ring. */
const CTX = { used: 0, max: 0, est: false };
const RING = 47.75;

function drawCtx() {
  const chip = $('#ctxChip');
  if (!chip) return;
  chip.hidden = !CTX.max;
  if (!CTX.max) return;
  const pct = Math.min(100, Math.max(0, Math.round((100 * CTX.used) / CTX.max)));
  chip.classList.toggle('warm', pct >= 70 && pct < 90);
  chip.classList.toggle('hot', pct >= 90);
  const fg = chip.querySelector('.fg');
  if (fg) fg.setAttribute('stroke-dashoffset', String(RING * (1 - pct / 100)));
  /* A resumed session's fill is counted from the stored transcript rather than
     reported by the provider, so it says so: a tilde is cheaper than a number
     that quietly pretends to be measured. */
  const tip = T('gui.ctx.tip', {
    used: fmtTokens(CTX.used),
    max: fmtTokens(CTX.max),
    pct: String(pct),
  });
  chip.dataset.tip = tip;
  chip.setAttribute('aria-label', tip);
}

const fmtTokens = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n));

function setCtx(used, max, estimated) {
  if (typeof max === 'number' && max > 0) CTX.max = max;
  if (typeof used === 'number' && used >= 0) CTX.used = used;
  CTX.est = !!estimated;
  drawCtx();
}

/* ---- permission mode ----------------------------------------------
   Three tiers, ordered from the strictest to the one with no brakes, because
   that is the order a reader should meet them in. Front end only for now:
   nothing in the engine reads the choice yet, and the panel says so rather
   than letting the row imply a guarantee it cannot keep. The default is the
   behaviour Raven actually has today -- full access -- and it is the one tier
   drawn in the warning colour, since that is a fact about it, not decoration. */
const PERMS = [
  { id: 'ask', label: 'gui.perm.ask', sub: 'gui.perm.ask_h' },
  { id: 'smart', label: 'gui.perm.smart', sub: 'gui.perm.smart_h' },
  { id: 'full', label: 'gui.perm.full', sub: 'gui.perm.full_h', risk: true },
];
let perm = 'full';
try { perm = localStorage.getItem('raven.perm') || 'full'; } catch { perm = 'full'; }
if (!PERMS.some((p) => p.id === perm)) perm = 'full';

const PERM_ICO = {
  ask: '<path d="M12 3.5 19 6v5.5c0 4-2.9 7.4-7 9-4.1-1.6-7-5-7-9V6l7-2.5Z"/><path d="M9.6 10.2a2.4 2.4 0 1 1 3.3 2.2v1.1"/><path d="M12 16h.01"/>',
  smart: '<path d="M12 3.5 19 6v5.5c0 4-2.9 7.4-7 9-4.1-1.6-7-5-7-9V6l7-2.5Z"/><path d="M9 12.2l2 2 4-4.4"/>',
  full: '<path d="M12 3.5 19 6v5.5c0 4-2.9 7.4-7 9-4.1-1.6-7-5-7-9V6l7-2.5Z"/><path d="M12 8.6v3.6M12 15.2h.01"/>',
};

function drawPerm() {
  const cur = PERMS.find((p) => p.id === perm) || PERMS[0];
  $('#permName').textContent = T(cur.label);
  const chip = $('#permChip');
  /* No hover label: the chip already reads its own state, and the detail of
     each tier belongs in the panel the click opens. */
  delete chip.dataset.tip;
  chip.classList.toggle('risk', !!cur.risk);
  const ic = chip.querySelector('.pico');
  if (ic) ic.innerHTML = PERM_ICO[cur.id] || PERM_ICO.full;
  chip.setAttribute('aria-label', `${T('gui.perm.title')}: ${T(cur.label)}`);
}

function openPermPop() {
  const box = $('#permList'); box.innerHTML = '';
  PERMS.forEach((p) => {
    const r = mk('button', 'prow' + (p.risk ? ' risk' : ''));
    r.setAttribute('role', 'radio');
    r.setAttribute('aria-checked', String(p.id === perm));
    const g = mk('span', 'pic');
    g.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${PERM_ICO[p.id]}</svg>`;
    const txt = mk('span', 'txt');
    txt.append(mk('span', 'nm', T(p.label)), mk('span', 'sub', T(p.sub)));
    r.append(g, txt);
    if (p.id === perm) r.appendChild(ico(ACT_ICO.check, 'tick'));
    r.onclick = () => {
      perm = p.id;
      try { localStorage.setItem('raven.perm', perm); } catch { /* private mode */ }
      drawPerm(); closePermPop();
    };
    box.appendChild(r);
  });
  const pop = $('#permPop');
  /* Off the CHIP itself, not off the composer card: the card's static anchor
     left the panel hanging the field's whole height above the button it came
     from. Fixed coordinates measured from the chip put its bottom edge right
     on the control, wherever the composer happens to sit -- and the pop has to
     leave the card's DOM for that, because the card's entrance animation makes
     it a containing block that quietly re-bases position:fixed. */
  if (pop.parentElement !== document.body) document.body.appendChild(pop);
  pop.dataset.open = 'true';
  /* clientWidth/Height, not innerWidth/Height: a backgrounded tab reports the
     window as 0x0, and a panel placed from that lands in a corner. */
  const vw = document.documentElement.clientWidth;
  const chip = $('#permChip').getBoundingClientRect();
  const r = pop.getBoundingClientRect();
  pop.style.position = 'fixed';
  pop.style.left = Math.max(8, Math.min(vw - r.width - 8, chip.left - 8)) + 'px';
  pop.style.right = 'auto';
  pop.style.top = Math.max(8, chip.top - r.height - 6) + 'px';
  pop.style.bottom = 'auto';
  /* Above the dock and everything mounted on it: a mode picker the user just
     opened loses to nothing that was already on screen. */
  pop.style.zIndex = 46;
  $('#permChip').setAttribute('aria-expanded', 'true');
}

function closePermPop() {
  $('#permPop').dataset.open = 'false';
  $('#permChip').setAttribute('aria-expanded', 'false');
}

