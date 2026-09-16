/* -- external agents and the dag sheet: the seam ----------------------
   The `subagents.*` source is ui-web/src/features/xa/source.ts; what is left
   here is its install, plus the three delegation verbs the transcript source
   grows in live mode and the panel chrome a node click opens. */

import { plainTitle } from '../../features/rail/title'
import { xaSource } from '../../features/xa/source'
import { islands } from '../../islands'
import { has } from '../../rpc/capabilities'
import { current as sessionCurrent } from '../../shell/session'
import { gateway } from '../../state/gateway'
import { sources } from '../../state/sources'
import { drawWs, setWs, wsOpen, wsPick } from '../demo/100-workspace.js'
import { bootPage } from '../demo/160-boot.js'
import { onEvent } from './050-turn.js'

/* ── the dag sheet ───────────────────────────────────────
   The graph a `run_subagent_dag` call is orchestrating, docked above the
   composer on the conversation that asked for it. Drawn by the island
   (ui-web/src/features/dag/), which holds the runs as well: what is left on this
   side is the events, and the panel chrome a node click opens.

   The three `dag.*` branches in live/050-turn.js feed it; the two calls below
   are the ones that need this file's own chrome. */

/* A node opens where a sub-agent's work already lives, rather than growing a
   second transcript view inside the sheet: same panel, same renderer, and the
   sheet stays the map rather than becoming the territory. */
function dagOpenNode(runId, n) {
  islands.subagents.openDagNode(runId, n);
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

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.xa = xaSource;

  /* The trail's dag card opens a node through the same reader. Installed on the
   transcript source rather than into a page binding the fixture layer declared:
   the island asks its source for these three, and this is the layer that can
   answer. Assigned as fields, the way live/060-parked.js and
   live/190-session-actions.js add theirs -- the source object itself was built
   back in live/040-history.js. */
  sources.transcript.openDagNode = (runId, nodeId, summary) => dagOpenNode(runId, { id: nodeId, summary });

  /* Per-node status for a card whose events are long gone: `dag.get` reads the
   run back off disk, reconciled against the registry, so a graph reopened from
   history shows what actually happened rather than a row of pending dots. */
  /* The rows as the server sends them, unreduced. They used to be mapped down to
   four fields here, which is why a card restored from history could never show a
   dependency, a prompt template or an input -- a field this mapper did not name
   was a field the card could not have. The shape the card wants is decided by the
   adapter that reads it (ui-web/src/features/dag/nodes.ts), not by this seam. */
  sources.transcript.dagRun = (runId) => gateway().call('dag.get', { run_id: runId, session_key: sessionCurrent() })
    .then((r) => (r && r.run) || {});

  /* One spawned run's messages so far. Answers a MOVING stream while the run is
   live: the record's own transcript.jsonl is only written when the run finishes,
   and until then `subagent.context` serves the activity collector's copy, which
   the acp backend republishes on every update it receives. */
  sources.transcript.spawnRecord = (callId) =>
    gateway().call('subagent.context', { id: callId, session_id: sessionCurrent() });

  /* Every delegated call this conversation made. Read once per conversation, to
   turn a restored card's task id into the record id its stream is read by: a
   record's directory is `<stamp>-<task_id>`, so the row is found by suffix. */
  sources.transcript.spawnList = () => {
    /* Guarded like sources.agents.list is: a server without the subagent surface answers
     -32601, and a card that asked would then re-ask on every reopen for an answer
     that cannot arrive. */
    if (!has('subagent')) return Promise.resolve([]);
    return gateway().call('subagent.list', { session_id: sessionCurrent() })
      .then((r) => (r && r.items) || []);
  };

  /* "View in workspace" on a spawn row: open the panel on the run's own record,
   not just on the list. The list may not have caught the new run yet, so a
   couple of short retries cover the gap between the call and its row. */
  sources.transcript.openSpawn = (agent, label) => {
    /* Same rule as dagOpenNode: `openRow` below raises the window, and in desk
     mode that is the whole answer. The panel's agents view is only needed where
     there are no windows. */
    if (!document.documentElement.classList.contains('desk-ready')) setWs(true, 'agents');
    const match = () => islands.subagents.rows().find((x) => x.kind !== 'dag'
      && (!label || plainTitle(x.label) === plainTitle(label))
      && (!agent || (x.agent || 'raven') === (agent || 'raven')));
    const attempt = (n) => {
      const it = match();
      if (it) { islands.subagents.openRow(it); return; }
      if (n >= 4) {
        /* Nothing was found, so nothing was opened -- and a click that opens
         nothing reads as broken. `refresh` keeps the drawn list on a failed
         read rather than emptying it, so a gateway hiccup or a label the
         registry spells differently lands here, and the reader is left with a
         list they can search by hand. Only on this branch: the palette beside
         a window the reader did get is the thing this whole change removes. */
        islands.workspace.openDeskTab('agents');
        return;
      }
      islands.subagents.refresh(true);
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

  /* This is the live manifest's last part. Every synchronous source installer
   is now in place, so the first data-driven paint cannot observe a fixture
   source. */
  queueMicrotask(bootPage);
}

export { dagOpenNode }
