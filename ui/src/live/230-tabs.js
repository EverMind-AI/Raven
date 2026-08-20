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
   Draws a delegated run's record with the transcript's own renderer -- the
   transcript island now, which owns the incremental bookkeeping too (what is
   already drawn, the held-back streaming answer, the working glyph, the
   scroll). Published to the subagents island through the shell's
   agentStagePaint verb; this assignment is what arms that verb in live mode. */
agentPaint = (box, r, opts) => RavenIslands.transcript.agentStage(box, r, opts);
