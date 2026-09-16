/* ---- history: the real source behind the transcript island ---------- */
/* Reading a stored turn as segments lives with the renderer (the transcript
   island); how a tool result is previewed and judged is wire knowledge and
   lives with the transcript's own source. The spread keeps whatever the
   fixture layer put on the seam; the five delegation verbs are added later, by
   live/240, once the dag reader they close over exists. */

import { cleanPreview, okOf } from '../../features/transcript/source'
import { sources } from '../../state/sources'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.transcript = {
    ...sources.transcript,
    clean: (text) => cleanPreview(text),
    okOf: (name, preview) => okOf(name, preview),
  };
}
