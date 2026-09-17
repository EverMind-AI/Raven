/* ══ module 2: capabilities page ══════════════════════════════════ */

import { islands } from '../../islands'
import { show as toast } from '../../shell/toast'
import * as caps from '../../state/caps'
import * as detail from '../../state/detail'
import * as page from '../../state/page'
import * as settingsDialog from '../../state/settingsDialog'
import { sources } from '../../state/sources'
import { $, applyDecorators } from './010-kernel.js'

/* The tab, the filter reset and the drawer's close are src/state/caps.ts's now,
   and the two layers above subscribe there instead of decorating this. The
   registry stays because a harness registers through it
   (features/plugins/capabilities-source.test.ts). */
var extSetDecorators;
function extSet(tab) { return applyDecorators(extSetDecorators, extSetBase)(tab); }
function decorateExtSet(wrap) { (extSetDecorators ??= []).push(wrap); }

function extSetBase(tab) { caps.extSet(tab); }

async function openCaps(tab) {
  extSet(tab);
  page.show('capsPage');
  const src = sources.capabilities;
  if (src.loaded()) caps.draw();
  else {
    const box = $('#capsBody'); box.innerHTML = '';
    box.appendChild(islands.skills.skeleton);
  }
  try {
    if (await src.load()) caps.draw();
  } catch (e) {
    toast(`加载失败：${e.message || e}`);
    caps.draw();
  }
}
const openSkills = () => openCaps('skill');
const openPlugins = () => openCaps('plugin');
/* A dialog, so it layers over whatever you were reading rather than replacing
   it -- but the rail still marks itself, since that is where you came from.
   The flag, the rail's marks and the focus are src/state/settingsDialog.ts's
   now; these are the names the Esc chain, the settings shortcut and the bridge
   still call. */
function setIsOpen() { return settingsDialog.isOpen(); }
function openSet() { settingsDialog.open(); }
function closeSet() { settingsDialog.close(); }

/* The flag, the fade and the four islands' cards are src/state/detail.ts's now,
   and the two layers above no longer decorate this. It is still the name the
   skills tab's "use in a task" calls. */
function closeDetail() { detail.close(); }

/* ══ module 2b: external agents ════════════════════════════════════
   The renderer is the xa island (ui-web/src/features/xa/); what remains here
   is its shell face -- the one name the Escape order still calls -- and the
   fixture source. The More row opens the page by importing the island
   (shell/navfly.ts); it does not come through here. The island owns everything
   drawn inside the body and its sheet in the shared detail drawer. */
function closeXa() { islands.xa.close(); }

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
}

export { extSetDecorators, extSet, decorateExtSet, extSetBase, openCaps, openSkills, openPlugins, setIsOpen, openSet, closeSet, closeDetail, closeXa }
