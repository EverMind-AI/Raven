/* ══ theme ════════════════════════════════════════════════════════ */
function toggleTheme() {
  const now = document.documentElement.dataset.theme
    || (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light');
  const next = now === 'dark' ? 'light' : 'dark';
  document.documentElement.dataset.theme = next;
  CFG.theme = next;
  shellTheme();
  if (setIsOpen()) drawSettings();
}

/* An IME sends its keystrokes as keydown too, so while a composition is open
   Enter belongs to the input method: it commits the candidate being typed
   (letters included, which is how CJK users type Latin). Acting on it would
   swallow the text the reader was in the middle of writing. Every Enter
   handler over a text field asks this first. keyCode 229 is the older spelling
   some IMEs still send instead of isComposing. */
const composing = (e) => !!(e.isComposing || e.keyCode === 229);

/* Same reason as the copy button below: prose is replaced on every answer. */
document.addEventListener('click', (e) => {
  const p = e.target.closest && e.target.closest('code.pth');
  if (p) pathOpen(p.dataset.p);
  const a = e.target.closest && e.target.closest('.artf');
  if (a) (a.dataset.d ? dirOpen : pathOpen)(a.dataset.p);
});
document.addEventListener('keydown', (e) => {
  if (e.key !== 'Enter' && e.key !== ' ') return;
  const p = document.activeElement;
  if (!p || !p.classList) return;
  if (p.classList.contains('pth')) { e.preventDefault(); pathOpen(p.dataset.p); }
  if (p.classList.contains('artf')) { e.preventDefault(); (p.dataset.d ? dirOpen : pathOpen)(p.dataset.p); }
});

/* Code blocks come and go with every answer, so the click is caught once here
   rather than bound per block. The text comes from the DOM the reader sees. */
document.addEventListener('click', (e) => {
  const b = e.target.closest && e.target.closest('.cbcp');
  if (!b) return;
  const blk = b.closest('.cblk');
  const pre = blk && blk.querySelector('pre');
  if (!pre) return;
  if (navigator.clipboard) navigator.clipboard.writeText(pre.textContent);
  b.classList.add('ok');
  b.title = T('gui.code.copied');
  b.setAttribute('aria-label', T('gui.code.copied'));
  setTimeout(() => {
    b.classList.remove('ok');
    b.title = T('gui.code.copy');
    b.setAttribute('aria-label', T('gui.code.copy'));
  }, 1500);
});

document.addEventListener('keydown', (e) => {
  /* Escape ends an open composition; it must not also close a panel or halt the
     running turn behind the reader's back. */
  if (composing(e)) return;
  const inField = /INPUT|TEXTAREA/.test(document.activeElement.tagName);
  if (e.key === 'Escape') {
    if (document.querySelector('.lightbox')) return closeImage();
    if ($('#veil').dataset.open === 'true') return $('#cfNo').click();
    /* After the confirm veil, before the page: a dialog raised over the entry
       list is what Escape should take back first. */
    if ($('#connVeil').dataset.open === 'true') return connCloseDialog();
    if ($('#detail').dataset.open === 'true') return closeDetail();
    if ($('#jobVeil').dataset.open === 'true') return $('#jobNo').click();
    if ($('#cronPage').dataset.open === 'true') return closeCron();
    if ($('#memPage').dataset.open === 'true') return closeMem();
    if ($('#capsPage').dataset.open === 'true') return closeCaps();
    if ($('#xaPage').dataset.open === 'true') return closeXa();
    if ($('#connPage').dataset.open === 'true') return closeConn();
    if (setIsOpen()) return closeSet();
    if (busy) return halt();
  }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'f') {
    e.preventDefault(); setRail(true); toggleFind(true);
  }
  if ((e.metaKey || e.ctrlKey) && e.key === '\\') {
    e.preventDefault(); setRail(document.querySelector('.app').dataset.rail === 'off');
  }
  if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'n' && !inField) { e.preventDefault(); $('#newBtn').click(); }
});

$('#newBtn').onclick = () => {
  showPage(null);
  const s = { id: 'n' + Date.now(), title: '新任务', last: '还没开始', when: '刚刚', run: null };
  SESS.unshift(s); cur = s.id; drawList(); openSession(s); ta.focus();
};
$('#renameBtn').onclick = () => renameTitle();

$('#sfind').oninput = () => {
  query = $('#sfind').value.trim().toLowerCase();
  $('#sclr').hidden = !query;
  drawList();
};
$('#sclr').onclick = () => { $('#sfind').value = ''; query = ''; $('#sclr').hidden = true; drawList(); $('#sfind').focus(); };

/* Search is a chore, so it hides until asked for; leaving it empty and
   clicking away puts the row back. */
function toggleFind(force) {
  const box = $('#findBox');
  const open = force != null ? force : box.hidden;
  box.hidden = !open;
  $('#findBtn').setAttribute('aria-expanded', String(open));
  if (open) $('#sfind').focus();
  else if (query) { $('#sfind').value = ''; query = ''; $('#sclr').hidden = true; drawList(); }
}
$('#findBtn').onclick = () => toggleFind();
$('#sfind').onkeydown = (e) => {
  if (composing(e)) return;
  if (e.key === 'Escape') { e.stopPropagation(); toggleFind(false); }
};
$('#sfind').onblur = () => { if (!query) toggleFind(false); };

/* ══ overlay scrollbars ══════════════════════════════════════════════
   One implementation for every scroller in the app, installed once by
   listening for scroll in the capture phase -- a panel drawn later, or a list
   that only exists while a dialog is open, is covered without registering
   anything. Thumbs are created on demand and parked in a fixed layer, so they
   float over the content instead of taking a column out of it.

   SB_HIDE is how long a bar stays after the last scroll event. Long enough to
   read where you are in a long transcript, short enough that it is gone before
   you look at the layout again. */
const SB_HIDE = 900;
const SB_MIN = 26;
const SB_PAD = 2;
const sbLayer = mk('div', 'sbars');
document.body.appendChild(sbLayer);
const sbMap = new WeakMap();

function sbRoot(el) {
  return el === document || el === document.documentElement || el === window
    ? document.scrollingElement : el;
}

function sbBars(el) {
  let b = sbMap.get(el);
  if (!b) {
    b = { v: null, h: null, timer: 0, drag: null };
    sbMap.set(el, b);
  }
  return b;
}

function sbThumb(bars, axis) {
  if (bars[axis]) return bars[axis];
  const t = mk('div', 'sbar');
  t.dataset.axis = axis;
  sbLayer.appendChild(t);
  bars[axis] = t;
  return t;
}

/* Geometry is read from the element every time rather than cached: a scroller
   can be resized, moved by a panel drag, or re-rendered under the same node. */
function sbPlace(el, bars, axis) {
  const vert = axis === 'v';
  const size = vert ? el.clientHeight : el.clientWidth;
  const full = vert ? el.scrollHeight : el.scrollWidth;
  if (!el.isConnected || full <= size + 1 || size < 40) {
    if (bars[axis]) { bars[axis].remove(); bars[axis] = null; }
    return;
  }
  const r = el.getBoundingClientRect();
  if (!r.width || !r.height) return;
  const t = sbThumb(bars, axis);
  const track = (vert ? r.height : r.width) - SB_PAD * 2;
  const len = Math.max(SB_MIN, Math.round(track * (size / full)));
  const pos = (vert ? el.scrollTop : el.scrollLeft) / (full - size);
  const off = SB_PAD + Math.round((track - len) * Math.min(1, Math.max(0, pos)));
  if (vert) {
    t.style.cssText = `top:${r.top + off}px;left:${r.right - 8}px;width:6px;height:${len}px`;
  } else {
    t.style.cssText = `left:${r.left + off}px;top:${r.bottom - 8}px;height:6px;width:${len}px`;
  }
  sbDrag(t, el, axis);
  return t;
}

/* Whoever is showing a bar right now. A scroller that moved without being
   scrolled -- because an ancestor scrolled, or a panel next to it was dragged
   wider -- still has to have its thumb put back where the box now is. */
const sbLive = new Set();

function sbShow(el) {
  const bars = sbBars(el);
  ['v', 'h'].forEach((a) => {
    const t = sbPlace(el, bars, a);
    if (t) t.dataset.on = 'true';
  });
  sbLive.add(el);
  clearTimeout(bars.timer);
  bars.timer = setTimeout(() => {
    /* A bar being dragged must not time out from under the pointer. */
    if (bars.drag) return;
    ['v', 'h'].forEach((a) => { if (bars[a]) bars[a].dataset.on = 'false'; });
    sbLive.delete(el);
  }, SB_HIDE);
}

function sbSync(skip) {
  sbLive.forEach((el) => {
    if (el === skip) return;
    if (!el.isConnected) { sbHide(el); return; }
    const bars = sbBars(el);
    ['v', 'h'].forEach((a) => { if (bars[a]) sbPlace(el, bars, a); });
  });
}

function sbHide(el) {
  const bars = sbMap.get(el);
  sbLive.delete(el);
  if (!bars) return;
  clearTimeout(bars.timer);
  ['v', 'h'].forEach((a) => { if (bars[a]) { bars[a].remove(); bars[a] = null; } });
}

/* The native bar is gone, so the thumb has to be draggable itself or scrolling
   by grabbing the bar -- the one gesture a trackpad cannot do -- would be lost. */
function sbDrag(t, el, axis) {
  if (t._wired) return;
  t._wired = true;
  t.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    e.stopPropagation();
    const vert = axis === 'v';
    const bars = sbBars(el);
    const start = vert ? e.clientY : e.clientX;
    const from = vert ? el.scrollTop : el.scrollLeft;
    const size = vert ? el.clientHeight : el.clientWidth;
    const full = vert ? el.scrollHeight : el.scrollWidth;
    const track = (vert ? el.clientHeight : el.clientWidth) - SB_PAD * 2;
    const len = Math.max(SB_MIN, track * (size / full));
    bars.drag = true;
    t.dataset.drag = 'true';
    /* A pointer can be gone by the time this runs (released mid-dispatch, or a
       synthetic event); capture is an optimisation, not the drag itself. */
    try { t.setPointerCapture(e.pointerId); } catch { /* not capturable */ }
    document.body.style.userSelect = 'none';
    const move = (ev) => {
      const d = (vert ? ev.clientY : ev.clientX) - start;
      const ratio = (full - size) / Math.max(1, track - len);
      if (vert) el.scrollTop = from + d * ratio;
      else el.scrollLeft = from + d * ratio;
    };
    const up = () => {
      t.removeEventListener('pointermove', move);
      t.removeEventListener('pointerup', up);
      t.removeEventListener('pointercancel', up);
      delete t.dataset.drag;
      bars.drag = null;
      document.body.style.userSelect = '';
      sbShow(el);
    };
    t.addEventListener('pointermove', move);
    t.addEventListener('pointerup', up);
    t.addEventListener('pointercancel', up);
  });
}

document.addEventListener('scroll', (e) => {
  const el = sbRoot(e.target);
  if (!el || el.nodeType !== 1) return;
  sbShow(el);
  sbSync(el);
}, true);
/* Resizing moves every box at once, and a bar mid-fade would be left hanging
   over whatever landed under it. */
window.addEventListener('resize', () => { sbLive.forEach(sbHide); });

/* ══ resizable panels ════════════════════════════════════════════════
   Both panels drag the same way, so the differences are data: which variable
   the grip writes, which direction widens it, and the bounds. The floor and
   ceiling are not decoration -- a 90px rail cannot show a session title and a
   900px panel leaves no transcript -- and the ceiling is additionally clamped
   against the window so a drag can never squeeze the chat below its floor. */
const PANE = {
  rail: { v: '--rail', min: 190, max: 420, key: 'raven.gui.railw', edge: 'left' },
  ws:   { v: '--wsw',  min: 320, max: 760, key: 'raven.gui.wsw',   edge: 'right' }
};
/* The chat's floor is a CSS token because the grid enforces it too -- read it
   back instead of keeping a second copy of the number here. */
const cssPx = (name) => parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || 0;
function paneMax(p) {
  const railOff = document.querySelector('.app').dataset.rail === 'off';
  const taken = p.edge === 'right' && !railOff ? document.querySelector('.rail').offsetWidth : 0;
  return Math.max(p.min, Math.min(p.max, window.innerWidth - taken - cssPx('--chat-min')));
}
function paneSet(name, px, persist) {
  const p = PANE[name];
  const w = Math.round(Math.max(p.min, Math.min(paneMax(p), px)));
  document.documentElement.style.setProperty(p.v, w + 'px');
  if (persist) { try { localStorage.setItem(p.key, String(w)); } catch {} }
  sbSync();
  return w;
}
/* A width stored on a wide screen must not survive onto a narrow one unusably,
   so the stored value is re-clamped rather than trusted. */
function paneLoad() {
  Object.keys(PANE).forEach((name) => {
    let v = null;
    try { v = localStorage.getItem(PANE[name].key); } catch {}
    if (v) paneSet(name, parseFloat(v), false);
  });
}
function gripDrag(el, name) {
  const p = PANE[name];
  el.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    const startX = e.clientX;
    const start = cssPx(p.v) || p.min;
    el.dataset.drag = 'true';
    el.setPointerCapture(e.pointerId);
    /* While dragging, the pointer is over the transcript half the time; without
       this the drag keeps selecting text under it. */
    document.body.style.userSelect = 'none';
    const move = (ev) => {
      const d = ev.clientX - startX;
      paneSet(name, start + (p.edge === 'right' ? -d : d), false);
      dockLift();
    };
    const up = () => {
      el.removeEventListener('pointermove', move);
      el.removeEventListener('pointerup', up);
      el.removeEventListener('pointercancel', up);
      delete el.dataset.drag;
      document.body.style.userSelect = '';
      paneSet(name, cssPx(p.v), true);
    };
    el.addEventListener('pointermove', move);
    el.addEventListener('pointerup', up);
    el.addEventListener('pointercancel', up);
  });
  /* Keyboard: the grip is a real separator, so arrows move it. */
  el.tabIndex = 0;
  el.addEventListener('keydown', (e) => {
    const step = e.shiftKey ? 40 : 10;
    const cur = cssPx(p.v) || p.min;
    if (e.key === 'ArrowLeft') { e.preventDefault(); paneSet(name, cur + (p.edge === 'right' ? step : -step), true); }
    if (e.key === 'ArrowRight') { e.preventDefault(); paneSet(name, cur + (p.edge === 'right' ? -step : step), true); }
  });
}
gripDrag($('#railGrip'), 'rail');
gripDrag($('#wsGrip'), 'ws');
/* Shrinking the window must re-clamp both, or the chat loses its floor. */
window.addEventListener('resize', () => {
  Object.keys(PANE).forEach((n) => {
    paneSet(n, cssPx(PANE[n].v) || PANE[n].min, false);
  });
});

const setRail = (on) => {
  const app = document.querySelector('.app');
  app.dataset.rail = on ? 'on' : 'off';
  document.documentElement.dataset.rail = on ? 'on' : 'off';
  $('#railShow').hidden = on;
};
// Collapsing the rail is the user's call, never the window's: it holds the
// session list, and having it vanish on resize loses your place.
$('#railBtn').onclick = () => setRail(false);
$('#railShow').onclick = () => setRail(true);
// Shrinking past the split point must not leave the conversation hidden.
const tooNarrowToSplit = matchMedia('(max-width: 1040px)');
tooNarrowToSplit.addEventListener('change', (e) => { if (e.matches && wsOpen) setWs(false); });

/* ---- the foot row --------------------------------------------------
   One door to settings. The sub-line carries the running build, which is the
   single fact the old identity row was actually showing anyone. */

/* The one door to settings. live.js reassigns it to refresh the server's
   config before drawing -- same pattern as applyLang. */
let openSettings = async () => { drawSettings(); openSet(); };

function drawFoot() {
  const sub = $('#meSub');
  if (sub) sub.textContent = `Raven ${APP_VERSION ? 'v' + APP_VERSION : '--'}`;
  const kbd = $('#meKbd');
  if (kbd) kbd.textContent = `${modKey()}${isMac() ? '' : ' '},`;
}

$('#meBtn').onclick = () => openSettings();

$('#modelChip').onclick = () => {
  const r = $('#modelChip').getBoundingClientRect();
  const items = [];
  PROVIDERS.filter((p) => p.on).forEach((p) => p.models.forEach((m) => items.push({
    label: m === model ? `${m} ✓` : m,
    // No toast: the chip right there already shows the new model.
    fn: () => { model = m; $('#modelName').textContent = m; }
  })));
  items.push('-', { label: T('gui.slash.manage_models'), fn: () => { sTab = 'model'; drawSettings(); openSet(); } });
  menuAt(r.left, r.bottom + 6, items);
};

$('#permChip').onclick = () => ($('#permPop').dataset.open === 'true' ? closePermPop() : openPermPop());

/* Arrows, not references: live.js swaps openCaps for one that loads real data
   first, and a stored reference would keep calling the demo. */
$('#skillBtn').onclick = () => openSkills();
$('#plugBtn').onclick = () => openPlugins();
/* wrapper, not the reference: live.js replaces openMem with the RPC loader */
$('#memBtn').onclick = () => openMem();

/* ── the 更多 flyout ──────────────────────────────────────────────────
   连接 / 入口 / 定时 live here. Arrows for the same reason as above: live.js
   replaces every one of these openers with an RPC-loading version. */
const MORE_ROWS = [
  ['xaPage', 'gui.nav.agents',
    '<rect x="3.5" y="4" width="7" height="7" rx="1.6"/><rect x="13.5" y="13" width="7" height="7" rx="1.6"/><path d="M10.5 7.5h3.5a3 3 0 0 1 3 3v2.5"/>',
    () => openXa()],
  ['connPage', 'gui.nav.conn',
    '<path d="M9.5 14.5 6.8 17.2a3.3 3.3 0 0 1-4.7-4.7l2.7-2.7M14.5 9.5l2.7-2.7a3.3 3.3 0 0 1 4.7 4.7l-2.7 2.7M9 15l6-6"/>',
    () => openConn()],
  ['cronPage', 'gui.nav.cron',
    '<circle cx="12" cy="12.5" r="7.5"/><path d="M12 8.5v4.2l2.6 1.6M9 2.5h6"/>',
    () => openCron()],
];

function drawMoreFly() {
  const fly = $('#moreFly');
  fly.innerHTML = '';
  MORE_ROWS.forEach(([page, nameKey, path, go]) => {
    const b = mk('button', 'mrow');
    b.setAttribute('aria-current', String($('#' + page).dataset.open === 'true'));
    b.innerHTML = `<svg viewBox="0 0 24 24" aria-hidden="true">${path}</svg>`;
    /* Names only. These three rows are places the reader already knows by
       name; a sentence under each turned a three-item group into a panel. */
    b.appendChild(mk('div', 'nm', T(nameKey)));
    /* The group stays open on a pick: it is navigation now, and the row's own
       current mark is the answer to "where am I". */
    b.onclick = () => { go(); markNewCurrent(); };
    fly.appendChild(b);
  });
}

function toggleMoreFly(force) {
  const fly = $('#moreFly');
  const open = force != null ? force : fly.dataset.open !== 'true';
  if (open) drawMoreFly();
  fly.dataset.open = String(open);
  $('#moreBtn').setAttribute('aria-expanded', String(open));
  markNewCurrent();
}
$('#moreBtn').onclick = (e) => { e.stopPropagation(); toggleMoreFly(); };
/* The platform's own shortcut, same door as the foot row. */
document.addEventListener('keydown', (e) => {
  if (e.key !== ',' || !(isMac() ? e.metaKey : e.ctrlKey)) return;
  e.preventDefault();
  if (setIsOpen()) return closeSet();
  openSettings();
});
$('#setClose').onclick = () => closeSet();
$('#setVeil').onclick = (e) => { if (e.target === $('#setVeil')) closeSet(); };
$('#dClose').onclick = closeDetail;
/* The scrim closes the sheet; calls through the name so late rebinds win. */
$('#detail').addEventListener('click', (e) => { if (e.target === $('#detail')) closeDetail(); });

$('#cq').oninput = () => { cQuery = $('#cq').value.trim().toLowerCase(); drawCaps(); };
$('#cKind').onclick = (e) => {
  const b = e.target.closest('button'); if (!b) return;
  cKind = b.dataset.k;
  [...$('#cKind').children].forEach((c) => c.setAttribute('aria-pressed', String(c === b)));
  drawCaps();
};
$('#mAdd').onclick = () => {
  const n = $('#mName').value.trim(), a = $('#mAddr').value.trim();
  if (!n || !a) { toast(T('gui.adv.need_fields')); return; }
  PLUGINS.push({ id: 'x' + Date.now(), name: n, reach: 'auth', state: 'on', glyph: '◆',
    one: '手动添加的插件', src: a, ver: '—', cat: '我的系统', tools: [], perms: ['联网'] });
  $('#mName').value = ''; $('#mAddr').value = '';
  drawCaps(); drawCapsBadge(); toast(T('gui.adv.added_x', { name: n }));
};

