# Raven Proactivity — Design

> From passive notification to anticipatory collaboration: the design of
> Raven's proactivity subsystem.

This document describes the design intent — what the proactivity subsystem is
meant to be and why it is shaped the way it is. Its companion,
`docs/Proactivity-Implementation.md`, is the as-built reference: it maps each
piece to a module path under `raven/proactive_engine/` and the spine
(`raven/spine/`).

---

## Contents

1. [Three layers of proactivity](#1-three-layers-of-proactivity)
2. [The core idea: periodic Planner plus on-demand spawn](#2-the-core-idea-periodic-planner-plus-on-demand-spawn)
3. [Components](#3-components)
4. [Action space](#4-action-space)
5. [Anti-spam: the NudgePolicy gate](#5-anti-spam-the-nudgepolicy-gate)
6. [Delivery and turn transport: the spine](#6-delivery-and-turn-transport-the-spine)
7. [Scenarios](#7-scenarios)
8. [Cost](#8-cost)
9. [Risks and mitigations](#9-risks-and-mitigations)

---

## 1. Three layers of proactivity

A widely used framing splits agent proactivity into three layers, each built on
the one before it:

| Layer | Name | Definition |
|:---:|---|---|
| L1 | Reactive monitoring | An event has happened, is detected, and the agent decides whether to notify the user. |
| L2 | Predictive / routine-based | Recurring patterns are learned from the user's history and acted on before the user asks. |
| L3 | Anticipatory / contextual | The agent reasons over the user's current situation, infers a latent need, and acts. |

A system that stops at L1 is felt as a notifier, not a collaborator. Raven's
subsystem reaches L2 and L3 while staying conservative by default.

Design principles that follow from this:

1. Three-way decisions, not binary: stay silent, send a short message, or run a
   multi-step task — not just notify-or-not.
2. A per-user model: proactivity preferences must vary by user, not be a single
   global setting.
3. A feedback loop: every nudge collects a signal that tightens or loosens
   future behavior.
4. Low intrusion first: conservative defaults, gradual takeover; the quality of
   the first few proactive messages decides whether the user keeps the feature.
5. Content quality over frequency: one precise proactive message beats ten
   generic notifications.

---

## 2. The core idea: periodic Planner plus on-demand spawn

The subsystem does not run a second agent loop and does not subscribe to an
event stream. Instead it wakes periodically, reads a packaged context, and makes
one structured LLM decision per tick. When a decision needs multi-step
execution, it spawns a micro-agent through the existing `SubagentManager` rather
than reimplementing a loop.

This keeps the cost bounded (one LLM call per tick, a tail-bounded spawn) while
still covering routine automation (L2) and multi-step anticipation (L3). The
periodic Planner mirrors the shape of the heartbeat service
(`raven/proactive_engine/schedulers/heartbeat/service.py`), and execution
reuses the subagent machinery rather than a bespoke runtime.

The two sources of proactivity are:

- Sentinel — the LLM decides each tick whether and how to reach the user.
- Cron — the user explicitly schedules a reminder.

Both reach the agent as origin-tagged turns through the spine, and both pass the
same `NudgePolicy` ledger so the two surfaces never double-remind the user on
the same topic.

---

## 3. Components

### ProactivePlanner — periodic reasoner

Wakes on its own interval, reads a single packaged context, and makes one LLM
call that returns a structured decision. It is a pure function of its inputs:
it has no side effects and never raises — any failure degrades to `skip`.

Its only input is the assembled `PlannerContext`: the user's long-term memory, a
tail of recent history, currently active sessions, learned routines, calendar
entries, the current `NudgePolicy` state, the previous tick's decision, a recent
fire history, selected sections of an `attention.md` state file, and a folded
window of recent behavior. The Planner sees nothing else.

### ContextAssembler — input packaging

Aggregates every signal source into the `PlannerContext`, with graceful
degradation per field (a missing source yields an empty value rather than a
crash). This is the single place that decides what the Planner gets to see.

### RoutineLearner — behavior-pattern learning

Mines recurring patterns from the user's history (recency-weighted token
frequency, no LLM required) and emits candidate routines for the Planner to
consume. Candidates are persisted with a confirmation lifecycle: a pattern is
proposed before it is acted on, confirmed by the user, then triggered
automatically; it is paused on rejection and retired after long disuse.

### NudgePolicy — the shared anti-spam gate

Every proactive message — from any executor and from the task-discovery menu —
must pass `NudgePolicy.check()` before it is delivered. It enforces quotas,
quiet hours, cooldowns, content de-duplication, and per-topic limits, and it
learns to tighten or loosen from user feedback. See section 5.

### ProactiveSpawn — multi-step execution bridge

When a decision is `spawn_agent`, this wraps `SubagentManager.spawn(...)` to run
a micro-agent for a multi-step task (for example a status check or a digest),
then routes the result back through the NudgePolicy and the dispatcher. It adds
no new agent loop — only a thin layer for the proactive source tag, result
formatting, and a concurrency/timeout bound on top of the subagent's own
iteration cap.

### Task discovery — anticipatory menus

A daily batch reads recent memory and history, proposes a small menu of
candidate tasks, and posts it for the user to pick from by number. A pick is
caught before it reaches the agent and routed to an executor (reply, tool, or
spawn), optionally behind a confirm step.

---

## 4. Action space

A Planner tick returns exactly one of five actions, validated against a tool
schema so the Planner cannot emit a malformed decision:

- `skip` — nothing worth doing this tick.
- `nudge` — send a standalone message now.
- `nudge_inject` — append the message to the agent's next reply in the target
  session (the user is already in that conversation, so the information extends
  the current thread naturally).
- `nudge_defer` — wait until the target session's current thread settles, then
  send (the user is busy with something else and should not be interrupted).
- `spawn_agent` — dispatch a micro-agent for a multi-step task.

`nudge_inject` and `nudge_defer` are the distinctive ones: they make the agent
aware of what the user is doing right now, rather than only choosing send-now or
not. Their execution paths are described in the implementation reference.

---

## 5. Anti-spam: the NudgePolicy gate

The NudgePolicy is a layered gate with a clean read/write split: `check()` is a
pure verdict, and `record_fired()` writes state only after a successful
delivery. The layers, applied in order, cover:

- quiet hours (a static window plus a per-hour window learned from feedback);
- per-persona do-not-disturb windows;
- per-day and per-hour quotas;
- per-session and per-dismissal cooldowns;
- per-topic acceptance-rate and hard-reject cooldowns;
- content de-duplication within a window;
- a rolling per-topic quota stack (hour / day / week).

A high-priority message can bypass some soft layers, but the hard quotas and
cooldowns hold, and the high-priority bypass is itself withdrawn when the user
has shown low acceptance even of high-priority messages.

The hour quota is scaled by an adaptive multiplier that moves symmetrically with
the user's recent acceptance rate: a highly engaged user can receive more, a
disengaged user fewer. The multiplier is also surfaced to the Planner prompt as
a soft signal so the Planner can raise its own value threshold and avoid LLM
calls that would only be denied.

The policy is personalizable: a `ProactivityPreferencesReader` lets learned user
preferences override the static config, but only in the tightening direction (a
user preference can widen the quiet window, never narrow it).

All of this state is persisted across processes (an `fcntl`-locked atomic-rename
JSON store), so the REPL and the gateway share one ledger and a restart does not
lose quota or cooldown state.

---

## 6. Delivery and turn transport: the spine

There is no message bus. The spine is the sole turn transport and delivery path.

- A turn is submitted as a `TurnRequest` to the per-process `Scheduler`
  (`raven/spine/scheduler.py`), which routes it to a per-conversation serial
  `Lane`. Each request carries an `Origin` — `USER`, `SENTINEL`, `CRON`,
  `HEARTBEAT`, or `SUBAGENT` — that drives concurrency pooling and control
  eligibility (`raven/spine/turn.py`).
- A reply, or any proactive message, is delivered through the `DeliveryHub`
  (`raven/spine/delivery.py`), which routes each deliverable to its channel's
  outlet. A plain nudge is posted to the hub directly (not run back through a
  turn), so the user receives it as a standalone message and the agent cannot
  "act on" a reminder.
- Sentinel, cron, and heartbeat all reach the agent the same way: as
  origin-tagged turns submitted through the spine. Cron's reminder fires as a
  `CRON`-origin turn; the heartbeat wakes as its own service.

`USER`-origin turns get the full user-inbound treatment (engagement detection,
the response-modifier chain that lets `nudge_inject` piggyback). Proactive
system turns that should not be treated as user input, or should not have a
nudge layered onto their own output, are gated out of those hooks by origin.

---

## 7. Scenarios

### L2 — routine automation

The user has checked the weather on the last few Monday mornings. The
RoutineLearner surfaces a candidate routine; on the next Monday-morning tick the
Planner proposes it ("I noticed you check the weather on Monday mornings — want
me to do it automatically?"). Once confirmed, a later Monday-morning tick emits
`spawn_agent` to fetch and summarize the forecast and delivers a concise digest.

### L3 — memory-linked reminder

The user mentioned an SSL certificate expiring at the end of the month. Reading
memory, the Planner reminds the user a week out (`nudge`, medium priority) and,
if no action followed, again two days out at high priority.

### L3 — context-aware resumption

The user was debugging a Redis connection two hours ago and never replied. The
Planner reads the active session, sees what the agent last suggested, and asks a
contextual follow-up ("Did the Redis connection issue get sorted? If
`systemctl start redis` did not help, I can check firewall rules or the bind
address.") — not a context-free "are you still there?".

### L3 — proactive status check

The user deployed to staging and said "let it run for a bit". After a reasonable
interval the Planner emits `spawn_agent` to run a health check and reports the
result — the agent has already looked, rather than reminding the user to look.

---

## 8. Cost

The Planner makes a single bounded LLM call per tick (a small input, a small
structured output). The default tick interval is 30 minutes, and the
RoutineLearner uses no LLM. Spawned micro-agents are the only multi-step cost,
and they are tail-bounded by the subagent iteration cap plus a per-task timeout
and a concurrency limit. The whole subsystem is off by default
(`sentinel.enabled=false`), so an opt-out user pays nothing.

Tail-risk controls:

- a concurrency cap on proactive micro-agents;
- a per-task timeout on each;
- the subagent's own iteration cap;
- the NudgePolicy rate limit, which indirectly bounds spawn frequency.

---

## 9. Risks and mitigations

### Over-notification

If the Planner misjudges and pushes low-value nudges, the user disables the
feature. Mitigations: conservative defaults; the RoutineLearner proposes before
it acts; the adaptive NudgePolicy tightens on low acceptance; the Planner prompt
defaults to `skip`.

### Planner decision quality

A small model can misjudge complex context. Mitigations: the Planner output is a
structured tool call (reliable to parse); the context is kept small to stay in
the model's sweet spot; high-impact actions require at least medium priority; the
Planner model is configurable for users who want a stronger one.

### Spawn safety

An unattended micro-agent could take a damaging action. Mitigations: proactive
spawn is off by default; the subagent runs under workspace restriction with no
messaging or recursive-spawn tools, the iteration cap, and an extra timeout.

### History format drift

The RoutineLearner depends on a timestamped history format. Mitigations: the
history is written by the consolidator under a controlled format, the parser
tolerates deviation, and an unparseable entry is skipped rather than fatal.
