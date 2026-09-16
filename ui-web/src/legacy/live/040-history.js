/* ---- history: the real source behind the transcript island ---------- */
/* Reading a stored turn as segments lives with the renderer (the transcript
   island); what stays here is the live half of sources.transcript -- how a tool
   result is previewed and judged, which is wire knowledge -- and the shim the
   session openers still call. The spread keeps whatever the fixture layer put
   on the seam; the three delegation verbs are added later, by live/240, once
   the dag reader they close over exists. */

import { islands } from '../../islands'
import { sources } from '../../state/sources'
import { unpitch } from '../demo/060-conversation.js'
import { cleanPreview, okOf } from './030-sessions.js'

function renderHistory(messages) {
  /* Opening a stored conversation IS content: the new-task flag comes down
     before the island paints. Already down if the switch went through
     resetView; still needed for the reconnect replay, which repaints a
     conversation without leaving it. */
  unpitch();
  islands.transcript.history(messages);
}

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.transcript = {
    ...sources.transcript,
    clean: (text) => cleanPreview(text),
    okOf: (name, preview) => okOf(name, preview),
  };
}

export { renderHistory }
