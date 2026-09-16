/* The shell half of the strangler bridge: what a migrated island (see
   ui-web/src/shell/bridge.ts) may call of the legacy page. Late-bound closures,
   not references, because several of these resolve their decorator chain when
   they are called -- showPage, closeDetail and drawCaps are each wrapped by
   two later parts -- and the island must reach the outermost wrapper. Grows
   one line per helper an island actually needs; never ahead of need. */

import { $, I18N, T } from './010-kernel.js'
import { confirmAsk } from './040-state.js'
import { markNewCurrent } from './050-rail.js'
import { setWs, wsOpen, wsPick, wsTab, wsView } from './100-workspace.js'
import { NAV_OF, closeDetail, closeSet, extTab, openPlugins, openSet, setIsOpen, showPage } from './120-capabilities.js'
import { drawCaps, useInTask } from './152-skills.js'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. The body keeps the
   statements' original column: the sandbox harnesses slice them out by text. */
export function install() {
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
}
