/* -- plugins: the seam and the pushes ---------------------------------
   The renderer is the plugins island (ui-web/src/features/plugins/) and the
   source beside it; the tab chrome and the shims live in demo/153-plugins.js.
   What is left here is the install and the five gateway pushes this layer
   forwards into the island. */

import { extPlugins, loadExt, pluginsSource } from '../../features/plugins/source'
import { islands } from '../../islands'
import { setFault as setMemFault } from '../../shell/banner'
import { gateway } from '../../state/gateway'
import { sources } from '../../state/sources'
import { T } from '../demo/010-kernel.js'
import { showUpNote } from './210-update-notice.js'

let pmExtSoon = null;

/* ── events from the gateway ─────────────────────────────────────── */

/* The gateway announces a newer build the moment its periodic check finds one,
 so a tab that has been open for days hears about it without a reload. Same
 banner as the boot-time system.version path. */
function onUpdateAvailable(p) {
  if (p && p.latest_version) showUpNote('ver', p.latest_version);
}

/* Long-term memory stopped writing, or started again. Broadcast like the other
 per-server events, because a backend that cannot store is not part of any one
 conversation's turn. */
function onMemoryHealth(p) {
  setMemFault(p && p.ok === false ? (p.error || T('gui.mem.down')) : null);
}
function onMcpStatus(p) {
  const row = extPlugins().find((x) => x.m && x.m.name === p.name);
  if (row) Object.assign(row.m, p);
  else {
    // Unknown server (fresh install, or events arriving before the first
    // ext.list) — coalesce the reload; startup syncs fire one event per server.
    clearTimeout(pmExtSoon);
    pmExtSoon = setTimeout(() => loadExt()
      .then(() => islands.plugins.event({ kind: 'rows' }))
      .catch(() => {}), 250);
  }
  islands.plugins.event({
    kind: 'status', name: p.name, state: p.state, tool_count: p.tool_count, error: p.error,
    auth_url: p.auth_url || null,
  });
}

function onOauthPending(p) {
  islands.plugins.event({
    kind: 'authPending', server: p.server, url: p.url,
    expires_in: p.expires_in, interactive: p.interactive,
  });
}

function onOauthDone(p) {
  islands.plugins.event({ kind: 'authDone', server: p.server, ok: !!p.ok, error: p.error });
}

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.plugins = pluginsSource;

  /* The pushes this part answers. */
  gateway().on('system.update_available', onUpdateAvailable);
  gateway().on('memory.health', onMemoryHealth);
  gateway().on('mcp.status', onMcpStatus);
  gateway().on('oauth.pending', onOauthPending);
  gateway().on('oauth.done', onOauthDone);
}

export { pmExtSoon, onUpdateAvailable, onMemoryHealth, onMcpStatus, onOauthPending, onOauthDone }
