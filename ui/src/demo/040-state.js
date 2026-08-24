/* ══ app state ════════════════════════════════════════════════════ */
sessionSet('a');
let timers = [], use = null;
let rt = 'local', undoBin = null;
const CFG = {
  /* Light until the reader says otherwise: a fresh install has no stored
     preference, and following the system would mean the desktop shell's
     launch splash (decided before this page can report anything) and the
     first painted frame could disagree. 'system' stays available in settings. */
  theme: 'light', motion: 'on', codeFont: 'system'
};

const stop_ = () => { timers.forEach(clearTimeout); timers = []; };
const later = (ms, fn) => timers.push(setTimeout(fn, ms));
/* Follow the stream only while the reader is at the bottom. Scrolling up to
   reread must not be undone by the next token; wheel-up releases the anchor,
   returning to the bottom re-arms it. (wheel, not scroll: the smooth scroll
   we trigger ourselves fires scroll events mid-flight and would unstick.) */
let stick = true;
/* Instant, not smooth: a smooth scroll restarted by every token delta never
   reaches the bottom — each assignment cancels the animation mid-flight. */
/* Where transcript nodes land. Normally the chat column; while a subagent's
   context is being drawn it is that panel's box instead, which is what lets
   one renderer serve both -- a subagent's run is drawn by the very same code
   as the main thread, not a lookalike. Only ever set around a SYNCHRONOUS
   render, so a live event can never land in the wrong column. */
let stageHost = null;
const stageBox = () => stageHost || $('#stage');
const down = () => {
  /* Drawing into the panel must not yank the conversation to its tail. */
  if (!stick || stageHost) return;
  const s = $('#scroll');
  s.scrollTo({ top: s.scrollHeight, behavior: 'instant' });
};
const sess = (id) => sessionRows().find((s) => s.id === id);

/* ══ composer drafts ══════════════════════════════════════════════
   Storage and ownership live in RavenIslands.composer. */
/* ══ confirm dialog ═══════════════════════════════════════════════ */
let cfFn = null;
function confirmAsk(title, body, label, fn) {
  $('#cfTitle').textContent = title;
  $('#cfBody').textContent = body;
  $('#cfYes').textContent = label;
  cfFn = fn;
  $('#veil').dataset.open = 'true';
  $('#cfNo').focus();
}
$('#cfNo').onclick = () => { $('#veil').dataset.open = 'false'; cfFn = null; };
$('#cfYes').onclick = () => { $('#veil').dataset.open = 'false'; if (cfFn) cfFn(); cfFn = null; };
$('#veil').onclick = (e) => { if (e.target === $('#veil')) $('#cfNo').click(); };

/* ══ session-scoped sheets ════════════════════════════════
   The rack that holds everything docking above the composer -- a clarify
   question, an approval request, a dag graph -- is the composer island's now,
   filed per conversation so that switching sessions cannot leave another one's
   question sitting over the field. See ui/src/features/composer/sheets.ts for
   what that scoping is for and what it does not fix.

   One destructure, at this layer's top level so both layers see it: the live
   parts are an IIFE nested in this script, and the four of them that raise or
   retire a sheet keep calling these by name. */
const { sheetSession, sheetAdd, sheetRemove, sheetDropClass, sheetsSync, sheetsForget,
  approveSheet, clarifySheet, drawQueue: queueDraw, queuePush, queueShift,
  queueClear, queueSnapshot, queueRestore, parkDraft, loadDraft, dropDraft,
  claimDraft, turn } = RavenIslands.composer;
const { current: modelCurrent, setCurrent: modelSet } = RavenIslands.model;
/* Bound at the shared top level because the live parts run in the IIFE nested
   below it. The writers themselves stay in the modern bundle. */
const { failureBar, bootError, upShade } = RavenIslands.chrome;


/* Preview without an engine: __approve('rm -rf build/'). */
window.__approve = (p) => approveSheet(p || 'rm -rf build/',
  () => toast(T('gui.confirm.allow')), () => toast(T('gui.confirm.deny')));

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
}, true);

/* ── the hover pill ────────────────────────────────────────────────────
   The one .tipp element, driven from here. Placement prefers above (below for
   .tipdn), flips when the preferred side leaves the window, and clamps to the
   viewport either way. */
const tipEl = mk('div', 'tipp');
document.body.appendChild(tipEl);
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

/* Shell window drag: the OS title bar is hidden, so the header band has to do
   its job. WebKit's implicit background-drag heuristic proved fragile (it
   silently stopped once the band gained positioned content), so the shell is
   told explicitly -- grab anywhere in the band that is not a control. */
const dragBand = (e) => {
  if (document.documentElement.dataset.shell !== '1' || e.button !== 0) return false;
  if (!e.target.closest('.railtop, .top, .ws-top')) return false;
  return !e.target.closest('button, a, input, textarea, select, [role="link"], .grip');
};
document.addEventListener('mousedown', (e) => {
  if (!dragBand(e)) return;
  try { window.webkit.messageHandlers.raven.postMessage({ type: 'drag' }); } catch { /* browser */ }
});
document.addEventListener('dblclick', (e) => {
  if (!dragBand(e)) return;
  try { window.webkit.messageHandlers.raven.postMessage({ type: 'zoom' }); } catch { /* browser */ }
});

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
