/* ══ module 2c: skill market ══════════════════════════════════════
   The renderer is the skills island (ui-web/src/features/skills/): the hub
   market, the installed view and the shared detail drawer's skill sheet
   all render through it, off sources.skills. What remains here is its shell
   face -- the chrome around #capsBody (title, search field, the
   installed button), the names other layers still call -- and the
   fixture source. */

/* Start a task with the capability already named: a fresh session whose
   composer opens pre-filled, cursor at the end, ready to complete.
   Shared verb: the plugin layer and the skills island both call it. */

import { islands } from '../../islands'
import * as page from '../../state/page'
import { sources } from '../../state/sources'
import { $, T, applyDecorators, mk } from './010-kernel.js'
import { closeDetail, decorateCloseDetail, decorateExtSet, drawCapsBadge, extTab } from './120-capabilities.js'

function useInTask(promptKey, name) {
  closeDetail();
  $('#newBtn').click();
  const ta = $('#ta');
  ta.value = T(promptKey, { name });
  ta.dispatchEvent(new Event('input', { bubbles: true }));
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

/* Mirror of the island's view, read by the plugin layer's hero sync. */
let skView = 'market';

/* The installed entry point rides in the filter bar, exactly like the
   plugin tab's -- created once, shown only on the skill market. */
let skInstBtn;

/* The skill tab's face on #capsBody: the island owns everything inside the
   box, and the chrome above it is still the page's, set here on every draw.
   demo/153-plugins.js wraps this name for the plugin tab, so a call that
   arrives here is always a skill draw. */
var drawCapsDecorators;
function drawCaps() { return applyDecorators(drawCapsDecorators, drawCapsBase)(); }
function decorateDrawCaps(wrap) { (drawCapsDecorators ??= []).push(wrap); }

function drawCapsBase() {
  const box = $('#capsBody'); box.innerHTML = '';
  const title = T('gui.tab.skills');
  const installed = islands.skills.view() === 'installed';
  $('#capsTitle').textContent = installed ? T('gui.plug.installed_title') : title;
  $('#capsPage').setAttribute('aria-label', title);
  $('#cKind').hidden = true;
  $('#advAdd').hidden = true;
  $('#cq').placeholder = T('gui.hub.search_ph');
  $('.cbar').style.display = installed ? 'none' : '';
  islands.skills.attach(box);
  islands.skills.redraw();
  /* The first reveal fetches; a boot-time draw of the closed page must
     not fire a hub search nobody asked for. */
  if ($('#capsPage').dataset.open === 'true') islands.skills.ensureSearch();
  skInstBtn.sync();
  drawCapsBadge();
}

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  skInstBtn = (() => {
    const b = mk('button', 'pminstbtn');
    b.onclick = () => islands.skills.toggleView();
    $('.cbar').appendChild(b);
    return { sync() {
      b.hidden = extTab !== 'skill' || islands.skills.view() === 'installed';
      b.innerHTML = '';
      b.append(mk('span', null, T('gui.plug.installed_n', { n: sources.skills.installed().length })));
    } };
  })();

  {
    const prevInput = $('#cq').oninput;
    $('#cq').oninput = () => {
      if (extTab === 'skill') { islands.skills.setQuery($('#cq').value.trim()); return; }
      if (prevInput) prevInput();
    };
    $('#cq').onkeydown = (e) => {
      if (e.isComposing || e.keyCode === 229) return;
      if (e.key !== 'Enter' || extTab !== 'skill') return;
      e.preventDefault();
      islands.skills.searchNow($('#cq').value.trim());
    };

    decorateExtSet((prev) => (tab) => {
      const was = extTab;
      prev(tab);
      if (extTab !== was) islands.skills.reset();
    });

    page.subscribe(() => {
      if (page.get() !== 'capsPage') islands.skills.dropDrawer();
    });

    decorateCloseDetail((prev) => () => { islands.skills.dropDrawer(); prev(); });
    $('#dClose').onclick = () => closeDetail();

    /* The island owns the view; the chrome follows it from out here. A view
     flip redraws the whole tab (title, bar, hero) through drawCaps; any
     other change only needs the installed count refreshed. */
    islands.skills.subscribe(() => {
      const v = islands.skills.view();
      if (v !== skView) { skView = v; if (extTab === 'skill') drawCaps(); return; }
      skInstBtn.sync();
    });
  }

}

export { useInTask, skView, skInstBtn, drawCapsDecorators, drawCaps, decorateDrawCaps, drawCapsBase }
