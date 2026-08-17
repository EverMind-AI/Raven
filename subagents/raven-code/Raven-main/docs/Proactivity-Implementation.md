# Raven Proactivity — As-Built Reference

This is the as-built reference for Raven's proactivity subsystem: what the
code actually does, with module paths you can open directly. Its companion,
`docs/Proactivity-Plan.md`, is the design intent.

Raven's proactivity is a periodic tick plus an LLM decision plus
user-state awareness — not a user-scheduled timer. The key points:

1. Two sources: Sentinel (the LLM decides each tick whether and how to reach
   the user) and Cron (the user explicitly schedules a reminder). Both reach the
   agent as origin-tagged turns through the spine, and both share one
   `NudgePolicy` ledger so they never double-remind on the same topic.
2. A decision is one of five structured actions: `skip`, `nudge`,
   `nudge_inject`, `nudge_defer`, `spawn_agent`. `nudge_inject` (ride the next
   reply) and `nudge_defer` (wait until the current thread settles) are what
   make the agent aware of what the user is doing right now.
3. The decision reads one packaged context, the `PlannerContext`. The Planner is
   a pure function and degrades any failure to `skip`; it never raises.
4. One shared anti-spam gate, the `NudgePolicy`, learns its tightness from user
   feedback. Every nudge executor and task discovery passes through it.
5. State is persisted, not rebuilt each tick: derived decision signals land in
   `user_memory/attention.md`, long-term behavior in `behaviors.md`, and runtime
   state in an `fcntl`-locked `state.json`. The REPL and gateway share it.
6. Off by default (`sentinel.enabled=false`); opt-in to activate.

**Module root**: `raven/proactive_engine/`, with `sentinel/` (the decision
and execution subsystem), `schedulers/cron/` and `schedulers/heartbeat/` (the
two timer-driven services), and `wake.py` (event-driven early wake).

**Subdirectories of `sentinel/`**: `predictor/` (ContextAssembler, RoutineLearner,
RoutineStore, TaskDiscoverer, DailyAnalysisService), `executor/` (Runner,
NudgeDispatcher, NudgeInjector, DeferManager, ProactiveSpawn, PendingDecisionStore,
DecisionRouter, DecisionConsumer, ActionExecutor), `feedback/`
(NudgeFeedbackTracker, JsonStateStore), `trigger_policy/` (NudgePolicy,
ProactivityPreferencesReader, prompts), `tools/` (the nudge-feedback tool),
`attention_producers/` (the attention.md producers plus their base), and the
top-level `attention_updater.py` / `discover_triggers.py`.

---

## Architecture overview

Proactivity has two sources, Sentinel (LLM decision) and Cron (user-scheduled),
and both reach the agent as origin-tagged turns submitted to the spine
`Scheduler`. There is no message bus.

```
SentinelRunner (tick loop)
  ContextAssembler.assemble()                    -> PlannerContext
    MemoryStore.read_long_term()                 -> memory_md
    history file tail                            -> history_md_recent
    RoutineLearner.learn(history)                -> routines
    SessionManager.sessions (active window)      -> active_sessions
    NudgePolicy.snapshot_state()                 -> nudge_policy_state
    attention.md selected sections               -> attention_md
    behaviors.md folded window                   -> behaviors_recent
    NudgePolicy ledger                           -> fire_history

  fast-path rules (skip-only)                    -> Decision | None
    quiet hours hard hit -> skip
    unchanged-context dedup -> skip

  scheduled-fire (cron-style plan execution)     -> Decision | None

  ProactivePlanner.decide(ctx)                   -> PlannerDecision
    tool call: planner_decision(...)
    5 actions: skip | nudge | nudge_inject | nudge_defer | spawn_agent

  _route(decision):
    skip         -> record tick, return
    nudge        -> NudgePolicy.check -> NudgeDispatcher.dispatch
    nudge_inject -> NudgePolicy.check -> NudgeInjector.queue
    nudge_defer  -> NudgePolicy.check -> DeferManager.register
    spawn_agent  -> ProactiveSpawn.dispatch (its own policy check inside)

  JsonStateStore (fcntl + atomic rename) <- NudgePolicy / NudgeInjector / DeferManager
  DeliveryHub.post(...)                  -> channel outlet
  NudgeFeedbackTracker                   <- engagement signals


CronService (timer loop, sleep capped so peer-process job edits are seen)
  _on_timer():
    fcntl lock on jobs.json.lock
    filter by allowed_channels + claim unclaimed jobs
    save claim, release lock
    execute job out of lock -> on_cron_job callback
      submit(TurnRequest(origin=CRON, ...)) -> run_turn -> hub delivery / broadcast
```

Core design decisions:

1. Pure-function decision layer: the Planner is `(ctx, provider, model) ->
   Decision` with no side effects; any failure degrades to `skip` and it never
   raises.
2. Structured tool call: the Planner returns one of the five actions via the
   `planner_decision` tool schema.
3. Five actions, three nudge executors plus spawn: finer-grained than a binary
   run/skip, and aware of the user's current state.
4. One shared NudgePolicy gate: all three nudge executors and task discovery use
   it.
5. The spine is the sole transport: proactive messages are posted to the
   DeliveryHub; proactive sources submit origin-tagged turns to the Scheduler.

---

## 1. Data types (`sentinel/types.py`)

### Action

```python
Action = Literal[
    "skip",            # nothing worth doing this tick
    "nudge",           # send a standalone message NOW
    "nudge_inject",    # append to the agent's next reply in target_session
    "nudge_defer",     # wait until target_session's current thread settles
    "spawn_agent",     # dispatch a micro-agent for a multi-step task
]
```

### PlannerDecision

```python
@dataclass
class PlannerDecision:
    action: Action
    reason: str = ""
    priority: Priority = "low"           # low | medium | high
    proactivity_score: float = 0.0       # 0-1 confidence
    target_session: str | None = None    # "channel:chat_id"
    nudge_message: str | None = None     # required for the three nudge actions
    spawn_task: str | None = None        # required for spawn_agent
    defer_condition: str | None = None   # required for nudge_defer
    raw_llm_response: dict | None = None
```

### PlannerContext

The Planner's only input. In production it is built by the ContextAssembler.
Key fields:

- `memory_md` / `history_md_recent`: the workspace MEMORY.md and a tail of the
  history file.
- `active_sessions`: channel sessions active within the recent window (with
  their last user/assistant message).
- `routines`: candidate recurring patterns from the RoutineLearner.
- `calendar`: calendar entries.
- `nudge_policy_state`: remaining quota and quiet-hours state.
- `last_decision`: the previous tick's decision (so the Planner does not repeat
  itself).
- `fire_history`: what topics the Planner recently fired and whether the user
  dismissed them. Filled directly from the in-memory NudgePolicy (no LLM, no
  disk).
- `attention_md`: a markdown block of selected sections from `attention.md` (not
  the whole file). Which sections is config-driven.
- `behaviors_recent`: a folded single-line-per-event block from the tail of
  `behaviors.md`.

These last three are what feed the Planner the subsystem's derived decision
state explicitly; each is assembled by its own ContextAssembler helper. The
Planner prompt rendering lives in `trigger_policy/prompts.py`.

---

## 2. Orchestration: SentinelRunner (`sentinel/executor/runner.py`)

### One tick

```python
async def tick_once(self) -> TickOutcome:
    ctx = self.assembler.assemble()
    return await self.tick_with_context(ctx)

async def tick_with_context(self, ctx):
    self._maybe_cleanup_feedback()        # daily trim of the feedback JSONL
    self._maybe_retune_policy()           # adaptive NudgePolicy multiplier
    await self._refresh_memory_state()    # refresh attention.md + behaviors.md
    await self._maybe_run_task_discovery()# daily task-discovery batch
    scheduled = self._fast_path_scheduled_fire(now)   # cron-style plan execution
    if scheduled is not None:
        self.assembler.remember_last_decision(scheduled)
        return await self._route(scheduled)
    fast = self._fast_path_rules(ctx)     # skip-only rule short-circuit
    if fast is not None:
        self.assembler.remember_last_decision(fast)
        return TickOutcome(decision=fast, result=None, route="fast_path_skip")
    try:
        decision = await self.planner.decide(ctx)
    except Exception:
        if (fb := self._fallback_deadline_fire(now)) is not None:
            return await self._route(fb)  # high-priority deadline outage fallback
        decision = PlannerDecision(action="skip", reason="planner_error:...")
    self._warn_unfired_due_deadline(decision, now)
    outcome = await self._route(decision)
    if decision.action == "skip":
        setattr(decision, "_ctx_signature", self._context_signature(ctx))
    self.assembler.remember_last_decision(decision)
    return outcome
```

### Fast-path rules (skip-only)

Two rules short-circuit the Planner LLM call. Both are safe-to-deny — they only
produce `skip`, never a false nudge:

| Rule | Condition | Benefit |
|---|---|---|
| quiet hours | `ctx.nudge_policy_state.in_quiet_hours` | running the Planner in a muted window is wasted (NudgePolicy would deny anyway) |
| unchanged-context dedup | `last_decision.action == skip` and the context signature matches | memory/history/sessions did not change between ticks; skip the second LLM call |

The signature is a short byte hash over `memory_md`, `history_md_recent`, and
the active session keys, stashed on the decision as a dynamic attribute. A skip
cache expires after a bounded TTL so a quiet persona cannot lock `skip`
indefinitely and starve the feedback loop.

### Scheduled-fire fast path

A daily-plan producer lays out the day's intended proactive fires. The
scheduled-fire path is its cron-style executor: when a tick lands within a slot's
time window and that topic has not yet fired today, it sends the pre-planned
message without an LLM call. Recurring slots fire directly; one-shot deadline
slots fall through to the Planner instead, because only the Planner can read the
recent history and tell "not done yet" from "user already finished it" — a
language-level judgment. If the Planner is unavailable, a guarded fallback can
blind-fire only the high-priority deadline slots, and only through the normal
policy gates, to keep a hard deadline from being silently missed during an
outage.

### Drive modes

- `start()` / `stop()`: the runner owns its own tick loop on a plain interval
  (default 1800s / 30 minutes), and concurrently runs the DeferManager loop and,
  in the gateway, a trigger-consume loop. The runner does not depend on the wake
  scheduler — event-driven wake is the heartbeat's concern (see section 11).
- `tick_once()` / `tick_with_context()`: a single synchronous tick, used by
  tests and benchmark adapters.

### Degradation

Every layer is wrapped: a ContextAssembler failure becomes a `skip`; a Planner
failure tries the deadline fallback then `skip`; an executor failure returns a
non-delivered result and the tick continues. The runner never raises — a single
failed tick never breaks the lifecycle.

### TickOutcome

```python
@dataclass
class TickOutcome:
    decision: PlannerDecision
    result: ExecutionResult | None   # None when skip or no executor fired
    nudge_id: str | None = None      # correlates the NudgeFeedbackTracker
    route: str = ""                  # which executor path was taken
    notes: list[str] = field(default_factory=list)
```

---

## 3. Context assembly: ContextAssembler (`sentinel/predictor/context_assembler.py`)

Aggregates every signal source into the PlannerContext, with graceful
degradation per field:

| Field | Source | When the source is missing |
|---|---|---|
| `memory_md` | `MemoryStore.read_long_term()` | `""` |
| `history_md_recent` | tail of the history file | `""` |
| `routines` | `RoutineLearner.learn(history_md)` | `[]` |
| `active_sessions` | `SessionManager.sessions` filtered to the active window | `[]` |
| `nudge_policy_state` | `NudgePolicy.snapshot_state()` | default object |
| `calendar` | a caller-injected calendar function | `[]` |
| `last_decision` | the runner's `remember_last_decision()` | `None` |
| `attention_md` | selected H2 sections of `attention.md` | `""` |
| `behaviors_recent` | folded tail of `behaviors.md` | `""` |
| `fire_history` | the in-memory NudgePolicy ledger (no LLM, no disk) | `{}` |

The coupling to SessionManager is deliberately loose (attribute access, no type
assumption).

---

## 4. Decision layer: ProactivePlanner (`sentinel/planner.py`)

Generation parameters are pinned (a small max-tokens, a low temperature, no
reasoning effort) so a global provider configuration cannot leak in and pollute
the decision.

`decide()` flow:

1. Build the messages: the system prompt plus the rendered context prompt.
2. Call the provider with the `planner_decision` tool.
3. Every failure path degrades to `skip` (provider error, no tool call, tool
   args not a dict).
4. Field validation and clamping: an invalid `action` becomes `skip`, an invalid
   `priority` becomes `low`, an out-of-range `proactivity_score` is clamped to
   `[0, 1]`.
5. Action/field consistency guard: a nudge action with no `nudge_message`, a
   `nudge_defer` with no `defer_condition`, or a `spawn_agent` with no
   `spawn_task` is all downgraded to `skip`.

Step 5 is the key defense: the tool schema only requires action / reason /
score, so the conditionally-required fields are enforced here and the downstream
executors do not need defensive checks.

Prompts live in `sentinel/trigger_policy/prompts.py`: the system prompt (the
Planner's identity, the five-action semantics, and "default to skip unless
clearly worthwhile"), the structured tool schema, and the context prompt builder
that renders the PlannerContext to markdown.

---

## 5. The gate: NudgePolicy (`sentinel/trigger_policy/policy.py`)

All three nudge executors (dispatcher / injector / defer) and the task
discoverer must pass `check()` before delivering.

### Layered checks

`check()` runs a sequence of gates, roughly in this order:

| Layer | Rule | High priority can bypass? |
|---|---|---|
| 1 | `action == skip` -> deny | n/a |
| 2 | quiet hours (default 23:00-07:00) | yes (but the bypass is itself withdrawn when high-priority acceptance is low) |
| 2b | dynamic per-hour do-not-disturb learned from feedback | yes |
| 3 | per-persona do-not-disturb windows | yes |
| 4 | per-day quota | no (hard cap) |
| 5 | per-hour quota (scaled by the adaptive + weekend multipliers) | yes |
| 6 | per-session cooldown | no |
| 7 | dismissal cooldown | no |
| 8 | per-topic weighted hard-reject cooldown | no |
| 9 | per-topic acceptance-rate gate | no |
| 10 | content de-duplication within a window | no |
| 11 | rolling per-topic quota stack (hour / day / week) | no |

### Adaptive multiplier

`apply_adaptive_tuning()`, called by the runner each tick, moves the hour-quota
multiplier symmetrically with the user's recent acceptance rate: a highly
engaged user can be loosened above the baseline, a disengaged user tightened
below it, with a cold-start floor, an asymmetric volume gate (loosening needs
more samples than tightening), and a hysteresis band to prevent flapping. The
weekend tightener stacks a separate factor on top. The multiplier is also
surfaced to the Planner prompt as a soft signal so the Planner can raise its own
value threshold and avoid LLM calls that would only be denied.

### Read/write split

```python
verdict = policy.check(action, session_key, content, priority)
if verdict.verdict == "allow":
    await dispatch(...)
    policy.record_fired(action, session_key, content)
```

State is written only after a successful dispatch, so a "deny -> dispatch ->
error -> do not charge quota" case is handled correctly.

### Personalization and persistence

The policy accepts an optional overrides function. The ContextAssembler binds a
`ProactivityPreferencesReader` that re-reads the user's preferences each tick, so
the chain from learned preference to effective gate is always current. Overrides
may only tighten (a user preference can widen the quiet window, never narrow it).

When constructed with a `JsonStateStore`, the policy hydrates from disk before
each check and writes back atomically after each fire. One store instance is
shared by the NudgePolicy, the NudgeInjector, and the DeferManager, so every
mutation goes through one `fcntl` lock and the REPL and gateway never tear each
other's state.

A `now_fn` injection point lets tests drive quota windows, dedup TTLs, and
cooldowns under a frozen clock.

---

## 6. The three nudge execution paths

### NudgeDispatcher (`sentinel/executor/dispatcher.py`)

The dispatcher is stateless (the caller owns rate limiting and target
resolution) and posts to the spine DeliveryHub. The hub's `post` callable is
late-bound via `set_post` because the hub is built inside the running loop after
the dispatcher.

- `dispatch(decision, targets)` handles `action == nudge`. It posts a `Text` to
  each resolved `(channel, chat_id)` with `source.extras._sentinel_origin=True`.
  It goes through the hub's non-turn `post`, so the user receives it as a
  standalone proactive message — it never re-enters the tool-enabled agent loop,
  and the agent therefore cannot "act on" a reminder (fabricate a deliverable or
  mark a deadline done). Real channel outlets deliver the content verbatim; the
  interactive CLI outlet reads `_sentinel_origin` to prefix a proactive marker.
- `dispatch_options(decision)` handles a task-discovery menu. It renders the
  PendingDecision to a markdown menu and posts it the same way. The menu is the
  finished user-facing artifact (numbered options, one-line reasons, "reply with
  a number"), so posting it raw is intentional — running it through the agent
  would paraphrase and break the format. The user's pick is caught downstream by
  the DecisionConsumer hook before it reaches the LLM.

### NudgeInjector (`sentinel/executor/injector.py`)

`action == nudge_inject` queues the message and is consumed as the agent's
`response_modifier`: `NudgeInjector.__call__(session_key, content) -> content`
pops the pending messages for that session and appends them to the agent's
outgoing reply. The agent loop applies this in its `after_send` chain and skips
it for system-origin turns so a proactive reply does not get a nudge layered on
top of itself.

Constraints: a TTL drops stale entries, a per-session cap evicts the oldest
beyond it (FIFO), and the pending queue is persisted through the shared
`JsonStateStore` so the REPL and gateway do not double-consume or lose an inject.

The seam is a plain `Callable[[str, str], str]`: the agent loop does not import
the Sentinel, and the Sentinel does not import the agent loop.

### DeferManager (`sentinel/executor/defer_manager.py`)

`action == nudge_defer` registers the decision on a priority heap and dispatches
it (via the same nudge routing) once the target session has settled. Settling is
time-based: the session has been idle for a threshold. A maximum wait bounds how
long a deferred decision can linger before it expires. The heap is persisted
through the shared `JsonStateStore`, so pending defers survive a restart (the
optional on-dispatch callback is the only thing not serialized, and it is just
instrumentation).

---

## 7. The spawn_agent path: ProactiveSpawn (`sentinel/executor/spawn.py`)

`action == spawn_agent` wraps `SubagentManager.spawn(...)` to run an independent
micro-agent for a multi-step task (a digest, a status check). `dispatch()`:

1. validates `action == spawn_agent` and a non-empty `spawn_task`;
2. passes its own NudgePolicy check, reusing the shared quota and dedup (keyed on
   the spawn task as the content hash);
3. splits the target session into channel and chat_id;
4. spawns the micro-agent; the result is delivered back through the
   NudgeDispatcher to the originating channel.

Spawn uses its own quota line (so it does not steal the reactive-nudge quota) but
shares the same dedup so the same spawn task does not repeat in a short window.

---

## 8. Feedback loop: NudgeFeedbackTracker + the nudge-feedback tool

The NudgeFeedbackTracker (`sentinel/feedback/tracker.py`) records each nudge's
lifecycle (dispatched / accepted / dismissed / neutral) to a JSONL log, and the
runner reads its recent acceptance rate each tick to retune the NudgePolicy
multiplier.

Verdicts come from a tool (`sentinel/tools/`): when the user replies, the main
LLM can call `nudge_feedback(verdict, nudge_id, reason)` to mark a dispatched
nudge accepted, dismissed, or neutral. This costs no extra LLM call (the main
turn runs anyway) and avoids the brittle "default to accepted unless the user
said stop" heuristic that miscounted an explicit "stop reminding me".

A per-turn session key is published by the user-inbound hook through a context
variable so the tool can find the current session without changing the agent's
tool-execute signature.

---

## 9. State files

The subsystem persists its derived state rather than rebuilding it each tick.

- `attention.md` (under `user_memory/`): a set of producers
  (`sentinel/attention_producers/`) each own one H2 section, maintained each
  tick by the `AttentionUpdater` (`sentinel/attention_updater.py`) in two phases
  (compute out of lock, splice in under a file lock), with a compare-and-skip so
  a cold tick does not write, and per-producer failure isolation. A daily
  analysis service (`sentinel/predictor/daily_analysis.py`) makes one LLM call a
  day whose result several producers share. Most producers are pure algorithm;
  the LLM-backed ones and the daily analysis are off by default.
- `behaviors.md` (under `user_memory/`): an idle-triggered LLM extractor turns
  session messages into structured behavior events; the Planner reads a folded
  window of the tail. The extractor is off by default.
- `state.json` (under the sentinel data dir): the runtime ledger shared by the
  NudgePolicy, NudgeInjector, and DeferManager, written under one `fcntl` lock
  with atomic rename.

---

## 10. Cron (`schedulers/cron/`)

The CronService (`schedulers/cron/service.py`) runs its own timer loop and
persists jobs to an `fcntl`-locked `jobs.json`. Its sleep-until-next-wake is
capped so it picks up jobs written by a peer process (the store reloads on mtime
change). Each fire is claimed with a pid and timestamp under the lock (with a
stale-claim TTL) so two processes do not double-fire the same job, and a channel
filter lets a REPL avoid stealing a job destined for a real channel.

When a job fires, the `on_cron_job` callback (`raven/cli/_cron_handler.py`)
submits a `CRON`-origin `TurnRequest` to the spine, bound to the `cron:<job_id>`
conversation. Delivery is explicit per branch: a single-target delivering job
rides the hub to its one outlet; a broadcast or a silent job submits with the
job's own (ephemeral) channel as the source so the hub drops the reply, and a
broadcast then delivers the reply explicitly to every resolved target. Delivery
targets are resolved at trigger time (so a `cron config set` takes effect on the
next fire): a real channel passes through, an ephemeral one (cli / tui) expands
to the configured forward channels, and the chat_id is looked up from the most
recent session for each channel.

A cron fire also writes the shared NudgePolicy ledger (a topic-tag fire plus a
dispatched record marked neutral) so the Sentinel suppresses its own proactive
nudge on the same topic within the dedup window, without dragging down the
acceptance rate the Sentinel learns from (a cron is user-initiated, not the
Sentinel's own proposal). A recurring job that fires repeatedly with no user
response auto-decays after a strike limit, to contain a runaway "every few
minutes forever" job.

---

## 11. Heartbeat and event-driven wake

The HeartbeatService (`schedulers/heartbeat/service.py`) is a separate
timer-driven service. Phase one reads a `HEARTBEAT.md` and asks the LLM, via a
virtual tool call, whether there are active tasks (avoiding free-text parsing);
phase two, only on a `run` decision, executes through the full agent loop and
delivers the result.

Event-driven wake (`raven/proactive_engine/wake.py`) coalesces wake requests
into early heartbeat ticks. Producers (a cron completion, a subagent completion,
a manual trigger) call `request_wake_now`, and the heartbeat loop waits on the
scheduler's wake event instead of a bare sleep, so a wake simply ends the
current sleep early. A rate guard spaces consecutive fires, and while the agent
is busy with user messages the wake is parked and re-fired from the agent loop's
turn-complete callback. Wake drives the HeartbeatService only — the
SentinelRunner keeps its own independent tick loop and does not reference the
wake scheduler. The trigger store (`sentinel/discover_triggers.py`) is a
separate file-based IPC (modeled on the cron jobs file) the runner drains on its
own short-cadence loop, for an operator-initiated discovery run.

---

## 12. Task discovery: anticipatory menus

Alongside the reactive nudge path, a daily task-discovery batch proposes a menu
of candidate tasks for the user to pick from. The pipeline:

- TaskDiscoverer (`sentinel/predictor/task_discoverer.py`): triggered once a day
  by the runner's tick (a time guard, sharing the `sentinel.enabled` lifecycle),
  it reads recent memory and history, produces a `PendingDecision` with a few
  options, gates it through the NudgePolicy, and dispatches the formatted menu
  via `NudgeDispatcher.dispatch_options`.
- PendingDecisionStore (`sentinel/executor/pending_decision.py`): an
  `fcntl`-locked JSON store with TTL, awaiting-confirm state, and supersede
  semantics.
- DecisionRouter (`sentinel/executor/decision_router.py`): watches user replies,
  matching a number / `/pick N` deterministically with an LLM classifier
  fallback (above a confidence threshold). A match consumes the reply so it does
  not reach the agent loop.
- DecisionConsumer (`sentinel/executor/decision_consumer.py`): turns a matched
  pick into an ActionExecutor call, optionally behind a confirm step.
- ActionExecutor (`sentinel/executor/action_executor.py`): executes a `reply`
  (through the injector), a `tool` (through the agent's tools), or a `spawn`
  (through ProactiveSpawn). Each path passes the NudgePolicy and records the
  fire.

The user's pick is short-circuited in the agent loop's hook chain (the
DecisionConsumer adapter) before the LLM is called.

---

## 13. Spine integration and the user-inbound gates

Proactive turns reach the agent the same way a user message does: as a
`TurnRequest` with an `Origin` (`USER`, `SENTINEL`, `CRON`, `HEARTBEAT`,
`SUBAGENT`) submitted to the per-process `Scheduler` (`raven/spine/scheduler.py`),
which routes it to a per-conversation serial `Lane`. Replies and proactive
messages are delivered through the `DeliveryHub` (`raven/spine/delivery.py`).

The agent loop (`raven/agent/loop/main.py`, `run_turn`) reads `req.origin` to
gate two things:

- The user-inbound hooks (engagement detection, the discovery-menu consumer) run
  only for genuine user input. `SENTINEL` and `SUBAGENT` turns are not real user
  input, so they are gated out.
- The `after_send` chain (the NudgeInjector / response-modifier) is skipped for
  origins whose output is system-generated, so a proactive reply does not get a
  nudge layered onto it. This is a separate gate from the user-inbound one, even
  though their members coincide today, because they mean different things.

A cron turn additionally guards the cron tool (via a context variable set in the
lane task) so the agent cannot schedule new cron jobs mid-run.

### Mid-turn user input (BusyPolicy.INJECT)

When a user message arrives while a turn is already running for that
conversation, the gateway inbound dispatch
(`raven/cli/gateway_commands.py`) detects the in-flight turn
(`Scheduler.has_inflight`) and submits the message with `BusyPolicy.INJECT`
instead of queuing a fresh turn. The lane holds the inject in a mailbox; the
running turn's loop calls `drain()` at the top of each tool-loop iteration and
merges the pending injects as user turns before the next LLM call
(`raven/spine/scheduler.py`, `raven/agent/loop/main.py`). An inject the
turn never drains falls back to a fresh appended turn, so nothing is lost.
`INJECT` and `INTERRUPT` are user-only; a proactive origin requesting either is
demoted to `APPEND`.

### ask_user — pausing a turn to ask the user

The `ask_user` tool (`raven/agent/tools/ask_user.py`) pauses a turn to ask the
user a structured question and awaits the reply. It hands the turn's
conversation_id and prompt to a `QuestionBroker`
(`raven/tui_rpc/question_broker.py`), which emits a `clarify.request`
notification and blocks (on a future keyed by conversation_id) until an answer
arrives, with a fail-safe default so the loop always gets a string back.
`clarify.request` / `clarify.respond` is the ui-tui frontend's existing
multi-choice prompt contract (ClarifyPrompt), which the broker reuses.

The answer reaches the broker by two routes:

- TUI: the frontend renders the ClarifyPrompt and answers with a
  `clarify.respond` RPC, handled in `raven/tui_rpc/methods/question.py`,
  which calls `broker.reply(...)`.
- Channel: the broker renders the question as an outbound Text to the
  conversation's channel; the gateway inbound dispatch, on the next message for a
  conversation with a pending question (`broker.pending_req(cid)` is set), routes
  that message to `broker.reply(cid, text)` instead of starting or injecting a
  turn (`raven/cli/gateway_commands.py`).

Because a turn is serial, at most one question is pending per conversation; an
overlapping question fail-safes the stale one.

A turn blocked on `ask_user` holds its lane and one user concurrency slot
(`OriginPools(user=...)`) until the answer arrives or the broker's timeout
fail-safe fires. With the gateway's pool of 4, three pending questions still
leave a free slot; only four conversations blocked at once exhaust the pool.
The default timeout bounds the worst case, so a forgotten question cannot wedge
the gateway indefinitely.

The `ask_user` tool schema accepts `multiple` / `custom` per question, but the
`clarify.request` wire payload carries only `{question, choices}`, so the
frontend ClarifyPrompt renders a single-select prompt — multi-select and
free-form answers degrade to a single choice for now.
