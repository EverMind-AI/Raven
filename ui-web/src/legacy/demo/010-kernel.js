/* ═══════════════════════════════════════════════════════════════════
   The page's own small runtime: the helpers every part reaches for.
   There is one data path now -- a page with no raven behind it reads the
   same contract off a fixture transport (ui-web/src/rpc/fixtures/) -- so
   nothing here knows which.

   The message catalogue and the language are src/i18n/t.ts and
   src/state/lang.ts; what is left here is the names the other parts still
   import, re-exported from those two.
   ═══════════════════════════════════════════════════════════════════ */

import { code as LANG, I18N, T, fillVars, slashText, slashName, slashHelp } from '../../i18n/t'
import * as lang from '../../state/lang'

const $ = (s) => document.querySelector(s);
const mk = (t, c, x) => { const n = document.createElement(t); if (c) n.className = c; if (x != null) n.textContent = x; return n; };
const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;' }[c]));

/* The only writer of LANG, and the reason it has one: the current language and
   the catalogue rendered over the static markup have to move together, and
   three separate places used to assign the first and remember the second. Both
   halves are state/lang.ts now, and this forwards to the setter that does not
   notify: moving the language has never repainted what is drawn from
   JavaScript, which every caller does for itself. */
function langSet(v) { lang.setQuiet(v); }
/* The GATEWAY host's OS family -- host-side actions (reveal in Finder) happen
   there, not in this browser. The UA is only the prior for the usual localhost
   case; system.hello corrects it. The live layer calls the setter rather than
   assigning, the way it does for LANG: an imported binding is read-only, and a
   field the other layer writes is a strand count-shared-globals.mjs counts. */
/** @type {string} */
let HOST_PLATFORM;
function hostPlatformSet(v) { HOST_PLATFORM = v; }

/* Later layers decorate a handful of this shell's verbs rather than reassigning
   them. Registration order is application order from the inside out, so the
   last registrar wraps every earlier one -- the order the reassignment chain
   produced when each layer captured the then-current value. */
function applyDecorators(list, base) {
  return (list ?? []).reduce((f, wrap) => wrap(f), base);
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

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  // If the script runs at all, this marker goes; if you still see it, it did not.
  (() => { const n = document.getElementById('noJs'); if (n) n.remove(); })();
  HOST_PLATFORM = /Mac/.test(navigator.platform) ? 'mac'
    : /Win/.test(navigator.platform) ? 'windows' : 'linux';
}

export { $, mk, esc, I18N, LANG, langSet, HOST_PLATFORM, hostPlatformSet, applyDecorators, fillVars, T, slashText, slashName, slashHelp, MCP_RE, rawVerb, ACP_TITLE_RE, callParts, verb, verbIng, dur, COPY_ICO, tipFlash }
