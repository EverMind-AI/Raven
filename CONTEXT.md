# Raven Runtime

> **Status: review baseline (2026-06-28).** Under team review via this PR — owners refine
> their assigned terms by branching off this PR branch and merging back.

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

**Agent Hook** (`agent/hook/`):
The turn-loop extension point: an `AgentHook` ABC with five async phases
(`before_user_inbound`, `before_iteration`, `after_iteration`, `after_send`, `on_tool_call`).
Multiple hooks chain via `CompositeHook`; the EvalEngine wires three concrete implementations.
_Avoid_: "callback" or "middleware" — neither captures the phase-specific, chain-aware semantics.

**Subagent** (`agent/subagent/`):
A background agent task spawned by `SubagentManager`. Runs with its own tool set; its result
re-enters the session as a `SUBAGENT`-origin `TurnRequest` via Spine submit. Bounded by
`max_concurrent` (default 8) and a per-session hourly rate limit, both shared with the
nodes of a DAG run — every sub-agent dispatch draws on the one allowance.
_Avoid_: conflating with a Turn — a Subagent lives outside the main turn and re-enters via Spine.

**Agent table** (`subagents.agents[]` in config → `agent/subagent/registry.py`):
The one list of agents raven can dispatch to, materialized once per process as an
`AgentRegistry` that `spawn`, `run_subagent_dag` and the playbook generator all read.
A row is a name plus a `kind` (`builtin` / `cli` / `acp` / `openai`) plus that kind's
connection fields; `AgentCaps` and `Injectable` are *derived* from it, and are what a
consumer branches on so that nothing has to switch on the transport.
Three sources compose it, weakest first: **vendored** rows discovered on the
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

**Vendored agent** (`agent/subagent/vendored_agents.py`):
An agent row discovered under `subagents/` rather than written anywhere — one of the
separate raven builds that ship beside this one, each its own checkout with its own venv
and manifest. Materialized as a `cli` row on every table build, so a folder that is
deleted stops being an agent and a manifest that changes is picked up without a stored
copy to contradict it. Readiness (its venv built, and a credential of its own or a host
provider key to inherit) decides `enabled`, not whether the row exists: an unready folder
is listed and disabled, because a name the dispatching model can pick and then fail on is
worse than no name, and hiding it would also hide "present, not set up" from the
operations view. Not deletable through config — removing one means removing its folder,
or setting `"enabled": false` in its own `subagent.json`.
_Avoid_: "third-party agent" — these are raven's own builds, and nobody registered them;
"builtin" — that is the in-process row, which has no subprocess and no venv.

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

**Tool** (`agent/tools/`):
An agent capability behind a uniform `Tool` ABC (name, parameter schema, async
`execute`). Built-ins: file read/write/edit/list, grep/find, exec, web search/fetch,
message, ask_user, spawn (Subagent), MCP, media generation, skill read/use, and the
plugin market (`plugin`).
_Avoid_: "function" — a Tool is the agent-facing capability, not a Python function.

**Tool Registry** (`agent/tools/registry.py`):
The name→`Tool` table the Agent Loop dispatches into: resolves a tool by name and runs
its `execute` under a timeout, returning the string result or a structured error.

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
`RecoveryLimits`; otherwise the turn COMPLETEs.
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
not a broadcast bus — replaces the dormant `bus/` pub/sub.
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
A platform adapter (a `BaseChannel` subclass: telegram, matrix, discord, …) that
connects an external chat platform to the Runtime; managed by the ChannelManager
in gateway mode.
_Avoid_: calling the TUI a channel — `channel="tui"` on a message is a routing tag, not a Channel

**TUI**:
The terminal front-end (`ui-tui/`) and the only interactive local front-end; talks to
the Runtime solely via the RPC protocol. Not a Channel.

**CLI**:
The one-shot command-line entry point (`raven <command>`) for operations and
configuration. Not a conversation front-end.
_Avoid_: using "CLI" for the interactive REPL (retiring)

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
The cross-cutting token-efficiency layer: a set of independently toggled
TokenStrategies, not a single module.

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
The token/cost accounting unit for a single LLM call: input / output / cache-read /
cache-write / reasoning tokens plus the estimated USD cost.

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
_Avoid_: "credential kind" for the whole shape — that names only the material.

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

**Provider Pin**:
`agents.defaults.provider`: an explicit override of the Provider a Model Ref names.
Every surface that changes the model rewrites it by one rule
(`providers/pin.py::resolve`), because a pin left behind routes the new model to the old
vendor with the old vendor's key.
_Avoid_: reading it as a provider *signal* -- a pinned name says which section to ask
about, never that the section holds credentials.

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
(Segment 6).
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
_Avoid_: summarize, compact (ambiguous between this and Archive)

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

**EverOS** (`raven/plugin/memory/everos/`):
Raven's default bundled memory-backend plugin (`everos-memory`; ships enabled, works
out of the box). Provides dual-track semantic recall — the user track (episodes/profiles,
injected into the `# Memory` segment) and the agent track (skills/cases, one of
SkillForge's three sources at RRF weight 0.9). The name refers to the external package
[EverMind-AI/EverOS](https://github.com/EverMind-AI/EverOS); the in-tree code is only an
adapter. The same plugin also contributes the `understand_media` multimodal-parsing tool.

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

**PlugHub** (`plughub/`):
The plugin marketplace: a catalogue of installable integrations (`catalog.json`), and the
transactional installer that lands one. A catalogue entry contributes pieces -- an MCP
server, credentials, a skill -- and `install` lands them all or none. Distinct from **Skill
Hub**, which is a remote marketplace for skills alone.
_Avoid_: "market" on its own for either one -- both surfaces are called that in prose, and
the RPC groups (`plughub.*` vs `skillhub.*`) are separate.

**`plugin` tool** (`agent/tools/plughub.py`):
PlugHub's agent-facing surface: `find` / `connect` / `authorize` / `list` / `remove`, over the
same `plughub/connect.py` transaction the panel's `plug.*` RPC drives, called in-process. It
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
is two such roots layered: `raven/playbook/builtin/` ships with the package and
has no write path, and `<agent_home>/playbooks/` (override: `playbooks.dir`) is
where both creation entries — `raven playbook create` and the `create_playbook`
tool — land their product, disabled for review; a user directory reusing a
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
The Memory Engine component (`MemoryConsolidator`) that performs Consolidation —
under session-token pressure it annotates evicted message chunks into Episodes,
refreshes hot Profile sections, and (opt-in) emits Foresight. The agent loop skips
it when the Curator Context Engine is active.
_Avoid_: conflating with the Curator — the Curator builds the context window
losslessly; the Consolidator is the legacy lossy path that writes long-term memory.

### Plugins

**Plugin** (`plugin/`):
A component declared by a `raven-plugin.toml` manifest (`[plugin]`: `id`, `version`, optional
`bundled` / `enabled_by_default`). It contributes capabilities via
`[[plugin.contributes.<kind>]]` arrays — currently `memory_backends` and `tools` — each naming
a `factory` (`module:callable`). The host passes the user's `plugins.config["<id>"]` dict
verbatim to the factory as `PluginContext.config`.

**Plugin Registry** (`plugin/registry.py`):
The `PluginRegistry` discovers manifests, activates those not in `plugins.disabled` (respecting
`enabled_by_default`), resolves each `module:callable` factory by dynamic import, and registers
contributions into per-kind tables — deduping plugins by `id` and contributions by `name`
(`PluginConflictError` on collision). `build_memory_backend()` / `build_tool()` construct a
contribution with a fresh `PluginContext`.

### Security & Access

**AUTH** (`auth/`):
Authentication & authorization primitives (e.g. allowlist).

**SECURITY** (`security/`):
Network access control (e.g. `network.py`).

### Execution & Evaluation

**SandBox** (`sandbox/`):
Isolated command execution (microVM / boxlite); owns the debug server and VM lifecycle.

**EvalEngine** (`eval_engine/`):
The L3 evaluation engine: task judging and cognitive coordination, implemented as three
`AgentHook` instances (`BeforeIterationHook`, `AfterIterationHook`, `ToolAuditHook`)
wired into `AgentLoop` via `CompositeHook`.

**EvalJudge** (`eval_engine/judge/`):
The single-call LLM judge behind the EvalEngine's task-completion check: it compares the
turn's original user goal against the final response and returns a JudgeVerdict. Any error
path returns `unknown`, so the judge can never crash the Agent Loop.
_Avoid_: "task judge" as a class name — the class is `EvalJudge`.

**JudgeVerdict**:
The three-state outcome an EvalJudge returns: `completed` (goal addressed), `failed`
(visible error / missed objective), or `unknown` (indeterminate). The `AfterIterationHook`
writes completed/failed (never unknown) into `HISTORY.md`.

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

**Subagent history** (`raven/agent/subagent_history.py`):
The per-session audit trail of every delegation to a Subagent, inside that session's
metadata directory at
`<agent home>/sessions/<group>/<chat_id>/subagents/`, holding `spawn/<call_id>/`
(one directory per `spawn` call: `prompt.md`, `out.md` or `error.md`, `meta.json`) and
`mas_dag/<run_id>/` (one per `run_subagent_dag` run: `graph.json`, `manifest.json`,
`<node>.prompt.md`, `<node>.out.md`, plus a per-session `mas_dag/index.json`). Both record
what `SubagentBackend.run()` returned, so both are already truncated to the sub-agent's
`max_output_chars` — not raw stdout. Failed and cancelled calls are recorded too. Lives
beside the session transcript rather than in the Session workspace: it has the transcript's
lifetime, while a working directory can be repointed at any project on disk. `sessions/` is
a protected subtree, so no working directory can be aimed at it and no tool write can reach
the history. Append-only: no expiry, no size cap, reclaimed only by deleting the session.
DAG runs dominate its volume — a node's rendered `prompt.md` inlines each dependency's full
output, so a chain stores the same text once per hop.
The `mas_dag/index.json` is not only a discovery list: it carries each run's node
ids, claimed when the run starts, and their outcome once it ends, which is what makes a
**DAG node id** addressable (below). Reads and writes of it are serialized per root
within a process (`_store.index_guard`); two processes sharing one session still race.
_Avoid_: `.ravenx_dag/` — the previous location, a naming residue from the RavenX port; it
sat in whatever directory the run happened to use and had no `spawn` counterpart.

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

**Capability Snapshot** (`raven/agent/acp/capabilities.py`):
What one ACP agent reported at its last handshake - protocol version, whether it can
resume / fork / load a session, its models and auth methods - recorded by a Test and read
back as the source of a Subagent's statefulness. Keyed by agent name and stamped with a
fingerprint of the fields that decide how it launches (`command`, `cwd`, `env`,
`readyTimeoutMs`; deliberately not `name` / `enabled`, which change nothing about what an
agent can do). A snapshot whose fingerprint no longer matches is **stale**, not absent: its
*capabilities* are still used, because dropping them defaults the agent to stateless - which
costs it resume, its Instance Chip, and its place among the targets the spawn schema's
`instance` parameter accepts (the parameter itself is always offered, since the default
sub-agent is resumable whatever the roster holds) - while
its *verdict* is not, because a green light for a command that has since been edited is a
claim no measurement backs. The `/subagents` row for a stale entry asks for a test.
_Avoid_: reading it as a liveness check - it is one measurement, taken at Test time, not a
statement about the agent right now.

**Unattended Approval** (`raven/agent/acp/permissions.py`):
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

**Frame Journal** (`raven/agent/acp/journal.py`):
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

**ACP Dialect** (`raven/agent/subagent/acp_dialects/`):
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

**Tool Vocabulary** (`raven/agent/subagent/tool_vocabulary.py`):
Raven's own tool names (`exec`, `read_file`, ...), and the mapping into them applied when a
delegated run's rows go on the wire. A record carries the transport's name because
presentation is recoverable from provenance and provenance is not recoverable from
presentation, and the record is what a memory extractor reads; the wire carries raven's
because every renderer's verb table is keyed by it, and the main session log stores the
host's own calls under those same names. The same pass re-keys a call's subject onto that
tool's own argument name, trying the tool's key first, then the keys adapters are known to
use, then any string the payload carries. Applied at the three reads that serve a delegated
transcript - an instance's history, a dag node's messages, a sub-agent's context - and
deliberately not inside `_map_to_wire`, which also serves the session log whose calls are
already raven-named. A name with no entry is passed through, which is how an openai step
type and a claude tool the table never listed both reach a client under their own name.
_Avoid_: applying it at write time - that is what this replaced.

**Closing Message** (`raven/agent/subagent/backends/acp_agent.py`, `activity.py`):
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

**Stop Reason** (`raven/agent/subagent/backends/acp_agent.py`):
What an ACP agent reports at the end of a turn. Only `end_turn` means it finished; every
other value (`cancelled`, `max_tokens`, `refusal`, ...) leaves a reply that reads complete
and is not. Such a reply is kept and carries an appended `[raven]` notice naming the stop
reason, budgeted before the reply is clamped to `maxOutputChars` so the notice cannot be
the part that is cut. Kept rather than raised because a partial answer is worth having;
noticed rather than returned bare because neither the main agent nor a person in a Direct
Chat can otherwise tell the text simply stops.

**DAG node id** (`raven/agent/subagent_dag/_graph.py`, `_store.py`):
A node's name inside a `run_subagent_dag` graph, and the address a *later* graph in the
same conversation uses to read what that node produced — `{{ <id>.output }}`, needing no
`depends_on`, since the node has already finished (naming it there is allowed and orders
nothing). That second role is why the id is
**unique per conversation, not per graph**: reusing one an earlier run took is refused, so
an id names one node and one output. An id is claimed for the whole run, whatever the
outcome, but only a `completed` node can be referenced; a failed, skipped, cancelled or
still-running one keeps its id and is refused with which of the four it is. A run stopped
by `/stop` or a shutdown records its still-running nodes as `cancelled` and its pending
ones as `skipped` on the way out, so "still-running" means what it says rather than
outliving the run that claimed it. Distinct from an
`instance` handle, which shares a sub-agent *session* rather than naming an output.
_Avoid_: "node name" — the id is an address, not a label.

**Working directory** (`raven/agent/workdir.py`):
The directory a turn reads and writes files in — shared by the session's leader `AgentLoop`
and every Subagent it spawns, and resolved per turn by `WorkdirResolver.resolve()`.
`raven tui` and `raven agent` use the process launch directory, so the agent works in the
checkout you started it from (Workdir policy `LAUNCH_DIR`); intermediate artifacts it
produces there go under that directory's `.raven/` (the shadow-git repo lives at
`.raven/shadow.git`). `raven gateway` gives each channel one directory (Workdir policy
`PER_CHANNEL`), set by `channels.<name>.workspace` — `gateway.web.workspace` for the web
channel — and defaulting to `<agent home>/../tmp/<channel>`, i.e. `~/.raven/tmp/<channel>`.
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

**Project slug** (`project_slug()`, `raven/utils/helpers.py`):
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
The first-run wizard (LLM provider → sandbox → channel → EverOS memory → sub-agents → cold-start import) that also seeds
Agent home via `sync_workspace_templates()`; gated at startup by `ensure_configured_or_onboard()`.

**Bootstrap Files**:
The identity files concatenated into every prompt — `soul.md` + `agent.md` + `TOOLS.md` —
rendered by the Context Builder / bootstrap segment.
_Avoid_: lumping `user.md` in — the user profile enters via the `# Memory` segment, not bootstrap.
