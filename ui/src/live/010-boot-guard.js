/* ══ live mode ═════════════════════════════════════════════════════
   Wires the demo shell to a running `raven serve` over the /rpc
   WebSocket. Loaded over http(s) it replaces the canned replay with
   real turn events; opened from disk (file://) or with ?stub=1 the
   demo keeps its mock data untouched. */
(() => {
if (!/^http/.test(location.protocol) || /(^|[?&])stub=1/.test(location.search)) return;

if (/RavenShell/.test(navigator.userAgent)) {
  document.documentElement.dataset.shell = '1';
  /* Inside the app the native overlay is the one and only splash: it covers
     this window from launch until the boot below posts {type:"ready"}. The
     page-level splash would just replay the same scene under it and be caught
     mid-fade when the overlay lifts -- the "two splashes" launch. */
  const s = document.getElementById('splash');
  if (s) s.remove();
}

/* Tells the shell the page has real pixels worth revealing. A no-op in a
   plain browser tab, where the page-level splash handles the same moment. */
const shellReady = () => {
  try { window.webkit.messageHandlers.raven.postMessage({ type: 'ready' }); } catch { /* not the shell */ }
};

/* Claims the splash and the onboarding moment from the demo shell: its load
   handler backs off when this flag is set, and the boot below decides when
   the splash lifts and whether first-run setup is due (setup.status). */
window.__liveBoot = 1;

/* Set before the first paint, cleared once the real counts land: the demo boot
   has already written its mock attention counts into the rail badges by the
   time this runs, and letting them through was the badge that flashed on the
   rail on every refresh. */
(() => { const r = document.querySelector('.rail'); if (r) r.dataset.counts = 'pending'; })();

/* The demo shell has already painted by the time this runs. Install the empty
   live session source synchronously, blank the conversation, and hold the rail
   on skeleton rows until the real list lands. */
let liveSessionRows = [];
DS.sessions = {
  snapshot: () => ({ rows: liveSessionRows, cur: sessionCurrent(), busy: turn.busy() }),
  replace: (rows) => { liveSessionRows = rows; },
  open: (s) => openLiveSession(s),
};
RavenIslands.rail.hold();
sessionSet(null);
CRONS.length = 0;
$('#stage').innerHTML = '';
$('#flash').textContent = '';
$('#title').textContent = T('gui.new_task');
sessionDraw();
