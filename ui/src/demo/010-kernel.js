/* ═══════════════════════════════════════════════════════════════════
   Front-end demo. The transcript renderer consumes the same event
   shapes Raven's JSON-RPC gateway already emits (episode.start /
   thinking.delta / token.delta / tool.start / tool.complete /
   message.complete), so going live means replacing replay() with a
   socket — the rendering code below does not change.
   ═══════════════════════════════════════════════════════════════════ */
// If the script runs at all, this marker goes; if you still see it, it did not.
(() => { const n = document.getElementById('noJs'); if (n) n.remove(); })();

const $ = (s) => document.querySelector(s);
const mk = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c; if (x != null) n.textContent = x; return n; };
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;' }[c]));

/* ── i18n ────────────────────────────────────────────────────────────
   The catalogue is spliced in from i18n/messages.json at build time —
   the same file the TUI generates its own copy from, so one edit moves
   both front ends. LANG mirrors config.language. */
const I18N = /*__I18N__*/{ "slash": {}, "ui": {} };
let LANG = 'en';
/* The GATEWAY host's OS family -- host-side actions (reveal in Finder) happen
   there, not in this browser. The UA is only the prior for the usual localhost
   case; system.hello corrects it. */
let HOST_PLATFORM = /Mac/.test(navigator.platform) ? 'mac'
  : /Win/.test(navigator.platform) ? 'windows' : 'linux';
/* Assigned by live.js, which owns the RPC write. Null in the offline demo,
   where flipping the language is a local repaint and nothing more. */
let applyLang = null;

const fillVars = (s, vars) =>
  vars ? String(s).replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m)) : String(s);

const T = (key, vars, fallback) => {
  const e = I18N.ui[key] || {};
  return fillVars(e[LANG] != null ? e[LANG] : (e.en != null ? e.en : (fallback != null ? fallback : key)), vars);
};

const slashText = (id) => {
  const e = I18N.slash[id] || {};
  return (LANG !== 'en' && e[LANG]) || e.en || {};
};
const slashName = (id) => slashText(id).name || id;
const slashHelp = (id) => slashText(id).help || '';

/* Static markup carries keys, not copy: data-i18n for text, -ph for a
   placeholder, -title / -aria for the two attributes that are read out. */
function applyI18n(root) {
  (root || document).querySelectorAll('[data-i18n]').forEach((n) => { n.textContent = T(n.dataset.i18n); });
  (root || document).querySelectorAll('[data-i18n-ph]').forEach((n) => { n.placeholder = T(n.dataset.i18nPh); });
  (root || document).querySelectorAll('[data-i18n-title]').forEach((n) => { n.title = T(n.dataset.i18nTitle); });
  (root || document).querySelectorAll('[data-i18n-aria]').forEach((n) => {
    n.setAttribute('aria-label', T(n.dataset.i18nAria));
  });
  (root || document).querySelectorAll('[data-i18n-tip]').forEach((n) => {
    n.dataset.tip = T(n.dataset.i18nTip);
  });
  document.documentElement.lang = LANG === 'zh' ? 'zh-CN' : 'en';
}

/* Verbs come from i18n; a tool without an entry keeps its raw name rather
   than getting a made-up translation. mcp_* names show as [server] tool. */
const MCP_RE = /^mcp_([^_]+)_(.+)$/;
const rawVerb = (n) => {
  const m = MCP_RE.exec(n || '');
  return m ? m[2].split('_').join(' ') : String(n || '').split('_').join(' ');
};
/* An ACP agent names a call with a human title -- `terminal: cd /x && git ...`
   -- and that whole string arrives where a tool name belongs. Rendered as one
   it becomes the row's verb, so a stretch of work reads as a column of shell
   commands and the summary line joins two of them with a dot. Split at the
   first colon: the program is the verb, the rest is the argument, which is
   where every other call in the transcript keeps its argument. A name in any
   other shape (every raven tool -- `read_file`, `exec`) has no colon and comes
   back untouched. The argument half spans newlines on purpose: a shell title is
   routinely `terminal: cd x\necho y`, and stopping at the first line left half
   of them unsplit and the summary reading as a column of commands again. */
const ACP_TITLE_RE = /^([A-Za-z_][\w.-]{0,31}):\s*(\S[\s\S]*)$/;
const callParts = (raw) => {
  const m = ACP_TITLE_RE.exec(String(raw || ''));
  return m ? { name: m[1], display: m[2] } : { name: String(raw || ''), display: '' };
};

const verb = (n) => T('gui.act.v.' + n, null, rawVerb(n));
const verbIng = (n) => T('gui.act.ing.' + n, null, rawVerb(n));
/* One unit while it still fits in one: seconds under a minute, minutes and
   seconds under an hour, hours on top of that. The smaller fields are padded so
   a column of durations lines up instead of jittering. */
const dur = (ms) => {
  const t = Math.max(0, Math.round(ms / 1000));
  const p = (n) => String(n).padStart(2, '0');
  if (t >= 3600) return `${Math.floor(t / 3600)}h${p(Math.floor(t % 3600 / 60))}m${p(t % 60)}s`;
  if (t >= 60) return `${Math.floor(t / 60)}m${p(t % 60)}s`;
  return ms < 10000 ? `${(ms / 1000).toFixed(1)}s` : `${t}s`;
};

/* One copy glyph for every footer that offers to copy something. */
const COPY_ICO = '<rect x="9" y="9" width="11" height="11" rx="2.5"/>'
  + '<path d="M5.5 15H5a1.5 1.5 0 0 1-1.5-1.5v-8A1.5 1.5 0 0 1 5 4h8A1.5 1.5 0 0 1 14.5 5.5V6"/>';

/* No toasts for tiny actions: the button reports back through its own tip. */
function tipFlash(b, word) {
  const keep = b.dataset.tip;
  b.dataset.tip = word;
  setTimeout(() => { b.dataset.tip = keep; }, 1400);
}

