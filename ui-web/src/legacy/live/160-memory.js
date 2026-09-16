/* -- data & memory: the rpc source ------------------------------------
   The page renderer is the memory island (ui-web/src/features/memory/); this
   file only knows how to speak memory.* over /rpc. Installing onto the
   seam replaces the fixture source before the first paint. */

import { DS } from '../seam/000-datasource.js'
import { T } from '../demo/010-kernel.js'
import { rpc } from './020-rpc.js'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. The body keeps the
   statements' original column: the sandbox harnesses slice them out by text. */
export function install() {
DS.memory = {
  stats: () => rpc.call('memory.stats', {}),
  list: (req) => rpc.call('memory.list', {
    kind: req.kind, page: req.page, page_size: req.page_size, q: req.q || null,
  }),
  /* Toasted here, and still rejected as handled: the island's success
     branch closes the drawer and reloads, which must not run on a failed
     delete. */
  remove: (it) => rpc.call('memory.delete', { kind: it.kind, id: it.id })
    .then(() => toast(T('gui.mem.deleted')))
    .catch((e) => {
      toast(T('gui.plug.op_failed', { err: (e.data && e.data.detail) || e.message || e }));
      throw { handled: true };
    }),
};
}
