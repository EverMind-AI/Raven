import type { DirectTurn, InstanceRow, SubagentRow } from '../../rpc/generated'

export type { DirectTurn, InstanceRow, SubagentRow }

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
  /* The addressable handle this run committed under, when it had one. On the
     wire since `SubagentCall` carried it and unread here until now, which is
     why a spawn row could only ever open its record. */
  instance?: string | null
  started_at?: string
  ended_at?: string
  tokens?: number | null
}

/* A spawned run's whole record, as subagent.context answers it. The island
   never reads the messages itself -- its source hands them to the transcript
   renderer through stagePaint. */
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
   Declared here rather than imported from the transcript island because the
   source owns the boundary and accepts records from both RPC shapes. */
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
  roster?(): Promise<SubagentRow[]>
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
  /* Start a fresh instance of one sub-agent. Optional like the rest: a server
     without direct chat has none to start. Only an enabled, stateful agent can
     be instantiated -- a stateless one answers each turn from nothing, so a
     handle onto it would name a conversation that does not exist. */
  instanceCreate?(agent: string, sessionKey: string): Promise<InstanceRow>
  /* The live heartbeat: the source calls back every couple of seconds and
     the island decides whether anything on screen needs asking about. */
  watch?(fn: () => void): void
  /* Draws one run's record into a detail stage -- a spawn's AgentCtx or a
     node's DagNodeCtx. Optional for the same reason `context` is: with no
     record to hand out there is nothing to paint. */
  stagePaint?(box: HTMLElement, ctx: unknown,
              opts?: { key?: string; empty?: string; reset?: boolean }): void
}

export type OpenItem =
  | { kind: 'spawn'; id: string }
  | { kind: 'dag'; run_id: string; node: string; agent?: string | null; label: string }
  | { kind: 'instance'; agent: string; handle: string }
