"""``knowledge.*`` RPC handlers -- the page's view of the knowledge bases.

The engine runs in this process. Its vector store is embedded and file-backed
and its records are JSON on disk, both under the installation's data directory,
so these handlers ask ``raven.knowledge`` directly instead of crossing a
service boundary. That is the constraint the package is shaped around: the
surface it replaces needed Redis, a Qdrant instance and a web service to answer
one question about a document.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from raven.rpc.errors import ConfigValidationError, InternalError

if TYPE_CHECKING:
    from raven.knowledge import KnowledgeManager
    from raven.rpc.dispatcher import Dispatcher

_manager: KnowledgeManager | None = None


def knowledge_manager() -> KnowledgeManager:
    """The process's one manager, built when something first asks.

    Lazily rather than at import time: constructing it resolves and creates a
    directory, and a deployment whose page never opens the knowledge tab should
    not grow one.
    """
    global _manager
    if _manager is None:
        from raven.config.paths import get_runtime_subdir
        from raven.knowledge import KnowledgeManager

        _manager = KnowledgeManager(get_runtime_subdir("knowledge"))
    return _manager


def _set_manager_for_tests(manager: KnowledgeManager | None) -> None:
    """Point the handlers at a manager on a tmp_path, and back again."""
    global _manager
    _manager = manager


async def knowledge_status(_params: dict[str, Any]) -> dict[str, Any]:
    """Whether a base can be created at all, and with which model.

    Read from the embedding config rather than from
    ``KnowledgeManager.embedding_available``: that answers the same question --
    it is the same call underneath -- but the page also wants the model's name
    to say *what* it would embed with, and only the config carries it. No
    credential is reported; a key's presence is the ``configured`` flag.
    """
    from raven.knowledge import load_embedding_config

    config = load_embedding_config()
    return {"configured": config is not None, "model": config.model if config is not None else ""}


async def knowledge_bases_list(_params: dict[str, Any]) -> dict[str, Any]:
    """Every base, with the document count the list view shows on each row.

    The count is included rather than left to a second call per row: a list of
    bases with no sizes is a list a reader cannot act on, and the records are
    already on disk beside the bases.
    """
    manager = knowledge_manager()
    return {"bases": [_base_row(manager, base) for base in manager.list_bases()]}


def _base_row(manager: KnowledgeManager, base: Any) -> dict[str, Any]:
    """One base in the shape the contract declares, counts included."""
    return {
        "id": base.id,
        "name": base.name,
        "description": base.description,
        "embedding_model": base.embedding_model,
        "dimensions": base.dimensions,
        "created_at": base.created_at,
        "updated_at": base.updated_at,
        "documents": len(manager.list_documents(base.id)),
    }


async def knowledge_bases_create(params: dict[str, Any]) -> dict[str, Any]:
    """Make a base, sized to the embedding model as it is right now.

    The width is measured against the model rather than taken from the config,
    which is why this reaches the endpoint and can therefore fail: a wrong width
    sizes the collection to something no vector fits, and that surfaces at the
    first insert with nothing pointing back here.
    """
    name = str(params.get("name") or "").strip()
    if not name:
        raise ConfigValidationError("name is required")
    manager = knowledge_manager()
    try:
        base = await manager.create_base(name=name, description=str(params.get("description") or ""))
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed RPC error
        raise InternalError(f"could not create the base: {exc}") from exc
    return {"base": _base_row(manager, base)}


async def knowledge_bases_rename(params: dict[str, Any]) -> dict[str, Any]:
    """Edit a base's name or description. Neither touches its collection.

    The collection is named for the id, so a rename is only an edit -- which is
    the reason a base can be renamed at all without stranding its rows.
    """
    base_id = str(params.get("base_id") or "")
    if not base_id:
        raise ConfigValidationError("base_id is required")
    name = params.get("name")
    description = params.get("description")
    manager = knowledge_manager()
    base = manager.rename_base(
        base_id,
        name=str(name) if isinstance(name, str) else None,
        description=str(description) if isinstance(description, str) else None,
    )
    if base is None:
        raise ConfigValidationError(f"no such base: {base_id}")
    return {"base": _base_row(manager, base)}


async def knowledge_bases_delete(params: dict[str, Any]) -> dict[str, Any]:
    """Drop a base with its documents, their blobs and its collection.

    ``removed`` is false for a base that was not there, rather than an error: a
    second delete from a stale page is the same outcome the caller wanted.
    """
    base_id = str(params.get("base_id") or "")
    if not base_id:
        raise ConfigValidationError("base_id is required")
    try:
        removed = await knowledge_manager().delete_base(base_id)
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed RPC error
        raise InternalError(f"could not delete the base: {exc}") from exc
    return {"removed": bool(removed)}


async def knowledge_documents_list(params: dict[str, Any]) -> dict[str, Any]:
    """The documents in one base, with the indexing state each row shows."""
    base_id = str(params.get("base_id") or "")
    if not base_id:
        raise ConfigValidationError("base_id is required")
    manager = knowledge_manager()
    if manager.get_base(base_id) is None:
        raise ConfigValidationError(f"no such base: {base_id}")
    return {"documents": [_doc_row(doc) for doc in manager.list_documents(base_id)]}


def _doc_row(doc: Any) -> dict[str, Any]:
    """One document in the shape the contract declares."""
    return {
        "id": doc.id,
        "base_id": doc.base_id,
        "source": doc.source,
        "media_type": doc.media_type,
        "size": doc.size,
        "status": doc.status,
        "chunk_count": doc.chunk_count,
        "error": doc.error or "",
        "created_at": doc.created_at,
        "updated_at": doc.updated_at,
    }


def _readable(raw: str) -> Path:
    """Resolve a path the page sent, through the fence every reading surface shares.

    The page holds what ``fs.upload`` gave it -- a workspace path such as
    ``uploads/handbook.md`` -- and that is the spelling every file tool takes.

    Through ``resolve_readable`` rather than ``_resolve_path`` alone, because
    the workspace fence is off by default: with no allowed roots the path
    policy returns any absolute path unchanged, so a client could name
    ``config.json`` or ``serve.json`` here and read the provider keys or the
    nonce-minting token straight back out of ``knowledge.search``, which
    answers with the chunk text. The state directory is excluded regardless of
    that setting, and it is one fence rather than one per surface -- a base
    that renders bytes to the page is one more surface, not a new question.

    Unlike ``turn.send``, which drops a bad attachment rather than failing a
    turn, a refusal here is an error. Somebody asked for *this* document, and
    quietly adding nothing is worse than saying no.
    """
    from raven.rpc.files import resolve_readable

    text = raw.strip()
    if not text:
        raise ConfigValidationError("path is required")
    try:
        return resolve_readable(text)
    except (FileNotFoundError, IsADirectoryError) as exc:
        raise ConfigValidationError(f"not a file: {text}") from exc
    except Exception as exc:  # noqa: BLE001 - refusal, bad name, oversized, all one answer
        raise ConfigValidationError(f"path refused: {exc}") from exc


async def knowledge_documents_add(params: dict[str, Any]) -> dict[str, Any]:
    """Take an uploaded file into a base. Indexing is a separate call.

    Separate because embedding a document is slow enough to outlive a request,
    and a page that cannot show "queued, now indexing" has to block on it
    instead.
    """
    base_id = str(params.get("base_id") or "")
    if not base_id:
        raise ConfigValidationError("base_id is required")
    resolved = _readable(str(params.get("path") or ""))
    manager = knowledge_manager()
    try:
        content = resolved.read_bytes()
    except OSError as exc:
        raise ConfigValidationError(f"cannot read {resolved.name}: {exc}") from exc
    try:
        doc = manager.add_document(base_id, filename=resolved.name, content=content)
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed RPC error
        raise ConfigValidationError(str(exc)) from exc
    return {"document": _doc_row(doc)}


async def knowledge_documents_index(params: dict[str, Any]) -> dict[str, Any]:
    """Embed one document's chunks, and answer where that got to.

    The record is returned rather than a bare ok: indexing is the step that can
    half-succeed, and its own ``status`` and ``error`` are what the row shows.
    """
    document_id = str(params.get("document_id") or "")
    if not document_id:
        raise ConfigValidationError("document_id is required")
    try:
        doc = await knowledge_manager().index_document(document_id)
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed RPC error
        raise InternalError(f"indexing failed: {exc}") from exc
    if doc is None:
        raise ConfigValidationError(f"no such document: {document_id}")
    return {"document": _doc_row(doc)}


async def knowledge_documents_delete(params: dict[str, Any]) -> dict[str, Any]:
    """Take one document out of its base, with its chunks and its blob.

    The page's only way out of a document that will not index. Without it a
    parse that keeps failing, or a row left `pending` by a gateway restart
    mid-index, could be cleared only by deleting the base around it -- every
    other document with it.

    ``removed`` rather than a raise on a document that is already gone: two
    clicks on the same row, or a row the reader deleted in another tab, is not
    an error to report to them.
    """
    document_id = str(params.get("document_id") or "")
    if not document_id:
        raise ConfigValidationError("document_id is required")
    try:
        removed = await knowledge_manager().delete_document(document_id)
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed RPC error
        raise InternalError(f"delete failed: {exc}") from exc
    return {"removed": bool(removed)}


async def knowledge_search(params: dict[str, Any]) -> dict[str, Any]:
    """Nearest chunks across the named bases.

    ``score`` is a similarity, so higher is nearer -- the direction every caller
    already reads.
    """
    raw_ids = params.get("base_ids")
    base_ids = [str(b) for b in raw_ids if str(b).strip()] if isinstance(raw_ids, list) else []
    if not base_ids:
        raise ConfigValidationError("base_ids is required")
    query = str(params.get("query") or "").strip()
    if not query:
        raise ConfigValidationError("query is required")
    top_k = params.get("top_k")
    try:
        hits = await knowledge_manager().search(
            base_ids, query, top_k=int(top_k) if isinstance(top_k, int) and top_k > 0 else 5
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed RPC error
        raise InternalError(f"search failed: {exc}") from exc
    return {
        "hits": [
            {
                "score": float(hit.score),
                "document_id": hit.document_id,
                "text": getattr(hit.chunk, "text", "") or "",
            }
            for hit in hits
        ]
    }


def register_knowledge_methods(dispatcher: Dispatcher) -> None:
    dispatcher.register("knowledge.status", knowledge_status)
    dispatcher.register("knowledge.bases.list", knowledge_bases_list)
    dispatcher.register("knowledge.bases.create", knowledge_bases_create)
    dispatcher.register("knowledge.bases.rename", knowledge_bases_rename)
    dispatcher.register("knowledge.bases.delete", knowledge_bases_delete)
    dispatcher.register("knowledge.documents.list", knowledge_documents_list)
    dispatcher.register("knowledge.documents.add", knowledge_documents_add)
    dispatcher.register("knowledge.documents.index", knowledge_documents_index)
    dispatcher.register("knowledge.documents.delete", knowledge_documents_delete)
    dispatcher.register("knowledge.search", knowledge_search)


__all__ = [
    "knowledge_bases_create",
    "knowledge_bases_delete",
    "knowledge_bases_list",
    "knowledge_bases_rename",
    "knowledge_documents_add",
    "knowledge_documents_index",
    "knowledge_documents_delete",
    "knowledge_documents_list",
    "knowledge_manager",
    "knowledge_search",
    "knowledge_status",
    "register_knowledge_methods",
]
