/* -- data & memory: the seam ------------------------------------------
   The source is ui-web/src/features/memory/source.ts; installing it here
   is what makes it the page's one memory source in every mode. */

import { memorySource } from '../../features/memory/source'
import { sources } from '../../state/sources'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.memory = memorySource;
}
