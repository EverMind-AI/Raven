/* ══ module 2d: plugin market ═════════════════════════════════════
   The renderer is the plugins island (ui-web/src/features/plugins/); what
   remains here is its shell face -- the plugin tab's draw, its installed
   button, the hero above the bar, and the names other layers still call. The
   island owns everything drawn inside the body and the shared detail drawer.

   This part must load AFTER 152-skills.js: its steps are the outer half of a
   draw, mirroring the old live-layer order -- the skill branch hands over to
   the skill tab's own draw, and the shared page hero is synced from here for
   both tabs. src/state/caps.ts declares that order and calls these. */

// Stable per-name hue: same plugin, same colour, every render and page.
// Kept as a shell helper because the memory drawer uses it too.

import { islands } from '../../islands'
import * as caps from '../../state/caps'
import * as page from '../../state/page'
import { sources } from '../../state/sources'
import { $, T, mk } from './010-kernel.js'
import { skView } from './152-skills.js'

function pmTile(name) {
  let h = 0;
  for (let i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) >>> 0;
  const t = mk('span', 'pmtile th' + (h % 8));
  t.textContent = (name[0] || '?').toUpperCase();
  return t;
}

/* The live extensions loader flips MCP rows through this name. */
function pmToggle(name, on) { islands.plugins.toggleMcp(name, on); }

/* Which rows the rail's attention badge counts. Skills never block (they are
   method, not access), so a badge that says "something needs you" belongs to
   the plugins module alone -- and it reads the installed rows through the
   source, which is why it lives beside the page that draws them rather than
   with the inventory. */
const needsAttn = (c) => c.state === 'need' || c.state === 'fail' || !!c.update;
const attnCount = () => sources.plugins.rows().filter(needsAttn).length;

/* The plugin tab's face on #capsBody: the host first, then the chrome.
   The host node survives other tabs clearing #capsBody (they only detach
   it), so re-appending it costs nothing and loses no React state. */
function drawPlugTab() {
  const box = $('#capsBody');
  box.innerHTML = '';
  box.appendChild(islands.plugins.host);
  const title = T('gui.tab.plugins');
  const view = islands.plugins.view();
  caps.chrome({
    title: view === 'installed' ? T('gui.plug.installed_title') : title,
    label: title,
    search: T('gui.plug.search_ph'),
    pillsHidden: true,
    advHidden: view !== 'market',
    bar: view === 'installed' ? 'none' : '',
  });
  syncInstalledButton();
  islands.plugins.redraw();
  /* The first reveal fetches; a boot-time draw of the closed page must
     not fire a market search nobody asked for. */
  if ($('#capsPage').dataset.open === 'true') islands.plugins.searchIfIdle();
}

/* The "installed" entry point rides in the filter bar, like the skills view
   switch — shown only on the plugin tab, and synced on the skill tab too. */
function syncInstalledButton() {
  const attn = attnCount();
  caps.installedButton('plugin', {
    hidden: caps.get().tab !== 'plugin' || islands.plugins.view() === 'installed',
    label: T('gui.plug.installed_n', { n: islands.plugins.installedCount() }),
    badge: attn ? String(attn) : null,
  });
}

/* Title only — no tagline under it; that copy read as marketing, not UI.
   It covers both tabs, which is why it is the last step of either draw:
   skView is the mirror demo/152-skills.js maintains. */
function syncHero() {
  let title = '';
  if (caps.get().tab === 'plugin' && islands.plugins.view() === 'market') title = T('gui.plug.hero');
  else if (caps.get().tab === 'skill' && skView === 'market') title = T('gui.hub.hero');
  caps.hero(title);
}

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  caps.installedButton('plugin', { hidden: false, label: null, badge: null });
  caps.onDraw({ plugin: drawPlugTab, pluginButton: syncInstalledButton, hero: syncHero });

  caps.onTab(() => {
    islands.plugins.reset();
    caps.bar('');
  });

  page.subscribe(() => {
    if (page.get() !== 'capsPage') { islands.plugins.drawerClosed(); caps.bar(''); }
  });
}

export { pmTile, pmToggle, drawPlugTab }
