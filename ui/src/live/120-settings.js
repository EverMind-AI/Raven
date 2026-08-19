/* -- settings --------------------------------------------------------- */
/* No standing notice banners: a config gap belongs in the settings page, not
   as a strip above every conversation. */
drawBanner = function () {
  $('#bannerHost').innerHTML = '';
};

/* The config settings.get returned, kept so the pages can display real values.
   Keys arrive camelCased (agents.defaults.reasoningEffort), one level per dot. */
let RAW = {};
V = (path, fallback) => {
  const v = String(path).split('.').reduce((o, k) => (o == null ? o : o[k]), RAW);
  return v == null || v === '' ? fallback : v;
};
/* Like V but null is a value: memory.backend stores null for "plugin memory
   off", and painting the schema default over it would misreport the choice. */
Vnull = (path, fallback) => {
  let node = RAW;
  for (const k of String(path).split('.')) {
    if (node == null || typeof node !== 'object' || !(k in node)) return fallback;
    node = node[k];
  }
  return node;
};

/* The write half of the settings dialog: one whitelisted dotted key per
   control. Reload-then-redraw keeps every V() read honest after a write. */
settingsWrite = (key, value, el) => rpc.call('settings.set', { key, value })
  .then(() => loadSettings())
  .then(() => { if (setIsOpen()) drawSettings(); toast(T('gui.set.saved')); })
  .catch((e) => {
    toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e }));
    if (setIsOpen()) drawSettings();
  });

/* EverOS model roles: read on settings open, written per section. A null
   fields object means "clear the section" (optional roles only). */
everosWrite = (section, fields, el) => {
  const p = fields ? { section, fields } : { section, clear: true };
  return rpc.call('settings.everosSet', p)
    .then(() => loadEveros())
    .then(() => {
      memEdit = null;
      if (setIsOpen()) drawSettings();
      toast(T('gui.set.mem.saved'));
    })
    .catch((e) => {
      toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e }));
      if (setIsOpen()) drawSettings();
    });
};

let usageBusy = false;
let usageAt = 0;
usageLoad = () => {
  /* The page asks on every redraw and a fresh reply causes one, so without the
     floor the two would spin. It also bounds how often the tab polls itself. */
  if (usageBusy || Date.now() - usageAt < 3000) return;
  usageBusy = true;
  rpc.call('settings.usage', {})
    .then((r) => { USAGE = r; })
    /* A failed refresh keeps the numbers it already has: zeroing them would
       report "no usage" for what is really a dropped call. */
    .catch(() => {
      if (!USAGE) USAGE = { days: 30, llm: { total: { calls: 0, input_tokens: 0, output_tokens: 0, cost_usd: 0 }, models: [] }, tools: { total: 0, counts: [] } };
    })
    .then(() => {
      usageBusy = false; usageAt = Date.now();
      if (setIsOpen() && sTab === 'usage') drawSettings();
    });
};
/* Counters that only move when the dialog is reopened read as broken. The page
   keeps itself current for as long as it is the one on screen. */
setInterval(() => { if (setIsOpen() && sTab === 'usage') usageLoad(); }, 15000);

async function loadEveros() {
  try {
    EVEROS = await rpc.call('settings.everos', {});
  } catch { /* section rows render as unset; writes still surface their error */ }
}

/* config.set's whitelisted keys are readable only through config.get;
   settings.get reports the effective agent defaults instead, which are a
   different number (agents.defaults.temperature 0.1 vs agent.temperature 1).
   These three are display-only -- see the note on `wire` below. */
const HOT_KEYS = ['agent.temperature', 'agent.thinking_budget', 'tui.show_token_usage'];

async function loadSettings() {
  const r = await rpc.call('settings.get', {});
  const raw = r.settings || {};
  RAW = raw;
  CONFIG_PATH = r.config_path || CONFIG_PATH;
  drawBanner();
  const defaults = (raw.agents && raw.agents.defaults) || {};
  if (!CFG._live) {
    CFG._live = true;
    let tempVal = CFG.temp, budgetVal = CFG.budget;
    /* Read-only on purpose. config.set takes both keys and then writes a
       top-level `agent` section that the config schema forbids, so the next
       load of config.json fails and every config-reading RPC goes down with
       it. Until the writer maps them onto agents.defaults, the settings page
       shows the value and refuses the write. */
    const wire = (prop, get, set) => Object.defineProperty(CFG, prop, { get, set });
    wire('temp', () => tempVal, (v) => { tempVal = v; });
    wire('budget', () => budgetVal, (v) => { budgetVal = v; });
    CFG._setRaw = (t, b) => { if (typeof t === 'number') tempVal = t; if (typeof b === 'number') budgetVal = b; };
  }
  if (defaults.workspace) CFG.cwd = defaults.workspace;
  CFG.endpoint = r.config_path;
  try {
    const w = await rpc.call('config.get', { keys: HOT_KEYS });
    const c = (w && w.config) || {};
    CFG._setRaw(c['agent.temperature'], c['agent.thinking_budget']);
    if (typeof c['tui.show_token_usage'] === 'boolean') SHOW_TOKENS = c['tui.show_token_usage'];
  } catch { /* leave the sliders where they are rather than show a guess */ }
  if (defaults.model) { model = defaults.model; setModelLabel(); }
  try { await loadProviders(); } catch { /* model options unavailable — keep demo providers */ }
}

let curProvider = '';

async function loadProviders() {
  const mo = await rpc.call('model.options', {});
  PROVIDERS.length = 0;
  (mo.providers || []).forEach((p) => PROVIDERS.push({
    id: p.slug, name: p.name, models: p.models || [], on: p.authenticated,
    kind: p.auth_type || 'api_key', needsBase: !!p.needs_api_base,
    env: p.key_env || '', warn: p.warning || '',
    key: p.authenticated ? '已配置' : '',
  }));
  curProvider = mo.provider || '';
  if (mo.model) { model = mo.model; setModelLabel(); }
}

openSettings = async function () {
  /* loadExt too: the agent's tool inventory is a settings page now, and a
     panel drawn from the demo list would flip switches that do not exist. */
  try { await Promise.all([loadSettings(), loadExt(), loadEveros()]); } catch (e) { toast(`加载失败：${e.message || e}`); }
  drawSettings(); openSet();
};

/* -- language ---------------------------------------------------------
   One key, both front ends: config.language also drives the TUI (which
   polls it) and the language the agent replies in. */
applyLang = async function (next, { persist } = {}) {
  if (next === LANG) return;
  const prev = LANG;
  LANG = next;
  applyI18n();
  redrawAll();
  if (!persist) return;
  try {
    await rpc.call('config.set', { key: 'language', value: next });
  } catch (e) {
    LANG = prev; applyI18n(); redrawAll();
    toast(`切换语言失败：${(e.data && e.data.detail) || e.message || e}`);
  }
};

/* Everything the catalogue reaches that is drawn rather than written in
   the markup. Cheap enough to run wholesale on a language flip. */
function redrawAll() {
  drawList();
  drawFoot();
  drawCapsBadge();
  setModelLabel();
  drawPerm();
  drawCtx();
  // Every module page, not just the open one: a hidden page keeps its old
  // DOM, so it would still be in the previous language when reopened.
  drawSettings();
  try { drawCaps(); } catch { /* extensions not loaded yet */ }
  try { drawConn(); } catch { /* channels not loaded yet */ }
  try { drawCron(); } catch { /* schedules not loaded yet */ }
  try { drawXa(); } catch { /* agents not loaded yet */ }
  try { drawMem(); } catch { /* memory not loaded yet */ }
  // The More rows are redrawn on each open, so only a group standing open at
  // the moment of the flip keeps the old names.
  drawMoreFly();
  /* The shared drawer is closed rather than redrawn: it is not on any page, so
     nothing above reaches it, and every one of its five openers would have to
     hand back the subject it was drawn from. Left open it would sit in the old
     language over a page now in the new one, which reads worse than losing the
     place -- and only the settings dialog, which the flip is made from, is
     above it. */
  closeDetail();
  const p = $('#stage').querySelector('.pitch');
  if (p) { p.remove(); pitch(); }
  /* The transcript writes its words into the DOM as it renders -- fold headers,
     work phrases, answer footers -- so a flip has to rebuild it, not just
     re-run the catalogue over the markup. Skipped while a turn is streaming:
     re-opening the session mid-turn would cut the stream off. */
  if (!draft && cur && !busy) openSession(sess(cur));
}

async function loadLang() {
  try {
    const r = await rpc.call('config.get', { keys: ['language'] });
    const v = r && r.config && r.config.language;
    if (v === 'en' || v === 'zh') { LANG = v; applyI18n(); redrawAll(); }
  } catch { /* stay on the built-in default */ }
}

/* A model id is provider-qualified (openrouter/anthropic/claude-opus-4.6);
   the chip only has room for the part that identifies the model. */
const shortModel = (m) => String(m || '').split('/').pop();
const setModelLabel = () => { $('#modelName').textContent = shortModel(model); $('#modelChip').title = model; };

/* Anchored to the composer chip by default; the settings panel passes its own
   button and a callback so both places pick a model the same way. */
function openModelPicker(anchor, after) {
  document.querySelectorAll('.mpick').forEach((n) => n.remove());
  const host = anchor || $('#modelChip');
  const authed = PROVIDERS.filter((p) => p.on && p.models.length);
  if (!authed.length) { toast(T('gui.picker.no_account')); return; }

  const pick = mk('div', 'mpick');
  pick.setAttribute('role', 'dialog');
  let provIdx = Math.max(0, authed.findIndex((p) => p.models.includes(model)));
  let query = '';

  const find = mk('div', 'find');
  find.appendChild(mk('span', null, '⌕')).style.cssText = 'color:var(--faint)';
  const q = mk('input');
  q.placeholder = T('gui.picker.search_ph');
  find.appendChild(q);
  pick.appendChild(find);

  const cols = mk('div', 'cols');
  const provs = mk('div', 'provs');
  const models = mk('div', 'models');
  cols.append(provs, models);
  pick.appendChild(cols);

  if (!anchor) {
    const foot = mk('div', 'foot');
    const manage = mk('button', null, T('gui.picker.manage'));
    manage.onclick = () => { close(); openSettings(); };
    foot.appendChild(manage);
    pick.appendChild(foot);
  }

  const choose = (m) => {
    close();
    const prev = model;
    model = m; setModelLabel();
    if (after) after();
    rpc.call('config.set', { key: 'model', value: m })
      .then(() => toast(`已切换到 ${shortModel(m)}`))
      .catch((e) => {
        model = prev; setModelLabel();
        if (after) after();
        toast(`切换失败：${(e.data && e.data.detail) || e.message || e}`);
      });
  };

  const modelRow = (m, subtitle) => {
    const b = mk('button', 'row');
    const nm = mk('span', 'nm', shortModel(m));
    b.appendChild(nm);
    if (subtitle) b.appendChild(mk('span', 'sub', subtitle));
    if (m === model) b.appendChild(mk('span', 'tick', '✓'));
    b.onclick = () => choose(m);
    return b;
  };

  function render() {
    provs.innerHTML = '';
    models.innerHTML = '';
    // Search narrows each provider's list in place: the provider column stays,
    // its count turns into a hit count, and empty providers dim out.
    const hits = authed.map((p) => (query
      ? p.models.filter((m) => shortModel(m).toLowerCase().includes(query))
      : p.models));
    if (query && !hits[provIdx].length) {
      const first = hits.findIndex((h) => h.length);
      if (first >= 0) provIdx = first;
    }
    authed.forEach((p, i) => {
      const b = mk('button', 'row' + (hits[i].length ? '' : ' dim'));
      b.setAttribute('aria-selected', String(i === provIdx && hits[i].length > 0));
      b.append(mk('span', 'nm', p.name), mk('span', 'ct', String(hits[i].length)));
      if (hits[i].includes(model)) b.appendChild(mk('span', 'tick', '•'));
      b.onclick = () => { if (!hits[i].length) return; provIdx = i; render(); };
      provs.appendChild(b);
    });
    const list = hits[provIdx] || [];
    if (!list.length) {
      models.appendChild(mk('div', 'empty', query ? T('gui.picker.no_match') : T('gui.picker.empty_provider')));
      return;
    }
    list.forEach((m) => models.appendChild(modelRow(m)));
    const active = models.querySelector('.tick');
    if (active) active.parentElement.scrollIntoView({ block: 'nearest' });
  }

  q.oninput = () => { query = q.value.trim().toLowerCase(); render(); };
  q.onkeydown = (e) => {
    e.stopPropagation();
    if (composing(e)) return;
    if (e.key === 'Escape') { e.preventDefault(); close(); }
    if (e.key === 'Enter') {
      const first = models.querySelector('.row');
      if (first) first.click();
    }
  };

  const onDown = (e) => { if (!e.target.closest('.mpick') && !host.contains(e.target)) close(); };
  function close() {
    document.removeEventListener('pointerdown', onDown, true);
    pick.remove();
  }

  render();
  document.body.appendChild(pick);
  // documentElement metrics, not window.innerWidth: the latter reads 0 inside
  // some embedded webviews and would push the popover into the corner.
  const vw = document.documentElement.clientWidth;
  const vh = document.documentElement.clientHeight;
  const r = host.getBoundingClientRect();
  const box = pick.getBoundingClientRect();
  const above = r.top - box.height - 8;
  pick.style.left = `${Math.max(12, Math.min(r.left, vw - box.width - 12))}px`;
  pick.style.top = `${above >= 12 ? above : Math.min(r.bottom + 8, Math.max(12, vh - box.height - 12))}px`;
  document.addEventListener('pointerdown', onDown, true);
  q.focus();
}

$('#modelChip').onclick = () => openModelPicker();

/* -- settings page: model & accounts ---------------------------------
   Replaces the demo panel wholesale. Every control here writes through a
   model.* method, so what the page shows is what is on disk. */
const kindLabel = (kind) => T('gui.model.kind.' + (kind || 'key'), null, T('gui.model.kind.key'));
const loginCmd = (slug) => `raven provider login ${String(slug).replace(/_/g, '-')}`;
const OFF_HEAD = 5;

let provOpen = null;
let provAll = false;
let provErr = '';
let provBusy = false;
let provFocus = false;

async function provWrite(fn) {
  if (provBusy) return;
  provBusy = true; provErr = '';
  try {
    await fn();
    await loadProviders();
  } catch (e) {
    provErr = (e.data && e.data.detail) || e.message || String(e);
  }
  provBusy = false;
  drawSettings();
}

function modelChips(pv) {
  const box = mk('div');
  const head = mk('div', 'pnote', T('gui.model.count_pick', { n: pv.models.length }));
  box.appendChild(head);
  if (!pv.models.length) return box;
  const chips = mk('div', 'chips');
  pv.models.slice(0, 10).forEach((m) => {
    const c = mk('span', 'chipm');
    c.appendChild(mk('span', null, shortModel(m)));
    const x = mk('button', null, '✕');
    x.title = T('gui.model.remove');
    x.setAttribute('aria-label', `${T('gui.model.remove')} ${m}`);
    x.onclick = () => provWrite(() => rpc.call('model.remove_model', { slug: pv.id, model: m }));
    c.appendChild(x);
    chips.appendChild(c);
  });
  if (pv.models.length > 10) chips.appendChild(mk('span', 'pnote', T('gui.model.count_more', { n: pv.models.length - 10 })));
  box.appendChild(chips);
  return box;
}

function provForm(pv) {
  const f = mk('div', 'pform');

  if (pv.kind === 'oauth') {
    f.appendChild(mk('div', 'pnote', T('gui.model.oauth_hint', { name: pv.name })));
    const cmd = mk('div', 'cmd');
    cmd.appendChild(mk('code', null, loginCmd(pv.id)));
    const cp = mk('button', 'mini ghost', T('gui.model.copy'));
    cp.onclick = () => {
      navigator.clipboard && navigator.clipboard.writeText(loginCmd(pv.id));
      cp.textContent = T('gui.model.copied');
      setTimeout(() => { cp.textContent = T('gui.model.copy'); }, 1200);
    };
    cmd.appendChild(cp);
    f.appendChild(cmd);
  } else {
    const base = mk('input');
    base.type = 'text';
    base.placeholder = pv.kind === 'local' ? T('gui.model.base_ph_local') : T('gui.model.base_ph');
    const key = mk('input');
    key.type = 'password';
    key.placeholder = pv.on ? T('gui.model.key_ph_update') : T('gui.model.key_ph', { name: pv.name });
    key.setAttribute('aria-label', `${pv.name} API Key`);

    const save = mk('button', 'mini', pv.on ? T('gui.model.update') : T('gui.model.connect'));
    save.onclick = () => {
      const params = { slug: pv.id };
      if (pv.kind !== 'local') params.api_key = key.value.trim();
      if (base.value.trim()) params.api_base = base.value.trim();
      if (pv.kind === 'local' && !params.api_base) { provErr = T('gui.model.need_base'); drawSettings(); return; }
      if (pv.kind !== 'local' && !params.api_key) { provErr = T('gui.model.need_key'); drawSettings(); return; }
      provWrite(() => rpc.call('model.save_key', params));
    };

    if (pv.needsBase || pv.kind === 'endpoint') {
      const r0 = mk('div', 'keyrow'); r0.appendChild(base); f.appendChild(r0);
    }
    const r1 = mk('div', 'keyrow');
    if (pv.kind !== 'local') r1.appendChild(key);
    r1.appendChild(save);
    f.appendChild(r1);
    if (pv.env) f.appendChild(mk('div', 'pnote', T('gui.model.env_hint', { env: pv.env })));
  }

  const add = mk('div', 'keyrow');
  const ai = mk('input');
  ai.type = 'text';
  ai.placeholder = T('gui.model.add_ph');
  const ab = mk('button', 'mini ghost', T('gui.add'));
  ab.onclick = () => {
    const v = ai.value.trim();
    if (!v) return;
    provWrite(() => rpc.call('model.add_model', { slug: pv.id, model: v }));
  };
  add.append(ai, ab);
  f.appendChild(add);
  f.appendChild(modelChips(pv));

  if (pv.on) {
    const cut = mk('button', 'mini ghost danger', T('gui.model.disconnect'));
    cut.style.justifySelf = 'start';
    cut.onclick = () => confirmAsk(T('gui.model.disconnect_title'),
      T('gui.model.disconnect_body', { name: pv.name }), T('gui.model.disconnect_title'),
      () => provWrite(() => rpc.call('model.disconnect', { slug: pv.id })));
    f.appendChild(cut);
  }
  if (provErr) f.appendChild(mk('div', 'perr', provErr));
  return f;
}

function provCard(pv) {
  const open = provOpen === pv.id;
  const c = mk('div', 'pcard' + (open ? ' open' : ''));
  const nm = mk('div', 'nm');
  nm.append(mk('span', 'led' + (pv.on ? '' : ' warn')), mk('span', null, pv.name));
  if (pv.id === curProvider) nm.appendChild(mk('span', 'tagm', T('gui.model.is_default')));
  c.appendChild(nm);
  const bits = [pv.on ? T('gui.model.state.connected') : kindLabel(pv.kind)];
  if (pv.models.length) bits.push(T('gui.model.count', { n: pv.models.length }));
  if (!pv.on && pv.kind === 'oauth') bits.push(T('gui.model.needs_login'));
  c.appendChild(mk('div', 'mo', bits.join(' · ')));
  const ctl = mk('div', 'ctl');
  const go = mk('button', 'mini' + (pv.on || open ? ' ghost' : ''),
    open ? T('gui.model.collapse') : (pv.on ? T('gui.model.manage') : T('gui.model.connect')));
  go.setAttribute('aria-expanded', String(open));
  go.onclick = () => { provOpen = open ? null : pv.id; provErr = ''; provFocus = !open; drawSettings(); };
  ctl.appendChild(go);
  c.appendChild(ctl);
  if (open) c.appendChild(provForm(pv));
  return c;
}

function drawModelPanel() {
  const host = $('#spanels');
  // Expanding a card redraws the panel; keeping the scroll offset and skipping
  // the panel's entry animation makes that read as an expand, not a reload.
  const keep = host.scrollTop;
  host.innerHTML = '';
  const p = mk('div', 'panel'); p.dataset.on = 'true';
  p.style.animation = 'none';

  const home = PROVIDERS.find((x) => x.id === curProvider);
  const d = scard(p, T('gui.model.default'));
  const pick = mk('button', 'mini ghost pickm');
  pick.append(mk('span', 'mono', shortModel(model) || T('gui.model.unset')), mk('span', 'car', '⌄'));
  pick.onclick = () => openModelPicker(pick, drawSettings);
  d.appendChild(pick);

  const on = PROVIDERS.filter((x) => x.on);
  const off = PROVIDERS.filter((x) => !x.on);

  /* Connected first and in its own card: which providers are live is the one
     thing this panel is asked, and the long tail of unconnected ones must not
     be the first thing the eye lands on. */
  const cOn = scard(p, T('gui.model.connected', { n: on.length }));
  if (!on.length) {
    cOn.appendChild(mk('div', 'pnote', T('gui.model.none_connected')));
  } else {
    const s = mk('div', 'fset');
    on.forEach((pv) => s.appendChild(provCard(pv)));
    cOn.appendChild(s);
  }

  const cOff = scard(p, T('gui.model.others', { n: off.length }));
  const s2 = mk('div', 'fset');
  (provAll ? off : off.slice(0, OFF_HEAD)).forEach((pv) => s2.appendChild(provCard(pv)));
  cOff.appendChild(s2);
  if (off.length > OFF_HEAD) {
    const more = mk('button', 'mini ghost',
      provAll ? T('gui.model.collapse') : T('gui.model.expand_rest', { n: off.length - OFF_HEAD }));
    more.setAttribute('aria-expanded', String(provAll));
    more.onclick = () => { provAll = !provAll; drawSettings(); };
    cOff.appendChild(more);
  }

  host.appendChild(p);
  host.scrollTop = keep;
  if (provFocus) {
    provFocus = false;
    const card = p.querySelector('.pcard.open');
    if (card) {
      const field = card.querySelector('.pform input');
      if (field) field.focus();
      card.scrollIntoView({ block: 'nearest' });
    }
  }
}

{
  const origDrawSettings = drawSettings;
  drawSettings = function () {
    origDrawSettings();
    if (sTab !== 'model') return;
    drawModelPanel();
    const p = $('#spanels').querySelector('.panel');
    if (p) modelTuning(p);
  };
}

