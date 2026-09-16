/* -- the embedded browser: the seam -----------------------------------
   The source and the two frame decoders are
   ui-web/src/features/browser/source.ts; what is left here is the install and
   the two registrations that point the gateway's pushes at them. */

import { browserSource, onFrameBytes, onFrameJson } from '../../features/browser/source'
import { gateway } from '../../state/gateway'
import { sources } from '../../state/sources'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.browser = browserSource;

  /* Screencast frames arrive as binary WS messages:
   "RVF1" + u32 header length + JSON header + raw JPEG. */
  gateway().binary(onFrameBytes);

  /* Old servers still notify frames as base64 JSON; same hook after decode. */
  gateway().on('browser.frame', onFrameJson);
}
