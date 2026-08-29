"""Proactive Engine — every way the agent wakes itself up.

- ``schedulers/cron/``      — wall-clock and recurring jobs, persisted to
                              jobs.json.
- ``schedulers/heartbeat/`` — the fixed-interval HEARTBEAT.md check.
- ``sentinel/``             — the LLM planner and the pipeline around it:
                              attention producers -> predictor -> trigger
                              policy -> executor -> feedback.
- ``system_events.py``      — the queue a finished background action reports
                              through.
- ``wake.py``               — the scheduler-side wake a parked turn resumes on.

External callers import from the sub-package paths directly.
"""
