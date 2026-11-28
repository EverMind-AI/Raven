"""The field names a persisted row files the form it was sent under.

They live here rather than beside the runtime-context reader that defines what
they mean, because the token estimator has to count them and
``raven.context_engine`` imports the estimator: the constants are below both, so
neither has to reach across for the other.
"""

WIRE_CONTENT_FIELD = "wire_content"
"""The body a user or tool row was sent with, when the display content differs.

``content`` keeps what a reader sees (the user's words, the filed image note);
this keeps the body the model received (an attachment note, a hook rewrite, the
live withdrawn-picture note, a tool body the save cap would shorten). The
history projection replays it instead of ``content``.
"""

WIRE_AFTER_FIELD = "wire_after"
"""Provider-safe rows a recovery appended after a kept row.

The empty placeholder, the nudge, the continuation: the model was shown them,
but they are scaffolding and never become display rows. They ride the kept row
they followed and the history projection inserts them back in place.
"""

__all__ = ["WIRE_AFTER_FIELD", "WIRE_CONTENT_FIELD"]
