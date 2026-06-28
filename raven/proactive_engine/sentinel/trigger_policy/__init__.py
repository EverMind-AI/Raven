"""Sentinel — trigger-policy stage.

Owns the rate-limit / quiet-hours / topic-cooldown / dismissal-cooldown
checks (NudgePolicy + adaptive tuning), the per-user proactivity
preference overrides (ProactivityPreferencesReader +
PersonalizedOverrides), and the Planner LLM prompt that turns a
``PlannerContext`` into a concrete ``PlannerDecision``.

Re-exported from ``raven.proactive_engine.sentinel.__init__`` so
the canonical import paths still resolve via the parent package.
"""
