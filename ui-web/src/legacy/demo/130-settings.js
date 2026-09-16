/* ══ module 3: settings page ══════════════════════════════════════
   The renderer is the settings island (ui-web/src/features/settings/); what
   remains here is its shell face -- the names the chrome, the boot list,
   the capabilities rows and the live layer's redrawAll still call. */

import { islands } from '../../islands'
import { draw as drawCtx } from '../../shell/ctxchip'
import { draw as drawFoot } from '../../shell/foot'
import { draw as drawPerm } from '../../shell/perm'
import { show as toast } from '../../shell/toast'
import { settingsTab } from '../../state/settingsTab'
import { $, T, langSet, mk } from './010-kernel.js'
import { sessionDraw } from './050-rail.js'

// Filled in from system.version once the socket is up, and unknown until then:
// the running install is the only thing that knows its version. The rail foot
// and the About card both render this as "--" rather than as a guess.
/** @type {string | null} */
let APP_VERSION = null;
function appVersionSet(v) { APP_VERSION = v; }

/* A tagged control never renders the new value; the refusal is spoken in the
   row, not in a toast. Kept for the legacy rows (the capabilities page's tool
   credentials). */
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

/* The language pick with no persist behind it: flip the catalogue and repaint
   what this layer draws itself. Unreached today -- the settings source's own
   pick is installed over it (live/120-settings.js) and writes
   `config.language` in every mode -- and kept next to the redraw it performs
   until the settings chrome is a component. */
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
  islands.settings.redraw();
}

// Everything runs on this machine; the chip is a label, not a switch.
function setRuntime() {
  $('#envName').textContent = '本机';
  $('#envChip').querySelector('.led').className = 'led';
}

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  /* The open tab. A shared slot rather than a binding of this part's: the
   chrome writes it before opening the dialog and the settings island reads and
   writes the same one (src/state/settingsTab.ts). */
  settingsTab.id = 'usage';

}

export { APP_VERSION, appVersionSet, nlSay, notLive, langPickDemo, isMac, modKey, drawSettings, setRuntime }
