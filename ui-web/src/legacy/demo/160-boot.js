/* ══ boot ═════════════════════════════════════════════════════════
   Declared here but queued by the page's own boot (src/state/boot.ts), after
   every synchronous source installer: the first data-driven paint must not
   observe a half-wired seam.
   Each step is isolated so one failure stays visible and the rest still
   renders. */

import { islands } from '../../islands'
import { draw as drawCtx } from '../../shell/ctxchip'
import { draw as drawFoot } from '../../shell/foot'
import { load as lookLoad } from '../../shell/look'
import { load as paneLoad } from '../../shell/panes'
import { draw as drawPerm } from '../../shell/perm'
import { load as loadTier } from '../../shell/tier'
import * as caps from '../../state/caps'
import { set as setRail } from '../../state/rail'
import { hideSplash, markStart } from '../../state/splash'
import { bootError } from './040-state.js'
import { sessionDraw, sessionOpen, sessionRows } from './050-rail.js'
import { goState } from './090-composer.js'
import { bumpWs } from './100-workspace.js'
import { drawSettings, setRuntime } from './130-settings.js'

function bootPage() {
  [
    ['lookLoad', () => lookLoad()],
    ['paneLoad', () => paneLoad()],
    ['setRail', () => setRail(true)],
    ['sessionDraw', () => sessionDraw()],
    /* Live boot deliberately starts with an empty source and chooses a draft
       after the real list lands. Demo mode has a fixture row to open here. */
    ['sessionOpen', () => { const first = sessionRows()[0]; if (first) sessionOpen(first); }],
    ['drawPerm', () => drawPerm()],
    ['loadTier', () => loadTier()],
    ['drawCtx', () => drawCtx()],
    ['drawCaps', () => caps.draw()],
    ['drawFoot', () => drawFoot()],
    ['bumpWs', () => bumpWs()],
    ['drawSettings', () => drawSettings()],
    ['setRuntime', () => setRuntime('local')],
    ['goState', () => goState()]
  ].forEach(([where, step]) => {
    try { step(); } catch (e) { bootError(where, e); }
  });
}

/* ══ boot splash + first-run onboarding ══════════════════════════ */

/* The first-run flow, through the bag because the bag is what the onboarding
   island is reached by. */
const showOnboard = () => islands.onboard.open();

/* Whether the page's own boot has taken the splash and the onboarding moment
   off this part's hands. A setter rather than a field the live half writes: the
   writer is in the other layer, and a container one layer declares and the
   other fills is the coupling `scripts/count-shared-globals.mjs` counts.
   Was window.__liveBoot. */
let liveClaimed = false;

function claimBoot() { liveClaimed = true; }

/* The loaded page: the splash lifts unless the page's own boot has claimed
   that moment. Registered with the page's other window listeners
   (ui-web/src/state/globalListeners.ts); the flag above is read when the event
   arrives, not when it is registered, which is what lets the claim happen in
   between. */
function onLoad() {
  if (/[?&]onboard=demo/.test(location.search)) showOnboard();
  if (liveClaimed) return;
  hideSplash(250);
}

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  /* The page's own boot runs later in this same task and claims this moment
   before microtasks drain; its last step queues bootPage itself. */
  queueMicrotask(() => { if (!liveClaimed) bootPage(); });
  markStart();
}

export { bootPage, showOnboard, liveClaimed, claimBoot, onLoad }
