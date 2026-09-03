"""Scheduler family — no-LLM triggers that wake the agent on a clock or
event signal.

Two sub-packages, each independently selectable in config:

- ``cron``      — wall-clock + recurring jobs persisted to jobs.json.
- ``heartbeat`` — fixed-interval HEARTBEAT.md inspection.
"""
