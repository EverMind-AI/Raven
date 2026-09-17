/* ══ module 1a: session rail ══════════════════════════════════════
   The renderer is the rail island (ui-web/src/features/rail/). What is left
   here is the shell face -- the five names the rest of the page reaches the
   session source by; the source itself is installed by the boot guard and
   answers `session.list` (ui-web/src/rpc/fixtures/sessions.ts offline). */

import { islands } from '../../islands'
import { sources } from '../../state/sources'

function markNewCurrent() { islands.rail.markNew(); }
/* inline rename in the top bar, from the title bar's own button; the list
   follows. Kept as a local name because the chat header's button still calls it
   (ui-web/src/chrome/ChatTop.tsx) -- the live layer no longer replaces it,
   which is the part that mattered. */
function renameTitle() { islands.rail.rename(); }

const sessionSource = () => sources.sessions;
const sessionRows = () => sessionSource().snapshot().rows;
const sessionReplace = (rows) => sessionSource().replace(rows);
const sessionDraw = () => islands.rail.draw();
const sessionOpen = (s) => sessionSource().open(s);

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {

}

export { markNewCurrent, renameTitle, sessionSource, sessionRows, sessionReplace, sessionDraw, sessionOpen }
