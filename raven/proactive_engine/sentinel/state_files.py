"""The sentinel state directory's filenames, named once.

core/proactive_stack builds the stores from these and the ``raven sentinel``
CLI introspects the same files; a filename written by hand on both sides is
how an introspection command ends up silently reading a file nothing writes.
"""

STATE_FILENAME = "state.json"
FEEDBACK_FILENAME = "feedback.jsonl"
PENDING_DECISIONS_FILENAME = "pending_decisions.json"
ROUTINES_FILENAME = "routines.json"
DISCOVER_TRIGGERS_FILENAME = "discover_triggers.json"

#: Where the feedback log lived before it moved into the sentinel state dir;
#: the migration source on the build side, the fallback probe on the CLI side.
LEGACY_FEEDBACK_FILENAME = "sentinel_feedback.jsonl"
