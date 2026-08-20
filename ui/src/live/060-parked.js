/* ---- in-flight turns survive session switches ----------------------
   Server-side subscriptions are additive and never torn down here, so a
   background session's turn keeps streaming over the socket. Leaving a
   session mid-turn parks its transcript DOM (detached nodes keep every
   streamed token, which is persisted nowhere else until the turn ends) and
   buffers the events that arrive while it is away; returning reattaches the
   DOM and replays the buffer, so nothing is lost. */
const parkedTurns = new Map();  // session_key -> parked turn snapshot
const subBySession = {};        // session_key -> subscription_id
const subSession = {};          // subscription_id -> session_key
const PARK_EVENT_CAP = 4000;
/* The session the running turn belongs to. parkTurn must NOT key by `cur`:
   every rail click does `cur = s.id; openSession(s)`, so by the time the
   old turn is parked, `cur` already names the TARGET session — parking
   under it would file the old transcript in the wrong drawer and the
   parked-restore branch would immediately hand it back as the new
   session's content (the "switching still shows the old session" bug). */
let turnOwner = null;
/* The message a retry would re-send. Held here rather than read back off the
   last `.ask` bubble, which is markup and may belong to another session. */
let lastAsk = '';

function parkTurn() {
  if (!turnOwner || !busy) return;
  stopSayPaint();
  const s = sess(turnOwner);
  if (s) s.status = 'run';
  parkedTurns.set(turnOwner, {
    nodes: [...$('#stage').childNodes],
    turn: { st: live.st, steps: live.steps, say: live.say, open: new Map(live.open),
      sawEpisode: live.sawEpisode, startedAt: live.startedAt, answerAt: live.answerAt },
    busy, use, tl, raw, lastRun, q,
    /* The live clock's anchor. It is the composer island's own state, and the
       away session's idle turn-live paint zeroes it -- without carrying it
       here, a turn ten minutes in read "2s" after a round trip through
       another session. */
    liveT0: RavenIslands.composer.liveAnchor(),
    ws: { changes: WS.changes, cmds: WS.cmds, urls: WS.urls, file: WS.file, turn: WS.turn, unseen: WS.unseen },
    wsTab, wsPicked,
    events: [], overflow: false,
  });
}

/* A parked node is detached but not finished with: the transcript island
   releases a lane host once it leaves the page, and the only copy of a turn
   still streaming lives in one of these arrays until restoreTurn puts it
   back. */
DS.transcript.parked = (node) => {
  for (const pk of parkedTurns.values()) if (pk.nodes.includes(node)) return true;
  return false;
};

function restoreTurn(pk) {
  turnOwner = cur;
  const stage = $('#stage');
  stage.innerHTML = '';
  pk.nodes.forEach((n) => stage.appendChild(n));
  Object.assign(live, pk.turn);
  busy = pk.busy; use = pk.use; tl = pk.tl; raw = pk.raw; lastRun = pk.lastRun; q = pk.q;
  /* Before drawMeter below: its turn-live paint keeps a non-zero anchor, so the
     clock resumes from the turn's real start rather than from the switch. */
  RavenIslands.composer.setLiveAnchor(pk.liveT0 || 0);
  Object.assign(WS, pk.ws);
  wsTab = pk.wsTab; wsPicked = pk.wsPicked;
  const s = sess(cur);
  if (s && s.status === 'run') s.status = null;
  pk.events.forEach((ev) => { try { onEvent(ev); } catch { /* one bad frame must not eat the rest */ } });
  paintSay();
  drawQ(); drawMeter(); goState(); drawList(); drawBanner();
  if (typeof drawWs === 'function' && wsOpen) drawWs();
  down();
}

