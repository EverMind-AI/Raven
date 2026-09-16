/* ── subagents: what this conversation handed off ──────────────────────
   Scoped to the open session on every call, so background work from another
   conversation can never surface here; the rows, the records and the heartbeat
   are the subagents feature's own source
   (ui-web/src/features/subagents/source.ts). What is left here is the wiring.

   The `?desk-demo=1` canvas that used to be written out below is an override
   group now (ui-web/src/rpc/fixtures/subagents.ts), applied to whichever
   transport the page chose (src/state/transport.ts) -- so it still works on a
   live page, which is where it has always been applied, and the seven
   instances it draws are answers to `subagents.instances` rather than a second
   source installed over the real one. */

import { agentsSource, startAgentHeartbeat } from '../../features/subagents/source'
import { sources } from '../../state/sources'

/* Everything this part used to do while the concatenated page script ran, in
   the same order. src/legacy/index.js is the only caller. */
export function install() {
  sources.agents = agentsSource;
  startAgentHeartbeat();
}
