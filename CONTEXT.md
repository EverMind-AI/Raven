# Raven Runtime

The Python agent runtime: receives messages from chat channels, runs the agent loop
against LLM providers, and hosts the feature engines (context, memory, proactive, eval)
plus the TokenWise efficiency layer.

## Language

### Agent Core

**Session**:
The ordered, append-only record of turns for one conversation, identified by a
session key (`channel:chat_id`). Identity lives in the `chat_id` slot: a TUI/CLI
session mints an opaque, sortable `chat_id` (`%Y%m%d_%H%M%S_xxxxxx`), so one surface
can hold many sessions while the `session_key={channel}:{chat_id}` invariant is
unchanged. Channel is a dimension (key prefix + store subdirectory + metadata
field), not part of the user-facing identity.

A session's `metadata["model"]`, when present, is the model its turns run on; it
outranks the router and falls back to `agents.defaults.model` when absent.

**Session id** (user-facing term only):
The bare `chat_id` value shown to and accepted from users (the channel prefix is
stripped for display, re-prepended to form the session key). Presentation term; in
code the value lives in the `chat_id` field and the composite is the `session_key`.

**Model binding**:
A model id together with the provider whose credential serves it, as one value
(`raven/providers/binding.py`). The pairing is the point: a model id alone does
not say which key reaches it, and updating one half is how one vendor's key ends
up on another vendor's endpoint. A turn resolves its binding once at `run_turn`
entry and holds it in a context var for the whole turn tree, so everything under
that turn -- the loop, the context engine's LLM-backed segments, the skill gate
and rewriter, the consolidator, and any task the turn detaches -- reads the same
pair. _Avoid_: "the current model" / "the active provider" for this; both name
one half.

**Session binding**:
The model binding one conversation runs on. Sessions that never switched have no
entry and resolve to the **default binding**; a switch writes only that
session's entry, so it moves no other conversation and does not change what a
new one starts on. Stored on the session record so it survives a restart.

**Default binding**:
What a session with no binding of its own runs on: `agents.defaults` from config,
verbatim. Changed by a `scope="default"` switch, which leaves sessions that
already chose their own model where they are.

**Provider pool**:
The one place a model id is resolved to the credential that serves it
(`raven/providers/pool.py`), caching a provider per (vendor, model) and dropping
the cache when the credentials behind it change. Also what turns a **subsystem
pin** into a pair. Constructing a `ModelBinding` from an already-resolved pair
happens in several places; deciding *which* provider a model id pairs with
happens only here.

**Subsystem pin**:
A model configured for one subsystem rather than for the conversation, as a
model and the provider serving it (`context.curator_model` +
`curator_provider`, `skill_forge.llm_gate_model` + `llm_gate_provider`). Both
halves because an id alone is ambiguous the moment a gateway is configured:
`openrouter` + `anthropic/claude-haiku-4-5` and `anthropic` +
`claude-haiku-4-5` are both valid and name different credentials. With the
provider set nothing is derived, and a named vendor without usable credentials
is reported and dropped -- the subsystem then follows the conversation's model,
because a bare pinned id sent on the conversation's key is exactly the
mis-pairing above. With the provider unset the pin still binds: a configured
gateway takes it (it serves whatever id it is handed, under its own
credential), and only without one is the vendor guessed from the id. Unset out
of the box -- no subsystem ships a vendor default.

**Turn**:
One complete agent reaction: from an inbound message entering the agent loop to the
agent's final response, including every LLM call and tool execution in between.
Sentinel nudges and cron firings each start a turn of their own; a confirm
round-trip pauses a turn, it does not end it.
_Avoid_: calling a single LLM round-trip a turn

**Iteration**:
One LLM call plus the tool executions that follow it, inside a turn.

**Agent Loop** (`agent/loop/`):
The turn orchestration engine: receives a `TurnRequest` from the Spine, assembles context,
drives the LLM + tool-execution iterations, consolidates memory, and emits `Deliverable`
events via the Spine `emit` callback. Exposed to the Spine via `AgentTurnRunner`.
_Avoid_: calling a single LLM call the "agent loop" — the loop spans all Iterations of one turn.

**Turn Runner**:
The behavioural `Protocol` seam between Spine and an agent implementation:
`async run(req, emit, drain) → TurnOutcome`. Spine never imports the agent side; the agent
supplies `AgentTurnRunner` (wraps `AgentLoop`). Gateway and TUI variants also exist.
_Avoid_: conflating with Agent Loop — Turn Runner is the Protocol; Agent Loop is one implementation.

**Agent Hook** (`contracts/loop_hooks.py`; implementations in `agent/hook/`):
The turn-loop extension point: an `AgentHook` ABC with six async phases
(`before_user_inbound`, `before_iteration`, `before_execute_tools`, `after_iteration`,
`terminal_answerless`, `after_send`), all fired by the loop. A decision may pass through,
short-circuit, modify outbound content, or roll the iteration back and re-sample (with
injected messages and generation overrides for that one call); `before_iteration` may also
withhold tools for the iteration, the iteration phases may leave the model a harness note on
the last message (`append_note`), and `before_user_inbound` may rewrite the inbound text
(`modified_content`, chained through `inbound_content`). Multiple hooks chain via `CompositeHook`; the EvalEngine
wires three concrete implementations, and a product steers the loop with its own.
_Avoid_: "callback" or "middleware" — neither captures the phase-specific, chain-aware semantics.

**Session Mode** (`acp/modes.py`; declared under `acp.modes` in config):
A named per-session operating profile a client switches over ACP `session/set_mode`; every
session response carries the `SessionModeState`. A mode's own three knobs are the iteration
cap the loop enforces, the reasoning effort its model calls run at (`reasoningEffort`,
passed as an explicit argument on every call of the turn; unset inherits the connection's
own), and an `overlay` the loop hands the hook chain as `ctx.metadata["mode_overlay"]`
without interpreting -- a product's own hooks read their own knobs from it. The shipped
built-in catalogue (see **Session Tier**) leaves all three knobs at their defaults on all
three of its modes; a deployment that declares its own catalogue is what actually moves
them -- `raven-code` declares `medium`/`high`/`max` on the host's own ladder, each moving
the effort alone. Session state, not transcript state; a switch lands on the session's
next turn.
_Avoid_: re-spelling a mode as a `session/set_config_option` entry -- modes are first-class in
the stable schema.

**Session Tier** (`medium`/`high`/`max`; `TIER_LADDER` in `config/schema.py`):
What the shipped built-in Session Mode catalogue moves in place of an iteration cap or an
overlay: a session's current mode id (`session_policy(key).mode`, falling through to the
catalogue's default) is offered to every sub-agent that session dispatches, as the effort
level to run it at. `clamp_tier` (`agent/subagent/mode_tiers.py`) is the clamp that lands
it on one sub-agent: the nearest rung at or below on that agent's own probed menu, or
`None` -- leave the agent on its own default -- in three cases. A tier outside the ladder
(another vocabulary, "nearest" undefined); a menu sharing no rung with it at all; and a menu
carrying a rung the ladder cannot rank at the point where raising the effort is the only
move left, since the cheapest rung the ladder can *see* may not be the cheapest the agent
has. An agent that converges on the ladder plus an id of its own still clamps on the real
overlap, and an exact hit is honoured in any vocabulary that spells the rung the same way.
Exactly one thing outranks it, and a person sets it: a standing override on a named instance
(`subagents.instance.set_mode`). There is no per-dispatch mode -- the model composing a
`spawn` cannot know what the operator chose, so `resolve_mode` takes no such argument and
the spawn schema offers none.
The three built-in descriptions are `raven.i18n` message ids resolved in
`build_mode_catalogue`, not where they are declared: a pydantic `default_factory` runs
before an entrance calls `set_language`, so declaring them translated would bake in
English. A catalogue a deployment declared is passed through untouched --
`AcpConfig.uses_builtin_modes` is the line between the rows raven owns and the rows it
merely carries.
_Avoid_: assuming a deployment's own Session Mode catalogue carries a tier too --
`clamp_tier` only ever recognizes `TIER_LADDER`'s three names, so a renamed catalogue is
exactly what it declines to guess at.
_Avoid_: putting the scope of the control on the rungs. Each row says only what
distinguishes it; that raven's own effort is unchanged is stated once by whichever surface
draws the control.

**Subagent** (`agent/subagent/`):
A background agent task spawned by `SubagentManager`. Runs with its own tool set; its result
re-enters the session as a `SUBAGENT`-origin `TurnRequest` via Spine submit. Bounded by
`max_concurrent` (default 8) and a per-session hourly rate limit, both shared with the
nodes of a DAG run — every sub-agent dispatch draws on the one allowance.
_Avoid_: conflating with a Turn — a Subagent lives outside the main turn and re-enters via Spine.

**Sub-agent role** (`RAVEN_SUBAGENT` → `agent/subagent/role.py`):
What a raven process knows about why it was started. The host sets it on every child it
launches, beside the `RAVEN_HOME` it already sends, and a process that reads it as set
registers none of the tools that hand work to a further agent (`spawn`, `run_subagent_dag`,
the three graph controls) and builds no playbook funnel, so `load_playbook` and
`create_playbook` unregister themselves. The `## Delegation` block and the DAG orchestration
guide follow the tools out on their own, both being conditional on a live dispatch path.
`WITHHELD_FROM_SUBAGENT` is the specification; the gates enforce it by not building, and the
tests hold the two in agreement. A config `env` entry outranks the host's, which is how one
agent is handed back a full registry.
It is a property of the *process*, and one pooled acp connection serves every call to an
agent, so the same process answers a dispatched DAG node and a person's direct chat and
cannot tell them apart. Withholding is correct on both lanes; anything written into the
`## Sub-agent` prompt section is bounded by it, and may not claim who is reading or that no
turn follows. Nor may it deny later execution outright: an acp process is wired with a cron
service, so `cron` is registered and a fired job does run later on the session that armed it.
_Avoid_: reading it as the same thing as `IN_SUBAGENT_RUN` — that `ContextVar` says a
*task* is a sub-agent run inside this process and cannot cross a process boundary; this says
the whole process is one, and is the only form that survives an exec.

**Agent table** (`subagents.agents[]` in config → `agent/subagent/registry.py`):
The one list of agents raven can dispatch to, materialized once per process as an
`AgentRegistry` that `spawn`, `run_subagent_dag` and the playbook generator all read.
A row is a name plus a `kind` (`builtin` / `cli` / `acp` / `openai`) plus that kind's
connection fields; the `kind` set is closed -- a new kind is a new backend module plus a
branch in `agent/subagent/backends/__init__.py:build_third_party_backend`, and no plugin
door for subagent kinds exists (recorded, not promised); `AgentCaps` and `Injectable` are *derived* from it, and are what a
consumer branches on so that nothing has to switch on the transport. A declaration a row
carries (`owns`, `owns_watched_work`) is read once here, where the schema resolves every
spelling it has ever accepted, and travels on the row and its `AgentMeta`; a consumer that
re-opened config or a folder manifest for itself would be a second reader free to disagree
with the table being dispatched against.
Three sources compose it, weakest first: **Product agent** rows discovered on the
filesystem, then `builtin` package seeds, then config. `builtin` rows are package seeds
(`agent/subagent/builtin_agents.py`): they exist whether or not config mentions them, and a
config row of the same name is a field-level override -- of every field but `enabled`,
which the merge discards, so a seed row cannot be taken off the roster at all. An unnamed
`spawn` and a DAG node with no `subagent` both dispatch to the generic seed, `Raven`. A name
a seed used to answer to resolves to it (`LEGACY_AGENT_ALIASES`; `raven` -> `Raven`), so
stored instance rows, direct-chat records and dag nodes written before a rename still find
their agent -- exact match first, and the roster never offers an alias back. Read under the
older key `thirdParty` too; the write path emits `agents`.
_Avoid_: "third-party registry" — the table holds raven's own agents as well, which is the
point of it: `spawn` and a DAG node pick from one roster, so an agent reachable from one
entry point and not the other is no longer a state that exists.

**Product agent** (`agent/subagent/vendored_agents.py`):
An agent row discovered under the `agents/` product tree rather than written anywhere —
one of the shipped products, each a launcher (`run.py`) over the installed raven plus a
`subagent.json` manifest. Materialized as a row on every table build, so a folder that is
deleted stops being an agent and a manifest that changes is picked up without a stored
copy to contradict it. The tree resolves through `agents_root()` — the raven home
(`~/.raven/agents`, installed out of the wheel so a product's `.env` survives upgrades),
then beside the package in a checkout, then the wheel's own `raven/agents`. Readiness
(the launcher files its command names on disk, and its manifest-declared engine wheel
importable where raven runs) decides `enabled`, not whether the row exists: an unready
folder is listed and disabled with the reason on the row, because a name the dispatching
model can pick and then fail on is worse than no name, and hiding it would also hide
"present, not set up" from the operations view. A missing credential is deliberately not
a readiness reason: the launcher inherits the host's provider block, so readiness asks
about the folder, not about a token. Switching one on is the layer where a credential
does count — `subagents.toggle` sends one real prompt through the row's own backend
before it writes the flag, and refuses the enable in the agent's own words when nothing
answers (`force: true` is the operator's override). The two layers therefore ask
different questions: readiness decides how the row is listed and spends nothing, the
switch spends one call on that agent's quota before it writes a yes. Neither validates
retroactively — a folder that ships enabled, and a row already switched on, stay on the
roster unpinged — because the gate is on the act that turns an agent on, and not on
membership. That act is the switch, or an add that writes a preset in already enabled:
`subagents.add` proves a pinged kind the same way and stores nothing when it does not
answer, so a preset cannot arrive on the roster unproved either.
Not deletable through config — removing one means removing its folder, or setting
`"enabled": false` in its own `subagent.json`. On the RPC wire the row source is still
spelled `vendored`; renaming that is a schema change.
_Avoid_: "vendored agent" — the retired fork-tree (`subagents/`) meaning, whose rows
carried venv and credential readiness; "third-party agent" — these are raven's own
products, and nobody registered them; "builtin" — that is the in-process row, which has
no subprocess and no launcher.

**Machine** (`raven/ops/connections.py`):
A compute host the owner registered with `raven ops connection add`, held in
`connections.json` beside the config or wherever `RAVEN_CONNECTIONS` points.
An on-call-style agent runs its work outside the dispatching Raven
process — on a GPU box, a lab workstation, another machine entirely — and the
host's whole part in that is keeping the registry and handing it over: a
launcher points the agent's `RAVEN_CONNECTIONS` at the owner's store rather
than copying rows into the agent's own home. Which machine a job lands on,
and whether it can run at all, is settled inside the agent that runs it.
Nothing on the dispatch path reads the registry, so no graph and no spawn is
ever refused over the state of it.
_Avoid_: "host" / "server" / "node" (too broad, no link to the
`ops connection` registry that supplies the rows); "GPU box" (only some are
GPU hosts, and the term covers any registered compute destination);
"connection" (a `connection` in Raven channels is a chat-room binding, and
here the rows name compute destinations with that binding as an
implementation detail).

**Roster** (`format_agent_listing`):
The agent table rendered as the text spliced into `spawn`'s and `run_subagent_dag`'s tool
descriptions — `name [stateful, local-files, live-progress] (description)`. The model's only
account of which agents exist, so an agent absent from it cannot be chosen; the `enum` on
the `subagent` parameter constrains the same set. All three capabilities render positive *or*
negative, because "no tag" and "the roster does not say" are indistinguishable otherwise.
Also spliced into the skill gate's prompt under push discovery, where it is the account of
what can be delegated and so decides which candidate skills are dropped as already covered;
that copy omits the generic `builtin` row, which claims no capability bias and would read as
covering everything. Pull discovery builds no skills segment, so it has no gate and drops
nothing on these grounds.
_Avoid_: treating it as the table — the roster is the enabled subset, formatted for a prompt.

**Ownership** (`owns`, on a sub-agent's manifest and on any config entry, built-in included):
One clause naming the kind of work an agent owns, completing "`<name>` ...". Every agent
declaring one gets a line in the identity prompt's `## Delegation` section telling the model
not to do that work itself; an install where none declares one renders no section and reads
byte-identically to one without the field. Distinct from `description`, which says what the
agent *can* do and is read when choosing between agents — this says what the main agent must
*stop* doing, and is read before it reaches for a tool. `None` means undeclared and is filled
in for it: from the folder's manifest for a vendored agent, from the package seed for a
built-in override, so a config written before the field existed still gets one. `""` is the
user declaring the agent owns nothing and is never refilled.

Two things never carry ownership. The generic row (`raven`) claims none whatever a config
says, because it carries no capability bias and a line about it would prohibit the agent
reading it from doing its own work. And the section is withheld entirely on a turn holding
no dispatch tool — `spawn` and `run_subagent_dag` can both be withheld by
`tools.disabledTools`, and a prohibition outliving every means of handing the work over
leaves a request with no compliant action at all. Only the paths the turn does hold are named.
_Avoid_: putting it in `description` — that copy is spliced into the tool descriptions, where
the model reads it only once it is already choosing an agent.

**Subagent working directory** (`workspace=` on every backend's `run`):
Where a sub-agent's commands and file tools act: the *session's* working directory, the same
one the dispatching turn's own tools get. Every dispatch supplies it — `spawn` captures
`workdir.current()` at spawn time (the sub-agent outlives the turn whose binding it would
read), a DAG node takes the same, and a Direct Chat resolves `session_workdir` because its
branch returns before `workdir.bind` wraps the turn body. Distinct from **Agent home**
(`SubagentManager.workspace`, `~/.raven/workspace`), which holds raven's memory and skills
and is only the fallback for a dispatch that supplied nothing.
_Avoid_: treating the fallback as the default — a sub-agent working in Agent home inspects
raven's own memory instead of the user's checkout, and says nothing about having done so.

**Task summary** (`task_summary`, on `spawn`, `run_subagent_dag` and `PlaybookSpec`):
the short title naming what is being dispatched, written before the prompt it titles --
chat-title length, under ten words rather than a sentence.
Only `spawn`'s reaches the user, naming the dispatch in instance handles, sub-agent rows,
spawn records and announcements, and never reaching the sub-agent's own input. The other
two reach the user through the Instance Title and the Run Title below:
`run_subagent_dag`'s becomes `SubAgentDagSpec.task_summary`, persisted into `graph.json`,
and a playbook's flows into that same field once the playbook dispatches. On a playbook it
still sits beside `description`,
which answers a different question: `description` is matched against to decide whether to
run the playbook at all, `task_summary` says what running it dispatches.
_Avoid_: `label` for this on the spawn path — the tool parameter is gone. The wire field
`SubagentCall.label` and the span attribute `subagent.label` (`raven/observability/semconv.py`)
keep the name and are filled from the summary.

**Node summary** (`node_summary`, on `DagNodeSpec`):
the same obligation for one node of a graph, and the node row's subject. Blank survives
parsing so a playbook can leave it for the model to fill, and `validate_and_order` refuses
it before any node runs. It replaces the first-line-of-the-template guess a row used to
make.

**Instance Title** / **Run Title** (`InstanceRow.title` / `InstanceRow.runTitle`):
what one instance was asked, and what the graph it belongs to was asked. Computed by
`subagents.instances` rather than stored, the way `resumable` is and for the same reason:
four front ends draw this list, and a join each of them wrote separately is four chances to
join differently. A node's title is read from its run's `graph.json` (one file per run, not
per row); a spawn's from the Instance Log header, which is already addressed by
`(agent, handle)`. Neither is on the Instance Registry: every writer there rebuilds a record
wholesale, so a copy would be one dropped key away from vanishing. A run title is *absent*
rather than empty when the instance came from no graph, so its presence is what a reader
tests to decide whether to draw a source at all; a missing instance title falls back to the
handle.

Three sources, in precedence: a spawn's `task_summary`, a graph node's `node_summary`, and --
for an instance nobody dispatched, one the reader started themselves -- the first line of the
message that opened it, by `derive_title`, the same rule that names an untitled session. That
third one is taken *once*, when the log is opened, so the opening message names the instance
and later ones do not rename it; the header is written on `open` rather than on the first
completed turn, or a panel would be headed by an id until something finished.
_Avoid_: reading either as the node id or the handle - those are addresses. A playbook
namespaces every node id with its own name and a run tag, which is exactly why they read
badly as titles.

**Tool** (`agent/tools/`):
An agent capability behind a uniform `Tool` ABC (name, parameter schema, async
`execute`). Built-ins: file read/write/edit/list, grep/find, exec, web search/fetch,
message, ask_user, spawn (Subagent), MCP, media generation, skill read/use, and the
plugin market (`plugin`).
_Avoid_: "function" — a Tool is the agent-facing capability, not a Python function.

**Web Vendor** (`config/schema.py`, `WebProvidersConfig`):
Who issues a web credential, as opposed to which tool spends it. Keys live at
`tools.web.providers.<vendor>.apiKey` because one AnySearch, Tavily, Exa or Firecrawl
account serves both `web_search` and `web_fetch`; a key held per tool would have to be
pasted twice and could drift into two values for one credential. The pre-vendor leaves
(`tools.web.search.apiKey`, `tools.web.jinaApiKey`) are still read, after the vendor
slot, and only for Serper and Jina. `~/.raven/env` mirrors every vendor key out under
its bare env var.
_Avoid_: "provider" unqualified — that is the LLM Provider in this vocabulary; and
"the web key" — there is one per vendor.

**Web Search Provider** / **Web Fetch Provider** (`agent/tools/web.py`, `SEARCH_PROVIDERS` / `FETCH_PROVIDERS`):
Which interchangeable backend each web tool calls: `tools.web.search.provider` selects
`serper` (default), `anysearch`, `serpapi`, `tavily`, `exa`, `brave` or `firecrawl`, and
`tools.web.fetch.provider` selects `jina` (default), `anysearch`, `tavily`, `exa` or
`firecrawl`. Every endpoint is a literal in the tool, so a selection names a vendor and
never a URL. `web_search` is registered whatever the config holds and *withheld* from the
offered set while the selected vendor's key does not resolve, so a key pasted mid-run
surfaces it without a restart -- read live from the vendor slot and, for Serper, from the
pre-vendor leaf, in the order `WebToolsConfig.vendor_key` resolves them, and an empty slot
is a revocation rather than a miss; `web_fetch` is always offered, falling back to Jina,
which reads pages without a key. The two sub-agent launchers inherit the host's selection
differently: the in-process backend (`agent/subagent/backends/raven_loop.py`) takes it and
then declines to *register* `web_search` when no key resolves -- the older gate, which the
vendored-invariant guard still requires there -- while the vendored checkout's launcher
(`subagents/raven-research/run.py`) declines to copy a selection it cannot key and keeps
its own default, because its gate exits rather than degrading. Most vendors take the key
in a header; SerpApi takes it as a query parameter, so for that one the key travels inside
every request URL and two surfaces have to keep it out -- the error path renders vendor plus
status rather than the exception text, and the persisted log sink redacts a URL-borne
credential (`cli/_log_file.py`), since httpx logs each request at INFO on the success path.
_Avoid_: a per-vendor tool name — the model is always offered `web_search` and
`web_fetch`, whichever vendor answers.

**Tool Registry** (`agent/tools/registry.py`):
The name→`Tool` table the Agent Loop dispatches into: resolves a tool by name and runs
its `execute` under a timeout, returning the string result or a structured error.
Three doors feed it, every one through `register` and its `admit_tool` check: the loop's
own wiring registers the builtin tools (`agent/loop/wiring.py`), the assembly root registers
plugin tools built by `PluginRegistry.build_tool` (`core/plugin_stack.py`), and `mcp_glue`
registers MCP tools per connected server.
_Avoid_: a fourth door -- a tool reaching the table any other way skips admission.

**Deep Research** (`agent/tools/deep_research.py`):
Opt-in tool delegating an open-ended research question to the MiroThinker API; returns a
finished, cited answer. Streamed inline on CLI/TUI, async on channels (background run +
verbatim `deliver_text` push). Configured via `raven deep-research`.
_Avoid_: "Subagent" — it is a single long-running tool, not a spawned agent.

**Checkpoint** (`agent/loop/checkpoint.py`):
A once-per-turn commit of the session workspace into a shadow git repo (separate from the
user's `.git`), so an interrupted or failed turn can be rolled back. One `CheckpointService`
per working directory, cached by `AgentLoop._turn_checkpoint()` and keyed on the directory
the running turn is bound to.
_Avoid_: "shadow git" as the term — Checkpoint is the per-turn snapshot it produces.

**Empty-Response Recovery** (`agent/loop/recovery.py`):
The opt-in policy for when the model returns no text: re-feed its reasoning (PREFILL),
inject a nudge after a tool call (NUDGE), or plain RETRY — each bounded by
`RecoveryLimits`; otherwise the turn COMPLETEs. PREFILL is asked of the provider first
(`LLMProvider.supports_assistant_prefill`): the Anthropic family rejects a trailing
assistant message while thinking is on, and through a gateway that rejection can arrive as
a stream that never yields a byte, so a provider that answers no never gets PREFILL -- the
same thinking-only turn takes NUDGE after a tool, else RETRY, and no request handed to it
ends on an assistant message.
_Avoid_: calling the whole mechanism a "nudge" — nudge is one of its modes.

**Synthesis**:
The tools-disabled final LLM call the Agent Loop makes when a turn hits `max_iterations`
(default 40): it summarizes progress and returns partial results, and the turn ends with
status `interrupted`.
_Avoid_: "timeout" — Synthesis is iteration-bounded, not time-bounded.

**Personalizer** (`agent/personalizer/`):
The four-step preference flow wrapped around a turn: classify whether a preference
question is needed, ask it, run the Agent Loop, then post-learn signals from the
finished turn.

**Context Builder** (`agent/context/`):
The bootstrap/identity renderer (`ContextBuilder`) that loads Bootstrap Files and the
runtime-context block, feeding the Context Engine's segments.
_Avoid_: conflating with `ContextAssembler` — Context Builder renders identity pieces;
the Context Engine assembles the whole window.

**Spine** (`spine/`):
The single backbone every turn flows through: one entry
(`Scheduler.submit(TurnRequest) → TurnHandle.result()`) and one exit (`emit(Deliverable)`).
Per-conversation **Lanes** are the unit of both ordering and cancellation. Deliberately
not a broadcast bus.
_Avoid_: "the bus" — there is no Bus; "queue" for Lane — Lane is a serial+cancel domain.

**Lane**:
The per-conversation serial execution domain inside the Scheduler: runs one turn at a time
and is the unit of cancellation. A stalled Lane never blocks other Lanes. A conversation can
be a *sub*-conversation: a Direct Chat runs on `session#agent/handle`
(`raven.spine.turn.direct_lane`), which is what lets several instances answer at once while
the main agent keeps its own serial lane.
_Avoid_: conflating Lane with OriginPools — different dimensions (ordering vs. concurrency).

**TurnRequest**:
The single input to Spine: carries `origin`, `source`, `text`, `media`, and `busy` policy.
Replaces the old `InboundMessage`.

**Deliverable** (= `RunnerEvent`):
The union of all content-type events a runner can emit: `Text | MediaOut | StreamDelta |
Reasoning | Notice | ToolEvent`. Routed to delivery outlets by the `DeliveryHub`.
Replaces the old `OutboundMessage`.
_Avoid_: conflating Deliverable with lifecycle events (`TurnStarted`/`TurnFailed`/`TurnEnded`) —
those are emitted by the Spine worker, not a runner.

**OriginPools**:
Per-origin concurrency gates: a `USER` pool, a `system` pool for proactive origins
(`SENTINEL`, `CRON`, `HEARTBEAT`, `SUBAGENT`), and a `direct` pool for Direct Chats, sized
independently with no borrowing. A user turn never waits on a proactive task's LLM slot, and
never waits behind several sub-agents answering. The direct pool is chosen per *request*
rather than per origin - a Direct Chat is a `USER` turn, and an origin of its own would need
a deliberate home in every origin switch in the codebase.

### Proactivity

**Proactive Engine**:
The subsystem that decides when the agent acts unprompted. Contains exactly two
trigger paths: Sentinel (event-driven) and Scheduler (time-driven).

**Sentinel**:
The event-driven attention pipeline inside the Proactive Engine:
attention producers → predictor → trigger policy → executor → feedback.
_Avoid_: using "Sentinel" as the name of the whole proactivity subsystem (stale README usage)

**Scheduler**:
The time-driven trigger path inside the Proactive Engine: cron jobs and heartbeat.
_Avoid_: conflating with Sentinel

**Fire-at-origin**:
The cron ownership rule: a job is claimed and delivered only by the runner that
owns its creation-time channel binding (`payload.channel/to`) — the gateway for
enabled IM channels, an open TUI session for `tui`.
A job whose surface is closed waits (recurring) or lapses (one-shot `at`,
dropped at that runner's next startup); there is no trigger-time re-routing.
_Avoid_: reintroducing fire-time channel selection (the retired
`cron.forward_channels`) — bind the target at creation instead. The `cli`
channel value is retired with the REPL; stored `cli`-bound jobs migrate to
`tui` at load time.

**Fixed-delay interval**:
The scheduling contract for `--every` jobs: the next run is computed from the
moment the previous fire **completed**, not from the moment it was due. A job
that takes 15s to run therefore repeats every `interval + 15s`, and its clock
drifts by design — the property being bought is that a slow run can never
overlap itself or leave a backlog to catch up on.
_Avoid_: calling this fixed-rate, or reading `--every 2m` as a promise to fire
on the two-minute mark; calendar-anchored schedules are what `--cron` is for.

**Predictor**:
The Sentinel pipeline stage that turns signals into predicted user needs (the
proactive side of prediction).
_Avoid_: conflating with the Memory Engine's Foresight — Predictor is the live stage,
Foresight is the stored memory artifact.

### Channels & Front-ends

**Channel**:
A platform adapter (`channels/base.py:ChannelBase` -- inherited for the plumbing,
and satisfying the `Channel` paper: telegram, matrix, discord, ...) that connects
an external chat platform to the Runtime; managed by the ChannelManager in
gateway mode.
_Avoid_: calling the TUI a channel — `channel="tui"` on a message is a routing tag, not a Channel

**TUI**:
The terminal front-end (`ui-tui/`) and the only interactive local front-end; talks to
the Runtime solely via the RPC protocol. Not a Channel.

**CLI**:
The one-shot command-line entry point (`raven <command>`) for operations and
configuration. Not a conversation front-end.
_Avoid_: using "CLI" for the interactive REPL (retiring)

**Routing Profile** (`routing/profiles.py`):
A named quality-versus-cost weighting the model selector scores candidates with:
`RoutingProfile(quality_weight, cost_weight)`, three shipped names in `ROUTING_PROFILES`
(`best` 0.99/0.01, `balanced` 0.50/0.50, `eco` 0.20/0.80); `routing/selector.py` combines
a model's quality score and its cost score by these weights.
_Avoid_: confusing with Routing Tag — the tag names a turn's recipient, the profile weighs models.

**Routing Tag**:
The `channel` field on a `TurnRequest`; names the recipient — a Channel, or the TUI.

**Deliverable**:
An output file the agent hands to the user through the `deliver_files` tool, addressed by an
opaque token in the persisted registry (`deliverables/deliverables.json`) rather than by path.
Web-channel only: the download UI exists only there, and the tool is not registered on any
other surface.
_Avoid_: calling any file the agent wrote a Deliverable - only a `deliver_files` call makes one.

**Delivery Manifest**:
The structured list of Deliverables a single `deliver_files` call produced (name, size, media
type, token), carried on `ToolEvent.metadata` so it reaches the web UI and persists in message
history. Never carries file bytes.

### Token Efficiency

**TokenWise**:
The token-efficiency shelf (L3): a set of independently toggled TokenStrategies, not a
single module.

**TokenStrategy**:
One independently enable-able efficiency measure, implemented as a `TokenStrategy` ABC
with `before_llm_call` (may rewrite messages / tools / model) and `after_llm_call`
(observes usage) hooks; e.g. usage tracking, cache optimization, smart routing.
_Avoid_: bare "Strategy"

**StrategyRegistry**:
The ordered chain that wraps every Provider call, invoking each registered
TokenStrategy's `before_llm_call` / `after_llm_call` hooks in registration order.
`before` errors propagate (a bad request fails fast); `after` errors are logged and
swallowed so telemetry never crashes the turn.

**UsageTracker**:
The shipped TokenStrategy (`"usage_tracker"`) that records each call's UsageSnapshot and
rolls token counts and USD cost up into per-session, per-day, and lifetime aggregates.

**CacheOptimizer**:
The shipped TokenStrategy (`"cache_optimizer"`) that places Anthropic's ≤4 ephemeral
`cache_control` breakpoints adaptively (tools tail + system tail + a rolling message-tail
window). A Hermes-faithful `SystemAndTailCacheStrategy` ships alongside as an A/B reference.

**UsageSnapshot**:
The token/cost accounting unit for a single LLM or built-in image-generation API call: input / output / cache-read /
cache-write / reasoning tokens plus provider-reported USD cost. Missing cache
counters and cost remain unknown, distinct from zero. The tracker persists version-2
records and counts missing values alongside its aggregates; old estimated costs do not
contribute to reported-cost totals. Chat, Responses, and Anthropic adapters interpret
API-returned numeric `usage.cost` as USD regardless of provider or endpoint; compatible
gateways must use that unit. Other monetary fields and SDK estimates are not inferred.
The built-in image tool records each returned generation/edit response before saving
image files, through the runtime UsageTracker. Unreported image input/output tokens
remain null in telemetry and are counted as missing in aggregates.
_Avoid_: the turn-end wire payload is `TurnUsage` (rpc/models.py), not UsageSnapshot.

**Provider**:
An LLM vendor adapter (`providers/`: Anthropic, OpenAI, Gemini, …), shared by the
agent loop and the Curator.
_Avoid_: conflating provider (vendor) with model (a model name a provider serves)

A Provider is described along four independent axes -- identity, connection, routing,
and what its models can do -- each with its own home. Mixing them in one record is what
left per-model facts nowhere to live and per-provider facts stated in several places at
once. The terms below name the pieces those axes are built from; they are properties of
a model or of a connection, not four synonyms for Provider.

**Model Ref**:
The canonical way a model is written down: `provider/model`, naming whoever serves it.
Usually that is the section it was configured under; where a Provider declares
`skip_prefixes` it may instead be the gateway already named in the id
(`openrouter/z-ai/glm-4.6` stored under `zai` keeps OpenRouter's name, because
OpenRouter is what serves it). Produced by `providers/wire.py::stored_model_id`, which
every surface that persists a choice goes through.
_Avoid_: "model id" for the stored form when the sent form is also in play — say Model
Ref or Wire Model.

**Merge Key**:
The identity of a Model Ref for comparison and de-duplication — the provider and the
vendor's own id, spelling-folded. Two refs naming one model share a Merge Key whatever
spelling either was written in.

**Wire Model**:
The form a Model Ref takes on the request: a LiteLLM route string, an Azure deployment
name, or a Codex slug. Derived, never stored, and derived in one place
(`providers/wire.py::wire_model`).
_Avoid_: treating the stored and sent forms as one string — they differ per provider.

**Auth Method**:
One way of connecting to a Provider: what credential material it needs (as an AND of
OR-groups), how that material is obtained, where it is kept, and how it is verified.
A Provider may declare several and is usable when any one is satisfied.
`providers/auth.py::credential_status` answers "is this Provider usable", and is the
only place that may: seven surfaces once decided it independently and disagreed with
each other on the two configurations that made the rewrite necessary.
_Avoid_: "credential kind" for the whole shape -- that names only the material.

**Auth Shape**:
Which of four ways setting up a Provider goes: a sign-in, an address, a key with an
address, or a key alone (`providers/registry.py::auth_shape`). What the onboarding
wizard and the model picker branch on, and coarser than an **Auth Method**: a shape
says what the user is asked for, a method says what satisfies the connection and
whether it is satisfied.
_Avoid_: deriving it a second time from `is_oauth` / `is_local` / `requires_api_base`
-- that list is already read in two places, which is one more than the Auth Method
entry above says this family may have.

**Model Row**:
One model as a person reads it: a Model Ref plus a label and a description, tagged with
the source that supplied them. Display only — nothing shaping a request reads a Model
Row (`providers/catalog.py`).
_Avoid_: confusing it with what a model can *do*. Whether a request may carry
`cache_control` blocks is a Prompt Cache Breakpoint question, not a Model Row one.

**Model Overlay**:
What a user states about a model no catalogue carries — a label and a description for a
self-hosted deployment. Beats the catalogue for the fields it sets.

**Prompt Cache Breakpoint**:
An Anthropic-shaped `cache_control` marker placed on a request so the prefix before it is
cached. Whether one may be placed is **(wire x model family)**: the wire has to have
somewhere to carry the field (`ProviderSpec.supports_prompt_caching`, a property of the
API being spoken) *and* the model's vendor has to be the one that reads it. A gateway
accepting the field is not the same as its upstream honouring it -- OpenRouter carries it
for every model it fronts and forwards it to vendors that bill the prompt twice.
Decided once, in `providers/prompt_cache.py`, which every marker asks.
_Avoid_: reading LiteLLM's per-model `supports_prompt_caching`, which answers "does this
model cache at all" -- a different question, and the one that produced the doubled bill.

**Token Rates**:
What a model costs per token, and separately how much context it holds. Both are facts
about a Provider's catalogue, so both are resolved in `providers/rates.py` rather than by
whoever is about to report a number. The two are deliberately sourced differently: rates
price a call after it happened, so the ladder may reach a community-maintained catalogue;
a context window sizes trimming and therefore shapes the *next* request, so only the
tables that also route may answer it. The window walks its own ladder
(`effective_context_window`): an explicitly configured value wins outright, then the
model's real window, then the module's documented fallback -- and a gauge that cannot
resolve the real window reports 0 so the UI shows its empty state rather than a number
that is nobody's.
_Avoid_: "pricing" for the resolution -- that names the arithmetic on top
(`token_wise/pricing.py`), which is a different module for a reason.

**Configured provider**:
`agents.defaults.provider`: which vendor's credential serves `agents.defaults.model`.
Said by the user, derived by nothing -- every surface that changes the model writes the
pair, and `config.set model` refuses a model without one. A config predating that rule
carries the empty string until the loader resolves it once and writes the answer down
(`config/loader.py::_migrate_auto_provider`); until then the vendor is derived from the
id, which is the guess the field exists to end.
_Avoid_: "pin" for this -- **Subsystem pin** above is a different thing (a model for one
subsystem, not for the conversation). Also avoid reading it as a provider *signal*: a
name says which section to ask about, never that the section holds credentials.

**Provider Endpoint**:
One url/key/headers group a provider section offers, of possibly several
(`ProviderConfig.endpoints`, resolved through `providers/endpoints.py::provider_endpoints`
whichever spelling the section used -- explicit list, Gemini's `api_key_list`, or the
flat fields). Several endpoints on one section mean several accounts on the same vendor;
`EndpointRotorProvider` spreads and fails over across them.
_Avoid_: two same-sounding neighbors. Routing's `ModelEndpoint` (`RoutingConfig.models`)
keys by *model* and picks a backend per request; a Provider Endpoint keys by *account*
under one provider. And a bare `api_base` is one endpoint's address, not the endpoint --
an endpoint is the whole credential group under a label.

**ResolvingProvider**:
The Provider the gateway is built with (`providers/resolving_provider.py`): it
holds no endpoint of its own and dispatches each call to the vendor adapter that
`Config.get_provider_name(model)` resolves to, memoized per vendor. Lets two
sessions on two vendors run concurrently without swapping a shared adapter. It is
the gateway's entire provider only with routing off or on the `ecoclaw` backend;
with `routing.backend == "knn"`, `build_model_routing` wraps it as
`PerModelProvider(..., fallback=ResolvingProvider)`, so routed model names go to
their configured endpoints and every other model still resolves through it.
_Avoid_: confusing it with ModelRouter / KNNModelRouter, which select a *model*;
this selects the *vendor* for an already-chosen model.

### RPC Protocol

**RPC Protocol**:
The single transport between Runtime and any interactive client (stdio pipe / Unix socket
for the TUI, a WebSocket for `raven serve`), carrying two message kinds: Request/Response
(client → Runtime method calls) and Notification (Runtime → client one-way events).
The rpc surface also hosts the CLI in-process: `cli.dispatch` runs Typer commands,
`commands.catalog` reflects them into the slash catalog, and the console injection
redirects their output — a surface-to-surface dependency the layer rule permits, recorded
here so it is a seat and not a surprise.
_Avoid_: calling it TUI-RPC — the terminal is one of its clients, not its owner; and calling
a Notification "the bus" or "broadcast" — Spine events never cross into a client directly

**Turn Event**:
A typed payload streamed to the TUI over Notifications while a turn runs
(e.g. `cron.delivered`, `confirm.request`).

**Subscription**:
A TUI client's registration to receive turn events for a session.

**Confirm Round-Trip**:
The interaction pattern for destructive operations: one `confirm.request` Notification
out, the turn pauses, one answering Request back.

### Context

**Context Engine** (`context_engine/`):
The layer that assembles each turn's LLM window. One unified engine —
`ContextAssembler` (`context_engine/assembler.py`) — runs an ordered pipeline of
SegmentBuilders in two phases: Phase A builds the system prefix in parallel, Phase B
budgets history serially against that fixed overhead. The historical
`legacy` / `curator` / `default` engine split was collapsed into this one engine;
`engine:` survives only as a backward-compat config alias.
_Avoid_: describing "legacy" and "Curator" as two separate engines — there is one
engine and the Curator is its Segment 6.

**SegmentBuilder**:
A pluggable contributor to the prompt; each builder produces one Segment for a fixed
slot in the pipeline. Builders run in `order`, optionally flagged `needs_prefix` to
defer into Phase B.

**Segment**:
A SegmentBuilder's uniform output: system-slot text, optional history (only the
Curator sets this), and metadata merged into the assembled context.

**Prompt Segments**:
The ordered blocks `ContextAssembler` renders into the system prompt, one per
SegmentBuilder: `# Raven` (identity), the Bootstrap Files block, `# Memory`
(host `user.md` ⊕ EverOS recall), `# Active Skills` (always-on) and `# Skills`
(SkillForge-routed candidates — see SkillForge), and `# Curator Working State`
(Segment 6). `context.dropSegments` names, by builder name, the host segments an agent does
without — a product whose bootstrap files carry its own identity drops `identity`.
_Avoid_: treating the system prompt as one opaque blob — each segment has an owner and order.

**Inject Mode**:
How an `always` skill occupies `# Active Skills`, declared per skill as
`inject: full` (default — the whole SKILL.md body) or `inject: description`
(a digest entry: name, description, and the absolute SKILL.md path to read on
demand). Description mode keeps a skill permanently discoverable at a few dozen
tokens; it is the only way to surface an `always` skill cheaply, since being
`always` also excludes it from BM25 routing into `# Skills`.
_Avoid_: calling a description-mode entry an "injected skill" — its body never enters the prompt.

**Skill Requirements**:
A skill's optional `requires` block, declaring what its procedure needs before
it is worth showing. `bins` / `env` are process-static and resolved in
`SkillRegistry.check_available`; `tools` is live runtime state (a tool can be
registered or hot-applied at runtime) and is enforced per turn by `ActiveSkillsSegmentBuilder` against the definitions
actually being sent. Every sub-key is optional and every malformed shape
degrades to "nothing declared" — see `requires_list`.
_Avoid_: checking `requires.tools` in the registry — it has no view of the live ToolRegistry.

**Curator**:
An internal, bounded agent loop whose only job is to build the main agent's next
context window; wired in as Segment 6 (`CuratorSegmentBuilder`). It never answers the
user and never runs user-facing tools.
_Avoid_: calling legacy's lossy summarization "curating"

**Fast Path**:
Curator's zero-LLM route, taken when history is under the pressure threshold:
full history passes through unchanged.

**Slow Path**:
Curator's small-model agent loop, run under context pressure: inspects the Manifest,
archives/retrieves, and submits a ContextPlan that a deterministic assembler validates.

**ContextPlan**:
The Curator's structured output that the deterministic assembler validates and applies:
which message ids and archive refs to include, which to drop, plus memory sections and
the Working State injection.

**Fail-Safe**:
The deterministic fallback when the Slow Path errors or produces no valid plan:
protected + most relevant + most recent messages, no LLM involved.

**Archive**:
Curator's lossless eviction: messages written verbatim to disk with a reference,
retrievable word-for-word later.
_Avoid_: archive vs Consolidation confusion — Archive loses nothing

**Consolidation**:
The legacy path's lossy distillation: when the prompt outgrows the window, old
messages are summarized into memory notes and leave the live history view; the
originals never return to context.
_Avoid_: summarize, compact -- three neighbours split this ground: Archive evicts
losslessly to disk, Consolidation distills across turns into memory notes, Compaction
(below) squeezes the live prompt inside one turn.

**Compaction** (`agents.defaults.compaction`, `config/schema.py:CompactionConfig`):
In-turn transcript compaction for long agentic turns, off by default: without it the
loop's in-turn shrinks are the standing image window (`_window_images`, which retires
pictures the model has already looked at before every call, bounded by
`agents.defaults.imageWindowBudgetBytes`) and the reactive, deterministic elision it has
always run on a provider's overflow error. Enabled, two layers join them on the same usage
readings: a
proactive layer that, once context crosses the trigger, prunes older tool-result bodies
first (deterministic, no LLM call) and only then replaces the transcript head with an LLM
summary while a recent tail stays verbatim; and a reactive completion that lets an
overflow retry with nothing left to elide take the summary path instead of surfacing a
fatal error. Summaries always run on the turn's own model and provider. Compaction
squeezes the live prompt inside one turn and writes no memory notes.
_Avoid_: compact for Consolidation or Archive -- those are cross-turn; this is not.

**Manifest**:
Curator's per-message metadata index for one session (tokens, snippet, relevance,
protected, pinned, archived) — what the Slow Path reads instead of full history.

**Pinned**:
A Manifest flag on the messages that fetched a skill body the agent cannot
re-derive (`context.pinnedSkillIds`, default the sub-agent DAG guide): the whole
tool exchange is added to every later ContextPlan whether or not the plan names
it, refused by Archive, and trimmed last. Set on the latest fetch per skill id
only, so a re-read moves the pin instead of adding a second copy.
_Avoid_: using "protected" for this — Protected means the head-of-session
exchanges, and it only shields an id from budget trimming, not from a
ContextPlan that never mentioned it.

**Working State**:
The distilled session notes (goals, open threads, decisions) the Curator maintains
and injects into the main agent's system prompt so evicted facts stay present.

### Memory

**EverOS** (`plugins-dist/everos-memory/raven_everos/`):
Raven's default memory-backend plugin (`everos-memory`; ships enabled, works out of
the box). Its own distribution rather than part of the raven wheel, found through the
`raven.plugins` entry-point group. (`plugins-dist/ppt-engine/` and
`plugins-dist/design-engine/` ship the same way -- the group's other two
distributed members, contributing a deck-building toolchain and a visual-design
engine rather than memory.) Provides dual-track semantic recall — the user track (episodes/profiles,
injected into the `# Memory` segment) and the agent track (skills/cases, one of
SkillForge's three sources at RRF weight 0.9). The name refers to the external package
[EverMind-AI/EverOS](https://github.com/EverMind-AI/EverOS); the in-tree code is only an
adapter. The same plugin also contributes the `understand_media` multimodal-parsing tool.

**Memory Engine face** (`memory_engine/__init__.py`):
The one address the rest of the tree reaches memory machinery by: `MemoryStore`,
`MemoryConsolidator`, the attention and behaviors parsers, the skill catalog,
sources, router, gate and rewriter, the store pipeline. Names resolve lazily, so
importing the face costs nothing until one is used, and `tests/test_memory_engine_face.py`
keeps every consumer on it -- the modules underneath are the engine's to rearrange.
_Avoid_: importing `memory_engine.consolidate.consolidator` (or any other submodule)
from outside the engine.

**SkillForge** (`memory_engine/skill_forge/`):
A skill retrieval and injection subsystem — it fuses candidates from three sources
(local BM25-indexed files, self-evolved skills recalled from the pluggable `MemoryBackend`
— typically the EverOS plugin — and remote skills from the Skill Hub) via weighted RRF,
with optional LLM gating and query rewriting before injecting them into the agent prompt.
Skill distillation/evolution is handled by the embedded EverOS extraction pipeline
(`skillForge.everos`), not by SkillForge itself — there is no feedback-driven evolution or
versioning, and the retirement knobs (`retire_confidence`, `retirement_idle_days`) are
unwired config placeholders, not active behavior. The name is retained; it is now a live
module under the Memory Engine, not the old top-level husk.

**Skill Discovery** (`skillForge.discovery`, default `"pull"`):
How retrieved skills reach the model. Under **pull**, a per-turn **Scent Menu**
(`context_engine/scent.py`) rides the user envelope: on a *fat* turn (rule-judged —
length, function-word residue, character-bigram novelty against the recent user
window), one `SkillForgeRouter.select` renders a few `qualified_id: description`
lines, and the model fetches bodies itself through `find_skill` (intent-bearing
search over the same router) and `read_skill`; the system prefix carries no
retrieved-skill bytes and no per-turn rewriter/gate LLM calls run. Under **push**,
the pre-existing pipeline (rewriter, router, gate, selected bodies rendered into
the system prefix) is restored unchanged.
_Avoid_: conflating the Scent Menu with the `# Skills` segment — the menu is
advisory tail-of-sequence data (wrapped untrusted), never a system segment; and
conflating `find_skill` (search, returns ids + descriptions) with `read_skill`
(body fetch by id).

**Skill Hub** (`skill_hub/`):
A remote OpenAPI skill marketplace, configured via `skillForge.router.hub` (`endpoint` /
`api_key` / `timeout_s` / `min_safety`; `endpoint=None` disables it). `SkillHubClient` offers
progressive disclosure — `search()` (metadata-only discovery), `get()` (skill body),
`install()` (download + safe extract); during routing `HubSkillSource` feeds metadata-only
candidates into the weighted RRF (weight 0.85, below Local 0.96 and Everos 0.9), and the
`read_skill` / `use_skill` tools do on-demand body fetch / script materialization. Replaces
the retired "Mass" source.

**PlugHub** (`market/`, package renamed from `plughub/`; wire names and RPC group keep the `plughub` spelling):
The plugin marketplace: a catalogue of installable integrations (`catalog.json`), and the
transactional installer that lands one. A catalogue entry contributes pieces -- an MCP
server, credentials, a skill -- and `install` lands them all or none. Distinct from **Skill
Hub**, which is a remote marketplace for skills alone.
_Avoid_: "market" on its own for either one -- both surfaces are called that in prose, and
the RPC groups (`plughub.*` vs `skillhub.*`) are separate.

**`plugin` tool** (`agent/tools/plughub.py`):
PlugHub's agent-facing surface: `find` / `connect` / `authorize` / `list` / `remove`, over the
same `market/connect.py` transaction the panel's `plug.*` RPC drives, called in-process. It
installs catalogue entries only and accepts no credentials, so an entry that needs an API key
is reported by field name rather than installed; an OAuth connect returns the authorization
URL as soon as the flow mints it instead of waiting for the click, and opens no page -- the
host running a turn is not necessarily the machine the person who asked is sitting at.
_Avoid_: reading its name as the **Plugin** term below -- that is a `raven-plugin.toml`
component under `plugin/`, which this tool neither sees nor installs. The two vocabularies
meet only in the word.

**Ledger**:
One JSON file per PlugHub-installed plugin (`plugins/<catalog_id>.json`), recording the
exact pieces a transaction landed so uninstall replays them in reverse rather than
guessing. It is also the provenance oracle: a config entry **with** a ledger came from the
market, **without** one was written by hand -- which is what decides whether removing it
replays pieces or just deletes a config stanza.
_Avoid_: "manifest" -- that is the plugin's own declaration; a ledger is the record of one
install of it.

**Playbook** (`raven/playbook/`):
A stored, reusable orchestration for a family of tasks: one `playbook.md` per
directory under a playbook root, in three regions — a two-field frontmatter
(`name` / `description`), a human-readable body, and one fenced
`yaml playbook-spec` block holding every machine field. Being under a root is
what makes it a playbook, so no marker field can disagree with where the file
sits — one directory, one file, no sidecar and no lifecycle fields. The library
is two such roots layered: `raven/playbook/builtin/` is the release layer — read-only,
resolved at that path and absent until a release adds a playbook there; the layer is
what lets a release carry a playbook, not a bundled catalogue — while `<agent_home>/playbooks/` (override:
`playbooks.dir`) is
where both creation entries — `raven playbook create` and the `create_playbook`
tool — land their product, usable on arrival (`playbooks.disabled` holds one
back, and is read live); a user directory reusing a
builtin's name shadows it, with a load warning. A generator's open questions and
assumptions go into the body (its `## Open questions` section) for a human to
read; whether a playbook is offered on this machine is config (the
`playbooks.disabled` deny list — absent means on; disabling takes it out of what
the model is shown, and `raven playbook run` still resolves it), because the file
is the distribution unit and local state must not travel with it.

The two modes differ in where the graph comes from, and therefore in who acts on
a load: `dag` ships it as `nodes`, which the engine fills and dispatches through
`SubAgentDagTool.execute` — the same entry a model-composed graph takes, so one
validation, one scheduler, one billing path. `prompt` ships assembly guidance as
`prompts` and the *caller* composes: in a conversation the model gets the filled
guidance and submits its own `run_subagent_dag` call, while the CLI, having no
model in the room, composes with one call of its own. `mode` is the author's
statement of how completely they specified the procedure, and is deliberately not
in the tool signature.

**Discovery is the model's, not a matcher's.** A playbook is reached through
`load_playbook`, one of the tools a turn can use, alongside `spawn` and
`run_subagent_dag` — there is no pre-turn interception and no LLM gate.
`triggers.keywords` decides which playbooks get *described* in that tool when the
library is larger than `playbooks.router.topK`; the `name` enum stays the whole
library, so a retrieval miss leaves a playbook undescribed rather than
unreachable. What the caller may supply is bounded to `params` and `fills`, and a
`fills` entry aimed at a field the playbook already wrote is refused — so a
playbook can be completed but never edited, and the file in git stays an accurate
account of what ran.
_Avoid_: calling `triggers.keywords` a trigger — a keyword makes a playbook
visible, never run. And avoid describing `confirm` as a playbook-level gate: it is
`SubAgentDagSpec.confirm`, a graph-level parameter the playbook's value is
injected into, which is what let the passive funnel be deleted without the gate
going with it.
_Avoid_: calling it a Skill or a SKILL.md — a playbook has its own root, its own
file name and its own loader, and is not indexed by `skill_local`. Also avoid
conflating it with a sub-agent DAG run (`run_subagent_dag` executes one graph a
model just wrote; a playbook stores one for reuse).

**SkillPolicy** (`skill_hub/policy.py`):
The install-time safety decision both Hub install paths consult before any
`SkillHubClient.install()` — the segment builder's post-gate hydrate and the `use_skill`
tool. `refusal_for_detail()` checks, in order: the operator blocklist
(`skillForge.blocklist`, matched case-insensitively against name / slug / native id), the
`min_safety` bar against the *detail*-level `score_safety` (the catalog payload omits the
score; a missing or malformed score passes), and an external home-dotdir lint over the
skill body (`~/.raven` is allowed; any other dotdir reference refuses the install). A hub
candidate whose detail fetch fails is unvetted and dropped — it never reaches `install()`.
Every install that passes is appended to a JSONL audit trail
(`<workspace>/skills/hub/installs.jsonl`, `skill_hub/audit.py`).
`install_skip_reason()` is the separate operator-consent gate over the bundle download
itself (`skillForge.autoInstall`: `auto` / `prompt` / `off`), consulted by both call sites
right before `install()`, after all safety vetting. A consent decline is a **skip**, not a
refusal: the already-vetted skill body still injects (and `read_skill` still works), only
the on-disk bundle is withheld. Alongside the JSONL trail, a passing install stamps a
one-time `.install-meta.json` into the skill directory (`write_install_meta`, first
install wins) — the O(1) provenance source behind `raven skill list`'s Installed column.
_Avoid_: calling an autoInstall skip a "refusal" or "block" — refusals are safety verdicts
on the skill; a skip is withheld operator consent for the download.

**Episode**:
A distilled event note the Consolidation step writes to `episodes.md`.

**Profile**:
The user-profile sections in `user.md`, refreshed when their tags run hot.

**Foresight**:
A prediction the Memory Engine derives about the user's likely future behavior
(each carries prediction / time-window / confidence), written by the consolidator.
_Avoid_: conflating with the Proactive Engine's Predictor — Foresight is the stored
memory artifact; the Predictor is the live proactive stage.

**Consolidator** (`memory_engine/consolidate/`):
The Memory Engine component (`MemoryConsolidator`) that performs Consolidation --
under session-token pressure it annotates evicted message chunks into Episodes,
refreshes hot Profile sections, and (opt-in) emits Foresight. The single context
engine declares `owns_compaction`, so a turn does not call it for token pressure;
`/new` still archives an unconsolidated session through it.
_Avoid_: conflating with the Curator -- the Curator builds the context window
losslessly and archives history itself; the Consolidator is what writes long-term
memory.

### Knowledge

**Knowledge Base** (`knowledge/`):
A named set of documents a user uploaded, indexed for retrieval in a turn. Records
the embedding model and vector width it was built with, because those are facts
about the base rather than about today's config.
_Avoid_: conflating with Memory — Memory is what the agent learned from its own
turns; a Knowledge Base is material a person handed it.

**Section**:
One parsed region of a source document, before chunking — a heading and the text
under it, a page, a slide. A parser produces Sections and never splits them.

**Chunk**:
One embeddable piece of a Section, carrying its place in its own document
(`chunk_index` / `total_chunks`) and the Section it came from. A Chunk never spans
two Sections, which is what keeps the structure a parser found from being averaged
away before anything is retrieved.

**Collection**:
The vector store's container for one Knowledge Base's Chunks, named for the base's
id. Sized to the embedding width at creation.

**Stale Base**:
A Knowledge Base whose recorded embedding model or width no longer matches the
configured one. Refused rather than searched: its vectors answer to the old model,
so a query embedded with the new one lands somewhere unrelated in the same space.
Moving the endpoint or rotating the key does not make a base stale.

### Plugins

**Plugin** (`plugins/`):
A component declared by a `raven-plugin.toml` manifest (`[plugin]`: `id`, `version`, optional
`bundled` / `enabled_by_default`). It contributes capabilities via
`[[plugin.contributes.<kind>]]` arrays — currently `memory_backends`, `tools`, `hooks`, `services`, `tool_gates` and `session_observers` —
each naming a `factory` (`module:callable`). The host passes the user's
`plugins.config["<id>"]` dict verbatim to the factory as `PluginContext.config`. A `hooks`
contribution returns an `AgentHook` the assembly root appends to the loop's chain: it is how
product code steers the turn loop from a plugin directory (`<home>/plugins`, `./.raven/plugins`
beside a product, a root named in `plugins.dirs`, or an entry point) instead of a fork. A factory
may decline by returning `None` (a clean opt-out, logged, never fatal), and a contributed tool
that needs what only the assembled loop owns declares `bind_runtime(handles)` and receives the
frozen `RuntimeHandles` grants once the loop finishes assembling -- the register-first,
bind-later idiom `ask_user` has always used for its broker, as a first-class contribution shape;
a binder may raise `BindDeclinedError` to be taken off the table quietly, the late-bound twin of a
factory returning `None`. The wheel carries its own shelf of such plugins,
`raven/plugins/bundled/` (origin `bundled`, shadowed by nothing): the playbook entry tools
(`load_playbook` / `create_playbook`) live there and bind the loop-assembled
`RuntimeHandles.playbook_runtime` funnel, so the loop keeps one dispatch gate, one quota and
one announce path while the tools ride the plugin contract.

**Plugin Registry** (`plugins/registry.py`):
The `PluginRegistry` discovers manifests, activates those not in `plugins.disabled` (respecting
`enabled_by_default`), resolves each `module:callable` factory by dynamic import, and registers
contributions into per-kind tables — deduping plugins by `id` and contributions by `name`
(`PluginConflictError` on collision). `build_memory_backend()` / `build_tool()` construct a
contribution with a fresh `PluginContext`.

**Service** (`raven/contracts/services.py`):
A plugin's background-service contribution (the `services` kind): a resident host runs it
and owns it, for watching that outlives any turn (an event poller pulling keyed wakes
forward, a queue drainer). A dumb loop on the B side of the seam: it consumes its
`PluginContext` and, when it declares `bind_runtime`, the late-bound `RuntimeHandles`
grants -- and never mutates the host's assembly.

**ToolGate** (`raven/contracts/tool_gate.py`):
A plugin's per-call tool-adjudication contribution (the `tool_gates` kind). Gates are cast
over the tool registry at assembly and fixed for the generation; `adjudicate` runs after
parameters are validated and before dispatch. A non-None verdict replaces that one call's
result (the call does not execute), None waves it through, and a gate that raises refuses
the call it was adjudicating -- failing open would make its bugs silent permission grants.
Gates run in lexicographic (name, contributing plugin id) order; the first non-None
verdict wins.

**SessionObserver** (`raven/contracts/session_events.py`):
A plugin's session-retirement contribution (the `session_observers` kind): the session
store calls `on_session_deleted` synchronously after it has acted on a delete, from
whichever host surface asked. The store notifies and never waits -- an observer that
raises is logged and skipped; no veto, no repair. For a plugin keeping per-session state
outside the session store (an allocation ledger, a provisioned directory).

**Visual Domain Selector** (`plugins-dist/design-engine/raven_design/selector.py`, hook seat `raven_design/plugin/hook.py`):
The design engine's per-turn domain router, seated on the `hooks` kind
(`before_user_inbound`): one LLM call per user turn compares the query against
the full bodies of the fifteen packaged domain Skills and appends a two-tier
card block (preferred and alternative Skill ids, bodies read on demand via
`read_skill`) below a separator on the model's view of the inbound -- the
session record keeps the user's own words on every turn outcome. The call
rides the conversation's own binding; `plugins.config["design-engine"]
.visualDomainSelector.enabled` is the off switch, and a selection failure
degrades to the full description catalog for that turn.

**Admission** (`config/admission.py`, `plugins/registry.py:_admit`, `agent/tools/registry.py:admit_tool`):
The declare-check-dispense pattern at a boundary: the owner declares its authored members
(a manifest's `config_schema`, a tool's four authored members), the door checks the
declaration once at entry, and dispenses a frozen result (an admitted config slice, a
`ToolSpec`) that the machinery reads afterwards. An empty declaration keeps verbatim
pass-through. Failures name the owner and the key at the door, not deep inside a turn.

**Config-with-cargo** (`channels/contract.py:ChannelSpec.config_schema`, `raven-plugin.toml [plugin.config_schema]`):
A cargo declares the config keys only it consumes -- types, defaults, secrecy,
requiredness, choices, nested `fields` -- next to the code that consumes them. The
declaration is the only truth: the door
dispenses from it, the writer (`config/update_channels.py`) validates through the same
door, and the declaration guard (`tests/test_channels_config_declaration.py`) pins the
door's own contract.
_Avoid_: "schema" alone — the config file's JSON and the cargo declaration are
different artifacts.

**Channel Socket** (`config/schema.py:ChannelSocket`):
The host-side view of one channel section: `enabled`, `allow_from`, `workspace` --
what the host plugs every channel into, uniform across adapters. `ChannelsConfig` is
dynamic: any discovered adapter answers a socket view whether or not the file has its
section (sticky access, so mutation persists), and sections indistinguishable from the
default socket are dropped at serialization to keep the file sparse.
_Avoid_: "channel config class" (the twelve central classes are retired); cargo fields
read through the dispensed view, never by name on the socket.

**Generation** (`core/runtime.py`, gateway):
One assembled `RavenRuntime` serving turns -- a frozen dataclass: after construction a
generation is sealed. What FREEZE seals is member IDENTITY -- which objects the
generation is made of; a member's own data plane stays its own business, and exactly
three declared doors reconcile it mid-generation after the durable truth is written
(the agents table via `apply_agents`, the MCP server set via `apply_mcp_config`, the
default binding via `set_default_binding` -- criterion and roster on the `RavenRuntime`
paper, operators pinned by the door-roster guard in `tests/test_core_runtime_swap.py`).
Every other change is generation N+1, never an in-place mutation
(the composition phases COLLECT / ADMIT / BIND / START / FREEZE; `build_runtime` maps
its steps to them). A config change swaps generations at a turn
boundary: BUILD N+1 comes first (a candidate that fails to assemble leaves N serving),
SWAP re-runs the gateway's generation wiring (spine, sinks, sentinel attach), DISPOSE
retires N in a pinned order (`RavenRuntime.dispose`). Process-lifetime transports --
channels, cron, the sentinel runner, the control plane, health -- survive the swap. One
swap at a time (`SwapCoordinator`: the slot is held from BUILD until right before the new
loop runs, and accepted swaps are rate-limited). Honest timing: the serving loop stops
within ~1s of the request; in-flight turns get `gateway.shutdown_grace` and are then
cancelled. Triggers: `gateway.reload` on the Control Plane (cross-platform) and SIGHUP
(POSIX alias of `reload --force`; a signal has no reply channel).
_Avoid_: "hot reload" (`reload.mcp` is a tool-set reconcile inside one generation, and a
preference edit taking effect is the Live preference lane below -- neither swaps a generation);
"restart" (the `/restart` control command, a whole-process execv).

**Live preference** (`config/live.py`):
The pull lane for config that may change its answer while a generation serves: one file,
re-parsed only when its bytes change, keeping the last good answer through a torn write.
The module is the roster -- every key this lane serves has a named reader there (both file
spellings), and machinery on this lane asks the reader, never the file by key: `disabled_tool_names`,
`disabled_playbook_names`, `exec_extra_deny_patterns`, `web_search_key`,
`media_tool_config`, `mcp_server_configs` (feeding the turn-boundary reconcile),
`routing_profile`, `default_model`. The boundary is the module docstring's rule: anything
whose change implies WORK rather than a different answer (constructing a provider,
an MCP handshake, moving a workspace) keeps its apply path -- a door, the pool's
fingerprint cache, or the generation swap. Timing is asymmetric by design: a revocation
binds the next read (a tightened deny pattern gates the very next tool call), while an
addition to the model's tool array lands on the next turn -- `ToolRegistry.turn_scope`
freezes both registry membership and the withheld set at turn entry, because the array
is the prompt-cache prefix and must not move between two model calls of one turn.
_Avoid_: reading `config.json` keys ad hoc outside this module; treating a live
preference as a door (doors reconcile members after a durable write; this lane never
touches member identity).

**Wire Schema** (`rpc-schema/openrpc.json` at repo root):
The hand-maintained OpenRPC contract for the terminal dialect every interactive client
speaks (TUI, the served page, ACP). Cross-language neutral ground, machine-read by both
frontends' codegen scripts, the Python match guards, CI and a pre-commit drift hook --
which is why it lives at the root and not inside any one consumer (moved up from
`ui-tui/` by ce526ad5: a shared contract is not named after one of its clients). The
gateway control plane has no OpenRPC document: its only client is another raven process.
_Avoid_: treating it as a paper -- papers describe Python seams; this is a wire artifact
consumed as cargo by tooling in two languages.

**Control Plane** (`rpc/control.py` server, `gateway/live_probe.py` client):
The gateway daemon's window for other raven processes: runtime facts only it can answer
(`gateway.channels.live`, `.qr`, `gateway.status`) and host commands only it can execute
(`gateway.channels.start`, `gateway.reload`, `gateway.shutdown`). Loopback WebSocket,
per-boot token as the first frame, endpoint published in the gateway lock (0600);
process-lifetime with the dispatcher registered once -- nothing on it depends on the
generation. The charter is the whole vocabulary, pinned by `tests/test_rpc_control.py`;
never on it: turn or chat streams, config writes. The server sits on the surface side
because the daemon package is inner and may not import `raven.rpc`.
_Avoid_: "web channel" / `web_rpc` (retired: the ui-webui dialect this grew out of).

**Kernel** (`spine/` + `contracts/` + `tracing/` + `home.py`):
The shippable core: the L0 spine, the L1 papers, tracing (whose only import-time edge
into the kernel is the paper's instrument decorator), and the address resolver they all
need. Machine-enforced by the "the kernel stands alone" import-linter contract in
pyproject.toml, which carries no exceptions; the raven-core wheel is this set as a build
artifact: `make build-core` runs scripts/build_core_wheel.py, whose roster is read from
that same contract, and tests/integration/test_kernel_wheel_smoke.py proves the wheel
stands alone in a clean venv.

**Home** (`home.py`):
Where raven keeps everything: `RAVEN_HOME` or `~/.raven`, and the config file inside it,
with an override a second instance can set. Kernel rather than config, because the kernel
has to find its own settings -- a core that could not locate `config.json` without the
config shelf would not be the closure the wheel claims -- and because the answer steers
the installer, the node runtime lookup, the trace directory, the cron store and the serve
state file alike. `config/loader.py` re-exports all three names, so every existing caller
reads them where it always did.
_Avoid_: resolving `RAVEN_HOME` again anywhere else -- that is how two directories become
the answer to one question.

**PathPolicy** (`contracts/path_policy.py`):
The disk-layout paper: the home rule's vocabulary (`HOME_ENV_VAR`,
`DEFAULT_HOME_DIRNAME`, `CONFIG_FILENAME`) and the workspace default sentinel,
declared once so neither the tree nor the vendored launchers (which re-derive
them by hand) drift. Constants only, deliberately: path-escape checking is the
tools' security boundary, and a callable protocol joins when a consumer types
against it.
_Avoid_: hardcoding `~/.raven`, `config.json` or the workspace sentinel string
outside this paper and its implementers.

**Historical Plans** (`docs/plans/`):
An archive, not a promise: a plan describes the tree as it stood on its own
date, and holding one to today's layout would make it lie about that date. A
runnable snippet inside one is therefore not a public seam -- the public surface
the vendored products pin is read out of `subagents/*/install.py`, `install.sh`
and the README by `tests/test_external_consumer_surface.py`, and the living-doc
pointer guard deliberately skips this directory.
_Avoid_: updating an old plan to match a rename -- fix the living document that
cites it instead, or leave it as the record it is.

**Span Vocabulary** (`observability/`):
What a raven span means -- the attribute extractors, and the usage block they report --
as against the machinery that opens and closes one, which is kernel. The split is what
lets the kernel stand alone: deciding that an LLM span carries its routing backend means
splitting a model id (`providers/registry.py`), and putting a number on a usage block
means pricing tokens (`token_wise/pricing.py`), so a kernel that held the vocabulary
would reach into two shelves for it. An instrumented site passes its extractor as an
argument (`@trace.instrument("llm.call", extract=semconv.llm_call)`), so the extractor
travels with the caller and the kernel never names one.
_Avoid_: "telemetry" for either half -- it names neither the machinery nor the meaning.

**Layer Seats** (pyproject.toml `[tool.importlinter]` + `tests/test_l4_entrances.py`):
Where every package sits, as the machine enforces it. Inner (may not import a surface):
the shelves and engines the contract lists as sources (`tracing` among them -- a kernel
member, seated inner here as well so every package appears in one roster), `config` and
`utils` and `i18n` (cross-cutting leaves), `mcp`, `playbook`, `knowledge`, `skill_hub`, `trajectory`,
`permissions` (the gate the registry consults; its turn context is bound by entrances),
`eval_engine` and `proactive_engine` (L3 shelf members -- proactive_engine originates
turns through its schedulers and sentinel but is an engine the loop and the assembly root
consume, not a transport), and `core` (the L2 assembly root). `templates` is packaged data
and takes no seat. Surfaces: `cli`, `rpc`, and `acp` (an entrance: Raven serving as an
agent for another host). `browser` and `importer` are seated inner (feature
libraries consumed by surfaces, importing none themselves -- the edge is watched
by the contract now, not by a ruling note). `evolver` is not a seat at all: it left the
package for the repo-level `evolver/` tool (outside the wheel) that drives raven as a library,
and a fifth import-linter contract keeps the runtime from importing it back. `agents/` is the
same kind of non-seat: repo-level product definitions (the A/B pilots against the frozen
`subagents/`) that consume installed raven over `raven acp`, with a sixth contract keeping
the runtime out of them — the wheel carries the tree as data (`raven/agents`, mapped by
`hatch_build.py`) for the roster's file-level discovery, which imports nothing from it;
the directory name is provisional by ruling. One ruled edge: `trajectory` (L3)
reaches `config.admission` for the door vocabulary and builds a loop by hand for replay --
legal, because it is a harness over recorded runs, not an entrance. One package holds two
seats: in `agent/`, `agent/loop` is the L2 harness shell every entrance runs, and its
siblings -- `tools`, `subagent`, `context`, `hook`, `personalizer`,
`workdir` -- are L3 cargo the loop consumes; the "cargo does not import the loop shell"
import-linter contract keeps the two seats apart in the shared directory, which is why
the package is not split physically (ruled 2026-08-30). The one exception the target
tree always named: the ACP client family -- the client, the `acp_agent` backend that
speaks it, and the `acp_dialects` that translate other vendors' tool records -- moved
out to the top-level `acp_client/` shelf (2026-08-31), which joined the inner-layers
seat list and stays under the cargo contract.
`acp_client/` is named for its side of ACP (Raven driving somebody else's agent);
`acp/` is the other side, the entrance.

**Surfaces law** (ruled 2026-08-31):
The three entrances relate asymmetrically. A SERVED surface (`rpc`, `acp`)
never imports the launcher or a sibling surface's insides -- two import-linter
contracts pin `{rpc, acp} -x-> cli` and `rpc -x-> acp` with zero exceptions.
The LAUNCHER direction (`cli -> rpc/acp`) is sanctioned by the existing axiom
that an entrance brings its own transport-side wiring: the cli is the entrance
that assembles and hosts the others. What a served surface genuinely needs
from the cli arrives by registration (`rpc/cli_socket.py` carries the console
feature's command table; `rpc/serve_control.py` is owned by the reading side
and armed by the host). The one remaining directed edge -- acp hosting an rpc
stack over its translator -- is pinned to the single `raven.rpc.bootstrap`
facade module by the roster guard in `tests/test_l4_entrances.py`.

**Updates** (`updates/`):
The install's own lifecycle as an inner feature library (the browser/importer
pattern): release lookup and version keys, the upgrade plan and detached
handoff (`upgrade`), the startup update nudge (`update_notice`), the beta
channel pointer (`beta_channel`), and the install-integrity record
(`install_guard`). Consumed by the cli (which keeps only the `raven upgrade`
typer shell) and by `rpc.methods.system` -- the largest chunk of the
rpc-imports-cli edges retired by moving it inward (2026-08-31).

**Assembly Root** (`core/`):
The package that composes a running agent out of parts: one `*_stack` builder per assembly
concern, and `runtime.build_runtime` as the one door every entrance assembles through --
an entrance brings its transport-side wiring (`TurnPolicy`, `HostWiring`) and takes back a
`RavenRuntime`; deriving a cargo bundle by hand in an entrance is the regression
`test_cli_agent_loop_parity.py` exists to catch. One deliberate bypass: `trajectory/replay.py`
builds a loop by hand against a recorded run (fake provider, replaced registry) and drives it
directly; it is a replay harness, not an entrance, and `test_cli_agent_loop_wiring.py` lists it.

**Paper** (`contracts/`):
A declared shape the layers hold each other to -- the shapes a shelf implements against,
the asking capabilities a tool types against (`contracts/asking.py`). The turn contract is
not a paper: it is spine's (`spine/turn.py`).
Papers export declared members only and
import no machinery -- the stdlib, pydantic, the other papers and the spine, nothing else
(`TYPE_CHECKING` blocks exempt). Two promise tiers, stamped per module via `__tier__`:
`contract` (frozen for every loop) and `factory_loop` (versioned with the factory loop).
Both rules are enforced by `tests/test_contracts_two_tier_ledger.py`.
The contract tier is versioned: `CONTRACTS_VERSION` (`contracts/__init__.py`) moves
whenever a contract-tier paper's declared surface changes shape (never for prose);
the surface digest pinned in the same test file makes a silent change a red gate.

### Security & Access

**AUTH** (`auth/`):
Authentication & authorization primitives (e.g. allowlist). Classified as a
cross-cutting mechanism: a leaf consumed by inner layers and cargo, never the
other way (enforced by the layer contracts in `pyproject.toml`).

**Security** (`security/`):
The address vocabulary (`hosts.py`: what a host string denotes, in every
spelling) that the outbound policy (`network.py`: default-deny fetchability, the
guarded per-hop fetch), the market's vetting (`market/vetting.py`) and the browser's navigation
policy all read; and prompt-injection fences (`trust.py`). A cross-cutting
mechanism and a member of the channels' shared-services shelf -- cargo may
depend on it (dingtalk and qq do). Same leaf rule as `auth`.

**Permission Gate** (`permissions/`):
The decision waterfall `ToolRegistry.execute` consults before dispatching any
tool call: the builtin unconditional deny list (catastrophe-class commands
only, a recursive delete of the root or home tree among them), then the
user's tiers and exec prefix rules from `permissions` in config, then the
permission mode's reading of the ask tier. A command family a surface declares
(none by default; the ACP editor declares deletion and the external-effect
families) does not decide -- it names the prompt the human reads when the call
lands on one, and is recorded on the call's tool.call span as
`permission.family` whichever way the tiers then decide. Answers with the `Decision` vocabulary from
`contracts/permissions.py`; the turn's asking capability is bound through
`permissions/turn.py` by the entrance. Distinct from a plugin's Tool Gate
(`contracts/tool_gate.py`): the gate is platform authority, runs first, can
wait on a human, and answers with a full ToolResult.
_Avoid_: "tool gate" for this -- that name is the plugin paper's.

**Permission Mode**:
How the gate reads the ask tier, and only the ask tier: `ask` prompts a human
for everything in it, `smart` has an LLM reviewer (`permissions/judge.py`,
fail-closed onto escalation, every review recorded on the tool.call trace span) allow or escalate, `full`
runs it without prompting. The unconditional deny list and user deny rules hold in every
mode.
Stored at `permissions.mode` in config, the default every conversation starts
on; a conversation can run in a mode of its own (`config.set` with its
`session_id`), kept the way its model is kept: in memory by
`permissions/session.py` and on the conversation's record
(`metadata["permissions_mode"]`), so a restart does not undo it. The gate
reads both live per tool call, the conversation's own first.

**Tier**:
One tool call's standing with the gate: `allow` runs, `ask` needs a human (as
the mode reads it), `deny` is refused. Resolved from the user's
`permissions.tools` node -- a tool name to a tier, or for `exec` a table of
command prefix patterns where specific matches resolve strictest-wins and `*`
is the fallback -- with read-only tools defaulting to allow and everything
else, unknown tools included, to ask.

**Templates** (`templates/`):
Packaged data assets, zero Python: read as package data (`utils/workspace.py`)
and shipped by the wheel, the per-language prompt templates under
`templates/prompts/<language>/` among them. An asset directory, not a code
package -- it takes no layer assignment.

**I18n** (`i18n/`):
User-facing text in the user's language. `t(text, **arguments)` translates by
the English source text (gettext style, so a message id is the message), `t_in`
does the same in a language the content rather than the UI decides, and
`prompt(name)` loads a model-facing template from `templates/prompts/`. The
Chinese catalog is `zh.py`; `zh_lexicon.py` holds Chinese language *data* an
engine consults (cue words, punctuation classes, the legacy attention headers),
which is not translation. The language is process state a host sets: the
onboarding wizard from its first screen, every other entrance from
`config.language`. A cross-cutting leaf, seated inner: `tests/test_i18n_boundary.py`
keeps Chinese literals out of every other module.
_Avoid_: calling `zh_lexicon` a catalog -- one is what raven says, the other is
what raven recognises.

**Browser** (`browser/`):
Browser automation (`driver.py`) and its outbound policy (`policy.py`).
Consumed by surfaces only; a surface-side feature library like `importer`.

### Execution & Evaluation

**SandBox** (`sandbox/`):
Isolated command execution (microVM / boxlite); owns the debug server and VM lifecycle.

**EvalEngine** (`eval_engine/`):
The L3 evaluation engine: task judging and cognitive coordination, implemented as three
`AgentHook` instances (`BeforeIterationHook`, `AfterIterationHook`, `ToolAuditHook`).
Buildable (`core/eval_stack.py`) but unwired today: no production path constructs an
EvalEngine or passes its hooks to the loop -- activation is a pending ruling, and this
entry says so rather than describing wiring that does not exist.

**EvalJudge** (`eval_engine/judge/`):
The single-call LLM judge behind the EvalEngine's task-completion check: it compares the
turn's original user goal against the final response and returns a JudgeVerdict. Any error
path returns `unknown`, so the judge can never crash the Agent Loop.
_Avoid_: "task judge" as a class name — the class is `EvalJudge`.

**JudgeVerdict**:
The three-state outcome an EvalJudge returns: `completed` (goal addressed), `failed`
(visible error / missed objective), or `unknown` (indeterminate). The `AfterIterationHook`
writes completed/failed (never unknown) into `HISTORY.md`.

### Trajectory

**Attempt**:
One task try, possibly spanning several turns — the stable address of a trajectory.
Every span carries `attempt.id` (`raven/tracing/spans.py`); without an explicitly
opened attempt (`trace.begin_attempt(session_key)`) each turn is its own single-turn
attempt whose id equals the trace id, so every trace is addressable as an attempt.
_Avoid_: "run id" / "task id" — neither is bound to span records.

**Trajectory Verdict** (`raven/trajectory/verdict.py`):
The task-outcome label for one Attempt: `pass` / `fail` (agent failure) / `infra`
(environment or harness crash, excluded from diagnosis), plus the judging `source`.
Appended to `verdicts.jsonl` beside the trace logs by whoever can judge; deliberately
outside tracing — `status.code` says whether code crashed, a verdict says whether the
task succeeded.
_Avoid_: confusing with JudgeVerdict (the EvalEngine's completed/failed/unknown).

**Trajectory Pin** (`raven/trajectory/store.py`):
The retention promise for an Attempt or trace id, recorded in `pins.json` in the trace
state dir: pinned ids are corpus, not diagnostics — purge tooling must never delete
their spans or the artifacts those spans reference.

**Trajectory Bundle** (`raven/trajectory/bundle.py`):
The self-contained offline directory `collect_bundle` / `raven trajectory save` packs
for one Attempt: `manifest.json` + `spans.jsonl` (artifact references rewritten to
bundle-relative paths) + `artifacts/` + the session's conversation record + its
verdicts. Bundling declares the trajectory corpus, so the id is auto-pinned.
_Avoid_: "archive" — that names the tracing store's rotated-log directory.

**Trajectory Redaction** (`raven/trajectory/redact.py`):
The three-layer sanitization `redact_bundle` applies to a **copy** of a Trajectory
Bundle (the original is never modified): exact replacement of known secret values
(secret-typed config fields + credential-shaped env vars, stable
`[REDACTED:<source>]` placeholders, JSON-escaped spellings included), regex fallback
for common credential shapes, and a residual scan that flags high-entropy leftovers
for human review without rewriting. Non-UTF-8 files are excluded from the copy.
_Avoid_: "masking"/"anonymization" — redaction removes credentials, it does not
de-identify the user.

**Trajectory Report** (`raven/trajectory/report.py`):
The shippable form of a trajectory produced by `raven trajectory report`: the
redacted copy of its Bundle plus `redaction.json` (per-layer replacement counts,
residual findings, binary policy) packed into a `.tar.gz`, delivered through the
pluggable `Uploader` protocol (v1 backend: `local` — the tarball itself, nothing
is sent anywhere).
_Avoid_: calling the unredacted Bundle a "report" — only the redacted tarball leaves
the machine.

**Trajectory Replay** (`raven/trajectory/replay.py`):
Mock re-run of the harness against a Trajectory Bundle (`raven trajectory replay`):
recorded model replies (`llm.output`) and tool results (`tool.output`) are fed back
in recording order through a `ReplayProvider` and a `ReplayToolRegistry` while the
live agent-loop code runs for real. No real tool ever executes, and the replay run
emits no spans (tracing is disabled for its duration).
_Avoid_: confusing with a real re-run against live models/tools — that is evolver
evaluation, not replay.

**Replay Divergence** (`raven/trajectory/replay.py`):
The point where the live harness's request stops matching the recording — the
expected outcome once a bug is fixed, not an error. Detected per model call
(model id, message roles/contents, tool-call names+arguments, offered tool names,
under nonce/timestamp/cache-control normalization) and per tool call (name +
arguments). Policy `strict` halts at the first divergence; `warn` reports and
keeps feeding by order. Each divergence carries the structured `expected`/`actual`
values of its field, and the replay report captures every live request
(`llm_requests`/`tool_requests`) for programmatic assertions.

**Trajectory Cassette** (`raven/trajectory/cassette.py`):
The committable form of a Trajectory Bundle, produced by `minimize_bundle` /
`raven trajectory minimize`: same directory layout, but shrunk to the exact
surface `load_recording` consumes (consumed spans/artifacts/fields only,
system-prompt content replaced by a placeholder, the session record sliced to
the pre-attempt history) and passed through Trajectory Redaction. Payloads are
never truncated — a field is kept whole or dropped whole.
_Avoid_: "minimized bundle" as a distinct term — a cassette *is* a bundle to
the replay layer.

**Trajectory Regression Case** (`raven/trajectory/regression.py`, `tests/trajectories/`):
One directory pinning a fixed harness bug into CI: a Trajectory Cassette
(`cassette/`) plus an expectation file (`expect.yaml`) declaring where the
replay's first Replay Divergence must land and what the live side must do
there (message contains/not-contains/equals, tool name/params checks).
Discovered and run by `tests/test_trajectory_regressions.py`; asserting
"divergence at the expected call, live value = fixed behavior" is the normal
shape — zero divergence is the special case guarding faithful reproduction.

### Workspace & Onboarding

**Agent home** (`get_workspace_path()`, `raven/config/paths.py`):
The per-agent filesystem tree (default `~/.raven/workspace`) holding the agent's and user's
memory (`MemoryStore`'s `user_memory/`), skills, session transcripts and their metadata
directories (`SessionManager`'s `sessions/<group>/`), the Skill Hub cache (`skills/hub/`), and the Workspace Template seed. Seeded by
`sync_workspace_templates()`. Exactly one per agent, never per session. Set by `--home` or
`agents.defaults.workspace`.
_Avoid_: "workspace" unqualified — this term used to cover both agent-wide and per-session
storage; it now names only the agent-wide tree, so an unqualified "workspace" should be
Agent home or Session workspace, whichever is meant.

**Product state root** (`raven/config/product_render.py:product_state_root`):
Where a product served over ACP keeps its WORK -- repos, instance buckets, flow stores,
rendered configs -- never in the product's own folder: default
`<raven home>/workspace/subagent_sessions/<product>`, overridden by the product's own
state-root variable (the `raven-` prefix drops, dashes become underscores, the rest
upper-cases: `raven-code` answers to `CODE_STATE_ROOT`). The engine's own Agent home is
deliberately NOT here -- it goes through the Product ACP home (below). Work where the
work is, the home in the data directory.

**Product ACP home** (`raven/config/product_render.py:product_acp_home`):
The engine's own Agent home for a product served over ACP -- never inside the host's
Agent home. The host hands a session's working directory to whatever it dispatches to,
and a raven engine refuses a working directory that CONTAINS its own home (the per-turn
checkpoint runs `add -A` over the working directory and would commit its config and
provider tokens into a shadow repository) -- so homing an engine under the host Agent
home made every dispatch fail while capability probing still passed. Default
`<raven home>/subagent_sessions/<product>/acp`, checked against the CONFIGURED host
Agent home (`agents.defaults.workspace`); when the default lands inside it, the engine
is homed beside the host home instead, tagged per instance. A placement must also be
creatable, and a refusal names the product's `*_ACP_HOME` override variable, which wins
outright. The Product state root is untouched by all of this.

**Subagent history** (`raven/agent/subagent/history.py`):
The per-session audit trail of every delegation to a Subagent, inside that session's
metadata directory at
`<agent home>/sessions/<group>/<chat_id>/subagents/`, holding `mas_dag/<run_id>/` (one per
`run_subagent_dag` run, holding only that run's own `graph.json` and `manifest.json`) and
`nodes/` (every node's own artifacts, from either delegation surface, flat and keyed by node
id alone rather than by the run or call that produced them: `<node>.prompt.md`,
`<node>.out.md`, `<node>.error.md`, `<node>.meta.json`, `<node>.memory.json`,
`<node>.transcript.jsonl`, plus `.attempt-<n>` variants of the prompt, output, and
transcript archiving a continued node's earlier tries). A spawn writes there too and owns no
directory of its own; only Direct Chat still keeps one per call. Both spawn and
DAG record what `SubagentBackend.run()` returned, so both are already truncated to the
sub-agent's `max_output_chars` — not raw stdout. Failed and cancelled calls are recorded
too. Lives beside the session transcript rather than in the Session workspace: it has the
transcript's lifetime, while a working directory can be repointed at any project on disk.
`sessions/` is a protected subtree, so no working directory can be aimed at it and no tool
write can reach the history. Append-only: no expiry, no size cap, reclaimed only by
deleting the session.
DAG runs dominate its volume — a node's rendered `prompt.md` inlines each dependency's full
output, so a chain stores the same text once per hop.
`nodes/`'s flat, id-keyed naming is what makes a **Node id** addressable (below): a
node's files resolve straight from its id, with no run lookup in between. `nodes.json`
(sibling to `mas_dag/` and `nodes/`) is the node registry that keeps ids unique instead --
it carries every claimed id, whether a run or a spawn claimed it (`kind`), the outcome once
it ends, and whether an output file was actually written (`has_output`, which a `completed`
status does not promise). No reader locating a node's own files consults it; what it answers
is whether an id is taken and whether it may be read. Reads and writes are serialized per
root within a process (`_store.index_guard`); two processes sharing one session still race.
_Avoid_: `.ravenx_dag/` — the previous location, a naming residue from the RavenX port; it
sat in whatever directory the run happened to use and had no `spawn` counterpart.
_Avoid_: `spawn/<call_id>/` — the tree a spawn owned before it joined this namespace, and
`call_id`, the minted stamp that named it. A spawn's id is the model's `node_id` now, and
the wire field still spelled `call_id` carries it.

**Memory record** (`raven/agent/subagent_memory.py`):
What one Subagent wrote into everos during one call, written beside that call's own
`prompt.md` / `out.md` inside Subagent history: `<node>.memory.json` for anything with a node
id, which is every `spawn` call and every DAG node, and `memory.json` for a Direct Chat turn,
which still owns a directory. Holds the sub-agent's name, a
`status`, its `instance` handle when the call had one, and a list of `{type, text}` items -- `episode` (its `subject` and `episode`) and
`agent_case` (its `task_intent`, `approach` and `key_insight`), joined with ` - ` and
uncapped. Never everos's `summary`, which is a 200-character prefix of `episode` cut
mid-word; it is the fallback only when `episode` is absent. Its reader is another
sub-agent asking what this one did, so it carries text and nothing else; identity, session
id and item ids are logged, not recorded. `status` distinguishes three outcomes: `settled`
(everos returned memories and the result stopped growing), `pending` (nothing was found
within the poll budget, which may mean the sub-agent wrote nothing or that extraction had
not finished), `unavailable` (everos could not be read, or -- for a `trace`-sourced record --
the conversation could not be written to it in the first place).

`source` says where the memories came from: `agent` for a sub-agent that runs
everos and wrote them itself, `trace` for one that does not, whose conversation
the host handed to everos to extract from. A reader weighing a record should
know which it is holding.

Produced only for an agent whose
config declares an everos identity; the
join key is `<sessionPrefix><instance agent id>`, the id the host mints and the fork
passes on to its own Raven. A `trace`-sourced record keys on `trace:<agent>:<call_id>`
instead (`trace_session_id`), its own namespace, not a session-prefixed instance id.
For an `agent`-sourced record, attribution is per *instance*, not per call: a
memory everos extracts late can appear in two consecutive records. A `trace`-sourced
record does not share this ambiguity -- its key is already per-call by construction. Unlike the Handoff Block below, this
file is NOT raven-minted: its `text` is an everos LLM summary of the sub-agent's own output,
and the spec names another sub-agent as its eventual reader. Nothing reads it today, so a
channel that carries its `text` does not exist, but whichever one is built must run that text
through `wrap_untrusted` (`raven/security/trust.py`) before it reaches another sub-agent's
prompt, the same as any other sub-agent-controlled content. Passing a path, as the DAG block
below does, does not need wrapping: the path is raven-minted, only the file's contents are not.
A DAG node is told where its upstream records are: every node whose sub-agent can open
local paths gets an `## Upstream memory records` block appended to its rendered prompt,
listing each transitive upstream's node id and the absolute path of its record. Paths only --
nothing injects a record's text. Suppressed entirely for a sub-agent the roster tags
[no-local-files], gated the same as a `_path` placeholder (Prompt template, below). The
listed files usually do not exist yet when the node starts, because the record is written by a
fire-and-forget poller after the upstream finished while the runner starts the next wave
immediately; the block therefore tells the node to proceed without a missing or `pending`
record rather than wait for it.
_Avoid_: "memory trace" -- an earlier name for the recorder, from a draft where it also
captured the call's time window.

**Prompt template** (`raven/agent/subagent/prompt_placeholders.py`):
The `{{ ... }}` grammar a dispatched sub-agent's prompt may carry, shared by `spawn` and
`run_subagent_dag`. Six shapes: `inputs.<k>` / `inputs.<k>.path` read a per-call input as
text or as a path, `<node>.output` / `<node>.output_path` read another node's result the
same two ways, and `ref:<path>` / `ref_path:<path>` do it for an arbitrary file -- the bare
form always injects content, the `_path` form always injects a location. `output` and
`output_path` name a node by the id it ran under, and both surfaces resolve them against the
same flat node root, so a spawn may name a graph's node and a graph may name a spawn's. The
id must already be readable: a node that failed, was skipped, was cancelled or is still
running is refused with which of those it was, rather than handed the leftover file a bare
path would resolve to. A `{{ ... }}` body
matching none of the six is not a mistyped placeholder -- it is ordinary text, carried
through untouched, so template syntax from another system (Jinja, Vue, Handlebars) can sit
in a prompt with no escape form needed. A body that does match a shape still fails
downstream on a bad key, path, or node id, so a typo inside a placeholder is still caught.
A `_path` shape aimed at a sub-agent the roster tags [no-local-files] is refused before
dispatch -- the content forms still work, since those hand over text rather than a location
a remote backend cannot open -- by one gate shared across both surfaces
(`check_path_placeholders`, `raven/agent/subagent/prompt_capabilities.py`), which takes
already-parsed placeholders rather than a raw template, so a grammar error is the parse
step's own to raise and never something this gate catches and re-labels as a capability
refusal.

**Reference roots** (`check_confined`, `raven/agent/subagent/prompt_paths.py`):
The directories a content or path reference may resolve into: the session's working
directory, and its Subagent history above (`<session_dir>/subagents/`), so a later call in
the same conversation can name an earlier spawn's own output file, or a DAG run's own
files, by path. A relative reference resolves against the working directory; either root
may also be named absolute. `@nodes/<node_id>...` is a third, narrower address -- this
session's own node artifacts alone, checked lexically against that one prefix rather than
against these roots -- reaching a node's own prompt file too, which the short
`{{ <node_id>.output }}` form cannot.
Stops at Subagent history rather than at Agent home on purpose. Agent home also holds user
memory, installed skills, and every *other* conversation's transcript and sub-agent history,
already off limits to a working directory (`workdir.py`'s `_PROTECTED_SUBTREES`) -- and a
template is LLM-authored and auto-run, so a root spanning Agent home would let a `ref`
(exempt from the capability gate above, since it hands over content rather than a path a
remote backend would have to open) inline another conversation's history, or the user's own
memory, into a sub-agent's prompt. Containment is decided on where the reference lands on
disk rather than on how it is spelled: both the resolved path and each root go through
`realpath`, so a symlink inside a root that points outside every one of them is refused
rather than followed, and a root reached through a symlink still contains its own files. The
error names the path the author wrote, never the physical one, which could describe a
directory they were not entitled to learn about.

**The fence rule** (`raven/agent/subagent/prompt_render.py`, `dag_render.py`):
What a resolved reference's content gets on the way into a prompt, decided by the kind of
reference rather than by where the file turned out to sit. A content-form file reference --
`ref`, or a file-shaped `inputs.<k>` entry -- is wrapped with `wrap_untrusted`
(`source="file"`, `raven/security/trust.py`). Another node's output -- `output`, or a
node-shaped `inputs.<k>` entry, both `run_subagent_dag` only -- is wrapped with
`source="subagent"`. Neither is the dispatching model's own words: a file may hold whatever
a run fetched or a checkout brought in, and a node's output is sub-agent-authored by
construction. A literal `inputs.<k>` string is not fenced, because the author typed it into
the call, and neither is any `_path` form or the `## Upstream memory records` block -- those
carry a raven-minted path rather than content.
This rule replaces one that keyed on location: a read whose resolved path landed under
Subagent history was fenced, and every other read was not. That left the contents of a file
in the working directory reaching a prompt bare, which is the wrong way round -- a checkout
someone put in the working directory is precisely where text that must not be read as
instructions arrives. What stays location-keyed is confinement, not fencing: Reference roots
decides which directories a reference may resolve into at all, and does so on the resolved
physical path (Reference roots, above), so the two questions -- may this be read, and what
does its content get -- are answered independently and neither leans on the other.

**Handoff Block** (`raven/agent/subagent/direct_chat.py`):
The pointer block the runtime prepends to the user's next turn to the main agent after
direct-chat activity: per instance, a UTC time span and the paths of each turn's
`prompt.md` and `out.md`, inside `subagents/direct/<agent>/<handle>/<call_id>/` beside the
Subagent history. Activity, not only chats - a User-Created Instance is reported in its own
right, and one with no turns yet names no path, because the record directories are made per
turn. Carries no transcript text. Accumulated per session by `DirectChatHandoff`
and taken-and-cleared on the next turn that has no `direct_target`, so a segment is reported
exactly once. Every byte in it is raven-minted - agent names from config, handles from the
registry (minted, never typed), call ids from `make_call_id` - which is why it is prepended
unwrapped; a field echoing a sub-agent's own reply would break that.
_Avoid_: "handoff summary" - it is deliberately not a summary; nothing in it is generated.

**User-Created Instance** (`SubagentManager.create_instance`):
A sub-agent instance the user started by hand rather than one the main agent produced by
delegating. `subagents.instance.create` mints its handle and writes one registry row with
status `idle`; nothing else exists until its first turn. Only an enabled, stateful agent can
have one - the same two refusals `chat` makes, since a direct chat is a continuation. `idle`
is deliberately not among the statuses `reconcile_instance_rows` rewrites: unlike an
unfinished `running`, it stays true across a gateway restart.
_Avoid_: "empty instance" - it is addressable and resumable from the moment it exists; what
it lacks is turns, not capability.

**Reply Streaming** (`raven/agent/subagent/backends/base.py`):
Whether a Subagent backend hands its reply over as it forms - `SubagentBackend.streams`
plus the `on_delta` callback - rather than only returning it whole. Read from the mechanism
that would have to deliver it, never declared: raven-loop and openai always stream, acp
forwards the `agent_message_chunk` updates it already receives, and a cli agent streams only
if its configured command asks its CLI for partial output - `claude` under
`--include-partial-messages` (on the resume template too), while `codex exec --json` has no
partial event to ask for. Asked for by a Direct Chat alone; a spawn takes the whole reply and keeps
`chat_with_retry`'s retry ladder, which streaming trades away (a stream that already
rendered cannot be retried without duplicating itself). What streams is the same text the
record stores, so a caller that rendered the deltas must not deliver the return value again.
_Avoid_: conflating it with the roster's `live-progress` tag, which says a transport reports
its *intermediate work* (acp only) and is advertised to the model. Reply streaming is
invisible to the model and is about the answer itself.

**ACP Shim** (`raven/agent/subagent/presets.py:SHIM_LAUNCHED_PRESETS`):
A dedicated ACP adapter package whose agent lives somewhere else - `pi-acp` driving a local
`pi`, `@agentclientprotocol/codex-acp` driving `codex`. The one thing a preset is allowed to
fetch: its command is a pinned `npx` / `uvx` one, because nobody installs a shim on purpose,
it carries no credential of its own, and there is no local build to defer to - while an
unpinned `npx -y` would silently change which shim build a user runs. The **agent itself** is
never fetched; its preset names the bare executable (`opencode acp`, `hermes acp`), so the
build that answers is the one the user installed, at the version they chose, holding the login
they already granted. `SHIM_LAUNCHED_PRESETS` declares that split per preset and a test holds
every acp command to it.
_Avoid_: inferring it from the command shape. An agent whose own CLI happens to ship on npm
(`opencode-ai`) is not a shim, and fetching it would run a second copy beside the user's
install and report the agent as present on a machine that does not have it - `_probe_acp`
resolves `argv[0]`, and `npx` always resolves.

**Capability Snapshot** (`raven/acp_client/capabilities.py`):
What one ACP agent reported at its last handshake - protocol version, whether it can
resume / fork / load a session, whether it takes a Steer (`canSteer`, from
`agentCapabilities._meta`), its models and auth methods - recorded by a Test and read
back as the source of a Subagent's statefulness. Keyed by agent name and stamped with a
fingerprint of the fields that decide how it launches (`command`, `cwd`, `env`,
`readyTimeoutMs`; deliberately not `name` / `enabled`, which change nothing about what an
agent can do). A snapshot whose fingerprint no longer matches is **stale**, not absent: its
*capabilities* are still used, because dropping them defaults the agent to stateless - which
costs it resume, its standing Live Agents Strip row, and its place among the targets the spawn schema's
`instance` parameter accepts (the parameter itself is always offered, since the default
sub-agent is resumable whatever the roster holds) - while
its *verdict* is not, because a green light for a command that has since been edited is a
claim no measurement backs. The `/subagents` row for a stale entry asks for a test.
_Avoid_: reading it as a liveness check - it is one measurement, taken at Test time, not a
statement about the agent right now.

**Unattended Approval** (`raven/acp_client/permissions.py`):
How raven answers an ACP Subagent's `session/request_permission`: it approves, choosing
from the options the agent offered by their protocol `kind` (`allow_always`, then
`allow_once`) and never by `optionId`, which is the agent's own vocabulary. There is no
third answer - a dispatch has no operator and no surface that could render a prompt - and
*not* answering is not one either: measured on `codex-acp`, any error to this request,
including the `method not found` raven used to send, cancels the whole turn. Presets that
take a launch-time never-ask setting carry it too, so the question is not asked at all.
The same trust boundary the cli transport already ran under (`codex -a never`,
`claude --permission-mode auto`), stated in one place instead of per command template.
Distinct from what raven still refuses: `fs/read_text_file` and its siblings are declared
unsupported in `CLIENT_CAPABILITIES`, and a handler returning `UNHANDLED` is how they stay
that way.

**Steer** (`raven/acp_client/protocol.py`, `raven/agent/subagent/activity.py`,
`raven/agent/subagent/manager.py`):
A person's words merged into a Subagent's turn while it is still running, read by the agent
before its next model call, as opposed to a prompt that opens a turn. ACP 1.20.0 has no such
method - a second `session/prompt` on a busy session is refused - so it is raven's own
extension: the agent serves `_raven/session/steer` (`STEER_METHOD`) and announces it in
`agentCapabilities._meta` under `raven.steer` (`STEER_CAPABILITY`); a client that did not
read the declaration must not call it. The backend publishes the way to steer a run
(`RunActivity.steer`, via `offer_steer`) for exactly the span of its prompt and withdraws it
after, so the hook is a fact about the run, not the agent. `SubagentManager.steer_instance`
and `subagents.instance.steer` answer one of three statuses rather than raising:
`injected` (merged; the run reads it before its next step), `no_turn` (nothing is running,
nothing was started, the caller keeps the text and may send it as a turn), `unsupported`
(the run's transport cannot take text mid-turn - a cli agent, or an acp agent without the
extension). The agent announces the merged words back as a `user_message_chunk`, which the
record keeps as a user row marked `steer`, in the position they were said.
_Avoid_: "inject" for the whole feature - `injected` is one status of a steer, and the spine's
`BusyPolicy.INJECT` is a different thing (a turn queued behind the running one);
"interrupt" - a steer does not stop the turn.

**Unprompted Turn** (`raven/acp_client/unprompted.py`):
A turn an ACP Subagent ran with nobody having asked - an on-call agent waking on its own
schedule is the case it exists for. Every other sink on the session router is attached for
one run and detached at its end, so these frames used to be dropped as a late usage report;
a resident recorder now takes what no run claims, streams it to the pane as the same
`message.start` / `token.delta` / `message.complete` a typed direct-chat turn produces
(the client cannot tell the two apart, which is the point), and logs it under its own
`kind` beside `spawn` and `dag`, with a stated-fact `user` row for the fold boundary the
wake's real message never reaches this process to provide. The turn's end is `usage_update`
when the agent reports one and silence otherwise - `message.complete` produces no wire
frame without usage, so a fixed quiet period (sized over the longest healthy tool-call gap)
is the fallback ending.
_Avoid_: "background turn" - nothing about it is backgrounded; it runs and streams like any
other turn, and only its *origin* differs.

**Elicitation Pass-Through** (`raven/acp_client/elicitor.py`):
How an ACP Subagent's `elicitation/create` reaches the user. Form mode only, and advertised
as only that: `url` elicitation is for out-of-band credential and payment collection, so
advertising it would let a sub-agent send the reader to an address of its own choosing. The
requested schema is decomposed into one question per property, and each one **Question
Autofill** leaves for the user is put through the same `clarify.request` contract as
`ask_user` - so a surface that already answers a question needs nothing new - and the answers
are reassembled into one `accept`; a required property nobody
answered declines the whole form rather than handing back content its schema rejects. Routed
by `sessionId` to the run that asked and answered off the connection's read loop, so one
pending question does not stall the other sessions of a pooled connection. A form with anything left to ask
holds a per-conversation lock for the whole of it, because the question broker allows one
pending question per conversation and fail-safes an overlapping one to its default - which here would read as a
skip nobody ever saw. The lifetime is the backend's, since a sub-agent asks after the turn
that spawned it has replied: `clarify.closed` retracts a question that can no longer be
answered, a run that ends cancels the elicitor it attached, and `$/cancel_request` from the
sub-agent retracts the one request it names - the only signal there is that the run behind a
question has stopped listening, since a sub-agent that gives up says nothing else.
_Avoid_: reading it as the same kind of thing as **Unattended Approval**. That one is
answered by raven with nobody in the loop as a matter of policy; this one reaches for somebody
by default and declines when a dispatch has no reachable user. **Question Autofill** is the
one thing that answers on this path without asking, and only from what the turn already
established.

**Ask-User Round Trip** (`raven/acp_client/ask_user.py`):
How an ACP Subagent's question reaches the user when it does not use `elicitation/create`.
Raven-X routes its deep-research clarify through an extension of its own: the question
leaves as a `session/update` whose `sessionUpdate` is `ask_user_request`, and the answer
goes back as a `_raven/clarify_respond` request of raven's own. Armed by declaration on both
sides - the agent sends nothing unless the client declared `_meta.raven.askUser` at
`initialize`, and unarmed its tool falls back to ending the turn on the questions, so this is
the difference between a clarify that interrupts one turn and one that costs a whole round
trip through the caller. A question **Question Autofill** does not answer
ends at the same `clarify.request` contract as **Elicitation Pass-Through** and takes its
per-conversation lock, because both reach one question broker that allows a single pending
question per conversation; one that is answered takes neither. Answered off the connection's read
loop for a sharper reason than the request path's: notifications are dispatched inline there,
so a question awaited on that loop stalls every other session of a pooled connection. Every
question a run owns is answered, including the ones nobody can put to a user - a background
turn and a finished run both reply with an empty string, which the asking side already reads
as "the user did not answer". A session nobody owns is the one case that is routed and not
answered - raven cannot answer for a run that is gone, and the agent falls back to its own
timeout. The window is a frame landing after the backend's `finally` has detached the
responder.
_Avoid_: reading a dropped frame as a no-op. The agent blocks its tool call on the reply for
ten minutes before falling back to the question's default, so not answering is the stall this
exists to prevent, not an abstention.

**Question Autofill** (`raven/acp_client/autofill.py`, `raven/acp_client/resolver.py`):
The step in which raven answers a Subagent's question from the turn's own context instead
of putting it to the user. It sits in front of both question routes -- **Elicitation
Pass-Through** and **Ask-User Round Trip** -- and decides per form rather than per
question, in one model call that continues the turn that spawned the sub-agent: the live
message list holds both what the user said and the spawn call's own arguments, plus one
recall keyed on the questions rather than on the user's message. Each question comes back
`answer`, `partial` or `defer`. Only `answer` skips the user; a `partial` is still asked,
carrying what raven does know appended to the sub-agent's own wording; and a form answered
in full never takes the per-conversation question lock, so a form nobody has to see cannot
park another agent's question behind it. Every failure defers -- the switch off
(`subagentQuestions.autofillEnabled`), no provider, the call past its budget, an answer
outside the offered options, an answer the schema cannot hold -- and a question asking to
authorise an action (pushing, deleting, sending, paying) is instructed back as `partial`
however plainly the context supports it, because authorising is the user's to do. The step
renders as a synthetic `answer_for_user` tool call, deliberately absent from the **Tool
Registry** so the model has no interface for claiming it, and is written into the
conversation at the loop's `drain` seam -- the one point where the turn's own task owns
the message list with every tool result already in it.
_Avoid_: reading it as a *default* for a question. The broker's `default` is what its
fail-safe paths return (timeout, cancellation, an undeliverable question, connection EOF),
and autofill never sets one, so a question it deferred and nobody answered is the same
empty skip it always was.

**Frame Journal** (`raven/acp_client/journal.py`):
Every frame of one ACP connection, both directions, in wire order, on disk. Distinct from
the run transcript, which holds the `session/update` notifications routed to one session -
the reading of a delegated run, and not everything that crossed the wire. Four classes of
traffic exist only here: the agent's own requests and what raven answered (so an Unattended
Approval is recorded rather than only logged), raven's outbound frames, a notification no
session was listening for, and stderr. Per connection rather than per call because an ACP
session is - one process serves every session of one agent, and the `initialize` handshake
belongs to no single call. Since ACP has nowhere to carry raven's own identity, the
dispatcher writes an `acp_call` record naming the agent, instance, task and conversation the
session it just opened belongs to; without it the file could only be read by joining its
session ids against spans or every `meta.json` on the host, and a stateless agent registers
no instance row for that join to land on. Bounded by a stated ceiling per connection and a
retention window, and reaching the ceiling is written into the file rather than left to look
like a connection that went quiet. Mode `0600`, because the frames carry the whole prompt
and every tool result.
_Avoid_: calling it a transcript - a reader drawing a delegated run wants the Instance Log
or `transcript.jsonl`, not this.

**Instance Log** (`raven/agent/subagent/instance_log.py`):
One sub-agent instance's own conversation, for the whole conversation that owns it, at
`<session_dir>/subagents/instances/<agent>/<handle>.jsonl`. A call record answers "what was
this one dispatch"; an instance outlives it - a stateful agent resumed under one handle
spans many calls, and those calls arrive through three lanes (`spawn`, a DAG node, a Direct
Chat) that each write a different directory shape, so an instance's conversation was only
readable by stitching all three together in the right order, which nothing did. Written in
the *same format as the session log* at `sessions/<group>/<chat_id>.jsonl` - a
`_type: "metadata"` header, then untagged message rows - so anything that reads a raven
conversation reads this, and a Direct Chat draws a turn's thought and tool calls with the
renderer it already has. The wire frames behind those turns are deliberately not copied
here: the Frame Journal already holds them in full, a per-instance copy was measured to
carry no record the journal did not (47 against 47, for 78x the transcript's bytes), and a
call's record still names the journal and the byte range it occupied.
_Avoid_: reading it as the wire log - that is the Frame Journal.

**Turn Rows** (`raven/agent/subagent/backends/turn_rows.py`):
The provider-shaped message rows one delegated turn contributes to the Instance Log, built
from a transport-neutral event list (`say` / `thought` / `call` / `result`). Both the ACP
collector and the OpenAI Step Dialect produce that list, which is what makes an `openai`
instance's conversation read identically to an `acp` one - two implementations of one shape
would diverge at the first fix applied to only one. A call row wears whatever thought preceded
it and opens with that thought's clock, so a renderer can fold a finished stretch with a real
duration. The final answer is not among them: the record keeps it and the reader appends it as
the Closing Message.
_Avoid_: confusing them with **Live rows** - the same shape from a different source, and only
the latter is a snapshot.

**Step Dialect** (`raven/agent/subagent/openai_steps.py`):
How one OpenAI-compatible endpoint's `reasoning_steps` extension is read into Turn Rows events
- the step's own tool name, its own argument keys, and its result with the transport's wrapping
removed (`fetch_url_content` nests its result as a JSON string, and a decoded failure there is
what makes the row not-ok). Sibling to **ACP Dialect**, for a transport that reports its steps
in a response field instead of a notification. Buffered and streamed responses differ in shape
- a streamed `thinking` step arrives as token fragments, measured at 106 frames for 4 thoughts
- and one accumulator serves both, which is what keeps a live view and a settled record in
agreement.
_Avoid_: reading a step type as a raven tool name - it is the endpoint's, and **Tool
Vocabulary** maps it.

**ACP Dialect** (`raven/acp_client/acp_dialects/`):
How one ACP adapter's tool-call frames are read into a record: the adapter's own name for the
tool at the finest grain the transport gives - `_meta.claudeCode.toolName` where the adapter
sends one, the spec's `kind` otherwise - the subject to show beside it, and output with the
transport's wrapping removed. The name is stored as sent, not translated; mapping it into
raven's own vocabulary is the Tool Vocabulary's job, on the way to a client. Needed because
an adapter reports a call twice over -
machine-readably in the spec's `kind` and `locations`, and for a human in `title` - and only
the first is comparable across adapters, since the same `kind: "execute"` arrives titled
`Terminal` from claude-agent-acp and titled with the whole shell pipeline from codex-acp.
Naming the tool honestly is what lets a Direct Chat draw a delegated turn with the
transcript's own renderer, which reads a tool name to choose a verb, and lets a later reader
still tell which tool actually ran. Selected from `agentInfo.name` in the
connection's own `initialize` result rather than from config, so a renamed agent and two
entries pointing at one adapter both resolve. An adapter with no file of its own gets the
spec-only base class, which reads nothing the protocol does not require - so an unmeasured
adapter works without one. Result unwrapping is the genuinely per-adapter part:
claude-agent-acp sends its output twice, plain in `rawOutput` and markdown-fenced in
`content`, while codex-acp sends no `content` at all and reports a failed command only
through `exit_code` inside `rawOutput`.
_Avoid_: reading `title` as the tool name - it is a label, and for one adapter it is the
entire command.

**Dialect discriminator** -- the field on an ACP frame that identifies which of
one adapter's tools a call is, when the spec's `kind` cannot. codex-acp sends
five `kind` values for eleven tools, and separates them with `rawInput.type`,
`_meta.is_mcp_tool_call`, `_meta.codex.collaboration`, `_meta.codex.subagent`
and `_meta.contextCompaction`. Read by `acp_dialects/codex.py`.

**Subject back-fill** -- setting a tool call's subject from a frame later than
the one that opened it. Three frames can supply one: a `tool_call_update`
revising `rawInput` (`_revise_call`), a completed result carrying the subject in
its output (`_backfill_subject`, used by codex's `apply_patch`), and a
`session/request_permission` carrying the command a re-badged call really ran.

**Tool Vocabulary** (`raven/agent/subagent/tool_vocabulary.py`):
Raven's own tool names (`exec`, `read_file`, ...), and the mapping into them applied when a
delegated run's rows go on the wire. A record carries the transport's name because
presentation is recoverable from provenance and provenance is not recoverable from
presentation, and the record is what a memory extractor reads; the wire carries raven's for
the ACP spec's `kind` entries, but deliberately not for claude-agent-acp's twelve -- those
keep Claude Code's own vocabulary, and the main session log stores the host's own calls
under raven's names regardless, since those were never anything else. The same pass re-keys
a call's subject onto that tool's own argument name, trying the tool's key first, then the
keys adapters are known to use, then any string the payload carries. Applied at the three
reads that serve a delegated transcript - an instance's history, a dag node's messages, a
sub-agent's context - and deliberately not inside `_map_to_wire`, which also serves the
session log whose calls are already raven-named. A name with no entry is passed through,
which is how an openai step type reaches a client under its own name, and now every
claude-agent-acp name does too; a renderer answers with one of three verb tables
(`OVERRIDES`, `CODEX_VERBS`, `CLAUDE_VERBS`, unified by `ruleFor`) rather than one
vocabulary keyed the same way throughout.
_Avoid_: applying it at write time - that is what this replaced.

**Closing Message** (`raven/acp_client/acp_agent.py`, `activity.py`):
What a delegated run said *after its last tool call*, as distinct from its whole reply. An
ACP turn may narrate as it works - measured on codex-acp: a plan, then a progress note
before each of three calls, then the report - and the run's returned answer joins all of it,
which is right for the caller receiving it and wrong for a transcript, where each note
belongs on the step it preceded. So the Instance Log carries narration on the calling rows
and closes with this. `""` (the turn ended on a step and said nothing after) is deliberately
different from `None` (this lane cannot tell the two apart), which falls back to the whole
output.
_Avoid_: calling it the answer - the answer is what the run returns, and for a narrating
agent the two differ.

**Response Meta** (`raven/acp/methods.py`, `raven/acp_client/acp_agent.py`,
`raven/agent/subagent/activity.py`):
What an ACP agent attaches to its `session/prompt` response under `_meta`, the field the
schema reserves for an agent's own metadata, kept on the run record as `acp_response_meta`
verbatim and namespaced as sent. Raven serving the agent side fills it from the turn's
observer stash: whatever a hook filed under `metadata["observers"]["acp_meta"]` is read
back off the turn's last substantive assistant message once the turn has landed, so the
loop_hooks paper's filing is the only seam a plugin needs - no plugin touches the wire, and
a turn that filed nothing answers with its stop reason alone. Raven driving the agent side
reads none of it: the table reaches the spawn record's `meta.json` and the DAG run's
per-node entry through the same channel as the frame pointer, and a product's report
arrives without the host knowing the product.
_Avoid_: reading it in the host to make a decision - it is a product's record, not a
protocol the orchestration acts on; "manifest" for the channel - the DAG run already has a
`manifest.json` of its own, and this is any agent's table, not one product's.

**Harness Manifest** (`agents/raven-code/plugins/code-flow/code_flow/manifest.py`):
Raven-Code's workspace report, filed at the send of every prompt as the Response Meta entry
`raven.harnessManifest`: what the working directory shows since this session began,
machine-read from git against the base commit the session ledger pinned at the session's
first turn - root, branch, head, the commits past base, the working tree, a diff stat, and a
status (`no_changes`, `needs_commit`, `ready_for_integration`, `shared_workspace`, or
`unknown` with the blocker named) that is ready only when every fact answered. Its scope is
the workspace (`scope: workspace`): HEAD is shared by every session in the directory, so the
facts are the session's own (`attribution: session`) only while no other session of the
process has shared the directory during its life, as the ledger's peers record; once one
has, `attribution` is `shared`, `sharedWith` counts them, the status is `shared_workspace`
and the report is never ready - a commit past base may be another session's. A directory
outside git is reported as `plain`. Nothing comes from model prose, and no allocation record
is needed: the write gate and its worktree isolation retired, and with them the manifest's
old ride inside the reply text between sentinel lines.
_Avoid_: reading `readyForIntegration` from a manifest whose status is `unknown` - it is
false there by construction, never a fact about the tree.
_Avoid_: reading a `shared` report as one session's work - it is the shared tree's, and
`baseCommit..HEAD` cannot tell whose.

**Concurrency Notice** (`agents/raven-code/plugins/code-flow/code_flow/flow.py`,
`code_flow/sessions.py`):
The paragraph the code-flow hook puts in front of a Raven-Code turn's prompt when other
sessions of the same process are mid-turn in the same working directory - the parallel
nodes of one DAG, which the host serves through one connection and one session each - and
only then; a lone session hears nothing. It replaces the lock: re-read before every write,
touch only the task's own files, stop and report on a file that changed underneath. The
count comes from the process's own session ledger (in flight from inbound to send, refreshed
at every iteration so a long turn stays counted, a mark a failed turn left behind expiring),
not from the host, which has no per-node place to say it.
_Avoid_: calling it isolation - nothing prevents two sessions from writing one file; it is
the cooperation contract that stands where the worktree used to.

**Live rows** (`raven/rpc/methods/instances.py`, `raven/agent/subagent/activity.py`):
The rows `subagents.instance.history` returns for a turn that is *still running*, marked
`live: true` on the wire. They come from the activity the runtime is collecting, not from any
file: the Instance Log is written when the turn lands, so until then the steps exist nowhere
else. They carry the *whole* turn - its prompt, its steps and the answer text so far - so a client
rebuilds the in-flight turn from one read, which it must: a `spawn` or a DAG node is a turn of
this instance that the client never sent and so has no row of its own to anchor on, and for
those two lanes this read is the only thing that carries any of it (the wire tags an instance
on the four events of a *direct* turn and nothing else). Addressed by
`(session_key, agent, handle)` through a second live index, because the first one is keyed by
the record's directory - a task id no reader of a *conversation* ever sees.
_Avoid_: reading the absence of live rows as "the turn ended" - a transport with no per-step
visibility reports none for the whole of every turn.

**Stop Reason** (`raven/acp_client/acp_agent.py`):
What an ACP agent reports at the end of a turn. Only `end_turn` means it finished; every
other value (`cancelled`, `max_tokens`, `refusal`, ...) leaves a reply that reads complete
and is not. Such a reply is kept and carries an appended `[raven]` notice naming the stop
reason, budgeted before the reply is clamped to `maxOutputChars` so the notice cannot be
the part that is cut. Kept rather than raised because a partial answer is worth having;
noticed rather than returned bare because neither the main agent nor a person in a Direct
Chat can otherwise tell the text simply stops.

**Node id** (`raven/agent/subagent/dag_graph.py`, `dag_store.py`):
The model's own name for one delegated task, and the address a *later* task in the same
conversation uses to read what it produced — `{{ <id>.output }}`, needing no `depends_on`,
since the task has already finished (naming it there is allowed and orders nothing). Both
delegation surfaces draw from one namespace: a `spawn` chooses its `node_id` the same way a
`run_subagent_dag` node declares its `id`, so either may reference the other. That second
role is why the id is
**unique per conversation, not per graph**: reusing one an earlier run or spawn took is
refused, so an id names one task and one output. Compared case-folded, because a node's
artifacts are files named after it and `Plan` and `plan` are one file on macOS and Windows.
An id is claimed at dispatch and kept whatever the outcome, but referencing it needs both a
`completed` status and an output file actually written — a task can finish having written
nothing, which the status alone does not say. A failed, skipped, cancelled or still-running
one — or, as a replan validating its replacement sees it, one still pending or awaiting a
decision — keeps its id and is refused with which of those it is. A run stopped
by `/stop` or a shutdown records its still-running nodes as `cancelled` and its pending
ones as `skipped` on the way out, so "still-running" means what it says rather than
outliving the run that claimed it. Distinct from an
`instance` handle, which shares a sub-agent *session* rather than naming an output.
_Avoid_: "node name" — the id is an address, not a label. _Avoid_: "DAG node id" — the
name from when only a graph could claim one.

**verdict** -- the judgement on whether a finished DAG node accomplished the task
its prompt set. Made by one constrained model call over the node's prompt, its
output, and the tail of its transcript (`raven/agent/subagent/dag_verdict.py`). A node whose
backend returned without raising is not thereby successful; the verdict is what
decides.
_Avoid_: confusing with JudgeVerdict or Trajectory Verdict -- both name a different
judgement (a turn's completion, an Attempt's pass/fail) made by a different
subsystem; this one judges a single DAG node's output against its own prompt.

**exception** (node status) -- a DAG node that did not accomplish its task and is
waiting for the main agent to decide whether to continue, abandon, or replan it.
Reached by two routes: the backend raised, or the backend returned and the
verdict said the task was not accomplished. Non-terminal: its dependents stay
`pending` rather than cascading to `skipped`.
_Avoid_: it is not a synonym for a Python exception. A raised exception is only one
of the two routes into this status, and `status[node.id] = "exception"` sits next to
`except Exception as exc` in `_run_node` for that reason.

**replan** (adjudication decision; `raven/agent/subagent/dag_adjudication.py`) -- the answer
to an exception report that replaces the plan instead of the node: the agent hands
`resolve_dag_node` a new node list, the answered run stops where it is and finalizes, and a
new run starts from that list. Chained rather than spliced -- the successor is a separate
run with its own id and dir, and the old run's `graph.json` carries a reserved `replan` key
naming it. A completed node of the old run is reused by reference (`depends_on` plus
`{{ <id>.output }}`); no id it claimed can be re-declared, so a replan gives up on the
adjudicated node and no attempt budget crosses the boundary.
_Avoid_: "reorchestrate" -- orchestration is what the main agent does with graphs generally;
this names one decision about one graph.

**outbox** (`raven/agent/subagent/dag_adjudication.py`) -- a foreground DAG run's tray of
events: its nodes' exception reports and its final result, waiting for the tool call
that is awaiting the run. One per foreground run, in memory beside the run's
adjudication desk. The desk carries decisions from the main agent to the run; the
outbox carries reports from the run to the main agent. While the run is bound the
outbox hands or buffers a question and drops a notification -- the blocking call is
still there and the run's summary is what it will be handed, so announcing the same
news again would put an unanswerable question beside it -- and never announces; once
released it re-sends what is unanswered and announces later events as turns, both
kinds.
_Avoid_: "mailbox" -- the lane's inject mailbox is a different object with a different
reader.

**bound / released** (foreground DAG run; `raven/agent/subagent/dag_tool.py`,
`raven/agent/loop/main.py`) -- a `background: false` run is *bound* while the turn that
started it is still running, and *released* once that turn has ended, however it ended
(`AgentLoop.run_turn` releases every run the conversation's turn still binds). A bound
run's suspended nodes wait without a deadline and its outbox buffers; a released run
behaves as a backgrounded one: the adjudication window is clocked from the release, the
unanswered reports are re-sent as turns, a report the turn took but never decided
counting as unanswered (they are dropped instead when the turn was cancelled), and later
events announce -- notifications as well as questions, since no blocking call remains for
a run's summary to reach. `resolve_dag_node` blocks only on a bound run.
_Avoid_: "orphaned" -- a released run is not lost, it has changed lane.

**Working directory** (`raven/agent/workdir.py`):
The directory a turn reads and writes files in — shared by the session's leader `AgentLoop`
and every Subagent it spawns, and resolved per turn by `WorkdirResolver.resolve()`.
`raven tui` and `raven agent` use the process launch directory, so the agent works in the
checkout you started it from (Workdir policy `LAUNCH_DIR`); intermediate artifacts it
produces there go under that directory's `.raven/` (the shadow-git repo lives at
`.raven/shadow.git`). `raven gateway` gives each channel one directory (Workdir policy
`PER_CHANNEL`), set by `channels.<name>.workspace`, defaulting to `<agent home>/../tmp/<channel>`, i.e. `~/.raven/tmp/<channel>`.
Overridable per invocation via `--workspace`/`-w` (the working directory itself on
tui/agent, the root the per-channel defaults hang off on gateway), or per running gateway
session from the web UI, persisted in `Session.metadata["workdir"]` and taking effect on
the next turn. An override must be absolute, and may be neither Agent home, nor one of its
memory/skills/transcript subtrees, nor any directory containing Agent home. Each distinct
working directory grows its own shadow-git repository once a checkpoint runs there; they
are reclaimed only by deleting those directories.
_Avoid_: "session workspace" — the gateway's unit is the channel, not the conversation.
_Avoid_: confusing with Agent home — when `restrict_to_workspace` fences tools, it admits
both roots, but they stay two different directories with different lifetimes.

**Project slug** (`project_slug()`, `raven/utils/paths.py`):
A launch directory flattened into one filesystem-safe segment, following the convention
Claude Code uses for `~/.claude/projects/`: every run of non-alphanumeric characters becomes
a single `-` (per character, not per run), and past 200 characters the slug is truncated with
a base36 hash of the whole path appended. `/srv/work/my_app` is `-srv-work-my-app`. Groups a
project's sessions on `raven tui` / `raven agent`, where it is the `<group>` directory under
`sessions/`. Neither reversible nor collision-free — `/srv/a_b` and `/srv/a/b` slug the same,
as they do in the reference. The project's identity is therefore carried by
`Session.metadata["project_dir"]`, not by the directory name.

**Workdir policy** (`WorkdirPolicy`, `raven/agent/workdir.py`):
Which default a `WorkdirResolver` falls back to when a session has no explicit override:
`LAUNCH_DIR` or `PER_CHANNEL`. Fixed per entrypoint (tui/agent vs. gateway), not user-facing.

**Workspace Template** (`templates/`):
The bundled markdown seed files copied into Agent home on first run by
`sync_workspace_templates()` (idempotent — fills only missing files, so user edits win):
`SOUL.md` (agent persona), `AGENTS.md` (agent operating instructions), `USER.md` (user
profile), `HEARTBEAT.md` (periodic-task list read by the heartbeat Scheduler), `TOOLS.md`
(tool-usage notes), `memory/MEMORY.md` (legacy memory seed). On the L4 layout these map
under `agent_memory/profile/` (soul.md, agent.md) and `user_memory/profile/` (user.md);
`HEARTBEAT.md` / `TOOLS.md` stay at the Agent home root.

**Onboarding** (`raven onboard` → `run_wizard`):
The first-run wizard (LLM provider → sandbox → channel → EverOS memory → web access → sub-agents → cold-start import) that also seeds
Agent home via `sync_workspace_templates()`; gated at startup by `ensure_configured_or_onboard()`.

**Bootstrap Files**:
The identity files concatenated into every prompt — `soul.md` + `agent.md` + `TOOLS.md` —
rendered by the Context Builder / bootstrap segment.
_Avoid_: lumping `user.md` in — the user profile enters via the `# Memory` segment, not bootstrap.
