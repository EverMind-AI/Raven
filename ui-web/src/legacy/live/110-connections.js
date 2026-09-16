/* -- connections (channels): the seam ---------------------------------
   The source is ui-web/src/features/connections/source.ts; installing it here
   replaces the fixture source before the first paint. */

import { connSource } from '../../features/connections/source'
import { sources } from '../../state/sources'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.conn = connSource;
}
