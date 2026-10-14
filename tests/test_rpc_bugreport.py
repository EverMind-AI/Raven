"""Tests for the ``bugreport.*`` RPC handlers (`raven.rpc.methods.bugreport`)."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods import bugreport as rpc_bugreport
from raven.rpc.methods import system as rpc_system
from raven.rpc.methods import trajectory_export as rpc_export
from raven.rpc.methods.bugreport import register_bugreport_methods
from raven.rpc.models import BugReportItemResult, BugReportStatusResult
from raven.session.manager import SessionManager
from raven.trajectory import bugreport as breport
from raven.trajectory import policy as tpol
from raven.trajectory import redaction_history as rhistory
from raven.trajectory import report_session as rs
from raven.trajectory.upload import UploadReceipt

SESSION = "tui:s1"
TOKEN = "aB3xK9mQ7pL2vR8sT4wZ6yN1"


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch):
    state = tmp_path / "traces"
    (state / "logs").mkdir(parents=True)
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(state))
    monkeypatch.setenv("RAVEN_BUGREPORT_URL", "https://receiver.example")
    monkeypatch.setattr(breport, "collect_known_secrets", lambda _p: ([], True))
    tpol._reset_for_tests()
    manager = SessionManager(tmp_path / "workspace")
    monkeypatch.setattr(rpc_export, "_manager", lambda _factory: manager)
    monkeypatch.setattr(rpc_export, "_answering", lambda _key: False)
    monkeypatch.setattr(rpc_bugreport, "_answering", lambda _key: False)
    reports = rs.ReportSessions(history=lambda: rhistory.RedactionHistory(tmp_path / "history.json"))
    monkeypatch.setattr(rs, "REPORTS", reports)
    yield
    reports.shutdown(timeout=10)
    tpol._reset_for_tests()


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


def _dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    register_bugreport_methods(dispatcher)
    return dispatcher


async def _call(dispatcher: Dispatcher, method: str, params: dict) -> dict:
    response = await dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    assert response is not None
    return response


def _start_params(trace_ids: list[str], description: str = f"the token {TOKEN} leaked") -> dict[str, Any]:
    return {
        "session_key": SESSION,
        "trace_ids": trace_ids,
        "problem": {"description": description, "severity": "medium"},
    }


async def _wait(dispatcher: Dispatcher, report_id: str, phase: str) -> dict:
    deadline = time.monotonic() + 10
    while True:
        status = (await _call(dispatcher, "bugreport.status", {"report_id": report_id}))["result"]
        if status["phase"] == phase:
            return status
        assert time.monotonic() < deadline, status
        time.sleep(0.02)


# -- a report through the rpc ----------------------------------------------------


async def test_a_report_is_decided_and_frozen_over_rpc(tmp_path):
    _write(tmp_path, _turn("trace-a", 1))
    dispatcher = _dispatcher()
    started = await _call(dispatcher, "bugreport.start", _start_params(["trace-a"]))
    rid = started["result"]["report_id"]
    status = await _wait(dispatcher, rid, "deciding")
    BugReportStatusResult.model_validate(status)
    assert TOKEN not in json.dumps(status)
    [item] = status["items"]
    contexts = (await _call(dispatcher, "bugreport.item", {"report_id": rid, "item_id": item["id"]}))["result"]
    BugReportItemResult.model_validate(contexts)
    decided = await _call(
        dispatcher,
        "bugreport.decide",
        {"report_id": rid, "decisions": [{"item_id": item["id"], "action": "redacted"}], "acknowledged": []},
    )
    assert decided["result"] == {"ok": True}
    status = await _wait(dispatcher, rid, "ready")
    BugReportStatusResult.model_validate(status)
    refused = await _call(dispatcher, "bugreport.upload", {"report_id": rid, "risk_accepted": False})
    assert refused["error"]["code"] == -32028 and refused["error"]["data"]["reason"] == "risk_not_accepted"
    assert (await _call(dispatcher, "bugreport.cancel", {"report_id": rid}))["result"] == {"ok": True}


# -- validation before the report exists ---------------------------------------------


@pytest.mark.parametrize(
    "params",
    [
        {"session_key": SESSION, "trace_ids": ["trace-a"], "problem": {"description": "x", "severity": "urgent"}},
        {"session_key": SESSION, "trace_ids": ["trace-a"], "problem": {"description": "x", "extra": 1}},
        {"session_key": SESSION, "trace_ids": ["trace-a"]},
        {"session_key": SESSION, "trace_ids": ["trace-a"], "problem": {"description": "   "}},
    ],
)
async def test_bad_problem_fields_are_refused(params, tmp_path):
    _write(tmp_path, _turn("trace-a", 1))
    assert (await _call(_dispatcher(), "bugreport.start", params))["error"]["code"] == -32602
    assert rs.REPORTS._reports == {}


async def test_the_running_turn_is_refused_before_any_report_exists(tmp_path, monkeypatch):
    _write(tmp_path, _turn("trace-a", 1), _turn("trace-b", 3, checkpoint=True))
    monkeypatch.setattr(rpc_bugreport, "_answering", lambda _key: True)
    response = await _call(_dispatcher(), "bugreport.start", _start_params(["trace-b"]))
    assert response["error"]["code"] == -32602
    assert response["error"]["data"]["invalid"] == [{"trace_id": "trace-b", "why": "in_progress"}]
    assert rs.REPORTS._reports == {}
    assert not (tmp_path / "traces" / "bugreports").exists(), "no staging was created"


async def test_a_checkpoint_left_by_a_dead_process_can_be_reported(tmp_path):
    _write(tmp_path, _turn("trace-a", 1), _turn("trace-b", 3, checkpoint=True))
    dispatcher = _dispatcher()
    started = await _call(dispatcher, "bugreport.start", _start_params(["trace-b"], "plain words only"))
    await _wait(dispatcher, started["result"]["report_id"], "ready")


async def test_no_receiver_means_no_report(tmp_path, monkeypatch):
    _write(tmp_path, _turn("trace-a", 1))
    monkeypatch.delenv("RAVEN_BUGREPORT_URL")
    assert (await _call(_dispatcher(), "bugreport.start", _start_params(["trace-a"])))["error"]["code"] == -32026


async def test_the_policy_off_refuses_every_method(tmp_path):
    tpol.arm(False)
    dispatcher = _dispatcher()
    assert (await _call(dispatcher, "bugreport.start", _start_params(["trace-a"])))["error"]["code"] == -32020
    for method, params in (
        ("bugreport.status", {"report_id": "x"}),
        ("bugreport.item", {"report_id": "x", "item_id": "y"}),
        ("bugreport.decide", {"report_id": "x", "decisions": [], "acknowledged": []}),
        ("bugreport.upload", {"report_id": "x", "risk_accepted": True}),
        ("bugreport.retry_upload", {"report_id": "x"}),
        ("bugreport.cancel", {"report_id": "x"}),
    ):
        assert (await _call(dispatcher, method, params))["error"]["code"] == -32020, method


# -- errors carry what the page needs ------------------------------------------------


@pytest.mark.parametrize(
    ("method", "params"),
    [
        ("bugreport.status", {"report_id": "rs-nope"}),
        ("bugreport.item", {"report_id": "rs-nope", "item_id": "x"}),
        ("bugreport.decide", {"report_id": "rs-nope", "decisions": [], "acknowledged": []}),
        ("bugreport.upload", {"report_id": "rs-nope", "risk_accepted": True}),
        ("bugreport.retry_upload", {"report_id": "rs-nope"}),
        ("bugreport.cancel", {"report_id": "rs-nope"}),
    ],
)
async def test_an_unknown_report_is_named(method, params):
    response = await _call(_dispatcher(), method, params)
    assert response["error"]["code"] == -32024 and response["error"]["data"]["id"] == "rs-nope"


async def test_a_full_registry_names_its_limit(tmp_path, monkeypatch):
    _write(tmp_path, _turn("trace-a", 1))
    monkeypatch.setattr(rs, "REPORTS", rs.ReportSessions(limit=0))
    response = await _call(_dispatcher(), "bugreport.start", _start_params(["trace-a"]))
    assert response["error"]["code"] == -32025 and response["error"]["data"]["limit"] == 0


async def test_disagreeing_linked_items_come_back_as_a_conflict(tmp_path):
    inner = "Hj5tR8uE3iO7pA1sD4fGk9lZ"
    _write(tmp_path, _turn("trace-a", 1))
    dispatcher = _dispatcher()
    started = await _call(dispatcher, "bugreport.start", _start_params(["trace-a"], f"{inner} and {inner}W6xC2v"))
    rid = started["result"]["report_id"]
    items = (await _wait(dispatcher, rid, "deciding"))["items"]
    response = await _call(
        dispatcher,
        "bugreport.decide",
        {
            "report_id": rid,
            "decisions": [
                {"item_id": items[0]["id"], "action": "kept"},
                {"item_id": items[1]["id"], "action": "redacted"},
            ],
            "acknowledged": [],
        },
    )
    assert response["error"]["code"] == -32027
    assert sorted(response["error"]["data"]["item_ids"]) == sorted(i["id"] for i in items)


async def test_a_wrong_phase_says_which(tmp_path):
    _write(tmp_path, _turn("trace-a", 1))
    dispatcher = _dispatcher()
    started = await _call(dispatcher, "bugreport.start", _start_params(["trace-a"]))
    rid = started["result"]["report_id"]
    await _wait(dispatcher, rid, "deciding")
    response = await _call(dispatcher, "bugreport.retry_upload", {"report_id": rid})
    assert response["error"]["code"] == -32028
    assert response["error"]["data"]["phase"] == "deciding" and response["error"]["data"]["reason"] == "not_retryable"


async def test_an_uploaded_report_answers_its_status_but_takes_no_more_steps(tmp_path, monkeypatch):
    def delivered(record_dir: Path, *, base_url: str):
        record = breport.load_record(record_dir)
        return record, UploadReceipt("rcpt-1", record["report_id"], record["package"]["sha256"], 1, "now", False)

    monkeypatch.setattr(rs, "upload_record", delivered)
    _write(tmp_path, _turn("trace-a", 1))
    dispatcher = _dispatcher()
    started = await _call(dispatcher, "bugreport.start", _start_params(["trace-a"], "plain words only"))
    rid = started["result"]["report_id"]
    await _wait(dispatcher, rid, "ready")
    await _call(dispatcher, "bugreport.upload", {"report_id": rid, "risk_accepted": True})
    await _wait(dispatcher, rid, "uploaded")
    deadline = time.monotonic() + 10
    while rs.REPORTS._reports:
        assert time.monotonic() < deadline
        time.sleep(0.02)
    status = (await _call(dispatcher, "bugreport.status", {"report_id": rid}))["result"]
    BugReportStatusResult.model_validate(status)
    assert status["receipt"]["receipt_id"] == "rcpt-1"
    response = await _call(dispatcher, "bugreport.retry_upload", {"report_id": rid})
    assert response["error"]["code"] == -32028 and response["error"]["data"]["phase"] == "uploaded"
    assert (await _call(dispatcher, "bugreport.cancel", {"report_id": rid}))["result"] == {"ok": True}
    assert (await _call(dispatcher, "bugreport.status", {"report_id": rid}))["error"]["code"] == -32024


# -- the handshake --------------------------------------------------------------------


def test_the_handshake_announces_bug_reports_only_with_a_valid_receiver(monkeypatch, caplog):
    assert rpc_system.BUGREPORT_CAPABILITY in rpc_system.server_capabilities()
    monkeypatch.delenv("RAVEN_BUGREPORT_URL")
    assert rpc_system.BUGREPORT_CAPABILITY not in rpc_system.server_capabilities()
    monkeypatch.setattr(rpc_system, "_bugreport_url_warned", False)
    warnings: list[str] = []
    monkeypatch.setattr(rpc_system.logger, "warning", lambda message, *args: warnings.append(message))
    monkeypatch.setenv("RAVEN_BUGREPORT_URL", "http://receiver.example")
    assert rpc_system.BUGREPORT_CAPABILITY not in rpc_system.server_capabilities()
    assert rpc_system.BUGREPORT_CAPABILITY not in rpc_system.server_capabilities()
    assert len(warnings) == 1, "an invalid receiver is logged once"
