/* -- playbooks: the seam ----------------------------------------------
   The source is ui-web/src/features/playbooks/source.ts; installing it onto
   the same name is what swaps the demo library for what is really on disk. */

import { playbooksSource } from '../../features/playbooks/source'
import { sources } from '../../state/sources'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.playbooks = playbooksSource;
}
