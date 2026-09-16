/* ---- the writes the live layer can actually make --------------------- */
/* One delete per session, and a session that refuses stays in the list -- the
   rail must never claim something is gone while the file is still on disk. The
   predicate is the single delete's, character for character, and both live with
   the rail's own data (ui-web/src/features/rail/source.ts). */

import { deleteAllSessions } from '../../features/rail/leave'
import { sources } from '../../state/sources'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.sessions.deleteAll = deleteAllSessions;
}
