"""The viewer's file endpoint: who may read what, and how it is served.

The path policy is the agent's own (``raven.agent.tools.filesystem.resolve_path``
with the configured workspace and ``tools.restrict_to_workspace``), so these
tests pin the wiring rather than a second policy: what the agent may read, the
viewer serves; what it may not, the viewer refuses.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from raven.agent.tools.deliverables import DeliverableStore
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


async def test_file_relative_path_uses_the_session_workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = load_config()
    cfg.tools.restrict_to_workspace = False
    monkeypatch.setattr(files_module, "load_config", lambda: cfg)
    session_root = tmp_path / "project"
    session_root.mkdir()
    (session_root / "notes.md").write_text("# session\n", encoding="utf-8")

    class Loop:
        def peek_session_workdir(self, session: str) -> Path:
            assert session == "tui:session"
            return session_root

    monkeypatch.setenv("RAVEN_SERVE_TOKEN", TOKEN)
    gateway = WsGateway()
    server = TestServer(build_app(gateway, None, agent_loop_factory=lambda: Loop()))
    client = TestClient(server)
    await client.start_server()
    try:
        response = await client.get(
            "/file",
            params={"path": "notes.md", "session": "tui:session"},
            headers=auth(),
        )
        assert response.status == 200
        assert await response.text() == "# session\n"
    finally:
        await client.close()


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
        path=str(path),
        name=path.name,
        media_type="application/octet-stream",
        size=path.stat().st_size,
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


# ---------------------------------------------------------------------------
# render=pdf: a deck is shown as the PDF LibreOffice makes of it
# ---------------------------------------------------------------------------

FAKE_PDF = b"%PDF-1.4 fake\n%%EOF\n"

_FAKE_SOFFICE = """#!/bin/sh
# The fixture narrows PATH to the fake alone, so the fake names its own tools.
PATH=/usr/bin:/bin
echo "$@" >> "{log}"
mode=$(cat "{mode}")
outdir=""
prev=""
for a in "$@"; do
  [ "$prev" = "--outdir" ] && outdir=$a
  prev=$a
  last=$a
done
stem=$(basename "$last" .pptx)
case $mode in
  hang) sleep 30 ;;
  slow) sleep 0.4; printf '%%PDF-1.4 fake\\n%%%%EOF\\n' > "$outdir/$stem.pdf" ;;
  empty) echo "failed to launch javaldx; Error: source file could not be loaded" >&2 ;;
  *) printf '%%PDF-1.4 fake\\n%%%%EOF\\n' > "$outdir/$stem.pdf" ;;
esac
"""


class FakeSoffice:
    """A `soffice` on PATH that behaves as the mode file says, and keeps a log."""

    def __init__(self, root: Path) -> None:
        root.mkdir()
        self.log = root / "soffice.log"
        self.mode = root / "soffice.mode"
        self.mode.write_text("ok")
        self.bin = root / "bin"
        self.bin.mkdir()
        script = self.bin / "soffice"
        script.write_text(_FAKE_SOFFICE.format(log=self.log, mode=self.mode))
        script.chmod(0o755)

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []


@pytest.fixture
def soffice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeSoffice:
    from raven.rpc import pdf_preview

    fake = FakeSoffice(tmp_path / "fake-office")
    monkeypatch.setenv("PATH", str(fake.bin))
    monkeypatch.setattr(pdf_preview, "cache_dir", lambda: tmp_path / "state" / "pdf-preview")
    monkeypatch.setattr(pdf_preview, "_locks", {})
    return fake


def _deck(tmp_path: Path, name: str = "deck.pptx", payload: bytes = b"PK\x03\x04 slides") -> Path:
    deck = tmp_path / name
    deck.write_bytes(payload)
    return deck


async def _render(client: TestClient, deck: Path):
    return await client.get("/file", params={"path": str(deck), "render": "pdf"}, headers=auth())


async def test_a_deck_is_rendered_and_served_as_a_pdf(client: TestClient, tmp_path: Path, soffice: FakeSoffice) -> None:
    """The viewer asks for a PDF of a deck and gets one, with the PDF's own treatment."""
    deck = _deck(tmp_path)

    r = await _render(client, deck)

    assert r.status == 200
    assert r.headers["Content-Type"] == "application/pdf"
    assert r.headers["Content-Security-Policy"] == "sandbox allow-scripts"
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert await r.read() == FAKE_PDF
    [call] = soffice.calls()
    assert "--headless" in call and "--convert-to pdf" in call and str(deck) in call
    # A profile of its own, so a second LibreOffice never hands its job to a
    # running instance and exits 0 having written nothing.
    assert "-env:UserInstallation=file://" in call
    assert "--norestore" in call


async def test_a_second_click_reads_the_cache(client: TestClient, tmp_path: Path, soffice: FakeSoffice) -> None:
    deck = _deck(tmp_path)

    first = await _render(client, deck)
    second = await _render(client, deck)

    assert first.status == second.status == 200
    assert await second.read() == FAKE_PDF
    assert len(soffice.calls()) == 1


async def test_a_rebuilt_deck_renders_again(client: TestClient, tmp_path: Path, soffice: FakeSoffice) -> None:
    """The cache is keyed by size and mtime, so a deck written over is a miss."""
    import os

    deck = _deck(tmp_path)
    assert (await _render(client, deck)).status == 200

    deck.write_bytes(b"PK\x03\x04 more slides than before")
    later = deck.stat().st_mtime + 5
    os.utime(deck, (later, later))
    assert (await _render(client, deck)).status == 200

    assert len(soffice.calls()) == 2


async def test_the_published_pdf_beside_a_deck_is_served_without_libreoffice(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice
) -> None:
    """The engine publishes `<stem>.pdf` next to every deck it delivers; when it is
    current, that is the rendering, and no conversion runs."""
    import os

    deck = _deck(tmp_path)
    published = tmp_path / "deck.pdf"
    published.write_bytes(b"%PDF-1.4 published by the engine\n%%EOF\n")
    newer = deck.stat().st_mtime + 2
    os.utime(published, (newer, newer))

    r = await _render(client, deck)

    assert r.status == 200
    assert await r.read() == published.read_bytes()
    assert soffice.calls() == []


async def test_a_published_pdf_that_leaves_the_fence_is_rendered_instead(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sibling is a path the page never asked for, so it goes through the viewer's
    own check. A deck whose neighbour is a symlink into raven's state directory would
    otherwise serve that file: the shortcut reaches it without resolve_readable, which
    is what resolves the link and refuses where it lands. A refused sibling is not an
    error -- the conversion runs, into the cache this route owns."""
    import os

    state = tmp_path / "state-home"
    (state / "oauth").mkdir(parents=True)
    secret = state / "oauth" / "tokens.json"
    secret.write_bytes(b"%PDF-1.4 not a preview at all\n%%EOF\n")
    monkeypatch.setenv("RAVEN_HOME", str(state))

    deck = _deck(tmp_path)
    sibling = tmp_path / "deck.pdf"
    sibling.symlink_to(secret)
    newer = deck.stat().st_mtime + 2
    os.utime(secret, (newer, newer))

    r = await _render(client, deck)

    assert r.status == 200
    body = await r.read()
    assert body == FAKE_PDF, "the conversion answered, not the file behind the link"
    assert secret.read_bytes() not in body
    assert len(soffice.calls()) == 1


def test_the_sibling_is_fenced_against_the_workspace_it_is_given(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One policy context, not two. handle_file admits the deck against the session's
    own workspace, so the sibling shortcut has to face that same root: checking it
    against the configured default pointed both ways with restrict_to_workspace on --
    a deck.pdf in the session's root symlinked to a PDF the session may not request
    was accepted, and an ordinary sibling in that root was refused into a render of
    what already existed."""
    import os

    from raven.rpc import pdf_preview

    session = tmp_path / "session-root"
    elsewhere = tmp_path / "configured"
    session.mkdir()
    elsewhere.mkdir()
    forbidden = elsewhere / "other.pdf"
    forbidden.write_bytes(b"%PDF-1.4 a file this session may not ask for\n%%EOF\n")

    cfg = load_config()
    cfg.tools.restrict_to_workspace = True
    cfg.agents.defaults.workspace = str(elsewhere)
    monkeypatch.setattr(files_module, "load_config", lambda: cfg)

    deck = _deck(session)
    link = session / "deck.pdf"
    link.symlink_to(forbidden)
    newer = deck.stat().st_mtime + 2
    os.utime(forbidden, (newer, newer))

    assert pdf_preview.published_pdf(deck, session) is None, "the link leaves the session's root"
    # The fence the bug applied: the configured workspace holds the target, so the
    # default context takes it.
    assert pdf_preview.published_pdf(deck) == forbidden.resolve()

    link.unlink()
    published = session / "deck.pdf"
    published.write_bytes(b"%PDF-1.4 published beside the deck\n%%EOF\n")
    os.utime(published, (newer, newer))

    assert pdf_preview.published_pdf(deck, session) == published, "an ordinary sibling is not a render"
    assert pdf_preview.published_pdf(deck) is None, "and the wrong fence refused it"


async def test_the_render_route_carries_the_sessions_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The route resolves the deck against the session's workspace and hands the same
    root to the render path, so the sibling shortcut cannot face a different fence from
    the deck. Its own gateway, because the session branch only runs where an agent-loop
    factory is wired."""
    from raven.rpc import pdf_preview

    cfg = load_config()
    cfg.agents.defaults.workspace = str(tmp_path)
    cfg.tools.restrict_to_workspace = False
    monkeypatch.setattr(files_module, "load_config", lambda: cfg)
    monkeypatch.setenv("RAVEN_SERVE_TOKEN", TOKEN)

    seen: list[object] = []
    rendered = tmp_path / "rendered.pdf"
    rendered.write_bytes(FAKE_PDF)

    async def _fake(source: Path, *, timeout_s: float | None = None, workspace: Path | None = None) -> Path:
        seen.append(workspace)
        return rendered

    monkeypatch.setattr(pdf_preview, "pdf_for", _fake)
    monkeypatch.setattr("raven.rpc.methods.console._safe_loop", lambda factory: object(), raising=False)
    monkeypatch.setattr("raven.rpc.methods.console._workspace_root", lambda loop, key: tmp_path, raising=False)

    # Through build_app's own parameter: it assigns the attribute from the argument,
    # so setting it on the instance first is overwritten.
    app = build_app(WsGateway(), None, agent_loop_factory=lambda: object())
    c = TestClient(TestServer(app))
    await c.start_server()
    try:
        deck = _deck(tmp_path)
        r = await c.get("/file", params={"path": str(deck), "render": "pdf"}, headers=auth())
        assert r.status == 200 and seen == [None], "no session named, no workspace to carry"

        r = await c.get("/file", params={"path": str(deck), "render": "pdf", "session": "web:s1"}, headers=auth())
        assert r.status == 200
        assert seen[-1] == tmp_path, "the session's root reached the render path"
    finally:
        await c.close()


async def test_a_stale_published_pdf_is_not_trusted(client: TestClient, tmp_path: Path, soffice: FakeSoffice) -> None:
    import os

    deck = _deck(tmp_path)
    published = tmp_path / "deck.pdf"
    published.write_bytes(b"%PDF-1.4 from an older build\n%%EOF\n")
    older = deck.stat().st_mtime - 60
    os.utime(published, (older, older))

    r = await _render(client, deck)

    assert r.status == 200
    assert await r.read() == FAKE_PDF
    assert len(soffice.calls()) == 1


async def test_two_simultaneous_clicks_run_one_conversion(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice
) -> None:
    """A double click must not start two LibreOffices against the same deck."""
    import asyncio

    soffice.mode.write_text("slow")
    deck = _deck(tmp_path)

    first, second = await asyncio.gather(_render(client, deck), _render(client, deck))

    assert first.status == second.status == 200
    assert await first.read() == await second.read() == FAKE_PDF
    assert len(soffice.calls()) == 1


async def test_a_conversion_that_writes_nothing_is_a_clear_error(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice
) -> None:
    """LibreOffice exits 0 for a document it could not load; the missing file is the
    failure, and the page is told so in words."""
    soffice.mode.write_text("empty")
    deck = _deck(tmp_path)

    r = await _render(client, deck)

    assert r.status == 500
    body = await r.text()
    assert "did not produce a PDF for deck.pptx" in body
    assert "could not be loaded" in body


async def test_a_hung_conversion_is_stopped_and_reported(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    from raven.rpc import pdf_preview

    monkeypatch.setattr(pdf_preview, "CONVERT_TIMEOUT_S", 0.5)
    soffice.mode.write_text("hang")
    deck = _deck(tmp_path)

    started = time.monotonic()
    r = await _render(client, deck)

    assert r.status == 504
    assert "longer than 0.5s" in await r.text()
    assert time.monotonic() - started < 10, "the process group was not killed"
    assert not any(pdf_preview.cache_dir().glob("*.pdf"))


async def test_a_cached_rendering_that_is_still_in_use_is_not_swept(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sweep reads mtime, and mtime is now when the entry was last served.

    Written-at would have deleted a rendering this route had just handed back: a
    cached file older than the TTL is returned as it stands, and the next render's
    sweep took it while the response that asked for it had not opened it yet --
    lost on any platform, and on Windows lost with a handle open.
    """
    import os

    from raven.rpc import pdf_preview

    deck = _deck(tmp_path)
    assert (await _render(client, deck)).status == 200
    cached = next(pdf_preview.cache_dir().glob("*.pdf"))
    stale = time.time() - pdf_preview.CACHE_TTL_S - 60
    os.utime(cached, (stale, stale))

    served = await _render(client, deck)

    assert served.status == 200 and cached.is_file(), "the entry was swept while it was the answer"
    assert cached.stat().st_mtime > stale, "and it is dated by this use, so the next sweep spares it"

    other = _deck(tmp_path, name="second.pptx", payload=b"PK\x03\x04 other")
    assert (await _render(client, other)).status == 200
    assert cached.is_file(), "a later render sweeps by last use, and this one was just used"


async def test_a_host_without_libreoffice_says_so(
    client: TestClient, tmp_path: Path, soffice: FakeSoffice, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    deck = _deck(tmp_path)

    r = await _render(client, deck)

    assert r.status == 503
    assert "LibreOffice is not installed" in await r.text()


async def test_only_a_deck_can_be_asked_for_as_a_pdf(client: TestClient, tmp_path: Path, soffice: FakeSoffice) -> None:
    (tmp_path / "notes.md").write_text("# hi\n", encoding="utf-8")

    r = await client.get("/file", params={"path": str(tmp_path / "notes.md"), "render": "pdf"}, headers=auth())

    assert r.status == 400
    assert soffice.calls() == []


async def test_rendering_needs_a_session_too(client: TestClient, tmp_path: Path, soffice: FakeSoffice) -> None:
    """Starting LibreOffice on the host is not something an unauthenticated
    request may do, however harmless the file."""
    deck = _deck(tmp_path)

    r = await client.get("/file", params={"path": str(deck), "render": "pdf"})

    assert r.status == 401
    assert soffice.calls() == []
