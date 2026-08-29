"""Multi-module smoke: the tool registers a deliverable and the gateway's real
aiohttp routes serve exactly those bytes back."""

from __future__ import annotations

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from raven.agent.tools.deliver import DeliverFilesTool
from raven.agent.tools.deliverables import DeliverableStore
from raven.rpc.transports.deliverables import add_files_routes


async def test_deliver_then_download_round_trip(tmp_path) -> None:
    workspace = tmp_path / "chanwork"
    workspace.mkdir()
    payload = b"report bytes, exactly these"
    (workspace / "report.txt").write_bytes(payload)

    store = DeliverableStore(tmp_path / "deliverables.json")
    tool = DeliverFilesTool(store, workspace=workspace, allowed_dirs=(workspace,))
    tool.set_context("web", "default", "web:s1")

    await tool.execute(files=[{"path": "report.txt", "title": "The report"}])
    manifest = tool.take_metadata()["raven_delivery"]
    entry = manifest["files"][0]

    app = web.Application()
    add_files_routes(app, store)
    async with TestClient(TestServer(app)) as client:
        res = await client.get("/files/download", params={"token": entry["token"]})

        assert res.status == 200
        assert await res.read() == payload
        assert "report.txt" in res.headers["Content-Disposition"]


async def test_delivery_survives_a_store_restart(tmp_path) -> None:
    """The gateway can restart between delivery and download; the persisted
    registry is what keeps the button working."""
    workspace = tmp_path / "chanwork"
    workspace.mkdir()
    (workspace / "a.txt").write_bytes(b"AAA")
    path = tmp_path / "deliverables.json"

    tool = DeliverFilesTool(DeliverableStore(path), workspace=workspace, allowed_dirs=(workspace,))
    tool.set_context("web", "default", "web:s1")
    await tool.execute(files=[{"path": "a.txt"}])
    token = tool.take_metadata()["raven_delivery"]["files"][0]["token"]

    app = web.Application()
    add_files_routes(app, DeliverableStore(path))
    async with TestClient(TestServer(app)) as client:
        res = await client.get("/files/download", params={"token": token})

        assert res.status == 200
        assert await res.read() == b"AAA"
