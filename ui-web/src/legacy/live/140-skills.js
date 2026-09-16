/* -- skills: the seam -------------------------------------------------
   The source is ui-web/src/features/plugins/source.ts, beside the one read
   that fills the installed rows; installing it here replaces the fixture
   source before the first paint. */

import { skillsSource } from '../../features/plugins/source'
import { sources } from '../../state/sources'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.skills = skillsSource;
}
