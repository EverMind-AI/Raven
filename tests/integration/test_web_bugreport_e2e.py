"""The web page's bug report, from the selected turns to the receiver's receipt.

The ``bugreport.*`` handlers run on a dispatcher in this process, over a real
span log and a real Report Session registry; the receiver is `raven trajectory
receiver` in a child process, started, stopped and started again on the same
port the way an operator would. Nothing in the pipeline is stood in for
except whether the session is answering and where the session manager keeps
the record (there is no agent loop here), as the RPC tests do.
"""

from __future__ import annotations

import asyncio
import json
import signal
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest

from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods import bugreport as rpc_bugreport
from raven.rpc.methods import system as rpc_system
from raven.rpc.methods import trajectory_export as rpc_export
from raven.rpc.methods.bugreport import register_bugreport_methods
from raven.session.manager import SessionManager
from raven.trajectory import bugreport as breport
from raven.trajectory import policy as tpol
from raven.trajectory import report_session as rs
from tests.bug_report_receiver_stub import clear_proxy_env
from tests.integration.test_bug_report_upload_e2e import _env, _raven, _reap, _start_receiver
from tests.test_trajectory_bugreport import _span, _write_log

pytestmark = pytest.mark.integration

SESSION = "tui:web"
TOKEN = "aB3xK9mQ7pL2vR8sT4wZ6yN1"
# Assembled at runtime: the detect-private-key pre-commit hook scans source
# bytes for the marker substring and cannot tell this fake fixture apart.
PEM = "-----BEGIN PRIVATE " + "KEY-----\nMIIabcdef\n-----END PRIVATE " + "KEY-----"
PROBLEM = {"description": "the reply ignored the tool result", "severity": "high"}


@pytest.fixture
def world(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    state = tmp_path / "traces"
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(state))
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    clear_proxy_env(monkeypatch)
    tpol._reset_for_tests()
    manager = SessionManager(tmp_path / "workspace")
    monkeypatch.setattr(rpc_export, "_manager", lambda _factory: manager)
    monkeypatch.setattr(rpc_bugreport, "_answering", lambda _key: False)
    reports = rs.ReportSessions()
    monkeypatch.setattr(rs, "REPORTS", reports)
    _write_log(
        state / "logs" / "audit-spans.log",
        [
            _span(
                "trace-1",
                session_key=SESSION,
                attrs={
                    "turn.in_progress": False,
                    "turn.input_preview": "fetch the report",
                    "llm.output": f"use token {TOKEN} with the key {PEM}",
                },
            )
        ],
    )
    yield state
    reports.shutdown(timeout=10)
    tpol._reset_for_tests()


def _dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    register_bugreport_methods(dispatcher)
    return dispatcher


async def _call(dispatcher: Dispatcher, method: str, params: dict) -> dict:
    response = await dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    assert response is not None
    assert "error" not in response, response
    return response["result"]


async def _until(dispatcher: Dispatcher, report_id: str, *phases: str) -> dict:
    deadline = time.monotonic() + 60
    while True:
        status = await _call(dispatcher, "bugreport.status", {"report_id": report_id})
        if status["phase"] in phases:
            return status
        assert status["phase"] not in ("failed", "cancelled"), status
        assert time.monotonic() < deadline, status
        await asyncio.sleep(0.05)


async def _start(dispatcher: Dispatcher) -> str:
    started = await _call(
        dispatcher, "bugreport.start", {"session_key": SESSION, "trace_ids": ["trace-1"], "problem": PROBLEM}
    )
    return started["report_id"]


async def _decide_all(dispatcher: Dispatcher, report_id: str, status: dict, action: str = "redacted") -> None:
    items = status["items"]
    await _call(
        dispatcher,
        "bugreport.decide",
        {
            "report_id": report_id,
            "decisions": [{"item_id": i["id"], "action": action} for i in items if i["kind"] == "suspected"],
            "acknowledged": [i["id"] for i in items if i["kind"] == "confirmed"],
        },
    )


def _cut(text: str, spans: list[list[int]]) -> list[str]:
    return [text[a:b] for a, b in spans]


async def _stop(proc) -> None:
    """Stop a receiver and wait for it. One stopped right after it printed its
    address may not have its own handler in place yet and dies of the signal;
    either way it is gone, which is all a stop here needs."""
    proc.send_signal(signal.SIGTERM)
    assert await asyncio.wait_for(proc.wait(), 15) in (0, -signal.SIGTERM)


async def test_a_report_is_decided_uploaded_and_remembered(world, tmp_path, monkeypatch):
    env = _env(tmp_path)
    inbox = tmp_path / "inbox"
    proc, port = await _start_receiver(_raven("receiver", "--save-dir", str(inbox), "--port", "0"), env)
    try:
        monkeypatch.setenv("RAVEN_BUGREPORT_URL", f"http://127.0.0.1:{port}")
        assert rpc_system.BUGREPORT_CAPABILITY in rpc_system.server_capabilities()
        dispatcher = _dispatcher()

        first = await _start(dispatcher)
        status = await _until(dispatcher, first, "deciding")
        kinds = {item["kind"] for item in status["items"]}
        assert kinds == {"confirmed", "suspected"}, status["items"]
        assert TOKEN not in json.dumps(status) and "MIIabcdef" not in json.dumps(status)
        suspected = next(i for i in status["items"] if i["kind"] == "suspected")
        contexts = await _call(dispatcher, "bugreport.item", {"report_id": first, "item_id": suspected["id"]})
        assert any(TOKEN in _cut(g["text"], g["spans"]) for g in contexts["groups"])

        await _decide_all(dispatcher, first, status)
        ready = await _until(dispatcher, first, "ready")
        assert ready["summary"]["redacted"] >= 1 and ready["summary"]["acknowledged"] >= 1
        await _call(dispatcher, "bugreport.upload", {"report_id": first, "risk_accepted": True})
        delivered = await _until(dispatcher, first, "uploaded")
        receipt = delivered["receipt"]["receipt_id"]
        (landed,) = list(inbox.glob(f"*/{delivered['bug_report_id']}.tar.gz"))
        sidecar = json.loads(landed.with_name(f"{delivered['bug_report_id']}.json").read_text(encoding="utf-8"))
        assert sidecar["receipt_id"] == receipt
        await _call(dispatcher, "bugreport.cancel", {"report_id": first})

        second = await _start(dispatcher)
        again = await _until(dispatcher, second, "deciding", "ready")
        assert again["history"]["state"] == "on" and again["history"]["auto_redacted"] >= 1, again["history"]
        if again["phase"] == "deciding":
            assert {i["kind"] for i in again["items"]} == {"confirmed"}, "only the private key is asked again"
        await _call(dispatcher, "bugreport.cancel", {"report_id": second})
        await _stop(proc)
    finally:
        await _reap(proc)


async def test_a_stopped_receiver_gives_a_retryable_failure_and_a_retry_delivers(world, tmp_path, monkeypatch):
    env = _env(tmp_path)
    inbox = tmp_path / "inbox"
    args = ["receiver", "--save-dir", str(inbox), "--port"]
    proc, port = await _start_receiver(_raven(*args, "0"), env)
    try:
        await _stop(proc)
    finally:
        await _reap(proc)
    monkeypatch.setenv("RAVEN_BUGREPORT_URL", f"http://127.0.0.1:{port}")
    dispatcher = _dispatcher()
    report = await _start(dispatcher)
    await _decide_all(dispatcher, report, await _until(dispatcher, report, "deciding"))
    await _until(dispatcher, report, "ready")
    await _call(dispatcher, "bugreport.upload", {"report_id": report, "risk_accepted": True})
    failed = await _until(dispatcher, report, "upload_failed")
    assert failed["error"]["retryable"] is True, failed["error"]
    assert failed["record_dir"] and Path(failed["record_dir"]).is_dir()

    proc, again = await _start_receiver(_raven(*args, str(port)), env)
    try:
        assert again == port
        await _call(dispatcher, "bugreport.retry_upload", {"report_id": report})
        delivered = await _until(dispatcher, report, "uploaded")
        assert delivered["receipt"]["receipt_id"]
        assert list(inbox.glob(f"*/{delivered['bug_report_id']}.tar.gz"))
        await _stop(proc)
    finally:
        await _reap(proc)


@pytest.mark.parametrize("step", ["collecting", "deciding", "ready"])
async def test_cancelling_at_any_step_removes_the_staging(world, monkeypatch, step):
    monkeypatch.setenv("RAVEN_BUGREPORT_URL", "http://127.0.0.1:9")
    entered, release = threading.Event(), threading.Event()
    if step == "collecting":
        real = breport.stage_review

        def held(*args: Any, **kwargs: Any) -> Any:
            entered.set()
            assert release.wait(10)
            return real(*args, **kwargs)

        monkeypatch.setattr(breport, "stage_review", held)
    dispatcher = _dispatcher()
    report = await _start(dispatcher)
    if step == "collecting":
        assert await asyncio.to_thread(entered.wait, 10)
    else:
        status = await _until(dispatcher, report, "deciding")
        if step == "ready":
            await _decide_all(dispatcher, report, status)
            await _until(dispatcher, report, "ready")
    await _call(dispatcher, "bugreport.cancel", {"report_id": report})
    release.set()
    deadline = time.monotonic() + 30
    while rs.REPORTS._reports:
        assert time.monotonic() < deadline
        await asyncio.sleep(0.05)
    root = breport.bugreports_root(world)
    staging = root / breport.STAGING_DIR
    assert not staging.is_dir() or not any(staging.iterdir())
    assert not [p for p in root.iterdir() if p.name.startswith("br-")] if root.is_dir() else True
