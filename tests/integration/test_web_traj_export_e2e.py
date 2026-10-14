"""The web page's trajectory export, from the turn list to the downloaded package.

Everything the page reaches runs for real: the ``trajectory.*`` handlers on a
dispatcher, the export job writing the package from a real span log, its
artifacts, its v2 message blobs and the session record, and the download route
serving it over HTTP. Two things are stood in for, as the RPC tests do: whether
the session is answering (there is no agent loop here) and where the session
manager keeps the record.
"""

from __future__ import annotations

import asyncio
import io
import json
import tarfile
import threading
import time
from pathlib import Path
from typing import Any, Iterator

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods import trajectory_export as rpc_export
from raven.rpc.methods.trajectory_export import register_trajectory_export_methods
from raven.rpc.transports.trajectory_downloads import add_export_routes
from raven.session.manager import Session, SessionManager
from raven.tracing import artifact_v2
from raven.trajectory import export as ex
from raven.trajectory import export_jobs
from raven.trajectory import policy as tpol

pytestmark = pytest.mark.integration

SESSION = "tui:trip"
SECRET = "api_key=sk-live-not-redacted-0123456789"


@pytest.fixture
def state(tmp_path: Path, monkeypatch) -> Iterator[Path]:
    root = tmp_path / "traces"
    (root / "logs" / "audit-artifacts").mkdir(parents=True)
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(root))
    tpol._reset_for_tests()
    manager = SessionManager(tmp_path / "workspace")
    monkeypatch.setattr(rpc_export, "_manager", lambda _factory: manager)
    monkeypatch.setattr(rpc_export, "_answering", lambda _key: False)
    monkeypatch.setattr(export_jobs, "JOBS", export_jobs.ExportJobs(lambda: tmp_path / "exports"))
    yield root
    assert export_jobs.JOBS.shutdown(timeout=30), "an export worker outlived its test"
    tpol._reset_for_tests()


def _artifact(state: Path, kind: str, name: str, payload: Any) -> Path:
    path = state / "logs" / "audit-artifacts" / kind / "2026-10-01" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload if isinstance(payload, str) else json.dumps(payload), encoding="utf-8")
    return path


def _blob(state: Path, message: dict[str, Any]) -> str:
    sha1 = artifact_v2.message_sha1(message)
    path = artifact_v2.message_path(state / "logs" / "audit-artifacts", sha1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(artifact_v2.message_text(message), encoding="utf-8")
    return sha1


def _span(trace: str, span: str, name: str, minute: int, *, parent: str | None = None, **attrs: Any) -> dict[str, Any]:
    record: dict[str, Any] = {
        "traceId": trace,
        "spanId": span,
        "name": name,
        "startTime": f"2026-10-01T10:{minute:02d}:00+00:00",
        "endTime": f"2026-10-01T10:{minute:02d}:30+00:00",
        "attributes": {"session.key": SESSION, **attrs},
    }
    if parent:
        record["parentSpanId"] = parent
    return record


@pytest.fixture
def world(state: Path) -> dict[str, Any]:
    """Three turns; the first dispatched a sub-agent; the first and the third are exported."""
    asked = _blob(state, {"role": "user", "content": "plan a weekend in Kyoto"})
    system = _blob(state, {"role": "system", "content": "you are raven"})
    shell = _artifact(
        state,
        "llm.input",
        "t1-llm.json",
        {
            "artifactFormat": artifact_v2.ARTIFACT_FORMAT,
            "messages": [artifact_v2.make_ref(asked)],
            "systemPrompt": artifact_v2.make_ref(system),
        },
    )
    tool_out = _artifact(state, "tool.output", "t1-tool.txt", SECRET)
    sub_out = _artifact(state, "turn.output", "sub-out.json", {"content": "found 3 ryokans"})
    unselected = _artifact(state, "turn.input", "t2-in.json", {"content": "swap day two"})
    third = _artifact(state, "turn.input", "t3-in.json", {"content": "book the ryokan"})
    turn = {"turn.in_progress": False}
    records = [
        _span("trace-1", "s1", "session.turn", 1, **turn, **{"turn.input_preview": "plan a weekend in Kyoto"}),
        _span("trace-1", "s1-llm", "llm.call", 1, parent="s1", **{"llm.input.artifact_path": str(shell)}),
        _span("trace-1", "s1-tool", "tool.call", 1, parent="s1", **{"tool.output.artifact_path": str(tool_out)}),
        _span(
            "trace-sub",
            "sub",
            "session.turn",
            2,
            **{
                "session.key": "subagent:hotels",
                "trace.dispatched_in_trace_id": "trace-1",
                "turn.output.artifact_path": str(sub_out),
            },
        ),
        _span(
            "trace-2",
            "s2",
            "session.turn",
            3,
            **turn,
            **{"turn.input_preview": "swap day two", "turn.input.artifact_path": str(unselected)},
        ),
        _span(
            "trace-3",
            "s3",
            "session.turn",
            5,
            **turn,
            **{"turn.input_preview": "book the ryokan", "turn.input.artifact_path": str(third)},
        ),
    ]
    (state / "logs" / "audit-spans.log").write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")

    session = Session(key=SESSION)
    session.metadata["title"] = "Kyoto weekend"
    for trace, words, answer in (
        ("trace-1", "plan a weekend in Kyoto", "here is a plan"),
        ("trace-2", "swap day two", "swapped"),
        ("trace-3", "book the ryokan", "booked"),
    ):
        session.record({"role": "user", "content": words, "trace_id": trace})
        session.record({"role": "assistant", "content": answer})
    rpc_export._manager(None).save(session)
    return {
        "asked": asked,
        "system": system,
        "tool_out": tool_out,
        "sub_out": sub_out,
        "unselected": unselected,
        "third": third,
    }


def _dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    register_trajectory_export_methods(dispatcher)
    return dispatcher


async def _call(dispatcher: Dispatcher, method: str, params: dict) -> dict:
    response = await dispatcher.dispatch({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
    assert response is not None
    assert "error" not in response, response
    return response["result"]


async def _settled(dispatcher: Dispatcher, job_id: str) -> dict:
    deadline = time.monotonic() + 30
    while True:
        status = await _call(dispatcher, "trajectory.export_status", {"job_id": job_id})
        if status["state"] != "running":
            return status
        assert time.monotonic() < deadline, status
        await asyncio.sleep(0.05)


def _members(data: bytes) -> dict[str, bytes]:
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        return {m.name: tar.extractfile(m).read() for m in tar.getmembers() if m.isfile()}  # type: ignore[union-attr]


async def test_two_selected_turns_download_as_one_package(state, world):
    dispatcher = _dispatcher()
    listed = await _call(dispatcher, "trajectory.turns", {"session_key": SESSION})
    assert listed["title"] == "Kyoto weekend"
    assert [t["trace_id"] for t in listed["turns"]] == ["trace-1", "trace-2", "trace-3"]

    started = await _call(
        dispatcher,
        "trajectory.export",
        {"session_key": SESSION, "trace_ids": ["trace-1", "trace-3"], "file_name": "Kyoto weekend.tar.gz"},
    )
    status = await _settled(dispatcher, started["job_id"])
    assert status["state"] == "done", status

    app = web.Application()
    add_export_routes(app, lambda: export_jobs.JOBS)
    async with TestClient(TestServer(app)) as client:
        response = await client.get(status["download_url"])
        assert response.status == 200
        assert "Kyoto weekend.tar.gz" in response.headers["Content-Disposition"]
        files = _members(await response.read())

    (root,) = {name.split("/", 1)[0] for name in files}
    manifest = json.loads(files[f"{root}/{ex.MANIFEST_FILE}"])
    assert tuple(manifest) == ex.MANIFEST_KEYS
    assert manifest["schema"] == ex.EXPORT_SCHEMA and manifest["format_version"] == ex.EXPORT_FORMAT_VERSION == 1
    assert manifest["source"] == {"session_key": SESSION, "title": "Kyoto weekend", "transcript_included": True}
    assert [t["trace_id"] for t in manifest["turns"]] == ["trace-1", "trace-3"]
    assert manifest["traces"] == {"selected": ["trace-1", "trace-3"], "dispatched": ["trace-sub"]}

    spans = [json.loads(line) for line in files[f"{root}/{ex.SPANS_FILE}"].decode("utf-8").splitlines()]
    assert {s["traceId"] for s in spans} == {"trace-1", "trace-sub", "trace-3"}
    assert manifest["counts"]["spans"] == len(spans)

    artifacts = {name: data for name, data in files.items() if name.startswith(f"{root}/{ex.ARTIFACTS_DIR}/")}
    assert manifest["counts"]["artifacts"] == len(artifacts) == 4
    assert SECRET.encode() in artifacts.values(), "the export is the raw trace, not redacted"
    assert not any(name.endswith("t2-in.json") for name in artifacts)
    pointers = [v for s in spans for k, v in s["attributes"].items() if k.endswith(".artifact_path")]
    assert pointers and all(f"{root}/{p}" in files for p in pointers), "every pointer names a member of the package"

    blobs = {name for name in files if name.startswith(f"{root}/{ex.MESSAGES_DIR}/")}
    assert {name.rsplit("/", 1)[-1] for name in blobs} == {f"{world['asked']}.json", f"{world['system']}.json"}
    assert manifest["counts"]["messages"] == 2

    rows = [json.loads(line) for line in files[f"{root}/{ex.SESSION_FILE}"].decode("utf-8").splitlines()]
    said = [r.get("content") for r in rows if r.get("role") in ("user", "assistant")]
    assert said == ["plan a weekend in Kyoto", "here is a plan", "book the ryokan", "booked"]


async def test_a_cancelled_export_leaves_no_package(state, world, tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real_write = export_jobs.write_export

    def held_write(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(10)
        return real_write(*args, **kwargs)

    monkeypatch.setattr(export_jobs, "write_export", held_write)
    dispatcher = _dispatcher()
    try:
        started = await _call(
            dispatcher, "trajectory.export", {"session_key": SESSION, "trace_ids": ["trace-1"], "file_name": "x.tar.gz"}
        )
        assert await asyncio.to_thread(entered.wait, 10)
        await _call(dispatcher, "trajectory.export_cancel", {"job_id": started["job_id"]})
        status = await _call(dispatcher, "trajectory.export_status", {"job_id": started["job_id"]})
        assert status["state"] == "cancelled" and status["download_url"] is None
    finally:
        release.set()
    # The status says cancelled at once; the worker still writes, sees the
    # cancel and removes what it wrote. Only once the registry has no worker
    # and no cleaner left is an empty directory a result rather than a moment.
    assert await asyncio.to_thread(export_jobs.JOBS.shutdown, timeout=30)
    exports = tmp_path / "exports"
    assert not exports.is_dir() or not any(exports.rglob("*")), sorted(exports.rglob("*"))
