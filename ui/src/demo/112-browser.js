/* ══ browser ═══════════════════════════════════════════════════════
   The renderer is the browser island (ui/src/features/browser/); what
   remains here is its shell face -- the draw name the workspace panel
   dispatches to -- and the fixture source. */
function drawWsWeb(box) {
  /* box is #wsBody, the workspace panel's body. */
  RavenIslands.browser.draw(box);
}

/* drawWs wipes #wsBody before dispatching, which would tear the island's
   DOM out from under React: unmount first, while that DOM is still intact.
   And leaving the browser view -- another tab, another session -- must drop
   the frame watch, which the live layer's own drawWs override used to do. */
const drawWsBare = drawWs;
drawWs = function () {
  RavenIslands.browser.detach();
  drawWsBare();
  if (!(wsOpen && wsTab === 'browser')) RavenIslands.browser.hidden();
};

/* The fixture source: the demo has no embedded Chromium, so the island
   draws the fetched-links list from the same WS.urls the replay fills.
   Registered, not declared-for-override -- live mode installs its own
   DS.browser and this object is never consulted. */
DS.browser ??= {
  embedded: false,
  urls: () => WS.urls,
  openUrl: (u) => wsOpenUrl(u),
};
