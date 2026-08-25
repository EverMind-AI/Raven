/* ══ module 1a: session rail ══════════════════════════════════════
   The renderer is the rail island (ui/src/features/rail/). The fixture source
   owns its rows; live mode replaces the whole source with live-owned storage. */
function markNewCurrent() { RavenIslands.rail.markNew(); }
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
DS.sessions ??= (() => {
  let rows = SESSION_FIXTURES;
  return {
    snapshot: () => ({ rows, cur: sessionCurrent(), busy: turn.busy() }),
    replace: (next) => { rows = next; },
    open: (s) => openDemoSession(s),
  };
})();

const sessionSource = () => DS.sessions;
const sessionRows = () => sessionSource().snapshot().rows;
const sessionReplace = (rows) => sessionSource().replace(rows);
const sessionDraw = () => RavenIslands.rail.draw();
const sessionOpen = (s) => sessionSource().open(s);
