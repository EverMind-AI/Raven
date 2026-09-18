"""What went wrong during a parse that did not stop it.

A parser has two kinds of bad news and only one way to report it. A file it
cannot read at all is an exception, which fails the document and puts the
reason on its row. A *part* of a file it could not read is neither: the
document is still worth indexing, and raising would throw away the ninety
paragraphs that parsed to report the one picture that did not.

Before this, that second kind went to the log. The document indexed, the row
said ready, and nothing anywhere said that half its figures are missing from
what a search can reach -- which is the failure mode a reader cannot discover
by looking, because the file is searchable and the answers are merely worse.

So a parse collects notes, and the manager writes them onto the document beside
its status. A context variable rather than a return value because
``ParserBase.parse`` answers with sections: threading a second channel through
every parser, every caller and every test would be a large change to say one
small thing, and a parser deep in a figure loop is the only place that knows.

Mutating the list is what crosses a task boundary, not setting it: a coroutine
started with ``asyncio.gather`` copies the context, so it sees the same list
object and appends to it, while a ``set`` inside it would be invisible here.
That is exactly the shape the figure loop needs.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_notes: ContextVar["list[str] | None"] = ContextVar("knowledge_parse_notes", default=None)


@contextmanager
def collecting() -> Iterator[list[str]]:
    """Collect the notes written while parsing, and hand them back.

    Reentrant by way of the token: a nested parse (a ``.doc`` converted and
    handed to the Word parser) restores the outer collector rather than
    clearing it.
    """
    notes: list[str] = []
    token = _notes.set(notes)
    try:
        yield notes
    finally:
        _notes.reset(token)


def note(message: str) -> None:
    """Record one thing the reader should know about this parse.

    Does nothing outside :func:`collecting`, so a parser called directly -- by
    a test, by a script -- behaves exactly as it did before notes existed.

    Deduplicated, because the natural way to write one of these is inside a
    loop over figures, and "a picture could not be read" said forty times is a
    row nobody can read.
    """
    bucket = _notes.get()
    if bucket is not None and message and message not in bucket:
        bucket.append(message)


def joined(notes: list[str], *, limit: int = 3) -> str:
    """The notes as one line for a document row.

    Capped, with the rest counted: this lands in a tooltip, and a file whose
    every figure failed for a different reason would otherwise put a paragraph
    there.
    """
    if not notes:
        return ""
    if len(notes) <= limit:
        return " ".join(notes)
    return " ".join(notes[:limit]) + f" (+{len(notes) - limit} more)"


__all__ = ["collecting", "joined", "note"]
