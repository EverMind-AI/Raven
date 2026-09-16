/* -- real session actions: branch / clear ---------------------------- */
/* The answer block is the transcript island's; the fork its footer offers is
   the transcript source's, and the two slash verbs that act on the open
   conversation are the runtime's (ui-web/src/state/session/runtime.ts). What
   is left here is the wiring and the two dev hooks. */

import { branch } from '../../features/transcript/source'
import { clarifyRequest } from '../../state/session/pipeline'
import { installSlashActions } from '../../state/session/runtime'
import { sources } from '../../state/sources'
import { showUpNote } from './210-update-notice.js'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.transcript.branch = branch;
  installSlashActions();

  // Dev-only hooks, on window because a devtools console is the only caller
  // either will ever have. The first lets a design pass preview the clarify
  // sheet without spending a model turn (window.__clarify({question,
  // choices})).
  window.__clarify = (p) => clarifyRequest(p || { request_id: 'dev', question: '预览', choices: ['A', 'B'] });

  // Same reason: the update row's version state only appears when a release is
  // actually newer, which never happens on a dev checkout
  // (window.__upnote('ver', '0.1.11')).
  window.__upnote = (kind, latest) => showUpNote(kind || 'ver', latest);
}
