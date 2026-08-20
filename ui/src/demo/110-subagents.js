/* ══ subagents ════════════════════════════════════════════════════
   Every agent this conversation handed work to. The list is scoped to the session
   that spawned them -- another conversation's background work is not this panel's
   business -- and opening one draws its whole run with the transcript renderer, so
   a subagent reads exactly like the thread that started it.

   Both kinds of delegation, because the reader's question is "who worked on this"
   and the answer used to be half of them: a spawned call was listed and a graph's
   nodes were not, on the grounds that the graph sheet shows them. It does, while
   the run is on screen -- and the graph is dismissed, or the page was reloaded, or
   the reader simply never saw the sheet. A dag row opens through dag.node, the
   same reader the graph uses. */
let AGENTS = [];
let agentOpen = null;    // id of the spawn row whose context is on screen
let dagNode = null;      // {run_id, node, agent, label} while a graph node is open
/* Answered by live.js once a call has come back -32601. The shell's own canned
   replay never says that, so on its own it reports every surface present --
   which is what the demo is for. */
let rpcAbsent = () => false;
let agentRender = null;  // (box, id) => void -- draw one run into a box
let dagNodeRender = null; // (box, {run_id, node}) => void -- draw one dag node

/* The fixture source. A conversation replayed with no server behind it has
   whatever runs the fixtures gave it, and they never change -- which is also
   why the watch that keeps this fresh lives in the live layer and not here. */
DS.agents ??= { list: async () => AGENTS };

/* Refreshing the list is three separate judgements, and all three are about
   what is on screen rather than about the transport, so they live here:
   whether to ask at all, whether the answer still belongs to the conversation
   the reader is in, and whether anything changed enough to repaint. */
let agentsBusy = false;
let agentsAt = 0;
/* One fingerprint per drawn list. The watch answers every couple of seconds
   whether or not anything moved, and drawWs() wipes the panel body to rebuild
   it -- so redrawing an unchanged list is a list that flickers on a timer and
   loses the reader's scroll every time. Keyed by conversation as well as
   content, so switching between two sessions that listed the same thing still
   repaints. */
let agentsDrawn = '';

function agentsRefresh(force) {
  /* The panel asks on each redraw and a fresh answer causes one; the floor
     keeps that from spinning, and doubles as the watch's rate limit. */
  if (!cur || agentsBusy || (!force && Date.now() - agentsAt < 2500)) return;
  agentsBusy = true;
  const asked = cur;
  DS.agents.list(asked)
    .then((rows) => {
      /* An answer for a conversation the reader already left: dropping it is
         the difference between a stale list and somebody else's list. */
      if (asked !== cur) return;
      AGENTS = rows || [];
      const dot = $('#wsAgentRun');
      if (dot) dot.hidden = !AGENTS.some((a) => a.status === 'run');
      const drawn = `${asked}|${JSON.stringify(AGENTS)}`;
      if (drawn === agentsDrawn) return;
      agentsDrawn = drawn;
      if (wsOpen && wsTab === 'agents' && !agentOpen && !dagNode) drawWs();
    })
    .catch(() => { /* an empty list is not a broken one */ })
    .then(() => { agentsBusy = false; agentsAt = Date.now(); });
}

function agentSpan(it) {
  const t0 = it.started_at ? new Date(it.started_at).getTime() : 0;
  if (!t0) return '';
  const t1 = it.ended_at ? new Date(it.ended_at).getTime() : Date.now();
  return dur(Math.max(t1 - t0, 1000));
}

/* The span, plus the anchor a still-running one needs to keep counting.
   `agentsDrawn` suppresses a repaint while the payload is unchanged, and a
   running row's payload does not change for its whole run -- `meta.json` is
   rewritten once, at the end. So the one part of the row that is derived from
   the clock froze at whatever the first poll after it appeared had drawn: a
   four-minute run read "1s" for four minutes, then jumped to "4m 1s". The graph
   hit this and grew `dagStartClock`; the list needs the same separation of the
   ticking part from the repaint. */
function agentSpanEl(it) {
  const text = agentSpan(it);
  if (!text) return null;
  const el = mk('span', 'sp', text);
  if (!it.ended_at && it.started_at) {
    const t0 = new Date(it.started_at).getTime();
    if (t0) el.dataset.t0 = String(t0);
  }
  return el;
}

let agentTicker = null;
function agentStopClock() {
  if (agentTicker) { clearInterval(agentTicker); agentTicker = null; }
}

/* Drives every live span on screen, list row and open detail header alike, and
   stops itself once none is left -- so a finished run costs nothing. */
function agentStartClock() {
  agentStopClock();
  const live = () => document.querySelectorAll('#wsBody .sp[data-t0]');
  if (!live().length) return;
  agentTicker = setInterval(() => {
    const els = live();
    if (!els.length) { agentStopClock(); return; }
    els.forEach((el) => {
      const t0 = Number(el.dataset.t0);
      if (t0) el.textContent = dur(Math.max(Date.now() - t0, 1000));
    });
  }, 1000);
}

/* When the run ended, as a wall-clock stamp: the duration says how long it
   took, this says how long ago -- the fact a list of past runs sorts by. */
function agentEndAt(it) {
  if (!it.ended_at) return '';
  const d = new Date(it.ended_at);
  if (isNaN(d.getTime())) return '';
  const p = (n) => String(n).padStart(2, '0');
  const sameDay = d.toDateString() === new Date().toDateString();
  return (sameDay ? '' : `${d.getMonth() + 1}/${d.getDate()} `) + `${p(d.getHours())}:${p(d.getMinutes())}`;
}

/* Which agent ran a call. Null is raven's own sub-agent: the difference changes
   what the transcript means, so no row leaves it unsaid. */
function agentWho(it) {
  return (it && it.agent) || 'raven';
}

function agentDot(status) {
  return status === 'run' ? 'run'
    : status === 'error' ? 'bad'
    : status === 'queued' || status === 'skipped' ? 'que'
    : 'ok';
}

/* What a run cost, for the row. Absent rather than zero when the transport that
   ran it cannot report usage -- the cli lane never can, and "0" there would be a
   claim about the agent instead of an admission about the record. */
function agentCost(it) {
  if (it.tokens == null) return '';
  return it.tokens >= 1000 ? `${(it.tokens / 1000).toFixed(1)}k` : String(it.tokens);
}

/* Opening a row goes to whichever reader can answer for it: a spawn's record is
   addressed by (session, call id), a graph node's by (run, node). One list, two
   addresses -- which is the whole reason a row says its `kind`. */
function agentOpenRow(it) {
  if (it.kind === 'dag') {
    dagNode = { run_id: it.run_id, node: it.node, agent: it.agent, label: it.node };
    agentOpen = null;
  } else {
    agentOpen = it.id;
    dagNode = null;
  }
  drawWs();
}

/* A run's state, as ONE element in the row's leading column. In flight that is
   the work glyph -- the same one the turn's own row wears, so delegated work and
   the reader's own turn look alike while they are alive; finished, it is the
   status dot. The word below is only written when there is news to write: a
   pulsing dot beside a pulsing glyph was two indicators for one fact. */
function agentMark(status) {
  if (status !== 'run') return mk('span', 'dot ' + agentDot(status));
  const w = workGlyph();
  w.classList.add('sw');
  w.title = T('gui.ws.agent_run');
  return w;
}

function drawWsAgents(box) {
  if (dagNode) return drawWsDagNode(box);
  if (agentOpen) return drawWsAgentCtx(box);
  agentsRefresh();
  /* A queued node has not run, has no transcript, and opening it shows an empty
     panel -- it is a row that can only disappoint. The graph is where waiting
     work belongs, and the sheet already draws it there, in the shape that says
     what it is waiting *for*. This list is the runs there is something to read
     about. AGENTS itself keeps them: the sheet resolves a node's status through
     it, so filtering the source would blank the marks on the graph. */
  const shown = AGENTS.filter((a) => a.status !== 'queued');
  if (!shown.length) {
    const e = mk('div', 'wsempty');
    /* An empty list and a server that cannot list are different things, and
       the reader is the one who has to tell them apart -- the second one is not
       waiting for work to appear. */
    e.appendChild(mk('div', 'ttl', T(rpcAbsent('subagent') ? 'gui.ws.agents_absent' : 'gui.ws.agents_none')));
    box.appendChild(e);
    return;
  }
  const list = mk('div', 'salist');
  shown.forEach((it) => {
    const row = mk('div', 'sarow');
    row.setAttribute('role', 'button');
    row.tabIndex = 0;
    row.appendChild(agentMark(it.status));
    const b = mk('div', 'bd');
    b.appendChild(mk('div', 'nm', plainTitle(it.label)));
    const st = mk('div', 'st');
    /* skipped shares queued's muted dot but not its word: "queued" promises
       progress, and a node skipped because its upstream failed is done. */
    if (it.status !== 'run') {
      st.appendChild(mk('span', null,
        T(it.status === 'skipped' ? 'gui.ws.agent_skip' : 'gui.ws.agent_' + agentDot(it.status))));
    }
    const span = agentSpanEl(it);
    if (span) st.appendChild(span);
    const ended = agentEndAt(it);
    if (ended) {
      const ed = mk('span', 'ed', ended);
      ed.title = T('gui.ws.agent_ended', { t: ended });
      st.appendChild(ed);
    }
    // What it cost, where the transport reported it. Beside the clock because the
    // two answer one question -- how much did this run take -- in the two units a
    // reader is actually spending.
    const cost = agentCost(it);
    if (cost) {
      const tk = mk('span', 'tk', cost);
      tk.title = T('gui.ws.agent_tokens', { n: String(it.tokens) });
      st.appendChild(tk);
    }
    st.appendChild(mk('span', 'who', agentWho(it)));
    // Which graph it came out of, for a node. Without it a run's six nodes are
    // six unexplained rows named survey / read_a / merge.
    if (it.kind === 'dag') st.appendChild(mk('span', 'gr', T('gui.ws.agent_of_graph')));
    b.appendChild(st);
    row.appendChild(b);
    row.appendChild(mk('span', 'chev', '›'));
    const go = () => agentOpenRow(it);
    row.onclick = go;
    row.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); go(); } };
    list.appendChild(row);
  });
  box.appendChild(list);
  agentStartClock();
}

function drawWsDagNode(box) {
  const it = dagNode;
  const hd = mk('div', 'sahd');
  const back = mk('button', 'back');
  back.appendChild(ico(ICO.up));
  back.appendChild(mk('span', null, T('gui.cron.back')));
  /* Back to the list, not to the graph: the graph never went anywhere -- it is
     still on the composer where it was clicked. */
  back.onclick = () => { dagNode = null; drawWs(); };
  hd.appendChild(back);
  const ttl = mk('div', 'trow');
  ttl.appendChild(mk('b', null, it.node));
  ttl.appendChild(mk('span', 'who', it.agent || 'raven'));
  hd.appendChild(ttl);
  box.appendChild(hd);

  const stage = mk('div', 'satx');
  box.appendChild(stage);
  if (dagNodeRender) dagNodeRender(stage, it);
  else stage.appendChild(mk('div', 'wsempty', T('gui.ws.agents_none')));
}

function drawWsAgentCtx(box) {
  const it = AGENTS.find((a) => a.id === agentOpen) || { label: agentOpen, status: 'run' };
  const hd = mk('div', 'sahd');
  const back = mk('button', 'back');
  back.appendChild(ico(ICO.up));
  back.appendChild(mk('span', null, T('gui.cron.back')));
  back.onclick = () => { agentOpen = null; drawWs(); };
  hd.appendChild(back);
  const ttl = mk('div', 'trow');
  /* No status mark here. It belonged in the list, where a row is all a reader
     gets -- but on the open transcript the transcript itself already says what
     is happening: a run in flight ends in the working glyph, and a finished one
     ends in its answer. A second indicator on the title only competes with it. */
  ttl.appendChild(mk('b', null, plainTitle(it.label)));
  const span = agentSpanEl(it);
  if (span) ttl.appendChild(span);
  const cost = agentCost(it);
  if (cost) ttl.appendChild(mk('span', 'tk', cost));
  ttl.appendChild(mk('span', 'who', agentWho(it)));
  hd.appendChild(ttl);
  box.appendChild(hd);

  /* Its own scroller: the run is a transcript of unknown length and must not
     push the header it belongs to off the top of the panel. */
  const stage = mk('div', 'satx');
  box.appendChild(stage);
  if (agentRender) agentRender(stage, agentOpen);
  else stage.appendChild(mk('div', 'wsempty', T('gui.ws.agents_none')));
  agentStartClock();
}

/* What the run did between the question and the answer.
   The transcript is two messages by construction for the lanes that record no
   per-step detail -- what was asked, what came back -- so this view showed a
   sub-agent's whole working life as a prompt and a paragraph. The tool calls
   were being collected all along (an ACP agent reports every one of them as it
   happens) and written only to a tracing span, so the answer to "what did it
   actually do" lived somewhere no reader of this panel would look.

   Returns the calls to draw, or an empty list when there is nothing to add:
   a record that carries the run's own transcript already has these as rows,
   and drawing them again is double bookkeeping. */
function agentFlatCalls(ctx) {
  const calls = (ctx && ctx.tool_calls) || [];
  const msgs = (ctx && ctx.messages) || [];
  if (!calls.length || msgs.length > 2) return [];
  if (msgs.some((m) => m && m.tool_calls && m.tool_calls.length)) return [];
  return calls;
}

/* Drawn with the transcript's own step widget, not a strip of chips: this panel
   exists to read like the conversation that spawned it, and a row of monospace
   chips is a shape the transcript has nowhere else. Placing them between the
   prompt and the answer invents nothing -- the record keeps their order, and
   every one of them happened after the question and before the reply. What it
   does not keep is per-call timing or results, so the rows carry no clock. */
function agentDidStep(calls) {
  const st = newStep();
  calls.forEach((title) => {
    /* Split the same way the transcript splits one, so the two lanes -- a
       record with a transcript and one with only names -- draw alike. A title
       with no program in front keeps a verb that says only that a call
       happened, which is all this record knows. */
    const parts = callParts(title);
    st.tool(parts.display ? parts.name : 'subagent_call', {}, parts.display || String(title)).done(true, '', 0);
  });
  st.seal();
}

/* The fold renderHistory just closed has no clock on it: the turn was drawn in
   two passes so the work could land between the messages, which costs the
   question-to-answer span it would otherwise measure. The record's own start and
   end are that span, and are the truer pair for a delegated run anyway. */
function agentFoldTime(box, ctx) {
  const folds = box.querySelectorAll('.tfold');
  const fold = folds[folds.length - 1];
  if (!fold) return;
  const from = Date.parse((ctx && ctx.started_at) || '');
  const to = Date.parse((ctx && ctx.ended_at) || '');
  if (from && to && to > from) foldTime(fold, dur(to - from));
}

/* Opening a url for real needs the host: overridden in live.js, and honest
   about being a demo here rather than pretending. Opening a path is no longer
   here at all -- that one is DS.workspace.openPath, which the island asks. */
function wsOpenUrl(u) { toast(`demo：正式版会用系统浏览器打开 ${u}`); }

/* ── tool-event hooks ──────────────────────────────────────────────────
   Fed the FULL argument object, because that is where the diff lives. */
function wsOnTool(name, args, silent) {
  const a = wsArgs(name, args);
  const path = a.path || a.file_path || '';
  let hit = null;
  if (name === 'edit_file' && path) {
    hit = wsRecordChange(path, 'edit', hunkFromEdit(a.old_text, a.new_text));
  } else if (name === 'write_file' && path) {
    hit = wsRecordChange(path, 'write', hunkFromWrite(a.content));
  } else if (name === 'web_fetch' && a.url) {
    WS.urls.unshift({ url: String(a.url), kind: 'fetch', at: T('gui.sess.just_now') });
  } else if (name === 'web_search' && a.query) {
    WS.urls.unshift({ url: String(a.query), kind: 'search', at: T('gui.sess.just_now') });
  } else return;

  if (hit && wsOpen && wsTab === 'diff') hit.flash = true;
  /* Draw before counting: the Changes view marks rows seen as it renders, so
     counting first would flash a badge that the very next line clears. */
  if (wsShowsTurn()) drawWs();
  bumpWs();
}

function wsOnToolDone(name, args, ok, preview, ms, diff) {
  const a = wsArgs(name, args);
  /* The tool's own diff is the ground truth -- for a whole-file write it is the
     only record of what was replaced, which the arguments cannot show. It
     replaces the hunk guessed at tool.start. */
  if (diff && diff.length && /^(edit_file|write_file)$/.test(name)) {
    const path = a.path || a.file_path || '';
    const c = WS.changes.find((x) => x.key === path && x.turn === WS.turn);
    if (c) {
      const h = hunkFromUnified(diff);
      const stale = c.hunks.pop();
      if (stale) { c.add -= stale.add; c.del -= stale.del; }
      c.hunks.push(h); c.add += h.add; c.del += h.del;
    }
  }
  if (wsShowsTurn()) drawWs();
  bumpWs();
}

$('#wsBtn').onclick = () => setWs(!wsOpen);
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


