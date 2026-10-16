"""``bugreport.*``: the web page's bug report, one Report Session per report.

- ``bugreport.start`` -- validate the problem fields and the turn selection, then
  start collecting; answers a handle the page polls.
- ``bugreport.status`` -- the phase, the items to decide (each value in full and
  masked), the summary, the receipt.
- ``bugreport.item`` -- one item's contexts, asked for one at a time.
- ``bugreport.decide`` / ``bugreport.upload`` / ``bugreport.retry_upload`` /
  ``bugreport.cancel`` -- move the report on.

Like ``trajectory.*`` they answer ``trajectory_disabled`` while the trajectory
policy is off. ``bugreport.start`` also needs a valid upload URL, the same
condition under which the handshake announces ``bugreport-v1``; the other
methods do not, so a report already started can still finish or be cancelled.
Every handler only reads or moves in-memory state, except the selection scan
in ``bugreport.start``, which runs in a worker thread before the report
exists.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from raven.rpc.errors import (
    BugReportStateError,
    BugReportUnavailableError,
    InvalidParamsError,
    ReviewConflictRpcError,
    TrajectoryTaskLimitError,
    TrajectoryTaskNotFoundError,
)
from raven.rpc.methods.system import bug_report_receiver_configured
from raven.rpc.methods.trajectory import _params, _require_enabled, _session_key
from raven.rpc.methods.trajectory_export import _answering, _invalid_selection, _manager
from raven.rpc.models import (
    BugReportCancelParams,
    BugReportDecideParams,
    BugReportItemParams,
    BugReportRetryParams,
    BugReportStartParams,
    BugReportStatusParams,
    BugReportUploadParams,
)
from raven.trajectory import report_session, selection

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher


def _not_found(exc: report_session.ReportNotFoundError) -> TrajectoryTaskNotFoundError:
    return TrajectoryTaskNotFoundError(str(exc), data={"id": exc.report_id})


def _state(exc: report_session.ReportStateError) -> BugReportStateError:
    data: dict[str, Any] = {"phase": exc.phase, "reason": exc.reason}
    if exc.item_ids:
        data["item_ids"] = list(exc.item_ids)
    return BugReportStateError(str(exc), data=data)


def _guarded(call: Any) -> Any:
    try:
        return call()
    except report_session.ReportNotFoundError as exc:
        raise _not_found(exc) from None
    except report_session.ReportStateError as exc:
        raise _state(exc) from None
    except report_session.ReportConflictError as exc:
        raise ReviewConflictRpcError(str(exc), data={"item_ids": list(exc.item_ids)}) from None


async def bugreport_start(params: dict, *, agent_loop_factory: Any = None) -> dict:
    request = _params(BugReportStartParams, params)
    _require_enabled()
    if not bug_report_receiver_configured():
        raise BugReportUnavailableError("no bug report receiver is configured")
    session_key = _session_key(request.session_key)
    problem = request.problem.model_dump()
    if not problem["description"].strip():
        raise InvalidParamsError("problem.description: a problem description is required")
    answering = _answering(session_key)

    def plan() -> tuple[selection.SelectionPlan, Any, Any]:
        chosen = selection.plan_selection(session_key, request.trace_ids, answering=answering)
        manager = _manager(agent_loop_factory)
        return chosen, manager.workspace, manager.session_path(session_key)

    try:
        chosen, workspace, session_path = await asyncio.to_thread(plan)
    except selection.SelectionError as exc:
        raise _invalid_selection(exc) from None
    try:
        report_id = report_session.REPORTS.start(chosen, problem, workspace=workspace, session_path=session_path)
    except report_session.ReportLimitError as exc:
        raise TrajectoryTaskLimitError(str(exc), data={"limit": exc.limit}) from None
    return {"report_id": report_id}


async def bugreport_status(params: dict) -> dict:
    request = _params(BugReportStatusParams, params)
    _require_enabled()
    return _guarded(lambda: report_session.REPORTS.status(request.report_id))


async def bugreport_item(params: dict) -> dict:
    request = _params(BugReportItemParams, params)
    _require_enabled()
    return _guarded(lambda: report_session.REPORTS.item(request.report_id, request.item_id))


async def bugreport_decide(params: dict) -> dict:
    request = _params(BugReportDecideParams, params)
    _require_enabled()
    decisions = [(d.item_id, d.action) for d in request.decisions]
    _guarded(lambda: report_session.REPORTS.decide(request.report_id, decisions, list(request.acknowledged)))
    return {"ok": True}


async def bugreport_upload(params: dict) -> dict:
    request = _params(BugReportUploadParams, params)
    _require_enabled()
    _guarded(lambda: report_session.REPORTS.upload(request.report_id, risk_accepted=request.risk_accepted))
    return {"ok": True}


async def bugreport_retry_upload(params: dict) -> dict:
    request = _params(BugReportRetryParams, params)
    _require_enabled()
    _guarded(lambda: report_session.REPORTS.retry_upload(request.report_id))
    return {"ok": True}


async def bugreport_cancel(params: dict) -> dict:
    request = _params(BugReportCancelParams, params)
    _require_enabled()
    _guarded(lambda: report_session.REPORTS.cancel(request.report_id))
    return {"ok": True}


def register_bugreport_methods(dispatcher: "Dispatcher", *, agent_loop_factory: Any = None) -> None:
    async def _start(params: dict) -> dict:
        return await bugreport_start(params, agent_loop_factory=agent_loop_factory)

    dispatcher.register("bugreport.start", _start)
    dispatcher.register("bugreport.status", bugreport_status)
    dispatcher.register("bugreport.item", bugreport_item)
    dispatcher.register("bugreport.decide", bugreport_decide)
    dispatcher.register("bugreport.upload", bugreport_upload)
    dispatcher.register("bugreport.retry_upload", bugreport_retry_upload)
    dispatcher.register("bugreport.cancel", bugreport_cancel)


__all__ = [
    "bugreport_cancel",
    "bugreport_decide",
    "bugreport_item",
    "bugreport_retry_upload",
    "bugreport_start",
    "bugreport_status",
    "bugreport_upload",
    "register_bugreport_methods",
]
