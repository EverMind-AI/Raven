/* ══ boot ═════════════════════════════════════════════════════════
   Declared here but queued after the whole assembled script: in live mode
   every synchronous source installer must run before the first data-driven
   paint.
   Each step is isolated so one failure stays visible and the rest still
   renders. */

import { islands } from '../../islands'
import { draw as drawCtx } from '../../shell/ctxchip'
import { draw as drawFoot } from '../../shell/foot'
import { load as lookLoad } from '../../shell/look'
import { load as paneLoad } from '../../shell/panes'
import { draw as drawPerm } from '../../shell/perm'
import { load as loadTier } from '../../shell/tier'
import { bootError } from './040-state.js'
import { sessionDraw, sessionOpen, sessionRows } from './050-rail.js'
import { goState } from './090-composer.js'
import { bumpWs } from './100-workspace.js'
import { drawCapsBadge } from './120-capabilities.js'
import { drawSettings, setRuntime } from './130-settings.js'
import { setRail } from './150-chrome.js'
import { drawCaps } from './152-skills.js'

function bootPage() {
  [
    ['lookLoad', () => lookLoad()],
    ['paneLoad', () => paneLoad()],
    ['setRail', () => setRail(true)],
    ['sessionDraw', () => sessionDraw()],
    /* Live boot deliberately starts with an empty source and chooses a draft
       after the real list lands. Demo mode has a fixture row to open here. */
    ['sessionOpen', () => { const first = sessionRows()[0]; if (first) sessionOpen(first); }],
    ['drawCapsBadge', () => drawCapsBadge()],
    ['drawPerm', () => drawPerm()],
    ['drawWorkdir', () => drawWorkdir()],
    ['loadTier', () => loadTier()],
    ['drawCtx', () => drawCtx()],
    ['drawCaps', () => drawCaps()],
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

/* The splash is already on screen (it is the first thing in <body>); all the
   page has to do is take it down at the right moment. A floor on its display
   time keeps a fast boot from flashing it for two frames. */
let _spT0;
function hideSplash(minMs) {
  const s = document.getElementById('splash');
  if (!s) return;
  const wait = Math.max(0, (minMs == null ? 600 : minMs) - (Date.now() - _spT0));
  setTimeout(() => {
    s.dataset.off = '1';
    setTimeout(() => s.remove(), 560);
  }, wait);
}

let showOnboard;

/* Whether the live layer has taken the splash and the onboarding moment off
   this part's hands. A setter rather than a field the live half writes: the
   writer is in the other layer, and a container one layer declares and the
   other fills is the coupling `scripts/count-shared-globals.mjs` counts.
   Was window.__liveBoot. */
let liveClaimed = false;

function claimBoot() { liveClaimed = true; }

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  /* The live guard runs later in this same script task and claims boot before
   microtasks drain. Its final part queues bootPage after every source install. */
  queueMicrotask(() => { if (!liveClaimed) bootPage(); });
  _spT0 = Date.now();
  ({ open: showOnboard } = islands.onboard);

  addEventListener('load', () => {
    if (/[?&]onboard=demo/.test(location.search)) showOnboard();
    if (liveClaimed) return;
    hideSplash(250);
  });
}

export { bootPage, _spT0, hideSplash, showOnboard, liveClaimed, claimBoot }
