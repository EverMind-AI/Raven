"""The original file behind a knowledge document, for the page's viewer.

A knowledge base keeps the bytes it was given, and the page shows them: a
reader checking why a chunk says what it says wants the document, not a
paraphrase of it.

Addressed by document id, never by path. The blobs live under raven's state
directory, which ``rpc.files.resolve_readable`` refuses on purpose -- that
directory also holds provider credentials and ``serve.json``, whose token mints
session nonces, and the viewer's path policy exists to keep a page-chosen path
away from all of it. An id sidesteps the question rather than weakening the
answer: nothing the page sends names a location, the record says where the
bytes are, and the handle is served instead of the request's own path.

Two things the record knows that the stored copy does not. Blobs are written as
``blobs/<document_id>`` with no suffix, so the file on disk cannot say what it
is -- while ``source`` keeps the name it was uploaded under and ``media_type``
what that name meant. Both the content type and the CSP sandbox are decided
from that name for exactly this reason: read off the blob they would come back
as ``application/octet-stream`` with no ``allow-scripts``, which serves a PDF
as a download and renders it blank in the frame.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

from raven.rpc import pdf_preview

if TYPE_CHECKING:
    from raven.knowledge._types import KnowledgeDocumentRecord


class DocumentMissingError(LookupError):
    """No document under that id, or its stored copy is gone."""


def resolve(document_id: str) -> tuple["KnowledgeDocumentRecord", Path]:
    """The record and the file behind it, or raise.

    The two are looked up together because either alone is a lie: a record
    whose blob was swept names a file that is not there, and a blob with no
    record has no name, no type and nobody to say which base it belonged to.
    """
    from raven.rpc.methods.knowledge import knowledge_manager

    if not document_id:
        raise DocumentMissingError("no document id")
    manager = knowledge_manager()
    record = manager.get_document(document_id)
    if record is None:
        raise DocumentMissingError(f"no document {document_id!r}")
    path = manager.document_path(document_id)
    if path is None:
        raise DocumentMissingError(f"the stored copy of {record.source!r} is missing")
    return record, path


def named(record: "KnowledgeDocumentRecord") -> Path:
    """The upload's own filename, as a path to ask the header helpers about.

    Not a file anyone opens -- ``content_type_for`` and ``sandbox_for`` read
    only the suffix, and this is how the blob borrows the name it was stored
    without. Going through those two rather than mapping media types here keeps
    one answer to "what is this and may it run scripts" for every surface that
    serves a file.
    """
    return Path(record.source or "document")


def is_renderable(record: "KnowledgeDocumentRecord") -> bool:
    """Whether a PDF rendering is on offer for this document."""
    return pdf_preview.is_renderable(named(record))


async def pdf_for(record: "KnowledgeDocumentRecord", blob: Path) -> Path:
    """A PDF rendering of the stored copy.

    LibreOffice is handed a suffixed alias rather than the blob. It decides the
    input filter partly from the extension, and a legacy ``.doc`` arriving as an
    extensionless file is exactly the case its sniffing is worst at -- the
    conversion fails, or worse, succeeds as the wrong format.

    A hard link, so the alias is the same inode: ``cache_key`` reads size and
    mtime, and a copy would change neither by accident but would double the
    bytes on disk for every preview. The fallback is a copy, for the case the
    cache and the blobs are on different filesystems.
    """
    alias = _alias_for(record, blob)
    return await pdf_preview.pdf_for(alias)


def _alias_dir() -> Path:
    # Under the pdf-preview cache but not in its root: the root holds
    # ``<key>.pdf`` outputs, and ``published_pdf`` looks for a sibling PDF
    # beside whatever it is given. An alias sitting next to those would let one
    # document's rendering answer for another's.
    return pdf_preview.cache_dir() / "sources"


def _alias_for(record: "KnowledgeDocumentRecord", blob: Path) -> Path:
    suffix = named(record).suffix
    alias = _alias_dir() / f"{record.id}{suffix}"
    if alias.is_file() and alias.stat().st_mtime == blob.stat().st_mtime:
        return alias
    alias.parent.mkdir(parents=True, exist_ok=True)
    alias.unlink(missing_ok=True)
    try:
        os.link(blob, alias)
    except OSError:
        shutil.copy2(blob, alias)
    return alias


def forget(document_id: str) -> None:
    """Drop the retained source copy for one document, if there is one.

    There is one whenever an office file has been previewed: rendering needs a
    path whose suffix says what the bytes are, and a blob is stored under a
    bare id. The copy is a hard link where the filesystem allows it and a full
    second copy where it does not, so leaving it behind after a delete keeps
    the document's content on disk under a name nothing lists -- and for a
    copy, its bytes as well.

    Matched on the id rather than on the name the record carries, because a
    note that was renamed leaves an alias under the old suffix, and the point
    of this call is that nothing is left.
    """
    directory = _alias_dir()
    if not directory.is_dir():
        return
    for alias in directory.glob(f"{document_id}.*"):
        try:
            alias.unlink()
        except OSError:
            # A sweep will take it later; failing a delete over its cache copy
            # would leave the reader with a row they cannot be rid of.
            logger.debug("knowledge: could not remove the retained source {}", alias)


__all__ = ["DocumentMissingError", "forget", "is_renderable", "named", "pdf_for", "resolve"]
