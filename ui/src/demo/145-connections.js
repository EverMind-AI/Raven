/* ══ module 2b: entrances — where you reach it ════════════════════
   NOT a capability. A plugin is "what it can touch"; an entrance is
   "where you find it". Same brand can be both (Slack plugin vs Slack
   entrance) and the two point in opposite directions.

   The renderer is the connections island (ui/src/features/connections/);
   what remains here is its shell face -- the names the More flyout, the
   Esc handler and the language flip still call -- and the fixture source. */
function openConn() { RavenIslands.connections.open(); }
function closeConn() { RavenIslands.connections.close(); }
function drawConn() {
  /* A language flip re-renders #connBody with the new catalogue. */
  RavenIslands.connections.redraw();
}
/* Esc and the veil both land here; unmounting the dialog is also what stops
   the island's scan poll, so no close path can leave a timer running. */
function connCloseDialog() { RavenIslands.connections.closeDialog(); }

/* The fixture source: the demo's canned channels behind the same interface
   the rpc source implements. Rows are the CHANNELS objects themselves,
   mutated in place, which is what makes demo edits stick across a redraw.
   Writes refuse politely, as the offline demo always did. */
DS.conn ??= {
  rows: async () => CHANNELS,
  toggle: async (c, on) => { c.on = on; },
  apply: async () => { nlSay(null); },
  qr: async () => null,
};
