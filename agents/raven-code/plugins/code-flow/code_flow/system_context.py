"""Size the product's system addition against the remaining prompt allowance.

The host has already selected history. The allowance includes tools and the
active model's full reply ceiling; the transcript is never shrunk to make room
for product instructions -- when they do not fit, they are truncated, and when
even the notice cannot fit, the turn is ended with the fix named. The splice
itself (and taking the previous iteration's addition back out) is the participant
adapter's job; this module only answers what the addition should say.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from raven.contracts.participant import Answer, Intake
from raven.providers.base import send_max_tokens
from raven.providers.binding import active_binding
from raven.utils.tokens import estimate_prompt_tokens

_TRUNCATED = (
    "\n\n[Repository instructions truncated to fit the context window. "
    "Read the relevant instruction files before changing code.]"
)
_NO_SYSTEM_MESSAGE = "Raven-Code could not find a system message for repository instructions."
_UNFIT = (
    "Raven-Code could not fit repository instructions and workspace notices alongside this conversation "
    "and the model's reply allowance. Shorten the conversation or use a model with a larger context window."
)


def sized_addendum(
    transcript: Sequence[dict[str, Any]],
    tools: Sequence[dict[str, Any]],
    repository: str,
    concurrency: str,
    *,
    window: int | None = None,
    pending_note: str | None = None,
) -> Answer | None:
    """The system addition this call carries, sized to fit, or the reply that ends the turn.

    ``transcript`` is the prompt as the call will send it, without any earlier
    addition of this participant's (the adapter strips that before asking).
    ``pending_note`` is counted in the sizing because it lands on the prompt in
    the same call. Nothing to add answers nothing; a prompt with no system
    message to splice into ends the turn with the fix named.
    """
    if not repository and not concurrency:
        return None
    messages = list(transcript)
    if not messages:
        # A caller with no prompt yet has nothing to splice into and nothing
        # to protect; the missing-seat error is for a real prompt missing its
        # system row.
        return None
    system = next(
        (message for message in messages if isinstance(message, dict) and message.get("role") == "system"), None
    )
    if system is None:
        return Intake(text="", reply=_NO_SYSTEM_MESSAGE)
    content = system.get("content") or ""

    def render(text: str):
        addition = ("\n\n" if content else "") + text
        if isinstance(content, list):
            return [*content, {"type": "text", "text": addition}]
        return content + addition

    binding = active_binding()
    if binding is not None:
        # Production turns carry a binding. A standalone caller without one has
        # no model ceiling to consult, so it retains half of its stated window.
        window = binding.context_window
    allowance = None
    if window:
        reserved = window // 2
        if binding is not None:
            provider = binding.provider
            reserved = send_max_tokens(
                getattr(provider, "generation", None),
                getattr(provider, "wire_model_id", lambda model: model)(binding.model),
                allow_fetch=False,
            )
        allowance = max(0, window - reserved - 32)

    def fits(text: str) -> bool:
        if allowance is None:
            return True
        candidate = [({**message, "content": render(text)} if message is system else message) for message in messages]
        if pending_note:
            candidate.append({"role": "user", "content": pending_note})
        return estimate_prompt_tokens(candidate, list(tools) or None) <= allowance

    tail = ("\n\n" if repository and concurrency else "") + concurrency
    text = repository + tail
    if not fits(text):
        # Keep the complete concurrency warning even when repository text has
        # to shrink. If even the notice cannot fit, do not send an oversized call.
        suffix = (_TRUNCATED if repository else "") + tail
        if not fits(suffix):
            return Intake(text="", reply=_UNFIT)
        lo, hi = 0, len(repository)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if fits(repository[:mid] + suffix):
                lo = mid
            else:
                hi = mid - 1
        text = repository[:lo] + suffix
    return Intake(text=text)
