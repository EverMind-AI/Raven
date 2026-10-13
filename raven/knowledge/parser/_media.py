"""The media types this package's parsers are routed by, stated rather than looked up.

`mimetypes.guess_type` answers from the host: Python's built-in table plus
whatever `/etc/mime.types` and its siblings happen to carry. On a developer's
machine that is a full Apache table and every format here resolves. On a slim
container image there is no such file at all, and the built-in table -- which
predates OOXML -- has no answer for `.docx`, `.pptx` or `.xlsx` while still
answering for `.doc` and `.ppt`. `.rtf` moves with the interpreter: nothing on
3.12, `text/rtf` on 3.13, and neither of those is the `application/rtf` the
text parser claims.

Both things that reach for a media type then go wrong, in opposite directions:

* routing. `KnowledgeManager` falls back to `text/plain` for an unknown type,
  so a Word document was handed to the text parser, which decoded the zip
  container as UTF-8 and failed on the first byte that is not a character.
  The reader sees "codec can't decode byte 0x91" against a file nothing is
  wrong with;
* the file picker. `ParserBase.supported_extensions` reverses the same table
  with `guess_all_extensions`, so the formats missing from it were missing
  from the `accept` list as well.

A parser shipped in this package can say what it reads without asking the
machine it is running on. Registering is the fix rather than a private lookup
because both of the above go through `mimetypes`, and the module is built to
be extended this way.
"""

from __future__ import annotations

import mimetypes

#: Extension to media type, for every format this package parses. Ordered so
#: the first extension registered against a type is the one `guess_extension`
#: gives back for it.
MEDIA_TYPES: "tuple[tuple[str, str], ...]" = (
    (".pdf", "application/pdf"),
    (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    (".doc", "application/msword"),
    (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    (".xls", "application/vnd.ms-excel"),
    (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
    (".ppt", "application/vnd.ms-powerpoint"),
    (".rtf", "application/rtf"),
    (".txt", "text/plain"),
    (".md", "text/markdown"),
    (".markdown", "text/markdown"),
    (".csv", "text/csv"),
    (".html", "text/html"),
    (".htm", "text/html"),
    (".rst", "text/x-rst"),
    (".json", "application/json"),
    (".xml", "application/xml"),
    (".yaml", "application/x-yaml"),
    (".yml", "application/x-yaml"),
    (".png", "image/png"),
    (".jpg", "image/jpeg"),
    (".jpeg", "image/jpeg"),
    (".gif", "image/gif"),
    (".webp", "image/webp"),
    (".bmp", "image/bmp"),
    (".tif", "image/tiff"),
    (".tiff", "image/tiff"),
)


def register() -> None:
    """Teach `mimetypes` the formats this package reads.

    Idempotent, and additive rather than corrective: a host that already maps
    one of these maps it to the same type, so this changes nothing there and
    supplies it where the host has nothing. Called at the import of
    :mod:`raven.knowledge.parser`, which is below every caller that guesses.
    """
    # No `init()` first: `add_type` initialises the table itself when nothing
    # has yet, and calling it here would reset a table some other caller had
    # already pointed at files of its own.
    for suffix, media_type in MEDIA_TYPES:
        mimetypes.add_type(media_type, suffix)
