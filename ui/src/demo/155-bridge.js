/* The shell half of the strangler bridge: what a migrated island (see
   ui/src/shell/bridge.ts) may call of the legacy page. Late-bound closures,
   not references, because the live layer rebinds some of these after this
   file evaluates -- toast most notably -- and the island must see the
   rebound one. Grows one line per helper an island actually needs; never
   ahead of need. */
window.RavenShell = {
  T: (key, vars, fallback) => T(key, vars, fallback),
  confirmAsk: (title, body, label, fn) => confirmAsk(title, body, label, fn),
  showPage: (id) => showPage(id),
  useInTask: (key, name) => useInTask(key, name),
  closeDetail: () => closeDetail(),
  showWorkspace: (tab) => { if (!wsOpen) setWs(true); wsPick(tab); },
  workspaceSetOpen: (open) => setWs(Boolean(open)),
  wsShows: (tab) => wsOpen && wsTab === tab,
  wsView: () => wsView(),
  wsPick: (tab) => wsPick(tab),
  attNotes: () => Object.values(I18N.ui['gui.att.note'] || {}),
  navState: () => ({ pages: Object.keys(NAV_OF), btnOf: (p) => (typeof NAV_OF[p] === 'function' ? NAV_OF[p]() : NAV_OF[p]) }),
  openWebsearch: () => { openPlugins(); RavenIslands.plugins.openMarket('websearch'); },
  markNew: () => markNewCurrent(),
  plugRedraw: () => { if ($('#capsPage').dataset.open === 'true' && extTab === 'plugin') drawCaps(); },
};

/* Settings-island verbs, one guarded line each: a helper missing from this
   build leaves its verb absent, and the island refuses that control instead
   of the whole bundle crashing at evaluation. */
if (typeof openSet === 'function') window.RavenShell.openSet = () => openSet();
if (typeof closeSet === 'function') window.RavenShell.closeSet = () => closeSet();
if (typeof setIsOpen === 'function') window.RavenShell.setIsOpen = () => setIsOpen();
