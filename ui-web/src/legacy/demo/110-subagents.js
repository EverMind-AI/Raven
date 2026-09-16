/* ══ subagents ════════════════════════════════════════════════════
   Every agent this conversation handed work to. The renderer is the
   subagents island (ui-web/src/features/subagents/), reached through the
   workspace island's dispatch; the list, the running clocks and the detail
   headers all live there now. What remains here is the fixture source and the
   workspace panel wiring that always lived in this part (tool-event hooks,
   panel chrome). */

/* The fixture source. A conversation replayed with no server behind it has
   no delegated runs and the demo never invents any -- which is why the members
   this source leaves out are the ones a run would have needed: the watch that
   keeps the list fresh, the per-run record, and the painter that draws one. */
DS.agents ??= { list: async () => [] };

/* ── tool-event hooks ──────────────────────────────────────────────────
   Fed the FULL argument object, because that is where the diff lives. */
function wsOnTool(name, args, silent) {
  const a = wsArgs(name, args);
  const path = a.path || a.file_path || '';
  let hit = null;
  if (name === 'edit_file' && path) {
    hit = wsRecordChange(path, 'edit', hunkFromEdit(a.old_text, a.new_text));
  } else if (name === 'write_file' && path) {
    hit = wsRecordChange(path, 'write', hunkFromWrite(a.content));
  } else if (name === 'web_fetch' && a.url) {
    WS.urls.unshift({ url: String(a.url), kind: 'fetch', at: T('gui.sess.just_now') });
  } else if (name === 'web_search' && a.query) {
    WS.urls.unshift({ url: String(a.query), kind: 'search', at: T('gui.sess.just_now') });
  } else return;

  if (hit && wsOpen && wsTab === 'diff') hit.flash = true;
  /* Draw before counting: the Changes view marks rows seen as it renders, so
     counting first would flash a badge that the very next line clears. */
  if (wsShowsTurn()) drawWs();
  bumpWs();
}

function wsOnToolDone(name, args, ok, preview, ms, diff) {
  const a = wsArgs(name, args);
  /* The tool's own diff is the ground truth -- for a whole-file write it is the
     only record of what was replaced, which the arguments cannot show. It
     replaces the hunk guessed at tool.start. */
  if (diff && diff.length && /^(edit_file|write_file)$/.test(name)) {
    const path = a.path || a.file_path || '';
    const c = WS.changes.find((x) => x.key === path && x.turn === WS.turn);
    if (c) {
      const h = hunkFromUnified(diff);
      const stale = c.hunks.pop();
      if (stale) { c.add -= stale.add; c.del -= stale.del; }
      c.hunks.push(h); c.add += h.add; c.del += h.del;
    }
  }
  if (wsShowsTurn()) drawWs();
  bumpWs();
}

$('#wsBtn').onclick = () => RavenIslands.workspace.toggleDesk();
$('#wsClose').onclick = () => setWs(false);
/* Widening by hand is the seam's job now, so this button does the thing dragging
   cannot: hand the whole window to the panel. */
$('#wsWide').onclick = () => setWsFull(!wsWide);
$('#wsTabs').onclick = (e) => {
  const b = e.target.closest('button'); if (!b) return;
  wsPick(b.dataset.w);
};
/* 1-4 pick a view while the panel has focus. Cmd-J is bound with the rest of
   the global shortcuts. */
$('#ws').addEventListener('keydown', (e) => {
  if (e.metaKey || e.ctrlKey || e.altKey || composing(e)) return;
  const pick = { 1: 'diff', 2: 'file', 3: 'browser', 4: 'agents' }[e.key];
  if (!pick) return;
  if (/^(INPUT|TEXTAREA)$/.test(e.target.tagName)) return;
  e.preventDefault();
  wsPick(pick);
});
