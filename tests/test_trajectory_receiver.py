"""Tests for the Bug Report Receiver (`raven.trajectory.receiver`)."""

from __future__ import annotations

import asyncio
import gc
import gzip
import hashlib
import json
import os
import stat
import threading
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from raven.trajectory import package_check as pcheck
from raven.trajectory import receiver as rcv
from raven.trajectory.receiver import ReceiverConfig, ReceiverStore, build_app
from tests.test_trajectory_package_check import OTHER_ID, ROOT, _entries, _metadata, _tar


def _package_bytes(**kw):
    return gzip.compress(_tar(_entries(**kw)), mtime=0)


PACKAGE = _package_bytes()
DIFFERENT = _package_bytes(metadata=_metadata(problem={"description": "another problem"}))
SHA = hashlib.sha256(PACKAGE).hexdigest()
DIFFERENT_SHA = hashlib.sha256(DIFFERENT).hexdigest()
DATE = "2026-10-12"


class Clock:
    def __init__(self):
        self.now = datetime(2026, 10, 12, 6, 40, 11, tzinfo=timezone.utc)

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def save_dir(tmp_path):
    return tmp_path / "inbox"


@pytest.fixture
def config(save_dir, clock):
    return ReceiverConfig(save_dir=save_dir, clock=clock)


async def _start(config) -> TestClient:
    # Runner options go to the server's start (TestServer forwards them to
    # AppRunner); TestClient.start_server() takes none.
    server = TestServer(build_app(config))
    await server.start_server(**rcv.RUNNER_OPTIONS)
    client = TestClient(server)
    await client.start_server()
    return client


@pytest.fixture
async def client(config) -> AsyncIterator[TestClient]:
    client = await _start(config)
    try:
        yield client
    finally:
        await client.close()


def _headers(report_id=ROOT, sha=SHA, content_type="application/gzip", **extra):
    headers = {"Content-Type": content_type}
    if report_id is not None:
        headers[rcv.REPORT_ID_HEADER] = report_id
    if sha is not None:
        headers[rcv.SHA256_HEADER] = sha
    headers.update(extra)
    return headers


async def _post(client, body=PACKAGE, **header_kw):
    return await client.post(rcv.ENDPOINT_PATH, data=body, headers=_headers(**header_kw))


def _landed(save_dir, report_id=ROOT):
    return sorted(save_dir.glob(f"????-??-??/{report_id}.tar.gz"))


def _sidecar(save_dir, report_id=ROOT, date=DATE):
    return json.loads((save_dir / date / f"{report_id}.json").read_text(encoding="utf-8"))


def _mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def _place(save_dir, body=PACKAGE, payload=None, *, date=DATE, report_id=ROOT, package=True):
    date_dir = save_dir / date
    date_dir.mkdir(parents=True, exist_ok=True)
    if package:
        (date_dir / f"{report_id}.tar.gz").write_bytes(body)
    if payload is not None:
        text = payload if isinstance(payload, str) else json.dumps(payload)
        (date_dir / f"{report_id}.json").write_text(text, encoding="utf-8")


def _valid_payload(save_dir, clock, body=PACKAGE):
    store = ReceiverStore(save_dir, clock=clock)
    return store.receipt_payload(
        report_id=ROOT,
        sha256=hashlib.sha256(body).hexdigest(),
        size_bytes=len(body),
        accounting_date=DATE,
        remote={"peer": "10.0.0.9", "client": "10.0.0.9", "user_agent": "earlier"},
        package=rcv._package_digest({}),
        notices=(),
        recovered=False,
    )


async def _eventually(condition, timeout=5.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


# --- the normal path --------------------------------------------------------


async def test_upload_lands_the_package_with_a_receipt(client, save_dir):
    resp = await _post(client)
    assert resp.status == 201
    receipt = await resp.json()
    assert set(receipt) == {"receipt_id", "report_id", "sha256", "size_bytes", "received_at", "duplicate"}
    assert rcv._RECEIPT_ID.fullmatch(receipt["receipt_id"])
    assert receipt["report_id"] == ROOT
    assert receipt["sha256"] == SHA
    assert receipt["size_bytes"] == len(PACKAGE)
    assert receipt["received_at"] == "2026-10-12T06:40:11Z"
    assert receipt["duplicate"] is False

    package = save_dir / DATE / f"{ROOT}.tar.gz"
    assert package.read_bytes() == PACKAGE
    sidecar = _sidecar(save_dir)
    assert sidecar["schema"] == "bug_report_receipt" and sidecar["schema_version"] == 1
    assert {k: sidecar[k] for k in receipt if k != "duplicate"} == {
        k: v for k, v in receipt.items() if k != "duplicate"
    }
    assert sidecar["accounting_date"] == DATE
    assert sidecar["remote"]["peer"] == "127.0.0.1" == sidecar["remote"]["client"]
    assert sidecar["remote"]["user_agent"]
    assert sidecar["package"]["description"] == "it broke"
    assert sidecar["package"]["classification"] == "clean"
    assert sidecar["package"]["risk_accepted"] is False
    assert sidecar["notices"] == [] and sidecar["recovered"] is False

    assert _mode(package) == 0o600 and _mode(save_dir / DATE / f"{ROOT}.json") == 0o600
    assert _mode(save_dir / DATE) == 0o700 and _mode(save_dir / ".incoming") == 0o700
    assert list((save_dir / ".incoming").iterdir()) == []


async def test_same_bytes_again_is_a_duplicate(client, save_dir):
    first = await (await _post(client)).json()
    resp = await _post(client)
    assert resp.status == 200
    again = await resp.json()
    assert again["duplicate"] is True
    assert again["receipt_id"] == first["receipt_id"]
    assert len(list((save_dir / DATE).iterdir())) == 2


async def test_different_bytes_for_the_same_id_conflict(client, save_dir):
    assert (await _post(client)).status == 201
    resp = await _post(client, DIFFERENT, sha=DIFFERENT_SHA)
    assert resp.status == 409
    assert (await resp.json())["error"] == "conflict"
    assert (save_dir / DATE / f"{ROOT}.tar.gz").read_bytes() == PACKAGE


# --- headers ----------------------------------------------------------------


@pytest.mark.parametrize(
    "header_kw",
    [{"report_id": None}, {"report_id": "nope"}, {"sha": None}, {"sha": "zz"}, {"sha": SHA.upper()}],
    ids=["no-id", "bad-id", "no-sha", "bad-sha", "uppercase-sha"],
)
async def test_missing_or_malformed_headers(client, save_dir, header_kw):
    resp = await _post(client, **header_kw)
    assert resp.status == 400
    assert (await resp.json())["error"] == "bad_request"
    assert not _landed(save_dir)


async def test_header_id_must_match_the_package(client):
    resp = await _post(client, report_id=OTHER_ID)
    assert resp.status == 400
    body = await resp.json()
    assert body["error"] == "bad_package" and body["message"].startswith("report_id_mismatch:")


async def test_wrong_content_type(client):
    resp = await _post(client, content_type="text/plain")
    assert resp.status == 415


async def test_content_encoding_is_refused(client, save_dir):
    resp = await client.post(rcv.ENDPOINT_PATH, data=PACKAGE, headers=_headers(**{"Content-Encoding": "gzip"}))
    assert resp.status == 415
    assert "Content-Encoding" in (await resp.json())["message"]
    assert not _landed(save_dir)


async def test_chunked_bodies_need_a_length(client):
    async def gen():
        yield PACKAGE[:10]
        yield PACKAGE[10:]

    resp = await client.post(rcv.ENDPOINT_PATH, data=gen(), headers=_headers())
    assert resp.status == 411
    assert (await resp.json())["error"] == "length_required"


# --- bad bodies -------------------------------------------------------------


async def test_digest_mismatch_leaves_nothing_behind(client, save_dir):
    resp = await _post(client, sha="0" * 64)
    assert resp.status == 400
    assert (await resp.json())["error"] == "digest_mismatch"
    assert list((save_dir / ".incoming").iterdir()) == []
    assert not (save_dir / DATE).exists()


async def test_bad_package_is_named(client, save_dir):
    body = b"not a gzip stream at all"
    resp = await _post(client, body, sha=hashlib.sha256(body).hexdigest())
    assert resp.status == 400
    payload = await resp.json()
    assert payload["error"] == "bad_package" and payload["message"].startswith("not_gzip_tar:")
    assert list((save_dir / ".incoming").iterdir()) == []


# --- routes -----------------------------------------------------------------


async def test_health_and_json_errors(client):
    resp = await client.get(rcv.HEALTH_PATH)
    assert resp.status == 200 and await resp.json() == {"status": "ok"}
    resp = await client.get("/nope")
    assert resp.status == 404 and (await resp.json())["error"] == "not_found"
    resp = await client.get(rcv.ENDPOINT_PATH)
    assert resp.status == 405 and (await resp.json())["error"] == "method_not_allowed"


# --- startup and repair -----------------------------------------------------


async def test_startup_creates_private_dirs_and_clears_incoming(config, save_dir):
    (save_dir / ".incoming").mkdir(parents=True)
    (save_dir / ".incoming" / "br-20260904-abcdef.deadbeef.part").write_bytes(b"x")
    (save_dir / ".incoming" / "junk").write_bytes(b"y")
    client = await _start(config)
    try:
        assert list((save_dir / ".incoming").iterdir()) == []
        assert _mode(save_dir / ".incoming") == 0o700
    finally:
        await client.close()


async def test_fresh_save_dir_is_private(tmp_path, clock):
    save_dir = tmp_path / "deeper" / "inbox"
    client = await _start(ReceiverConfig(save_dir=save_dir, clock=clock))
    try:
        assert _mode(save_dir) == 0o700
    finally:
        await client.close()


async def test_committed_package_with_sidecar_is_recognised(config, save_dir, clock):
    payload = _valid_payload(save_dir, clock)
    _place(save_dir, payload=payload)
    client = await _start(config)
    try:
        resp = await _post(client)
        assert resp.status == 200
        assert (await resp.json())["receipt_id"] == payload["receipt_id"]
    finally:
        await client.close()


async def test_package_without_sidecar_is_repaired_at_startup(config, save_dir):
    _place(save_dir)
    client = await _start(config)
    try:
        sidecar = _sidecar(save_dir)
        assert sidecar["recovered"] is True
        assert sidecar["sha256"] == SHA and sidecar["size_bytes"] == len(PACKAGE)
        assert sidecar["package"]["description"] == "it broke"
        resp = await _post(client)
        assert resp.status == 200 and (await resp.json())["receipt_id"] == sidecar["receipt_id"]
        assert (await _post(client, DIFFERENT, sha=DIFFERENT_SHA)).status == 409
        assert (save_dir / DATE / f"{ROOT}.tar.gz").read_bytes() == PACKAGE
    finally:
        await client.close()


async def test_sidecar_without_package_is_removed_at_startup(config, save_dir, clock):
    _place(save_dir, payload=_valid_payload(save_dir, clock), package=False)
    client = await _start(config)
    try:
        assert not (save_dir / DATE / f"{ROOT}.json").exists()
        assert (await _post(client)).status == 201
    finally:
        await client.close()


async def test_package_without_sidecar_is_repaired_on_lookup(client, save_dir):
    _place(save_dir)
    resp = await _post(client)
    assert resp.status == 200
    assert _sidecar(save_dir)["recovered"] is True
    assert (await resp.json())["receipt_id"] == _sidecar(save_dir)["receipt_id"]


def _corrupt(payload, spec):
    if spec == "empty":
        return {}
    if spec == "missing-receipt-id":
        del payload["receipt_id"]
    elif spec == "wrong-schema":
        payload["schema"] = "other"
    elif spec == "string-version":
        payload["schema_version"] = "1"
    elif spec == "other-id":
        payload["report_id"] = OTHER_ID
    elif spec == "wrong-size":
        payload["size_bytes"] += 1
    elif spec == "bad-sha":
        payload["sha256"] = "zz"
    elif spec == "notices-string":
        payload["notices"] = "none"
    elif spec == "wrong-date":
        payload["accounting_date"] = "2026-01-01"
    elif spec == "not-json":
        return "{not json"
    return payload


_CORRUPTIONS = [
    "empty",
    "missing-receipt-id",
    "wrong-schema",
    "string-version",
    "other-id",
    "wrong-size",
    "bad-sha",
    "notices-string",
    "wrong-date",
    "not-json",
]


def _assert_rebuilt(save_dir):
    sidecar = _sidecar(save_dir)
    assert sidecar["recovered"] is True
    assert sidecar["sha256"] == SHA and sidecar["size_bytes"] == len(PACKAGE)
    assert rcv._RECEIPT_ID.fullmatch(sidecar["receipt_id"])
    return sidecar


async def _assert_repaired(client, save_dir):
    """The same package is a duplicate of the rebuilt receipt; a different one conflicts."""
    resp = await _post(client)
    assert resp.status == 200
    sidecar = _assert_rebuilt(save_dir)
    assert (await resp.json())["receipt_id"] == sidecar["receipt_id"]
    assert (await _post(client, DIFFERENT, sha=DIFFERENT_SHA)).status == 409
    assert (save_dir / DATE / f"{ROOT}.tar.gz").read_bytes() == PACKAGE


@pytest.mark.parametrize("spec", _CORRUPTIONS)
async def test_broken_sidecar_is_rebuilt_at_startup(config, save_dir, clock, spec):
    _place(save_dir, payload=_corrupt(_valid_payload(save_dir, clock), spec))
    client = await _start(config)
    try:
        _assert_rebuilt(save_dir)
        await _assert_repaired(client, save_dir)
    finally:
        await client.close()


@pytest.mark.parametrize("spec", _CORRUPTIONS)
async def test_broken_sidecar_is_rebuilt_on_lookup(client, save_dir, clock, spec):
    _place(save_dir, payload=_corrupt(_valid_payload(save_dir, clock), spec))
    await _assert_repaired(client, save_dir)


# --- fault injection --------------------------------------------------------


async def test_sidecar_write_failure_lands_nothing(client, save_dir, monkeypatch):
    def _fail(self, sidecar, payload):
        raise OSError("disk says no")

    monkeypatch.setattr(ReceiverStore, "_write_sidecar", _fail)
    resp = await _post(client)
    assert resp.status == 500 and (await resp.json())["error"] == "internal"
    assert not (save_dir / DATE).exists() or list((save_dir / DATE).iterdir()) == []
    assert list((save_dir / ".incoming").iterdir()) == []


async def test_package_rename_failure_withdraws_the_sidecar(client, save_dir, monkeypatch):
    real_replace = os.replace

    def _replace(src, dst):
        if str(dst).endswith(".tar.gz"):
            raise OSError("rename refused")
        return real_replace(src, dst)

    monkeypatch.setattr(rcv.os, "replace", _replace)
    resp = await _post(client)
    assert resp.status == 500
    assert list((save_dir / DATE).iterdir()) == []
    assert list((save_dir / ".incoming").iterdir()) == []


async def test_inspection_crash_is_internal(client, save_dir, monkeypatch):
    def _crash(*args, **kwargs):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(rcv, "inspect_package", _crash)
    resp = await _post(client)
    assert resp.status == 500
    assert list((save_dir / ".incoming").iterdir()) == []


# --- cancellation while inspecting -----------------------------------------


class _GatedInspection:
    """Stands in for inspect_package: blocks until released or asked to stop."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.exited = threading.Event()
        self.part_existed_at_exit = None

    def __call__(self, path, *, limits, expected_report_id, should_stop):
        self.started.set()
        try:
            while not should_stop():
                if self.release.is_set():
                    return pcheck.inspect_package(path, limits=limits, expected_report_id=expected_report_id)
                threading.Event().wait(0.01)
            raise pcheck.InspectionStoppedError("stopped by the test")
        finally:
            self.part_existed_at_exit = path.exists()
            self.exited.set()


def _request_head(length, report_id=ROOT, sha=SHA, extra=""):
    return (
        f"POST {rcv.ENDPOINT_PATH} HTTP/1.1\r\nHost: t\r\nContent-Type: application/gzip\r\n"
        f"Content-Length: {length}\r\n{rcv.REPORT_ID_HEADER}: {report_id}\r\n{rcv.SHA256_HEADER}: {sha}\r\n"
        f"Connection: close\r\n{extra}\r\n"
    )


async def _raw(client, head, *parts, pause=0.0, close=False):
    reader, writer = await asyncio.open_connection(client.server.host, client.server.port)
    writer.write(head.encode())
    for index, part in enumerate(parts):
        if index and pause:
            await asyncio.sleep(pause)
        writer.write(part)
        await writer.drain()
    if close:
        writer.close()
        return None
    try:
        data = await asyncio.wait_for(reader.read(1 << 16), 5)
    finally:
        writer.close()
    return data


def _status_of(data):
    return int(data.split(b" ", 2)[1])


def _json_of(data):
    return json.loads(data.split(b"\r\n\r\n", 1)[1])


async def test_cancelled_request_waits_for_the_inspection_thread(client, save_dir, monkeypatch):
    gate = _GatedInspection()
    monkeypatch.setattr(rcv, "inspect_package", gate)
    errors = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda _loop, context: errors.append(context))
    try:
        _reader, writer = await asyncio.open_connection(client.server.host, client.server.port)
        writer.write(_request_head(len(PACKAGE)).encode() + PACKAGE)
        await writer.drain()
        await asyncio.to_thread(gate.started.wait, 5)
        writer.close()
        await asyncio.to_thread(gate.exited.wait, 5)
        assert gate.exited.is_set()
        assert gate.part_existed_at_exit is True
        await _eventually(lambda: list((save_dir / ".incoming").iterdir()) == [])
        await _eventually(lambda: len(client.server.app[rcv.RECEIVER_KEY].locks) == 0)
        assert not _landed(save_dir)
        gc.collect()
        await asyncio.sleep(0.05)
    finally:
        loop.set_exception_handler(None)
    assert errors == []


async def test_released_inspection_completes_normally(client, save_dir, monkeypatch):
    gate = _GatedInspection()
    gate.release.set()
    monkeypatch.setattr(rcv, "inspect_package", gate)
    resp = await _post(client)
    assert resp.status == 201
    assert gate.part_existed_at_exit is True
    assert _landed(save_dir)


# --- locks ------------------------------------------------------------------


async def test_id_locks_serialise_late_arrivals():
    locks = rcv._IdLocks()
    order = []
    inside = 0
    peak = 0
    gate_a = asyncio.Event()
    gate_b = asyncio.Event()

    async def hold(name, gate):
        nonlocal inside, peak
        async with locks.acquire(ROOT):
            inside += 1
            peak = max(peak, inside)
            order.append(name)
            await gate.wait()
            inside -= 1

    task_a = asyncio.create_task(hold("A", gate_a))
    await asyncio.sleep(0)
    task_b = asyncio.create_task(hold("B", gate_b))
    await asyncio.sleep(0)
    assert order == ["A"] and len(locks) == 1
    gate_a.set()
    await task_a
    assert len(locks) == 1
    gate_c = asyncio.Event()
    task_c = asyncio.create_task(hold("C", gate_c))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert order == ["A", "B"]
    gate_b.set()
    await task_b
    await asyncio.sleep(0)
    assert order == ["A", "B", "C"]
    gate_c.set()
    await task_c
    assert peak == 1 and len(locks) == 0


async def test_concurrent_identical_uploads_land_once(client, save_dir):
    responses = await asyncio.gather(*(_post(client) for _ in range(5)))
    statuses = sorted(r.status for r in responses)
    assert statuses == [200, 200, 200, 200, 201]
    receipts = {(await r.json())["receipt_id"] for r in responses}
    assert len(receipts) == 1
    assert sorted(p.name for p in (save_dir / DATE).iterdir()) == [f"{ROOT}.json", f"{ROOT}.tar.gz"]


# --- Content-Length boundaries over a raw socket ----------------------------


async def test_over_delivery_in_two_writes_is_a_digest_mismatch(client, save_dir):
    half = len(PACKAGE) // 2
    data = await _raw(client, _request_head(half), PACKAGE[:half], b"EXTRA", pause=0.2)
    assert _status_of(data) == 400
    assert _json_of(data)["error"] == "digest_mismatch"
    assert not _landed(save_dir)
    assert list((save_dir / ".incoming").iterdir()) == []


async def test_over_delivery_in_one_write_stores_nothing(client, save_dir):
    half = len(PACKAGE) // 2
    data = await _raw(client, _request_head(half), PACKAGE[:half] + b"EXTRA")
    assert _status_of(data) == 400
    assert not _landed(save_dir)
    await _eventually(lambda: list((save_dir / ".incoming").iterdir()) == [])


async def test_under_delivery_then_close_leaves_nothing(client, save_dir):
    await _raw(client, _request_head(len(PACKAGE)), PACKAGE[:100], close=True)
    await _eventually(lambda: list((save_dir / ".incoming").iterdir()) == [])
    assert not _landed(save_dir)


async def test_oversized_length_is_refused_before_the_body(client, config):
    data = await _raw(client, _request_head(config.limits.max_package_bytes + 1))
    assert _status_of(data) == 413
    assert _json_of(data)["error"] == "too_large"


# --- first commits of a day race on the date directory ----------------------

OTHER_PACKAGE = _package_bytes(root=OTHER_ID)
OTHER_SHA = hashlib.sha256(OTHER_PACKAGE).hexdigest()


def _barrier_before_date_mkdir(monkeypatch, parties=2):
    """Hold every thread that is about to create the date directory until all have arrived."""
    barrier = threading.Barrier(parties)
    real_mkdir = Path.mkdir

    def _mkdir(self, *args, **kwargs):
        if self.name == DATE:
            barrier.wait(timeout=5)
        return real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _mkdir)
    return barrier


def test_two_ids_can_create_the_date_dir_at_once(save_dir, clock, monkeypatch):
    store = ReceiverStore(save_dir, clock=clock)
    store.prepare()
    _barrier_before_date_mkdir(monkeypatch)
    outcomes = {}

    def _commit(report_id, body):
        part = store.new_part(report_id)
        part.write_bytes(body)
        payload = store.receipt_payload(
            report_id=report_id,
            sha256=hashlib.sha256(body).hexdigest(),
            size_bytes=len(body),
            accounting_date=DATE,
            remote={"peer": "", "client": "", "user_agent": ""},
            package=rcv._package_digest({}),
            notices=(),
            recovered=False,
        )
        try:
            outcomes[report_id] = store.commit(report_id, part, payload)
        except BaseException as exc:
            outcomes[report_id] = exc

    threads = [
        threading.Thread(target=_commit, args=(ROOT, PACKAGE)),
        threading.Thread(target=_commit, args=(OTHER_ID, OTHER_PACKAGE)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(isinstance(result, Path) for result in outcomes.values()), outcomes
    assert sorted(p.name for p in (save_dir / DATE).iterdir()) == sorted(
        [f"{ROOT}.json", f"{ROOT}.tar.gz", f"{OTHER_ID}.json", f"{OTHER_ID}.tar.gz"]
    )
    assert list((save_dir / ".incoming").iterdir()) == []
    assert _mode(save_dir / DATE) == 0o700


async def test_two_ids_uploaded_at_once_on_a_fresh_day(client, save_dir, monkeypatch):
    _barrier_before_date_mkdir(monkeypatch)
    first, second = await asyncio.gather(_post(client), _post(client, OTHER_PACKAGE, report_id=OTHER_ID, sha=OTHER_SHA))
    assert (first.status, second.status) == (201, 201)
    assert sorted(p.name for p in (save_dir / DATE).iterdir()) == sorted(
        [f"{ROOT}.json", f"{ROOT}.tar.gz", f"{OTHER_ID}.json", f"{OTHER_ID}.tar.gz"]
    )
    assert list((save_dir / ".incoming").iterdir()) == []
