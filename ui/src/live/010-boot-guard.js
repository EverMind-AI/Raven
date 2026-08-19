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

/* The demo shell has already seeded and painted its mock data by the time this
   runs. In live mode none of it may reach the eye: clear it synchronously (same
   task as the demo paint, so nothing mock survives to the first frame) and hold
   the session rail on skeleton rows until the real list lands. */
SESS = []; cur = null;
SKILLS.length = 0; PLUGINS.length = 0; CRONS.length = 0; TOOLS.length = 0;
$('#stage').innerHTML = '';
$('#flash').textContent = '';
$('#title').textContent = T('gui.new_task');
let listReady = false;
{
  const origDrawList = drawList;
  drawList = function () {
    if (listReady) { origDrawList(); return; }
    const box = $('#list'); box.innerHTML = '';
    for (let i = 0; i < 6; i++) {
      const r = mk('div', 'sess skel');
      const bar = (w, h) => { const b = mk('span', 'sk'); b.style.cssText = `width:${w};height:${h}`; return b; };
      r.append(bar(`${52 + ((i * 17) % 30)}%`, '11px'), bar('28px', '9px'));
      box.appendChild(r);
    }
  };
  drawList();
}

/* No toasts: every state change the user triggers is already visible where
   they made it (the list redraws, the switch moves, the transcript shows the
   error), so a floating strip only repeats it. Failures still reach the
   console for diagnosis. */
toast = function (text) {
  if (window.console) console.info('[raven]', text);
};

