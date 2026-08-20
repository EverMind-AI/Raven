/* The shell half of the strangler bridge: what a migrated island (see
   ui/src/shell/bridge.ts) may call of the legacy page. Late-bound closures,
   not references, because the live layer rebinds some of these after this
   file evaluates -- toast most notably -- and the island must see the
   rebound one. Grows one line per helper an island actually needs; never
   ahead of need. */
window.RavenShell = {
  T: (key, vars, fallback) => T(key, vars, fallback),
  toast: (text, action) => toast(text, action),
  menuAt: (x, y, items) => menuAt(x, y, items),
  confirmAsk: (title, body, label, fn) => confirmAsk(title, body, label, fn),
  showPage: (id) => showPage(id),
  useInTask: (key, name) => useInTask(key, name),
  reachText: (reach) => reachText(reach),
  closeDetail: () => closeDetail(),
  copyToClip: (text, done) => copyToClip(text, done),
  showWorkspace: (tab) => { if (!wsOpen) setWs(true); wsPick(tab); },
  wsShows: (tab) => wsOpen && wsTab === tab,
  lang: () => LANG,
};
