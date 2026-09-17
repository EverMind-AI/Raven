/* ══ module 3b: data & memory ═════════════════════════════════════
   The renderer is the memory island (ui-web/src/features/memory/); what
   remains here is its shell face -- the names the nav button and the Escape
   order still call. */

import { islands } from '../../islands'

function openMem() { islands.memory.open(); }
function closeMem() { islands.memory.close(); }
function openKb() { islands.knowledge.open(); }
function closeKb() { islands.knowledge.close(); }

/* ══ module 4: scheduled work ═════════════════════════════════════
   The renderer is the cron island (ui-web/src/features/cron/); what remains
   here is its shell face -- the names the Escape order and the cron source's
   run-opening actions still call. Opening a run closes this page and lands on
   the session it made, which
   is why the source's own action closes it (features/cron/source.ts). The rail
   opens the page by importing the island (features/rail/RailPage.tsx); it does
   not come through here. */
function closeCron() { islands.cron.close(); }
function refreshCron() { return islands.cron.refresh(); }
/* Boot calls this to prefetch rows without opening the page. */
function cronWarm() { return islands.cron.warm(); }

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
}

export { openMem, closeMem, openKb, closeKb, closeCron, refreshCron, cronWarm }
