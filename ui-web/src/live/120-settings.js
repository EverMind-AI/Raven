/* -- settings: the rpc source ------------------------------------------
   The island (ui-web/src/features/settings/) owns the dialog's drawing; this
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
   (agents.defaults.reasoningEffort), one level per dot. Handed to the island
   inside its snapshot, which is now the only reader. */
let RAW = {};

let configPathLive = '~/.raven/config.json';
let everosLive = null;

/* The pick's write-back. In a conversation it reaches that conversation only
   (config.set under its session_id; the gate reads it live). A draft has no
   session_id to write under yet, so the pick is staged and applied to the
   session the first message mints -- the shape the model and tier chips take
   (`applyStagedPerm` in 080). The default is the settings panel's to change.
   Published here because this file owns the settings transport; the island
   calls it late-bound. */
window.persistPermMode = (m) => {
  const sid = sessionCurrent();
  if (!sid) { pendingPerm = m; return true; }
  return rpc.call('config.set', { key: 'permissions.mode', value: m, scope: 'session', session_id: sid })
    .then((r) => {
      if (r && r.applied) return true;
      toast(T('gui.perm.save_failed'));
      return false;
    })
    .catch(() => { toast(T('gui.perm.save_failed')); return false; });
};

/* Read by the send path, next to the staged model and tier. `pendingPerm` is
   declared beside them in 080, where all three are reset on the two paths
   that abandon a draft. */
function stagedPerm() { const p = pendingPerm; pendingPerm = null; return p; }

async function loadEveros() {
  try {
    everosLive = await rpc.call('settings.everos', {});
  } catch { /* section rows render as unset; writes still surface their error */ }
}

/* The permission chip mirrors what the gate reads for the visible conversation:
   its own mode when it has one, else the default. Asked of the server rather
   than lifted from the settings snapshot, which only knows the default; pushed
   into the island so the chip needs no transport of its own. Lives out here
   rather than inside loadSettings because scripts/model-refresh-live.test.mjs
   extracts that function's source and evaluates it with a supplied `deps` set,
   under node -- a browser global reached from inside it is a ReferenceError
   there, and the harness exists precisely to catch the live layer drifting. */
async function loadPermMode(sid, gen) {
  // The same ticket loadProviders carries, for the same race: two opens in a
  // row, the first answer landing last and repainting the one chip with the
  // mode of a conversation the reader has left.
  const ticket = gen !== undefined ? gen : viewGen;
  let r;
  try {
    r = await rpc.call('config.get', { keys: ['permissions.mode'], ...(sid ? { session_id: sid } : {}) });
  } catch {
    return;
  }
  if (ticket !== viewGen) return;
  window.setPermMode?.(((r && r.config) || {})['permissions.mode'] || 'ask');
}
const pushPermMode = () => loadPermMode(sessionCurrent());

async function loadSettings() {
  const r = await rpc.call('settings.get', {});
  RAW = r.settings || {};
  configPathLive = r.config_path || configPathLive;
  drawBanner();
  const defaults = (RAW.agents && RAW.agents.defaults) || {};
  // The configured default, kept apart from the visible session's model: the
  // settings default-model control shows and edits THIS pair (model AND
  // provider), while the composer chip shows whatever the open conversation
  // runs. Sharing one value made the settings control display the session's
  // model -- and badge the session's provider -- as the default.
  defaultModelLive = defaults.model || '';
  defaultProviderLive = defaults.provider || '';
  if (defaults.model) { modelSet(defaults.model); setModelLabel(); }
  try { await loadProviders(); } catch { /* model options unavailable — keep the rows already shown */ }
}

let providersLive = [];
let defaultModelLive = '';
let defaultProviderLive = '';

async function loadProviders(sid, gen) {
  // The model is per conversation, so ask for the visible one's -- model.options
  // stars the row that conversation actually runs, not agents.defaults. Omit the
  // field when there is no session (boot, a draft): the Wire Schema types it as
  // an optional string, and a serialized null is outside that contract.
  const target = sid !== undefined ? sid : sessionCurrent();
  // Captured here when the caller did not bring one, so every refresh carries a
  // ticket by construction rather than by each call site remembering. A caller
  // whose session was resolved BEFORE its own await must still pass the
  // generation it captured then -- the answer is about that older view, and a
  // ticket taken here would read as current.
  const ticket = gen !== undefined ? gen : viewGen;
  const mo = await rpc.call('model.options', target ? { session_id: target } : {});
  // model.options does its catalogue work off-thread, so responses can land out
  // of click order. A refresh keyed to a superseded view must not repaint the
  // page the reader has since moved to.
  if (ticket !== viewGen) return;
  providersLive = (mo.providers || []).map((p) => ({
    id: p.slug, name: p.name, homepage: p.homepage || '', models: p.models || [], on: p.authenticated,
    protocols: p.protocols || {}, protocolOverrides: p.protocol_overrides || {},
    kind: p.auth_type || 'api_key', needsBase: !!p.needs_api_base,
    apiBase: p.api_base || '', defaultApiBase: p.default_api_base || '',
    env: p.key_env || '', warn: p.warning || '',
    key: p.authenticated ? '已配置' : '',
  }));
  if (mo.model) { modelSet(mo.model); setModelLabel(); }
}

const settingsSnapshot = () => ({
  raw: RAW, configPath: configPathLive, everos: everosLive,
  // Both default-scoped on purpose: the settings page describes what new
  // conversations start on, so pairing the default model with the visible
  // session's provider badged the wrong row whenever the two scopes differ.
  providers: providersLive, curProvider: defaultProviderLive, model: defaultModelLive,
  toolGroups: TOOL_GROUPS, tools: toolsLive,
});

const settingsErr = (e) => (e.data && e.data.detail) || e.message || e;

DS.settings = {
  load: async () => {
    /* The tool inventory is part of settings. Loading it here keeps every
       opener on the island's one refresh path rather than replacing the
       demo-layer openSettings binding in live mode. */
    try { await loadExt(); } catch (e) { toast(`加载失败：${e.message || e}`); }
    await loadSettings();
    pushPermMode();
    await loadEveros();
    return settingsSnapshot();
  },
  /* Toasts are spoken here, where the legacy wording lived; the thrown
     handled tag tells the island to only redraw. */
  set: async (key, value) => {
    try {
      await rpc.call('settings.set', { key, value });
      await loadSettings();
      pushPermMode();
      toast(T('gui.set.saved'));
    } catch (e) {
      toast(T('gui.plug.op_failed', { err: settingsErr(e) }));
      throw { handled: true };
    }
    return settingsSnapshot();
  },
  /* A null fields object means "clear the section" (optional roles only). */
  everosSet: async (section, fields, borrowFrom) => {
    const p = fields ? { section, fields } : { section, clear: true };
    /* Only the name travels. The key stays where it is and the server copies
       it across -- what this page holds is `****set****`. */
    if (borrowFrom) p.borrow_from = borrowFrom;
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
  usage: (sessionKey) => rpc.call('settings.usage', { session_key: sessionKey || null }),
  provider: async (op, params) => {
    await rpc.call('model.' + op, params);
    await loadProviders();
    return settingsSnapshot();
  },
  model: () => defaultModelLive,
  defaultProvider: () => defaultProviderLive,
  version: () => APP_VERSION,
  /* The version check the rail-foot notice already does, on demand. No new
     backend: system.version carries the answer. */
  checkUpdate: async (btn) => {
    const was = btn.textContent;
    btn.textContent = T('gui.set.checking'); btn.disabled = true;
    try {
      /* check:true = fetch now, not the daily cache: the button says check for
         updates, and a person who just clicked it is asking about now. */
      const v = await rpc.call('system.version', { check: true });
      if (v.raven_version) APP_VERSION = v.raven_version;
      if (v.update_available) {
        showUpNote('ver', v.latest_version);
        drawSettings();
        askUpgrade();
        return;
      }
      /* The answer has to land on the button: this layer sends toasts to the
         console, and "nothing happened" is indistinguishable from a broken
         check. */
      btn.disabled = false;
      btn.textContent = T('gui.set.abt.latest');
      setTimeout(() => { btn.textContent = was; }, 2200);
      return;
    } catch (e) {
      btn.textContent = T('gui.set.abt.check_fail');
      setTimeout(() => { btn.textContent = was; }, 2600);
      if (window.console) console.error('[update check]', e);
    }
    btn.disabled = false;
  },
  /* The composer's picker popover, offered to the island's default-model
     button so both places pick a model the same way. */
  pickModel: (anchor, after) => openModelPicker(anchor, after, defaultModelLive),
  /* Declared here rather than beside langPickLive so the whole contract reads
     in one place. Not awaited: the pick repaints synchronously and the persist
     speaks for itself if it fails. */
  setLang: (v) => { langPickLive(v, { persist: true }); },
};

/* -- language ---------------------------------------------------------
   One key, both front ends: config.language also drives the TUI (which
   polls it) and the language the agent replies in. */
/* Named, and a local rather than a binding the demo layer declares for this
   layer to fill: the pick reaches it through DS.settings.setLang below.
   langSet moves the language and the catalogue together; what is added here is
   the persist and the redraw of everything drawn from JavaScript. */
async function langPickLive(next, { persist } = {}) {
  if (next === LANG) return;
  const prev = LANG;
  langSet(next);
  redrawAll();
  if (!persist) return;
  try {
    await rpc.call('config.set', { key: 'language', value: next });
    langRemember(next);
  } catch (e) {
    /* Put it back rather than leaving the page in a language the gateway does
       not agree with -- the same key drives the TUI and the agent's replies. */
    langSet(prev);
    redrawAll();
    toast(`切换语言失败：${(e.data && e.data.detail) || e.message || e}`);
  }
}

/* Everything the catalogue reaches that is drawn rather than written in
   the markup. Cheap enough to run wholesale on a language flip. */
function redrawAll() {
  sessionDraw();
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
  try { drawKb(); } catch { /* knowledge not loaded yet */ }
  try { drawPb(); } catch { /* playbooks not loaded yet */ }
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
  queueDraw();
  /* The words baked into stored segments (note labels, phrased previews) come
     back right on a rebuild from disk. Skipped while a turn is streaming:
     re-opening the session mid-turn would cut the stream off. */
  if (!draft && sessionCurrent() && !turn.busy()) sessionOpen(sess(sessionCurrent()));
}

/* The language the gateway last agreed to, kept where a page that cannot
   reach it can still read it. `loadLang` runs only after `rpc.connect()`
   succeeds, so on a failed connect nothing sets the language at all -- and
   the sign-in notice, the one message that explains the empty page, arrives
   in English on a Chinese install. Wrapped like the look settings, because
   private mode throws on access rather than answering null. */
const LANG_KEY = 'raven.gui.lang';

function langRemember(v) {
  try { localStorage.setItem(LANG_KEY, v); } catch { /* private mode */ }
}

/* Applied before the socket is up. Whatever `loadLang` resolves afterwards
   wins, so a language changed elsewhere still lands on this boot. */
function langRestore() {
  try {
    const v = localStorage.getItem(LANG_KEY);
    if (v === 'en' || v === 'zh') langSet(v);
  } catch { /* private mode */ }
}

async function loadLang() {
  try {
    const r = await rpc.call('config.get', { keys: ['language'] });
    const v = r && r.config && r.config.language;
    if (v === 'en' || v === 'zh') { langSet(v); langRemember(v); redrawAll(); }
  } catch { /* stay on the built-in default */ }
}

/* A model id is provider-qualified (openrouter/anthropic/claude-opus-4.6);
   the chip only has room for the part that identifies the model. */
const shortModel = (m) => String(m || '').split('/').pop();
const setModelLabel = () => {
  const model = modelCurrent();
  $('#modelName').textContent = shortModel(model); $('#modelChip').title = model;
};

/* The picker itself is an island (ui-web/src/features/model/). What stays here is
   the provider list this transport fetched and the rpc call that can reject.
   The island owns the current pick, so its optimistic update and rollback do
   not cross the page-layer boundary. */
/* provider is required -- a bare model id does not name whose credential serves
   it. scope comes from the opener, matching the TUI's `/model` with or without
   --default:
   - 'default' changes agents.defaults (the settings control). The visible
     session rides along so the server can say whether that conversation follows
     the default; when it does (`applies_to_session`), its chip is re-read, or
     the conversation would run the new default under a chip showing the old.
   - 'session' changes the open conversation; while it is still a draft there is
     no session to scope to, so the pick is staged (writing it would move the
     global default) and applied to the session the first message mints. Staging
     is said back to the caller: it is not an applied switch yet. */
async function persistModel(m, provider, scope) {
  const sid = sessionCurrent();
  // Captured with sid, before the write: the repaint below must prove the page
  // it would paint is still the page the reader is on. config.set and the
  // catalogue read behind model.options are both slow enough for the reader to
  // have left; the generation ticket is what lets loadProviders drop the late
  // answer instead of overwriting the conversation they moved to.
  const gen = viewGen;
  if (scope === 'default') {
    const p = { key: 'model', value: m, provider, scope: 'default' };
    if (sid) p.session_id = sid;
    const r = await rpc.call('config.set', p);
    // Reflect the new default only once it lands, so a refusal leaves the
    // settings row on the pair the config still holds.
    defaultModelLive = m;
    defaultProviderLive = provider;
    if (r && r.applies_to_session && sid) void loadProviders(sid, gen);
    // A visible draft follows the default the way an unswitched session does:
    // its first message creates the session on the new default, so the chip
    // must move with it -- unless the draft staged a pick of its own, which
    // outranks the default exactly as an own binding does.
    //
    // Under the same generation ticket as the session repaint above, and for
    // the same reason: the picker has closed, nothing locks the write, and
    // leaving the draft for a conversation of its own advances the generation
    // (every view switch does) -- so without this the resolved draft write
    // repaints a chip that has since been loaded correctly for someone else.
    if (!sid && !pendingModel && gen === viewGen) { modelSet(m); setModelLabel(); }
    return;
  }
  if (sid) {
    await rpc.call('config.set', { key: 'model', value: m, provider, session_id: sid });
    return;
  }
  pendingModel = { model: m, provider };
  return 'staged';
}

/* The tier over the wire. One method serves all three calls, and every reply
   carries the whole catalogue, so `read` and `set` differ only in whether they
   name a mode -- there is no separate menu fetch to keep in step.

   `session_key` is required by the handler and is the conversation the chip sits
   under; a page with no conversation yet has no tier to report, and the caller
   leaves the chip hidden on the refusal rather than inventing one. */
let tierMenu = [];

/* Read by the send path, which applies it next to the staged model.
   `pendingTier` itself is declared beside `pendingModel` in 080, where both are
   reset on the two paths that abandon a draft. */
function stagedTier() { const t = pendingTier; pendingTier = null; return t; }

DS.tier = {
  read: async () => {
    /* A draft asks too, and the handler answers the catalogue and its default
       for a key it has never seen -- which is exactly what the first turn of a
       new conversation will run at. */
    const r = await rpc.call('session.set_mode', { session_key: sessionCurrent() || '' });
    tierMenu = (r && r.availableModes) || tierMenu;
    return r;
  },
  set: async (mode) => {
    const sid = sessionCurrent();
    if (!sid) {
      /* Staged, and echoed back as though written: there is no server state to
         contradict it yet, and the chip has to show the reader what their next
         turn will run at. */
      pendingTier = mode;
      return { mode, availableModes: tierMenu };
    }
    const r = await rpc.call('session.set_mode', { session_key: sid, mode });
    tierMenu = (r && r.availableModes) || tierMenu;
    return r;
  },
};

DS.model = {
  providers: () => providersLive,
  persist: persistModel,
  setProtocol: async (model, provider, protocol) => {
    await rpc.call('model.set_protocol', { model, slug: provider, protocol });
    await loadProviders();
  },
  openSettings: () => RavenIslands.settings.open(),
};

$('#modelChip').onclick = () => openModelPicker(null, setModelLabel);
