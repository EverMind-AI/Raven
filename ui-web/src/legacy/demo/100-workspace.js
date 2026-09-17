/* ══ workspace ════════════════════════════════════════════════════
   The transcript answers "what happened". This panel answers "what is the
   state of my machine now" -- which files the agent rewrote, what it ran in
   a shell, where it went on the web. Anything that only restates the
   transcript belongs in the transcript, not here.

   It never opens itself: a pane that interrupts gets closed for good. The
   badge on the header chip does the asking. And it is never the only place a
   fact appears, so collapsing it can't lose information.

   The renderer and shared state are owned by the workspace island
   (ui-web/src/features/workspace/), and what the panel is a record of -- the
   changed-file rows, the pages read, the turn each belongs to -- is that
   feature's own module (features/workspace/record.ts). The panel chrome
   outside #wsBody is src/state/ws.ts and src/chrome/WsPane.tsx now; what is
   left here is the names the other parts still import, re-exported from the
   first of those, and the artifacts source the panel's record answers. */

import { islands } from '../../islands'
import { sources } from '../../state/sources'

/* Tools report absolute paths; the workspace root is the same on every row and
   carries no information. The workspace source knows the real root. */
function wsShortPath(p) { return sources.workspace.shortPath(p); }

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  const WS = islands.workspace.shared();

  /* ── the turn's products ───────────────────────────────────────────────
   The record's own rows for one turn, unfiltered. Which of them counts as a
   product, and what a tile can draw of it, are the transcript island's to
   decide -- see artifactsOf in features/transcript/store.ts.

   The turn number is the one WS.changes files rows under: bumped per turn by
   the pipeline, and per user message with text by the replay
   (features/workspace/record.ts). The transcript counts it the same way over
   the same payload. */
  sources.artifacts = {
    changes: (turn) => WS.changes.filter((c) => c.turn === turn),
  };
}

export {
  open as wsOpen, tab as wsTab, wide as wsWide, picked as wsPicked, epoch as wsEpoch,
  reset as wsReset, setOpen as setWs, setFull as setWsFull, bump as bumpWs, pick as wsPick,
  view as wsView, restore as wsRestore, stale as wsStale, showsTurn as wsShowsTurn, draw as drawWs,
} from '../../state/ws'
export { wsShortPath }
