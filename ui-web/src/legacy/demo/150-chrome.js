/* What is left of the page's chrome: the rail's twin toggle, the split point
   and the one door to settings. Every listener this part used to add on the
   document -- the copy button over a code block, the Escape order and the
   settings shortcut -- is registered with the page's other document listeners
   now (ui-web/src/state/globalListeners.ts), which is where the order they run
   in is declared. */

import { islands } from '../../islands'
import { set as setRail } from '../../state/rail'
import { watchNarrow } from '../../state/ws'
import { $ } from './010-kernel.js'

// Shrinking past the split point must not leave the conversation hidden.
let tooNarrowToSplit;

/* The one door to settings, which two things open: the rail's foot row
   (ui-web/src/chrome/Rail.tsx) and the platform shortcut
   (ui-web/src/state/globalListeners.ts). The island's source owns the refresh
   that must happen before drawing, so both use the same opener. */
const openSettings = async () => { await islands.settings.open(); };

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  /* Collapsing the rail is the user's call, never the window's: it holds the
   session list, and having it vanish on resize loses your place. The rail's own
   toggle is a component's click now (src/chrome/Rail.tsx); this is the twin
   that brings it back, and it stays here while button#railShow is static
   markup. */
  $('#railShow').onclick = () => setRail(true);
  /* What a match does is the panel's (src/state/ws.ts); the moment the query is
   watched from stays here, with the rail toggle it belongs to. */
  tooNarrowToSplit = watchNarrow();

  /* The model chip's own menu is installed by live/120-settings.js: it opens
   the picker island against the provider list the page really has. This part
   carried a second one built from a fixture table -- a list of providers with
   an `on` flag that only the offline canvas ever had -- and it was replaced on
   every page, because that install runs after this one. The permission and
   tier chips beside it are their own components' clicks now
   (ui-web/src/chrome/PermChip.tsx, ui-web/src/chrome/TierChip.tsx). */
}

export { tooNarrowToSplit, openSettings }
