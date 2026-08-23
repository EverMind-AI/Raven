import type { DirectTurn, InstanceRow } from '../../rpc/generated'

export type { DirectTurn, InstanceRow }

/* One row per delegated run, both kinds: a spawned call is addressed by its
   call id, a graph node by (run, node) -- which is the whole reason a row
   says its `kind`. The shapes mirror what subagent.list returns and what the
   fixture source fabricates. */
export interface AgentRow {
  id?: string
  kind?: string
  run_id?: string
  node?: string
  agent?: string | null
  label?: string
  status?: string
  started_at?: string
  ended_at?: string
  tokens?: number | null
}

/* A spawned run's whole record, as subagent.context answers it. The island
   never reads the messages itself -- they go straight to the legacy
   transcript bridge (the shell's agentStagePaint verb). */
export interface AgentCtx {
  status?: string
  agent?: string | null
  started_at?: string
  ended_at?: string
  messages?: unknown[]
  tool_calls?: unknown[]
}

/* One dag node's record, normalised by the source (dag.node's `node`). */
export interface DagNodeCtx {
  messages?: unknown[]
  output_truncated?: boolean
}

/* `InstanceRow` and `DirectTurn` are re-exported from the generated contract at
   the top of this file rather than restated here. The hand-written copies they
   replace are what let the page read `messages` off an answer that carries
   `turns`, and a turn's text off `content` when the renderer reads `text`: two
   shapes that disagreed in three places while `tsc` saw no problem.

   One instance's conversation, exactly as `subagents.instance.history` answers
   it. Optional because the seam tolerates an empty answer from an older server. */
export interface InstanceChat {
  turns?: DirectTurn[]
}

/* What the transcript renderer takes, as much of it as one instance can fill.
   Declared here rather than imported from the transcript island for the same
   reason `stagePaint` below takes `unknown`: the value reaches the painter
   through the shell's untyped verb, and this is the shape this island is
   answerable for producing. */
export interface InstanceCtx {
  status?: string
  messages: Array<{
    role: string
    text: string
    timestamp?: number
    tool_call_id?: string
    reasoning_content?: string
    tool_calls?: Array<{ id?: string; name?: string; arguments?: string }>
  }>
}

export interface AgentsSource {
  list(sessionId: string): Promise<AgentRow[]>
  /* Live-only: the fixture replay records no per-run context, so the demo
     detail keeps its empty note exactly as the legacy renderer did. */
  context?(id: string): Promise<AgentCtx>
  node?(runId: string, node: string): Promise<DagNodeCtx>
  /* An empty list and a server that cannot list are different things; the
     empty state reads this to tell them apart. */
  absent?(): boolean
  /* The stateful handles, on their own axis from the runs above. Optional for
     the same reason `context` is: a server without the surface has none to hand
     out, and the panel says so rather than showing an empty group. */
  instances?(sessionId: string): Promise<InstanceRow[]>
  instanceHistory?(agent: string, handle: string): Promise<InstanceChat>
  /* Drops the registry row. The record directories stay on disk, which is why
     the verb is forget and not delete. */
  instanceForget?(agent: string, handle: string): Promise<void>
  /* One turn addressed to this instance instead of to the conversation. Optional
     like the rest: a server without direct chat has no lane to send it down. */
  instanceSend?(agent: string, handle: string, text: string): Promise<void>
  /* The live heartbeat: the source calls back every couple of seconds and
     the island decides whether anything on screen needs asking about. */
  watch?(fn: () => void): void
  /* Draws one run's record into a detail stage -- a spawn's AgentCtx or a
     node's DagNodeCtx. Optional for the same reason `context` is: with no
     record to hand out there is nothing to paint. The island never calls this
     directly; it reaches the painter through the shell's agentStagePaint verb,
     whose signature this mirrors, `unknown` included -- the renderer on the
     far side takes the legacy shape, and a narrower type here would be a
     claim about a value that crosses an untyped layer to get there. */
  stagePaint?(box: HTMLElement, ctx: unknown,
              opts?: { key?: string; empty?: string; reset?: boolean }): void
}

export type OpenItem =
  | { kind: 'spawn'; id: string }
  | { kind: 'dag'; run_id: string; node: string; agent?: string | null; label: string }
  | { kind: 'instance'; agent: string; handle: string }
