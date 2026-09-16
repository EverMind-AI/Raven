/* -- knowledge bases: the seam ----------------------------------------
   The source is ui-web/src/features/knowledge/source.ts; installing it here
   swaps the demo fixtures for a real engine. */

import { knowledgeSource } from '../../features/knowledge/source'
import { sources } from '../../state/sources'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.knowledge = knowledgeSource;
}
