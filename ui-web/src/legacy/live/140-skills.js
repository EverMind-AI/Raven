/* -- skills: the rpc source -------------------------------------------
   The renderer is the skills island (ui-web/src/features/skills/); this file
   only knows how to speak skillhub.* over /rpc -- the hub's /skills/search,
   93k skills, paged, filterable by category. Installing onto the seam
   replaces the fixture source before the first paint.

   Search rejections travel back raw: the island renders them in place
   with a retry. install/remove toast their own failures here and reject
   `{handled: true}`, the contract the island's catch reads. Both refresh the
   live source through loadExt so every installed surface answers the change. */

import { DS } from '../seam/000-datasource.js'
import { T } from '../demo/010-kernel.js'
import { rpc } from './020-rpc.js'
import { extLoaded, loadExt, skillsLive } from './090-extensions.js'

const skillhubErr = (e) => (e.data && e.data.detail) || e.message || e;

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. The body keeps the
   statements' original column: the sandbox harnesses slice them out by text. */
export function install() {
DS.skills = {
  search: (p) => rpc.call('skillhub.search', {
    query: p.query, category: p.category, page: p.page, limit: p.limit,
  }),
  detail: (id) => rpc.call('skillhub.detail', { id }),
  install: (id) => rpc.call('skillhub.install', { id })
    .then(() => loadExt().catch(() => {}))
    .catch((e) => {
      toast(T('gui.plug.op_failed', { err: skillhubErr(e) }));
      throw { handled: true };
    }),
  remove: (name) => rpc.call('skillhub.remove', { name })
    .then(() => loadExt().catch(() => {}))
    .catch((e) => {
      toast(T('gui.plug.op_failed', { err: skillhubErr(e) }));
      throw { handled: true };
    }),
  installed: () => skillsLive,
  /* Whether the one boot-time read actually landed. Without it an empty
     list means both "nothing is installed" and "the read never happened",
     and the page has to draw the same nothing for a working install and a
     broken socket. */
  loaded: () => extLoaded,
};
}

export { skillhubErr }
