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

/* One entry of the menu an acp agent's handshake advertised: the id the agent
   takes back, what it asked to be shown, and its own bucketing. */
export interface ExtAgentModelChoice {
  value: string
  name: string
  group: string
}

/* One reasoning effort an acp agent advertised in its thought_level option:
   the value `session/set_config_option` takes back, and what to show for it. */
export interface ExtAgentEffortChoice {
  value: string
  name: string
}

/* What re-reading an acp agent's model menu answered: the menu now in force,
   and whether the handshake landed. A failed read keeps the recorded menu in
   place, so `models` is still drawable beside `detail` rather than emptying. */
export interface ExtAgentMenuRefresh {
  ok: boolean
  detail: string
  models: ExtAgentModelChoice[]
  effortChoices: ExtAgentEffortChoice[]
}

/* What `subagents.update` accepts for `model` on a row, by kind -- not
   ownership, which is `own`: 'raven' is the built-in row picking from raven's
   own providers, 'agent' an acp row picking from `model_choices`, 'fixed' an
   openai or cli row with no menu at all. */
export type ExtAgentModelSource = 'raven' | 'agent' | 'fixed'

export interface ExtAgentRow {
  name: string
  preset?: string
  kind: ExtAgentKind | string
  /* The registered machine the row runs on (an openai row: reaches its
     endpoint from), by its id in the machine registry; null for a row that
     runs here, and absent from a server that predates it. Shown as a chip
     after the name: the name is the user's and need not say. */
  machine?: string | null
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
  /* The executable the probe looked for and did not find, on a `missing` row:
     what to install. `npx` for a preset launched through it, which Node.js
     brings and the agent's own installer does not. Absent everywhere else,
     and from a server that predates it. */
  probe_missing?: string
  has_api_key: boolean
  /* The agent answered the handshake and refused to open a session without a
     credential it names. Only an acp row carries it; absent from a server
     that predates it, which reads as "not refused". */
  needs_auth?: boolean
  description: string
  test_running: boolean
  last_test_ok: boolean | null
  last_test_at_ms: number | null
  last_test_detail: string
  /* The fix the server named for the last failed test; absent when it knows
     none. `last_test_detail` stays the English record. */
  last_test_remedy?: Remedy | null
  upgrade_to?: string | null
  /* One of Raven's own, whichever way this install registered it: the
     built-in row, a discovered product, or a config row whose acp handshake
     named raven. Absent from a server that predates it. */
  own?: boolean
  /* The model this row sends, or null for the agent's own default. */
  model?: string | null
  model_choices?: ExtAgentModelChoice[]
  /* Absent from a server that predates the model field, which is a server
     with no model write -- so the sheet draws no pill at all. */
  model_source?: ExtAgentModelSource
  /* The reasoning effort this row sends, or null for the agent's own. */
  effort?: string | null
  /* The efforts the agent advertised in its thought_level option; empty for
     an agent with no such menu, which is also every agent before 1.19.4. */
  effort_choices?: ExtAgentEffortChoice[]
}

/* What the page can ask the server to do.

   Five of these are writes -- the four steps of "connect", plus the switch --
   and each is one of the row's two verbs for one kind of row. `test` and its
   cancel are the odd pair: they write nothing the reader asked for and answer
   a question instead. They are here rather than on a row for that reason, and
   they live in the card. Remove is still gone with the buttons that named it.
   `model` is the row's own model, set or cleared: an `update` too on the
   wire, kept apart here because its refusal is about the pick, not the row.
   `effort` is the same for the agent's thought_level option. */
export type ExtAgentOp =
  | 'build'
  | 'connect'
  | 'effort'
  | 'migrate'
  | 'model'
  | 'test'
  | 'test_cancel'
  | 'toggle'
  | 'update'

export interface ExtAgentActArgs {
  new_name?: string
  description?: string
  api_key?: string
  enabled?: boolean
  model?: string
  /* Whose credential serves `model`, for the built-in row: the server spells
     it into the stored id. An acp row's values are the agent's own. */
  provider?: string
  clear_model?: boolean
  /* The row's reasoning effort, from its agent's thought_level menu. */
  effort?: string
  clear_effort?: boolean
  /* Where a preset is connected to run: a registered machine's id, and for an
     acp or cli row the working directory there. Absent means this computer. */
  machine?: string
  remote_cwd?: string
}

/* One preset as the machine sheet's connect form offers it: what it is, and
   where an entry of it already runs. Listed whether or not this computer has
   it connected -- an entry is one preset on one machine -- which is what the
   roster's own rows cannot say, since they withhold a preset once a row holds
   its name. */
export interface PresetChoice {
  preset: string
  name: string
  kind: 'acp' | 'cli'
  description: string
  connected_here: boolean
  /* Machines (ids) a connected entry of it runs on: the form greys these. */
  on_machines: string[]
  /* Machines where an entry of it exists but is switched off, with that
     entry's name: it still holds the machine for the add (the server refuses
     a second), so the form offers it back under its own name instead. */
  off_on_machines: Array<{ machine: string; name: string }>
}

export interface ExtAgentsSource {
  /* `rescan` also has the server take the login shell's environment again before
     it probes: the one answer to an agent installed since the gateway started,
     whose installer added a PATH line only a new capture reads. Only an explicit
     re-check sends it -- the capture runs the user's login shell. */
  load(probe?: boolean, rescan?: boolean): Promise<ExtAgentRow[]>
  act(op: ExtAgentOp, row: ExtAgentRow, args?: ExtAgentActArgs): Promise<ExtAgentRow[]>
  /* The presets an entry can be made from on a registered machine. */
  presets(): Promise<PresetChoice[]>
  /* Connect `choice` on `machine` under the server's default name,
     `<preset>@<machine>`, in the working directory there (`''` for the
     default); the server starts it there once as proof, and refuses a preset
     the machine already has. The name it landed under, or the refusal --
     `cancelled` when the refusal is that the run was stopped (`cancelRun`),
     which the form says as a cancel rather than as the agent failing. */
  connectOnMachine(
    choice: PresetChoice,
    machine: string,
    remoteCwd: string
  ): Promise<{ name: string } | { refused: string; cancelled?: boolean }>
  /* Stop the run in flight under `name` -- the proof prompt of a connect
     pressed from the machine form, by the name the entry has or lands under;
     the same call that stops a test. True when there was one to stop; false
     when nothing was running, which leaves the connect past the point where
     it can be cancelled rather than cancelled (`awaitRun`). */
  cancelRun(name: string): Promise<boolean>
  /* Re-read an acp row's model menu from its own handshake, spending no
     prompt. `force` reads even when the menu was just read: a reader pressing
     refresh means it moved, while the automatic read on opening a picker does
     not. The menu now recorded, and whether the handshake answered -- a failed
     read keeps the previous menu rather than emptying it. */
  refreshMenus(row: ExtAgentRow, force?: boolean): Promise<ExtAgentMenuRefresh>
}

/* What fixes a refusal, as the server classified it: which kind of fix, and
   the command that makes it on this machine when one is known. The same
   verdict the server's English sentence spells out, as data, so the sheet can
   say it in the reader's language. The first four are about the agent itself;
   `model`, `billing`, `quota` and `network` are its model provider refusing or
   out of reach; `silent`, `upgrade` and `exited` are how it failed to answer;
   `plan` is its account's plan not including it, and `config` its own config
   file it cannot read. */
export interface Remedy {
  kind:
    | 'sign_in'
    | 'setup'
    | 'api_key'
    | 'download'
    | 'model'
    | 'billing'
    | 'quota'
    | 'network'
    | 'silent'
    | 'upgrade'
    | 'exited'
    | 'runtime'
    | 'plan'
    | 'config'
  command: string
  /* What to type once `command` is running, when the fix is a step inside the
     agent rather than the command itself -- Qwen Code's `/auth`. Only ever
     beside a command. */
  then?: string
  /* For `runtime`: the Node.js the agent's package declares it needs (`22`),
     and the one this machine launched it with (`18.20.8`). */
  needs?: string
  found?: string
}

/* -- machines: the registered machines an agent row may run on -------------
   What a machine is and how this computer's link and node on it are doing,
   never the way onto it: the server hands over no address, account or key,
   and the register form's fields travel once, in `add`, and are not read
   back. The same island as the agents because a machine is one more answer
   to "where can my work go". */
export type MachineLink = 'connected' | 'idle' | 'lost' | 'off' | 'error'

export interface MachineRow {
  id: string
  name: string
  transport: 'ssh' | 'local'
  /* The link from this computer: connected (agents running), idle (a link,
     no agent), lost (the link is down, agents wait), off (no link), error. */
  state: MachineLink
  /* One sentence for lost and error; '' otherwise. */
  detail: string
  agents: number
  /* The node's Raven version -- live while a link is up, else as recorded at
     the last install or contact -- and whether it is this Raven's own code,
     which a dispatch there needs. '' when none is known. */
  node_version: string
  node_current: boolean
  /* How this computer logs in to an ssh row: with the key whose path the
     registry carries, or with the password kept here for it. '' for a local
     row. The path and the password themselves never reach the page. */
  auth: 'key' | 'password' | ''
  /* The last node install asked for from this page: in flight, or how it ended. */
  install: { running: boolean; ok: boolean | null; detail: string }
  paths: string[]
  /* What blocks the row, one sentence each, as the registry's doctor says it. */
  problems: string[]
  /* The connected agent entries that run on this machine, by name: the line
     that tells a reader whose machine is up and idle where the next step is.
     Empty on a machine nothing is connected on yet. */
  agents_here: string[]
  /* The entries on this machine that are switched off, by name: shown as
     such, since they still hold the machine for the add. */
  agents_off_here: string[]
}

export interface MachineProblem {
  /* The row it is about, or '' for the registry file itself. */
  id: string
  blocking: boolean
  detail: string
}

/* Why a machine was not registered, as a kind the page explains in the
   reader's language; '' when it was. */
export type RegisterReason = '' | 'unreachable' | 'login' | 'prompt' | 'hostkey' | 'timeout' | 'nossh' | 'platform' | 'duplicate' | 'invalid' | 'registry'

export interface RegisterForm {
  id: string
  name: string
  host: string
  port: number
  user: string
  /* How Raven logs in, now and on every connection after: with the private
     key at `key`, or with `password`, which the server keeps on this computer
     for the owner alone. Both travel once, in `add`, and are not read back. */
  auth: 'key' | 'password'
  key: string
  password: string
  paths: string[]
}

export interface MachinesSource {
  /* `refresh` reaches the machines first -- the one read that opens links --
     all of them, or the one `id` names. */
  list(refresh?: boolean, id?: string): Promise<MachineRow[]>
  add(form: RegisterForm): Promise<{ ok: boolean; detail: string; reason: RegisterReason }>
  /* Starts the install; the list reports it (`install` on the row) until it ends. */
  installNode(id: string, nodeDir?: string): Promise<{ started: boolean; detail: string }>
  doctor(): Promise<MachineProblem[]>
  remove(id: string): Promise<{ removed: boolean; detail: string; agents: string[] }>
  /* The private keys this computer has, as `~/.ssh/<name>` paths, ssh's own
     first: what the register form fills in instead of a placeholder. */
  localKeys(): Promise<string[]>
}
