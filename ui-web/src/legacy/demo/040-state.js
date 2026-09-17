/* ══ app state ════════════════════════════════════════════════════ */

import { islands } from '../../islands'
import { closeApproval as approvalClose, open as approveSheet, openApproval as approvalSheet } from '../../features/composer/approve'
import { close as clarifyClose, open as clarifySheet } from '../../features/composer/clarify'
import { drawQueue as queueDraw, dropDraft, loadDraft, parkDraft, queueClear, queuePush, queueRestore, queueShift, queueSnapshot, turn } from '../../features/composer/mount'
import { add as sheetAdd, dropClass as sheetDropClass, forget as sheetsForget, remove as sheetRemove, session as sheetSession, sync as sheetsSync } from '../../features/composer/sheets'
import { current as modelCurrent, setCurrent as modelSet } from '../../features/model/store'
import { bootError, show as failureBar } from '../../shell/failure'
import { show as menuAt } from '../../shell/menu'
import { close as closePermPop } from '../../shell/perm'
import { setCurrent as sessionSet } from '../../shell/session'
import { close as closeTierPop } from '../../shell/tier'
import { show as toast } from '../../shell/toast'
import { open as upShade } from '../../shell/upgrade'
import * as confirm from '../../state/confirm'
import { $, T, mk } from './010-kernel.js'
import { sessionRows } from './050-rail.js'

let timers = [];
/* The running turn's usage totals. On an object because the conversation,
   replay and composer layers all write it. */
const runState = { use: null };
let rt = 'local', undoBin = null;

const stop_ = () => { timers.forEach(clearTimeout); timers = []; };
const later = (ms, fn) => timers.push(setTimeout(fn, ms));
const down = () => islands.transcript.down();
const sess = (id) => sessionRows().find((s) => s.id === id);

/* ══ composer drafts ══════════════════════════════════════════════
   Storage and ownership live in islands.composer. */
/* ══ confirm dialog ═══════════════════════════════════════════════
   The question, the two buttons and the veil are src/state/confirm.ts's now.
   This is the name the islands reach through the bridge and the live layer's
   callers still use. */
function confirmAsk(title, body, label, fn) { confirm.ask(title, body, label, fn); }

/* ══ session-scoped sheets ════════════════════════════════
   The rack that holds everything docking above the composer -- a clarify
   question, an approval request, a dag graph -- is the composer island's now,
   filed per conversation so that switching sessions cannot leave another one's
   question sitting over the field. See ui-web/src/features/composer/sheets.ts for
   what that scoping is for and what it does not fix.

   What this part re-exports are the island's own verbs under the names every
   caller has used all along. They were bound by one destructure of the island
   bag in this part's install(), which is why nothing could be typed and nothing
   could be read before the layer had installed; a plain import is the same
   binding without either limit. The bag still carries them for stage C. */

/* The one exception, and it is not the island's own verb: a draft that becomes
   a session has to be claimed in two stores at once, and the bag is where that
   pair is spelled (see islands.ts). */
const claimDraft = (id) => islands.composer.claimDraft(id);

/* ══ context menu ═════════════════════════════════════════════════
   One rule for right-click everywhere in the window, because the old answer was
   "whatever that element happened to wire up": a few rows opened the app menu,
   the transcript swallowed the event entirely, and everything else fell through
   to the host's own menu -- Look Up / Translate / Services, in the system's
   language, over a file tree. Three different panels for the same gesture.

   The rule:
   1. In a text field, or over a real text selection, the host menu wins. It has
      paste, dictation, spelling and Look Up, and none of that is ours to
      reimplement.
   2. On a surface that declares actions (a session, a change, a file, a
      command, a link), the app menu opens with those actions -- localized, same
      panel as the row's own "..." button.
   3. Anywhere else, nothing opens. Chrome has no context actions, and a menu
      offering only Services is noise. */
function ctxMenu(el, items) { el._ctx = items; }

/* Every menu that copies something reports it the same way, and a clipboard the
   host refused is not worth an error the reader cannot act on. */
function copyToClip(text, done) {
  if (!navigator.clipboard) return;
  navigator.clipboard.writeText(String(text)).then(() => toast(done), () => {});
}

function nativeCtxOk(t) {
  if (t.closest('input, textarea, select, [contenteditable="true"]')) return true;
  const sel = window.getSelection();
  if (!sel || sel.isCollapsed || !String(sel).trim()) return false;
  /* Only when the click is actually inside the selected text -- a selection
     left behind elsewhere on the page is not what this click is about. */
  return sel.containsNode(t, true) || (t.contains(sel.anchorNode) && t.contains(sel.focusNode));
}

/* ── the hover pill ────────────────────────────────────────────────────
   The one .tipp element, driven from here. Placement prefers above (below for
   .tipdn), flips when the preferred side leaves the window, and clamps to the
   viewport either way. */
let tipEl;
let tipFor = null;

function tipPlace() {
  if (!tipFor || !tipFor.isConnected || !tipFor.dataset.tip) { tipHide(); return; }
  tipEl.textContent = tipFor.dataset.tip;
  tipEl.dataset.on = 'true';
  const vw = document.documentElement.clientWidth;
  const vh = document.documentElement.clientHeight;
  const r = tipFor.getBoundingClientRect();
  const tr = tipEl.getBoundingClientRect();
  const below = tipFor.classList.contains('tipdn');
  let top = below ? r.bottom + 5 : r.top - tr.height - 5;
  if (top < 4) top = r.bottom + 5;
  if (top + tr.height > vh - 4) top = r.top - tr.height - 5;
  tipEl.style.left = Math.max(4, Math.min(vw - tr.width - 4, r.left + r.width / 2 - tr.width / 2)) + 'px';
  tipEl.style.top = Math.max(4, top) + 'px';
}
function tipHide() { tipFor = null; tipEl.dataset.on = 'false'; }

/* Shell window drag: the OS title bar is hidden, so the header band has to do
   its job. WebKit's implicit background-drag heuristic proved fragile (it
   silently stopped once the band gained positioned content), so the shell is
   told explicitly -- grab anywhere in the band that is not a control. */
const dragBand = (e) => {
  if (document.documentElement.dataset.shell !== '1' || e.button !== 0) return false;
  if (!e.target.closest('.railtop, .top, .ws-top')) return false;
  return !e.target.closest('button, a, input, textarea, select, [role="link"], .grip');
};

/* Selecting a whole line in the transcript -- a paragraph select, or a word
   select on the last line -- used to run past the end of the transcript and
   into the composer, because a paragraph selection ends at the next selectable
   position in document order and the last transcript line is the last text in
   the scroll region. Every empty box on the way (the seam, the wordmark, the
   dock, the queue strip, the field) then got a selection rect of its own, which
   reads as a blue band over blank space above the input, and the copy picked up
   their line breaks. `user-select: none` on the composer is not the fix -- it
   makes the search skip further rather than stop, so the bands grow -- and
   `user-select: contain`, which would be, is not implemented in Chromium. So
   clamp instead: a selection that starts inside the transcript ends inside it.
   Idempotent by construction, which is what keeps it from looping on the
   selectionchange it causes. */
const lastTextNodeIn = (root) => {
  const walk = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.data && n.data.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT),
  });
  let last = null;
  for (let n = walk.nextNode(); n; n = walk.nextNode()) last = n;
  return last;
};

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sessionSet('a');
  /* Dev-only hook, beside __clarify, __upnote and __dag in the live layer and
   for the same reason: the approval sheet only appears when an engine asks for
   one, which is too long a loop to design a sheet in. On window because a
   devtools console is the only caller there will ever be
   (__approve('rm -rf build/')). */
  window.__approve = (p) => approveSheet(p || 'rm -rf build/',
    () => toast(T('gui.confirm.allow')), () => toast(T('gui.confirm.deny')));

  document.addEventListener('contextmenu', (e) => {
    if (nativeCtxOk(e.target)) return;
    e.preventDefault();
    for (let n = e.target; n && n !== document; n = n.parentElement) {
      if (!n._ctx) continue;
      const items = n._ctx();
      if (items && items.length) menuAt(e.clientX, e.clientY, items);
      return;
    }
  });

  /* The 更多 group is rail navigation, not a popover: it folds on its own
   toggle only, never on an outside click. */
  document.addEventListener('pointerdown', (e) => {
    if (!e.target.closest('#permPop') && !e.target.closest('#permChip')) closePermPop();
    if (!e.target.closest('#tierPop') && !e.target.closest('#tierChip')) closeTierPop();
  }, true);
  tipEl = mk('div', 'tipp');
  document.body.appendChild(tipEl);

  document.addEventListener('pointerover', (e) => {
    const t = e.target.closest ? e.target.closest('[data-tip]') : null;
    if (t === tipFor) return;
    if (t) { tipFor = t; tipPlace(); } else tipHide();
  });
  /* A control that rewrites its own label while hovered (copy's "已复制" flash)
   keeps the visible pill in step. */
  new MutationObserver(() => { if (tipFor) tipPlace(); })
    .observe(document.body, { subtree: true, attributes: true, attributeFilter: ['data-tip'] });
  document.addEventListener('scroll', tipHide, true);
  document.addEventListener('mousedown', (e) => {
    if (!dragBand(e)) return;
    try { window.webkit.messageHandlers.raven.postMessage({ type: 'drag' }); } catch { /* browser */ }
  });
  document.addEventListener('dblclick', (e) => {
    if (!dragBand(e)) return;
    try { window.webkit.messageHandlers.raven.postMessage({ type: 'zoom' }); } catch { /* browser */ }
  });
  document.addEventListener('selectionchange', () => {
    const scroll = $('#scroll');
    const sel = document.getSelection();
    if (!scroll || !sel || !sel.rangeCount || sel.isCollapsed) return;
    const range = sel.getRangeAt(0);
    if (!scroll.contains(range.startContainer) || scroll.contains(range.endContainer)) return;
    const tail = lastTextNodeIn(scroll);
    if (!tail) return;
    const clamped = range.cloneRange();
    try { clamped.setEnd(tail, tail.data.length); } catch { return; }
    sel.removeAllRanges();
    sel.addRange(clamped);
  });
}

export { timers, runState, rt, undoBin, stop_, later, down, sess, confirmAsk, sheetSession, sheetAdd, sheetRemove, sheetDropClass, sheetsSync, sheetsForget, approveSheet, approvalSheet, approvalClose, clarifySheet, clarifyClose, queueDraw, queuePush, queueShift, queueClear, queueSnapshot, queueRestore, parkDraft, loadDraft, dropDraft, claimDraft, turn, modelCurrent, modelSet, failureBar, bootError, upShade, ctxMenu, copyToClip, nativeCtxOk, tipEl, tipFor, tipPlace, tipHide, dragBand, lastTextNodeIn }
