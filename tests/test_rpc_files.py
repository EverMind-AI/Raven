"""The viewer's file endpoint: who may read what, and how it is served.

The path policy is the agent's own (``raven.agent.tools.filesystem._resolve_path``
with the configured workspace and ``tools.restrict_to_workspace``), so these
tests pin the wiring rather than a second policy: what the agent may read, the
viewer serves; what it may not, the viewer refuses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from raven.agent.tools._deliverables import DeliverableStore
from raven.config import load_config
from raven.rpc import files as files_module
from raven.rpc.transports.ws import WsGateway, build_app

TOKEN = "test-token"


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[TestClient]:
    """A serve app whose workspace is tmp_path, reachable with TOKEN."""
    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    cfg.tools.restrict_to_workspace = False
    monkeypatch.setattr(files_module, "load_config", lambda: cfg)
    monkeypatch.setenv("RAVEN_SERVE_TOKEN", TOKEN)
    gateway = WsGateway()
    server = TestServer(build_app(gateway, None))
    c = TestClient(server)
    await c.start_server()
    c.raven_cfg = cfg  # type: ignore[attr-defined]
    try:
        yield c
    finally:
        await c.close()


def auth() -> dict[str, str]:
    return {"X-Raven-Token": TOKEN}


async def test_file_serves_a_workspace_file(client: TestClient, tmp_path: Path) -> None:
    """A file the agent just wrote opens, as UTF-8 text, inline."""
    (tmp_path / "notes.md").write_text("# hi\n\n- one\n", encoding="utf-8")

    r = await client.get("/file", params={"path": str(tmp_path / "notes.md")}, headers=auth())

    assert r.status == 200
    assert r.headers["Content-Type"] == "text/plain; charset=utf-8"
    assert r.headers["Content-Disposition"] == "inline"
    assert await r.text() == "# hi\n\n- one\n"


async def test_file_response_is_sandboxed(client: TestClient, tmp_path: Path) -> None:
    """HTML and SVG the agent produced must not run with the page's origin.

    Without the sandbox directive an artifact opened same-origin could reach the
    session cookie and the RPC socket, so the header is the load-bearing part of
    this endpoint -- not decoration.
    """
    (tmp_path / "report.html").write_text("<h1>hi</h1>", encoding="utf-8")

    r = await client.get("/file", params={"path": str(tmp_path / "report.html")}, headers=auth())

    assert r.status == 200
    assert r.headers["Content-Security-Policy"] == "sandbox"
    assert r.headers["X-Content-Type-Options"] == "nosniff"


async def test_pdf_is_sandboxed_but_may_run_its_viewer(client: TestClient, tmp_path: Path) -> None:
    """A PDF needs allow-scripts, and must still get an opaque origin.

    The browser's PDF viewer is script-driven: denied scripts it renders a blank
    frame. Granting allow-scripts without allow-same-origin keeps the frame off
    this page's origin, which is the property that matters.
    """
    (tmp_path / "doc.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")

    r = await client.get("/file", params={"path": str(tmp_path / "doc.pdf")}, headers=auth())

    assert r.status == 200
    csp = r.headers["Content-Security-Policy"]
    assert csp == "sandbox allow-scripts"
    assert "allow-same-origin" not in csp


async def test_file_needs_a_session(client: TestClient, tmp_path: Path) -> None:
    """No cookie and no token: the endpoint is not a public file server."""
    (tmp_path / "secret.txt").write_text("s", encoding="utf-8")

    r = await client.get("/file", params={"path": str(tmp_path / "secret.txt")})

    assert r.status == 401


async def test_file_relative_path_resolves_against_the_workspace(client: TestClient, tmp_path: Path) -> None:
    """The transcript carries workspace-relative paths, so those must open too."""
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "a.txt").write_text("a", encoding="utf-8")

    r = await client.get("/file", params={"path": "out/a.txt"}, headers=auth())

    assert r.status == 200
    assert await r.text() == "a"


async def test_file_outside_workspace_is_served_when_the_agent_may_read_it(client: TestClient, tmp_path: Path) -> None:
    """restrict_to_workspace is off by default, and the viewer follows the agent.

    Confining the viewer on its own would make it useless for the common case:
    the agent edits a file in the user's project, which is not the workspace.
    """
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("o", encoding="utf-8")
    try:
        r = await client.get("/file", params={"path": str(outside)}, headers=auth())
        assert r.status == 200
    finally:
        outside.unlink()


async def test_file_outside_workspace_is_refused_when_the_agent_is_confined(client: TestClient, tmp_path: Path) -> None:
    """With restrict_to_workspace on, the viewer refuses what the agent cannot read."""
    client.raven_cfg.tools.restrict_to_workspace = True  # type: ignore[attr-defined]
    outside = tmp_path.parent / "outside2.txt"
    outside.write_text("o", encoding="utf-8")
    try:
        r = await client.get("/file", params={"path": str(outside)}, headers=auth())
        assert r.status == 403
    finally:
        outside.unlink()


async def test_file_traversal_is_resolved_before_the_check(client: TestClient, tmp_path: Path) -> None:
    """`..` is not a path component the check can be talked out of."""
    client.raven_cfg.tools.restrict_to_workspace = True  # type: ignore[attr-defined]
    outside = tmp_path.parent / "outside3.txt"
    outside.write_text("o", encoding="utf-8")
    try:
        r = await client.get("/file", params={"path": f"../{outside.name}"}, headers=auth())
        assert r.status == 403
    finally:
        outside.unlink()


async def test_file_missing_and_directory_are_not_found(client: TestClient, tmp_path: Path) -> None:
    """A directory is not viewable, and neither is a path that is not there."""
    (tmp_path / "dir").mkdir()

    missing = await client.get("/file", params={"path": str(tmp_path / "nope.txt")}, headers=auth())
    directory = await client.get("/file", params={"path": str(tmp_path / "dir")}, headers=auth())

    assert missing.status == 404
    assert directory.status == 404


async def test_file_without_a_path_is_a_bad_request(client: TestClient) -> None:
    r = await client.get("/file", headers=auth())

    assert r.status == 400


async def test_file_too_large_is_refused(client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the cap no renderer in the page does anything useful with the bytes."""
    monkeypatch.setattr(files_module, "MAX_VIEW_BYTES", 8)
    (tmp_path / "big.txt").write_text("0123456789", encoding="utf-8")

    r = await client.get("/file", params={"path": str(tmp_path / "big.txt")}, headers=auth())

    assert r.status == 413


async def test_delivered_file_uses_its_capability_route_without_the_viewer_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RAVEN_SERVE_TOKEN", TOKEN)
    path = tmp_path / "large.bin"
    path.write_bytes(b"0123456789")
    store = DeliverableStore(tmp_path / "deliverables.json")
    record = store.register(
        path=str(path), name=path.name, media_type="application/octet-stream", size=path.stat().st_size,
        conversation="tui:default",
    )
    gateway = WsGateway()
    server = TestServer(build_app(gateway, None, deliverables=store))
    client = TestClient(server)
    await client.start_server()
    try:
        denied = await client.head("/files/download", params={"token": record.token})
        served = await client.head("/files/download", params={"token": record.token}, headers=auth())
        assert denied.status == 401
        assert served.status == 200
        assert served.headers["Content-Disposition"].startswith("attachment;")
    finally:
        await client.close()


async def test_content_type_for_known_binaries(tmp_path: Path) -> None:
    """An image opens as an image; an unknown extension does not pose as text."""
    assert files_module.content_type_for(tmp_path / "a.png") == "image/png"
    assert files_module.content_type_for(tmp_path / "a.pdf") == "application/pdf"
    assert files_module.content_type_for(tmp_path / "a.bin") == "application/octet-stream"
    assert files_module.content_type_for(tmp_path / "a.py") == "text/plain; charset=utf-8"
