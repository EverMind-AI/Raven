/* What is left of the page's chrome: the copy button over a code block, the
   rail's twin toggle, the two composer chips and the settings shortcut. The
   Escape order is a table in ui-web/src/state/overlays.ts and the one keydown
   that reads it, with the three shortcuts it shares a handler with, is
   ui-web/src/state/globalListeners.ts -- installed below from where the
   handler used to be added. The IME guard is the composer field's own, passed
   along because demo/110-subagents.js still asks this file for it. */

import { islands } from '../../islands'
import { composing } from '../../features/composer/store'
import { toggle as togglePerm } from '../../shell/perm'
import { toggle as toggleTier } from '../../shell/tier'
import { installEscapeChain } from '../../state/globalListeners'
import { set as setRail } from '../../state/rail'
import { $, T } from './010-kernel.js'
import { setWs, wsOpen } from './100-workspace.js'
import { closeSet, setIsOpen } from './120-capabilities.js'
import { isMac } from './130-settings.js'

// Shrinking past the split point must not leave the conversation hidden.
let tooNarrowToSplit;

/* The one door to settings, which two things open: the rail's foot row
   (ui-web/src/chrome/Rail.tsx) and the platform shortcut below. The island's
   source owns the refresh that must happen before drawing, so both use the
   same opener. */
const openSettings = async () => { await islands.settings.open(); };

/* ── the More flyout ──────────────────────────────────────────────────
   Sub-agents / entrances / schedules live here. The renderer is the nav flyout module
   (ui-web/src/shell/navfly.ts), which also owns the button that opens the group;
   what remains here is the one name the live layer still calls. */
function drawMoreFly() {
  /* A language flip re-runs the MORE_ROWS.forEach that names the rows. */
  islands.nav.draw();
}

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  /* Code blocks come and go with every answer, so the click is caught once here
   rather than bound per block. The text comes from the DOM the reader sees. */
  document.addEventListener('click', (e) => {
    const b = e.target.closest && e.target.closest('.cbcp');
    if (!b) return;
    const blk = b.closest('.cblk');
    const pre = blk && blk.querySelector('pre');
    if (!pre) return;
    if (navigator.clipboard) navigator.clipboard.writeText(pre.textContent);
    b.classList.add('ok');
    b.title = T('gui.code.copied');
    b.setAttribute('aria-label', T('gui.code.copied'));
    setTimeout(() => {
      b.classList.remove('ok');
      b.title = T('gui.code.copy');
      b.setAttribute('aria-label', T('gui.code.copy'));
    }, 1500);
  });

  installEscapeChain();

  /* Collapsing the rail is the user's call, never the window's: it holds the
   session list, and having it vanish on resize loses your place. The rail's own
   toggle is a component's click now (src/chrome/Rail.tsx); this is the twin
   that brings it back, and it stays here while button#railShow is static
   markup. */
  $('#railShow').onclick = () => setRail(true);
  tooNarrowToSplit = matchMedia('(max-width: 1040px)');
  tooNarrowToSplit.addEventListener('change', (e) => { if (e.matches && wsOpen) setWs(false); });

  /* The model chip's own menu is installed by live/120-settings.js: it opens
   the picker island against the provider list the page really has. This part
   carried a second one built from a fixture table -- a list of providers with
   an `on` flag that only the offline canvas ever had -- and it was replaced on
   every page, because that install runs after this one. */

  $('#permChip').onclick = () => togglePerm();
  $('#tierChip').onclick = () => toggleTier();

  /* The platform's own shortcut, same door as the foot row. */
  document.addEventListener('keydown', (e) => {
    if (e.key !== ',' || !(isMac() ? e.metaKey : e.ctrlKey)) return;
    e.preventDefault();
    if (setIsOpen()) return closeSet();
    openSettings();
  });
}

export { composing, tooNarrowToSplit, openSettings, drawMoreFly }
