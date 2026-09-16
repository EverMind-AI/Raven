/* ══ composer ═════════════════════════════════════════════════════
   The dock is the composer island (ui-web/src/features/composer/): the field, the
   send/stop button, the queued rows, the attachment tray, the slash palette
   and the live turn row that rides the tail of the transcript. What remains
   here is the island's shell face -- the names the rest of the page still
   calls, and the palette's half of sources.composer. */

/* The field itself stays a name: the skills panel drops a prompt into it and
   the draft store reads it back. */

import { islands } from '../../islands'
import { sources } from '../../state/sources'
import { $, slashHelp, slashName } from './010-kernel.js'

let ta;

function goState() { islands.composer.goPaint(); }
function drawMeter() { islands.composer.drawMeter(); }
function taFit() { islands.composer.fitField(); }
function dockLift() { islands.composer.dockLift(); }

/* ══ session commands ═════════════════════════════════════════════
   Only what acts on THIS conversation. Navigation lives in the rail, so
   putting it here too would just be a second, worse way to click it.
   Commands carry ids, not spellings: the palette renders whatever the active
   language calls them, and both spellings stay typeable.

   Two commands, deliberately. Anything else a palette could offer already has
   a button -- send/stop, the model chip, the answer footer, the rail -- and a
   second entry point for the same action is one more thing to keep in sync.
   What is left is what has no button: acting on the session as a whole. */
/* Both bodies are the session runtime's -- `installSlashActions` replaces them
   on these very rows -- so what belongs here is the pair of ids the palette
   renders, in the order it renders them. */
const SLASH = [
  { id: 'gui.compress', fn: () => {} },
  { id: 'gui.clear', fn: () => {} }
];

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  ta = $('#ta');

  /* The palette's half of sources.composer, which is the half no transport
   answers: which commands the dock offers and what each is called. The rest is
   installed over it -- the meter's wording, the two actions, the upload -- by
   the page's wiring (src/state/install.ts) and the session runtime. */
  sources.composer = {
    slash: SLASH,
    slashName: (id) => slashName(id),
    slashHelp: (id) => slashHelp(id),
  };
}

export { ta, goState, drawMeter, taFit, dockLift, SLASH }
