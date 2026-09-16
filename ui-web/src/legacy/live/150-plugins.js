/* -- plugins: the rpc source ------------------------------------------
   The renderer is the plugins island (ui-web/src/features/plugins/); the tab
   chrome and the shims live in demo/153-plugins.js. This file only knows
   how to speak plughub.* / plug.* over /rpc, keeps the installed rows
   fresh, and forwards gateway events into the island. Installing onto the
   seam replaces the fixture source before the first paint. */

import { gateway } from '../../state/gateway'
import { sources } from '../../state/sources'
import { LANG, T } from '../demo/010-kernel.js'
import { extLoaded, loadExt, pluginsLive } from './090-extensions.js'
import { showUpNote } from './210-update-notice.js'

const pmText = (v) => (v && typeof v === 'object' ? v[LANG] || v.en || '' : String(v || ''));
const pmErrText = (e) => (e && e.data && e.data.detail) || (e && e.message) || String(e);

/* The hub serves i18n objects for its text fields; which language wins is
   decided here, once, so the island only ever sees plain strings. */
function pmNormEntry(entry) {
  return {
    ...entry,
    name: pmText(entry.name),
    summary: pmText(entry.summary),
    description: pmText(entry.description),
    contributes: (entry.contributes || []).map((c) => {
      if (c.kind !== 'mcp' || !c.auth || !c.auth.fields) return c;
      return { ...c, auth: { ...c.auth, fields: c.auth.fields.map((f) => ({ ...f, label: pmText(f.label) })) } };
    }),
  };
}

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
  const row = pluginsLive.find((x) => x.m && x.m.name === p.name);
  if (row) Object.assign(row.m, p);
  else {
    // Unknown server (fresh install, or events arriving before the first
    // ext.list) — coalesce the reload; startup syncs fire one event per server.
    clearTimeout(pmExtSoon);
    pmExtSoon = setTimeout(() => loadExt()
      .then(() => RavenIslands.plugins.event({ kind: 'rows' }))
      .catch(() => {}), 250);
  }
  RavenIslands.plugins.event({
    kind: 'status', name: p.name, state: p.state, tool_count: p.tool_count, error: p.error,
    auth_url: p.auth_url || null,
  });
}

function onOauthPending(p) {
  RavenIslands.plugins.event({
    kind: 'authPending', server: p.server, url: p.url,
    expires_in: p.expires_in, interactive: p.interactive,
  });
}

function onOauthDone(p) {
  RavenIslands.plugins.event({ kind: 'authDone', server: p.server, ok: !!p.ok, error: p.error });
}

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.plugins = {
    // A search failure is rendered in the page (market_down + retry), not
    // toasted -- the island owns that surface.
    search: (q, category) => gateway().call('plughub.search', { q, category })
      .then((r) => ({ items: r.items || [], categories: r.categories || [] }))
      .catch((e) => { throw new Error(pmErrText(e)); }),
    detail: (id) => gateway().call('plughub.detail', { id })
      .then((r) => ({ entry: pmNormEntry(r.item), installed: !!r.installed }))
      .catch((e) => {
        toast(T('gui.plug.op_failed', { err: pmErrText(e) }));
        throw { handled: true };
      }),
    // Failure is NOT toasted here: whether it lands in the progress sheet or
    // a toast depends on whether the sheet is showing, which only the island
    // knows. The error detail travels normalized.
    install: (id, form) => gateway().call('plug.install', { id, form: form || {} })
      .catch((e) => { throw new Error(pmErrText(e)); }),
    // Same shape: the user-facing uninstall toasts from the island, and the
    // silent auth rollback swallows the failure -- one call serves both.
    remove: (name) => gateway().call('plug.remove', { name })
      .then(() => loadExt())
      .catch((e) => { throw new Error(pmErrText(e)); }),
    toggle: (name, enabled) => gateway().call('plug.toggle', { name, enabled })
      .then((r) => r.mcp || null)
      .catch((e) => {
        toast(T('gui.plug.op_failed', { err: pmErrText(e) }));
        return loadExt().catch(() => {}).then(() => { throw { handled: true }; });
      }),
    // The row is the loader's own object: assigning state runs its setter,
    // which persists plugins.disabled through settings.set.
    togglePy: async (row, on) => { row.state = on ? 'on' : 'off'; },
    auth: (name) => gateway().call('plug.auth', { name })
      .then((r) => r.mcp || null)
      .catch((e) => {
        toast(T('gui.plug.op_failed', { err: pmErrText(e) }));
        throw { handled: true };
      }),
    /* The two names the contract does not declare, so they cannot be typed
       calls: a resident gateway answers both with -32601 and the card shows
       that refusal, which is the behaviour to keep until the manual-add path
       has a declared method (rpc-schema/openrpc.json has no raven.mcp.*). */
    manual: async (name, address) => {
      const listed = await gateway().callUnchecked('raven.mcp.list', {});
      await gateway().callUnchecked('raven.mcp.set', {
        servers: [...(listed.servers || []), { name, address }],
      });
      await loadExt();
    },
    rows: () => pluginsLive,
    /* See sources.skills.loaded: the same one boot-time read feeds both. */
    loaded: () => extLoaded,
    reload: () => loadExt(),
  };

  /* The pushes this part answers. */
  gateway().on('system.update_available', onUpdateAvailable);
  gateway().on('memory.health', onMemoryHealth);
  gateway().on('mcp.status', onMcpStatus);
  gateway().on('oauth.pending', onOauthPending);
  gateway().on('oauth.done', onOauthDone);
}

export { pmText, pmErrText, pmNormEntry, pmExtSoon, onUpdateAvailable, onMemoryHealth, onMcpStatus, onOauthPending, onOauthDone }
