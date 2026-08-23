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
  /* The stateful handles. Scoped to the open session like everything else here,
     and answering with no rows on an absent surface for the same reason `list`
     does -- a panel that cannot tell "none" from "not supported" makes the
     reader guess. */
  instances: (sessionId) => {
    if (!rpcHas('subagents')) return Promise.resolve([]);
    return rpc.call('subagents.instances', { session_key: sessionId })
      .then((r) => (r && r.instances) || [])
      .catch((e) => { if (rpcGone('subagents', e)) return []; throw e; });
  },
  /* Addressed by (agent, handle) inside the open session: a handle is unique
     per agent, not globally, so both halves travel. */
  instanceHistory: (agent, handle) =>
    rpc.call('subagents.instance.history', { session_key: cur, agent, handle }).then((r) => r || {}),
  instanceForget: (agent, handle) =>
    rpc.call('subagents.instance.forget', { session_key: cur, agent, handle }).then(() => undefined),
  /* A turn addressed to one instance rather than to the conversation: the same
     `turn.send` the composer uses, with a `target`. An instance's turn runs on
     its own lane, so it is concurrent with the main agent's and with every other
     instance's, and is refused only by *that* instance still answering. */
  instanceSend: (agent, handle, text) =>
    rpc.call('turn.send', { session_key: cur, content: text, target: { agent, handle } })
      .then(() => undefined),
  /* The heartbeat, forwarded rather than acted on: a run in flight has to
     move on screen without being reopened, and every judgement about what
     that takes belongs to the island that is drawing it. */
  watch: (fn) => { agentsWatch = fn; },
  /* Draws a delegated run's record with the transcript's own renderer -- the
     transcript island, which owns the incremental bookkeeping too (what is
     already drawn, the held-back streaming answer, the working glyph, the
     scroll). Reached through the shell's agentStagePaint verb, which forwards
     to whatever this source offers; a member here rather than its own binding,
     because a painter is only ever wanted for a record, and this is the source
     the records come from. */
  stagePaint: (box, r, opts) => RavenIslands.transcript.agentStage(box, r, opts),
};
setInterval(() => { if (agentsWatch) agentsWatch(); }, 2000);
