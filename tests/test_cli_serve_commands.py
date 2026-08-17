"""`raven serve` -- locating the page it serves.

Two independent pieces of the repo have to agree on one path for a released
raven to answer with the real page: the wheel build hook copies ``ui/dist`` to
``raven/ui/dist``, and ``resolve_ui_dist`` looks for it there. Nothing failed
loudly when they disagreed -- the gateway came up, the WebSocket worked, and
``/`` answered with the "No front end built here" placeholder, which is
indistinguishable from a developer who simply never ran the build.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import typer

from raven.cli import serve_commands


@pytest.fixture
def two_candidates(tmp_path: Path, monkeypatch):
    """A packaged copy and a source-tree copy, neither built yet."""
    packaged = tmp_path / "wheel" / "raven" / "ui" / "dist"
    source = tmp_path / "repo" / "ui"
    monkeypatch.setattr(serve_commands, "_PACKAGED_UI_DIST", packaged)
    monkeypatch.setattr(serve_commands, "_UI_DIR", source)
    return packaged, source / "dist"


def _build(dist: Path, marker: str) -> None:
    dist.mkdir(parents=True, exist_ok=True)
    (dist / "index.html").write_text(marker, encoding="utf-8")


def test_no_page_built_is_none_rather_than_a_broken_path(two_candidates) -> None:
    """The gateway stays up and serves its notice; it must not be handed a
    directory that does not exist."""
    assert serve_commands.resolve_ui_dist() is None


def test_the_source_tree_serves_a_developer_who_built_it(two_candidates) -> None:
    _packaged, source = two_candidates
    _build(source, "source")

    assert serve_commands.resolve_ui_dist() == source


def test_the_packaged_copy_wins_over_a_stale_source_tree(two_candidates) -> None:
    """An installed raven runs from the wheel. A source tree that happens to sit
    beside it is somebody's checkout, not what this install shipped."""
    packaged, source = two_candidates
    _build(source, "source")
    _build(packaged, "packaged")

    assert serve_commands.resolve_ui_dist() == packaged


def test_a_directory_without_an_index_does_not_count(two_candidates) -> None:
    """A directory is not a built page.

    Not a state `ui/build.py` leaves behind, as it happens -- it writes
    index.html first and copies the assets second, and a failed marker check
    exits before `dist/` exists at all. The reachable half-built state is the
    opposite one (index.html present, assets missing or half-copied), and that
    one is accepted here on purpose: the page loads and its images do not,
    which is better than refusing to serve. The wheel gate in release.yml is
    where the missing assets are caught."""
    packaged, source = two_candidates
    (packaged / "assets").mkdir(parents=True)
    _build(source, "source")

    assert serve_commands.resolve_ui_dist() == source


def test_the_build_hook_copies_the_page_where_serve_looks_for_it() -> None:
    """The seam itself, asserted across the two files that must agree.

    Read as text rather than by importing the hook: hatchling is a build-time
    dependency and is not installed in the test environment, and the value that
    matters is the literal destination string either way.
    """
    hook = (Path(__file__).resolve().parent.parent / "hatch_build.py").read_text(encoding="utf-8")
    destinations = re.findall(r'=\s*"(raven/ui/[^"]+)"', hook)

    assert destinations == ["raven/ui/dist"], f"the hook's destination moved: {destinations}"

    packaged = serve_commands._PACKAGED_UI_DIST
    assert packaged.parts[-3:] == ("raven", "ui", "dist")
    # And it must be inside the installed package, not next to it: the hook puts
    # the page under `raven/`, so a resolver pointed one level out would find
    # nothing in a wheel while still passing every test above.
    assert packaged.parent.parent == Path(serve_commands.__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# `raven web` -- open the page, and attach rather than start a second engine.
#
# Split by layer because the layers can only be reached separately: `_attach`
# does real HTTP against a real gateway, and `_web` owns the loop that probe
# runs in, so a test inside a loop cannot call it.
# ---------------------------------------------------------------------------


@pytest.fixture
def home(tmp_path: Path, monkeypatch) -> Path:
    """An agent home of our own, so nothing here reads the developer's serve.json."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


class TestTheStateFileFollowsTheAgentHome:
    """`raven web` finds the engine through this file, so where it lands has to
    be the one answer the rest of raven gives. Read inline, `RAVEN_HOME` was
    resolved a second way that disagreed with the canonical one in three cases,
    and in each of them the state file became cwd-relative while the engine ran
    somewhere else -- which is a second engine on the next `raven web`."""

    @pytest.mark.parametrize("value", ["", "   "])
    def test_a_blank_setting_is_not_a_home(self, monkeypatch, value: str) -> None:
        # os.environ.get(name, default) hands back the empty string rather than
        # the default, so `RAVEN_HOME=` in a shell profile put serve.json in the
        # working directory.
        monkeypatch.setenv("RAVEN_HOME", value)
        assert serve_commands._state_path() == Path.home() / ".raven" / "serve.json"

    def test_a_tilde_is_expanded(self, monkeypatch) -> None:
        monkeypatch.setenv("RAVEN_HOME", "~/alt-home")
        assert serve_commands._state_path() == Path.home() / "alt-home" / "serve.json"

    def test_it_lands_beside_the_config(self, monkeypatch, tmp_path: Path) -> None:
        """The property that matters, stated once: same home, both files."""
        from raven.config.loader import get_config_path

        monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "elsewhere"))
        assert serve_commands._state_path().parent == get_config_path().parent


@pytest.fixture
def a_built_page(two_candidates) -> None:
    _packaged, source = two_candidates
    _build(source, "page")


@pytest.fixture
def opened(monkeypatch) -> list[str]:
    """Every URL `web` tried to open, instead of opening one."""
    urls: list[str] = []
    monkeypatch.setattr(serve_commands.webbrowser, "open", lambda url: urls.append(url) or True)
    return urls


@pytest.fixture
def started(monkeypatch) -> list[int]:
    """Every gateway `web` started, which most of these must not do."""
    ports: list[int] = []
    monkeypatch.setattr(serve_commands, "_run", lambda port, open_browser: ports.append(port))
    return ports


async def _live_gateway(home: Path, *, token: str | None = None):
    """A real gateway on a real port, recorded the way `serve` records it."""
    from aiohttp.test_utils import TestServer

    from raven.rpc.transports.ws import WsGateway, build_app

    gateway = WsGateway()
    server = TestServer(build_app(gateway, None))
    await server.start_server()
    home.mkdir(parents=True, exist_ok=True)
    (home / "serve.json").write_text(
        json.dumps({"port": server.port, "token": token or gateway.session_token, "pid": 1}), encoding="utf-8"
    )
    return server


class TestAttaching:
    """The probe, against a gateway that is really listening."""

    async def test_it_returns_an_auth_url_the_gateway_will_honour(self, home: Path) -> None:
        """End to end rather than asserting the string: the nonce has to have been
        minted by that process, which is the only reason attaching authenticates
        at all."""
        import aiohttp

        server = await _live_gateway(home)
        try:
            url = await serve_commands._attach(*serve_commands._read_serve_state())

            assert url.startswith(f"http://127.0.0.1:{server.port}/auth#")
            nonce = url.rsplit("#", 1)[1]
            async with aiohttp.ClientSession() as session:
                async with session.post(server.make_url("/auth/exchange"), json={"nonce": nonce}) as exchanged:
                    assert exchanged.status == 200, await exchanged.text()
        finally:
            await server.close()

    async def test_a_nonce_is_good_once(self, home: Path) -> None:
        """Which is why `web` mints a fresh one per launch instead of reusing the
        URL it printed last time."""
        import aiohttp

        server = await _live_gateway(home)
        try:
            url = await serve_commands._attach(*serve_commands._read_serve_state())
            nonce = url.rsplit("#", 1)[1]
            async with aiohttp.ClientSession() as session:
                async with session.post(server.make_url("/auth/exchange"), json={"nonce": nonce}):
                    pass
                async with session.post(server.make_url("/auth/exchange"), json={"nonce": nonce}) as again:
                    assert again.status != 200
        finally:
            await server.close()

    async def test_nothing_listening_is_nothing_to_attach_to(self, home: Path) -> None:
        home.mkdir(parents=True, exist_ok=True)
        (home / "serve.json").write_text(json.dumps({"port": 1, "token": "t", "pid": 9}), encoding="utf-8")

        assert await serve_commands._attach(*serve_commands._read_serve_state()) is None

    async def test_something_else_on_that_port_is_not_our_gateway(self, home: Path) -> None:
        """Ports get reused. Handing the token to whatever answers would be worse
        than starting a gateway of our own."""
        from aiohttp import web as aiohttp_web
        from aiohttp.test_utils import TestServer

        app = aiohttp_web.Application()

        async def _not_us(_request):
            return aiohttp_web.json_response({"service": "something-else"})

        app.router.add_get("/health", _not_us)
        stranger = TestServer(app)
        await stranger.start_server()
        home.mkdir(parents=True, exist_ok=True)
        (home / "serve.json").write_text(json.dumps({"port": stranger.port, "token": "t", "pid": 1}), encoding="utf-8")
        try:
            assert await serve_commands._attach(*serve_commands._read_serve_state()) is None
        finally:
            await stranger.close()

    async def test_a_gateway_of_ours_that_refuses_the_token_is_raised_not_ignored(self, home: Path) -> None:
        """Returning None here would start a second engine beside a live one."""
        server = await _live_gateway(home, token="not-its-token")
        try:
            with pytest.raises(PermissionError):
                await serve_commands._attach(*serve_commands._read_serve_state())
        finally:
            await server.close()


class TestTheCommand:
    """The orchestration, with the probe's answer supplied."""

    def test_it_opens_what_the_probe_found_and_starts_nothing(
        self, home: Path, a_built_page, opened: list[str], started: list[int], monkeypatch
    ) -> None:
        """Two engines on one agent home would race over the same sessions and the
        same store, so a second `web` attaches instead."""
        monkeypatch.setattr(serve_commands, "_attached_url", lambda: "http://127.0.0.1:31337/auth#abc")

        serve_commands._web(port=18999)

        assert opened == ["http://127.0.0.1:31337/auth#abc"]
        assert started == [], "it started a second gateway instead of attaching"

    def test_it_starts_one_when_there_is_nothing_to_attach_to(
        self, home: Path, a_built_page, opened: list[str], started: list[int], monkeypatch
    ) -> None:
        monkeypatch.setattr(serve_commands, "_attached_url", lambda: None)

        serve_commands._web(port=18999)

        assert started == [18999]
        assert opened == [], "the gateway it starts opens the browser itself"

    def test_a_dead_gateway_s_leftover_file_is_a_first_launch(
        self, home: Path, a_built_page, started: list[int]
    ) -> None:
        """The file is removed on a clean shutdown only, so a killed gateway leaves
        one behind. Nothing answers, so nothing is attached to."""
        home.mkdir(parents=True, exist_ok=True)
        (home / "serve.json").write_text(json.dumps({"port": 1, "token": "t", "pid": 999999}), encoding="utf-8")

        serve_commands._web(port=18999)

        assert started == [18999]

    def test_a_token_mismatch_stops_rather_than_doubling_up(
        self, home: Path, a_built_page, opened: list[str], started: list[int], monkeypatch
    ) -> None:
        def _refused() -> str:
            raise PermissionError("the gateway on port 31337 refused the recorded token")

        monkeypatch.setattr(serve_commands, "_attached_url", _refused)

        with pytest.raises(typer.Exit) as exit_info:
            serve_commands._web(port=18999)

        assert exit_info.value.exit_code == 1
        assert started == [] and opened == []

    def test_no_page_built_refuses_instead_of_opening_the_placeholder(
        self, two_candidates, home: Path, started: list[int]
    ) -> None:
        """`serve` is still useful with no page -- the WebSocket is the point of it
        -- but the page is the whole point of `web`."""
        with pytest.raises(typer.Exit) as exit_info:
            serve_commands._web(port=18999)

        assert exit_info.value.exit_code == 1
        assert started == []


def test_web_is_registered_as_its_own_command() -> None:
    """A verb nobody can type is not a verb."""
    app = typer.Typer()
    serve_commands.register(app)

    assert {c.name for c in app.registered_commands} == {"serve", "web"}
