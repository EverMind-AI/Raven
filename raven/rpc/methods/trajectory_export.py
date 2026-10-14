"""``trajectory.turns`` and ``trajectory.export*``: picking turns of a session and exporting them.

- ``trajectory.turns`` -- the session's main-line turns, for the page's turn picker.
- ``trajectory.export`` -- validate a selection and start an export job.
- ``trajectory.export_status`` -- where the job stands; a finished one names its download URL.
- ``trajectory.export_cancel`` -- stop a running job or discard a finished package.

Like the rest of ``trajectory.*`` they answer ``trajectory_disabled`` while the
trajectory policy is off. Scanning the span logs and resolving the session
file happen in a worker thread, and a selection is validated before the job
exists, so an invalid one is answered with ``invalid_params`` naming each
offending turn rather than surfacing later as a failed job. Whether the
session is answering right now is read on the loop (it is in memory) and
handed to the scan, which needs it to tell a running turn from a dead one.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from raven.rpc.errors import InvalidParamsError, TrajectoryTaskLimitError, TrajectoryTaskNotFoundError
from raven.rpc.methods.trajectory import _params, _require_enabled, _session_key
from raven.rpc.models import (
    TrajectoryExportCancelParams,
    TrajectoryExportParams,
    TrajectoryExportStatusParams,
    TrajectoryTurnsParams,
)
from raven.trajectory import export_jobs, selection
from raven.trajectory.export import clean_file_name

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher
    from raven.session.manager import SessionManager


def _answering(session_key: str) -> bool:
    from raven.rpc.methods.turn import is_session_answering

    return is_session_answering(session_key)


def _manager(agent_loop_factory: Any) -> "SessionManager":
    from raven.config import load_config
    from raven.rpc.methods.session import _safe_invoke_factory
    from raven.session.resolve import manager_for

    return manager_for(_safe_invoke_factory(agent_loop_factory), load_config())


def _invalid_selection(exc: selection.SelectionError) -> InvalidParamsError:
    data: dict[str, Any] = {
        "reason": "invalid_selection",
        "invalid": [{"trace_id": trace_id, "why": why} for trace_id, why in exc.invalid],
    }
    if exc.why is not None:
        data["why"] = exc.why
    return InvalidParamsError(str(exc), data=data)


def _turn_row(turn: selection.TurnSummary) -> dict[str, Any]:
    return {
        "trace_id": turn.trace_id,
        "number": turn.number,
        "start": turn.start,
        "input_preview": turn.input_preview,
        "in_progress": turn.in_progress,
    }


async def trajectory_turns(params: dict, *, agent_loop_factory: Any = None) -> dict:
    request = _params(TrajectoryTurnsParams, params)
    _require_enabled()
    session_key = _session_key(request.session_key)
    answering = _answering(session_key)

    def read() -> dict:
        turns = selection.session_turns(session_key, answering=answering)
        session = _manager(agent_loop_factory).peek(session_key)
        title = (session.metadata or {}).get("title") if session is not None else None
        return {"title": title if isinstance(title, str) else "", "turns": [_turn_row(t) for t in turns]}

    return await asyncio.to_thread(read)


async def trajectory_export(params: dict, *, agent_loop_factory: Any = None) -> dict:
    request = _params(TrajectoryExportParams, params)
    _require_enabled()
    session_key = _session_key(request.session_key)
    file_name = clean_file_name(request.file_name)
    answering = _answering(session_key)

    def plan() -> tuple[selection.SelectionPlan, Any]:
        chosen = selection.plan_selection(session_key, request.trace_ids, answering=answering)
        return chosen, _manager(agent_loop_factory).session_path(session_key)

    try:
        chosen, session_path = await asyncio.to_thread(plan)
    except selection.SelectionError as exc:
        raise _invalid_selection(exc) from None
    try:
        job_id = export_jobs.JOBS.start(chosen, file_name=file_name, session_path=session_path)
    except export_jobs.JobLimitError as exc:
        raise TrajectoryTaskLimitError(str(exc), data={"limit": exc.limit}) from None
    return {"job_id": job_id}


async def trajectory_export_status(params: dict) -> dict:
    request = _params(TrajectoryExportStatusParams, params)
    _require_enabled()
    try:
        return export_jobs.JOBS.status(request.job_id)
    except export_jobs.JobNotFoundError as exc:
        raise TrajectoryTaskNotFoundError(str(exc), data={"id": exc.job_id}) from None


async def trajectory_export_cancel(params: dict) -> dict:
    request = _params(TrajectoryExportCancelParams, params)
    _require_enabled()
    try:
        export_jobs.JOBS.cancel(request.job_id)
    except export_jobs.JobNotFoundError as exc:
        raise TrajectoryTaskNotFoundError(str(exc), data={"id": exc.job_id}) from None
    return {"ok": True}


def register_trajectory_export_methods(dispatcher: "Dispatcher", *, agent_loop_factory: Any = None) -> None:
    async def _turns(params: dict) -> dict:
        return await trajectory_turns(params, agent_loop_factory=agent_loop_factory)

    async def _export(params: dict) -> dict:
        return await trajectory_export(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("trajectory.turns", _turns)
    dispatcher.register("trajectory.export", _export)
    dispatcher.register("trajectory.export_status", trajectory_export_status)
    dispatcher.register("trajectory.export_cancel", trajectory_export_cancel)


__all__ = [
    "register_trajectory_export_methods",
    "trajectory_export",
    "trajectory_export_cancel",
    "trajectory_export_status",
    "trajectory_turns",
]
