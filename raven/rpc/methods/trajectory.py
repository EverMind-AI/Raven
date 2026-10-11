"""``trajectory.*`` RPC handlers: the Web trajectory view's read surface.

Five methods over the per-session index and the detail layer:

- ``trajectory.state`` -- whether this process serves the view, and whether
  tracing records anything. Always answerable.
- ``trajectory.list`` / ``trajectory.changes`` -- a consistent snapshot of a
  session's entries and the per-change feed that follows it.
- ``trajectory.detail`` / ``trajectory.block`` -- one entry's overview and one
  of its blocks, read from a single capture of the index.

Every handler validates its params against the contract model first (the
dispatcher only checks that ``params`` is an object), then checks the
process policy; the data methods refuse with ``trajectory_disabled`` when the
view is not enabled. Disk reads happen in a worker thread so the event loop
that streams turns stays free.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, TypeVar

from pydantic import BaseModel, ValidationError

from raven.rpc.errors import (
    CursorExpiredRpcError,
    EntryNotFoundError,
    InvalidParamsError,
    RevisionChangedRpcError,
    SessionNotFoundError,
    TrajectoryDisabledError,
)
from raven.rpc.models import (
    TrajectoryBlockParams,
    TrajectoryChangesParams,
    TrajectoryDetailParams,
    TrajectoryListParams,
    TrajectoryStateParams,
)
from raven.tracing import config as tracing_config
from raven.trajectory import details, index, policy

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher

_M = TypeVar("_M", bound=BaseModel)


def _params(model: type[_M], params: Any) -> _M:
    try:
        return model.model_validate(params if params is not None else {})
    except ValidationError as exc:
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ())) or "params"
        raise InvalidParamsError(f"{location}: {first.get('msg', 'invalid')}") from None


def _require_enabled() -> None:
    if not policy.current().enabled():
        raise TrajectoryDisabledError("the trajectory view is not enabled for this process")


def _session_key(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise SessionNotFoundError("session_key is required")
    return value


def _indexer() -> index.TrajectoryIndexer:
    return index.indexer_for(tracing_config.state_dir())


def _state(index_state: index.IndexState) -> dict[str, Any]:
    return asdict(index_state)


async def trajectory_state(params: dict) -> dict:
    _params(TrajectoryStateParams, params)
    current = policy.current()
    if current.enabled():
        # An open page asks this every few seconds, whichever view is up: the
        # beat that lets go of the indexes nobody has read for a while.
        _indexer().evict_idle()
    return {
        "enabled": current.enabled(),
        "policy_revision": current.revision,
        "recording_enabled": tracing_config.enabled(),
    }


async def trajectory_list(params: dict) -> dict:
    request = _params(TrajectoryListParams, params)
    _require_enabled()
    session_key = _session_key(request.session_key)
    session = await _indexer().refresh(session_key)
    try:
        page = session.list_page(request.cursor, request.limit)
    except index.CursorExpiredError as exc:
        raise CursorExpiredRpcError(str(exc)) from None
    return {
        "epoch": page.epoch,
        "snapshot_revision": page.snapshot_revision,
        "entries": [asdict(entry) for entry in page.entries],
        "next_cursor": page.next_cursor,
        "index_state": _state(page.index_state),
        "complete": page.complete,
    }


async def trajectory_changes(params: dict) -> dict:
    request = _params(TrajectoryChangesParams, params)
    _require_enabled()
    session_key = _session_key(request.session_key)
    session = await _indexer().refresh(session_key)
    batch = session.changes(request.epoch, request.after_revision, request.limit)
    return {
        "epoch": batch.epoch,
        "from_revision": batch.from_revision,
        "to_revision": batch.to_revision,
        "upserts": [asdict(entry) for entry in batch.upserts],
        "removed": [asdict(removed) for removed in batch.removed],
        "has_more": batch.has_more,
        "reset_required": batch.reset_required,
        "index_state": _state(batch.index_state),
    }


async def trajectory_detail(params: dict) -> dict:
    request = _params(TrajectoryDetailParams, params)
    _require_enabled()
    session_key = _session_key(request.session_key)
    # Refreshed like the list: after a restart or an idle eviction the index
    # is new, and an entry it has not scanned yet is not a gone one.
    session = await _indexer().refresh(session_key)
    descriptor = await asyncio.to_thread(
        details.describe,
        session,
        request.entry_id,
        state=tracing_config.state_dir(),
        expected_revision=request.entry_revision,
    )
    if descriptor is None:
        raise EntryNotFoundError(request.entry_id)
    return asdict(descriptor)


async def trajectory_block(params: dict) -> dict:
    request = _params(TrajectoryBlockParams, params)
    _require_enabled()
    session_key = _session_key(request.session_key)
    # Refreshed like the list: after a restart or an idle eviction the index
    # is new, and an entry it has not scanned yet is not a gone one.
    session = await _indexer().refresh(session_key)
    try:
        body = await asyncio.to_thread(
            details.read_block,
            session,
            request.entry_id,
            request.block_id,
            state=tracing_config.state_dir(),
            entry_revision=request.entry_revision,
            epoch=request.epoch,
            cursor=request.cursor,
        )
    except details.EntryGoneError:
        raise EntryNotFoundError(request.entry_id) from None
    except details.UnknownBlockError:
        raise EntryNotFoundError(request.block_id, data={"block_id": request.block_id}) from None
    except details.RevisionChangedError as exc:
        raise RevisionChangedRpcError(
            str(exc), data={"current_revision": exc.current_revision, "current_epoch": exc.current_epoch}
        ) from None
    except index.CursorExpiredError as exc:
        raise CursorExpiredRpcError(str(exc)) from None
    return asdict(body)


def register_trajectory_methods(dispatcher: "Dispatcher") -> None:
    dispatcher.register("trajectory.state", trajectory_state)
    dispatcher.register("trajectory.list", trajectory_list)
    dispatcher.register("trajectory.changes", trajectory_changes)
    dispatcher.register("trajectory.detail", trajectory_detail)
    dispatcher.register("trajectory.block", trajectory_block)


__all__ = [
    "register_trajectory_methods",
    "trajectory_block",
    "trajectory_changes",
    "trajectory_detail",
    "trajectory_list",
    "trajectory_state",
]
