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

**Agent Hook** (`agent/hook/`):
The turn-loop extension point: an `AgentHook` ABC with five async phases
(`before_user_inbound`, `before_iteration`, `after_iteration`, `after_send`, `on_tool_call`).
Multiple hooks chain via `CompositeHook`; the EvalEngine wires three concrete implementations.
_Avoid_: "callback" or "middleware" — neither captures the phase-specific, chain-aware semantics.

**Subagent** (`agent/subagent/`):
A background agent task spawned by `SubagentManager`. Runs with its own tool set; its result
re-enters the session as a `SUBAGENT`-origin `TurnRequest` via Spine submit. Bounded by
`max_concurrent` (default 4) and a per-session hourly rate limit.
_Avoid_: conflating with a Turn — a Subagent lives outside the main turn and re-enters via Spine.

**Tool** (`agent/tools/`):
An agent capability behind a uniform `Tool` ABC (name, parameter schema, async
`execute`). Built-ins: file read/write/edit/list, grep/find, exec, web search/fetch,
message, ask_user, spawn (Subagent), MCP, media generation, and skill read/use.
_Avoid_: "function" — a Tool is the agent-facing capability, not a Python function.

**Tool Registry** (`agent/tools/registry.py`):
The name→`Tool` table the Agent Loop dispatches into: resolves a tool by name and runs
its `execute` under a timeout, returning the string result or a structured error.

**Deep Research** (`agent/tools/deep_research.py`):
Opt-in tool delegating an open-ended research question to the MiroThinker API; returns a
finished, cited answer. Streamed inline on CLI/TUI, async on channels (background run +
verbatim `deliver_text` push). Configured via `raven deep-research` or onboarding Step 5.
_Avoid_: "Subagent" — it is a single long-running tool, not a spawned agent.

**Checkpoint** (`agent/loop/checkpoint.py`):
A once-per-turn commit of the workspace into a shadow git repo (separate from the
user's `.git`), so an interrupted or failed turn can be rolled back.
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
and is the unit of cancellation. A stalled Lane never blocks other Lanes.
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
Per-origin concurrency gates: a `USER` pool and a `system` pool for proactive origins
(`SENTINEL`, `CRON`, `HEARTBEAT`, `SUBAGENT`), sized independently with no borrowing.
A user turn never waits on a proactive task's LLM slot.

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
the Runtime solely via TUI-RPC. Not a Channel.

**CLI**:
The one-shot command-line entry point (`raven <command>`) for operations and
configuration. Not a conversation front-end.
_Avoid_: using "CLI" for the interactive REPL (retiring)

**Routing Tag**:
The `channel` field on a `TurnRequest`; names the recipient — a Channel, or the TUI.

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

### TUI-RPC

**TUI-RPC**:
The single transport between Runtime and TUI (stdio pipe / Unix socket), carrying two
message kinds: Request/Response (TUI → Runtime method calls) and Notification
(Runtime → TUI one-way events).
_Avoid_: calling a Notification "the bus" or "broadcast" — Spine events never cross into the TUI directly

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
protected, archived) — what the Slow Path reads instead of full history.

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

**Skill Hub** (`skill_hub/`):
A remote OpenAPI skill marketplace, configured via `skillForge.router.hub` (`endpoint` /
`api_key` / `timeout_s` / `min_safety`; `endpoint=None` disables it). `SkillHubClient` offers
progressive disclosure — `search()` (metadata-only discovery), `get()` (skill body),
`install()` (download + safe extract); during routing `HubSkillSource` feeds metadata-only
candidates into the weighted RRF (weight 0.85, below Local 0.96 and Everos 0.9), and the
`read_skill` / `use_skill` tools do on-demand body fetch / script materialization. Replaces
the retired "Mass" source.

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

**Workspace**:
The per-agent filesystem tree (default `~/.raven/workspace`) holding the agent's and user's
memory, skills, and root task files. Exactly one per running agent.
_Avoid_: confusing the Workspace (the live instance) with the Workspace Template it is seeded from.

**Workspace Template** (`templates/`):
The bundled markdown seed files copied into a Workspace on first run by
`sync_workspace_templates()` (idempotent — fills only missing files, so user edits win):
`SOUL.md` (agent persona), `AGENTS.md` (agent operating instructions), `USER.md` (user
profile), `HEARTBEAT.md` (periodic-task list read by the heartbeat Scheduler), `TOOLS.md`
(tool-usage notes), `memory/MEMORY.md` (legacy memory seed). On the L4 layout these map
under `agent_memory/profile/` (soul.md, agent.md) and `user_memory/profile/` (user.md);
`HEARTBEAT.md` / `TOOLS.md` stay at the Workspace root.

**Onboarding** (`raven onboard` → `run_wizard`):
The first-run wizard (LLM provider → sandbox → channel → EverOS memory → deep_research → cold-start import) that also seeds the
Workspace via `sync_workspace_templates()`; gated at startup by `ensure_configured_or_onboard()`.

**Bootstrap Files**:
The identity files concatenated into every prompt — `soul.md` + `agent.md` + `TOOLS.md` —
rendered by the Context Builder / bootstrap segment.
_Avoid_: lumping `user.md` in — the user profile enters via the `# Memory` segment, not bootstrap.
