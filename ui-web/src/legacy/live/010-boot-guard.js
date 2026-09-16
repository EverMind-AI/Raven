/* ══ live boot ═════════════════════════════════════════════════════
   The page's own half of the boot: the desktop shell's markers, the claim on
   the splash, and the session source the rail is held on until the first list
   lands. Which transport answers the calls is decided before this runs
   (src/state/transport.ts) -- a page opened from disk or with ?stub=1 installs
   these same parts and reads the offline fixture library through them. */

import { islands } from '../../islands'
import { current as sessionCurrent, setCurrent as sessionSet } from '../../shell/session'
import { sources } from '../../state/sources'
import { turn } from '../demo/040-state.js'
import { claimBoot } from '../demo/160-boot.js'
import { switchTo } from '../../state/session/registry'

/* Tells the shell the page has real pixels worth revealing. A no-op in a
   plain browser tab, where the page-level splash handles the same moment. */
const shellReady = () => {
  try { window.webkit.messageHandlers.raven.postMessage({ type: 'ready' }); } catch { /* not the shell */ }
};

/* Install the empty live session source synchronously and hold the rail on
   skeleton rows before the deferred boot reads either one. */
let liveSessionRows = [];

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  if (/RavenShell/.test(navigator.userAgent)) {
    document.documentElement.dataset.shell = '1';
    /* Inside the app the native overlay is the one and only splash: it covers
     this window from launch until the boot below posts {type:"ready"}. The
     page-level splash would just replay the same scene under it and be caught
     mid-fade when the overlay lifts -- the "two splashes" launch. */
    const s = document.getElementById('splash');
    if (s) s.remove();
  }

  /* Claims the splash and the onboarding moment from the demo shell: its load
   handler backs off when this flag is set, and the boot below decides when
   the splash lifts and whether first-run setup is due (setup.status). */
  claimBoot();

  /* Set before the deferred first paint, cleared once the real counts land. */
  (() => { const r = document.querySelector('.rail'); if (r) r.dataset.counts = 'pending'; })();
  sources.sessions = {
    snapshot: () => ({ rows: liveSessionRows, cur: sessionCurrent(), busy: turn.busy() }),
    replace: (rows) => { liveSessionRows = rows; },
    open: (s) => switchTo(s),
  };
  islands.rail.hold();
  sessionSet(null);
  /* From here on the pointer is the live layer's, so what it says can be recorded
   for the next reload. Started after the line above on purpose: the demo shell
   has already opened its canned session on this page, and both that and the
   clear above are fixture noise the note must not carry (see shell/resume.ts). */
  islands.view.watch();
}

export { shellReady, liveSessionRows }
