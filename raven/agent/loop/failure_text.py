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

from dataclasses import dataclass

from raven.contracts.llm_provider import CallRecord
from raven.providers.call_record import MAX_BODY_CHARS, exception_body, scrub_credentials
from raven.security.redact import redact
from raven.spine.scheduler import describe_failure, full_detail_of
from raven.spine.turn import AnswerlessTurnError

_ELLIPSIS = "..."

#: Bound synchronous work even when a crash carries a very large body. The
#: scrubber treats a private key without closing armour as secret to the end;
#: earlier replacements can move a scan-cut fragment inside the output cap.
_SCRUB_WINDOW = 4 * MAX_BODY_CHARS


@dataclass(frozen=True)
class _AttachedDetail:
    detail: str | None


def full_failure_text(text: str, *, truncated: bool = False) -> str:
    """``text`` with credentials scrubbed, capped at ``MAX_BODY_CHARS``.

    Two passes, because the text is either a provider body or an arbitrary crash
    message: the call record's body scrubber knows a JSON-quoted ``Authorization``
    value, and ``raven.security.redact`` knows the shapes a crash message carries
    (a key in a URL, userinfo, ``password=``, vendor tokens). Incomplete private
    key blocks and recognized credential tails are scrubbed too, including
    those a call record already cut.
    """
    text = text.strip()
    truncated = truncated or len(text) > _SCRUB_WINDOW
    text = redact(scrub_credentials(text[:_SCRUB_WINDOW]), truncated=truncated)
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
    return full_failure_text(head, truncated=bool(record and record.body_truncated)) if head else None


def attach_full_detail(exc: BaseException) -> str | None:
    """Hand the lane ``exc``'s long account when it says more than the short one.

    Set as ``exc.full_detail`` -- None when the bounded text already is the whole
    account, so the row offers nothing to open -- and returned. The turn path
    and runner reuse the same result. The base exception's instance storage
    bypasses foreign getters and setters, including a read-only ``full_detail``;
    the lane reads that same storage rather than invoking a descriptor.
    """
    attributes = BaseException.__dict__["__dict__"].__get__(exc)
    cached = attributes.get("_raven_failure_detail")
    if isinstance(cached, _AttachedDetail):
        detail = cached.detail
    else:
        long = exception_failure_text(exc)
        detail = long if long != describe_failure(exc) else None
        attributes["_raven_failure_detail"] = _AttachedDetail(detail)
    attributes["full_detail"] = detail
    return detail
