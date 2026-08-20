/* -- settings: the rpc source ------------------------------------------
   The island (ui/src/features/settings/) owns the dialog's drawing; this
   file speaks settings.* / model.* over /rpc and installs the source onto
   the seam. What stays beside it is shell plumbing that is not the page:
   the banner override, the language flip (redrawAll), and the composer's
   model chip with its picker popover. */

/* No websearch notice in live mode: a config gap belongs in the settings page,
   not as a strip above every conversation. Said through the source rather than
   by replacing drawBanner, which is what it used to do -- and replacing the
   drawing suppressed the OTHER notice too. A memory fault is not a config gap:
   it means the backend has stopped storing and has been handing back
   normal-looking replies the whole time, and live mode is the only mode where
   it can happen at all. Refusing one notice is a decision about that notice. */
DS.banner = {
  websearchNeeds: () => false,
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

/* The picker itself is an island (ui/src/features/model/). What stays here is
   the source it reads: the provider list this transport fetched, the page's
   current pick, and the two halves of writing one -- the local set the chip
   repaints from, and the rpc call that can reject. Splitting those two is what
   lets the picker be optimistic and still roll back. */
DS.model = {
  providers: () => PROVIDERS,
  current: () => model,
  setLocal: (m) => { model = m; setModelLabel(); },
  persist: (m) => rpc.call('config.set', { key: 'model', value: m }),
  openSettings: () => openSettings(),
};

$('#modelChip').onclick = () => openModelPicker();
