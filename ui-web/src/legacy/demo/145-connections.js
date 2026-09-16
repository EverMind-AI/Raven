/* ══ module 2b: entrances — where you reach it ════════════════════
   NOT a capability. A plugin is "what it can touch"; an entrance is
   "where you find it". Same brand can be both (Slack plugin vs Slack
   entrance) and the two point in opposite directions.

   The renderer is the connections island (ui-web/src/features/connections/);
   what remains here is its shell face -- the names the Esc handler and the
   live layer's redrawAll still call. The More flyout opens the page by
   importing the island (shell/navfly.ts); it does not come through here. */

import { islands } from '../../islands'

function closeConn() { islands.connections.close(); }
function drawConn() {
  /* A language flip re-renders #connBody with the new catalogue. */
  islands.connections.redraw();
}
/* Esc and the veil both land here; unmounting the dialog is also what stops
   the island's scan poll, so no close path can leave a timer running. */
function connCloseDialog() { islands.connections.closeDialog(); }

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
}

export { closeConn, drawConn, connCloseDialog }
