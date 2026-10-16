"""The long form of a failed turn's account, for a reader who expands its row.

A failed turn already has a short account: the lane bounds it to 200 characters
(``raven.spine.events.bound_failure_text``) because a chat reply, a cron record
and the session marker's ``reason`` all carry it. This module builds the long
form beside it: the same words unbounded, with the credential shapes a provider
body can carry replaced, capped at the ceiling a call record's body already has.

The kernel cannot build it -- ``raven.spine`` imports no other raven package --
so the runner does, and hands it to the lane as a ``full_detail`` attribute on
the exception it lets out (``attach_full_detail``). The lane copies it onto
``TurnFailed.full_detail``; ``_save_broken_turn`` files it on the marker.
"""

from __future__ import annotations

from raven.contracts.llm_provider import CallRecord
from raven.providers.call_record import MAX_BODY_CHARS, exception_body, scrub_credentials
from raven.security.redact import redact
from raven.spine.scheduler import describe_failure, full_detail_of
from raven.spine.turn import AnswerlessTurnError

_ELLIPSIS = "..."

#: How much of a failure text the scrubbers read. Both carry patterns whose cost
#: grows faster than the text (an unterminated PEM header, a dot-free run after
#: ``eyJ``), and this runs on the event loop inside a failing turn's except arms.
#: The window ends far past the cap, so a credential the cut splits is cut off
#: again by the cap.
_SCRUB_WINDOW = 4 * MAX_BODY_CHARS


def full_failure_text(text: str) -> str:
    """``text`` with credentials scrubbed, capped at ``MAX_BODY_CHARS``.

    Two passes, because the text is either a provider body or an arbitrary crash
    message: the call record's body scrubber knows a JSON-quoted ``Authorization``
    value, and ``raven.security.redact`` knows the shapes a crash message carries
    (a key in a URL, userinfo, ``password=``, vendor tokens). Scrubbed before the
    cap, so a credential straddling the cut cannot survive as a fragment.
    """
    text = redact(scrub_credentials(text.strip()[:_SCRUB_WINDOW]))
    if len(text) <= MAX_BODY_CHARS:
        return text
    return text[: MAX_BODY_CHARS - len(_ELLIPSIS)].rstrip() + _ELLIPSIS


def exception_failure_text(exc: BaseException) -> str:
    """``exc``'s account worded the way ``describe_failure`` words it, without its 200-character cut.

    An HTTP error's body (``call_record.exception_body``) follows the message,
    because a client library can keep the provider's whole answer there while
    its message holds one line.
    """
    name = type(exc).__name__
    if isinstance(exc, AnswerlessTurnError):
        return full_failure_text(full_detail_of(exc) or str(exc).strip() or name)
    text = str(exc).strip()
    if not text:
        head = name
    elif text.startswith(f"{name}:") or text.startswith(f"{name} ") or text == name:
        head = text
    else:
        head = f"{name}: {text}"
    body = (exception_body(exc) or "").strip()
    if body and body not in head:
        head = f"{head}\n\n{body}"
    return head if head == name else full_failure_text(head)


def response_failure_text(content: str | None, record: CallRecord | None) -> str | None:
    """A failed model call's long account: its content, then the body its call record kept.

    The content is the canonical sentence, whose detail was cut where it was
    built; the body is the provider's own response (``call_record.from_exception``),
    already scrubbed and capped. Either may be missing.
    """
    head = (content or "").strip()
    body = ((record.body if record is not None else None) or "").strip()
    if body and body not in head:
        head = f"{head}\n\n{body}" if head else body
    return full_failure_text(head) if head else None


def attach_full_detail(exc: BaseException) -> str | None:
    """Hand the lane ``exc``'s long account when it says more than the short one.

    Set as ``exc.full_detail`` -- None when the bounded text already is the whole
    account, so the row offers nothing to open -- and returned. Recomputed from
    the exception on every call, so the turn path's call and the runner's file the
    same text, and a value some library put there is overwritten (an exception that
    refuses the attribute keeps whatever it had).
    """
    long = exception_failure_text(exc)
    detail = long if long != describe_failure(exc) else None
    try:
        setattr(exc, "full_detail", detail)
    except (AttributeError, TypeError):
        pass
    return detail
