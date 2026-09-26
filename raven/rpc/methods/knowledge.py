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

from loguru import logger

from raven.knowledge import (
    DEFAULT_CHUNK_OVERLAP,
    DEFAULT_CHUNK_SIZE,
    DEFAULT_SEPARATOR,
    DEFAULT_TOP_K,
    DuplicateBaseNameError,
    EmbeddingError,
    KnowledgeError,
)
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
    from raven.knowledge import load_embedding_config, supported_extensions

    config = load_embedding_config()
    return {
        "configured": config is not None,
        "model": config.model if config is not None else "",
        # Who serves it. The page pairs the two to preselect the default in the
        # picker a new base is made from: a model id names no credential, so
        # half the pin is not enough to select with.
        "provider": config.provider if config is not None else "",
        # Read off the parsers rather than listed here: which formats can be
        # indexed moves with the optional extras installed, and a surface that
        # walks a folder has to filter by today's answer. Not through the
        # manager: this call must not be the one that builds it.
        "extensions": supported_extensions(),
    }


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
        # The other half of the pair: which account that model is reached
        # through. Without it the settings picker cannot show a base its own
        # endpoint, and every base reads as being on the configured one.
        "embedding_provider": str(getattr(base, "embedding_provider", "") or ""),
        "dimensions": base.dimensions,
        "created_at": base.created_at,
        "updated_at": base.updated_at,
        "documents": len(manager.list_documents(base.id)),
        # The settings panel's fields, read off the record rather than
        # defaulted in the page: a base written before they existed answers
        # with what it actually behaves as.
        "top_k": int(getattr(base, "top_k", DEFAULT_TOP_K) or DEFAULT_TOP_K),
        "smart_chunking": bool(getattr(base, "smart_chunking", True)),
        "separator": str(getattr(base, "separator", DEFAULT_SEPARATOR)),
        "table_context_size": int(getattr(base, "table_context_size", 0) or 0),
        "image_context_size": int(getattr(base, "image_context_size", 0) or 0),
        "chunk_size": int(getattr(base, "chunk_size", DEFAULT_CHUNK_SIZE) or DEFAULT_CHUNK_SIZE),
        "chunk_overlap": int(getattr(base, "chunk_overlap", DEFAULT_CHUNK_OVERLAP) or 0),
        "file_processing": str(getattr(base, "file_processing", "") or ""),
        # Why this base cannot embed, when it cannot. The page needs it on the
        # row rather than on a failure: a base that cannot index says so where
        # a reader is looking, instead of in a line of the gateway log.
        "embedding_reach": manager.embedding_reach(base),
    }


async def knowledge_bases_create(params: dict[str, Any]) -> dict[str, Any]:
    """Make a base, sized to the embedding model as it is right now.

    The width is measured against the model rather than taken from the config,
    which is why this reaches the endpoint and can therefore fail: a wrong width
    sizes the collection to something no vector fits, and that surfaces at the
    first insert with nothing pointing back here.

    ``embedding_model`` and ``embedding_provider`` are the pair the page picked,
    and the configured pin is what a caller that sends neither gets -- which is
    what every base was built on before the choice existed.

    ``embedding=false`` makes a base with no model at all, which is the one
    case that reaches no endpoint and cannot fail that way. Revising it later
    is ``knowledge.bases.settings``, which rebuilds the collection rather than
    editing a field.
    """
    name = str(params.get("name") or "").strip()
    if not name:
        raise ConfigValidationError("name is required")
    manager = knowledge_manager()
    try:
        base = await manager.create_base(
            name=name,
            description=str(params.get("description") or ""),
            embedding=params.get("embedding", True) is not False,
            embedding_model=str(params.get("embedding_model") or "").strip(),
            embedding_provider=str(params.get("embedding_provider") or "").strip(),
        )
    except DuplicateBaseNameError as exc:
        # The caller's mistake, not the gateway's: reported as a validation
        # error so the page shows the sentence rather than "internal error".
        raise ConfigValidationError(str(exc)) from None
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


#: What Top K is allowed to be. One is a base that answers with its single
#: nearest chunk; fifty is more context than any turn has room for, and a
#: slider has to stop somewhere a reader cannot type past.
TOP_K_MIN, TOP_K_MAX = 1, 50
#: A chunk has to be big enough to say something and small enough for the
#: model that embeds it; the overlap has to be smaller than the chunk, or
#: every chunk contains the one before it.
CHUNK_MIN, CHUNK_MAX = 64, 8192

#: A page of chunks. Twenty is what a reader scans without the page becoming a
#: scroll of its own, and the ceiling is what stops a caller asking for a
#: document's worth of text in one frame.
CHUNK_PAGE_SIZE, MAX_CHUNK_PAGE = 20, 100

#: The most prose a table or a figure may drag in with it. Past this the
#: context is the chunk and the table is a footnote to it.
CONTEXT_MAX = 2048


def _bounded(params: dict[str, Any], key: str, low: int, high: int) -> int | None:
    """One integer setting, refused rather than clamped when it is out of range.

    Clamping would answer a request the caller did not make and report success,
    which on a slider is invisible and on a typed number is a lie.
    """
    if key not in params or params[key] is None:
        return None
    raw = params[key]
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ConfigValidationError(f"{key} must be a whole number")
    if not low <= raw <= high:
        raise ConfigValidationError(f"{key} must be between {low} and {high}")
    return raw


async def knowledge_bases_settings(params: dict[str, Any]) -> dict[str, Any]:
    """Write one base's settings and answer with the base as it now stands.

    Every field is optional, and the ones left out are untouched -- a panel
    that saves one slider should not have to send the rest back unchanged, and
    a field this build does not know about yet cannot be blanked by one that
    does.

    ``embedding_model`` is the exception to "a field is a setting": sent, it
    rebuilds the base rather than writing a value, because the collection is
    sized to the model's width and the vectors in it were made by that model.
    Sending it empty turns embedding off. Sent alone, ``embedding_provider``
    still only moves where the same model is reached.
    """
    base_id = str(params.get("base_id") or "")
    manager = _base_or_refuse(base_id)

    settings: dict[str, Any] = {}
    top_k = _bounded(params, "top_k", TOP_K_MIN, TOP_K_MAX)
    if top_k is not None:
        settings["top_k"] = top_k
    chunk_size = _bounded(params, "chunk_size", CHUNK_MIN, CHUNK_MAX)
    if chunk_size is not None:
        settings["chunk_size"] = chunk_size
    overlap = _bounded(params, "chunk_overlap", 0, CHUNK_MAX)
    if overlap is not None:
        settings["chunk_overlap"] = overlap
    if params.get("smart_chunking") is not None:
        settings["smart_chunking"] = bool(params["smart_chunking"])
    if params.get("separator") is not None:
        settings["separator"] = str(params["separator"])
    # Only when the model is not being moved: the rebuild below records the
    # provider that actually served the width it measured, and a settings write
    # of the same key would be a second, unchecked opinion about it.
    switching = params.get("embedding_model") is not None
    if params.get("embedding_provider") is not None and not switching:
        settings["embedding_provider"] = str(params["embedding_provider"]).strip()
    for name in ("table_context_size", "image_context_size"):
        size = _bounded(params, name, 0, CONTEXT_MAX)
        if size is not None:
            settings[name] = size
    if params.get("file_processing") is not None:
        settings["file_processing"] = str(params["file_processing"])

    # Checked against what the base will hold once this write lands, not
    # against what was sent: a call that moves only the overlap has to be
    # judged against the chunk size already recorded.
    existing = manager.get_base(base_id)
    size = settings.get("chunk_size", getattr(existing, "chunk_size", DEFAULT_CHUNK_SIZE))
    lap = settings.get("chunk_overlap", getattr(existing, "chunk_overlap", DEFAULT_CHUNK_OVERLAP))
    if lap >= size:
        raise ConfigValidationError("chunk_overlap must be smaller than chunk_size")

    # Skipped when nothing was sent but the model: a settings write with no
    # settings in it is a file rewritten to say what it already said.
    base = manager.configure_base(base_id, **settings) if settings else existing
    if base is None:
        raise ConfigValidationError(f"no such base: {base_id}")

    if switching:
        # After the settings, not before: the documents this requeues are
        # re-cut on the way back in, and they should be cut by the numbers
        # this same call just wrote rather than by the ones it replaced.
        try:
            base = await manager.switch_embedding(
                base_id,
                model=str(params.get("embedding_model") or "").strip(),
                provider=str(params.get("embedding_provider") or "").strip(),
            )
        except (KnowledgeError, EmbeddingError) as exc:
            # The picked pair cannot be embedded with, which is the caller's
            # choice to correct: reported as a refusal so the panel shows the
            # sentence rather than "internal error". Nothing was dropped -- the
            # width is measured before the collection is touched.
            raise ConfigValidationError(str(exc)) from None
        except Exception as exc:  # noqa: BLE001 - surfaced as a typed RPC error
            raise InternalError(f"could not switch the embedding model: {exc}") from exc
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
    manager = knowledge_manager()
    # Listed before the delete, because afterwards there is nothing left to ask
    # which documents the base held.
    doomed = [doc.id for doc in manager.list_documents(base_id)]
    try:
        removed = await manager.delete_base(base_id)
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed RPC error
        raise InternalError(f"could not delete the base: {exc}") from exc
    if removed:
        for document_id in doomed:
            _forget_preview(document_id)
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
        # What the parse could not do, on a document that was indexed anyway.
        # Empty on every row that had nothing to report, and never a second
        # spelling of `error`: a row carries one or the other, because a
        # document that failed has no parse to warn about.
        "warning": str(getattr(doc, "warning", "") or ""),
        "created_at": doc.created_at,
        "updated_at": doc.updated_at,
        "origin": getattr(doc, "origin", "file"),
        "origin_ref": getattr(doc, "origin_ref", "") or "",
    }


def _readable(raw: str) -> Path:
    """Resolve a path the page sent, through the fence every reading surface shares.

    The page holds what ``fs.upload`` gave it -- a workspace path such as
    ``uploads/handbook.md`` -- and that is the spelling every file tool takes.

    Through ``resolve_readable`` rather than ``resolve_path`` alone, because
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


def _base_or_refuse(base_id: str) -> Any:
    if not base_id:
        raise ConfigValidationError("base_id is required")
    manager = knowledge_manager()
    if manager.get_base(base_id) is None:
        raise ConfigValidationError(f"no such base: {base_id}")
    return manager


async def knowledge_documents_add_note(params: dict[str, Any]) -> dict[str, Any]:
    """Take a typed note into a base as the markdown it was written in.

    Markdown rather than plain text because that is what the page's own editor
    writes and what its preview renders; the chunker also reads headings out of
    it, so a note with sections chunks along them rather than by length.
    """
    from raven.knowledge._sources import note_filename

    base_id = str(params.get("base_id") or "")
    manager = _base_or_refuse(base_id)
    text = str(params.get("text") or "")
    title = str(params.get("title") or "").strip()
    if not text.strip():
        raise ConfigValidationError("a note needs some text")
    try:
        doc = manager.add_document(
            base_id,
            filename=note_filename(title, text),
            content=text.encode("utf-8"),
            origin="note",
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed RPC error
        raise ConfigValidationError(str(exc)) from exc
    return {"document": _doc_row(doc)}


async def knowledge_documents_update_note(params: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a note in place. Only a note: every other origin is a copy of
    something the reader holds elsewhere, and editing that here would make this
    base the only place the change exists."""
    from raven.knowledge._sources import note_filename

    document_id = str(params.get("document_id") or "")
    if not document_id:
        raise ConfigValidationError("document_id is required")
    text = str(params.get("text") or "")
    title = str(params.get("title") or "").strip()
    if not text.strip():
        raise ConfigValidationError("a note needs some text")
    manager = knowledge_manager()
    existing = manager.get_document(document_id)
    if existing is None:
        raise ConfigValidationError(f"no such document: {document_id}")
    if getattr(existing, "origin", "file") != "note":
        raise ConfigValidationError("only a note can be edited here")
    doc = await manager.replace_document(
        document_id,
        filename=note_filename(title, text),
        content=text.encode("utf-8"),
    )
    if doc is None:
        raise ConfigValidationError(f"no such document: {document_id}")
    return {"document": _doc_row(doc)}


async def knowledge_documents_add_url(params: dict[str, Any]) -> dict[str, Any]:
    """Read one web page and take it into a base as markdown.

    The gateway fetches, not the page: a browser cannot read a third-party site
    on the reader's behalf, and the bytes have to reach this process to be
    chunked anyway. What the reader sees afterwards is a document like any
    other, keeping the URL it came from.
    """
    from raven.knowledge._sources import SourceFetchError, fetch_page, page_filename

    base_id = str(params.get("base_id") or "")
    manager = _base_or_refuse(base_id)
    url = str(params.get("url") or "").strip()
    if not url:
        raise ConfigValidationError("url is required")
    try:
        page = await fetch_page(url, api_key=_jina_key())
    except SourceFetchError as exc:
        raise ConfigValidationError(str(exc)) from exc
    try:
        doc = manager.add_document(
            base_id,
            filename=page_filename(page.markdown, url, page.title),
            content=page.markdown.encode("utf-8"),
            origin="url",
            origin_ref=url,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed RPC error
        raise ConfigValidationError(str(exc)) from exc
    return {"document": _doc_row(doc)}


def _jina_key() -> str:
    """The configured Jina key, or empty. Reader works without one, at a lower
    rate limit, so a deployment that has not set one still gets this."""
    try:
        from raven.config.update_tools import get_jina_api_key

        return get_jina_api_key(redact=False)
    except Exception:  # noqa: BLE001 - an unreadable config is not a reason to refuse the fetch
        return ""


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


async def knowledge_documents_chunks(params: dict[str, Any]) -> dict[str, Any]:
    """One document's indexed pieces, in reading order.

    What the search matches against, not what a fresh parse would produce: a
    reader checking why a document answers the way it does needs the pieces
    that are in the index, and those two stop agreeing the moment a chunking
    setting has moved.
    """
    document_id = _document_id(params)
    page = max(1, int(params.get("page") or 1))
    size = min(MAX_CHUNK_PAGE, max(1, int(params.get("page_size") or CHUNK_PAGE_SIZE)))
    available = params.get("available")
    query = str(params.get("query") or "").strip()
    manager = knowledge_manager()
    try:
        if query:
            # Searching answers with what matched, best first. Paging it would
            # promise a second page of relevance that the engine was never
            # asked for -- the limit is the page size, and a reader who wants
            # more narrows the query.
            found = await manager.search_document(document_id, query, limit=size)
            cropped = _cropped(manager, document_id, found)
            return {"chunks": [_chunk_row(piece, cropped) for piece in found], "total": len(found)}
        held, total = await manager.document_chunks(
            document_id,
            offset=(page - 1) * size,
            limit=size,
            enabled=available if isinstance(available, bool) else None,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed RPC error
        raise InternalError(f"reading chunks failed: {exc}") from exc
    cropped = _cropped(manager, document_id, held)
    return {"chunks": [_chunk_row(piece, cropped) for piece in held], "total": total}


async def knowledge_chunks_switch(params: dict[str, Any]) -> dict[str, Any]:
    """Turn pieces of one document on or off."""
    document_id = _document_id(params)
    chunk_ids = _chunk_ids(params)
    enabled = bool(params.get("enabled"))
    try:
        changed = await knowledge_manager().set_chunks_enabled(document_id, chunk_ids, enabled)
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed RPC error
        raise InternalError(f"switching chunks failed: {exc}") from exc
    return {"changed": changed}


async def knowledge_chunks_delete(params: dict[str, Any]) -> dict[str, Any]:
    """Remove pieces of one document, and say how many it has left."""
    document_id = _document_id(params)
    chunk_ids = _chunk_ids(params)
    try:
        remaining = await knowledge_manager().delete_chunks(document_id, chunk_ids)
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed RPC error
        raise InternalError(f"deleting chunks failed: {exc}") from exc
    return {"remaining": remaining}


async def knowledge_chunks_create(params: dict[str, Any]) -> dict[str, Any]:
    """Append a piece a person wrote, embedded like every other piece."""
    document_id = _document_id(params)
    text = str(params.get("text") or "").strip()
    if not text:
        raise ConfigValidationError("text is required")
    try:
        written = await knowledge_manager().add_chunk(document_id, text)
    except KnowledgeError as exc:
        raise ConfigValidationError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed RPC error
        raise InternalError(f"adding a chunk failed: {exc}") from exc
    return {"chunk": _chunk_row(written)}


async def knowledge_chunks_update(params: dict[str, Any]) -> dict[str, Any]:
    """Rewrite one piece, re-embedding it so the vector says what it says."""
    document_id = _document_id(params)
    chunk_id = str(params.get("chunk_id") or "").strip()
    if not chunk_id:
        raise ConfigValidationError("chunk_id is required")
    text = str(params.get("text") or "").strip()
    if not text:
        raise ConfigValidationError("text is required")
    try:
        written = await knowledge_manager().update_chunk(document_id, chunk_id, text)
    except KnowledgeError as exc:
        raise ConfigValidationError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed RPC error
        raise InternalError(f"editing a chunk failed: {exc}") from exc
    return {"chunk": _chunk_row(written)}


def _document_id(params: dict[str, Any]) -> str:
    """The document a chunk call is about.

    Stripped, the way `query` is: a blank id is never a document, and answering
    it with an empty list would read as "this file indexed to nothing" rather
    than "you asked for nothing".
    """
    document_id = str(params.get("document_id") or "").strip()
    if not document_id:
        raise ConfigValidationError("document_id is required")
    return document_id


def _chunk_ids(params: dict[str, Any]) -> list[str]:
    raw = params.get("chunk_ids")
    ids = [str(value).strip() for value in raw if str(value).strip()] if isinstance(raw, list) else []
    if not ids:
        raise ConfigValidationError("chunk_ids is required")
    return ids


def _page(value: Any) -> int | None:
    """A page number, or ``None`` for anything that is not one."""
    return int(value) if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _path(value: Any) -> list[str]:
    return [str(part) for part in value] if isinstance(value, list) else []


def _chunk_parts(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Where each piece of a merged chunk came from, in order.

    Empty for a chunk that merged nothing, which is most of them: its own
    fields already say where it is, and repeating that as a list of one would
    make "this chunk was merged" unreadable from the answer.

    Present, it is the only place the truth is. The naive strategy merges
    across section boundaries, so a chunk can hold two pages, two headings and
    two sections -- and the flattened fields beside this one can only carry the
    first of each. A reader given those alone is told the chunk is on page 4
    under one heading when half of what they are reading came from page 9 under
    another, and nothing about the row says so.
    """
    spans = metadata.get("elements")
    if not isinstance(spans, list) or len(spans) < 2:
        return []
    parts: list[dict[str, Any]] = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        ordinal = span.get("section_ordinal")
        parts.append(
            {
                "char_start": int(span.get("char_start") or 0),
                "char_end": int(span.get("char_end") or 0),
                "layout_type": str(span.get("layout_type") or ""),
                "page_number": _page(span.get("page_number")),
                "heading_path": _path(span.get("heading_path")),
                # The section identity, when the parser recorded one. A heading
                # path is not a substitute: two same-named children of one
                # parent share it.
                "section_ordinal": int(ordinal) if isinstance(ordinal, int) and not isinstance(ordinal, bool) else None,
            }
        )
    return parts


def _regions(chunk: Any) -> "list[dict[str, Any]]":
    """Where on its pages a piece was cut from, for a viewer that draws it.

    Through the same reader the crops use rather than a second walk over the
    metadata. The rule it applies is not obvious -- a merged chunk keeps each
    piece's page and box in its element spans and drops its own box when the
    pieces disagree on the page, while an unmerged one has no spans at all and
    carries one page and one box at the top level -- and two implementations of
    it would mean the picture and the highlight could disagree about where a
    piece came from.
    """
    from raven.knowledge._crops import regions_of

    try:
        found = regions_of(chunk)
    except Exception as exc:  # noqa: BLE001 - a row without boxes is still a row
        logger.debug("knowledge: cannot read the regions of a chunk ({})", exc)
        return []
    return [
        {"page_number": page, "x0": x0, "top": top, "x1": x1, "bottom": bottom} for page, (x0, top, x1, bottom) in found
    ]


def _cropped(manager: Any, document_id: str, held: "list[Any]") -> "set[str]":
    """Which of these pieces have a picture of their region stored.

    One pass over the rows rather than a lookup inside each: this is a stat per
    piece either way, and doing it here keeps the row builder a pure function
    of what it is handed.
    """
    found = set()
    for piece in held:
        chunk_id = str(getattr(piece, "chunk_id", "") or "")
        if chunk_id and manager.crop_path(document_id, chunk_id):
            found.add(chunk_id)
    return found


def _chunk_row(held: Any, cropped: "frozenset[str] | set[str]" = frozenset()) -> dict[str, Any]:
    """One stored piece as the page reads it, positional metadata flattened.

    The flattened fields describe where the chunk *starts*, which is all they
    can do for a chunk that merged several sections -- so ``parts`` carries the
    rest, and is empty for a chunk that merged nothing. Without it a merged
    chunk reads as a chunk of its first section, and the reader has no route to
    the other one: the box and the character ranges stay in the metadata, but
    which page, which heading and which section each piece came from is exactly
    what a person scanning this list is using.

    ``cropped`` is which of the pieces being rendered have a picture stored
    for them, worked out once by :func:`_cropped` rather than per row. What the
    row needs is only whether there is one to ask for; the picture itself is
    fetched from the crop route when the page decides to show it.
    """
    chunk = getattr(held, "chunk", held)
    metadata = getattr(chunk, "metadata", None) or {}
    chunk_id = str(getattr(held, "chunk_id", "") or "")
    return {
        "chunk_index": int(getattr(chunk, "chunk_index", 0) or 0),
        "total_chunks": int(getattr(chunk, "total_chunks", 0) or 0),
        "text": getattr(chunk, "text", "") or "",
        "layout_type": str(metadata.get("layout_type") or ""),
        "page_number": _page(metadata.get("page_number")),
        # The last page this piece touches. Equal to the first unless the chunk
        # runs over a page boundary, which it can whenever it merged.
        "page_end": _page(metadata.get("page_end")),
        "heading_path": _path(metadata.get("heading_path")),
        "parts": _chunk_parts(metadata),
        "chunk_id": chunk_id,
        "enabled": bool(getattr(held, "enabled", True)),
        "manual": bool(getattr(held, "manual", False)),
        "has_crop": chunk_id in cropped,
        "regions": _regions(chunk),
    }


def _forget_preview(document_id: str) -> None:
    """Drop the source copy a preview retained for this document.

    Rendering an office file needs a path whose suffix says what the bytes are,
    and a blob is stored under a bare id -- so previewing one leaves a second
    copy of its content in the cache. The engine deletes blobs and chunks and
    knows nothing about that copy, which lives in the rpc layer beside the
    renderer that made it, so removing it is this layer's to do.

    Never fails the delete: the row is gone either way, and a reader who cannot
    be rid of a document because of a cache file is worse off than one whose
    cache is swept a week later.
    """
    # Through the module that owns the cache directory rather than the one that
    # fills it: `knowledge_preview` imports this module, so reaching back for it
    # here would put the two in an import cycle.
    from raven.rpc import pdf_preview

    try:
        pdf_preview.forget_source(document_id)
    except Exception as exc:  # noqa: BLE001 - a cache copy is not worth a failed delete
        logger.warning("knowledge: could not drop the retained source for {}: {}", document_id, exc)


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
    _forget_preview(document_id)
    return {"removed": bool(removed)}


async def knowledge_search(params: dict[str, Any]) -> dict[str, Any]:
    """Nearest chunks across the named bases.

    ``score`` runs one direction whatever found the hit -- higher is nearer --
    but what it *is* differs, so every hit says. A base whose embedding model
    cannot be reached is searched by keyword instead of not at all, and its
    hits are BM25 scores on the index's own scale: unbounded, and not
    comparable by value with the cosine similarities beside them. Without
    ``retrieval`` on the hit and ``by_keyword`` on the answer, an endpoint
    going down turned into a normal-looking result set, differently scaled,
    with nothing anywhere saying retrieval had changed mode.
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
        # None rather than a number when the caller names none: the engine
        # then answers with what the bases themselves are configured for.
        found = await knowledge_manager().search(
            base_ids, query, top_k=int(top_k) if isinstance(top_k, int) and top_k > 0 else None
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as a typed RPC error
        raise InternalError(f"search failed: {exc}") from exc
    return {
        "hits": [
            {
                "score": float(hit.score),
                # What that number is: a cosine similarity, or a BM25 score
                # from the keyword fallback.
                "retrieval": str(getattr(hit, "retrieval", "vector") or "vector"),
                "document_id": hit.document_id,
                "text": getattr(hit.chunk, "text", "") or "",
                # Which piece of its document this was, and of how many. A
                # retrieved chunk read on its own says nothing about where in
                # the document it came from, which is the first thing anyone
                # testing recall asks.
                "chunk_index": int(getattr(hit.chunk, "chunk_index", 0) or 0),
                "total_chunks": int(getattr(hit.chunk, "total_chunks", 0) or 0),
                # The chunk's own record of what it was parsed from. Carried
                # even though the caller usually holds the document list: a hit
                # has to be readable on its own, including when the row it came
                # from was deleted while the search was in flight.
                "source": str(getattr(hit.chunk, "source", "") or ""),
            }
            for hit in found.hits
        ],
        # Which bases answered by words, and why their vectors could not be
        # reached. Not an error -- those bases are in the results above -- but
        # a reader comparing two sets of hits has to be told that some of them
        # came from a different kind of match, and the reason is the sentence
        # that says what to fix.
        "by_keyword": [{"base_id": base_id, "reason": reason} for base_id, reason in found.by_keyword.items()],
        # Rounded here rather than in the surface: this is a measurement, and
        # microseconds of it are noise either way.
        "search_ms": round(found.search_ms, 1),
        "embed_ms": round(found.embed_ms, 1),
    }


def register_knowledge_methods(dispatcher: Dispatcher) -> None:
    dispatcher.register("knowledge.status", knowledge_status)
    dispatcher.register("knowledge.bases.list", knowledge_bases_list)
    dispatcher.register("knowledge.bases.create", knowledge_bases_create)
    dispatcher.register("knowledge.bases.rename", knowledge_bases_rename)
    dispatcher.register("knowledge.bases.settings", knowledge_bases_settings)
    dispatcher.register("knowledge.bases.delete", knowledge_bases_delete)
    dispatcher.register("knowledge.documents.list", knowledge_documents_list)
    dispatcher.register("knowledge.documents.chunks", knowledge_documents_chunks)
    dispatcher.register("knowledge.chunks.switch", knowledge_chunks_switch)
    dispatcher.register("knowledge.chunks.delete", knowledge_chunks_delete)
    dispatcher.register("knowledge.chunks.create", knowledge_chunks_create)
    dispatcher.register("knowledge.chunks.update", knowledge_chunks_update)
    dispatcher.register("knowledge.documents.add", knowledge_documents_add)
    dispatcher.register("knowledge.documents.add_note", knowledge_documents_add_note)
    dispatcher.register("knowledge.documents.update_note", knowledge_documents_update_note)
    dispatcher.register("knowledge.documents.add_url", knowledge_documents_add_url)
    dispatcher.register("knowledge.documents.index", knowledge_documents_index)
    dispatcher.register("knowledge.documents.delete", knowledge_documents_delete)
    dispatcher.register("knowledge.search", knowledge_search)


__all__ = [
    "knowledge_bases_create",
    "knowledge_bases_delete",
    "knowledge_bases_list",
    "knowledge_bases_rename",
    "knowledge_bases_settings",
    "knowledge_documents_add",
    "knowledge_documents_add_note",
    "knowledge_documents_add_url",
    "knowledge_documents_update_note",
    "knowledge_documents_index",
    "knowledge_documents_delete",
    "knowledge_documents_list",
    "knowledge_manager",
    "knowledge_search",
    "knowledge_status",
    "register_knowledge_methods",
]
