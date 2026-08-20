/* ══ module 1a: session rail ══════════════════════════════════════
   The renderer is the rail island (ui/src/features/rail/); what remains
   here is its shell face -- the names the chrome, the conversation, the
   pages and the live layer still call -- and the snapshot source. The
   list itself stays in the shared SESS/cur globals: the transcript, the
   turn machinery, the schedule pages and settings all write them in
   place, so the source hands the island a view of them rather than a
   copy that could go stale. */
function markNewCurrent() { RavenIslands.rail.markNew(); }
function drawList() { /* the island renders into #list */ RavenIslands.rail.draw(); }
/* inline rename in the top bar, from the title bar's own button; the list
   follows. Kept as a local name because #renameBtn's handler still calls it --
   the live layer no longer replaces it, which is the part that mattered. */
function renameTitle() { RavenIslands.rail.rename(); }

/* remove, renamed and pin are deliberately absent: with no server to tell,
   the island's own optimistic behaviour IS the demo -- a row that leaves the
   list with an undo, a pin that just moves, a title that is only ever local.
   deleteAll is the exception and is installed by demo/130-settings.js, next to
   the rest of that page's writes, because wiping the list here means clearing
   page state only this layer can reach. */
DS.sessions ??= { snapshot: () => ({ rows: SESS, cur, busy }) };
