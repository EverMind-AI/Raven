"""Tests for ``trajectory.turns`` and ``trajectory.export*`` (`raven.rpc.methods.trajectory_export`)."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods import trajectory_export as rpc_export
from raven.rpc.methods.trajectory_export import register_trajectory_export_methods
from raven.rpc.models import TrajectoryExportStatusResult, TrajectoryTurnsResult
from raven.session.manager import Session, SessionManager
from raven.trajectory import export_jobs, selection
from raven.trajectory import policy as tpol

SESSION = "tui:s1"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    state = tmp_path / "traces"
    (state / "logs").mkdir(parents=True)
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(state))
    tpol._reset_for_tests()
    manager = SessionManager(tmp_path / "workspace")
    monkeypatch.setattr(rpc_export, "_manager", lambda _factory: manager)
    monkeypatch.setattr(rpc_export, "_answering", lambda _key: False)
    jobs = export_jobs.ExportJobs(lambda: tmp_path / "exports")
    monkeypatch.setattr(export_jobs, "JOBS", jobs)
    yield
    tpol._reset_for_tests()


@pytest.fixture
def jobs() -> export_jobs.ExportJobs:
    return export_jobs.JOBS


def _turn(trace: str, minute: int, *, checkpoint: bool = False, dispatched_in: str | None = None) -> dict[str, Any]:
    attrs: dict[str, Any] = {"session.key": SESSION, "turn.in_progress": checkpoint, "turn.input_preview": trace}
    if dispatched_in:
        attrs["trace.dispatched_in_trace_id"] = dispatched_in
    return {
        "traceId": trace,
        "spanId": f"span-{trace}",
        "name": "session.turn",
        "startTime": f"2026-10-01T10:{minute:02d}:00+00:00",
        "endTime": f"2026-10-01T10:{minute:02d}:30+00:00",
        "attributes": attrs,
    }


def _write(tmp_path: Path, *records: dict[str, Any]) -> None:
    log = tmp_path / "traces" / "logs" / "audit-spans.log"
    with log.open("a", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record) + "\n")


def _session(title: str = "Trip") -> None:
    manager = rpc_export._manager(None)
    session = Session(key=SESSION)
    session.metadata["title"] = title
    session.record({"role": "user", "content": "trace-a", "trace_id": "trace-a"})
    manager.save(session)


def _dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    register_trajectory_export_methods(dispatcher)
    return dispatcher


async def _call(dispatcher: Dispatcher, method: str, params: dict) -> dict:
    response = await dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    assert response is not None
    return response


async def _wait_done(dispatcher: Dispatcher, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while True:
        status = (await _call(dispatcher, "trajectory.export_status", {"job_id": job_id}))["result"]
        if status["state"] != "running":
            return status
        assert time.monotonic() < deadline
        await asyncio.sleep(0.02)


# -- trajectory.turns ----------------------------------------------------------


async def test_turns_lists_the_main_line_and_the_title(tmp_path):
    _write(tmp_path, _turn("trace-a", 1), _turn("trace-sub", 2, dispatched_in="trace-a"), _turn("trace-b", 3))
    _session("Trip to Kyoto")
    response = await _call(_dispatcher(), "trajectory.turns", {"session_key": SESSION})
    result = TrajectoryTurnsResult.model_validate(response["result"]).model_dump()
    assert result["title"] == "Trip to Kyoto"
    assert [(t["trace_id"], t["number"], t["in_progress"]) for t in result["turns"]] == [
        ("trace-a", 1, False),
        ("trace-b", 2, False),
    ]


async def test_turns_reports_the_running_turn_as_in_progress(tmp_path, monkeypatch):
    _write(tmp_path, _turn("trace-a", 1), _turn("trace-b", 3, checkpoint=True))
    monkeypatch.setattr(rpc_export, "_answering", lambda _key: True)
    turns = (await _call(_dispatcher(), "trajectory.turns", {"session_key": SESSION}))["result"]["turns"]
    assert [t["in_progress"] for t in turns] == [False, True]


async def test_an_unknown_session_has_no_turns_and_no_title():
    result = (await _call(_dispatcher(), "trajectory.turns", {"session_key": "tui:none"}))["result"]
    assert result == {"title": "", "turns": []}


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("trajectory.turns", {"session_key": SESSION, "extra": 1}),
        ("trajectory.turns", {"session_key": 7}),
        ("trajectory.export", {"session_key": SESSION, "trace_ids": "trace-a", "file_name": "x"}),
        ("trajectory.export", {"session_key": SESSION, "trace_ids": ["trace-a"]}),
        ("trajectory.export_status", {"job_id": 3}),
        ("trajectory.export_cancel", {}),
    ],
)
async def test_params_are_strict(method, params):
    assert (await _call(_dispatcher(), method, params))["error"]["code"] == -32602


async def test_an_empty_session_key_is_not_a_session():
    response = await _call(_dispatcher(), "trajectory.turns", {"session_key": ""})
    assert response["error"]["code"] == -32001


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("trajectory.turns", {"session_key": SESSION}),
        ("trajectory.export", {"session_key": SESSION, "trace_ids": ["trace-a"], "file_name": "x"}),
        ("trajectory.export_status", {"job_id": "exj-x"}),
        ("trajectory.export_cancel", {"job_id": "exj-x"}),
    ],
)
async def test_every_method_refuses_while_the_policy_is_off(method, params):
    tpol.arm(False)
    assert (await _call(_dispatcher(), method, params))["error"]["code"] == -32020


# -- trajectory.export ---------------------------------------------------------


async def test_an_export_runs_to_a_download(tmp_path, jobs):
    _write(tmp_path, _turn("trace-a", 1))
    _session()
    dispatcher = _dispatcher()
    started = await _call(
        dispatcher, "trajectory.export", {"session_key": SESSION, "trace_ids": ["trace-a"], "file_name": "../trip"}
    )
    status = await _wait_done(dispatcher, started["result"]["job_id"])
    TrajectoryExportStatusResult.model_validate(status)
    assert status["state"] == "done" and status["file_name"] == "trip.tar.gz"
    assert status["download_url"].startswith("/trajectory/export/download?token=")
    assert (await _call(dispatcher, "trajectory.export_cancel", {"job_id": status["job_id"]}))["result"] == {"ok": True}


async def test_the_running_turn_is_refused_before_any_job_exists(tmp_path, jobs, monkeypatch):
    _write(tmp_path, _turn("trace-a", 1), _turn("trace-b", 3, checkpoint=True))
    monkeypatch.setattr(rpc_export, "_answering", lambda _key: True)
    response = await _call(
        _dispatcher(), "trajectory.export", {"session_key": SESSION, "trace_ids": ["trace-b"], "file_name": "x"}
    )
    error = response["error"]
    assert error["code"] == -32602
    assert error["data"]["reason"] == "invalid_selection"
    assert error["data"]["invalid"] == [{"trace_id": "trace-b", "why": "in_progress"}]
    assert jobs._jobs == {}


async def test_a_checkpoint_left_by_a_dead_process_is_exported(tmp_path):
    _write(tmp_path, _turn("trace-a", 1), _turn("trace-b", 3, checkpoint=True))
    dispatcher = _dispatcher()
    started = await _call(
        dispatcher, "trajectory.export", {"session_key": SESSION, "trace_ids": ["trace-b"], "file_name": "x"}
    )
    assert (await _wait_done(dispatcher, started["result"]["job_id"]))["state"] == "done"


async def test_a_trace_outside_the_main_line_is_refused(tmp_path):
    _write(tmp_path, _turn("trace-a", 1), _turn("trace-sub", 2, dispatched_in="trace-a"))
    response = await _call(
        _dispatcher(), "trajectory.export", {"session_key": SESSION, "trace_ids": ["trace-sub"], "file_name": "x"}
    )
    assert response["error"]["code"] == -32602
    assert response["error"]["data"]["invalid"] == [{"trace_id": "trace-sub", "why": "not_in_session"}]


async def test_an_empty_selection_is_refused():
    response = await _call(
        _dispatcher(), "trajectory.export", {"session_key": SESSION, "trace_ids": [], "file_name": "x"}
    )
    assert response["error"]["code"] == -32602
    assert response["error"]["data"]["why"] == "empty" and response["error"]["data"]["invalid"] == []


async def test_the_job_limit_is_reported_with_its_size(tmp_path, monkeypatch):
    _write(tmp_path, _turn("trace-a", 1))
    monkeypatch.setattr(export_jobs, "JOBS", export_jobs.ExportJobs(lambda: tmp_path / "exports", limit=0))
    response = await _call(
        _dispatcher(), "trajectory.export", {"session_key": SESSION, "trace_ids": ["trace-a"], "file_name": "x"}
    )
    assert response["error"]["code"] == -32025 and response["error"]["data"]["limit"] == 0


@pytest.mark.parametrize("method", ["trajectory.export_status", "trajectory.export_cancel"])
async def test_an_unknown_job_is_reported_with_its_id(method):
    response = await _call(_dispatcher(), method, {"job_id": "exj-nope"})
    assert response["error"]["code"] == -32024 and response["error"]["data"]["id"] == "exj-nope"


async def test_validation_scans_off_the_loop(tmp_path, monkeypatch):
    """While the selection is being checked, the loop keeps answering."""
    release = threading.Event()
    entered = threading.Event()
    real = selection.plan_selection

    def slow_plan(*args: Any, **kwargs: Any):
        entered.set()
        assert release.wait(5)
        return real(*args, **kwargs)

    monkeypatch.setattr(selection, "plan_selection", slow_plan)
    _write(tmp_path, _turn("trace-a", 1))
    dispatcher = _dispatcher()
    pending = asyncio.create_task(
        _call(dispatcher, "trajectory.export", {"session_key": SESSION, "trace_ids": ["trace-a"], "file_name": "x"})
    )
    while not entered.is_set():
        await asyncio.sleep(0.01)
    answered = await asyncio.wait_for(_call(dispatcher, "trajectory.export_status", {"job_id": "exj-x"}), 1)
    assert answered["error"]["code"] == -32024
    release.set()
    started = await asyncio.wait_for(pending, 5)
    assert (await _wait_done(dispatcher, started["result"]["job_id"]))["state"] == "done"
