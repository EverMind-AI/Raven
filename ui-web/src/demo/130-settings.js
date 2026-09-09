/* ══ module 3: settings page ══════════════════════════════════════
   The renderer is the settings island (ui-web/src/features/settings/); what
   remains here is its shell face -- the names the chrome, the boot list,
   the capabilities rows and the live layer's redrawAll still call -- and the
   fixture source. */

/* The fixture provider rows. Live mode owns the rows fetched from its model
   source, so it never refills this demo list in place. */
const PROVIDERS = [
  { id: 'minimax', name: 'MiniMax (Global)', homepage: 'https://platform.minimax.io/', models: ['minimax/MiniMax-M3', 'minimax/MiniMax-M2'], on: true, kind: 'api_key' },
  { id: 'minimax_cn_api', name: 'MiniMax (CN)', models: ['minimax-cn-api/MiniMax-M3', 'minimax-cn-api/MiniMax-M2'], on: false,
    homepage: 'https://platform.minimaxi.com/', kind: 'endpoint', defaultApiBase: 'https://api.minimaxi.com/v1/' },
  { id: 'anthropic', name: 'Anthropic', homepage: 'https://anthropic.com/', models: ['claude-opus-4-5', 'claude-sonnet-4-6'], on: true, kind: 'api_key' },
  { id: 'openai', name: 'OpenAI', homepage: 'https://openai.com/', models: ['gpt-5.1', 'gpt-5-mini'], on: false, kind: 'api_key' },
  { id: 'deepseek', name: 'DeepSeek', homepage: 'https://deepseek.com/', models: ['deepseek-v3.2'], on: false, kind: 'api_key' },
  { id: 'nvidia_nim', name: 'NVIDIA', models: ['nvidia-nim/nvidia/nemotron-3-super-120b-a12b', 'nvidia-nim/openai/gpt-oss-120b'], on: false,
    homepage: 'https://build.nvidia.com/explore/discover', kind: 'api_key', defaultApiBase: 'https://integrate.api.nvidia.com/v1' },
  { id: 'lm_studio', name: 'LM Studio', models: [], on: false, kind: 'local', needsBase: true,
    homepage: 'https://lmstudio.ai/', defaultApiBase: 'http://localhost:1234/v1' }
];

/* The open tab. A window property, not a script binding: the chrome writes
   `sTab = 'model'` before opening the dialog, and the island (a separate
   script that cannot see this script's scope) reads and writes the slot. */
window.sTab = 'usage';

// Filled in from system.version once the socket is up, and unknown until then:
// the running install is the only thing that knows its version. The rail foot
// and the About card both render this as "--" rather than as a guess.
let APP_VERSION = null;

/* Wiping the list is a session operation, so it goes on the session source
   rather than staying a name the live layer overwrites. It has to live in this
   layer either way: the list and the current session are page bindings, and an
   island cannot reassign one. */
DS.sessions.deleteAll = () => {
  sessionReplace([]); sessionSet(null); sessionDraw(); $('#stage').innerHTML = '';
  $('#title').textContent = T('gui.new_task'); pitch();
};

/* A tagged control never renders the new value; the refusal is spoken in the
   row, not in a toast. Kept for the legacy rows (capabilities tool
   credentials, the connections fixture). */
function nlSay(el, reset, msg) {
  if (reset) reset();
  /* The row when the control sits in one, else the card: a chooser is a whole
     card wide, and the refusal has to land where the click did. */
  const host = el && el.closest ? el.closest('.crow') || el.closest('.scard') : null;
  const text = T(msg || 'gui.set.not_live');
  if (!host) { toast(text); return; }
  host.querySelectorAll('.nlmsg').forEach((n) => n.remove());
  const m = mk('div', 'nlmsg', text);
  host.appendChild(m);
  setTimeout(() => m.remove(), 3600);
}
const notLive = () => toast(T('gui.set.not_live'));

/* The fixture half of the island's language pick: flip the catalogue and
   repaint what this layer draws itself. Nothing is persisted, because a page
   with no gateway behind it has nowhere to persist to -- which is the honest
   offline answer rather than a silent no-op. Live mode installs its own, and
   this one is never consulted there. */
function langPickDemo(v) {
  langSet(v);
  sessionDraw(); drawSettings(); drawPerm(); drawCtx(); drawFoot();
}

const isMac = () => /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const modKey = () => (isMac() ? '⌘' : 'Ctrl +');

/* The draw shim every caller keeps: the boot list, the chrome, redrawAll,
   and the capabilities rows. */
function drawSettings() {
  /* The island repaints #spanels (and the #snavList rail) from its store. */
  RavenIslands.settings.redraw();
}

// Everything runs on this machine; the chip is a label, not a switch.
function setRuntime() {
  $('#envName').textContent = '本机';
  $('#envChip').querySelector('.led').className = 'led';
}

/* The fixture source: canned config behind the same interface the rpc source
   implements. Writes refuse with the tag the island renders as the in-row
   not-live message; usage answers null, which the island draws as the demo's
   no-data note. Registered, not declared-for-override -- live mode installs
   its own DS.settings and this object is never consulted. */
DS.settings ??= {
  load: async () => ({
    raw: {}, configPath: '~/.raven/config.json', everos: null,
    providers: PROVIDERS, curProvider: '', model: modelCurrent(),
    toolGroups: TOOL_GROUPS, tools: TOOLS,
  }),
  set: async () => { throw { notLive: true }; },
  everosSet: async () => { throw { notLive: true }; },
  usage: async () => null,
  provider: async () => { throw { notLive: true }; },
  model: () => modelCurrent(),
  version: () => APP_VERSION,
  checkUpdate: () => notLive(),
  setLang: (v) => langPickDemo(v),
};

/* The tier fixture: three rungs behind the same interface `session.set_mode`
   implements, so the design canvas can show the chip and its panel.

   The sentences are `raven/config/schema.py:_TIER_TEXTS` and their entries in
   `raven/i18n/zh.py`, verbatim in both languages, because ONE PER RUNG is the
   part of this control worth reviewing: the row draws the description, so a
   sentence of this file's own invention -- and especially one sentence repeated
   with the id swapped in -- previews a line height and a wrap the live UI never
   receives. Keyed to `LANG` for the same reason: raven translates these three
   itself, so the canvas can only show the Chinese lines by carrying them. */
let tierDemo = 'high';
const TIER_SUB = {
  en: {
    medium: 'The least effort a sub-agent is asked for.',
    high: 'The middle amount of effort, between the other two.',
    max: 'The most effort a sub-agent is asked for.',
  },
  zh: {
    medium: '子代理被要求付出的最少努力。',
    high: '居中的投入，介于另外两档之间。',
    max: '子代理被要求付出的最多努力。',
  },
};
const tierMenuDemo = () => ['medium', 'high', 'max'].map((id) => ({
  id,
  name: id.charAt(0).toUpperCase() + id.slice(1),
  description: (TIER_SUB[LANG] || TIER_SUB.en)[id],
}));
DS.tier ??= {
  read: async () => ({ mode: tierDemo, availableModes: tierMenuDemo() }),
  set: async (mode) => { tierDemo = mode; return { mode: tierDemo, availableModes: tierMenuDemo() }; },
};
