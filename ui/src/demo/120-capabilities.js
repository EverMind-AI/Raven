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
function closeDetail() { $('#detail').dataset.open = 'false'; dCur = null; }

/* ══ module 2b: external agents ════════════════════════════════════
   The renderer is the xa island (ui/src/features/xa/); what remains here
   is its shell face -- the names the More row, the Esc handler and the
   live layer's redrawAll still call -- and the fixture source. The island
   owns everything drawn inside the body and its sheet in the shared
   detail drawer. */
function openXa() { RavenIslands.xa.open(); }
function closeXa() { RavenIslands.xa.close(); }
function drawXa() {
  /* A language flip re-renders #xaBody with the new catalogue. */
  RavenIslands.xa.redraw();
}

/* The fixture rows. A row is (name, kind, configured, enabled, probe_status,
   last test). Three facts, deliberately kept apart: `configured` is "Raven
   knows about it", `enabled` is "Raven may dispatch to it", and
   `probe_status` is "the machine can actually run it". Collapsing them is
   how a disabled agent reads as broken, or a missing binary reads as
   switched off. */
const XA_FIXTURE = [
  { name: 'research-raven', preset: null, kind: 'builtin', configured: false, builtin: true, enabled: true,
    probe_status: 'ready', probe_detail: '', has_api_key: false, test_running: false,
    last_test_ok: null, last_test_at_ms: null, last_test_detail: '', upgrade_to: null,
    description: 'Deep retrieval and fact-checking: multi-source search, source-credibility judgment.' },
  { name: 'code-raven', preset: null, kind: 'builtin', configured: false, builtin: true, enabled: true,
    probe_status: 'ready', probe_detail: '', has_api_key: false, test_running: false,
    last_test_ok: null, last_test_at_ms: null, last_test_detail: '', upgrade_to: null,
    description: "Repository-level code work: locate where a change lands, implement with unit tests." },
  { name: 'raven', preset: null, kind: 'builtin', configured: false, builtin: true, enabled: true,
    probe_status: 'ready', probe_detail: '', has_api_key: false, test_running: false,
    last_test_ok: null, last_test_at_ms: null, last_test_detail: '', upgrade_to: null,
    description: 'General-purpose sub-agent with no capability bias.' },
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

/* The rail badge for capabilities that need attention. The composer used to
   carry a chip counting active capabilities too; it is gone -- how many tools
   are wired is a setup question, answered on the extensions page, not something
   to read while typing. What belongs under the field is the state of THIS
   session: how much context is left, and what the agent may do unasked. */
/* No counters on the rail's module rows -- the set-up-once modules do not
   nag from there; their state is spoken inside the page, where the fix is. */
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
