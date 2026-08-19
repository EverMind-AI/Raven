/* ══ workspace ════════════════════════════════════════════════════
   The transcript answers "what happened". This panel answers "what is the
   state of my machine now" -- which files the agent rewrote, what it ran in
   a shell, where it went on the web. Anything that only restates the
   transcript belongs in the transcript, not here.

   It never opens itself: a pane that interrupts gets closed for good. The
   badge on the header chip does the asking. And it is never the only place a
   fact appears, so collapsing it can't lose information.            */
let wsTab = 'diff', wsOpen = false, wsWide = false, wsPicked = false;

const WS = {
  changes: [],   // { key, path, kind:'edit'|'write', add, del, hunks, turn, open }
  urls: [],      // { url, kind:'fetch'|'search', at }
  file: null,    // files view: opened file  { path, lines, ro, truncated }
  turn: 0,       // bumped per turn so Changes can group "this turn" vs earlier
  unseen: 0      // changes arrived while the panel was closed or on another view
};

function wsReset() {
  WS.changes = []; WS.urls = []; WS.file = null; WS.turn = 0; WS.unseen = 0;
  wsTab = 'diff'; wsPicked = false;
  /* Subagents belong to the session that spawned them, so they leave with it
     -- carrying the list into the next conversation would attribute one
     conversation's background work to another. The open dag node goes for the
     same reason, and because `dag.node` is addressed by session: left set, the
     panel would ask the newly opened conversation for a run it never made. */
  AGENTS = []; agentOpen = null; dagNode = null;
  const dot = $('#wsAgentRun'); if (dot) dot.hidden = true;
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
const CTX_KEEP = 3;

function hunkFromEdit(oldText, newText) {
  const del = String(oldText || '').split('\n');
  const add = String(newText || '').split('\n');
  let head = 0;
  while (head < del.length && head < add.length && del[head] === add[head]) head += 1;
  let tail = 0;
  while (tail < del.length - head && tail < add.length - head
         && del[del.length - 1 - tail] === add[add.length - 1 - tail]) tail += 1;
  const rows = [];
  const lead = del.slice(0, head);
  if (lead.length > CTX_KEEP) rows.push(['gap', lead.slice(0, lead.length - CTX_KEEP)]);
  lead.slice(Math.max(0, lead.length - CTX_KEEP)).forEach((l) => rows.push(['ctx', l]));
  del.slice(head, del.length - tail).forEach((l) => rows.push(['del', l]));
  add.slice(head, add.length - tail).forEach((l) => rows.push(['add', l]));
  const rest = del.slice(del.length - tail);
  rest.slice(0, CTX_KEEP).forEach((l) => rows.push(['ctx', l]));
  if (rest.length > CTX_KEEP) rows.push(['gap', rest.slice(CTX_KEEP)]);
  return { rows, add: add.length - head - tail, del: del.length - head - tail };
}

function hunkFromWrite(content) {
  const all = String(content == null ? '' : content).split('\n');
  /* A file ending in a newline splits to a trailing "" that is not a line;
     counting it would report one more added line than any diff tool does. */
  if (all.length > 1 && all[all.length - 1] === '') all.pop();
  const rows = all.slice(0, 40).map((l, i) => ['add', l, null, i + 1]);
  if (all.length > 40) rows.push(['gap', all.slice(40)]);
  return { rows, add: all.length, del: 0 };
}

/* A tool that reports its own unified diff (or a replay that carries one)
   lands here instead: same row shape, so the view does not care which. */
/* Accepts the wire's unified-diff string or an array of its lines. The two
   ---/+++ file headers are dropped: they name the file the row already names,
   and read as one deletion and one addition if left in. */
function hunkFromUnified(lines) {
  const rows = []; let add = 0, del = 0;
  /* Rows carry [kind, text, oldLineNo, newLineNo], numbered from the @@
     headers -- the one hunk source that knows where in the file it landed.
     The headers themselves become 'hunk' separator rows: with a numbered
     gutter their "-12,7 +12,8" text says nothing the gutter does not. */
  let o = null, n = null;
  const src = typeof lines === 'string' ? lines.split('\n') : (lines || []);
  src.filter((l) => !/^(---|\+\+\+)( |$)/.test(String(l))).forEach((raw) => {
    const l = String(raw);
    const m = l.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (m) { o = Number(m[1]); n = Number(m[2]); rows.push(['hunk', l]); return; }
    if (l.startsWith('@@')) { rows.push(['hunk', l]); return; }
    if (l.startsWith('+')) { rows.push(['add', l.slice(1), null, n == null ? null : n++]); add += 1; return; }
    if (l.startsWith('-')) { rows.push(['del', l.slice(1), o == null ? null : o++, null]); del += 1; return; }
    rows.push(['ctx', l.replace(/^ /, ''), o == null ? null : o++, n == null ? null : n++]);
  });
  return { rows, add, del };
}

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
   carries no information. live.js overrides this with the real root. */
function wsShortPath(p) { return String(p); }

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
}

/* Picking a view is a commitment: from then on that view shows its own empty
   note rather than being replaced by the launcher. */
function wsPick(tab) {
  wsTab = tab; wsPicked = true;
  drawWs(); bumpWs();
}

/* Bumped on every redraw. A view that fetches before it can render (the file
   tree) must re-check this before appending, or a slow fs.list lands in
   whatever view the user switched to meanwhile. */
let wsEpoch = 0;
const wsStale = (mine) => mine !== wsEpoch;

/* Whether the view on screen draws anything a tool call changes. drawWs() wipes
   the panel body and rebuilds it, so redrawing on every tool.start and every
   tool.complete of a running turn -- for a view that shows none of that state --
   is a panel that flickers once per call for the length of the turn, and takes
   the reader's scroll position and text selection with it. The sub-agent tab is
   routed before every turn-state view below, so when it is up there is nothing
   here for a tool event to repaint. */
const wsShowsTurn = () => wsOpen && wsTab !== 'agents';

function drawWs() {
  wsEpoch += 1;
  [...$('#wsTabs').children].forEach((b) => b.setAttribute('aria-selected', String(b.dataset.w === wsTab)));
  const box = $('#wsBody'); box.innerHTML = '';
  /* Each view that needs one stamps its own; clearing here keeps a stale
     layout mode from following the reader into the next tab. */
  delete box.dataset.view;
  agentStopClock();
  const bare = !WS.changes.length && !WS.urls.length;
  /* The browser is a place, not a report: it works with an empty session, so it
     is never behind the launcher the way "what changed" has to be. */
  if (wsTab === 'browser') return drawWsWeb(box);
  /* Like the browser, this is a place rather than a report on the current
     turn: a session's subagents are worth opening even when nothing has
     been touched yet, so it sits ahead of the launcher. */
  if (wsTab === 'agents') { box.dataset.view = 'agents'; return drawWsAgents(box); }
  if (bare && !wsPicked) return drawWsLaunch(box);
  if (wsTab === 'file') return drawWsFile(box);
  drawWsChanges(box);
}

const ICO = {
  diff: 'M4 4h16v16H4zM12 8.5v7M8.5 12h7',
  file: 'M4 7.5c0-1.1.9-2 2-2h3.5l2 2.5H18c1.1 0 2 .9 2 2v7c0 1.1-.9 2-2 2H6c-1.1 0-2-.9-2-2v-9.5Z',
  term: 'M3.5 5h17v14h-17zM7.5 10l2.5 2-2.5 2M12.5 14.5H16',
  web: 'M4.5 12h15M12 4.5c-4.5 4.5-4.5 10.5 0 15M12 4.5c4.5 4.5 4.5 10.5 0 15',
  ext: 'M10 6H6.5A2.5 2.5 0 0 0 4 8.5v9A2.5 2.5 0 0 0 6.5 20h9a2.5 2.5 0 0 0 2.5-2.5V14M14 4h6v6M20 4l-9 9',
  doc: 'M7 3.5h7L18.5 8v10.5a2 2 0 0 1-2 2h-9a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2ZM13.5 3.5V8h4.5',
  up: 'M14.5 6.5 9 12l5.5 5.5',
  reveal: 'M4 7.5c0-1.1.9-2 2-2h3.5l2 2.5H18c1.1 0 2 .9 2 2v7c0 1.1-.9 2-2 2H6c-1.1 0-2-.9-2-2v-9.5Z'
    + 'M9.5 16l5-4.5M14.5 15V11.5H11'
};
function ico(d, cls) {
  const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('fill', 'none');
  s.setAttribute('stroke', 'currentColor'); s.setAttribute('stroke-width', '1.8');
  s.setAttribute('aria-hidden', 'true');
  if (cls) s.setAttribute('class', cls);
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  p.setAttribute('d', d); s.appendChild(p);
  return s;
}

function drawWsLaunch(box) {
  const w = mk('div', 'wslaunch');
  [['diff', ICO.diff, 'gui.ws.changes', 'gui.ws.sub.changes'],
   ['file', ICO.file, 'gui.ws.files', 'gui.ws.sub.files'],
   ['browser', ICO.web, 'gui.ws.browser', 'gui.ws.sub.browser']].forEach(([tab, d, name, sub]) => {
    const b = mk('button', 'lcard');
    b.appendChild(ico(d));
    const t = mk('span', 't', T(name));
    t.appendChild(mk('small', null, T(sub)));
    b.appendChild(t);
    b.onclick = () => wsPick(tab);
    w.appendChild(b);
  });
  box.appendChild(w);
}

function drawWsChanges(box) {
  if (!WS.changes.length) {
    box.appendChild(mk('div', 'wsnote', T('gui.ws.no_changes')));
    return;
  }
  let group = null;
  /* Rendering the list IS looking at it, so the badge clears here rather than
     at some separate "mark read" moment that could drift out of sync. */
  WS.changes.forEach((c) => { c.seen = true; });
  WS.changes.forEach((c) => {
    const g = c.turn === WS.turn ? 'gui.ws.turn_now' : 'gui.ws.turn_earlier';
    if (g !== group) { group = g; box.appendChild(mk('div', 'wsgrp', T(g))); }
    box.appendChild(chgRow(c));
  });
}

function chgRow(c) {
  const row = mk('div', 'chg' + (c.flash ? ' flash' : ''));
  c.flash = false;
  const hd = mk('div', 'chghd');
  hd.setAttribute('role', 'button');
  hd.setAttribute('tabindex', '0');
  hd.setAttribute('aria-expanded', String(!!c.open));
  const chip = mk('i', 'chgc ' + c.kind, T('gui.ws.chip.' + c.kind));
  chip.title = T('gui.ws.chip.' + c.kind + '_t');
  hd.appendChild(chip);
  /* direction:rtl on .chgp keeps the file NAME visible when a long path has to
     ellipsize -- the tail is what identifies the file, not the root. */
  const p = mk('span', 'chgp');
  p.appendChild(document.createTextNode(c.name));
  if (c.dir) p.insertBefore(mk('span', 'dir', c.dir), p.firstChild);
  p.title = c.key;
  hd.appendChild(p);
  const st = mk('span', 'chgs');
  if (c.add) st.appendChild(mk('span', 'a', `+${c.add}`));
  if (c.del) st.appendChild(mk('span', 'd', `−${c.del}`));
  hd.appendChild(st);
  /* File actions live behind this, not in a bar under the row: the list is read
     top to bottom, and a strip of icons per expanded file breaks that column. */
  const more = mk('button', 'chgm', '\u22EF');
  more.setAttribute('aria-label', T('gui.ws.actions'));
  more.onclick = (e) => { e.stopPropagation(); chgMenu(c, more.getBoundingClientRect()); };
  hd.appendChild(more);
  hd.onclick = () => { c.open = !c.open; c.auto = false; drawWs(); };
  hd.onkeydown = (e) => {
    if (e.key !== 'Enter' && e.key !== ' ') return;
    e.preventDefault(); c.open = !c.open; c.auto = false; drawWs();
  };
  ctxMenu(hd, () => chgItems(c));
  row.appendChild(hd);
  if (!c.open) return row;

  const d = mk('div', 'diff');
  /* One gutter decision per file, not per hunk: a mixed card (a numbered
     unified hunk after an unnumbered edit guess) must not zigzag its left
     edge between the two layouts. */
  const numbered = c.hunks.some((h) => h.rows.some((r) => r.length > 2));
  const dline = (kind, text, oldNo, newNo) => {
    const el = mk('div', 'dl ' + kind);
    if (!numbered) { el.textContent = text === '' ? ' ' : text; return el; }
    el.classList.add('num');
    /* The line's number in the file it still exists in: the new one for added
       and unchanged lines, the old one for a line that was deleted. Two columns
       side by side left one of them blank on every row that only exists on one
       side, which is every row of a whole-file write. */
    const no = newNo == null ? oldNo : newNo;
    el.append(
      mk('i', 'lno', no == null ? '' : String(no)),
      mk('b', 'sg', kind === 'add' ? '+' : kind === 'del' ? '−' : ''),
      mk('span', null, text === '' ? ' ' : text),
    );
    return el;
  };
  c.hunks.forEach((h, hi) => {
    if (hi) d.appendChild(mk('div', 'hsep'));
    h.rows.forEach((r, ri) => {
      if (r[0] === 'gap') {
        /* The toggle flips a flag instead of splicing the context lines in,
           so an opened run can be folded back -- and reopening the card
           renders whatever state the reader left it in. */
        const g = mk('button', 'gap', r.open
          ? `··· ${T('gui.ws.fold_lines', { n: r[1].length })} ···`
          : `··· ${T('gui.ws.expand_lines', { n: r[1].length })} ···`);
        g.onclick = (e) => { e.stopPropagation(); r.open = !r.open; drawWs(); };
        d.appendChild(g);
        if (r.open) r[1].forEach((l) => d.appendChild(dline('ctx', l)));
        return;
      }
      if (r[0] === 'hunk') { if (ri) d.appendChild(mk('div', 'hsep')); return; }
      d.appendChild(dline(r[0], r[1], r[2], r[3]));
    });
  });
  row.appendChild(d);
  return row;
}

function chgItems(c) {
  return [
    { label: T('gui.ws.open'), fn: () => wsOpenPath(c.key) },
    { label: T('gui.ws.copy_path_do'), fn: () => copyToClip(c.key, T('gui.ws.copy_path')) },
  ];
}
function chgMenu(c, r) { menuAt(r.left, r.bottom + 6, chgItems(c)); }

