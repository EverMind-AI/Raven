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

What ``session.*`` answers for an imported key -- the rail's rows, the
resumed conversation, its title and its removal -- lives beside the session
handlers in :mod:`raven.rpc.methods.session`.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from raven.rpc.errors import ImportedBusyError, ImportedPackageError, ImportedReadOnlyError
from raven.rpc.files import MAX_UPLOAD_BYTES
from raven.rpc.methods.trajectory import _params
from raven.rpc.models import ImportedUploadParams
from raven.trajectory import imported
from raven.trajectory.imported_package import ImportCancelledError
from raven.trajectory.package_check import CODE_TOO_LARGE, PackageError

if TYPE_CHECKING:
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
        "tasks.board",
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


def register_imported_methods(dispatcher: "Dispatcher") -> None:
    dispatcher.add_guard(guard)
    dispatcher.register("imported.upload", imported_upload)


__all__ = [
    "ALLOWED",
    "SESSION_PARAMS",
    "addresses_imported",
    "guard",
    "imported_upload",
    "register_imported_methods",
]
