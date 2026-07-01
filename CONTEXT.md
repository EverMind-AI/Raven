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
candidates into the weighted RRF (weight 0.85, below Local 1.0 and Everos 0.9), and the
`read_skill` / `use_skill` tools do on-demand body fetch / script materialization. Replaces
the retired "Mass" source.

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
(`PluginConflict` on collision). `build_memory_backend()` / `build_tool()` construct a
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
The first-run wizard (LLM provider → sandbox → channel → EverOS memory) that also seeds the
Workspace via `sync_workspace_templates()`; gated at startup by `ensure_configured_or_onboard()`.

**Bootstrap Files**:
The identity files concatenated into every prompt — `soul.md` + `agent.md` + `TOOLS.md` —
rendered by the Context Builder / bootstrap segment.
_Avoid_: lumping `user.md` in — the user profile enters via the `# Memory` segment, not bootstrap.
