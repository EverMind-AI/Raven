/* ══ module 3b2: playbooks ════════════════════════════════════════
   The renderer is the playbooks island (ui-web/src/features/playbooks/): the
   library wall and one playbook's graph both draw off sources.playbooks. What
   lives here is the shell face -- the two verbs the rail and Escape call. The
   library the offline page is explorable with is behind the transport now
   (ui-web/src/rpc/fixtures/playbooks.ts), where it was already wire-shaped. */

import { islands } from '../../islands'

function openPb() { islands.playbooks.open(); }
function closePb() { islands.playbooks.close(); }
function drawPb() {
  /* A language flip re-renders #pbBody with the new catalogue. */
  islands.playbooks.redraw();
}

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {

}

export { openPb, closePb, drawPb }
