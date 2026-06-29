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
field), not part of the user-facing identity. See `docs/adr/0001`.

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
One independently enable-able efficiency measure (usage tracking, cache
optimization, smart routing, …).
_Avoid_: bare "Strategy"

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

**Context Engine**:
The pluggable layer that decides, each turn, which messages enter the LLM window.
Two implementations: legacy (pass-through + Consolidation elsewhere) and Curator.

**Curator**:
An internal, bounded agent loop whose only job is to build the main agent's next
context window. It never answers the user and never runs user-facing tools.
_Avoid_: calling legacy's lossy summarization "curating"

**Fast Path**:
Curator's zero-LLM route, taken when history is under the pressure threshold:
full history passes through unchanged.

**Slow Path**:
Curator's small-model agent loop, run under context pressure: inspects the Manifest,
archives/retrieves, and submits a context plan that a deterministic assembler validates.

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

**EverOS**:
An external memory system ([EverMind-AI/EverOS](https://github.com/EverMind-AI/EverOS))
installed as a built-in tool in Raven.

**SkillForge** (`memory_engine/skill_forge/`):
The skill self-evolution subsystem — detects reusable procedures from sessions,
versions them, evolves them on feedback, and retires stale ones. The name is
retained; it is now a live module under the Memory Engine, not the old top-level husk.

**Episode**:
A distilled event note the Consolidation step writes to `episodes.md`.

**Profile**:
The user-profile sections in `USER.md`, refreshed when their tags run hot.

**Foresight**:
A prediction the Memory Engine derives about the user's likely future behavior
(each carries prediction / time-window / confidence), written by the consolidator.
_Avoid_: conflating with the Proactive Engine's Predictor — Foresight is the stored
memory artifact; the Predictor is the live proactive stage.

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
