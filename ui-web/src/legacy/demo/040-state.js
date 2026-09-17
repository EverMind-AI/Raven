/* ══ app state ════════════════════════════════════════════════════ */

import { islands } from '../../islands'
import { closeApproval as approvalClose, open as approveSheet, openApproval as approvalSheet } from '../../features/composer/approve'
import { close as clarifyClose, open as clarifySheet } from '../../features/composer/clarify'
import { drawQueue as queueDraw, dropDraft, loadDraft, parkDraft, queueClear, queuePush, queueRestore, queueShift, queueSnapshot, turn } from '../../features/composer/mount'
import { add as sheetAdd, dropClass as sheetDropClass, forget as sheetsForget, remove as sheetRemove, session as sheetSession, sync as sheetsSync } from '../../state/sheetRack'
import { current as modelCurrent, setCurrent as modelSet } from '../../features/model/store'
import { bootError, show as failureBar } from '../../shell/failure'
import { setCurrent as sessionSet } from '../../shell/session'
import { show as toast } from '../../shell/toast'
import { open as upShade } from '../../shell/upgrade'
import * as confirm from '../../state/confirm'
import { T } from './010-kernel.js'
import { sessionRows } from './050-rail.js'

let timers = [];
/* The running turn's usage totals. On an object because the conversation,
   replay and composer layers all write it. */
const runState = { use: null };
let rt = 'local', undoBin = null;

const stop_ = () => { timers.forEach(clearTimeout); timers = []; };
const later = (ms, fn) => timers.push(setTimeout(fn, ms));
const down = () => islands.transcript.down();
const sess = (id) => sessionRows().find((s) => s.id === id);

/* ══ composer drafts ══════════════════════════════════════════════
   Storage and ownership live in islands.composer. */
/* ══ confirm dialog ═══════════════════════════════════════════════
   The question, the two buttons and the veil are src/state/confirm.ts's now.
   This is the name the islands reach through the bridge and the live layer's
   callers still use. */
function confirmAsk(title, body, label, fn) { confirm.ask(title, body, label, fn); }

/* ══ session-scoped sheets ════════════════════════════════
   The rack that holds everything docking above the composer -- a clarify
   question, an approval request, a dag graph -- is the composer island's now,
   filed per conversation so that switching sessions cannot leave another one's
   question sitting over the field. See ui-web/src/state/sheetRack.ts for
   what that scoping is for and what it does not fix.

   What this part re-exports are the island's own verbs under the names every
   caller has used all along. They were bound by one destructure of the island
   bag in this part's install(), which is why nothing could be typed and nothing
   could be read before the layer had installed; a plain import is the same
   binding without either limit. The bag still carries them for stage C. */

/* The one exception, and it is not the island's own verb: a draft that becomes
   a session has to be claimed in two stores at once, and the bag is where that
   pair is spelled (see islands.ts). */
const claimDraft = (id) => islands.composer.claimDraft(id);

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sessionSet('a');
  /* Dev-only hook, beside __clarify, __upnote and __dag in the live layer and
   for the same reason: the approval sheet only appears when an engine asks for
   one, which is too long a loop to design a sheet in. On window because a
   devtools console is the only caller there will ever be
   (__approve('rm -rf build/')). */
  window.__approve = (p) => approveSheet(p || 'rm -rf build/',
    () => toast(T('gui.confirm.allow')), () => toast(T('gui.confirm.deny')));
}

export { timers, runState, rt, undoBin, stop_, later, down, sess, confirmAsk, sheetSession, sheetAdd, sheetRemove, sheetDropClass, sheetsSync, sheetsForget, approveSheet, approvalSheet, approvalClose, clarifySheet, clarifyClose, queueDraw, queuePush, queueShift, queueClear, queueSnapshot, queueRestore, parkDraft, loadDraft, dropDraft, claimDraft, turn, modelCurrent, modelSet, failureBar, bootError, upShade }
