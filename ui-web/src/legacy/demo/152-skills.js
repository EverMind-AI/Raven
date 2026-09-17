/* ══ module 2c: skill market ══════════════════════════════════════
   The renderer is the skills island (ui-web/src/features/skills/): the hub
   market, the installed view and the shared detail drawer's skill sheet
   all render through it, off sources.skills. What remains here is its shell
   face -- the skill tab's own draw, and the fixture source. The chrome the
   draw decides (the title, the search hint, the filter bar, the hero) is
   src/state/caps.ts's, and src/chrome/CapsPage.tsx renders it. */

/* Start a task with the capability already named: a fresh session whose
   composer opens pre-filled, cursor at the end, ready to complete.
   Shared verb: the plugin layer and the skills island both call it. */

import { islands } from '../../islands'
import * as caps from '../../state/caps'
import * as page from '../../state/page'
import { sources } from '../../state/sources'
import { $, T } from './010-kernel.js'
import { closeDetail } from './120-capabilities.js'

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

/* The skill tab's face: the island owns everything inside the box, and the
   chrome above it is still the page's, set from here on every draw. */
function drawSkillTab() {
  const box = $('#capsBody'); box.innerHTML = '';
  const title = T('gui.tab.skills');
  const installed = islands.skills.view() === 'installed';
  caps.chrome({
    title: installed ? T('gui.plug.installed_title') : title,
    label: title,
    search: T('gui.hub.search_ph'),
    pillsHidden: true,
    advHidden: true,
    bar: installed ? 'none' : '',
  });
  islands.skills.attach(box);
  islands.skills.redraw();
  /* The first reveal fetches; a boot-time draw of the closed page must
     not fire a hub search nobody asked for. */
  if ($('#capsPage').dataset.open === 'true') islands.skills.ensureSearch();
  syncInstalledButton();
}

/* The installed entry point rides in the filter bar, exactly like the plugin
   tab's -- shown only on the skill market, and synced on the plugin tab too so
   that it does not linger there. */
function syncInstalledButton() {
  caps.installedButton('skill', {
    hidden: caps.get().tab !== 'skill' || islands.skills.view() === 'installed',
    label: T('gui.plug.installed_n', { n: sources.skills.installed().length }),
    badge: null,
  });
}

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  /* Created once, the way it was appended to the bar once: no label until the
     first sync, which is the empty button mk() made. */
  caps.installedButton('skill', { hidden: false, label: null, badge: null });
  caps.onDraw({ skill: drawSkillTab, skillButton: syncInstalledButton });

  /* Switching module drops what this tab had. Registered before the plugin
     layer's, which is the order the two decorators' effects were visible in. */
  caps.onTab(() => {
    islands.skills.reset();
  });

  page.subscribe(() => {
    if (page.get() !== 'capsPage') islands.skills.dropDrawer();
  });

  /* The island owns the view; the chrome follows it from out here. A view
   flip redraws the whole tab (title, bar, hero) through drawCaps; any
   other change only needs the installed count refreshed. */
  islands.skills.subscribe(() => {
    const v = islands.skills.view();
    if (v !== skView) { skView = v; if (caps.get().tab === 'skill') caps.draw(); return; }
    syncInstalledButton();
  });
}

export { useInTask, skView, drawSkillTab }
