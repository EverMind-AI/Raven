/* ══ module 2: capabilities page ══════════════════════════════════ */
let extTab = 'skill', cKind = 'all', cQuery = '';

/* Only one module page at a time. They used to cover the whole window, so
   two open at once was invisible; now that the rail stays put, the one behind
   shows through. */
const NAV_OF = {
  capsPage: () => (extTab === 'plugin' ? 'plugBtn' : 'skillBtn'),
  xaPage: 'moreBtn',
  connPage: 'moreBtn',
  memPage: 'memBtn',
  cronPage: 'moreBtn',
};

function showPage(id) {
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
}

/* Switching module resets the filters: a query typed while browsing skills is
   not a question about plugins. */
function extSet(tab) {
  if (!tab || tab === extTab) return;
  extTab = tab; cKind = 'all'; cQuery = '';
  $('#cq').value = '';
  [...$('#cKind').children].forEach((c, i) => c.setAttribute('aria-pressed', String(i === 0)));
  closeDetail();
}

function openCaps(tab) { extSet(tab); showPage('capsPage'); drawCaps(); }
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

function closeDetail() { $('#detail').dataset.open = 'false'; }

/* ══ module 2b: external agents ════════════════════════════════════
   The renderer is the xa island (ui/src/features/xa/); what remains here
   is its shell face -- the names the More row, the Esc handler and the
   live layer's redrawAll still call -- and the fixture source. The island
   owns everything drawn inside the body and its sheet in the shared
   detail drawer. */
function openXa() { RavenIslands.xa.open(); }
function closeXa() { RavenIslands.xa.close(); }
function drawXa() {
  /* A language flip re-renders #xaBody with the new catalogue. */
  RavenIslands.xa.redraw();
}

/* The fixture rows. A row is (name, kind, configured, enabled, probe_status,
   last test). Three facts, deliberately kept apart: `configured` is "Raven
   knows about it", `enabled` is "Raven may dispatch to it", and
   `probe_status` is "the machine can actually run it". Collapsing them is
   how a disabled agent reads as broken, or a missing binary reads as
   switched off. */
const XA_FIXTURE = [
  { name: 'Raven-Research', preset: null, kind: 'cli', configured: false, builtin: false, enabled: true,
    probe_status: 'ready', probe_detail: '', has_api_key: false, test_running: false,
    last_test_ok: null, last_test_at_ms: null, last_test_detail: '', upgrade_to: null,
    description: 'A vendored build, discovered under subagents/ and registered as cli.' },
  { name: 'Raven-PPT', preset: null, kind: 'cli', configured: false, builtin: false, enabled: false,
    probe_status: 'unknown', probe_detail: '', has_api_key: false, test_running: false,
    last_test_ok: null, last_test_at_ms: null, last_test_detail: '', upgrade_to: null,
    description: 'A vendored build whose venv is not built yet, so it is listed and disabled.' },
  { name: 'raven', preset: null, kind: 'builtin', configured: false, builtin: true, enabled: true,
    probe_status: 'ready', probe_detail: '', has_api_key: false, test_running: false,
    last_test_ok: null, last_test_at_ms: null, last_test_detail: '', upgrade_to: null,
    description: 'General-purpose sub-agent with no capability bias.' },
  { name: 'claude_code', preset: 'claude_code', kind: 'cli', configured: true, enabled: true,
    probe_status: 'ready', probe_detail: '', has_api_key: false, test_running: false,
    last_test_ok: true, last_test_at_ms: Date.now() - 3600e3, last_test_detail: '',
    upgrade_to: null, description: 'Claude Code CLI - strong general coding / agent tasks.' },
  { name: 'codex', preset: 'codex', kind: 'cli', configured: false, enabled: false,
    probe_status: 'missing', probe_detail: 'codex: command not found', has_api_key: false,
    test_running: false, last_test_ok: null, last_test_at_ms: null, last_test_detail: '',
    description: 'OpenAI Codex CLI - coding tasks.' },
  { name: 'hermes', preset: 'hermes', kind: 'cli', configured: false, enabled: false,
    probe_status: 'ready', probe_detail: '', has_api_key: false, test_running: false,
    last_test_ok: null, last_test_at_ms: null, last_test_detail: '',
    description: 'Hermes Agent CLI - general assistant with tool calling.' },
  { name: 'mirothinker', preset: 'mirothinker', kind: 'openai', configured: false, enabled: false,
    probe_status: 'unknown', probe_detail: '', has_api_key: false, test_running: false,
    last_test_ok: null, last_test_at_ms: null, last_test_detail: '',
    description: 'MiroMind deep-research (OpenAI-compatible HTTP).' },
];

/* The fixture source: mutate the row in place so the page is still explorable
   with no gateway behind it, and answer with the same array every time, which
   is what makes those edits stick across a redraw. */
DS.xa ??= {
  load: async () => XA_FIXTURE,
  act: async (op, row, args) => {
    const a = args || {};
    if (op === 'connect') { row.configured = true; row.enabled = row.kind !== 'openai'; }
    if (op === 'remove') { row.configured = false; row.enabled = false; }
    if (op === 'upgrade') { row.kind = row.upgrade_to || row.kind; row.upgrade_to = null; }
    if (op === 'toggle') row.enabled = !!a.enabled;
    if (op === 'update') {
      if (a.new_name) row.name = a.new_name;
      if (a.description != null) row.description = a.description || row.description;
      if (a.api_key) { row.has_api_key = true; row.enabled = true; }
    }
    if (op === 'test') {
      row.last_test_ok = row.probe_status === 'ready';
      row.last_test_at_ms = Date.now();
    }
    return XA_FIXTURE;
  },
};

/* The rail badge for capabilities that need attention. The composer used to
   carry a chip counting active capabilities too; it is gone -- how many tools
   are wired is a setup question, answered on the extensions page, not something
   to read while typing. What belongs under the field is the state of THIS
   session: how much context is left, and what the agent may do unasked. */
/* No counters on the rail's module rows -- the set-up-once modules do not
   nag from there; their state is spoken inside the page, where the fix is. */
function drawCapsBadge() {}

/* The context ring is the ctxchip writer (ui/src/shell/ctxchip.ts), which owns
   the two numbers as well as the drawing; drawCtx and setCtx are its published
   names, assigned in main.tsx. */
