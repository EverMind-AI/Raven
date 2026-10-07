"""Tests for the ``trajectory.*`` RPC handlers (`raven.rpc.methods.trajectory`)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
from pathlib import Path

import pytest
from aiohttp.test_utils import make_mocked_request

from raven.rpc.dispatcher import Dispatcher
from raven.rpc.methods import trajectory as rpc_trajectory
from raven.rpc.methods.trajectory import register_trajectory_methods
from raven.rpc.models import (
    TrajectoryBlockResult,
    TrajectoryChangesResult,
    TrajectoryDetailResult,
    TrajectoryListResult,
    TrajectoryStateResult,
)
from raven.tracing import artifact_v2
from raven.trajectory import details as tdet
from raven.trajectory import index as tidx
from raven.trajectory import policy as tpol

SESSION = "tui:main"


def _ts(seconds: int) -> str:
    minutes, secs = divmod(seconds, 60)
    return f"2026-09-01T10:{minutes:02d}:{secs:02d}+00:00"


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path: Path):
    state = tmp_path / "traces"
    (state / "logs").mkdir(parents=True)
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(state))
    monkeypatch.delenv("RAVEN_TRACING", raising=False)
    tpol._reset_for_tests()
    tidx._reset_for_tests()
    yield
    tpol._reset_for_tests()
    tidx._reset_for_tests()


@pytest.fixture
def state(tmp_path: Path) -> Path:
    return tmp_path / "traces"


def _append(state: Path, spans: list[dict]) -> None:
    with (state / "logs" / "audit-spans.log").open("ab") as fh:
        for span in spans:
            fh.write(json.dumps(span).encode() + b"\n")


def _artifact(state: Path, payload, name: str) -> str:
    directory = state / "logs" / "audit-artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return str(path)


def _blob(state: Path, message: dict) -> dict:
    sha1 = hashlib.sha1(json.dumps(message, ensure_ascii=False, default=str).encode()).hexdigest()
    path = artifact_v2.message_path(state / "logs" / "audit-artifacts", sha1)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(message), encoding="utf-8")
    return {"$msg": sha1}


def _span(trace, span_id, name, *, parent=None, start=0, end=None, attrs=None, status=None):
    attributes = {"session.key": SESSION}
    attributes.update(attrs or {})
    return {
        "traceId": trace,
        "spanId": span_id,
        "parentSpanId": parent,
        "name": name,
        "startTime": _ts(start),
        "endTime": _ts(end if end is not None else start),
        "status": status if status is not None else {"code": "OK", "message": ""},
        "attributes": attributes,
    }


def _seed(state: Path, *, turns: int = 1, v2_messages: int = 0) -> None:
    spans = []
    for i in range(turns):
        base = i * 100
        spans.append(
            _span(
                "t",
                f"turn{i}",
                "session.turn",
                start=base,
                end=base + 50,
                attrs={
                    "turn.input_preview": f"ask {i}",
                    "turn.input.artifact_path": _artifact(state, {"content": f"ask {i}"}, f"turn{i}-in"),
                    "turn.output_preview": "done",
                    "turn.output.artifact_path": _artifact(state, {"content": "done"}, f"turn{i}-out"),
                },
            )
        )
        attrs = {"tool.name": "search", "tool.args_preview": "{}", "tool.result_preview": "ok"}
        attrs["tool.input.artifact_path"] = _artifact(state, {"name": "search", "params": {"q": i}}, f"tool{i}-in")
        attrs["tool.output.artifact_path"] = _artifact(state, {"result": "ok"}, f"tool{i}-out")
        spans.append(_span("t", f"tool{i}", "tool.call", parent=f"turn{i}", start=base + 1, end=base + 2, attrs=attrs))
        if v2_messages:
            refs = [_blob(state, {"role": "user", "content": f"message {n}"}) for n in range(v2_messages)]
            shell = {"artifactFormat": artifact_v2.ARTIFACT_FORMAT, "messages": refs, "prompt": refs[-1], "tools": []}
            spans.append(
                _span(
                    "t",
                    f"llm{i}",
                    "llm.call",
                    parent=f"turn{i}",
                    start=base + 3,
                    end=base + 4,
                    attrs={"llm.input.artifact_path": _artifact(state, shell, f"llm{i}-in")},
                )
            )
    _append(state, spans)


def _dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    register_trajectory_methods(dispatcher)
    return dispatcher


async def _call(dispatcher: Dispatcher, method: str, params: dict) -> dict:
    frame = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    response = await dispatcher.dispatch(frame)
    assert response is not None
    return response


def _error_code(response: dict) -> int | None:
    error = response.get("error")
    return error["code"] if error else None


async def _list_all(dispatcher: Dispatcher) -> tuple[list[dict], dict]:
    for _ in range(50):
        first = await _call(dispatcher, "trajectory.list", {"session_key": SESSION, "limit": 3})
        assert "result" in first, first
        if first["result"]["index_state"]["phase"] == "ready":
            break
    entries = list(first["result"]["entries"])
    cursor = first["result"]["next_cursor"]
    while cursor:
        page = await _call(dispatcher, "trajectory.list", {"session_key": SESSION, "cursor": cursor, "limit": 3})
        entries += page["result"]["entries"]
        cursor = page["result"]["next_cursor"]
    return entries, first["result"]


# ── policy and state ──────────────────────────────────────────────────


async def test_state_is_always_answerable_and_reports_policy(monkeypatch):
    dispatcher = _dispatcher()
    off = await _call(dispatcher, "trajectory.state", {})
    TrajectoryStateResult.model_validate(off["result"])
    assert off["result"] == {"enabled": False, "policy_revision": 0, "recording_enabled": True}
    tpol.arm(True)
    on = await _call(dispatcher, "trajectory.state", {})
    assert on["result"]["enabled"] is True and on["result"]["policy_revision"] == 1
    monkeypatch.setenv("RAVEN_TRACING", "0")
    assert (await _call(dispatcher, "trajectory.state", {}))["result"]["recording_enabled"] is False


async def test_data_methods_refuse_when_disabled():
    dispatcher = _dispatcher()
    calls = {
        "trajectory.list": {"session_key": SESSION},
        "trajectory.changes": {"session_key": SESSION, "epoch": "e", "after_revision": 0},
        "trajectory.detail": {"session_key": SESSION, "entry_id": "x"},
        "trajectory.block": {
            "session_key": SESSION,
            "entry_id": "x",
            "entry_revision": 1,
            "epoch": "e",
            "block_id": "content",
        },
    }
    for method, params in calls.items():
        response = await _call(dispatcher, method, params)
        assert _error_code(response) == -32020, method


# ── happy paths ───────────────────────────────────────────────────────


async def test_list_pages_then_changes_catch_up(state):
    _seed(state, turns=3)
    tpol.arm(True)
    dispatcher = _dispatcher()
    entries, first = await _list_all(dispatcher)
    TrajectoryListResult.model_validate(first)
    assert len(entries) == 12 and len({e["entry_id"] for e in entries}) == 12
    assert first["complete"] is False and first["index_state"]["phase"] == "ready"
    batch = await _call(
        dispatcher,
        "trajectory.changes",
        {"session_key": SESSION, "epoch": first["epoch"], "after_revision": first["snapshot_revision"]},
    )
    TrajectoryChangesResult.model_validate(batch["result"])
    assert batch["result"]["upserts"] == [] and batch["result"]["reset_required"] is False
    _seed(state, turns=4)
    # The handler's refresh is throttled; force the index forward instead of waiting it out.
    tidx.indexer_for(state).session(SESSION).refresh_sync(None)
    batch = await _call(
        dispatcher,
        "trajectory.changes",
        {"session_key": SESSION, "epoch": first["epoch"], "after_revision": first["snapshot_revision"]},
    )
    new_ids = {e["entry_id"] for e in batch["result"]["upserts"]}
    assert "t:turn3:turn.input" in new_ids and "t:tool3:tool.output" in new_ids
    stale = await _call(
        dispatcher, "trajectory.changes", {"session_key": SESSION, "epoch": "other", "after_revision": 0}
    )
    assert stale["result"]["reset_required"] is True


async def test_detail_and_block_round_trip(state):
    _seed(state, turns=1, v2_messages=45)
    tpol.arm(True)
    dispatcher = _dispatcher()
    entries, first = await _list_all(dispatcher)
    llm_in = next(e for e in entries if e["entry_id"] == "t:llm0:llm.input")
    detail = await _call(dispatcher, "trajectory.detail", {"session_key": SESSION, "entry_id": llm_in["entry_id"]})
    TrajectoryDetailResult.model_validate(detail["result"])
    assert detail["result"]["entry_revision"] == llm_in["revision"]
    block_ids = [b["id"] for b in detail["result"]["blocks"]]
    assert block_ids[0] == "messages" and "model" in block_ids and block_ids[-1] == "raw"
    stale = await _call(
        dispatcher,
        "trajectory.detail",
        {"session_key": SESSION, "entry_id": llm_in["entry_id"], "entry_revision": llm_in["revision"] + 7},
    )
    assert stale["result"]["revision_changed"] is True
    pages = []
    cursor = None
    while True:
        body = await _call(
            dispatcher,
            "trajectory.block",
            {
                "session_key": SESSION,
                "entry_id": llm_in["entry_id"],
                "entry_revision": llm_in["revision"],
                "epoch": first["epoch"],
                "block_id": "messages",
                **({"cursor": cursor} if cursor else {}),
            },
        )
        TrajectoryBlockResult.model_validate(body["result"])
        pages.append(body["result"])
        cursor = body["result"]["next_cursor"]
        if not cursor:
            break
    assert [len(p["data"]["items"]) for p in pages] == [20, 20, 5]
    assert pages[0]["total_items"] == 45


async def test_outline_block_lists_every_message_and_hands_out_page_cursors(state):
    _seed(state, turns=1, v2_messages=45)
    tpol.arm(True)
    dispatcher = _dispatcher()
    entries, first = await _list_all(dispatcher)
    llm_in = next(e for e in entries if e["entry_id"] == "t:llm0:llm.input")
    detail = await _call(dispatcher, "trajectory.detail", {"session_key": SESSION, "entry_id": llm_in["entry_id"]})
    outline = next(b for b in detail["result"]["blocks"] if b["id"] == "outline")
    assert outline["renderer"] == "items" and outline["preview"] is None and outline["total_items"] == 45
    base = {
        "session_key": SESSION,
        "entry_id": llm_in["entry_id"],
        "entry_revision": llm_in["revision"],
        "epoch": first["epoch"],
    }
    body = await _call(dispatcher, "trajectory.block", {**base, "block_id": "outline"})
    TrajectoryBlockResult.model_validate(body["result"])
    assert body["result"]["next_cursor"] is None and body["result"]["total_items"] == 45
    rows = body["result"]["data"]["items"]
    assert [row["index"] for row in rows] == list(range(45))
    assert {k: v for k, v in rows[7].items() if k not in ("bytes", "cursor")} == {
        "index": 7,
        "role": "user",
        "chars": len("message 7"),
        "preview": "message 7",
        "partial": False,
        "missing": False,
    }
    assert rows[7]["bytes"] > 0 and isinstance(rows[7]["cursor"], str)
    page = await _call(dispatcher, "trajectory.block", {**base, "block_id": "messages", "cursor": rows[20]["cursor"]})
    TrajectoryBlockResult.model_validate(page["result"])
    assert page["result"]["data"]["offset"] == 20
    assert [m["content"] for m in page["result"]["data"]["items"]] == [f"message {n}" for n in range(20, 40)]
    assert page["result"]["next_cursor"] is not None


async def test_error_mapping(state):
    _seed(state, turns=1)
    tpol.arm(True)
    dispatcher = _dispatcher()
    entries, first = await _list_all(dispatcher)
    tool_out = next(e for e in entries if e["entry_id"] == "t:tool0:tool.output")
    missing = await _call(dispatcher, "trajectory.detail", {"session_key": SESSION, "entry_id": "nope"})
    assert _error_code(missing) == -32021
    base = {"session_key": SESSION, "entry_id": tool_out["entry_id"], "epoch": first["epoch"]}
    unknown_block = await _call(
        dispatcher, "trajectory.block", {**base, "entry_revision": tool_out["revision"], "block_id": "schema_x"}
    )
    assert _error_code(unknown_block) == -32021 and unknown_block["error"]["data"]["block_id"] == "schema_x"
    revision = await _call(
        dispatcher, "trajectory.block", {**base, "entry_revision": tool_out["revision"] + 3, "block_id": "result"}
    )
    assert _error_code(revision) == -32023
    assert revision["error"]["data"]["current_revision"] == tool_out["revision"]
    assert revision["error"]["data"]["current_epoch"] == first["epoch"]
    cursor = await _call(
        dispatcher,
        "trajectory.block",
        {**base, "entry_revision": tool_out["revision"], "block_id": "result", "cursor": "garbage"},
    )
    assert _error_code(cursor) == -32022
    bad_list_cursor = await _call(dispatcher, "trajectory.list", {"session_key": SESSION, "cursor": "garbage"})
    assert _error_code(bad_list_cursor) == -32022
    empty = await _call(dispatcher, "trajectory.list", {"session_key": ""})
    assert _error_code(empty) == -32001


# ── parameter validation before any disk access ───────────────────────


@pytest.mark.parametrize(
    "method, params",
    [
        ("trajectory.list", {"session_key": SESSION, "limit": 0}),
        ("trajectory.list", {"session_key": SESSION, "limit": -1}),
        ("trajectory.list", {"session_key": SESSION, "limit": 501}),
        ("trajectory.list", {"session_key": SESSION, "limit": "20"}),
        ("trajectory.list", {"session_key": SESSION, "limit": True}),
        ("trajectory.list", {"session_key": SESSION, "cursor": 123}),
        ("trajectory.list", {"session_key": SESSION, "extra": 1}),
        ("trajectory.changes", {"session_key": SESSION, "epoch": "e", "after_revision": -1}),
        ("trajectory.changes", {"session_key": SESSION, "epoch": "", "after_revision": 0}),
        ("trajectory.changes", {"session_key": SESSION, "after_revision": 0}),
        ("trajectory.detail", {"session_key": SESSION}),
        ("trajectory.detail", {"session_key": SESSION, "entry_id": ""}),
        ("trajectory.detail", {"session_key": SESSION, "entry_id": "x", "entry_revision": -1}),
        ("trajectory.block", {"session_key": SESSION, "entry_id": "x", "entry_revision": 1, "block_id": "c"}),
        ("trajectory.block", {"session_key": SESSION, "entry_id": "x", "entry_revision": 1, "epoch": "e"}),
        (
            "trajectory.block",
            {"session_key": SESSION, "entry_id": "x", "entry_revision": -1, "epoch": "e", "block_id": "c"},
        ),
        (
            "trajectory.block",
            {"session_key": SESSION, "entry_id": "x", "entry_revision": 1, "epoch": "e", "block_id": "c", "cursor": 5},
        ),
    ],
)
async def test_invalid_params_are_rejected_before_any_read(monkeypatch, method, params):
    tpol.arm(True)

    def boom(*_a, **_k):
        raise AssertionError("reached the index or details layer with invalid params")

    monkeypatch.setattr(rpc_trajectory.index, "indexer_for", boom)
    monkeypatch.setattr(rpc_trajectory.details, "describe", boom)
    monkeypatch.setattr(rpc_trajectory.details, "read_block", boom)
    response = await _call(_dispatcher(), method, params)
    assert _error_code(response) == -32602, response


# ── the event loop stays free while a detail read runs ────────────────


async def test_detail_and_block_reads_do_not_block_the_loop(state, monkeypatch):
    _seed(state, turns=1)
    tpol.arm(True)
    dispatcher = _dispatcher()
    entries, first = await _list_all(dispatcher)
    tool_out = next(e for e in entries if e["entry_id"] == "t:tool0:tool.output")
    gate = threading.Event()
    original_describe = tdet.describe
    original_read_block = tdet.read_block

    def slow_describe(*args, **kwargs):
        gate.wait(5)
        return original_describe(*args, **kwargs)

    def slow_read_block(*args, **kwargs):
        gate.wait(5)
        return original_read_block(*args, **kwargs)

    monkeypatch.setattr(rpc_trajectory.details, "describe", slow_describe)
    monkeypatch.setattr(rpc_trajectory.details, "read_block", slow_read_block)
    order: list[str] = []

    async def light() -> None:
        await asyncio.sleep(0)
        order.append("light")

    detail_task = asyncio.create_task(
        _call(dispatcher, "trajectory.detail", {"session_key": SESSION, "entry_id": tool_out["entry_id"]})
    )
    block_task = asyncio.create_task(
        _call(
            dispatcher,
            "trajectory.block",
            {
                "session_key": SESSION,
                "entry_id": tool_out["entry_id"],
                "entry_revision": tool_out["revision"],
                "epoch": first["epoch"],
                "block_id": "result",
            },
        )
    )
    await light()
    assert order == ["light"] and not detail_task.done() and not block_task.done()
    gate.set()
    detail, block = await asyncio.gather(detail_task, block_task)
    assert detail["result"]["entry_id"] == tool_out["entry_id"]
    assert block["result"]["data"] == {"text": "ok"}


# ── the health endpoint reports the policy ────────────────────────────


async def test_health_reports_the_trajectory_view_flag():
    from raven.rpc.transports.ws import WsGateway

    gateway = WsGateway()
    request = make_mocked_request("GET", "/health")
    off = json.loads((await gateway.handle_health(request)).text)
    assert off["service"] == "raven-serve" and off["trajectory_view"] is False
    tpol.arm(True)
    on = json.loads((await gateway.handle_health(request)).text)
    assert on["trajectory_view"] is True and on["ok"] is True
