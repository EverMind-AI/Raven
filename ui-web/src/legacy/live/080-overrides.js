/* ---- overrides ----------------------------------------------------- */
/* Which conversation the page is on, what becomes of the one it leaves, and
   the turn running inside either are the session registry's and the session
   runtime's (ui-web/src/state/session/). What is left here is the wiring: the
   two composer actions, the three rail writes, the new-task button, and the
   reconnect, whose session half is the registry's and whose other two halves
   -- saying hello on the fresh socket, and re-reading what boot read once --
   belong to the page's own boot. */

import { openModelsForMissingProvider } from '../../features/model/source'
import { loadExt } from '../../features/plugins/source'
import { installSessionActions } from '../../features/rail/leave'
import { gateway } from '../../state/gateway'
import { reconnect, switchToDraft } from '../../state/session/registry'
import { installComposerActions } from '../../state/session/runtime'
import { $ } from '../demo/010-kernel.js'
import { drawCapsBadge, showPage } from '../demo/120-capabilities.js'
import { drawCaps } from '../demo/152-skills.js'
import { SURFACE, onReconnect } from './020-rpc.js'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  onReconnect(async () => {
    await gateway().call('system.hello', { client_version: '0.1.0', surface: SURFACE }).catch(() => {});
    await reconnect();
    /* The installed skills, plugins and tools are read once at boot into
     module state and served from there, so a socket that was down when boot
     ran leaves all three empty for the life of the tab -- an empty page
     rather than a failed one. Re-read them here: the session reload above
     already treats a reconnect as "refetch what the gap invalidated", and
     these are the only surfaces whose data never asks again on its own. */
    /* Repainted, not just re-read: the island renders on its own `set`, which
     refilling the module state does not call, so the extensions page would
     keep showing the offline note after the reconnect it tells the reader to
     wait for. `drawCaps` is the entry for both tabs (153-plugins.js wraps
     it), guarded the way `redrawAll` guards it -- the page may not be up. */
    loadExt()
      .then(() => {
        drawCapsBadge();
        try { drawCaps(); } catch { /* extensions page not built yet */ }
      })
      .catch(() => {});
  });

  installComposerActions();
  installSessionActions();

  $('#newBtn').onclick = () => {
    if (openModelsForMissingProvider()) return;
    showPage(null); switchToDraft();
  };
}
