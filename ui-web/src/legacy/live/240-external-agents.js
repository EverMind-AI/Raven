/* -- external agents and the dag sheet: the seam ----------------------
   The `subagents.*` source is ui-web/src/features/xa/source.ts, the five
   delegation verbs the transcript source grows in live mode are
   ui-web/src/features/transcript/source.ts, and the panel chrome a node click
   opens is ui-web/src/features/dag/open.ts. What is left here is the wiring,
   the dev hook, and the queued first paint this part has always ended with. */

import { dagOpenNode } from '../../features/dag/open'
import { dagRun, openDagNode, openSpawn, spawnList, spawnRecord } from '../../features/transcript/source'
import { xaSource } from '../../features/xa/source'
import { dispatch } from '../../state/session/pipeline'
import { sources } from '../../state/sources'
import { bootPage } from '../demo/160-boot.js'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.xa = xaSource;

  /* The trail's dag card opens a node through the same reader. Installed on the
   transcript source rather than into a page binding the fixture layer declared:
   the island asks its source for these five, and this is the layer that can
   answer. Assigned as fields, the way live/190-session-actions.js adds its
   own -- the source object itself was built back in live/040-history.js. */
  sources.transcript.openDagNode = openDagNode;
  sources.transcript.dagRun = dagRun;
  sources.transcript.spawnRecord = spawnRecord;
  sources.transcript.spawnList = spawnList;
  sources.transcript.openSpawn = openSpawn;

  /* Dev-only hook, beside __clarify and __approve and for the same reason: the
   graph is only reachable by configuring third-party sub-agents and spending a
   multi-agent run, which is too long a loop to design a layout in.
   `window.__dag()` feeds the same three events the server sends. */
  window.__dag = (ev) => dispatch(ev && ev.type ? ev : {
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
