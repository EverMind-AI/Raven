/* ══ module 2: capabilities page ══════════════════════════════════ */

import { islands } from '../../islands'
import { show as toast } from '../../shell/toast'
import { sources } from '../../state/sources'
import { $, applyDecorators } from './010-kernel.js'
import { markNewCurrent } from './050-rail.js'
import { drawCaps } from './152-skills.js'

let extTab = 'skill';
/* The filter bar's state. On an object because demo/150-chrome.js writes both
   fields from the pill row and the search field. */
const capFilter = { kind: 'all', query: '' };

/* Only one module page at a time. They used to cover the whole window, so
   two open at once was invisible; now that the rail stays put, the one behind
   shows through. */
const NAV_OF = {
  capsPage: () => (extTab === 'plugin' ? 'plugBtn' : 'skillBtn'),
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

function showPageBase(id) {
  Object.keys(NAV_OF).forEach((p) => { $('#' + p).dataset.open = String(p === id); });
  /* From the top, every time: the scroller keeps its position across a close
     and reopen, so a page could greet the reader halfway down its own list. */
  if (id) {
    const sc = $('#' + id).querySelector('.work');
    if (sc) sc.scrollTop = 0;
  }
  /* Read by the rail: while a page is up it owns the selected state, so the
     session behind it stops claiming one too. */
  document.querySelector('.app').dataset.page = id ? 'on' : 'off';
  markNewCurrent();
  /* caps and memory both use the shared detail drawer */
  if (id !== 'capsPage' && id !== 'memPage') closeDetail();
  /* Same rule for the overlays a single page owns: the channel drawer and the
     new-job sheet used to survive the switch and sit over whatever came next,
     still showing the entry the reader had left behind. */
  if (id !== 'connPage') islands?.connections?.closeDialog?.();
  if (id !== 'cronPage') islands?.cron?.closeSheet?.();
}

/* Switching module resets the filters: a query typed while browsing skills is
   not a question about plugins. */
var extSetDecorators;
function extSet(tab) { return applyDecorators(extSetDecorators, extSetBase)(tab); }
function decorateExtSet(wrap) { (extSetDecorators ??= []).push(wrap); }

function extSetBase(tab) {
  if (!tab || tab === extTab) return;
  extTab = tab; capFilter.kind = 'all'; capFilter.query = '';
  $('#cq').value = '';
  [...$('#cKind').children].forEach((c, i) => c.setAttribute('aria-pressed', String(i === 0)));
  closeDetail();
}

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
   it -- but the rail still marks itself, since that is where you came from. */
/* Called from markNewCurrent, which runs during the first draw -- so it has to
   tolerate being asked before the dialog's markup is in the document. */
function setIsOpen() {
  const v = $('#setVeil');
  return !!v && v.dataset.open === 'true';
}
function openSet() {
  $('#setVeil').dataset.open = 'true';
  markNewCurrent();
  $('#setModal').focus();
}
function closeSet() {
  $('#setVeil').dataset.open = 'false';
  markNewCurrent();
}

var closeDetailDecorators;
function closeDetail() { return applyDecorators(closeDetailDecorators, closeDetailBase)(); }
function decorateCloseDetail(wrap) { (closeDetailDecorators ??= []).push(wrap); }

function closeDetailBase() { $('#detail').dataset.open = 'false'; }

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

export { extTab, capFilter, NAV_OF, showPageDecorators, showPage, decorateShowPage, showPageBase, extSetDecorators, extSet, decorateExtSet, extSetBase, openCaps, openSkills, openPlugins, closeCaps, setIsOpen, openSet, closeSet, closeDetailDecorators, closeDetail, decorateCloseDetail, closeDetailBase, closeXa, drawXa, drawCapsBadge }
