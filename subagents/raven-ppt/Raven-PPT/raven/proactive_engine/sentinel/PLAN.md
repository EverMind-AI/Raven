# Sentinel — obsolete plan

This file used to hold a plan for an EventBus-based passive-monitoring skeleton
(a set of Monitors, a two-tier Evaluator, and a NudgePolicy that injected
`InboundMessage`s onto an in-memory message bus). None of that was built, and
the machinery it described — the message bus, `InboundMessage` / `OutboundMessage`,
`bus.consume_inbound`, the `Monitor` / `NudgeAction` / `NudgePolicyConfig`
interfaces — no longer exists in the codebase.

For the design and the as-built reference of the proactivity subsystem, see:

- `docs/Proactivity-Plan.md` — design intent (what the system should be).
- `docs/Proactivity-Implementation.md` — as-built reference (what the code is),
  with module paths under `raven/proactive_engine/`.

## Current state (one paragraph)

The proactivity subsystem now lives under `raven/proactive_engine/`. The
`SentinelRunner` (`sentinel/executor/runner.py`) wakes on its own interval,
assembles a `PlannerContext` (`sentinel/predictor/context_assembler.py`), and a
single LLM call from `ProactivePlanner` (`sentinel/planner.py`) returns one
structured decision: `skip`, `nudge`, `nudge_inject`, `nudge_defer`, or
`spawn_agent`. Every nudge passes the shared `NudgePolicy`
(`sentinel/trigger_policy/policy.py`) before an executor
(`sentinel/executor/`) carries it out. Delivery and turn transport are the
spine, not a bus: a plain nudge is posted to the `DeliveryHub`
(`raven/spine/delivery.py`), and the proactive sources — sentinel, cron
(`schedulers/cron/`), and heartbeat (`schedulers/heartbeat/`) — reach the agent
as origin-tagged `TurnRequest`s submitted to the `Scheduler`
(`raven/spine/scheduler.py`). The whole subsystem is off by default
(`sentinel.enabled=false`).
