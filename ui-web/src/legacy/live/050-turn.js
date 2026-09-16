/* ---- live turn state machine ------------------------------------- */
/* The turn belongs to its conversation now: which step is open, which calls
   are in flight, the say buffer the session row's preview reads and the clocks
   are all fields of a `SessionRuntime`, and what a frame means to them is the
   pipeline's stage table (ui-web/src/state/session/). What is left here is the
   three seams that layer installs onto the source objects this layer built. */

import { openDagRun } from '../../features/transcript/source'
import { pinSession } from '../../features/rail/source'
import { sources } from '../../state/sources'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  // Per-turn cost lives under each answer and "a turn is running" is now the
  // ticking row above the composer, so the strip under the field stays empty.
  sources.composer.meter = () => '';

  /* Opening a delegated graph: the last node if this page already holds the run's
   own record, the agents panel otherwise. Installed as a source verb rather
   than written inline in the delivered handler, because the row a RELOAD draws
   has to open the same thing the live row does. */
  sources.transcript.openDagRun = openDagRun;

  /* A refused persist must not stay quiet. The row moves optimistically, but a
   pin the server never accepted looks identical to one it did until the page
   is reloaded and the group is simply gone -- which is exactly how an older
   resident gateway, with no session.pin to call at all, presents itself. Put
   the row back and say so. */
  sources.sessions.pin = pinSession;
}
