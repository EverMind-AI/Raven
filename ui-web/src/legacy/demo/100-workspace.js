/* ══ workspace ════════════════════════════════════════════════════
   The transcript answers "what happened". This panel answers "what is the
   state of my machine now" -- which files the agent rewrote, what it ran in
   a shell, where it went on the web. Anything that only restates the
   transcript belongs in the transcript, not here.

   It never opens itself: a pane that interrupts gets closed for good. The
   badge on the header chip does the asking. And it is never the only place a
   fact appears, so collapsing it can't lose information.

   The renderer and shared state are owned by the workspace island
   (ui-web/src/features/workspace/); what stays here is the tool-event bookkeeping
   the fixture hooks feed, the panel chrome outside #wsBody, and the fixture DS
   source. */
let wsTab = 'diff', wsOpen = false, wsWide = false, wsPicked = false;

const WS = RavenIslands.workspace.shared();

function wsReset() {
  wsTab = 'diff'; wsPicked = false;
  /* A different session is a different workspace state: the island clears the
     record and drops the file tree listings it read before the switch. */
  RavenIslands.workspace.reset();
  /* Subagents belong to the session that spawned them, so they leave with it
     -- carrying the list into the next conversation would attribute one
     conversation's background work to another. The open dag node goes for the
     same reason, and because `dag.node` is addressed by session: left set, the
     panel would ask the newly opened conversation for a run it never made. */
  RavenIslands.subagents.reset();
}

/* ── resumed sessions ──────────────────────────────────────────────────
   The panel is rebuilt from the stored calls rather than starting empty after a
   reload: session.resume carries every assistant tool_call WITH its arguments,
   which is the same input the live hooks are fed, so replaying them yields the
   same rows, the same diffs and the same turn grouping. */
function wsOnHistory(messages) {
  /* The stored tool entries carry the real unified diff when the tool reported
     one; keyed here so each replayed call can swap its argument-guessed hunk
     for the numbered rows, exactly as the live completion event does. */
  const diffs = new Map();
  (messages || []).forEach((m) => {
    if (m && m.role === 'tool' && m.tool_call_id && m.diff) diffs.set(String(m.tool_call_id), m.diff);
  });
  (messages || []).forEach((m) => {
    if (!m) return;
    if (m.role === 'user' && m.delegated) {
      /* A delegated result re-entering counts as one turn here too -- a live
         client advances on turn.started, and without the same step on replay
         a reloaded session files the delegated reaction's files under its
         parent's turn. Mirrors the rule in features/transcript/store.ts. An
         origin-only entry (cron, sentinel) does NOT: it opens no workspace
         turn there either. */
      WS.turn += 1; return;
    }
    if (m.role === 'user' && m.text && m.text.trim()) { WS.turn += 1; return; }
    if (m.role !== 'assistant' || !Array.isArray(m.tool_calls)) return;
    m.tool_calls.forEach((c) => {
      let args = null;
      try { args = JSON.parse(c.arguments || '{}'); } catch (e) { return; }
      if (!args || typeof args !== 'object') return;
      const name = String(c.name || '');
      wsOnTool(name, args, true);
      const diff = diffs.get(String(c.id || ''));
      if (diff) wsOnToolDone(name, args, true, '', null, diff);
    });
  });
  /* Restored rows have no completion event coming, and nothing counts as
     unread because none of it arrived while the reader was away. */
  WS.urls.forEach((u) => { u.at = T('gui.ws.turn_earlier'); });
  WS.changes.forEach((c) => { c.seen = true; });
  WS.unseen = 0;
  bumpWs();
}

/* ── diff from the edit arguments ───────────────────────────────────────
   edit_file carries old_text and new_text, which IS the ground truth of the
   change, so the diff needs no backend support at all. Long runs of
   unchanged context are folded to one clickable row. */
const { hunkFromEdit, hunkFromWrite, hunkFromUnified } = RavenIslands.workspace;

/* The two front ends hand over different shapes -- the live RPC gives the
   whole argument object, the demo replay gives the one string it displays.
   Normalising here keeps every caller downstream simple. */
function wsArgs(name, args) {
  if (args && typeof args === 'object') return args;
  const s = String(args == null ? '' : args);
  if (name === 'exec') return { command: s };
  if (name === 'web_fetch') return { url: s };
  if (name === 'web_search') return { query: s };
  if (name === 'spawn') return { label: s };
  return { path: s };
}

/* Tools report absolute paths; the workspace root is the same on every row and
   carries no information. The live layer's source knows the real root. */
function wsShortPath(p) { return DS.workspace.shortPath(p); }

/* One row per path, not per call: five edits to the same file is one changed
   file with five hunks, which is how a person thinks about it. */
function wsRecordChange(path, kind, hunk) {
  const key = String(path);
  let c = WS.changes.find((x) => x.key === key && x.turn === WS.turn);
  if (!c) {
    const shown = wsShortPath(key);
    const cut = shown.lastIndexOf('/');
    /* The newest change is the one you came here to read, so it arrives
       expanded. `auto` marks it as opened by us, so the next arrival folds it
       back without touching a row the reader opened on purpose. */
    WS.changes.forEach((x) => { if (x.auto) { x.open = false; x.auto = false; } });
    c = { key, dir: cut < 0 ? '' : shown.slice(0, cut + 1), name: cut < 0 ? shown : shown.slice(cut + 1),
      kind, add: 0, del: 0, hunks: [], turn: WS.turn, open: true, auto: true, seen: false };
    WS.changes.unshift(c);
  }
  if (kind === 'write') c.kind = 'write';
  c.add += hunk.add; c.del += hunk.del;
  c.hunks.push(hunk);
  return c;
}

function setWs(open, tab) {
  if (open && tab && document.documentElement.classList.contains('desk-ready')) {
    const desk = RavenIslands.workspace && RavenIslands.workspace.openDeskTab;
    if (desk && tab !== 'browser') { desk(tab); return; }
  }
  wsOpen = open;
  if (tab) wsTab = tab;
  /* Collapsing leaves expanded mode too: coming back to a full-window panel
     that was dismissed is never what the reader meant. */
  if (!open) setWsFull(false);
  $('#split').dataset.open = String(open);
  const b = $('#wsBtn');
  b.setAttribute('aria-expanded', String(open));
  const k = open ? 'gui.collapse_ws' : 'gui.expand_ws';
  b.dataset.tip = T(k); b.setAttribute('aria-label', T(k));
  if (open) drawWs();
  bumpWs();
}

/* Expanded is a display mode, not a width: the panel leaves the grid and covers
   the window, so nothing here touches --wsw and the dragged width is waiting
   unchanged on the way back. */
function setWsFull(on) {
  wsWide = !!on;
  $('#split').dataset.full = String(wsWide);
  const b = $('#wsWide');
  b.classList.toggle('on', wsWide);
  const k = wsWide ? 'gui.ws.restore_panel' : 'gui.ws.expand_panel';
  b.dataset.tip = T(k); b.setAttribute('aria-label', T(k));
  b.setAttribute('aria-pressed', String(wsWide));
}

/* The header chip is the only thing allowed to interrupt, and only by
   counting. Running commands add a dot so "still going" reads at a glance.

   The count is CHANGED FILES, not tool calls: five edits to one file is one
   thing to look at, and "+5" for a single file reads as a lie. */
function bumpWs() {
  WS.unseen = WS.changes.filter((c) => !c.seen).length;
  const n = WS.unseen;
  const chip = $('#wsBdg');
  chip.textContent = n ? `+${n}` : '';
  chip.hidden = n === 0;
  const u = $('#wsUnseen');
  if (u) { u.textContent = n ? `+${n}` : ''; u.hidden = n === 0; }
  RavenIslands.workspace.notifyDesk?.();
}

/* Picking a view is a commitment: from then on that view shows its own empty
   note rather than being replaced by the launcher. */
function wsPick(tab) {
  if (document.documentElement.classList.contains('desk-ready')) {
    const desk = RavenIslands.workspace && RavenIslands.workspace.openDeskTab;
    if (desk && tab !== 'browser') { desk(tab); return; }
  }
  wsTab = tab; wsPicked = true;
  drawWs(); bumpWs();
}

/* The pane's state, out and back. The live layer's parked-turn machinery is the
   caller: it saves this when the reader leaves a session mid-turn and hands it
   back on return. Two functions rather than two bindings for that layer to read
   and write -- which view is up and whether the reader chose it belong to the
   panel, and only the panel knows a restore is not a fresh pick. */
function wsView() {
  return { tab: wsTab, open: wsOpen, picked: wsPicked };
}

function wsRestore(tab, picked) {
  wsTab = tab || 'diff';
  wsPicked = !!picked;
}

/* Bumped on every redraw. A view that fetches before it can render must
   re-check this before appending, or a slow answer lands in whatever view the
   user switched to meanwhile. */
let wsEpoch = 0;
const wsStale = (mine) => mine !== wsEpoch;

/* Whether the view on screen draws anything a tool call changes. Redrawing on
   every tool.start and every tool.complete of a running turn -- for a view
   that shows none of that state -- is a panel that flickers once per call for
   the length of the turn. The sub-agent tab shows nothing a tool event
   repaints, so it is exempt. */
const wsShowsTurn = () => wsOpen && wsTab !== 'agents';

function drawWs() {
  /* The island owns #wsBody; the agents and browser views draw into it
     through their own islands, dispatched by the workspace island's draw. */
  wsEpoch += 1;
  [...$('#wsTabs').children].forEach((b) => b.setAttribute('aria-selected', String(b.dataset.w === wsTab)));
  RavenIslands.workspace.draw();
}

/* The fixture source: what the workspace island may ask of demo mode. No
   list/reveal and no canBrowse -- the file tab keeps its demo empty note, and
   opening a change stays the honest toast. Registered, not
   declared-for-override: live mode installs its own DS.workspace and this
   object is never consulted. */
DS.workspace ??= {
  shortPath: (p) => String(p),
  hostPlatform: () => HOST_PLATFORM,
  openPath: (p) => toast(`demo：正式版会用系统默认程序打开 ${p}`),
};

/* ── the turn's products ───────────────────────────────────────────────
   The record's own rows for one turn, unfiltered. Which of them counts as a
   product, and what a tile can draw of it, are the transcript island's to
   decide -- see artifactsOf in features/transcript/store.ts.

   The turn number is the one WS.changes files rows under: bumped per turn by
   the live layer, and per user message with text by wsOnHistory. The
   transcript counts it the same way over the same payload. */
DS.artifacts ??= {
  changes: (turn) => WS.changes.filter((c) => c.turn === turn),
};
