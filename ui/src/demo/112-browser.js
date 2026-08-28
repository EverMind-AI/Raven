/* ══ browser ═══════════════════════════════════════════════════════
   The renderer is the browser island (ui/src/features/browser/), reached
   through the workspace island's dispatch (workspace/store.draw handles the
   unmount-before-wipe and the frame-watch drop its old drawWs wrapper did).
   What remains here is the fixture source. */

/* The fixture source: the demo has no embedded Chromium, so the island
   draws the fetched-links list from the same WS.urls the replay fills.
   Registered, not declared-for-override -- live mode installs its own
   DS.browser and this object is never consulted. */
DS.browser ??= {
  embedded: false,
  urls: () => WS.urls,
  openUrl: (u) => toast(`demo：正式版会用系统浏览器打开 ${u}`),
};
