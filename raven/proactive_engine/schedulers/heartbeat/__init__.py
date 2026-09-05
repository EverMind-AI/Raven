"""Heartbeat service for periodic agent wake-ups.

Relocated from ``raven.heartbeat`` to
``raven.proactive_engine.schedulers.heartbeat``. Internal
implementation is unchanged.
"""

from raven.proactive_engine.schedulers.heartbeat.service import HeartbeatService

__all__ = ["HeartbeatService"]
