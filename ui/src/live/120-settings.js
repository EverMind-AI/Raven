/* -- settings: the rpc source ------------------------------------------
   The island (ui/src/features/settings/) owns the dialog's drawing; this
   file speaks settings.* / model.* over /rpc and installs the source onto
   the seam. What stays beside it is shell plumbing that is not the page:
   the banner override, the language flip (redrawAll), and the composer's
   model chip with its picker popover. */

/* No standing notice banners: a config gap belongs in the settings page, not
   as a strip above every conversation. */
drawBanner = function () {
  $('#bannerHost').innerHTML = '';
};

/* The config settings.get returned. Keys arrive camelCased
   (agents.defaults.reasoningEffort), one level per dot. V serves the legacy
   rows that still read config through it (the capabilities tool rows); the
   island gets the same object inside its snapshot. */
let RAW = {};
V = (path, fallback) => {
  const v = String(path).split('.').reduce((o, k) => (o == null ? o : o[k]), RAW);
  return v == null || v === '' ? fallback : v;
};

let configPathLive = '~/.raven/config.json';
let everosLive = null;

/* The write half the legacy capabilities rows still use: one whitelisted
   dotted key per control. Reload-then-redraw keeps every V() read honest
   after a write. */
settingsWrite = (key, value, el) => rpc.call('settings.set', { key, value })
  .then(() => loadSettings())
  .then(() => { if (setIsOpen()) drawSettings(); toast(T('gui.set.saved')); })
  .catch((e) => {
    toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e }));
    if (setIsOpen()) drawSettings();
  });

async function loadEveros() {
  try {
    everosLive = await rpc.call('settings.everos', {});
  } catch { /* section rows render as unset; writes still surface their error */ }
}

async function loadSettings() {
  const r = await rpc.call('settings.get', {});
  RAW = r.settings || {};
  configPathLive = r.config_path || configPathLive;
  drawBanner();
  const defaults = (RAW.agents && RAW.agents.defaults) || {};
  if (defaults.workspace) CFG.cwd = defaults.workspace;
  CFG.endpoint = r.config_path;
  if (defaults.model) { model = defaults.model; setModelLabel(); }
  try { await loadProviders(); } catch { /* model options unavailable — keep the rows already shown */ }
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

const settingsSnapshot = () => ({
  raw: RAW, configPath: configPathLive, everos: everosLive,
  providers: PROVIDERS, curProvider, model,
});

const settingsErr = (e) => (e.data && e.data.detail) || e.message || e;

DS.settings = {
  load: async () => {
    await loadSettings();
    await loadEveros();
    return settingsSnapshot();
  },
  /* Toasts are spoken here, where the legacy wording lived; the thrown
     handled tag tells the island to only redraw. */
  set: async (key, value) => {
    try {
      await rpc.call('settings.set', { key, value });
      await loadSettings();
      toast(T('gui.set.saved'));
    } catch (e) {
      toast(T('gui.plug.op_failed', { err: settingsErr(e) }));
      throw { handled: true };
    }
    return settingsSnapshot();
  },
  /* A null fields object means "clear the section" (optional roles only). */
  everosSet: async (section, fields) => {
    const p = fields ? { section, fields } : { section, clear: true };
    try {
      await rpc.call('settings.everosSet', p);
      await loadEveros();
      toast(T('gui.set.mem.saved'));
    } catch (e) {
      toast(T('gui.plug.op_failed', { err: settingsErr(e) }));
      throw { handled: true };
    }
    return settingsSnapshot();
  },
  usage: () => rpc.call('settings.usage', {}),
  provider: async (op, params) => {
    await rpc.call('model.' + op, params);
    await loadProviders();
    return settingsSnapshot();
  },
  model: () => model,
  /* The composer's picker popover, offered to the island's default-model
     button so both places pick a model the same way. */
  pickModel: (anchor, after) => openModelPicker(anchor, after),
};

openSettings = async function () {
  /* loadExt too: the agent's tool inventory is a settings page, and a panel
     drawn from the demo list would flip switches that do not exist. */
  try { await loadExt(); } catch (e) { toast(`加载失败：${e.message || e}`); }
  await RavenIslands.settings.open();
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
  /* The transcript island re-renders its catalogue words (verbs, fold
     headers, footers) in place -- which is also what covers a turn still
     streaming, where the reload below must not run. */
  RavenIslands.transcript.redraw();
  /* The words baked into stored segments (note labels, phrased previews) come
     back right on a rebuild from disk. Skipped while a turn is streaming:
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

/* Anchored to the composer chip by default; the settings island passes its
   own button and a callback so both places pick a model the same way. */
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
