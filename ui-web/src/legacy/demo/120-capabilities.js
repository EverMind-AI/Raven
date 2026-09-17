/* ══ module 2: capabilities page ══════════════════════════════════ */

import { islands } from '../../islands'
import { show as toast } from '../../shell/toast'
import * as caps from '../../state/caps'
import * as detail from '../../state/detail'
import * as page from '../../state/page'
import * as settingsDialog from '../../state/settingsDialog'
import { sources } from '../../state/sources'
import { $, applyDecorators } from './010-kernel.js'
import { drawCaps } from './152-skills.js'

/* Only one module page at a time. They used to cover the whole window, so
   two open at once was invisible; now that the rail stays put, the one behind
   shows through. */
const NAV_OF = {
  capsPage: () => (caps.get().tab === 'plugin' ? 'plugBtn' : 'skillBtn'),
  xaPage: 'moreBtn',
  connPage: 'moreBtn',
  memPage: 'memBtn',
  pbPage: 'pbBtn',
  kbPage: 'kbBtn',
  cronPage: 'moreBtn',
};

/* The skill and plugin layers wrap this verb. The registry is a `var` with no
   initialiser on purpose: demo/152-skills.js is a cycle-mate of this file, so
   it can register before this statement has run, and an initialiser would
   discard what it registered. */
var showPageDecorators;
function showPage(id) { return applyDecorators(showPageDecorators, showPageBase)(id); }
function decorateShowPage(wrap) { (showPageDecorators ??= []).push(wrap); }

/* The seven open flags, the scroll reset and the overlay closes are
   src/state/page.ts's now. This is still the name the chrome calls and the two
   layers above decorate, so the shell stays where it was. */
function showPageBase(id) { page.show(id); }

/* The tab, the filter reset and the drawer's close are src/state/caps.ts's now,
   and the two layers above subscribe there instead of decorating this. The
   registry stays because a harness registers through it
   (features/plugins/capabilities-source.test.ts). */
var extSetDecorators;
function extSet(tab) { return applyDecorators(extSetDecorators, extSetBase)(tab); }
function decorateExtSet(wrap) { (extSetDecorators ??= []).push(wrap); }

function extSetBase(tab) { caps.extSet(tab); }

async function openCaps(tab) {
  extSet(tab);
  showPage('capsPage');
  const src = sources.capabilities;
  if (src.loaded()) { drawCaps(); drawCapsBadge(); }
  else {
    const box = $('#capsBody'); box.innerHTML = '';
    box.appendChild(islands.skills.skeleton);
  }
  try {
    if (await src.load()) { drawCaps(); drawCapsBadge(); }
  } catch (e) {
    toast(`加载失败：${e.message || e}`);
    drawCaps(); drawCapsBadge();
  }
}
const openSkills = () => openCaps('skill');
const openPlugins = () => openCaps('plugin');
function closeCaps() { showPage(null); closeDetail(); }
/* A dialog, so it layers over whatever you were reading rather than replacing
   it -- but the rail still marks itself, since that is where you came from.
   The flag, the rail's marks and the focus are src/state/settingsDialog.ts's
   now; these are the names the Esc chain, the settings shortcut and the bridge
   still call. */
function setIsOpen() { return settingsDialog.isOpen(); }
function openSet() { settingsDialog.open(); }
function closeSet() { settingsDialog.close(); }

/* The flag, the fade and the four islands' cards are src/state/detail.ts's now,
   and the two layers above no longer decorate this. It is still the name the
   Escape chain and the live layer's redrawAll call. */
function closeDetail() { detail.close(); }

/* ══ module 2b: external agents ════════════════════════════════════
   The renderer is the xa island (ui-web/src/features/xa/); what remains here
   is its shell face -- the names the Esc handler and the live layer's
   redrawAll still call -- and the fixture source. The More row opens the
   page by importing the island (shell/navfly.ts); it does not come through
   here. The island owns everything drawn inside the body and its sheet in
   the shared detail drawer. */
function closeXa() { islands.xa.close(); }
function drawXa() {
  /* A language flip re-renders #xaBody with the new catalogue. */
  islands.xa.redraw();
}

/* The rail badge for capabilities that need attention. The composer used to
   carry a chip counting active capabilities too; it is gone -- how many tools
   are wired is a setup question, answered on the extensions page, not something
   to read while typing. What belongs under the field is the state of THIS
   session: how much context is left, and what the agent may do unasked. */
/* No counters on the rail's module rows -- the set-up-once modules do not
   nag from there; their state is spoken inside the page, where the fix is. */
function drawCapsBadge() {}

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
}

export { NAV_OF, showPageDecorators, showPage, decorateShowPage, showPageBase, extSetDecorators, extSet, decorateExtSet, extSetBase, openCaps, openSkills, openPlugins, closeCaps, setIsOpen, openSet, closeSet, closeDetail, closeXa, drawXa, drawCapsBadge }
