/* -- external agents: the rpc source ---------------------------------
   `subagents.*` is one surface shared with the TUI and the web UI: the rows,
   the install grouping and the write path all live server-side, so this layer
   only maps a row into what the page draws and sends the mutation back. The
   xa island (ui/src/features/xa/) owns the renderer and every flag it reads;
   installing onto the seam replaces the fixture source before the first paint.

   The list is re-fetched after every mutation rather than patched locally: the
   handler recomputes `group`, `enabled` and the probe verdict together, and a
   client that guesses any one of them is how the page starts disagreeing with
   the config on disk. `probe: false` on that follow-up call skips the
   availability check, which can cost up to ten seconds per entry and would only
   re-measure what the write just changed. */
function xaRowOf(r) {
  return {
    name: r.name,
    preset: r.preset,
    kind: r.kind || (r.builtin ? 'builtin' : 'cli'),
    configured: !!r.configured,
    /* Raven's own agents. On the table whether or not config mentions them, so
       they are `configured: false` yet not something to install -- the page needs
       both facts to avoid offering a Connect button for a loop already running. */
    builtin: !!r.builtin,
    /* The Raven builds this install shipped, discovered under `subagents/`.
       Same shape of problem as `builtin` and the same reason it has to be
       carried explicitly: `configured: false` with nothing to install, so the
       page needs the flag to keep a Connect button off a row that has no preset
       to connect from. This mapper is a whitelist -- a field it does not name is
       a field the island never sees. */
    vendored: !!r.vendored,
    /* A build of this folder's venv is in flight. Carried because the row is the
       only place the page learns it: `subagents.build` returns the moment the
       build starts, so the button's own promise resolving proves nothing about
       whether it finished. */
    building: !!r.building,
    enabled: !!r.enabled,
    probe_status: r.probe_status || 'unknown',
    upgrade_to: r.upgrade_to || null,
    probe_detail: r.probe_detail || '',
    has_api_key: !!r.has_api_key,
    description: r.description || '',
    last_test_ok: r.last_test_ok,
    last_test_detail: r.last_test_detail || '',
    last_test_at_ms: r.last_test_at_ms || null,
    test_running: !!r.test_running,
  };
}

/* What the last fetch reported, kept here rather than read back off the page:
   the carry-over below is a fact about this transport (a probe-less list says
   "unknown" for every row), so the source answers it from its own memory
   instead of reaching into the array the page is rendering. */
let xaSeen = new Map();

async function xaFetch(probe) {
  const res = await rpc.call('subagents.list', { probe: !!probe });
  /* A probe-less list reports every row as "unknown", which would blank the
     health line of a row that was ready a second ago -- connecting an agent
     would look like it broke it. The verdict cannot have changed by writing
     config, so the last known one is carried over. */
  const rows = (res.rows || []).map((r) => {
    const row = xaRowOf(r);
    const prev = xaSeen.get(row.name);
    if (row.probe_status === 'unknown' && prev && prev.probe_status !== 'unknown') {
      row.probe_status = prev.probe_status;
      row.probe_detail = prev.probe_detail;
    }
    return row;
  });
  xaSeen = new Map(rows.map((r) => [r.name, r]));
  return rows;
}

DS.xa = {
  load: (probe) => xaFetch(!!probe),
  act: async (op, row, args) => {
    const a = args || {};
    if (op === 'connect') {
      /* Only name / description / key travel: every execution field comes from
         the preset server-side. A page that could post a command line would make
         "which agent is this" unanswerable. */
      await rpc.call('subagents.add', {
        preset: row.preset || row.name,
        name: a.new_name || undefined,
        description: a.description || undefined,
        api_key: a.api_key || undefined,
      });
    } else if (op === 'update') {
      await rpc.call('subagents.update', {
        name: row.name,
        new_name: a.new_name && a.new_name !== row.name ? a.new_name : undefined,
        description: a.description,
        api_key: a.api_key || undefined,
      });
    } else if (op === 'toggle') {
      /* One switch for every kind of row. A discovered folder has no config
         entry, so the server writes one from the folder's own manifest and puts
         the flag on it -- which is why connecting one back on is this same call
         and not a second mechanism. */
      await rpc.call('subagents.toggle', { name: row.name, enabled: !!a.enabled });
    } else if (op === 'migrate') {
      /* A stale transport is the one connect that cannot be a flag. There is no
         "change the transport" write -- `subagents.update` touches name,
         description and key only, on purpose -- so it is a remove plus an add
         from the preset, which is also what makes it visible in config as one
         entry replaced rather than an entry mutated underneath its session
         handles. Toggling `enabled` would have left `upgrade_to` standing and
         the old command line in place. */
      await rpc.call('subagents.remove', { name: row.name });
      await rpc.call('subagents.add', {
        preset: row.preset || row.name,
        name: row.name,
        description: row.description || undefined,
      });
    } else if (op === 'build') {
      /* Returns as soon as the build is under way, not when it is done: it is a
         few hundred MB of downloads. The row's `building` flag is what says it is
         still going, and the store polls the list while any row carries it. */
      await rpc.call('subagents.build', { name: row.name });
    }
    return xaFetch(false);
  },
};

/* ── the dag sheet ───────────────────────────────────────
   The graph a `run_subagent_dag` call is orchestrating, docked above the
   composer on the conversation that asked for it. Drawn by the island
   (ui/src/features/dag/), which holds the runs as well: what is left on this
   side is the events, and the panel chrome a node click opens.

   The three `dag.*` branches in live/050-turn.js feed it; the two calls below
   are the ones that need this file's own chrome. */

/* A node opens where a sub-agent's work already lives, rather than growing a
   second transcript view inside the sheet: same panel, same renderer, and the
   sheet stays the map rather than becoming the territory. */
function dagOpenNode(runId, n) {
  RavenIslands.subagents.openDagNode(runId, n);
  /* The open above already raised the node's own window, and in desk mode that
     window IS the view -- so there is no panel tab left to pick. Picking one
     anyway routed through `openDeskTab`, whose whole job is to open the
     palette, so every node opened from the trail's card or from the sheet
     popped the little desk open beside the window the reader had asked for.
     The lines below are the pre-desk panel, where selecting the agents view
     was how the instance got on screen at all. */
  if (document.documentElement.classList.contains('desk-ready')) return;
  if (!wsOpen) setWs(true);
  wsPick('agents');
  drawWs();
}
/* The trail's dag card opens a node through the same reader. Installed on the
   transcript source rather than into a page binding the fixture layer declared:
   the island asks its source for these three, and this is the layer that can
   answer. Assigned as fields, the way live/060-parked.js and
   live/190-session-actions.js add theirs -- the source object itself was built
   back in live/040-history.js. */
DS.transcript.openDagNode = (runId, nodeId) => dagOpenNode(runId, { id: nodeId });

/* Per-node status for a card whose events are long gone: `dag.get` reads the
   run back off disk, reconciled against the registry, so a graph reopened from
   history shows what actually happened rather than a row of pending dots. */
/* The rows as the server sends them, unreduced. They used to be mapped down to
   four fields here, which is why a card restored from history could never show a
   dependency, a prompt template or an input -- a field this mapper did not name
   was a field the card could not have. The shape the card wants is decided by the
   adapter that reads it (ui/src/features/dag/nodes.ts), not by this seam. */
DS.transcript.dagRun = (runId) => rpc.call('dag.get', { run_id: runId, session_key: sessionCurrent() })
  .then((r) => (r && r.run) || {});

/* One spawned run's messages so far. Answers a MOVING stream while the run is
   live: the record's own transcript.jsonl is only written when the run finishes,
   and until then `subagent.context` serves the activity collector's copy, which
   the acp backend republishes on every update it receives. */
DS.transcript.spawnRecord = (callId) =>
  rpc.call('subagent.context', { id: callId, session_id: sessionCurrent() });

/* Every delegated call this conversation made. Read once per conversation, to
   turn a restored card's task id into the record id its stream is read by: a
   record's directory is `<stamp>-<task_id>`, so the row is found by suffix. */
DS.transcript.spawnList = () => {
  /* Guarded like DS.agents.list is: a server without the subagent surface answers
     -32601, and a card that asked would then re-ask on every reopen for an answer
     that cannot arrive. */
  if (!rpcHas('subagent')) return Promise.resolve([]);
  return rpc.call('subagent.list', { session_id: sessionCurrent() })
    .then((r) => (r && r.items) || []);
};

/* "View in workspace" on a spawn row: open the panel on the run's own record,
   not just on the list. The list may not have caught the new run yet, so a
   couple of short retries cover the gap between the call and its row. */
DS.transcript.openSpawn = (agent, label) => {
  /* Same rule as dagOpenNode: `openRow` below raises the window, and in desk
     mode that is the whole answer. The panel's agents view is only needed where
     there are no windows. */
  if (!document.documentElement.classList.contains('desk-ready')) setWs(true, 'agents');
  const match = () => RavenIslands.subagents.rows().find((x) => x.kind !== 'dag'
    && (!label || plainTitle(x.label) === plainTitle(label))
    && (!agent || (x.agent || 'raven') === (agent || 'raven')));
  const attempt = (n) => {
    const it = match();
    if (it) { RavenIslands.subagents.openRow(it); return; }
    if (n >= 4) {
      /* Nothing was found, so nothing was opened -- and a click that opens
         nothing reads as broken. `refresh` keeps the drawn list on a failed
         read rather than emptying it, so a gateway hiccup or a label the
         registry spells differently lands here, and the reader is left with a
         list they can search by hand. Only on this branch: the palette beside
         a window the reader did get is the thing this whole change removes. */
      RavenIslands.workspace.openDeskTab('agents');
      return;
    }
    RavenIslands.subagents.refresh(true);
    setTimeout(() => attempt(n + 1), 700);
  };
  attempt(0);
};

/* Dev-only hook, beside __clarify and __approve and for the same reason: the
   graph is only reachable by configuring third-party sub-agents and spending a
   multi-agent run, which is too long a loop to design a layout in.
   `window.__dag()` feeds the same three events the server sends. */
window.__dag = (ev) => onEvent(ev && ev.type ? ev : {
  type: 'dag.run_started',
  payload: {
    run_id: '20260812T120000Z-deadbeef',
    nodes: [
      { id: 'survey', subagent: 'Researcher', depends_on: [] },
      { id: 'read_a', subagent: 'Researcher', depends_on: ['survey'] },
      { id: 'read_b', subagent: 'Coder', depends_on: ['survey'] },
      { id: 'read_c', subagent: 'Coder', instance: 'w2', depends_on: ['survey'] },
      { id: 'merge', subagent: 'Writer', depends_on: ['read_a', 'read_b', 'read_c'] },
      { id: 'review', subagent: 'Critic', depends_on: ['merge'] },
    ],
  },
});

/* This is the live manifest's last part. Every synchronous DS installer is now
   in place, so the first data-driven paint cannot observe a fixture source. */
queueMicrotask(bootPage);

})();
