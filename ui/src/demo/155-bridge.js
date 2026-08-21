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
  hostPlatform: () => HOST_PLATFORM,
  wsView: () => wsView(),
  wsState: () => WS,
  wsPick: (tab) => wsPick(tab),
  sessionKey: () => cur,
  dur: (ms) => dur(ms),
  plainTitle: (s) => plainTitle(s),
  agentStagePaint: (box, ctx, opts) => { if (agentPaint) agentPaint(box, ctx, opts); },
  down: () => down(),
  attImage: (p) => ATT_IMG.get(String(p)),
  attNotes: () => Object.values(I18N.ui['gui.att.note'] || {}),
  hunkFromEdit: (o, n) => hunkFromEdit(o, n),
  hunkFromWrite: (c) => hunkFromWrite(c),
  hunkFromUnified: (l) => hunkFromUnified(l),
  noteRow: (label, detail) => noteRow(label, detail),
  stick: () => stick,
  setStick: (on) => { stick = on; },
  draftTouch: () => { clearTimeout(draftTick); draftTick = setTimeout(parkDraft, 250); },
  draftDrop: () => { clearTimeout(draftTick); dropDraft(draftOwner); },
  draftPark: () => parkDraft(),
  attImageSet: (p, url) => { ATT_IMG.set(String(p), url); },
  slashName: (id) => slashName(id),
  slashHelp: (id) => slashHelp(id),
  drawList: () => drawList(),
  setCur: (id) => { cur = id; },
  openSession: (s) => openSession(s),
  dropDraft: (id) => dropDraft(id),
  openCron: () => openCron(),
  navState: () => ({ pages: Object.keys(NAV_OF), btnOf: (p) => (typeof NAV_OF[p] === 'function' ? NAV_OF[p]() : NAV_OF[p]) }),
  openWebsearch: () => { openPlugins(); openDetail('websearch'); },
  openXa: () => openXa(),
  openConn: () => openConn(),
  markNew: () => markNewCurrent(),
  themeSet: (next) => { CFG.theme = next; lookSave(); shellTheme(); if (setIsOpen()) drawSettings(); },
  appVersion: () => APP_VERSION,
  modKey: () => modKey(),
  isMac: () => isMac(),
  dockLift: () => dockLift(),
  plugRedraw: () => { if ($('#capsPage').dataset.open === 'true' && extTab === 'plugin') drawCaps(); },
};

/* Settings-island verbs, one guarded line each: a helper missing from this
   build leaves its verb absent, and the island refuses that control instead
   of the whole bundle crashing at evaluation. */
if (typeof openSet === 'function') window.RavenShell.openSet = () => openSet();
if (typeof closeSet === 'function') window.RavenShell.closeSet = () => closeSet();
if (typeof setIsOpen === 'function') window.RavenShell.setIsOpen = () => setIsOpen();
if (typeof openConn === 'function') window.RavenShell.openConn = () => openConn();
if (typeof APP_VERSION !== 'undefined') window.RavenShell.appVersion = () => APP_VERSION || null;
if (typeof SESS !== 'undefined') window.RavenShell.sessionCount = () => SESS.length;
if (typeof checkUpdate !== 'undefined') window.RavenShell.checkUpdate = (btn) => checkUpdate(btn);
if (typeof wsOpenUrl !== 'undefined') window.RavenShell.openUrl = (u) => wsOpenUrl(u);
if (typeof lookGet === 'function') window.RavenShell.look = { get: () => lookGet(), set: (patch) => lookSet(patch) };
if (typeof ntfSave === 'function') window.RavenShell.ntf = { get: () => NTF.on, set: (v) => { NTF.on = v; ntfSave(); }, push: (title) => ntfPush(title, '', { force: true }) };
if (typeof renderToolset === 'function') window.RavenShell.renderToolset = (host) => renderToolset(host);
