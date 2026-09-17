/* An IME sends its keystrokes as keydown too, so while a composition is open
   Enter belongs to the input method: it commits the candidate being typed
   (letters included, which is how CJK users type Latin). Acting on it would
   swallow the text the reader was in the middle of writing. Every Enter
   handler over a text field asks this first. keyCode 229 is the older spelling
   some IMEs still send instead of isComposing. */

import { islands } from '../../islands'
import { toggle as toggleFind } from '../../shell/find'
import { close as closeImage } from '../../shell/lightbox'
import { toggle as togglePerm } from '../../shell/perm'
import { toggle as toggleTier } from '../../shell/tier'
import { get as railOpen, set as setRail } from '../../state/rail'
import { sources } from '../../state/sources'
import { $, T } from './010-kernel.js'
import { turn } from './040-state.js'
import { setWs, wsOpen } from './100-workspace.js'
import { closeCaps, closeDetail, closeSet, closeXa, setIsOpen } from './120-capabilities.js'
import { isMac } from './130-settings.js'
import { closeCron, closeKb, closeMem } from './140-schedule.js'
import { closeConn, connCloseDialog } from './145-connections.js'
import { closePb } from './154-playbooks.js'

const composing = (e) => !!(e.isComposing || e.keyCode === 229);

/* ---- the search row ------------------------------------------------
   Owned by ui-web/src/shell/find.ts, which holds the term and whether the row
   is showing, and exports the toggle imported here. The Cmd+F handler below
   stays here because showing the rail first is a chrome decision, and it is the
   only caller from this side. */

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

  document.addEventListener('keydown', (e) => {
    /* Escape ends an open composition; it must not also close a panel or halt the
     running turn behind the reader's back. */
    if (composing(e)) return;
    const inField = /INPUT|TEXTAREA/.test(document.activeElement.tagName);
    if (e.key === 'Escape') {
      if (document.querySelector('.lightbox')) return closeImage();
      if ($('#veil').dataset.open === 'true') return $('#cfNo').click();
      /* After the confirm veil, before the page: a dialog raised over the entry
       list is what Escape should take back first. */
      if ($('#connVeil').dataset.open === 'true') return connCloseDialog();
      if ($('#detail').dataset.open === 'true') return closeDetail();
      if ($('#jobVeil').dataset.open === 'true') return $('#jobNo').click();
      if ($('#cronPage').dataset.open === 'true') return closeCron();
      if ($('#memPage').dataset.open === 'true') return closeMem();
      if ($('#pbPage').dataset.open === 'true') return closePb();
      if ($('#kbPage').dataset.open === 'true') return closeKb();
      if ($('#capsPage').dataset.open === 'true') return closeCaps();
      if ($('#xaPage').dataset.open === 'true') return closeXa();
      if ($('#connPage').dataset.open === 'true') return closeConn();
      if (setIsOpen()) return closeSet();
      if (turn.busy()) return sources.composer.stop();
    }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'f') {
      e.preventDefault(); setRail(true); toggleFind(true);
    }
    if ((e.metaKey || e.ctrlKey) && e.key === '\\') {
      e.preventDefault(); setRail(!railOpen());
    }
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'n' && !inField) { e.preventDefault(); $('#newBtn').click(); }
  });

  /* Collapsing the rail is the user's call, never the window's: it holds the
   session list, and having it vanish on resize loses your place. The rail's own
   toggle is a component's click now (src/chrome/Rail.tsx); this is the twin
   that brings it back, and it stays here with the two shortcuts above while
   button#railShow is static markup. */
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
