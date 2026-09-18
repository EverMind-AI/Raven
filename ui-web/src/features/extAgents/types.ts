/* The external-agents page's rows and its DataSource contract. The row is
 * what both sources answer: the fixture in src/rpc/fixtures/subagents.ts and
 * the `subagents.*` mapper in features/extAgents/source.ts.
 *
 * Three facts, deliberately kept apart: `configured` is "Raven knows about
 * it", `enabled` is "Raven may dispatch to it", and `probe_status` is "the
 * machine can actually run it". Collapsing them is how a disabled agent
 * reads as broken, or a missing binary reads as switched off.
 */

export type ExtAgentKind = 'builtin' | 'cli' | 'acp' | 'openai'

export type ExtAgentProbe = 'ready' | 'attention' | 'unknown' | 'missing'

export interface ExtAgentRow {
  name: string
  preset?: string
  kind: ExtAgentKind | string
  configured: boolean
  /* A built-in agent is this process. It has no row to write, which is why
     `configured` is false on one and why the connect paths below leave it out:
     not writing a row is what "use the default" means. */
  builtin?: boolean
  /* Discovered under the `agents/` product tree rather than written into
     config: one of the agent products this install shipped. Like `builtin` it
     leaves `configured` false -- there is nothing to delete, and removing it
     means removing its folder -- so it must be kept out of the "connect one"
     section too, whose only verb it cannot honour. Unlike `builtin` it is a
     real subprocess, so it is probed and it can be unready. Absent from a
     server that predates discovery. */
  vendored?: boolean
  /* Always false from this server: the fork-era venv build is gone, and
     `subagents.build` answers that there is nothing to build. Kept for wire
     compatibility. */
  building?: boolean
  enabled: boolean
  probe_status: ExtAgentProbe | string
  probe_detail: string
  has_api_key: boolean
  /* The agent answered the handshake and refused to open a session without a
     credential it names. Only an acp row carries it: an openai row's key is
     settled by the free `/models` probe, and a cli row has no handshake to be
     refused in. Absent from a server that predates it, which reads as "not
     refused" -- the same default as a row nobody has contacted. */
  needs_auth?: boolean
  description: string
  test_running: boolean
  last_test_ok: boolean | null
  last_test_at_ms: number | null
  last_test_detail: string
  upgrade_to?: string | null
}

/* What the page can ask the server to do.

   Five of these are writes -- the four steps of "connect", plus the switch --
   and each is one of the row's two verbs for one kind of row. `test` and its
   cancel are the odd pair: they write nothing the reader asked for and answer
   a question instead. They are here rather than on a row for that reason, and
   they live in the card. Remove is still gone with the buttons that named it. */
export type ExtAgentOp = 'build' | 'connect' | 'migrate' | 'test' | 'test_cancel' | 'toggle' | 'update'

export interface ExtAgentActArgs {
  new_name?: string
  description?: string
  api_key?: string
  enabled?: boolean
}

export interface ExtAgentsSource {
  load(probe?: boolean): Promise<ExtAgentRow[]>
  act(op: ExtAgentOp, row: ExtAgentRow, args?: ExtAgentActArgs): Promise<ExtAgentRow[]>
}
