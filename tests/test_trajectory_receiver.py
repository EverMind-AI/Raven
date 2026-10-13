"""Tests for the Bug Report Receiver (`raven.trajectory.receiver`)."""

from __future__ import annotations

import asyncio
import gc
import gzip
import hashlib
import json
import os
import stat
import tarfile
import threading
from collections.abc import AsyncIterator
from datetime import datetime, timezone
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from raven.trajectory import package_check as pcheck
from raven.trajectory import receiver as rcv
from raven.trajectory.receiver import ReceiverConfig, ReceiverStore, build_app
from tests.test_trajectory_package_check import OTHER_ID, ROOT, _chain, _entries, _metadata, _tar


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
    elif spec == "nested-beyond-recursion":
        return "[" * 10_000 + "]" * 10_000
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
    "nested-beyond-recursion",
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


async def test_metadata_nested_beyond_recursion_is_a_bad_package(client, save_dir):
    body = _package_bytes(metadata=b"[" * 10_000 + b"]" * 10_000)
    resp = await _post(client, body, sha=hashlib.sha256(body).hexdigest())
    assert resp.status == 400
    payload = await resp.json()
    assert payload["error"] == "bad_package" and payload["message"].startswith("metadata_invalid:")
    assert not list(save_dir.glob("*/*"))


@pytest.mark.parametrize("kind", [tarfile.XHDTYPE, tarfile.GNUTYPE_LONGNAME])
async def test_a_chain_of_extended_headers_is_a_bad_package(client, save_dir, kind):
    body = gzip.compress(_chain(kind, 2000) + _tar(_entries()), mtime=0)
    resp = await _post(client, body, sha=hashlib.sha256(body).hexdigest())
    assert resp.status == 400
    payload = await resp.json()
    assert payload["error"] == "bad_package" and payload["message"].startswith("header_chain_too_long:")
    assert not list(save_dir.glob("*/*"))


async def test_the_sidecar_digest_and_notices_are_bounded(client, save_dir):
    description = "x" * 5000 + " /a" * 100_000
    body = _package_bytes(metadata=_metadata(problem={"description": description}))
    resp = await _post(client, body, sha=hashlib.sha256(body).hexdigest())
    assert resp.status == 201
    sidecar = _sidecar(save_dir)
    assert sidecar["package"]["description"] == "x" * 1000
    assert len(sidecar["notices"]) == pcheck.MAX_NOTICES + 1
    assert sidecar["notices"][-1].startswith(f"more than {pcheck.MAX_NOTICES} absolute paths")
    assert (save_dir / DATE / f"{ROOT}.json").stat().st_size < 8192


async def test_metadata_nested_as_deep_as_the_decoder_allows_is_received(client, save_dir):
    depth = 900
    nested = json.loads("[" * depth + '"/etc/shadow"' + "]" * depth)
    body = _package_bytes(metadata=_metadata(problem={"description": "x", "extra": nested}))
    resp = await _post(client, body, sha=hashlib.sha256(body).hexdigest())
    assert resp.status == 201
    (notice,) = _sidecar(save_dir)["notices"]
    assert notice.endswith(": /etc/shadow") and len(notice) < 300


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
        self.fail = False

    def __call__(self, path, *, limits, expected_report_id, should_stop):
        self.started.set()
        try:
            while not should_stop():
                if self.release.is_set():
                    if self.fail:
                        raise pcheck.PackageError(pcheck.CODE_NOT_GZIP_TAR, "forced by the test")
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


async def test_concurrent_identical_uploads_land_once(make_client, save_dir):
    client = await make_client(concurrency=8)
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


# === step 3: admission, budgets, proxies, timeouts, serving ==================


@pytest.fixture
async def make_client(save_dir, clock):
    clients = []

    async def _make(**overrides):
        client = await _start(ReceiverConfig(save_dir=save_dir, clock=clock, **overrides))
        clients.append(client)
        return client

    yield _make
    for client in clients:
        await client.close()


def _receiver(client):
    return client.server.app[rcv.RECEIVER_KEY]


async def _post_other(client):
    return await _post(client, OTHER_PACKAGE, report_id=OTHER_ID, sha=OTHER_SHA)


# --- token buckets ------------------------------------------------------------


def test_buckets_refill_and_forget_the_least_recent_client():
    now = [0.0]
    buckets = rcv._Buckets(burst=2, per_10min=600, max_entries=2, monotonic=lambda: now[0])
    assert buckets.take("a") is None and buckets.take("a") is None
    assert buckets.take("a") == pytest.approx(1.0)
    now[0] += 1.0
    assert buckets.take("a") is None
    buckets.take("b")
    buckets.take("c")
    assert buckets.keys() == ["b", "c"]
    assert rcv._Buckets(burst=0, per_10min=600).take("x") is None
    assert rcv._Buckets(burst=5, per_10min=0).take("x") is None


async def test_rate_limited_client_gets_429_until_tokens_return(make_client, save_dir, monkeypatch):
    client = await make_client(rate_burst=2, rate_per_10min=600)
    assert (await _post(client)).status == 201
    assert (await _post(client)).status == 200
    resp = await _post_other(client)
    assert resp.status == 429
    assert (await resp.json())["error"] == "rate_limited"
    assert int(resp.headers["Retry-After"]) >= 1
    assert not _landed(save_dir, OTHER_ID)
    base = rcv._monotonic()
    monkeypatch.setattr(rcv, "_monotonic", lambda: base + 5.0)
    assert (await _post_other(client)).status == 201


# --- concurrency slots --------------------------------------------------------


async def test_busy_receiver_refuses_without_queueing(make_client, save_dir, monkeypatch):
    client = await make_client(concurrency=1)
    gate = _GatedInspection()
    monkeypatch.setattr(rcv, "inspect_package", gate)
    first = asyncio.create_task(_post(client))
    await asyncio.to_thread(gate.started.wait, 5)
    resp = await _post_other(client)
    assert resp.status == 503
    assert (await resp.json())["error"] == "busy" and resp.headers["Retry-After"] == "5"
    assert len(list((save_dir / ".incoming").iterdir())) == 1
    gate.release.set()
    assert (await first).status == 201
    assert (await _post_other(client)).status == 201


# --- the daily ledger ---------------------------------------------------------


def test_ledger_backfills_budgets_and_settles_once(save_dir, clock):
    _place(save_dir)
    _place(save_dir, OTHER_PACKAGE, report_id=OTHER_ID)
    (save_dir / DATE / "notes.tar.gz").write_bytes(b"x" * 10)
    stored = len(PACKAGE) + len(OTHER_PACKAGE)
    ledger = rcv.DailyLedger(save_dir, max_bytes=stored + 100, max_count=3, clock=clock)
    day = ledger.snapshot(DATE)
    assert (day.landed_count, day.landed_bytes, day.reserved_count) == (2, stored, 0)
    with pytest.raises(rcv.ReceiverError) as exc:
        ledger.reserve(DATE, 101)
    assert exc.value.status == 507
    reservation = ledger.reserve(DATE, 50)
    assert ledger.in_flight_bytes() == 50
    with pytest.raises(rcv.ReceiverError) as exc:
        ledger.reserve(DATE, 1)
    assert exc.value.status == 429 and exc.value.retry_after == 17 * 3600 + 19 * 60 + 49
    reservation.settle(40)
    reservation.settle(40)
    reservation.release()
    day = ledger.snapshot(DATE)
    assert (day.landed_count, day.landed_bytes, day.reserved_count, day.reserved_bytes) == (3, stored + 40, 0, 0)
    released = ledger.reserve("2026-10-11", 10)
    assert DATE not in ledger._days
    released.release()
    assert ledger.snapshot("2026-10-11").reserved_bytes == 0


async def test_daily_count_and_byte_budgets_over_http(make_client, save_dir):
    client = await make_client(max_daily_count=1)
    assert (await _post(client)).status == 201
    resp = await _post_other(client)
    assert resp.status == 429 and (await resp.json())["error"] == "rate_limited" and "Retry-After" in resp.headers
    await client.close()
    for path in (save_dir / DATE).iterdir():
        path.unlink()
    client = await make_client(max_daily_bytes=len(PACKAGE) + 10)
    assert (await _post(client)).status == 201
    resp = await _post_other(client)
    assert resp.status == 507 and (await resp.json())["error"] == "insufficient_storage"


def test_ledger_counts_sidecars_as_landed_bytes(save_dir, clock):
    _place(save_dir, payload=_valid_payload(save_dir, clock))
    _place(save_dir, OTHER_PACKAGE, report_id=OTHER_ID)
    sidecar = (save_dir / DATE / f"{ROOT}.json").stat().st_size
    assert sidecar > 0
    ledger = rcv.DailyLedger(save_dir, max_bytes=10**9, max_count=10, clock=clock)
    day = ledger.snapshot(DATE)
    assert (day.landed_count, day.landed_bytes) == (2, len(PACKAGE) + sidecar + len(OTHER_PACKAGE))


async def test_the_daily_byte_budget_covers_the_sidecar_too(make_client, save_dir):
    """Two packages fit the budget on their own; the first one's sidecar is what tips it."""
    client = await make_client(max_daily_bytes=len(PACKAGE) + len(OTHER_PACKAGE) + 10)
    assert (await _post(client)).status == 201
    sidecar = (save_dir / DATE / f"{ROOT}.json").stat().st_size
    assert sidecar > 10
    day = _receiver(client).ledger.snapshot(DATE)
    assert (day.landed_count, day.landed_bytes) == (1, len(PACKAGE) + sidecar)
    resp = await _post_other(client)
    assert resp.status == 507 and (await resp.json())["error"] == "insufficient_storage"


async def test_a_failed_upload_gives_its_budget_back(make_client, monkeypatch):
    client = await make_client(max_daily_bytes=len(PACKAGE) + len(OTHER_PACKAGE) - 1)
    gate = _GatedInspection()
    monkeypatch.setattr(rcv, "inspect_package", gate)
    first = asyncio.create_task(_post(client))
    await asyncio.to_thread(gate.started.wait, 5)
    assert (await _post_other(client)).status == 507
    gate.fail = True
    gate.release.set()
    resp = await first
    assert resp.status == 400 and (await resp.json())["message"].startswith("not_gzip_tar:")
    assert _receiver(client).ledger.snapshot(DATE).reserved_count == 0
    gate.fail = False
    assert (await _post_other(client)).status == 201


async def test_every_exit_settles_the_reservation(make_client, save_dir, monkeypatch):
    client = await make_client(idle_timeout=0.2)
    ledger = _receiver(client).ledger
    assert (await _post(client)).status == 201
    assert (await _post(client)).status == 200
    assert (await _post(client, DIFFERENT, sha=DIFFERENT_SHA)).status == 409
    assert (await _post(client, b"junk", sha=hashlib.sha256(b"junk").hexdigest())).status == 400
    assert (await _post(client, sha="0" * 64)).status == 400
    data = await _raw(client, _request_head(len(OTHER_PACKAGE), OTHER_ID, OTHER_SHA), OTHER_PACKAGE[:50])
    assert _status_of(data) == 408
    await _raw(client, _request_head(len(OTHER_PACKAGE), OTHER_ID, OTHER_SHA), OTHER_PACKAGE[:50], close=True)
    await _eventually(lambda: list((save_dir / ".incoming").iterdir()) == [])
    monkeypatch.setattr(rcv, "inspect_package", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert (await _post_other(client)).status == 500
    await _eventually(lambda: ledger.snapshot(DATE).reserved_count == 0)
    day = ledger.snapshot(DATE)
    landed = len(PACKAGE) + (save_dir / DATE / f"{ROOT}.json").stat().st_size
    assert (day.landed_count, day.landed_bytes, day.reserved_bytes) == (1, landed, 0)


async def test_an_upload_is_charged_to_the_day_it_was_admitted(make_client, save_dir, clock, monkeypatch):
    clock.now = datetime(2026, 10, 12, 23, 59, 59, tzinfo=timezone.utc)
    client = await make_client()
    gate = _GatedInspection()
    monkeypatch.setattr(rcv, "inspect_package", gate)
    task = asyncio.create_task(_post(client))
    await asyncio.to_thread(gate.started.wait, 5)
    clock.now = datetime(2026, 10, 13, 0, 0, 1, tzinfo=timezone.utc)
    gate.release.set()
    assert (await task).status == 201
    assert (save_dir / DATE / f"{ROOT}.tar.gz").exists()
    sidecar = _sidecar(save_dir)
    assert sidecar["accounting_date"] == DATE and sidecar["received_at"] == "2026-10-13T00:00:01Z"
    ledger = _receiver(client).ledger
    assert ledger.snapshot(DATE).landed_count == 1
    assert ledger.snapshot("2026-10-13") == rcv._Day()


# --- disk reserve -------------------------------------------------------------


def _usage(free):
    return type("Usage", (), {"total": 10**12, "used": 0, "free": free})()


async def test_disk_reserve_counts_in_flight_uploads(make_client, monkeypatch):
    client = await make_client(min_free_bytes=1000)
    monkeypatch.setattr(rcv.shutil, "disk_usage", lambda _path: _usage(1000 + len(PACKAGE) - 1))
    resp = await _post(client)
    assert resp.status == 507 and (await resp.json())["error"] == "insufficient_storage"
    monkeypatch.setattr(rcv.shutil, "disk_usage", lambda _path: _usage(1000 + len(PACKAGE)))
    assert (await _post(client)).status == 201
    gate = _GatedInspection()
    monkeypatch.setattr(rcv, "inspect_package", gate)
    monkeypatch.setattr(rcv.shutil, "disk_usage", lambda _path: _usage(1000 + len(OTHER_PACKAGE) + len(DIFFERENT) - 1))
    first = asyncio.create_task(_post_other(client))
    await asyncio.to_thread(gate.started.wait, 5)
    assert (await _post(client, DIFFERENT, sha=DIFFERENT_SHA)).status == 507
    gate.release.set()
    assert (await first).status == 201


# --- trusted proxies ----------------------------------------------------------

TRUSTED = rcv._parse_networks(("10.0.0.0/8", "fd00::/8"))


@pytest.mark.parametrize(
    "peer,forwarded,expected",
    [
        ("10.0.0.2", "1.2.3.4, 10.0.0.2", "1.2.3.4"),
        ("10.0.0.2", None, "10.0.0.2"),
        ("8.8.8.8", "1.2.3.4", "8.8.8.8"),
        ("10.0.0.2", "10.0.0.3, 10.0.0.2", "10.0.0.2"),
        ("10.0.0.2", "9.9.9.9, 1.2.3.4, 10.0.0.2", "1.2.3.4"),
        ("10.0.0.2", "junk, 1.2.3.4", "1.2.3.4"),
        ("10.0.0.2", "1.2.3.4, junk", "10.0.0.2"),
        ("10.0.0.2", "1.2.3.4, , 10.0.0.2", "10.0.0.2"),
        ("10.0.0.2", "junk, 10.0.0.2", "10.0.0.2"),
        ("fd00::1", "2001:db8::7, fd00::2", "2001:db8::7"),
        ("fd00::1", "2001:db8::7", "2001:db8::7"),
    ],
)
def test_client_ip_walks_the_proxy_chain_from_the_right(peer, forwarded, expected):
    assert rcv._client_ip(peer, forwarded, TRUSTED) == expected


def test_invalid_trusted_proxy_fails_at_construction(save_dir, clock):
    with pytest.raises(ValueError):
        rcv.Receiver(ReceiverConfig(save_dir=save_dir, clock=clock, trusted_proxies=("not-a-network",)))


async def test_forwarded_client_is_recorded_and_rate_limited(make_client, save_dir):
    client = await make_client(trusted_proxies=("127.0.0.0/8",), rate_burst=1, rate_per_10min=1)
    resp = await _post(client, **{"X-Forwarded-For": "1.2.3.4"})
    assert resp.status == 201
    sidecar = _sidecar(save_dir)
    assert sidecar["remote"] == {
        "peer": "127.0.0.1",
        "client": "1.2.3.4",
        "user_agent": sidecar["remote"]["user_agent"],
    }
    assert (
        await _post(client, OTHER_PACKAGE, report_id=OTHER_ID, sha=OTHER_SHA, **{"X-Forwarded-For": "5.6.7.8"})
    ).status == 201
    assert (await _post(client, **{"X-Forwarded-For": "5.6.7.8"})).status == 429
    assert (await _post(client, **{"X-Forwarded-For": "1.2.3.4, junk"})).status == 200
    assert sorted(_receiver(client).buckets.keys()) == ["1.2.3.4", "127.0.0.1", "5.6.7.8"]


# --- timeouts -----------------------------------------------------------------


async def test_idle_body_times_out(make_client, save_dir):
    client = await make_client(idle_timeout=0.2)
    data = await _raw(client, _request_head(len(PACKAGE)), PACKAGE[: len(PACKAGE) // 2])
    assert _status_of(data) == 408
    assert "idle" in _json_of(data)["message"]
    await _eventually(lambda: list((save_dir / ".incoming").iterdir()) == [])


async def test_total_timeout_stops_the_inspection_thread(make_client, save_dir, monkeypatch):
    client = await make_client(total_timeout=0.3)
    gate = _GatedInspection()
    monkeypatch.setattr(rcv, "inspect_package", gate)
    resp = await _post(client)
    assert resp.status == 408
    assert "total" in (await resp.json())["message"]
    assert gate.exited.is_set() and gate.part_existed_at_exit is True
    assert list((save_dir / ".incoming").iterdir()) == []
    monkeypatch.setattr(rcv, "inspect_package", pcheck.inspect_package)
    assert (await _post_other(client)).status == 201


# --- the request log ----------------------------------------------------------


async def test_each_request_logs_one_line_without_the_body(client):
    from loguru import logger

    lines = []
    sink = logger.add(lines.append, format="{message}")
    try:
        assert (await _post(client)).status == 201
    finally:
        logger.remove(sink)
    matching = [line for line in lines if line.startswith("POST ") and f"report={ROOT}" in line]
    assert len(matching) == 1
    line = matching[0].rstrip("\n")
    assert " 201 " in line and "client=127.0.0.1" in line and line.endswith(" ok")
    assert all(PACKAGE[:8].hex() not in line and "\x1f\x8b" not in line for line in lines)


# --- settlement follows the commit, not the response -------------------------


class _GatedCommit:
    """Wraps ReceiverStore.commit with a gate before or after the real commit."""

    def __init__(self, where):
        self.where = where
        self.entered = threading.Event()
        self.release = threading.Event()
        self.real = ReceiverStore.commit

    def __call__(self, store, report_id, part, payload):
        if self.where == "before":
            self.entered.set()
            self.release.wait(10)
            return self.real(store, report_id, part, payload)
        result = self.real(store, report_id, part, payload)
        self.entered.set()
        self.release.wait(10)
        return result

    def patch(self):
        """A plain function, so it binds as a method when set on the class."""

        def _commit(store, report_id, part, payload):
            return self(store, report_id, part, payload)

        return _commit


@pytest.mark.parametrize("where", ["before", "after"])
async def test_a_package_landed_during_a_timeout_is_still_charged(make_client, save_dir, monkeypatch, where):
    client = await make_client(total_timeout=0.3, max_daily_count=1)
    gate = _GatedCommit(where)
    monkeypatch.setattr(ReceiverStore, "commit", gate.patch())
    task = asyncio.create_task(_post(client))
    await asyncio.to_thread(gate.entered.wait, 5)
    await asyncio.sleep(0.5)
    gate.release.set()
    assert (await task).status == 408
    assert (save_dir / DATE / f"{ROOT}.tar.gz").read_bytes() == PACKAGE
    day = _receiver(client).ledger.snapshot(DATE)
    assert (day.landed_count, day.reserved_count, day.reserved_bytes) == (1, 0, 0)
    assert list((save_dir / ".incoming").iterdir()) == []
    monkeypatch.setattr(ReceiverStore, "commit", gate.real)
    assert (await _post_other(client)).status == 429


@pytest.mark.parametrize("where", ["before", "after"])
async def test_a_package_landed_after_the_client_left_is_still_charged(make_client, save_dir, monkeypatch, where):
    client = await make_client(max_daily_count=1)
    gate = _GatedCommit(where)
    monkeypatch.setattr(ReceiverStore, "commit", gate.patch())
    _reader, writer = await asyncio.open_connection(client.server.host, client.server.port)
    writer.write(_request_head(len(PACKAGE)).encode() + PACKAGE)
    await writer.drain()
    await asyncio.to_thread(gate.entered.wait, 5)
    writer.close()
    await asyncio.sleep(0.2)
    gate.release.set()
    ledger = _receiver(client).ledger
    await _eventually(lambda: ledger.snapshot(DATE).reserved_count == 0)
    assert (save_dir / DATE / f"{ROOT}.tar.gz").read_bytes() == PACKAGE
    assert ledger.snapshot(DATE).landed_count == 1
    await _eventually(lambda: list((save_dir / ".incoming").iterdir()) == [])
    monkeypatch.setattr(ReceiverStore, "commit", gate.real)
    assert (await _post_other(client)).status == 429


async def test_a_commit_that_fails_releases_the_budget(make_client, save_dir, monkeypatch):
    client = await make_client(max_daily_count=1)
    gate = _GatedCommit("before")
    gate.release.set()
    monkeypatch.setattr(ReceiverStore, "commit", gate.patch())
    real_replace = os.replace

    def _replace(src, dst):
        if str(dst).endswith(".tar.gz"):
            raise OSError("rename refused")
        return real_replace(src, dst)

    monkeypatch.setattr(rcv.os, "replace", _replace)
    assert (await _post(client)).status == 500
    day = _receiver(client).ledger.snapshot(DATE)
    assert (day.landed_count, day.reserved_count) == (0, 0)
    assert list((save_dir / ".incoming").iterdir()) == []
    monkeypatch.setattr(rcv.os, "replace", real_replace)
    assert (await _post_other(client)).status == 201


# --- serving ------------------------------------------------------------------


async def test_serve_binds_serves_and_stops(save_dir, clock):
    import signal

    import aiohttp

    (save_dir / ".incoming").mkdir(parents=True)
    (save_dir / ".incoming" / "stale.part").write_bytes(b"x")
    before = (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT))
    bound = asyncio.Event()
    address = {}

    def _ready(host, port):
        address.update(host=host, port=port)
        bound.set()

    stop = asyncio.Event()
    task = asyncio.create_task(
        rcv.serve(ReceiverConfig(save_dir=save_dir, port=0, clock=clock), ready=_ready, stop=stop)
    )
    await asyncio.wait_for(bound.wait(), 10)
    assert address["port"] != 0
    assert list((save_dir / ".incoming").iterdir()) == []
    url = f"http://{address['host']}:{address['port']}{rcv.ENDPOINT_PATH}"
    async with aiohttp.ClientSession() as session:
        async with session.post(url, data=PACKAGE, headers=_headers()) as resp:
            assert resp.status == 201
    stop.set()
    await asyncio.wait_for(task, 10)
    with pytest.raises(OSError):
        await asyncio.open_connection(address["host"], address["port"])
    assert (signal.getsignal(signal.SIGTERM), signal.getsignal(signal.SIGINT)) == before


def test_run_receiver_runs_serve(save_dir, clock, monkeypatch):
    runs = []

    def _run(coro):
        runs.append(coro.__qualname__)
        coro.close()

    monkeypatch.setattr(rcv.asyncio, "run", _run)
    rcv.run_receiver(ReceiverConfig(save_dir=save_dir, clock=clock))
    assert runs == ["serve"]


_CHILD = """
import sys
from pathlib import Path
from raven.trajectory.receiver import ReceiverConfig, run_receiver
run_receiver(ReceiverConfig(save_dir=Path(sys.argv[1]), port=0), ready=lambda host, port: print(port, flush=True))
"""


async def test_sigterm_stops_a_real_receiver_process(tmp_path):
    import signal
    import subprocess
    import sys

    import aiohttp

    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(tmp_path / "inbox")], stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    try:
        line = await asyncio.wait_for(asyncio.to_thread(proc.stdout.readline), 60)
        if not line:
            stderr = proc.stderr.read().decode(errors="replace")
            if "not permitted" in stderr or "Permission denied" in stderr:
                pytest.skip("this environment does not allow a local listening socket")
            raise AssertionError(f"the receiver did not start: {stderr[-2000:]}")
        port = int(line.strip())
        # aiohttp ignores proxy environment variables by default; urllib would honour them.
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{port}{rcv.HEALTH_PATH}") as resp:
                assert resp.status == 200
        proc.send_signal(signal.SIGTERM)
        assert await asyncio.wait_for(asyncio.to_thread(proc.wait), 15) == 0
        with pytest.raises(OSError):
            await asyncio.open_connection("127.0.0.1", port)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


# --- a temporary file that cannot be removed leaks nothing else ----------------


def _refuse_part_unlink(monkeypatch):
    real_unlink = Path.unlink
    enabled = [True]

    def _unlink(self, missing_ok=False):
        if enabled[0] and self.suffix == ".part":
            raise OSError("unlink refused")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", _unlink)
    return enabled


async def test_unremovable_part_of_a_rejected_upload_frees_the_slot_and_budget(make_client, save_dir, monkeypatch):
    client = await make_client(concurrency=1)
    enabled = _refuse_part_unlink(monkeypatch)
    body = b"not a package"
    resp = await _post(client, body, sha=hashlib.sha256(body).hexdigest())
    assert resp.status == 400
    stale = list((save_dir / ".incoming").iterdir())
    assert len(stale) == 1 and stale[0].suffix == ".part"
    day = _receiver(client).ledger.snapshot(DATE)
    assert (day.reserved_count, day.reserved_bytes) == (0, 0)
    assert (await _post(client)).status == 201
    enabled[0] = False
    restarted = await make_client()
    assert list((save_dir / ".incoming").iterdir()) == []
    assert (await _post(restarted)).status == 200


async def test_unremovable_part_of_a_timed_out_commit_still_counts_the_package(make_client, save_dir, monkeypatch):
    client = await make_client(concurrency=1, total_timeout=0.3, max_daily_count=1)
    gate = _GatedCommit("after")
    monkeypatch.setattr(ReceiverStore, "commit", gate.patch())
    enabled = _refuse_part_unlink(monkeypatch)
    task = asyncio.create_task(_post(client))
    await asyncio.to_thread(gate.entered.wait, 5)
    await asyncio.sleep(0.5)
    gate.release.set()
    assert (await task).status == 408
    assert (save_dir / DATE / f"{ROOT}.tar.gz").read_bytes() == PACKAGE
    day = _receiver(client).ledger.snapshot(DATE)
    assert (day.landed_count, day.reserved_count) == (1, 0)
    enabled[0] = False
    monkeypatch.setattr(ReceiverStore, "commit", gate.real)
    resp = await _post_other(client)
    assert resp.status == 429 and (await resp.json())["error"] == "rate_limited"
