/* ══ subagents ════════════════════════════════════════════════════
   Every agent this conversation handed work to. The renderer is the
   subagents island (ui-web/src/features/subagents/), reached through the
   workspace island's dispatch; the list, the running clocks and the detail
   headers all live there now, and what a tool call adds to the panel's record
   is the workspace feature's (features/workspace/record.ts). What remains here
   is the panel chrome this part has always carried. */

import { islands } from '../../islands'
import { $ } from './010-kernel.js'
import { setWs, setWsFull, wsPick, wsWide } from './100-workspace.js'
import { composing } from './150-chrome.js'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  $('#wsBtn').onclick = () => islands.workspace.toggleDesk();
  $('#wsClose').onclick = () => setWs(false);
  /* Widening by hand is the seam's job now, so this button does the thing dragging
   cannot: hand the whole window to the panel. */
  $('#wsWide').onclick = () => setWsFull(!wsWide);
  $('#wsTabs').onclick = (e) => {
    const b = e.target.closest('button'); if (!b) return;
    wsPick(b.dataset.w);
  };
  /* 1-4 pick a view while the panel has focus. Cmd-J is bound with the rest of
   the global shortcuts. */
  $('#ws').addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || composing(e)) return;
    const pick = { 1: 'diff', 2: 'file', 3: 'browser', 4: 'agents' }[e.key];
    if (!pick) return;
    if (/^(INPUT|TEXTAREA)$/.test(e.target.tagName)) return;
    e.preventDefault();
    wsPick(pick);
  });
}
