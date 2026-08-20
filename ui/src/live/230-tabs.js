/* ── subagents: what this conversation handed off ──────────────────────
   Scoped to the open session on every call, so background work from another
   conversation can never surface here. */
/* The rows, and only the rows: which of them is on screen, when to ask again
   and whether the answer is worth a repaint are all about what is drawn, and
   they live with the renderer in demo/110-subagents.js.

   A server without the surface answers -32601 forever otherwise, and the panel
   would sit empty with no way to tell an empty list from a missing feature --
   so an absent surface answers with no rows rather than an error, and
   `rpcAbsent` is what the empty state reads to tell the two apart. */
DS.agents = {
  list: (sessionId) => {
    if (!rpcHas('subagent')) return Promise.resolve([]);
    return rpc.call('subagent.list', { session_id: sessionId })
      .then((r) => (r && r.items) || [])
      /* Absent surface -> no rows (the shell words that empty state); a call
         that merely failed rethrows, so the page keeps what it last drew --
         a dropped socket must not repaint a live run as "no delegated work". */
      .catch((e) => { if (rpcGone('subagent', e)) return []; throw e; });
  },
};

/* One fingerprint per drawn detail: the poll repaints only when the answer
   actually changed, because a redraw every tick reads as flicker and eats any
   text selection the reader had. */
/* What has already been drawn for the open run. `drawn` counts *messages*, not
   nodes: a poll appends the ones that arrived since the last, and never touches
   what is on screen. Rebuilding the whole transcript every five seconds -- which
   is what a running record's poll used to do -- tore out whatever the reader was
   in the middle of, closed every fold they had opened, and dropped the selection.
   The scroll survived it and nothing else did. */
const agentDrawn = { key: null, drawn: 0, status: null };

function agentPaint(box, r, opts) {
  const msgs = (r && r.messages) || [];
  const running = r && r.status === 'run';
  const key = (opts && opts.key) || `sp:${agentOpen}`;
  const fresh = agentDrawn.key !== key;
  if (fresh) { box.innerHTML = ''; agentDrawn.key = key; agentDrawn.drawn = 0; }
  agentDrawn.status = r && r.status;

  /* An answer being streamed is held back until it settles: drawing it is what
     would force a redraw of it a moment later, and it is why an answer in
     flight carries no footer here, exactly as a live turn in the transcript
     carries none until it settles.
     Only an *assistant* message though. A user prompt is never rewritten, and
     for most of a run it is the only message there is -- holding it back drew
     the working glyph over an empty panel, so a reader watching a run could
     not tell which run they were watching. */
  const last = msgs[msgs.length - 1];
  const streaming = running && last && last.role === 'assistant';
  const commit = streaming ? msgs.length - 1 : msgs.length;
  const tail = msgs.slice(agentDrawn.drawn, commit);

  const sc = box;
  const atEnd = sc.scrollTop + sc.clientHeight >= sc.scrollHeight - 4;
  const top = sc.scrollTop;

  /* The working glyph is the tail of the box, so it is kept aside while
     messages are appended and put back after -- kept, not rebuilt: re-creating
     it restarts its CSS animation, and this paint runs on every poll, so a
     fresh glyph each time is a glyph that never finishes a cycle. The turn's
     own live row learned this first; see `drawTurnLive`. */
  const glyph = box.querySelector(':scope > .sarun');
  const empty = box.querySelector(':scope > .wsempty');
  if (empty) empty.remove();

  /* What it did, between what was asked and what came back -- only for the
     lanes whose record holds call names and nothing else; a real transcript
     carries its own rows. It is a whole-record shape, so it is drawn once, on
     the first paint. */
  const did = fresh ? agentFlatCalls(r) : [];
  if (tail.length || did.length) {
    /* The one line that makes this a subagent view rather than a second
       renderer: point the transcript's write target at this box, draw with
       the ordinary one, then put it back. Synchronous, so no live event can
       land in between. */
    const prev = stageHost;
    stageHost = box;
    try {
      if (did.length) {
        renderHistory(tail.filter((m) => m && m.role === 'user'));
        agentDidStep(did);
        renderHistory(tail.filter((m) => !(m && m.role === 'user')));
        agentFoldTime(box, r);
      } else {
        renderHistory(tail);
      }
    } finally { stageHost = prev; }
    agentDrawn.drawn = commit;
  }

  if (running) {
    /* The glyph alone, as the reader's own turn wears it. The word beside it
       ("Working") named the one thing the moving glyph already says, in a panel
       whose header carries the status too -- three ways of saying running, and
       the only one that survives a glance is the movement. */
    let w = glyph;
    if (!w) {
      w = mk('div', 'act in sarun');
      w.append(workGlyph());
    }
    if (box.lastElementChild !== w) box.appendChild(w);
  } else if (glyph) {
    glyph.remove();
  }
  if (!box.childElementCount) {
    box.appendChild(mk('div', 'wsempty', (opts && opts.empty) || T('gui.ws.agents_none')));
  }
  box.scrollTop = atEnd ? box.scrollHeight : top;
}

agentRender = (box, id) => {
  /* Which panel asked. The header below is reached through the document rather
     than through `box`, so an answer for a run the reader has already left
     would find whichever header is open now and rename it. The rest of this
     callback is safe on its own -- it writes into a detached box -- but a
     write to the live document has to know it is still the right document. */
  const epoch = wsEpoch;
  /* A call is addressed by conversation and call, not by call alone: its record
     lives inside that conversation's own directory. */
  agentDrawn.key = null;
  rpc.call('subagent.context', { id, session_id: cur })
    .then((r) => {
      if (wsStale(epoch)) return;
      /* The header drew from the listed row. Opened without one -- a reopened
         panel, a run that has aged out of the list -- it fell back to raven's
         own sub-agent, which would quietly mislabel an openclaw run. The answer
         carries the truth, so correct it on arrival. */
      const who = document.querySelector('.sahd .trow .who');
      if (who && r) who.textContent = (r.agent || 'raven');
      agentPaint(box, r);
    })
    .catch((e) => {
      box.innerHTML = '';
      box.appendChild(mk('div', 'wsempty', (e && e.message) || String(e)));
    });
};

/* A run in flight has to move on screen without being reopened, and its header
   has to change the moment the run does -- a detail page still saying "working"
   over a run the list already knows failed is the panel lying. */
setInterval(() => {
  if (!wsOpen || wsTab !== 'agents') return;
  if (!agentOpen && !dagNode) { agentsRefresh(); return; }
  agentsRefresh(true);
  /* A graph node is a row like any other now, so the same rule applies. It is
     found by (run, node) rather than by id because that is how the list
     addresses it. */
  const it = dagNode
    ? AGENTS.find((a) => a.kind === 'dag' && a.run_id === dagNode.run_id && a.node === dagNode.node)
    : AGENTS.find((a) => a.id === agentOpen);
  const stNow = it ? it.status : null;
  if (agentDrawn.status && stNow && agentDrawn.status !== stNow) {
    /* Status flipped under an open page: redraw the whole view once, so the
       header mark and the transcript land on the final state together. */
    agentDrawn.status = stNow;
    drawWs();
    return;
  }
  if (!it || it.status !== 'run') return;
  const box = document.querySelector('.satx');
  if (!box) return;
  if (dagNode) { dagNodePaint(box, dagNode); return; }
  rpc.call('subagent.context', { id: agentOpen, session_id: cur })
    .then((r) => {
      if (!document.body.contains(box)) return;
      agentPaint(box, r);
    })
    .catch(() => {});
}, 2000);

