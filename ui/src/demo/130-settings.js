/* ══ module 3: settings page ══════════════════════════════════════
   The renderer is the settings island (ui/src/features/settings/); what
   remains here is its shell face -- the names the chrome, the boot list,
   the capabilities rows and the live layer's redrawAll still call -- the
   look/notification plumbing other parts share, and the fixture source. */

/* Read by the chrome's model chip menu and rebuilt in place by the live
   loadProviders, so the fixture snapshot and the picker read the same rows. */
const PROVIDERS = [
  { id: 'minimax', name: 'MiniMax', models: ['minimax-m3', 'minimax-m2'], on: true, kind: 'api_key' },
  { id: 'anthropic', name: 'Anthropic', models: ['claude-opus-4-5', 'claude-sonnet-4-6'], on: true, kind: 'api_key' },
  { id: 'openai', name: 'OpenAI', models: ['gpt-5.1', 'gpt-5-mini'], on: false, kind: 'api_key' },
  { id: 'deepseek', name: 'DeepSeek', models: ['deepseek-v3.2'], on: false, kind: 'api_key' }
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
  SESS = []; cur = null; drawList(); $('#stage').innerHTML = '';
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

/* ---- appearance -----------------------------------------------------
   Each front end's own business, so it persists here rather than in the
   shared config. Language is the exception, it goes to config.language
   because the TUI and the agent's replies follow it. */
const LOOK_KEY = 'raven.gui.look';
/* Tells the desktop shell what ground colour this window actually shows, so
   its native launch splash can match on the NEXT run -- the page's theme
   lives in this origin's localStorage, which the shell cannot read before
   the page is up. No-op outside the shell. */
function shellTheme() {
  try {
    const t = document.documentElement.dataset.theme
      || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
    window.webkit.messageHandlers.raven.postMessage({ type: 'theme', value: t });
  } catch { /* not the shell */ }
}
function applyLook() {
  const d = document.documentElement.dataset;
  if (CFG.theme === 'system') delete d.theme; else d.theme = CFG.theme;
  d.motion = CFG.motion;
  d.font = CFG.codeFont;
  shellTheme();
}
function lookSave() {
  try {
    localStorage.setItem(LOOK_KEY, JSON.stringify(
      { theme: CFG.theme, codeFont: CFG.codeFont, motion: CFG.motion }));
  } catch { /* private mode or quota: this tab only */ }
}
function lookLoad() {
  try {
    const o = JSON.parse(localStorage.getItem(LOOK_KEY) || 'null');
    if (o && typeof o === 'object') {
      if (o.theme) CFG.theme = o.theme;
      if (o.codeFont) CFG.codeFont = o.codeFont;
      if (o.motion) CFG.motion = o.motion;
    }
  } catch { /* fall back to the defaults in CFG */ }
  applyLook();
}
/* The island's appearance page reads and writes through these two. */
function lookGet() {
  return { theme: CFG.theme, codeFont: CFG.codeFont, motion: CFG.motion, lang: LANG };
}
function lookSet(patch) {
  if (patch.theme) CFG.theme = patch.theme;
  if (patch.codeFont) CFG.codeFont = patch.codeFont;
  if (patch.motion) CFG.motion = patch.motion;
  applyLook();
  lookSave();
}
/* The fixture half of the island's language pick: flip the catalogue and
   repaint what this layer draws itself. Nothing is persisted, because a page
   with no gateway behind it has nowhere to persist to -- which is the honest
   offline answer rather than a silent no-op. Live mode installs its own, and
   this one is never consulted there. */
function langPickDemo(v) {
  langSet(v);
  drawList(); drawSettings(); drawPerm(); drawCtx(); drawFoot();
}

/* ---- notifications ---------------------------------------------------
   Front-end only: the OS notification fires when a turn finishes while the
   window is in the background. Preference persists per front end. */
const NTF_KEY = 'raven.gui.ntf';
const NTF = { on: false };
try {
  const o = JSON.parse(localStorage.getItem(NTF_KEY) || 'null');
  if (o && typeof o === 'object') NTF.on = !!o.on;
} catch { /* default off */ }
function ntfSave() {
  try { localStorage.setItem(NTF_KEY, JSON.stringify(NTF)); } catch { /* private mode */ }
}
function ntfPush(title, body, opts) {
  if (!NTF.on || !('Notification' in window) || Notification.permission !== 'granted') return;
  /* Only when the reader is away: a toast already covers the foreground. */
  if (!(opts && opts.force) && document.hasFocus()) return;
  try { new Notification(title, body ? { body } : undefined); } catch { /* platform quirk */ }
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
    providers: PROVIDERS, curProvider: '', model,
    toolGroups: TOOL_GROUPS, tools: TOOLS,
  }),
  set: async () => { throw { notLive: true }; },
  everosSet: async () => { throw { notLive: true }; },
  usage: async () => null,
  provider: async () => { throw { notLive: true }; },
  model: () => model,
  checkUpdate: () => notLive(),
  setLang: (v) => langPickDemo(v),
};
