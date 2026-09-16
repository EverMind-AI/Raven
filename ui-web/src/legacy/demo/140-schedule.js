/* ══ module 3b: data & memory ═════════════════════════════════════
   The renderer is the memory island (ui-web/src/features/memory/); what
   remains here is its shell face -- the names the nav button, the Esc
   handler and the live layer's redrawAll still call -- and the fixture
   source. */

import { cronExprHuman, cronWhen } from '../../features/cron/humanize'
import { islands } from '../../islands'
import { setCurrent as sessionSet } from '../../shell/session'
import { show as toast } from '../../shell/toast'
import { sources } from '../../state/sources'
import { $, T } from './010-kernel.js'
import { CRONS, FREQ } from './030-fixtures.js'
import { sessionDraw, sessionOpen, sessionRows } from './050-rail.js'
import { ask, noteRow } from './060-conversation.js'

function openMem() { islands.memory.open(); }
function closeMem() { islands.memory.close(); }
function openKb() { islands.knowledge.open(); }
function closeKb() { islands.knowledge.close(); }
function drawMem() {
  /* A language flip re-renders #memBody with the new catalogue. */
  islands.memory.redraw();
}
function drawKb() {
  /* A language flip re-renders #kbBody with the new catalogue. */
  islands.knowledge.redraw();
}

/* ══ module 4: scheduled work ═════════════════════════════════════
   The renderer is the cron island (ui-web/src/features/cron/); what remains
   here is its shell face -- the names the Esc handler, the cron source's
   run-opening actions in both layers, and the live layer's turn refresh and
   redrawAll still call -- and the fixture source. Opening a run closes this
   page and lands on the session it made, which is why the source's own
   actions close it: sources.cron.runNow and sources.cron.openRun below, and the live
   twin at live/100-schedules.js. The rail opens the page by importing the
   island (features/rail/RailPage.tsx); it does not come through here. */
function closeCron() { islands.cron.close(); }
function refreshCron() { return islands.cron.refresh(); }
function drawCron() {
  /* A language flip re-renders #cronBody with the new catalogue. */
  islands.cron.redraw();
}
/* Boot calls this to prefetch rows without opening the page. */
function cronWarm() { return islands.cron.warm(); }

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  /* The fixture source: the demo has no memory engine behind it, so it
   answers list with the down marker and the island shows the page's down
   note. Registered, not declared-for-override -- live mode installs its
   own sources.memory and this object is never consulted. */
  sources.memory ??= {
    stats: async () => null,
    list: async () => { throw { down: true }; },
    remove: async () => {},
  };

  /* The fixture source: the demo's canned jobs behind the same interface the
   rpc source implements. Registered, not declared-for-override -- live mode
   installs its own sources.cron and this object is never consulted. cronWhen and
   cronExprHuman live in the island bundle now (window globals). */
  sources.cron ??= {
    rows: async () => CRONS,
    toggle: async (j) => {
      j.on = !j.on;
      j.next = j.on ? (j.at.match(/\d{2}:\d{2}/) || ['08:00'])[0] : T('gui.cron.paused');
      toast(T(j.on ? 'gui.cron.resumed_x' : 'gui.cron.paused_x', { name: j.name }));
    },
    remove: async (j) => {
      const i = CRONS.indexOf(j); if (i >= 0) CRONS.splice(i, 1);
      toast(T('gui.cron.deleted_x', { name: j.name }));
    },
    save: async (draft) => {
      const j = CRONS.find((x) => x.id === draft.id);
      if (j) { Object.assign(j, draft, { when: cronWhen(draft) }); return j; }
      const fresh = { ...draft };
      delete fresh.fresh;
      fresh.when = fresh.freq === 'hour' ? T('gui.cron.h.every_h', { n: 1 })
        : fresh.freq === 'cron' ? cronExprHuman(fresh.at) : `${T(FREQ.find((f) => f.id === fresh.freq).label)} ${fresh.at}`;
      fresh.next = fresh.on ? fresh.at : T('gui.cron.paused');
      CRONS.push(fresh);
      return fresh;
    },
    runs: async (j) => (j.runs || []),
    runNow: async (j) => {
      closeCron();
      const s = { id: 'n' + Date.now(), title: j.name, last: T('gui.cron.manual_run'), when: T('gui.sess.just_now'),
        run: null, from: 'cron', job: j.id };
      sessionRows().unshift(s); sessionSet(s.id); sessionDraw(); sessionOpen(s);
      sources.composer.send(j.what);
      toast(T('gui.cron.running_x', { name: j.name }));
    },
    openRun: async (j, run) => {
      closeCron();
      const s = { id: 'n' + Date.now(), title: j.name, last: run ? run.note : '',
        when: run ? run.at : '', run: run && run.ok ? 'gtm' : null, status: run && !run.ok ? 'err' : null,
        from: 'cron', job: j.id };
      sessionRows().unshift(s); sessionSet(s.id); sessionDraw(); sessionOpen(s);
      if (run && !run.ok) {
        $('#stage').innerHTML = '';
        ask(j.what);
        noteRow(run.note, T('gui.cron.rerun_note'));
      }
    },
  };
}

export { openMem, closeMem, openKb, closeKb, drawMem, drawKb, closeCron, refreshCron, drawCron, cronWarm }
