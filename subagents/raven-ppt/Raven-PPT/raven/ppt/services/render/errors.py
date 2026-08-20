"""Why a render did not happen.

Three cases, kept apart because callers do different things with each. A missing
binary is a deployment fact and no amount of retrying fixes it, so the stage that
hits it tells the author what the deck can still be checked for. A timeout is
worth one retry. A converter that ran and produced nothing is the interesting
one, and its `detail` carries the exit code and the log tails, because that is
the only place the reason exists.
"""

from __future__ import annotations

from typing import Any

# Enough to carry a LibreOffice complaint, short enough that a tool result stays
# readable when it is quoted back to a model.
LOG_TAIL_CHARS = 800


class RenderError(RuntimeError):
    """A render that was attempted and failed."""

    code = "render_failed"
    retryable = False

    def __init__(self, message: str, *, detail: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail or {}

    def as_detail(self) -> dict[str, Any]:
        """Structured evidence, for a Finding's `detail` or a tool error."""
        return {"code": self.code, "message": self.message, "retryable": self.retryable, **self.detail}


class RenderUnavailableError(RenderError):
    """A tool the chain needs is not installed."""

    code = "renderer_unavailable"


class RenderTimeoutError(RenderError):
    """The converter outlived its budget and was killed."""

    code = "render_timeout"
    retryable = True
