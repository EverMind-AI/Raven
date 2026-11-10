"""Whether a failed call is the upstream refusing to be told it must call a tool.

A vendor can accept a tool list and still reject a forcing ``tool_choice``:
DeepSeek's thinking mode answers one with a 400, "Thinking mode does not
support this tool_choice". No table predicts it -- the same model takes the
forced call with thinking off, and behind a gateway it depends on which host
served the request.

One question, asked from the shared retry ladder so every wire and every
caller gets the same answer: the refusal is read off the live exception, where
a gateway's wrapped upstream body is still attached, and under ``auto`` the
tool stays on offer, which every caller that forces a choice already handles.

Nothing is remembered between calls. The refusal turns on the request, not
only the model, and a 400 can name the field for a reason of the caller's own
making; a process-wide switch would take forcing away from requests that were
never refused, for the price of one resend.
"""

from __future__ import annotations

import re
from dataclasses import replace
from typing import Any

from raven.contracts.llm_provider import ErrorClassification
from raven.providers import call_record
from raven.providers.prompt_cache import error_text, is_refused_request

_REFUSAL_MARKER = "tool_choice"

#: The status an adapter writes into the error it raises ("HTTP 500: ..."),
#: which is the only place it survives once the response object is gone.
_RENDERED_STATUS = re.compile(r"\bhttp (\d{3})\b")


def is_forcing(tool_choice: str | dict[str, Any] | None) -> bool:
    """Whether this choice demands a tool call, in any wire's spelling."""
    if isinstance(tool_choice, dict):
        return tool_choice.get("type") in {"function", "tool", "any"}
    return tool_choice in ("required", "any")


def is_rejection(error: object, status: int | None = None) -> bool:
    """Is this error the upstream refusing a forcing ``tool_choice``?

    Narrow on purpose: the field has to be named and the request has to have
    been refused as malformed. A known status that is not 400 is never a
    refusal -- a gateway can echo the request body into a 5xx, and relaxing
    there would drop the forced call on a transient fault.
    """
    if status is not None and status != 400:
        return False
    text = error_text(error)
    if _REFUSAL_MARKER not in text and isinstance(error, BaseException):
        # A gateway wraps its upstream's refusal ("Provider returned error") and
        # keeps the upstream's own words in the body, not in the message.
        text = f"{text} {(call_record.exception_body(error) or '').lower()}"
    if _REFUSAL_MARKER not in text:
        return False
    rendered = _RENDERED_STATUS.search(text)
    if status is None and rendered and rendered.group(1) != "400":
        return False
    return status == 400 or is_refused_request(text)


def already_relaxed(classification: ErrorClassification) -> ErrorClassification:
    """The verdict for a call whose adapter relaxed the choice itself and still failed.

    The ladder must not offer the tool a second time: that resend is the
    request the adapter just sent. It is told the choice was relaxed instead,
    so an attempt it makes for any other reason starts from ``auto`` rather
    than paying for the refusal again.
    """
    return replace(classification, refuses_forced_tool_choice=False, relaxed_forced_tool_choice=True)
