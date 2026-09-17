/* ══ module 2d: plugin market ═════════════════════════════════════
   The renderer is the plugins island (ui-web/src/features/plugins/); what
   remains here is its shell face -- the tab chrome around #capsBody and the
   names other layers still call. The island owns everything drawn inside the
   body and the shared detail drawer.

   This part must load AFTER 152-skills.js: its drawCaps wrapper is the
   outermost of the chain, mirroring the old live-layer order -- the skill
   branch below hands over to the skill tab's own draw, and the shared page
   hero is synced from here for both tabs. */

// Stable per-name hue: same plugin, same colour, every render and page.
// Kept as a shell helper because the memory drawer uses it too.

import { islands } from '../../islands'
import * as page from '../../state/page'
import { sources } from '../../state/sources'
import { $, T, mk } from './010-kernel.js'
import { closeDetail, decorateCloseDetail, decorateExtSet, extTab } from './120-capabilities.js'
import { decorateDrawCaps, skInstBtn, skView } from './152-skills.js'

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

/* The "installed" entry point rides in the filter bar, like the skills view
   switch — created once, shown only on the plugin tab. */
let pmInstBtn;

/* The plugin tab's face on #capsBody: chrome first, then the island host.
   The host node survives other tabs clearing #capsBody (they only detach
   it), so re-appending it costs nothing and loses no React state. */
function drawPlugTab() {
  const box = $('#capsBody');
  box.innerHTML = '';
  box.appendChild(islands.plugins.host);
  const title = T('gui.tab.plugins');
  const view = islands.plugins.view();
  $('#capsTitle').textContent = view === 'installed' ? T('gui.plug.installed_title') : title;
  $('#capsPage').setAttribute('aria-label', title);
  $('#cKind').hidden = true;
  $('#advAdd').hidden = view !== 'market';
  $('#cq').placeholder = T('gui.plug.search_ph');
  $('.cbar').style.display = view === 'installed' ? 'none' : '';
  pmInstBtn.sync();
  islands.plugins.redraw();
  /* The first reveal fetches; a boot-time draw of the closed page must
     not fire a market search nobody asked for. */
  if ($('#capsPage').dataset.open === 'true') islands.plugins.searchIfIdle();
}

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  pmInstBtn = (() => {
    const b = mk('button', 'pminstbtn');
    b.onclick = () => { islands.plugins.toggleView(); };
    $('.cbar').appendChild(b);
    return { el: b, sync() {
      b.hidden = extTab !== 'plugin' || islands.plugins.view() === 'installed';
      const n = islands.plugins.installedCount();
      const attn = attnCount();
      b.innerHTML = '';
      b.append(mk('span', null, T('gui.plug.installed_n', { n })));
      if (attn) b.appendChild(mk('span', 'pmbdg', String(attn)));
    } };
  })();

  {
    /* The hero sits above the search bar, so it lives outside #capsBody --
     one node, repopulated on every draw for whichever view is up. It
     covers both tabs, which is why it rides on this outermost wrapper:
     skView is the mirror demo/152-skills.js maintains. */
    const pageHero = () => {
      let h = $('#pageHero');
      if (!h) {
        h = mk('div', 'pmhero');
        h.id = 'pageHero';
        const bar = document.querySelector('#capsPage .cbar');
        bar.parentNode.insertBefore(h, bar);
      }
      return h;
    };
    /* Title only — no tagline under it; that copy read as marketing, not UI. */
    const syncHero = () => {
      const h = pageHero();
      h.innerHTML = '';
      let title = '';
      if (extTab === 'plugin' && islands.plugins.view() === 'market') title = T('gui.plug.hero');
      else if (extTab === 'skill' && skView === 'market') title = T('gui.hub.hero');
      h.hidden = !title;
      if (title) h.appendChild(mk('h3', null, title));
    };

    decorateDrawCaps((prev) => () => {
      if (extTab === 'plugin') {
        skInstBtn.sync();  // the skills "installed" button must not linger on this tab
        drawPlugTab();
        syncHero();
        return;
      }
      // Undo this tab's chrome before handing back: the plugin view hid the
      // status pills, and the skill view re-hides them for itself.
      $('#cKind').hidden = false;
      $('.cbar').style.display = '';
      prev();
      pmInstBtn.sync();
      syncHero();
    });

    decorateExtSet((prev) => (tab) => {
      const was = extTab;
      prev(tab);
      if (extTab !== was) { islands.plugins.reset(); $('.cbar').style.display = ''; }
    });

    page.subscribe(() => {
      if (page.get() !== 'capsPage') { islands.plugins.drawerClosed(); $('.cbar').style.display = ''; }
    });

    decorateCloseDetail((prev) => () => { islands.plugins.drawerClosed(); prev(); });
    $('#dClose').onclick = () => closeDetail();

    const prevInput = $('#cq').oninput;
    $('#cq').oninput = () => {
      if (extTab === 'plugin') { islands.plugins.setQuery($('#cq').value.trim()); return; }
      if (prevInput) prevInput();
    };
  }

}

export { pmTile, pmToggle, pmInstBtn, drawPlugTab }
