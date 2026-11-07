"""Tests for the trajectory export download route. The token is the trust boundary:
no path ever appears in a request."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from raven.rpc.transports.trajectory_downloads import add_export_routes
from raven.trajectory import export_jobs
from raven.trajectory import selection as sel
from raven.trajectory.export_jobs import DOWNLOAD_PATH, ExportJobs

PACKAGE = b"\x1f\x8b fake package bytes"


def _plan() -> sel.SelectionPlan:
    turn = sel.TurnSummary("trace-a", "span-a", 1, "2026-10-01T10:01:00+00:00", None, "", False, None, True)
    return sel.SelectionPlan("tui:s1", ("trace-a",), (), (turn,), (turn,))


@pytest.fixture
def jobs(tmp_path: Path, monkeypatch) -> ExportJobs:
    """A registry whose worker writes a fixed package instead of exporting."""

    def fake_collect(plan, *, state_dir=None):
        return sel.Selection(plan=plan, spans=())

    def fake_write(selection, cut, out_file, *, export_id, state_dir=None, should_stop=lambda: False):
        Path(out_file).write_bytes(PACKAGE)
        from raven.trajectory.export import ExportSummary

        return ExportSummary(export_id, {"spans": 0, "artifacts": 0, "messages": 0}, (), (), (), len(PACKAGE))

    monkeypatch.setattr(export_jobs, "collect_selection", fake_collect)
    monkeypatch.setattr(export_jobs, "write_export", fake_write)
    return ExportJobs(lambda: tmp_path / "exports")


def _finish(jobs: ExportJobs, tmp_path: Path, name: str = "trip.tar.gz") -> tuple[str, str]:
    job = jobs.start(_plan(), file_name=name, session_path=tmp_path / "none.jsonl")
    deadline = time.monotonic() + 5
    while jobs.status(job)["state"] != "done":
        assert time.monotonic() < deadline
        time.sleep(0.01)
    return job, jobs.status(job)["download_url"].split("token=", 1)[1]


@pytest.fixture
async def client(jobs):
    app = web.Application()
    add_export_routes(app, jobs)
    async with TestClient(TestServer(app)) as c:
        yield c


async def test_a_finished_export_downloads_with_an_attachment_header(client, jobs, tmp_path):
    _, token = _finish(jobs, tmp_path)
    res = await client.get(DOWNLOAD_PATH, params={"token": token})
    assert res.status == 200 and await res.read() == PACKAGE
    assert res.headers["Content-Type"] == "application/gzip"
    assert res.headers["Cache-Control"] == "no-store"
    assert res.headers["Content-Disposition"] == 'attachment; filename="trip.tar.gz"'


async def test_a_non_ascii_name_travels_percent_encoded(client, jobs, tmp_path):
    _, token = _finish(jobs, tmp_path, name="Trip été.tar.gz")
    res = await client.get(DOWNLOAD_PATH, params={"token": token})
    disposition = res.headers["Content-Disposition"]
    assert 'filename="Trip _t_.tar.gz"' in disposition
    assert "filename*=UTF-8''Trip%20%C3%A9t%C3%A9.tar.gz" in disposition


async def test_head_agrees_with_get(client, jobs, tmp_path):
    _, token = _finish(jobs, tmp_path)
    res = await client.head(DOWNLOAD_PATH, params={"token": token})
    assert res.status == 200 and res.headers["Content-Type"] == "application/gzip"


async def test_a_missing_token_is_400(client):
    assert (await client.get(DOWNLOAD_PATH)).status == 400


async def test_an_unknown_token_is_410(client):
    assert (await client.get(DOWNLOAD_PATH, params={"token": "nope"})).status == 410


async def test_a_cancelled_export_is_410(client, jobs, tmp_path):
    job, token = _finish(jobs, tmp_path)
    jobs.cancel(job)
    assert (await client.get(DOWNLOAD_PATH, params={"token": token})).status == 410


async def test_a_package_deleted_under_its_token_is_410(client, jobs, tmp_path):
    _, token = _finish(jobs, tmp_path)
    path, _ = jobs.download(token)
    path.unlink()
    assert (await client.get(DOWNLOAD_PATH, params={"token": token})).status == 410


async def test_an_expired_export_is_410(tmp_path, monkeypatch, jobs):
    now = [1000.0]
    expiring = ExportJobs(lambda: tmp_path / "exports", ttl=60.0, clock=lambda: now[0])
    _, token = _finish(expiring, tmp_path)
    app = web.Application()
    add_export_routes(app, lambda: expiring)
    async with TestClient(TestServer(app)) as c:
        assert (await c.get(DOWNLOAD_PATH, params={"token": token})).status == 200
        now[0] += 61
        assert (await c.get(DOWNLOAD_PATH, params={"token": token})).status == 410


# -- mounted by build_app, behind the transport's guard -------------------------


@pytest.fixture
async def gateway_client(jobs, monkeypatch):
    from raven.rpc.transports.ws import WsGateway, build_app

    monkeypatch.setattr(export_jobs, "JOBS", jobs)
    gateway = WsGateway()
    server = TestServer(build_app(gateway, None))
    client = TestClient(server)
    await client.start_server()
    gateway.port = server.port
    try:
        yield gateway, client
    finally:
        await client.close()


async def test_build_app_mounts_the_route_behind_the_session_guard(gateway_client, jobs, tmp_path):
    gateway, client = gateway_client
    _, token = _finish(jobs, tmp_path)
    assert (await client.get(DOWNLOAD_PATH, params={"token": token})).status == 401
    await client.post("/auth/exchange", json={"nonce": gateway.mint_nonce()})
    res = await client.get(DOWNLOAD_PATH, params={"token": token})
    assert res.status == 200 and await res.read() == PACKAGE


async def test_build_app_refuses_a_foreign_origin(gateway_client, jobs, tmp_path):
    gateway, client = gateway_client
    _, token = _finish(jobs, tmp_path)
    await client.post("/auth/exchange", json={"nonce": gateway.mint_nonce()})
    res = await client.get(DOWNLOAD_PATH, params={"token": token}, headers={"Origin": "http://evil.example"})
    assert res.status == 403


async def test_shutting_the_app_down_cancels_the_exports(jobs, tmp_path, monkeypatch):
    from raven.rpc.transports.ws import WsGateway, build_app

    monkeypatch.setattr(export_jobs, "JOBS", jobs)
    _, token = _finish(jobs, tmp_path)
    client = TestClient(TestServer(build_app(WsGateway(), None)))
    await client.start_server()
    await client.close()
    assert jobs.download(token) is None
    assert list((tmp_path / "exports").iterdir()) == []
