/* ══ transcript: three voices, three folding depths ═══════════════
   The renderer is the transcript island (ui-web/src/features/transcript/): a
   turn is read as segments -- the ask, the steps (thought, narration,
   activity rows), the answer -- and painted into a lane host inside the
   #stage container. What remains here is the island's shell face -- the names
   the replay, the composer, the schedules fixture and the live layer still
   call -- and the fixture half of DS.transcript. */

/* One step of a turn: the handle keeps the legacy widget surface
   (hasThink/hasSay/failed setters, tool().done(), seal()). */
function newStep() {
  /* Draws into the transcript lane inside #stage. */
  return RavenIslands.transcript.step();
}

function collapseTurn(time) { RavenIslands.transcript.collapse(time); }
function foldSilentRuns(steps) { RavenIslands.transcript.foldRuns(steps); }
function dagFlowFeed(type, p) { RavenIslands.transcript.dagFeed(type, p); }

/* An ask_user round trip is part of the work, not a message the reader
   typed: question and answer share one row. */
function askEcho(question, answer, opts) {
  RavenIslands.transcript.qa(question, answer, opts);
}

/* The turn is never silent: a breathing dot fills the dead air between
   pressing send and the first event. Any content kills it. */
function showStatus(text) { RavenIslands.transcript.status(text); }
function killStatus() { RavenIslands.transcript.killStatus(); }

/* Line icons the rest of the shell still borrows (the queue's edit pen, the
   capabilities tick); the island carries its own copy of this table. */
const ACT_ICO = {
  doc: 'M7 3.5h7L18.5 8v10.5a2 2 0 0 1-2 2h-9a2 2 0 0 1-2-2v-13a2 2 0 0 1 2-2ZM13.5 3.5V8h4.5',
  folder: 'M4 7.5c0-1.1.9-2 2-2h3.5l2 2.5H18c1.1 0 2 .9 2 2v7c0 1.1-.9 2-2 2H6c-1.1 0-2-.9-2-2v-9.5Z',
  term: 'M3.5 5h17v14h-17zM7.5 10l2.5 2-2.5 2M12.5 14.5H16',
  globe: 'M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17ZM3.5 12h17M12 3.5c-4 4.3-4 12.7 0 17'
    + 'M12 3.5c4 4.3 4 12.7 0 17',
  find: 'M10.5 4a6.5 6.5 0 1 0 0 13 6.5 6.5 0 0 0 0-13ZM15.4 15.4 20 20',
  pen: 'M4.5 19.5h4L19 9a2.12 2.12 0 0 0-3-3L5.5 16.5v3ZM15.5 6.5l2 2',
  star: 'M12 4l1.9 5.3L19 11l-5.1 1.7L12 18l-1.9-5.3L5 11l5.1-1.7Z',
  chat: 'M4.5 6.5a2 2 0 0 1 2-2h11a2 2 0 0 1 2 2v6.5a2 2 0 0 1-2 2H10l-4 3.5V15H6.5a2 2 0 0 1-2-2Z',
  clock: 'M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17ZM12 7.5V12l3.4 2',
  image: 'M4.5 5.5h15v13h-15zM8.6 11.2a1.55 1.55 0 1 0 0-3.1 1.55 1.55 0 0 0 0 3.1ZM6 17.5l4.5-4.5 3.5 3.5 2.5-2.5 2.5 2.5',
  sound: 'M5 10h3l4-3.5v11L8 14H5ZM15.5 9.5a4 4 0 0 1 0 5',
  video: 'M4.5 6.5h10v11h-10zM14.5 11l5-3v8l-5-3',
  ask: 'M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17ZM9.8 9.6a2.2 2.2 0 1 1 3.4 1.9c-.8.5-1.2 1-1.2 2M12 16.6h.01',
  bad: 'M12 4.5 20.5 19H3.5L12 4.5ZM12 10v3.6M12 16.4h.01',
  dot: 'M12 3.5a8.5 8.5 0 1 0 0 17 8.5 8.5 0 0 0 0-17ZM8.5 12h7',
  chev: 'M9.5 6.5 15 12l-5.5 5.5',
  check: 'M5 12.5l4.5 4.5L19 7',
  dag: 'M7.4 12a2.2 2.2 0 1 1-4.4 0 2.2 2.2 0 0 1 4.4 0ZM21 6a2.2 2.2 0 1 1-4.4 0 2.2 2.2 0 0 1 4.4 0Z'
    + 'M21 18a2.2 2.2 0 1 1-4.4 0 2.2 2.2 0 0 1 4.4 0ZM7.3 11l9.4-4M7.3 13l9.4 4',
};

/* The fixture half of DS.transcript. The demo replay passes explicit ok
   flags and clean previews, so the reading hooks are identity; branch keeps
   the demo's canned fork. Live mode installs the rpc source over this. */
DS.transcript ??= {
  clean: (t) => String(t == null ? '' : t).trim(),
  okOf: () => true,
  branch: (text) => {
    const s = { id: 'n' + Date.now(), title: (sess(sessionCurrent()) ? sess(sessionCurrent()).title : '新任务') + ' 的分支',
      last: '从上一轮回复分叉', when: '刚刚', run: null };
    sessionRows().unshift(s); sessionSet(s.id); sessionDraw(); sessionOpen(s); toast('已分叉出新会话');
  },
  /* dagRun, openDagNode and openSpawn are deliberately absent. The cards in
     the trail open real things only with a host behind them, and the island
     already has the honest answer for each: no node states to read, nothing to
     open a node into, and the agents panel for a spawn row. Installing
     null-guarded stand-ins here only moved that decision to the wrong layer --
     live/240-external-agents.js installs the three that can do the work. */
  /* Whether a detached lane host is one the shell means to bring back rather
     than one it threw away. Nothing is parked on this canvas -- one session,
     no socket -- so the honest answer here is no; live/060-parked.js installs
     the real check over this one. */
  parked: () => false,
};
