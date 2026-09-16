/* -- the installed surfaces: the seam ---------------------------------
   The rows themselves, and the one read that fills them, are
   ui-web/src/features/plugins/source.ts. What is left here is the install:
   the capabilities page is still legacy chrome, so this is where its source
   goes onto the seam. */

import { capabilitiesSource } from '../../features/plugins/source'
import { sources } from '../../state/sources'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.capabilities = capabilitiesSource;
}
