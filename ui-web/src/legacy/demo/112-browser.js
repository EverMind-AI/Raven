/* ══ browser ═══════════════════════════════════════════════════════
   The renderer is the browser island (ui-web/src/features/browser/), reached
   through the workspace island's dispatch (workspace/store.draw handles the
   unmount-before-wipe and the frame-watch drop its old drawWs wrapper did).
   What remains here is the fixture source. */

/* The fixture source: the demo has no embedded Chromium, so the island
   draws the fetched-links list from the same WS.urls the replay fills.
   Registered, not declared-for-override -- live mode installs its own
   sources.browser and this object is never consulted. */

import { sources } from '../../state/sources'
import { WS } from './100-workspace.js'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.browser ??= {
    embedded: false,
    urls: () => WS.urls,
    openUrl: (u) => toast(`demo：正式版会用系统浏览器打开 ${u}`),
  };
}
