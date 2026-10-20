"""The outbound guard: a send that cannot happen twice by accident.

Raven's own documentation says the quiet thing out loud: *"A retry can
repeat external side effects."* This package is the answer to that
sentence.

Three terms, and the distinction between them is the whole point:

* **Correlation token** -- a short, quotable handle on one outbound
  message. It goes in the message, and a reply quoting it resolves back to
  the message that sent it.
* **Unresolved send** -- a send that was prepared and whose outcome nobody
  knows. A second attempt at the same subject is refused while one of
  these is outstanding.
* **Ambiguous outcome** -- what an unresolved send is called when it is
  reported. It is surfaced to a person, never auto-resent and never quietly
  forgiven.

See :mod:`raven.outbound.guard` for the mechanism and
:mod:`raven.outbound.token` for the token.
"""

from raven.outbound.guard import (
    NOT_SENT,
    PREPARED,
    SENT,
    UNKNOWN,
    PreparedSend,
    Reconciliation,
    UnresolvedSendError,
    complete_send,
    find_by_token,
    guard_send,
    prepare_send,
    reconcile,
    unresolved,
)
from raven.outbound.token import TOKEN_PREFIX, is_token, new_token, token_from_text

__all__ = [
    "NOT_SENT",
    "PREPARED",
    "SENT",
    "TOKEN_PREFIX",
    "UNKNOWN",
    "PreparedSend",
    "Reconciliation",
    "UnresolvedSendError",
    "complete_send",
    "find_by_token",
    "guard_send",
    "is_token",
    "new_token",
    "prepare_send",
    "reconcile",
    "token_from_text",
    "unresolved",
]
