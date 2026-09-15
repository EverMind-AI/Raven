"""What knowledge bases and their documents are, and where that survives.

Two stores, deliberately: the vectors live in the collection, and everything a
page needs to *list* one -- names, sizes, indexing state, the error a failed
document stopped on -- lives here as JSON. Splitting them is what lets the
knowledge page answer without touching the index, and what makes an
interrupted index recoverable: a document is a record with a status long
before it is a set of vectors.

One JSON file, written the way the rest of the runtime writes its state: a
temp file and ``os.replace``. The disk under a deployment can reach zero free
mid-write, and a truncating write there does not fail -- it leaves an empty
file where the registry was.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from loguru import logger

DocumentStatus = Literal["pending", "indexing", "ready", "failed"]
#: Which kind of data source a document arrived through. A folder is not
#: one of these: the browser walks it and sends the files, so what lands
#: here is a file like any other.
DocumentOrigin = Literal["file", "note", "url"]

#: What a knowledge base is configured with until somebody changes it. Named
#: here, beside the record that holds them, because the handlers that read a
#: base written before these fields existed have to fall back on the same
#: numbers -- and three copies of a default are three chances to disagree.
DEFAULT_TOP_K = 6
DEFAULT_CHUNK_SIZE = 2048
DEFAULT_CHUNK_OVERLAP = 215
DEFAULT_SEPARATOR = "\n\n"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass(frozen=True)
class KnowledgeBaseRecord:
    """One knowledge base, minus its vectors.

    ``embedding_model`` and ``dimensions`` are recorded rather than looked up
    per query: the collection is sized to the model at creation, so a base
    outlives a change to whatever the operator has configured today, and the
    mismatch has to be detectable rather than silently searched against.
    """

    id: str
    name: str
    embedding_model: str
    dimensions: int
    created_at: str
    updated_at: str
    description: str = ""
    #: The settings a reader can change after the base exists. Every one of
    #: them is defaulted, so a registry written before they existed loads with
    #: the behaviour it already had.
    #:
    #: At most this many chunks come back from one search of this base. A
    #: property of the base rather than of each call: how much context this
    #: material is worth is a fact about the material.
    top_k: int = DEFAULT_TOP_K
    #: Split on the structure a parser found -- headings, slides, pages --
    #: rather than on length alone. What ``HeadingAwareChunker`` does, and the
    #: default because it is the chunker the manager already builds.
    smart_chunking: bool = True
    #: Where a plain split is allowed to cut, when smart chunking is off.
    separator: str = DEFAULT_SEPARATOR
    #: The size a chunk is aimed at, and how much of the previous one each
    #: carries, both in tokens.
    chunk_size: int = DEFAULT_CHUNK_SIZE
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP
    #: Which pre-processing a file goes through on the way in. Empty is
    #: "don't use", which is the only setting there is so far.
    file_processing: str = ""


@dataclass(frozen=True)
class KnowledgeDocumentRecord:
    """One uploaded document's identity and indexing state."""

    id: str
    base_id: str
    source: str
    media_type: str
    size: int
    status: DocumentStatus
    created_at: str
    updated_at: str
    chunk_count: int = 0
    error: str = ""
    #: Which kind of data source this came in as. Defaulted rather than
    #: required so a registry written before the field existed still loads --
    #: every document in one is a file, which is what the default says.
    origin: DocumentOrigin = "file"
    #: What the origin points back at: the page's URL for a url document, empty
    #: for the rest. A note keeps its text in the blob like any other document,
    #: so it needs nothing here.
    origin_ref: str = ""


class RecordStore:
    """Knowledge bases and documents, persisted as one JSON object."""

    def __init__(self, path: "str | Path") -> None:
        self._path = Path(path)
        self._bases: dict[str, KnowledgeBaseRecord] = {}
        self._documents: dict[str, KnowledgeDocumentRecord] = {}
        self._load()

    # ── persistence ───────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw: Any = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("knowledge: unreadable registry at {}; starting empty", self._path)
            return
        if not isinstance(raw, dict):
            return
        for base_id, fields in (raw.get("bases") or {}).items():
            try:
                self._bases[base_id] = KnowledgeBaseRecord(id=base_id, **fields)
            except TypeError:
                logger.warning("knowledge: dropping malformed base {}", base_id)
        for doc_id, fields in (raw.get("documents") or {}).items():
            try:
                self._documents[doc_id] = KnowledgeDocumentRecord(id=doc_id, **fields)
            except TypeError:
                logger.warning("knowledge: dropping malformed document {}", doc_id)
        self._requeue_interrupted()

    def _requeue_interrupted(self) -> None:
        """Return documents left mid-index to the queue.

        Indexing runs in the gateway process, so a document still marked
        ``indexing`` at load is one whose indexer died with the last process --
        there is no other worker that could still be holding it. Left alone it
        sits in that state for good: the page reports it as in progress, and
        nothing ever picks it up again.
        """
        stuck = [doc for doc in self._documents.values() if doc.status == "indexing"]
        for doc in stuck:
            self._documents[doc.id] = replace(doc, status="pending", updated_at=_now())
        if stuck:
            logger.info("knowledge: requeued {} document(s) interrupted by a restart", len(stuck))
            self._save()

    def _save(self) -> None:
        payload = {
            "bases": {b.id: {k: v for k, v in asdict(b).items() if k != "id"} for b in self._bases.values()},
            "documents": {d.id: {k: v for k, v in asdict(d).items() if k != "id"} for d in self._documents.values()},
        }
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self._path)

    # ── knowledge bases ───────────────────────────────────────────

    def create_base(
        self,
        *,
        name: str,
        embedding_model: str,
        dimensions: int,
        description: str = "",
    ) -> KnowledgeBaseRecord:
        now = _now()
        record = KnowledgeBaseRecord(
            id=_new_id(),
            name=name,
            embedding_model=embedding_model,
            dimensions=dimensions,
            description=description,
            created_at=now,
            updated_at=now,
        )
        self._bases[record.id] = record
        self._save()
        return record

    def get_base(self, base_id: str) -> KnowledgeBaseRecord | None:
        return self._bases.get(base_id)

    def list_bases(self) -> list[KnowledgeBaseRecord]:
        return sorted(self._bases.values(), key=lambda b: b.created_at)

    def rename_base(
        self, base_id: str, *, name: str | None = None, description: str | None = None
    ) -> KnowledgeBaseRecord | None:
        """Edit the two fields that carry no index consequences.

        Narrow on purpose: the embedding model and its width are what the
        collection was built to, so changing them is a rebuild, not an edit.
        """
        record = self._bases.get(base_id)
        if record is None:
            return None
        updated = replace(
            record,
            name=record.name if name is None else name,
            description=record.description if description is None else description,
            updated_at=_now(),
        )
        self._bases[base_id] = updated
        self._save()
        return updated

    def configure_base(self, base_id: str, **settings: object) -> KnowledgeBaseRecord | None:
        """Write the settings a reader can change after the base exists.

        Only the fields named in ``settings`` move. Narrow like
        ``rename_base``, and for the same reason: the embedding model and its
        width are what the collection was built to, so they are not settings.
        An unknown key is a caller mistake, and silently dropping it would
        leave a surface reporting a value it never stored.
        """
        record = self._bases.get(base_id)
        if record is None:
            return None
        allowed = {
            "top_k",
            "smart_chunking",
            "separator",
            "chunk_size",
            "chunk_overlap",
            "file_processing",
        }
        unknown = set(settings) - allowed
        if unknown:
            raise ValueError(f"not a knowledge base setting: {', '.join(sorted(unknown))}")
        updated = replace(record, **settings, updated_at=_now())  # type: ignore[arg-type]
        self._bases[base_id] = updated
        self._save()
        return updated

    def delete_base(self, base_id: str) -> bool:
        """Drop a base and every document record under it.

        The documents go with it in the same write: leaving them would strand
        rows that list by base id and can never be reached or deleted again.
        Dropping the base's *collection* is the caller's half -- this store
        does not reach into the index.
        """
        if base_id not in self._bases:
            return False
        del self._bases[base_id]
        for doc_id in [d.id for d in self._documents.values() if d.base_id == base_id]:
            del self._documents[doc_id]
        self._save()
        return True

    # ── documents ─────────────────────────────────────────────────

    def add_document(
        self,
        *,
        base_id: str,
        source: str,
        media_type: str,
        size: int,
        origin: DocumentOrigin = "file",
        origin_ref: str = "",
    ) -> KnowledgeDocumentRecord:
        now = _now()
        record = KnowledgeDocumentRecord(
            id=_new_id(),
            base_id=base_id,
            source=source,
            media_type=media_type,
            size=size,
            status="pending",
            created_at=now,
            updated_at=now,
            origin=origin,
            origin_ref=origin_ref,
        )
        self._documents[record.id] = record
        self._save()
        return record

    def get_document(self, document_id: str) -> KnowledgeDocumentRecord | None:
        return self._documents.get(document_id)

    def list_documents(self, base_id: str) -> list[KnowledgeDocumentRecord]:
        return sorted(
            (d for d in self._documents.values() if d.base_id == base_id),
            key=lambda d: d.created_at,
        )

    def set_status(
        self,
        document_id: str,
        status: DocumentStatus,
        *,
        chunk_count: int | None = None,
        error: str = "",
    ) -> KnowledgeDocumentRecord | None:
        """Move a document's state, clearing the previous error.

        The error is cleared rather than kept unless this call sets one: a
        retry that succeeds must not leave the failure that prompted it on
        screen next to a document the page now calls ready.
        """
        record = self._documents.get(document_id)
        if record is None:
            return None
        updated = replace(
            record,
            status=status,
            chunk_count=record.chunk_count if chunk_count is None else chunk_count,
            error=error,
            updated_at=_now(),
        )
        self._documents[document_id] = updated
        self._save()
        return updated

    def update_document(
        self,
        document_id: str,
        *,
        source: str,
        media_type: str,
        size: int,
    ) -> KnowledgeDocumentRecord | None:
        """Rewrite one document's content fields and send it back to the queue.

        Back to ``pending`` with no chunks: the text this record described is
        gone, and counting chunks that were embedded from it would report a
        document that no longer exists.
        """
        record = self._documents.get(document_id)
        if record is None:
            return None
        updated = replace(
            record,
            source=source,
            media_type=media_type,
            size=size,
            status="pending",
            chunk_count=0,
            error="",
            updated_at=_now(),
        )
        self._documents[document_id] = updated
        self._save()
        return updated

    def delete_document(self, document_id: str) -> bool:
        if document_id not in self._documents:
            return False
        del self._documents[document_id]
        self._save()
        return True

    def pending_documents(self) -> list[KnowledgeDocumentRecord]:
        """Everything waiting to be indexed, oldest first."""
        return sorted(
            (d for d in self._documents.values() if d.status == "pending"),
            key=lambda d: d.created_at,
        )
