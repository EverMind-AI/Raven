/* -- schedules: the seam ----------------------------------------------
   The source is ui-web/src/features/cron/source.ts, and installing it here is
   what makes it the page's one schedules source in every mode. */

import { cronSource } from '../../features/cron/source'
import { sources } from '../../state/sources'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.cron = cronSource;
}
