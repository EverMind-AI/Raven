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

**Turn Journal** (`agent/loop/journal.py`):
Crash-durable diagnostic sidecar for one turn: messages and marker events
(`turn_start`, `llm_call_start`) stream to `<session>.partial.jsonl` as they
happen, and the file is deleted once the canonical end-of-turn save succeeds.
A surviving partial file always marks a turn killed before persistence.
_Avoid_: treating it as a Session or feeding it to training/eval — the
canonical trajectory is the only source of truth; the journal is wire-truth
diagnostics (e.g. its user lines keep the runtime-context prefix).

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
(`before_user_inbound`, `before_iteration`, `before_execute_tools`, `after_iteration`,
`after_send`). The three iteration phases are dispatched from inside the agent loop each
ReAct iteration; a short-circuit decision ends the turn with that value as the reply, and a
rollback decision pops the iteration's messages and re-samples the LLM call without
consuming an iteration (bounded per turn, optional generation-parameter overrides).
Multiple hooks chain via `CompositeHook`; the EvalEngine wires three concrete implementations.
_Avoid_: "callback" or "middleware" — neither captures the phase-specific, chain-aware semantics.

**Loop Observer** (`agent/hook/observers/`):
An `AgentHook` implementation that watches the ReAct iteration phases and intervenes
through the decision channels (rollback / short-circuit) instead of adding branches to the
loop body. Default set (wired when `loop_observers=None`; pass `[]` to run bare):
`LoopscanObserver` (density-fingerprint decode-spin detection, bounded escalating-temperature
re-rolls), `BudgetObserver` (iteration/context watermark tagging, observability only),
`DuplicateQueryObserver` (byte-identical repeated search calls → bounded rollback).
Opt-in residents: `RefusalObserver` (terse-refusal re-roll — rollout/eval harnesses only,
a product refusal can be correct), `EmptySearchObserver` (empty-result streak tagging).
_Avoid_: conflating with TokenWise strategies — those hook the LLM-call boundary for
usage/cost concerns, not loop-behavior guards.

**Flow** (`agent/flow/`):
A versioned task-mode assembly over the agent loop: a frozen set of {observers, prompt
section, tool shaping, budgets} attached to the loop's existing seams (hook chain,
SegmentBuilder list, tool registration). The loop stays a task-agnostic engine; a flow is
selected by config at loop construction and stamps its version as a prefix on every
persisted trajectory line's `flow_version` (e.g. `dr@3.0/raven-0.1.5`).
_Avoid_: "workflow engine" / "DAG" — a Flow does not schedule nodes; it configures the
one loop.

**DR Flow** (`agent/flow/dr.py`, config `drFlow`; the current label lives in
`DRFlowConfig.version`, deliberately not repeated here so this entry cannot go stale):
The deep-research Flow: clarify discipline (assumption-stating prompt section) →
solve (the loop, with in-history budget notes appended to tool results and web tools
reshaped: answerBox/knowledgeGraph/snippets stripped, web_fetch distilled by a cheap
digest model via `info_to_extract`) → verify (end-of-turn `DraftReviewerGate`: an
independent-context reviewer that bounces a failing draft back once with feedback,
fail-open) → report (exhaustion triage). DR mode is a dedicated build, not a chat
variant: the tool surface narrows to `tools_allowlist` (default web_search / web_fetch — a
shell is deliberately absent, because with retrieval pinned to a fixed corpus a shell reaches
the open web around it), the system prompt drops the product segments (bootstrap / memory / skills —
`minimal_context`), and the CLI skips the Sentinel stack. Changing any of its defaults
or prompt text changes the trajectory distribution — bump `drFlow.version`.
_Avoid_: confusing with the removed `deep_research` tool (an outsourced API client);
the DR Flow is Raven's own research mode.

**Research turn** (`agent/flow/conversation.py`, config `drFlow.conversation`):
A turn of a DR conversation on which the research machine runs. Turn one follows
`drFlow.enabled`; from turn two the **conversation gate** classifies the message and a
non-research turn skips the DR observers and the answer shaping while keeping the DR system
prompt byte-identical — the prompt heads the cached prefix, so varying it per turn would
re-bill the conversation at uncached rates. Every classifier failure resolves to a research
turn. The **research memo** is the conversation-scoped record of URLs opened and queries run,
rebuilt from the client-side ledger and prepended to the next turn's message; it is a distinct
artifact from the **process appendix**, which is display-only and must never reach the model.
_Avoid_: "session memory" — the memo is per conversation and carries retrieval, not findings,
and it is unrelated to the cross-session Memory Engine.

**Clarify turn** (`agent/flow/ask_user.py`, config `drFlow.askUser`):
A turn that ends in an `ask_user` handoff and deliberately produces no answer: the model's
questions become the turn's reply and the user's next message carries the answer. Nothing
blocks — the tool call is the signal and the turn boundary is the transport, so the short
circuit happens in `before_execute_tools` and `after_iteration` never runs. Stamped
`turn_end.awaiting_user`, which is a boolean beside `status` and never a fourth value for it:
in every downstream consumer's eyes a clarify turn is a normally completed turn. Product
surface only, and structurally unreachable from a bench arm.
`drFlow.askUser.delivery` picks the transport: `handoff` (default, the measured behaviour)
is the short circuit above; `tool` runs the blocking `QuestionBroker` round trip instead —
the user answers a structured prompt (TUI / gateway; an ACP client that declared
`_meta.raven.askUser`, via `acp/questions.py`; the interactive REPL, via
`cli/_terminal_questions.py`), the answers return as the tool result, and the SAME turn
researches on them, so no clarify turn exists at all: no pending, no brief, no chain debit,
and the question text never enters the reply. Two consequences: `maxRounds` does not bound
this transport (nothing opens a chain; the budget is one granted round trip per turn), and
the outline half is not requested at all — it was the handoff reply's veto affordance, and
the broker prompt carries questions only, so clause, description and schema drop it
together whatever `askUser.outline` says. With no broker wired the gate falls back to
the handoff; `raven agent -m` stays headless by design and always takes that fallback.
_Avoid_: "clarification round" — that collides with the verify gate's revision round; and
"answerless turn", which is the failure this one is deliberately distinguished from.

**Mandated clarify turn** (`drFlow.askUser.mode = "first_turn"`, the default):
Turn one of a conversation, on which the contract asks for a clarify round unconditionally
rather than only when something is undecidable. A request and not a guarantee — nothing can
make a model emit a tool call — so `observers.ask_user.asked` on such a turn is a compliance
rate, and `first_turn` / `ask_required` mark it so those turns stay separable from turns where
asking was merely permitted. The clause text is the same on every turn: it states both regimes
at once, because the system prompt heads the cached prefix and varying it per turn re-bills the
conversation at uncached rates.
_Avoid_: "forced" — nothing is forced, and no turn is re-sampled for failing to ask.

**Clarify chain**:
The span from one handoff to the first following turn that asks nothing further.
`drFlow.askUser.maxRounds` bounds THIS, not a session: the count passes **through** the turn
that consumes a pending, and the chain closes — discarding the count — as soon as a turn
researches normally. A session-scoped budget would spend it on a conversation's first
research question and silently refuse its second, unrelated one.
_Avoid_: calling it a session — different scope, and conflating the two is the error this
scoping exists to fix.

**Research brief** (`agent/flow/ask_user.py`, `render_brief`):
The deterministic block prepended to the turn that answers a clarify handoff: the original
question, the questions asked, and the user's reply **transcribed verbatim**, never restated
as "the answer is X". Computed from the pending state rather than asked of the model, and
stripped before persist like the research memo and the report reminder — the outermost of the
three prefix blocks, which is what lets the memo's own prefix-anchored stripper still match.
The verbatim rule is what caps the cost of a misread reply at one stale paragraph instead of
a fabricated question-answer pair.
_Avoid_: "plan" — that is the outline / search plan; the brief is the task definition.

**Report template** (`agent/flow/dr.py`, `_DR_REPORT_STRUCTURE_CLAUSE`, knob
`drFlow.finalShape.reportStructure`):
The fixed markdown shape the DR contract asks the model for when the knob is on:
`## Answer` / `## Findings` / `## Limitations`, exact headings, all three present every
time, no other headings. Since `dr@3.4` the template also overrides any formatting
instructions carried by the question itself — a question asking for one word, a JSON
object, or a table is still answered as the three-section report on that question's topic,
with the requested shape satisfied inside the report where it fits; the override passage
rides its own sub-switch, `finalShape.reportFormatOverride` (default on; off restores the
pre-override clause byte for byte). Also in `dr@3.4` two mechanisms back the clause up, both inert
without it: a per-turn **reminder** (`reportReminder`, on) that restates the template on the
current user message and is stripped before persist, and a **shape bar**
(`reportBounce`, off — `flow/report_shape.py`) that bounces a draft missing a section back
once. A fourth sub-switch, `finalShape.reportDepth` (default off, unmeasured and therefore
unlabeled — an A/B arm pins the current default plus a `-depth` suffix;
`_DR_REPORT_STRUCTURE_CLAUSE_DEEP`), swaps in the **deep report template**: same three
sections, `## Findings` upgraded from a findings list to a full argued report (causal
narrative, per-datum source and as-of date, disagreements adjudicated in the open, facts
separated from forward-looking judgments, tracking signals inside `## Limitations`,
`###` subheadings allowed inside Findings); off restores the `dr@3.4` clause byte for
byte. Product surface only: every benchmark profile pins `reportStructure` off, which turns
all four off with it. _Avoid_: "report format" for the
`<answer>...</answer>` tags — those come from `finalShape.requireMarker`, a separate knob
that composes with this one.

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
A once-per-turn commit into a shadow git repo (separate from the user's `.git`), so an
interrupted or failed turn can be rolled back. Covers the tree the engine's file tools
are rooted in — the workspace everywhere but ACP, where it is the **Session File Root**,
so concurrent sessions neither race one repo nor stage each other's edits.
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

### ACP

**Session Engine**:
The `AgentLoop` serving one ACP session. One per session, not per process: the web
tools keep their per-turn state (saturation, evidence round, seen sets, retry budgets)
as instance attributes that `run_turn` resets at turn start, so two concurrent turns on
one engine clear each other's.
_Avoid_: "the engine" unqualified once more than one is resident.

**Engine Registry**:
`AcpLoops` — maps a conversation id to its Session Engine, builds one on first use, and
holds the pieces every engine shares (provider, session manager, memory backend). Also
the reclaim point: `session/delete` is not served, so the least recently used *idle*
session's engine is evicted at `acp.maxLoops`. A build that finds every victim mid-turn
goes over the cap rather than evict a running turn, so reclamation is retried whenever an
engine is handed out and whenever a turn settles.
Eviction is only lossless because everything a session needs across turns is held off the
engine: the transcript in `SessionManager`, and the interrupted-turn recovery record in the
shared map `AcpShared` hands every engine. Per-engine state added later has to answer the
same question — a rebuilt engine starts empty.

**Session File Root**:
The subtree under the shared workspace where one session's file, exec and media tools are
rooted (`acp_workspaces/<chat_id>`), and with them its **Checkpoint**, so two concurrent
turns writing the same filename neither overwrite each other nor race one shadow repo. Distinct from the **workspace** itself, which stays shared
because the system prompt's memory segments and the skill catalogue are read from it.
_Avoid_: calling it "the session's workspace" — that reads as the whole workspace moving.

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
SkillForge's three sources at RRF weight 0.9) — plus the after-turn write path
(`backend.store` → `/memory/add`, HTTP mode negotiating `/api/v2` with a `/api/v1`
fallback). Writes are addressed by four **scope keys**: `app_id` / `project_id` (hard
isolation — on-disk path segments; a search never crosses them), `agent_id` (soft owner
selector within a scope), and `session_id` (unprocessed-buffer key; one per task, never
shared between writers — EverOS fans agent cases out across every assistant sender in a
cell). With `session_id_prefix` set the backend mints `<prefix>_<uuid4hex>` per host
session key and persists the mapping in `<workspace>/.everos_session_map.json`, so the
same host key resolves to the same EverOS session in a later process — several `raven
agent -m` calls sharing one `-s` are one conversation to the host and have to be one
buffer to EverOS. Two write profiles: eager (default; `flush_every_turns` cadence) and
**deferred capture** (`defer_extraction=true`; `/add` is a buffer-only write with no
extraction LLM in the turn, promotion happens via an explicit flush —
`scripts/everos_flush_batch.py` after a measurement batch). A *successful* promotion is
idempotent (measured on everos 1.2.3: a second `/flush` answers `no_extraction` in
~10 ms), so `memory.flush_on_task_end` and the post-batch script can both be on. A
*failed* one is not retryable, which is the asymmetry to plan around: `/flush` runs
boundary detection and the user-track episode LLM inside the request under the server's
`memorize.session_lock_timeout_seconds`, while the agent track is dispatched to a
background queue. When the server hits that cap it has already committed the memcells and
drained the buffer, so cases and skills still land minutes later out of band, the episodes
never do, and the retry answers `no_extraction` — measured once at 70 buffered items
against a 360 s cap. Hence `flush_timeout_s` (and `everos_flush_batch.py`'s
`--flush-timeout-s`) must stay **above** the server's cap: equal budgets are the bad case,
because the server's timer starts after routing and fires first, turning an answer into a
500. Acceptance on a promotion compares the `users/` and `agents/` trees under the memory
root; the flush status alone cannot see this.
`recall_enabled=false` is the **store-only gate**: the host
keeps the backend on the after-turn store dispatch and out of the context engine, so
prompt bytes stay identical to `memory.backend=null` (what lets the write path ride on a
measurement arm without a `drFlow.version` bump). Per-call latency does not: the
after-turn store is detached under `_STORE_TURN_BUDGET_S` (5 s), so a turn still pays up
to that. Inside the gate,
`recall_memory_enabled` / `recall_skills_enabled` pick which lane earns its cost. What the
backend receives is cleaned by the same `AgentLoop._clean_turn_messages` pass that feeds the
session log, so the runtime-context envelope, the research memo and recovery scaffolding
never reach extraction; out-of-band reasoning is dropped unless `capture_reasoning` is on,
which folds it into tool-calling assistant rows only. The one place the two consumers
diverge is **flow-injected user turns**: a hook rollback (`rollback_inject` with
`role=user` — a verify-gate rejection, a `[finalize]` commit nudge, a spin-breaker
checkpoint) is the harness re-prompting itself, marked `_flow_synthetic` at the
application site in `_hook_rollback`. The log keeps them, because a revision with no
reason recorded above it is unreadable; capture drops them (`for_capture=True`), because
a `user` role there is a claim about the person — EverOS routes by sender, so they would
build episodes and a profile out of harness text and inflate the user-message count the
agent-case filter reads as evidence of a real correction. A genuine mid-turn user message
arrives through `drain()` (BusyPolicy.INJECT) and is unmarked, so it is still captured. `start()` probes `/health` once and
names what a reachable-but-degraded server cannot do (a 200 stopped implying a working
install at everos 1.2.1); that is a warning unless `require_service` is set, which raises
`MemoryServiceUnavailableError` instead — surfaced only by `raven agent` (as exit 1, with
the backend released), since an interactive session should degrade rather than refuse to
start; and inert outside `mode=http`, which is warned about rather than left silent.
Promotion at a task boundary is `memory.flush_on_task_end`: `raven agent` promotes the
one session it wrote, while the TUI and the gateway have no per-run boundary and promote
every session the process captured at exit (`AgentLoop.promote_all_backend_sessions`). The name refers to
the external package [EverMind-AI/EverOS](https://github.com/EverMind-AI/EverOS); the
in-tree code is only an adapter. The same plugin also contributes the `understand_media`
multimodal-parsing tool.
_Avoid_: sharing one `session_id` between a parent agent and sub-agents (cross-copies
cases); encoding the writer into `project_id` (that is `agent_id`'s job — hard isolation
is irreversible at write time).

**SkillForge** (`memory_engine/skill_forge/`):
A skill retrieval and injection subsystem — it fuses candidates from three sources
(local BM25-indexed files, self-evolved skills recalled from the pluggable `MemoryBackend`
— typically the EverOS plugin — and remote skills from the Skill Hub) via weighted RRF,
with optional LLM gating and query rewriting before injecting them into the agent prompt.
Bodies from any source other than `local` are fenced with `wrap_untrusted` on the way in:
the criterion is authorship, not retrieval quality — a local skill is a reviewed file in
this repo, while an everos skill is distilled from past conversations and a Hub skill is
written by a stranger, and both land in the system prompt. The fence is system-prompt
bytes, so it is a distribution change wherever it can fire — but it can only fire on a
turn that injects a non-local skill, which needs either `hub.endpoint` set or EverOS
recall on, and **turning either on is itself a distribution change** needing its own
`drFlow.version` and anchor pair. So the fence never independently invalidates a measured
arm; it rides along with the change that makes it observable. `router.everos_min_confidence`
is a client-side maturity floor on everos hits (the server's `min_score` is a relevance
floor only the episode path reads); hits without the field, which is every agent case, pass.
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
