"""Proactive Engine — L3 Cognition-Coord home for self-initiated triggers.

Per the EverBrain plan this package unifies the three families of
"agent wakes itself up" sources under one roof:

- ``proactive_engine/schedulers/cron/``       — wall-clock + recurring jobs.
- ``proactive_engine/schedulers/heartbeat/``  — fixed-interval HEARTBEAT.md
                                                 check.
- ``proactive_engine/schedulers/events/``     — event-driven (filesystem,
                                                 webhook…) sources. Stub only
                                                 for now; populated later.
- ``proactive_engine/sentinel/``              — LLM-based proactive planner.
- ``proactive_engine/core/``                  — ``TriggerEvent`` data class
                                                 + ``TriggerDispatcher``
                                                 facade + ``Scheduler`` /
                                                 ``Sentinel`` ABCs.

External callers should import from the sub-package paths directly.
"""
