/* ══ workspace ════════════════════════════════════════════════════
   The transcript answers "what happened". This panel answers "what is the
   state of my machine now" -- which files the agent rewrote, what it ran in
   a shell, where it went on the web. Anything that only restates the
   transcript belongs in the transcript, not here.

   It never opens itself: a pane that interrupts gets closed for good. The
   badge on the header chip does the asking. And it is never the only place a
   fact appears, so collapsing it can't lose information.

   The renderer and shared state are owned by the workspace island
   (ui-web/src/features/workspace/), and what the panel is a record of -- the
   changed-file rows, the pages read, the turn each belongs to -- is that
   feature's own module (features/workspace/record.ts). What stays here is the
   panel chrome outside #wsBody. */

import { islands } from '../../islands'
import { sources } from '../../state/sources'
import { $, T } from './010-kernel.js'

let wsTab = 'diff', wsOpen = false, wsWide = false, wsPicked = false;

let WS;

function wsReset() {
  wsTab = 'diff'; wsPicked = false;
  /* A different session is a different workspace state: the island clears the
     record and drops the file tree listings it read before the switch. */
  islands.workspace.reset();
  /* Subagents belong to the session that spawned them, so they leave with it
     -- carrying the list into the next conversation would attribute one
     conversation's background work to another. The open dag node goes for the
     same reason, and because `dag.node` is addressed by session: left set, the
     panel would ask the newly opened conversation for a run it never made. */
  islands.subagents.reset();
}

/* Tools report absolute paths; the workspace root is the same on every row and
   carries no information. The workspace source knows the real root. */
function wsShortPath(p) { return sources.workspace.shortPath(p); }

function setWs(open, tab) {
  if (open && tab && document.documentElement.classList.contains('desk-ready')) {
    const desk = islands.workspace && islands.workspace.openDeskTab;
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
  islands.workspace.notifyDesk?.();
}

/* Picking a view is a commitment: from then on that view shows its own empty
   note rather than being replaced by the launcher. */
function wsPick(tab) {
  if (document.documentElement.classList.contains('desk-ready')) {
    const desk = islands.workspace && islands.workspace.openDeskTab;
    if (desk && tab !== 'browser') { desk(tab); return; }
  }
  wsTab = tab; wsPicked = true;
  drawWs(); bumpWs();
}

/* The pane's state, out and back. The residency rule is the caller
   (src/state/session/residency.ts): it saves this when the reader leaves a
   session mid-turn and hands it back on return. Two functions rather than two
   bindings for it to read and write -- which view is up and whether the reader
   chose it belong to the panel, and only the panel knows a restore is not a
   fresh pick. */
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
  islands.workspace.draw();
}

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  WS = islands.workspace.shared();

  /* ── the turn's products ───────────────────────────────────────────────
   The record's own rows for one turn, unfiltered. Which of them counts as a
   product, and what a tile can draw of it, are the transcript island's to
   decide -- see artifactsOf in features/transcript/store.ts.

   The turn number is the one WS.changes files rows under: bumped per turn by
   the pipeline, and per user message with text by the replay
   (features/workspace/record.ts). The transcript counts it the same way over
   the same payload. */
  sources.artifacts = {
    changes: (turn) => WS.changes.filter((c) => c.turn === turn),
  };
}

export { wsTab, wsOpen, wsWide, wsPicked, WS, wsReset, wsShortPath, setWs, setWsFull, bumpWs, wsPick, wsView, wsRestore, wsEpoch, wsStale, wsShowsTurn, drawWs }
