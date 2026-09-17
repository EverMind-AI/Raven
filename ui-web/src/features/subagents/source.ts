/* What this conversation handed off: the roster, the delegated calls, and the
 * instances a reader can talk to directly.
 *
 * Scoped to the open session on every call, so background work from another
 * conversation can never surface here. Which of them is on screen, when to ask
 * again and whether an answer is worth a repaint are all about what is drawn,
 * and they live with the renderer (ui-web/src/features/subagents/).
 *
 * A server without the surface answers -32601 forever otherwise, and the panel
 * would sit empty with no way to tell an empty list from a missing feature --
 * so an absent surface answers with no rows rather than an error, and `absent`
 * is what the island's empty state reads to tell the two apart.
 */

import { islands } from '../../islands'
import { current as sessionCurrent } from '../../lib/session'
import { gone, has } from '../../rpc/capabilities'
import { gateway } from '../../state/gateway'
import { mediaOf } from '../../state/session/runtime'

import type { AgentCtxLike } from '../transcript/store'
import type { AgentsSource } from './types'

/* Every call here is addressed to the conversation on screen, and every caller
   is a panel that only exists inside one. */
const openKey = (): string => sessionCurrent() as string

/* The heartbeat's one subscriber. A run in flight has to move on screen
   without being reopened, and there is no push for it. */
let agentsWatch: (() => void) | null = null

export const agentsSource: AgentsSource = {
  /* Filtered on whether the agent can be dispatched, not on where it came
   from. It filtered `vendored` -- which is true of every agent that ships
   WITH raven -- so Raven-Code, Raven-PPT and Raven-Research were absent from
   the roster while `enabled: false` rows (an uninstalled acp preset, a
   disabled cli) were listed as if they were available. An agent that is off
   but has instances still gets a group: `orderAgentGroups` unions these names
   with the ones on the instance rows, so its history stays reachable. */
  roster: () => gateway().call('subagents.list', { probe: false })
    .then((r) => (r.rows || []).filter((row) => row.enabled)),
  list: (sessionId: string) => {
    if (!has('subagent')) return Promise.resolve([])
    return gateway().call('subagent.list', { session_id: sessionId })
      .then((r) => (r && r.items) || [])
      /* Absent surface -> no rows (the island words that empty state); a call
         that merely failed rethrows, so the page keeps what it last drew --
         a dropped socket must not repaint a live run as "no delegated work". */
      .catch((e) => { if (gone('subagent', e)) return []; throw e })
  },
  /* A call is addressed by conversation and call, not by call alone: its
   record lives inside that conversation's own directory. A dag node is
   addressed by (run, node), reconciled server-side against the registry. */
  context: (id: string) => gateway().call('subagent.context', { id, session_id: openKey() }),
  node: (runId: string, node: string) => gateway().call('dag.node', { run_id: runId, node, session_key: openKey() })
    .then((r) => (r && r.node) || {}),
  absent: () => !has('subagent'),
  /* The stateful handles. Scoped to the open session like everything else here,
   and answering with no rows on an absent surface for the same reason `list`
   does -- a panel that cannot tell "none" from "not supported" makes the
   reader guess. */
  instances: (sessionId: string) => {
    if (!has('subagents')) return Promise.resolve([])
    return gateway().call('subagents.instances', { session_key: sessionId })
      .then((r) => (r && r.instances) || [])
      .catch((e) => { if (gone('subagents', e)) return []; throw e })
  },
  /* Addressed by (agent, handle) inside the open session: a handle is unique
   per agent, not globally, so both halves travel. */
  instanceHistory: (agent: string, handle: string) =>
    gateway().call('subagents.instance.history', { session_key: openKey(), agent, handle }).then((r) => r || {}),
  instanceForget: (agent: string, handle: string) =>
    gateway().call('subagents.instance.forget', { session_key: openKey(), agent, handle }).then(() => undefined),
  /* A turn addressed to one instance rather than to the conversation: the same
   `turn.send` the composer uses, with a `target`. An instance's turn runs on
   its own lane, so it is concurrent with the main agent's and with every other
   instance's, and is refused only by *that* instance still answering. The
   attachment note the instance composer bakes into the text becomes the typed
   `media` field here, by the one rule the page composer's sends follow
   (`mediaOf`): a direct chat's files reach the sub-agent by path only when
   this call carries them, and it used to carry none. */
  instanceSend: (agent: string, handle: string, text: string) =>
    gateway().call('turn.send', { session_key: openKey(), content: text, target: { agent, handle }, ...mediaOf(text) })
      .then(() => undefined),
  instanceCreate: (agent: string, sessionKey: string) =>
    gateway().call('subagents.instance.create', { agent, session_key: sessionKey })
      .then((r) => r && r.instance),
  /* One reply serves report and set alike, and carries this agent's own rungs
   with it, so the control needs no second call for its menu. `null` clears the
   override rather than setting a mode -- there is no sentinel id for it, and
   an agent is free to call one of its own modes "default". */
  instanceMode: (agent: string, handle: string) =>
    gateway().call('subagents.instance.set_mode', { session_key: openKey(), agent, handle })
      .then((r) => r || {}),
  instanceSetMode: (agent: string, handle: string, mode: string | null) =>
    gateway().call('subagents.instance.set_mode', mode === null
      ? { session_key: openKey(), agent, handle, clear: true }
      : { session_key: openKey(), agent, handle, mode })
      .then((r) => r || {}),
  /* The model beside the mode, on the same three-call shape: neither field
   reports, `clear` drops the override, a value switches. Nothing is inherited
   here -- cleared means the agent's own choice, which this host cannot name --
   so there is no second field to read back. */
  instanceModel: (agent: string, handle: string) =>
    gateway().call('subagents.instance.set_model', { session_key: openKey(), agent, handle })
      .then((r) => r || {}),
  instanceSetModel: (agent: string, handle: string, model: string | null) =>
    gateway().call('subagents.instance.set_model', model === null
      ? { session_key: openKey(), agent, handle, clear: true }
      : { session_key: openKey(), agent, handle, model })
      .then((r) => r || {}),
  /* The heartbeat, forwarded rather than acted on: a run in flight has to
   move on screen without being reopened, and every judgement about what
   that takes belongs to the island that is drawing it. */
  watch: (fn: () => void) => { agentsWatch = fn },
  /* Draws a delegated run's record with the transcript's own renderer -- the
   transcript island, which owns the incremental bookkeeping too (what is
   already drawn, the held-back streaming answer, the working glyph, the
   scroll). A member here rather than its own binding, because a painter is
   only ever wanted for a record, and this is the source the records come
   from. */
  stagePaint: (box, r, opts) => islands.transcript.agentStage(box, r as AgentCtxLike | null, opts),
}

/* Two seconds, for the life of the tab whether or not the panel is open: the
   absent watcher is the ordinary case, not an error. */
export function startAgentHeartbeat(): void {
  setInterval(() => { if (agentsWatch) agentsWatch() }, 2000)
}
