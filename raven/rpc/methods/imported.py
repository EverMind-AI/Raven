"""``imported.*`` and the read-only rule for Imported Conversations.

``imported.upload`` takes a package as one base64 frame and hands it to the
import runner (:data:`raven.trajectory.imported.RUNNER`), which allows one
import at a time and owns it until it ends: the answer is awaited here, but a
caller that goes away does not stop an import it was granted, and a second
caller is told the runner is busy before anything is decoded.

An Imported Conversation is a session the page reads and never writes. The
rule is enforced once, as a dispatcher guard, rather than in each handler: any
call naming an ``imported:`` key in a session parameter is refused unless its
method is one that only reads -- the session methods that serve the
conversation from its own directory, the trajectory reads, and the handful of
lookups the page makes on opening any conversation, none of which create or
change a session. A method added later is refused until it is listed here.

The functions below ``imported_upload`` are what ``session.*`` answers for an
imported key: the rows of the rail, the resumed conversation, its title and its
removal.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import TYPE_CHECKING, Any

from raven.rpc.errors import (
    ImportedBusyError,
    ImportedPackageError,
    ImportedReadOnlyError,
    InvalidParamsError,
    SessionNotFoundError,
    SessionTitleTooLongError,
)
from raven.rpc.files import MAX_UPLOAD_BYTES
from raven.rpc.methods.session import _default_session_info, _fill_resumed_context, _map_to_wire
from raven.rpc.methods.trajectory import _params
from raven.rpc.models import ImportedUploadParams
from raven.session.title import TITLE_STORAGE_MAX, collapse_to_line
from raven.trajectory import imported, index
from raven.trajectory.imported_package import ImportCancelledError
from raven.trajectory.package_check import CODE_TOO_LARGE, PackageError

if TYPE_CHECKING:
    from raven.config.schema import Config
    from raven.rpc.dispatcher import Dispatcher

SESSION_PARAMS = ("session_id", "session_key", "session", "conversation_id")

ALLOWED = frozenset(
    {
        "session.resume",
        "session.title",
        "session.delete",
        "trajectory.list",
        "trajectory.changes",
        "trajectory.detail",
        "trajectory.block",
        "model.options",
        "config.get",
        "session.usage",
        "deliverables.list",
        "turn.subscribe",
        "tasks.list",
        "subagents.instances",
        "dag.get",
        "dag.node",
    }
)


def addresses_imported(params: dict[str, Any]) -> bool:
    return (
        any(imported.is_imported_key(params.get(name)) for name in SESSION_PARAMS)
        or params.get("channel") == imported.CHANNEL
    )


def guard(method: str, params: dict[str, Any]) -> None:
    """Refuse every call on an Imported Conversation except the ones that only read."""
    if not addresses_imported(params) or method in ALLOWED:
        return
    # Without a mode it reports the tier in force and changes nothing; the page
    # asks it of every conversation it opens.
    if method == "session.set_mode" and params.get("mode") is None and not params.get("clear"):
        return
    raise ImportedReadOnlyError(f"{method} does not apply to an imported conversation", data={"method": method})


def _decoded_size(content_b64: str) -> int:
    return len(content_b64) // 4 * 3 - content_b64[-2:].count("=")


async def imported_upload(params: dict) -> dict:
    request = _params(ImportedUploadParams, params)
    size = _decoded_size(request.content_b64)
    if size > MAX_UPLOAD_BYTES:
        raise ImportedPackageError(
            f"package is {size} bytes; the limit is {MAX_UPLOAD_BYTES}", data={"reason": CODE_TOO_LARGE}
        )
    try:
        future = imported.RUNNER.try_submit(request.content_b64, request.name, force=request.force)
    except imported.ImportBusyError:
        raise ImportedBusyError("another import is running") from None
    try:
        outcome = await asyncio.wrap_future(future)
    except PackageError as exc:
        raise ImportedPackageError(exc.message, data={"reason": exc.code}) from None
    except ImportCancelledError:
        raise ImportedBusyError("the gateway is shutting down") from None
    conversation = outcome.conversation
    return {
        "status": outcome.status,
        "session_key": conversation.key,
        "title": conversation.title,
        "kind": conversation.kind,
        "source_name": conversation.source_name,
        "imported_at": conversation.imported_at,
    }


# ── what session.* answers for an imported key ─────────────────────────


def _epoch(value: str) -> float:
    try:
        return datetime.fromisoformat(value).timestamp()
    except ValueError:
        return 0.0


def _row(conversation: imported.ImportedConversation) -> dict[str, Any]:
    at = _epoch(conversation.imported_at)
    return {
        "id": conversation.key,
        "message_count": conversation.message_count,
        "preview": conversation.title,
        "last_message_preview": "",
        "source": imported.CHANNEL,
        "started_at": at,
        "updated_at": at,
        "title": conversation.title,
        "pinned": False,
        "running": False,
        "workdir": None,
        "imported": {"kind": conversation.kind, "source_name": conversation.source_name},
    }


async def list_rows() -> list[dict[str, Any]]:
    conversations = await asyncio.to_thread(imported.list_conversations)
    return [_row(conversation) for conversation in conversations]


async def resume(key: str, config: Config) -> dict[str, Any]:
    conversation = await asyncio.to_thread(imported.get, key)
    if conversation is None:
        raise SessionNotFoundError(f"no imported conversation {key!r}")
    session = await asyncio.to_thread(imported.read_session, key)
    info = await _default_session_info(None, config, None)
    info["title"] = conversation.title
    info["read_only"] = True
    info["running"] = False
    messages: list[dict[str, Any]] = []
    if session is not None:
        _fill_resumed_context(info["usage"], session)
        messages = _map_to_wire(session.messages, key, deliveries_on_disk=False)
    return {"session_id": key, "info": info, "messages": messages}


async def title(key: str, new_title: Any) -> dict[str, Any]:
    if new_title is None:
        conversation = await asyncio.to_thread(imported.get, key)
        if conversation is None:
            raise SessionNotFoundError(f"no imported conversation {key!r}")
        return {"title": conversation.title, "session_key": key, "pending": False}
    if not isinstance(new_title, str):
        raise InvalidParamsError("title: must be a string")
    cleaned = collapse_to_line(new_title)
    if not cleaned:
        raise InvalidParamsError("title: an imported conversation keeps a name; it cannot be emptied")
    if len(cleaned) > TITLE_STORAGE_MAX:
        raise SessionTitleTooLongError(
            f"session title is {len(cleaned)} characters; the maximum is {TITLE_STORAGE_MAX}",
            data={"limit": TITLE_STORAGE_MAX},
        )
    try:
        renamed = await asyncio.to_thread(imported.rename, key, cleaned)
    except LookupError:
        raise SessionNotFoundError(f"no imported conversation {key!r}") from None
    return {"title": renamed.title, "session_key": key, "pending": False}


async def delete(key: str) -> dict[str, Any]:
    target = await asyncio.to_thread(imported.trajectory_target, key)
    if target is None:
        return {"deleted": None, "still_on_disk": False}
    removed = await asyncio.to_thread(imported.delete, key)
    # Back on the event loop, which is the only place the indexer registry is touched.
    index.drop_indexer(target[0])
    return {"deleted": key if removed else None, "still_on_disk": False}


def register_imported_methods(dispatcher: "Dispatcher") -> None:
    dispatcher.add_guard(guard)
    dispatcher.register("imported.upload", imported_upload)


__all__ = [
    "ALLOWED",
    "SESSION_PARAMS",
    "addresses_imported",
    "delete",
    "guard",
    "imported_upload",
    "list_rows",
    "register_imported_methods",
    "resume",
    "title",
]
