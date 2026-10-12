"""The purpose a model call is made for, carried to its span as ``llm.purpose``.

Several callers besides the main agent loop ask the session's model a
question of their own -- the watch-work judgement, the title generator, the
memory extractor, the permission judge -- and every one of them lands as an
``llm.call`` span under the same turn. The spans look alike; only the caller
knows which conversation a call belongs to. The caller says so here, in a
context variable the span extractor reads, so the trajectory view can tell
the main dialogue from the side questions without guessing from content.

Nothing is recorded when no caller spoke: an unlabelled span means "not
said", never "main".
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator

MAIN = "main"

_current: ContextVar[str | None] = ContextVar("llm_purpose", default=None)


def current() -> str | None:
    """The purpose set by the nearest enclosing :func:`purpose`, or None."""
    return _current.get()


@contextmanager
def purpose(name: str) -> Iterator[None]:
    """Label every model call made inside the block with ``name``."""
    token = _current.set(name)
    try:
        yield
    finally:
        _current.reset(token)


__all__ = ["MAIN", "current", "purpose"]
