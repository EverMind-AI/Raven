# Proactivity Design and Implementation { #proactivity-design }

For developers extending or debugging the Proactive Engine. This is the site's
implementation reference, with the design rationale kept alongside the
contracts it explains. For configuration, costs, and operating procedures, use
the [Proactivity Guide](proactivity.md).

Paths below are relative to `raven/proactive_engine/` unless stated otherwise.
Configuration types live in `raven/config/raven.py`; canonical runtime terms
live in `CONTEXT.md`.

<span id="1-three-layers-of-proactivity"></span>
<span id="2-the-core-idea-periodic-planner-plus-on-demand-spawn"></span>
<span id="7-scenarios"></span>
<span id="l2-routine-automation"></span>
<span id="l3-memory-linked-reminder"></span>
<span id="l3-context-aware-resumption"></span>
<span id="l3-proactive-status-check"></span>

## Design rationale

The aim is to offer useful follow-up work without treating every observation
as permission to interrupt or act. Three choices shape the implementation:

- **Separate decision from execution.** The Planner reads packaged context and
  returns a structured decision. Executors own delivery and task dispatch.
  This makes decisions testable without starting background tasks.
- **Match the interruption to the situation.** Silence, a standalone nudge,
  a reply-appended nudge, a deferred nudge, and a background task have different
  effects on an active conversation.
- **Reuse runtime services.** Policy, persisted feedback, the Spine, and
  SubagentManager provide the shared mechanisms. Sentinel does not implement a
  second tool-running agent loop.

Routine-based assistance and context-aware anticipation describe intended uses,
not guaranteed capabilities. For example, a remembered deadline can inform a
reminder and a recent deployment can inform a status-check proposal. Whether
either happens depends on available context, model output, policy, and tools.
A learned pattern alone is not a user-created Cron schedule.

<span id="3-components"></span>

## Components and assembly

| Responsibility | Implementation |
| --- | --- |
| Stack construction and hooks | `raven/core/proactive_stack.py` |
| Lifecycle, tick, and routing | `sentinel/executor/runner.py` |
| Context assembly | `sentinel/predictor/context_assembler.py` (`PlannerContextAssembler`) |
| Decision and validation | `sentinel/planner.py`, `sentinel/types.py` |
| Tool schema and context rendering | `sentinel/trigger_policy/prompts.py` |
| Limits and preferences | `sentinel/trigger_policy/` (`policy.py`, `prefs.py`) |
| Nudge delivery, reply append, and delay | `sentinel/executor/` (`dispatcher.py`, `injector.py`, `defer_manager.py`) |
| Proactive task dispatch | `sentinel/executor/spawn.py` |
| Routine learning and task discovery | `sentinel/predictor/` |
| Persistence and feedback | `sentinel/feedback/`, `sentinel/state_files.py` |
| Derived attention state | `sentinel/attention_updater.py`, `sentinel/attention_producers/` |

`build_sentinel_stack()` returns an inactive result when Sentinel is disabled.
Otherwise it builds the shared stores, policy, Planner, executors, and runner.
The gateway binds the dispatcher's `post` callback to its DeliveryHub after the
hub exists; `attach_sentinel_spawn()` and
`attach_sentinel_decision_consumer()` connect agent-dependent execution.

The Planner model defaults to the main agent model. `evaluator_model` can
override it; `evaluator_base_url` and `evaluator_api_key_env` can select a
separate provider. If the named API-key environment variable is empty, assembly
warns and falls back to the main provider. The system prompt is loaded through
`raven.i18n`; the tool schema and context renderer remain in
`trigger_policy/prompts.py`.

<span id="proactiveplanner-periodic-reasoner"></span>
<span id="contextassembler-input-packaging"></span>
<span id="planner-decision-quality"></span>

## Context and decision contracts

`PlannerContext` is the Planner's input. `PlannerContextAssembler` collects:

| Context | Source |
| --- | --- |
| `memory_md`, `history_md_recent` | Long-term memory and a recent history tail |
| `active_sessions` | Recently active sessions with their last user and assistant messages |
| `routines` | Deterministic history-pattern learning |
| `calendar` | An optional caller-supplied calendar function; no built-in calendar integration is implied |
| `nudge_policy_state`, `fire_history` | Policy counters, recent topic fires, and dismissals |
| `last_decision` | The runner's remembered previous decision |
| `attention_md`, `behaviors_recent` | Selected attention sections and a folded behavior window |

Missing sources generally produce empty fields rather than aborting assembly.
Memory filtering, attention-section selection, and behavior windows are
configurable; the Planner does not independently search other data sources.

`ProactivePlanner.decide()` requests a `planner_decision` tool call. It
normalizes invalid actions to `skip`, invalid priorities to `low`, and clamps
the score to `[0, 1]`. Nudge actions need `nudge_message`; defer additionally
needs `defer_condition`; spawn needs `spawn_task`. Missing required payloads
downgrade the decision to `skip`. A `topic_tag` supports per-topic policy;
the Planner derives a fallback tag if the model omits it.

Provider error responses, missing tool calls, and non-dict arguments become
`skip`. Raised exceptions are handled by the runner, not swallowed by the
Planner. Structured output constrains the action format; it does not establish
that the proposed action is correct or safe.

## Tick lifecycle

`await tick_once()` assembles context and calls `await tick_with_context(ctx)`.
The latter runs these steps:

1. Trim feedback when due and retune the policy.
2. Refresh derived memory state and run task discovery when due and enabled.
3. Check the daily fire plan for a due recurring slot; if one qualifies, route
   its prepared message without a Planner call.
4. Apply skip-only fast paths for quiet hours or unchanged context after a
   previous skip. A due high-priority deadline bypasses these shortcuts so it
   can reach the Planner.
5. Ask the Planner and route the decision. If it raises, try the guarded
   high-priority deadline fallback when enabled; otherwise return an error skip.
6. Remember the decision for the next tick.

One-shot deadline slots normally go to the Planner so recent context can
indicate that the user already completed the work. The outage fallback cannot
make that judgment; it is restricted to due high-priority deadline slots and
still uses the normal routing policy. A returned `skip` is not the same as a
raised exception and does not invoke this fallback.

`start()` / `stop()` manage the periodic loop, the defer loop, and the
discovery-trigger consumer when wired. The trigger consumer polls a separate
file-based store on a short cadence; it does not wait for the next Planner
tick. The runner logs unexpected background exceptions and continues.
`TickOutcome` exposes the decision, execution result, route, optional nudge
identifier, and notes for diagnostics.

<span id="4-action-space"></span>
<span id="proactivespawn-multi-step-execution-bridge"></span>

## Action routing

| Action | Path | When work is considered dispatched |
| --- | --- | --- |
| `skip` | No executor | No dispatch |
| `nudge` | Policy check, target resolution, NudgeDispatcher → `DeliveryHub.post` | After the dispatcher reports delivery |
| `nudge_inject` | Policy check, NudgeInjector queue | At queue insertion, before the user receives a reply |
| `nudge_defer` | Policy check, DeferManager registration | Registration is pending, not delivery |
| `spawn_agent` | ProactiveSpawn policy check → SubagentManager | At task dispatch, not task completion |

Plain nudges and discovery menus are posted directly to the DeliveryHub.
They are finished messages, not prompts to run through the tool-enabled Agent
Loop. This preserves menu formatting and avoids having the agent act on a
reminder as if it were a new user request.

NudgeInjector appends pending text through a response-modifier hook on an
eligible reply. It has an expiry and a per-session FIFO cap. DeferManager waits
for the target session's idle threshold, expires entries after a maximum wait,
and resolves destinations at fire time. Its `defer_condition` is not evaluated
by an LLM: settlement is time-based.

ProactiveSpawn validates the task, checks the shared policy using the task text
for deduplication, and calls SubagentManager. Completion returns through a
`SUBAGENT`-origin turn in the originating session, not NudgeDispatcher. It adds
neither a private quota nor a task-wide timeout.

<span id="8-cost"></span>
<span id="spawn-safety"></span>
Cost and spawn-safety guidance now lives in the guide's
[Costs and safety limits](proactivity.md#costs-and-safety-limits) section,
including model calls, backend execution, and isolation limits.

An unwired executor returns a degraded, non-delivered result. In particular,
disabling inject or defer does not convert those decisions into plain nudges.

<span id="nudgepolicy-the-shared-anti-spam-gate"></span>
<span id="5-anti-spam-the-nudgepolicy-gate"></span>
<span id="9-risks-and-mitigations"></span>
<span id="over-notification"></span>

## Policy boundaries

`NudgePolicy.check()` evaluates quiet hours, learned and user-specified
do-not-disturb windows, daily/hourly limits, session and dismissal cooldowns,
topic feedback, content deduplication, and rolling topic quotas. High priority
may bypass selected soft limits, not the daily cap, cooldowns, or topic limits.
Ordinary user-specified quiet windows also follow the high-priority bypass
setting; they are not an unconditional block.

Adaptive tuning adjusts the hourly multiplier using feedback; a weekend
factor can tighten it further. Preference overrides can only tighten the
static policy. Policy state persists, so restarting is not a quota reset.

Checking and recording are separate operations. Do not assume that every path
charges quota at the same time, or that a persisted ledger is an atomic
check-and-reserve transaction:

- Plain nudges record a fire after reported delivery; injects record when queued;
  proactive spawns record after dispatch. Queuing or dispatching is not proof
  that the user has seen the result.
- Defer registration checks policy, but the current runner does not attach a
  callback to record the eventual fire. DeferManager directly dispatches after
  the idle check without rechecking policy. Delayed nudges therefore do not
  provide complete send-time quota or quiet-hours enforcement.
- The task-discovery menu is policy-gated. A user-selected option is a separate
  execution path, not a second pass through ProactiveSpawn for every action.
- Cron is explicitly user-scheduled and bypasses `check()`. With a Sentinel
  runner wired, successful fires update its shared counters and topic ledger.
  This helps suppress overlapping Sentinel reminders; it is not a guarantee
  of semantic deduplication across all messages.

## State and feedback

Default runtime state is under `~/.raven/sentinel/`, relocated with
`RAVEN_HOME`. Filenames are defined in `sentinel/state_files.py`.

| File | Role |
| --- | --- |
| `state.json` | Shared policy ledger, pending injects and defers, and engagement state |
| `feedback.jsonl` | Dispatch and feedback events used for adaptive tuning |
| `pending_decisions.json` | Discovery menus, expiry, and confirmation state |
| `routines.json` | Learned routines and their persisted confirmation state |
| `discover_triggers.json` | Operator-requested discovery runs consumed by the runner |

`JsonStateStore` uses an `fcntl` lock and atomic rename for its JSON
read-modify-write operations. Processes using the same state directory share
these files; this does not imply independent quotas for every chat recipient.

In the configured agent home, `user_memory/attention.md` holds derived
sections. AttentionUpdater computes producer output outside the file lock and
splices sections under lock, skipping unchanged output and isolating producer
failures. Optional daily analysis shares an LLM result across several
producers. `user_memory/behaviors.md` holds extracted behavior events. Daily
analysis and behavior extraction are off by default.

The current feedback hook handles a recent nudge's `/dismiss` reply as a
dismissal and session cooldown. An unclassified reply is neutral, not
automatically accepted. Discovery choices and confirmations record their own
feedback; unattended nudges can contribute ignored signals.

<span id="routinelearner-behavior-pattern-learning"></span>
<span id="task-discovery-anticipatory-menus"></span>
<span id="history-format-drift"></span>

## Routines and task discovery

RoutineLearner bins timestamped history by weekday and time slot and extracts
keywords without an LLM. Unparseable lines are skipped, and insufficient
history yields no candidates. Recency weighting favors current patterns.
RoutineStore preserves confirmed state when refreshed; routine confirmation
promotes a candidate to active, and dismissal retires it for a cooldown before
it can be proposed again. Silence alone does not create a confirmed routine.

When enabled, TaskDiscoverer refreshes candidates, optionally validates them,
groups them for presentation, and proposes a `PendingDecision` menu.
PendingDecisionStore maintains its expiry, supersession, and confirmation state.
DecisionRouter matches `/pick N` deterministically. Other replies, including bare
numbers, use a confidence-gated model classifier when a provider and model are
configured; if either is missing, only `/pick N` selects an option. DecisionConsumer
handles the matched reply before the normal agent turn continues.

After the configured confirmation step, ActionExecutor dispatches by kind:

- `reply`: submit the chosen prompt as a user-intent turn with
  `sentinel.action_origin`, not a NudgeInjector append. Submit without waiting
  on that same Lane inside the menu-pick hook, or the hook would deadlock.
- `tool`: invoke the registered tool.
- `spawn`: delegate directly to SubagentManager.
- `routine_confirm`: promote the routine and optionally create a Cron job
  when the payload requests it and a CronService is wired.

A deterministic selection avoids the normal conversational LLM path, but
classification, confirmation, or the selected work may still call a model.

<span id="6-delivery-and-turn-transport-the-spine"></span>

## Cron, Heartbeat, and the Spine

CronService (`schedulers/cron/service.py`) persists jobs under file locking
and claims due work before executing it outside the lock. Ownership follows
**Fire-at-origin**: the runner for the job's creation-time channel/recipient
binding executes and delivers it. There is no trigger-time forwarding or
broadcast. `raven/core/cron_stack.py` submits the work as a `CRON` turn in
`cron:<job_id>`.

Successful recurring fires increase `silent_fire_count`; matching user
activity resets it. Reaching `silent_fire_limit` (default `12`) disables the
job. This counts fires without user activity, not failures. Cron's feedback
record is marked neutral so it does not lower Sentinel's learned acceptance
rate. Fixed-delay `every` schedules compute their next run from completion,
not from the previous due time.

HeartbeatService (`schedulers/heartbeat/service.py`) checks `HEARTBEAT.md`
with a structured model decision and runs agent work only on a `run` result.
`wake.py` coalesces early wake requests, rate-limits them, and defers them
while user work is busy. Wake drives Heartbeat, not Sentinel's tick loop.

The Spine's Scheduler routes turns into per-conversation Lanes and origin
concurrency pools; DeliveryHub routes output to outlets. User-inbound and
response-modifier hooks distinguish genuine user turns from system-origin
work. A user-confirmed discovery action has its own marker to avoid counting
the selection twice.

General turn controls are not Sentinel features: `BusyPolicy.INJECT` concerns
mid-turn user input, while `ask_user` and QuestionBroker handle structured
questions. See [Architecture](architecture.md); implementation entry points
are `raven/spine/scheduler.py`, `raven/agent/loop/main.py`, and
`raven/rpc/question_broker.py`.

## Verification entry points

Use `tests/test_sentinel_planner.py` and `tests/test_sentinel_fast_path.py`
for decision behavior; `tests/test_sentinel_runner.py`,
`tests/test_nudge_policy.py`, and `tests/test_proactive_spawn.py` for routing
and policy. `tests/test_core_sentinel_stack.py` covers assembly,
`tests/test_core_cron_stack_ledger.py` covers Cron's ledger integration, and
`tests/test_cli_sentinel_commands.py` covers operator commands. Run tests
through `uv run pytest`; use injected clocks and fake providers rather than
live notifications to check these contracts.
