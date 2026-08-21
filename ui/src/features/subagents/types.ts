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

export interface AgentsSource {
  list(sessionId: string): Promise<AgentRow[]>
  /* Live-only: the fixture replay records no per-run context, so the demo
     detail keeps its empty note exactly as the legacy renderer did. */
  context?(id: string): Promise<AgentCtx>
  node?(runId: string, node: string): Promise<DagNodeCtx>
  /* An empty list and a server that cannot list are different things; the
     empty state reads this to tell them apart. */
  absent?(): boolean
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
