/* An IME sends its keystrokes as keydown too, so while a composition is open
   Enter belongs to the input method: it commits the candidate being typed
   (letters included, which is how CJK users type Latin). Acting on it would
   swallow the text the reader was in the middle of writing. Every Enter
   handler over a text field asks this first. keyCode 229 is the older spelling
   some IMEs still send instead of isComposing. */
const composing = (e) => !!(e.isComposing || e.keyCode === 229);

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
    if (turn.busy()) return DS.composer.stop();
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
  sessionRows().unshift(s); sessionSet(s.id); sessionDraw(); sessionOpen(s); ta.focus();
};
$('#renameBtn').onclick = () => renameTitle();

/* ---- the search row ------------------------------------------------
   Owned by ui/src/shell/find.ts, which holds the term and publishes
   toggleFind(). The Cmd+F handler above stays here because showing the rail
   first is a chrome decision, and it is the only caller from this side. */

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
   One door to settings. What the row says -- the running build and this
   platform's shortcut for that door -- is written by the foot module
   (ui/src/shell/foot.ts), which publishes drawFoot(); the door itself is
   here, because the dialog behind it is. */

/* The one door to settings. The island's source owns the refresh that must
   happen before drawing, so both modes use the same opener. */
const openSettings = async () => { await RavenIslands.settings.open(); };

$('#meBtn').onclick = () => openSettings();

$('#modelChip').onclick = () => {
  const r = $('#modelChip').getBoundingClientRect();
  const items = [];
  PROVIDERS.filter((p) => p.on).forEach((p) => p.models.forEach((m) => items.push({
    label: m === modelCurrent() ? `${m} ✓` : m,
    // No toast: the chip right there already shows the new model.
    fn: () => { modelSet(m); $('#modelName').textContent = m; }
  })));
  items.push('-', { label: T('gui.slash.manage_models'), fn: () => { sTab = 'model'; drawSettings(); openSet(); } });
  menuAt(r.left, r.bottom + 6, items);
};

$('#permChip').onclick = () => togglePerm();

/* Arrows, not references: live.js swaps openCaps for one that loads real data
   first, and a stored reference would keep calling the demo. */
$('#skillBtn').onclick = () => openSkills();
$('#plugBtn').onclick = () => openPlugins();
/* wrapper, not the reference: live.js replaces openMem with the RPC loader */
$('#memBtn').onclick = () => openMem();

/* ── the 更多 flyout ──────────────────────────────────────────────────
   连接 / 入口 / 定时 live here. The renderer is the nav flyout module
   (ui/src/shell/navfly.ts), which also owns the button that opens the group;
   what remains here is the one name the live layer still calls. */
function drawMoreFly() {
  /* A language flip re-runs the MORE_ROWS.forEach that names the rows. */
  RavenIslands.nav.draw();
}
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
$('#mAdd').onclick = async () => {
  const n = $('#mName').value.trim(), a = $('#mAddr').value.trim();
  if (!n || !a) { toast(T('gui.adv.need_fields')); return; }
  try { await DS.plugins.manual(n, a); }
  catch (e) { toast(T('gui.plug.op_failed', { err: e.message || e })); return; }
  $('#mName').value = ''; $('#mAddr').value = '';
  drawCaps(); drawCapsBadge(); toast(T('gui.adv.added_x', { name: n }));
};
