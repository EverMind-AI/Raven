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
function removeSession(s) { RavenIslands.rail.remove(s); }
/* inline rename in the top bar; the list follows */
function renameTitle() { RavenIslands.rail.rename(); }

DS.sessions ??= { snapshot: () => ({ rows: SESS, cur, busy, query }) };
