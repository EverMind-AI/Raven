/* ── subagents: what this conversation handed off ──────────────────────
   Scoped to the open session on every call, so background work from another
   conversation can never surface here. */
/* The rows and the records, and only those: which of them is on screen, when
   to ask again and whether the answer is worth a repaint are all about what
   is drawn, and they live with the renderer -- the subagents island
   (ui/src/features/subagents/).

   A server without the surface answers -32601 forever otherwise, and the panel
   would sit empty with no way to tell an empty list from a missing feature --
   so an absent surface answers with no rows rather than an error, and
   `absent` is what the island's empty state reads to tell the two apart. */
let agentsWatch = null;
DS.agents = {
  list: (sessionId) => {
    if (!rpcHas('subagent')) return Promise.resolve([]);
    return rpc.call('subagent.list', { session_id: sessionId })
      .then((r) => (r && r.items) || [])
      /* Absent surface -> no rows (the island words that empty state); a call
         that merely failed rethrows, so the page keeps what it last drew --
         a dropped socket must not repaint a live run as "no delegated work". */
      .catch((e) => { if (rpcGone('subagent', e)) return []; throw e; });
  },
  /* A call is addressed by conversation and call, not by call alone: its
     record lives inside that conversation's own directory. A dag node is
     addressed by (run, node), reconciled server-side against the registry. */
  context: (id) => rpc.call('subagent.context', { id, session_id: cur }),
  node: (runId, node) => rpc.call('dag.node', { run_id: runId, node, session_key: cur })
    .then((r) => (r && r.node) || {}),
  absent: () => !rpcHas('subagent'),
  /* The heartbeat, forwarded rather than acted on: a run in flight has to
     move on screen without being reopened, and every judgement about what
     that takes belongs to the island that is drawing it. */
  watch: (fn) => { agentsWatch = fn; },
};
setInterval(() => { if (agentsWatch) agentsWatch(); }, 2000);

/* ── the transcript bridge ─────────────────────────────────────────────
   Draws a delegated run's record with the transcript's own renderer, which
   lives in this scope (renderHistory) -- so the bridge does too, published to
   the island through the shell's agentStagePaint verb. */

/* What has already been drawn for the open run. `drawn` counts *messages*, not
   nodes: a poll appends the ones that arrived since the last, and never touches
   what is on screen. Rebuilding the whole transcript every few seconds -- which
   is what a running record's poll used to do -- tore out whatever the reader was
   in the middle of, closed every fold they had opened, and dropped the selection.
   The scroll survived it and nothing else did. */
const agentDrawn = { key: null, drawn: 0 };

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

agentPaint = function (box, r, opts) {
  const msgs = (r && r.messages) || [];
  const running = r && r.status === 'run';
  const key = (opts && opts.key) || '';
  /* A reopened panel draws into a new box, so the paint has to start over
     even though the run it is drawing has not changed: the island says so. */
  if (opts && opts.reset) agentDrawn.key = null;
  const fresh = agentDrawn.key !== key;
  if (fresh) { box.innerHTML = ''; agentDrawn.key = key; agentDrawn.drawn = 0; }

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
};
