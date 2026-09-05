"""Sentinel — feedback stage.

Captures user engagement with dispatched nudges (NudgeFeedbackTracker)
and persists the cross-process JSON state files (JsonStateStore +
related blobs) so multiple AgentLoop instances on the same workspace
see a consistent view of recent activity / dismissals.

Re-exported from ``raven.proactive_engine.sentinel.__init__`` so
the canonical import paths still resolve via the parent package.
"""
