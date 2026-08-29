"""`raven serve` -- locating the page it serves.

Two independent pieces of the repo have to agree on one path for a released
raven to answer with the real page: the wheel build hook copies ``ui-web/dist`` to
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
    source = tmp_path / "repo" / "ui-web"
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

    Not a state `ui-web/build.py` leaves behind, as it happens -- it writes
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
    """Every in-process serve stack these commands built.

    Right for `raven serve`, which IS that stack, and wrong for `raven web`: that
    engine is the one with no ChannelManager, so a `web` that reaches for it is a
    page whose entrances cannot start an adapter. Kept apart from the foreground
    launch below for exactly that reason -- one shared spy would let that
    regression through.
    """
    ports: list[int] = []
    monkeypatch.setattr(serve_commands, "_run", lambda port, open_browser: ports.append(port))
    return ports


@pytest.fixture
def foregrounded(monkeypatch) -> list[int]:
    """Every engine `web --foreground` launched as its own process."""
    ports: list[int] = []
    monkeypatch.setattr(serve_commands, "_run_foreground", lambda port: ports.append(port))
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


@pytest.fixture
def supervised(monkeypatch) -> list[int]:
    """Every port a supervisor was left behind on, instead of leaving one."""
    ports: list[int] = []
    monkeypatch.setattr(serve_commands, "_spawn_supervisor", lambda port: ports.append(port))
    return ports


class TestTheCommand:
    """The orchestration, with the probe's answer supplied."""

    def test_it_opens_what_the_probe_found_and_starts_nothing(
        self, home: Path, a_built_page, opened: list[str], started: list[int], supervised: list[int], monkeypatch
    ) -> None:
        """Two engines on one agent home would race over the same sessions and the
        same store, so a second `web` attaches instead."""
        monkeypatch.setattr(serve_commands, "_attached_url", lambda: "http://127.0.0.1:31337/auth#abc")

        serve_commands._web(port=18999)

        assert opened == ["http://127.0.0.1:31337/auth#abc"]
        assert started == [] and supervised == [], "it started a second gateway instead of attaching"

    def test_nothing_to_attach_to_leaves_a_resident_gateway(
        self,
        home: Path,
        a_built_page,
        opened: list[str],
        started: list[int],
        foregrounded: list[int],
        supervised: list[int],
        monkeypatch,
    ) -> None:
        """The page outlives the terminal that opened it, so the engine behind it
        must too: `web` leaves a supervised gateway rather than holding the
        terminal itself."""
        monkeypatch.setattr(serve_commands, "_attached_url", lambda: None)
        monkeypatch.setattr(serve_commands, "_await_attach", lambda *_a, **_k: "http://127.0.0.1:18999/auth#z")

        serve_commands._web(port=18999)

        assert supervised == [18999]
        assert started == [] and foregrounded == [], "it held the terminal instead of leaving a resident gateway"
        assert opened == ["http://127.0.0.1:18999/auth#z"]

    def test_foreground_holds_the_terminal_and_supervises_nothing(
        self,
        home: Path,
        a_built_page,
        opened: list[str],
        started: list[int],
        foregrounded: list[int],
        supervised: list[int],
        monkeypatch,
    ) -> None:
        """The point of --foreground is Ctrl-C meaning what it says, which a
        supervisor would undo by restarting what you just stopped.

        And it launches the same child the supervisor does. Building the
        standalone stack in this process left --foreground on the one engine with
        no ChannelManager -- the inert entrances page -- with the fix applying
        only to the detached path, which is not the one anybody debugging uses.
        """
        monkeypatch.setattr(serve_commands, "_attached_url", lambda: None)

        serve_commands._web(port=18999, foreground=True)

        assert foregrounded == [18999]
        assert started == [], "it built a serve stack in this process instead of launching the engine"
        assert supervised == []
        assert opened == [], "the engine it starts opens the browser itself"

    def test_it_waits_for_a_supervisor_that_is_between_restarts(
        self, home: Path, a_built_page, opened: list[str], supervised: list[int], monkeypatch
    ) -> None:
        """A live supervisor with no gateway answering yet is a restart in flight.
        Starting a second one would leave two racing for the same port."""
        import os

        home.mkdir(parents=True, exist_ok=True)
        (home / "web.json").write_text(json.dumps({"pid": os.getpid(), "port": 18999}), encoding="utf-8")
        monkeypatch.setattr(serve_commands, "_attached_url", lambda: None)
        monkeypatch.setattr(serve_commands, "_await_attach", lambda *_a, **_k: "http://127.0.0.1:18999/auth#z")

        serve_commands._web(port=18999)

        assert supervised == [], "it started a second supervisor beside a live one"
        assert opened == ["http://127.0.0.1:18999/auth#z"]

    def test_a_dead_supervisor_s_leftover_file_is_a_first_launch(
        self, home: Path, a_built_page, opened: list[str], supervised: list[int], monkeypatch
    ) -> None:
        """web.json is removed on a clean exit only, so a killed supervisor leaves
        one behind. Reading it as live would leave the page with no engine at all."""
        home.mkdir(parents=True, exist_ok=True)
        (home / "web.json").write_text(json.dumps({"pid": 999999, "port": 18999}), encoding="utf-8")
        monkeypatch.setattr(serve_commands, "_attached_url", lambda: None)
        monkeypatch.setattr(serve_commands, "_await_attach", lambda *_a, **_k: "http://127.0.0.1:18999/auth#z")

        serve_commands._web(port=18999)

        assert supervised == [18999]
        assert opened == ["http://127.0.0.1:18999/auth#z"]

    def test_a_gateway_that_never_comes_up_is_reported_not_opened(
        self, home: Path, a_built_page, opened: list[str], supervised: list[int], monkeypatch
    ) -> None:
        """An empty tab pointed at a dead port is the worst outcome available."""
        monkeypatch.setattr(serve_commands, "_attached_url", lambda: None)
        monkeypatch.setattr(serve_commands, "_await_attach", lambda *_a, **_k: None)

        with pytest.raises(typer.Exit) as exit_info:
            serve_commands._web(port=18999)

        assert exit_info.value.exit_code == 1
        assert opened == []

    def test_a_token_mismatch_stops_rather_than_doubling_up(
        self, home: Path, a_built_page, opened: list[str], started: list[int], supervised: list[int], monkeypatch
    ) -> None:
        def _refused() -> str:
            raise PermissionError("the gateway on port 31337 refused the recorded token")

        monkeypatch.setattr(serve_commands, "_attached_url", _refused)

        with pytest.raises(typer.Exit) as exit_info:
            serve_commands._web(port=18999)

        assert exit_info.value.exit_code == 1
        assert started == [] and opened == [] and supervised == []

    def test_no_page_built_refuses_instead_of_opening_the_placeholder(
        self, two_candidates, home: Path, opened: list[str], started: list[int], supervised: list[int]
    ) -> None:
        """`serve` is still useful with no page -- the WebSocket is the point of it
        -- but the page is the whole point of `web`."""
        with pytest.raises(typer.Exit) as exit_info:
            serve_commands._web(port=18999)

        assert exit_info.value.exit_code == 1
        assert started == [] and supervised == [] and opened == []


class _FakeProc:
    """A gateway run that ends with ``code``, without running one."""

    def __init__(self, pid: int, code: int, *, port: int | None = None, state: Path | None = None) -> None:
        self.pid, self._code = pid, code
        # Written the way a real gateway writes it, so the supervisor's
        # port-learning reads a file rather than a stub.
        if port is not None and state is not None:
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text(json.dumps({"port": port, "token": "t", "pid": pid}), encoding="utf-8")
        self._alive = port is not None

    def poll(self):
        if self._alive:
            self._alive = False
            return None
        return self._code

    def wait(self) -> int:
        return self._code


@pytest.fixture
def instant(monkeypatch):
    """No real waiting, so a backoff schedule is a list rather than a minute."""
    import time

    slept: list[float] = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    return slept


class TestTheSupervisor:
    """What brings the gateway back, and what it refuses to bring back."""

    def _run(self, monkeypatch, codes: list[int], **kw) -> list[list[str]]:
        """Supervise a gateway whose successive runs exit with ``codes``."""
        import subprocess

        argv_seen: list[list[str]] = []
        remaining = list(codes)

        def _popen(argv, *_a, **_k):
            argv_seen.append(list(argv))
            code = remaining.pop(0) if remaining else 0
            return _FakeProc(4242 + len(argv_seen), code, **kw)

        monkeypatch.setattr(subprocess, "Popen", _popen)
        return argv_seen

    def test_a_clean_exit_is_a_decision_not_a_fault(self, home: Path, instant, monkeypatch) -> None:
        """`system.upgrade` stops the gateway on purpose after spawning its
        replacement, and so does --stop. Restarting there fights the caller."""
        argv = self._run(monkeypatch, [0])

        serve_commands._supervise(18999)

        assert len(argv) == 1, "it restarted a gateway that stopped on purpose"

    def test_a_crash_is_undone(self, home: Path, instant, monkeypatch) -> None:
        argv = self._run(monkeypatch, [1, 1, 0])

        serve_commands._supervise(18999)

        assert len(argv) == 3
        assert instant[:2] == [2.0, 4.0], f"backoff did not climb: {instant}"

    def test_a_gateway_that_never_stays_up_is_given_up_on(self, home: Path, instant, monkeypatch) -> None:
        """A port it cannot bind or a config it cannot load is not fixed by trying
        again, and a process respawning forever is worse than one that said why."""
        argv = self._run(monkeypatch, [1] * 40)

        serve_commands._supervise(18999)

        assert len(argv) == serve_commands._CRASH_LOOP_GIVE_UP

    def test_a_run_that_worked_resets_the_backoff(self, home: Path, instant, monkeypatch) -> None:
        """Otherwise a gateway restarted once an hour would eventually be waiting
        the ceiling before coming back, and would hit the give-up count."""
        import time

        clock = iter(range(0, 100_000, int(serve_commands._HEALTHY_RUN_S) + 1))
        monkeypatch.setattr(time, "monotonic", lambda: float(next(clock)))
        argv = self._run(monkeypatch, [1] * 10)

        serve_commands._supervise(18999)

        assert len(argv) == 10 + 1, "a long-lived run was counted as a crash loop"
        assert set(instant) == {1.0}, f"backoff climbed across healthy runs: {instant}"

    def test_it_restarts_on_the_port_the_gateway_actually_bound(self, home: Path, instant, monkeypatch) -> None:
        """The first launch probes forward past whatever holds 18792, and the open
        tab is pointed at what it landed on -- not at what it asked for."""
        argv = self._run(monkeypatch, [1, 0], port=19100, state=home / "serve.json")

        serve_commands._supervise(18999)

        assert argv[0][-1] == "18999"
        assert argv[1][-1] == "19100", f"the restart aimed at the wrong port: {argv[1]}"

    def test_it_records_itself_and_then_stops_claiming_to_be_running(self, home: Path, instant, monkeypatch) -> None:
        """`--stop` needs a pid to signal, and a pid left behind after the
        supervisor is gone would make a later `web` wait for a restart that is
        never coming."""
        seen: list[bool] = []

        import subprocess

        def _popen(argv, *_a, **_k):
            seen.append((home / "web.json").exists())
            return _FakeProc(4242, 0)

        monkeypatch.setattr(subprocess, "Popen", _popen)

        serve_commands._supervise(18999)

        assert seen == [True], "it did not record itself before running the gateway"
        assert not (home / "web.json").exists(), "it left a pid behind that nothing is listening on"

    def test_the_gateway_is_run_through_the_interpreter_not_a_shim(self, monkeypatch) -> None:
        """The supervisor outlives the working directory it was started from, and
        `raven` on PATH may be a relative path or a shim that only resolved there."""
        import sys

        argv = serve_commands._gateway_argv(18999)

        assert argv[:3] == [sys.executable, "-m", "raven"]

    def test_it_supervises_the_engine_that_has_the_channel_adapters(self) -> None:
        """`serve` builds no ChannelManager, so a page behind it can never start an
        adapter: no sign-in code is ever minted and every enabled entrance reads
        "state unknown" for good. The gateway is the process that runs them, and it
        serves the page on its own loop, so it is what the page's supervisor runs.

        The port travels as ``--page-port``: the gateway's own ``--port`` is its
        health endpoint, and passing the tab's port there would leave the page
        wherever config happened to put it.
        """
        argv = self.argv_with_lock(None, 18999)

        assert argv[3] == "gateway"
        assert "--page-port" in argv
        assert argv[argv.index("--page-port") + 1] == "18999"
        assert "serve" not in argv

    def argv_with_lock(self, holder: object, port: int, monkeypatch=None) -> list[str]:
        """The page child chosen while `holder` answers for the gateway lock."""
        from unittest.mock import patch

        with patch.object(serve_commands, "_gateway_holds_the_lock", lambda: holder is not None):
            return serve_commands._gateway_argv(port)

    def test_the_foreground_run_launches_that_same_child(self, monkeypatch, home: Path, opened: list[str]) -> None:
        """And it keeps foreground semantics while doing it: the child's output is
        this terminal's, so stdio is inherited rather than piped, and the browser
        is opened once the child has bound a port and written it down."""
        import subprocess

        launched: list[dict] = []

        class _Child:
            returncode = 0

            def wait(self) -> int:
                return 0

        def _fake_popen(argv, **kwargs):
            launched.append({"argv": argv, "kwargs": kwargs})
            return _Child()

        monkeypatch.setattr(subprocess, "Popen", _fake_popen)
        monkeypatch.setattr(serve_commands, "_refuse_incomplete_install", lambda: None)
        monkeypatch.setattr(serve_commands, "_gateway_argv", lambda port: ["engine", str(port)])
        monkeypatch.setattr(serve_commands, "_await_attach", lambda *_a, **_k: "http://127.0.0.1:18999/auth#z")

        serve_commands._run_foreground(18999)

        assert [c["argv"] for c in launched] == [["engine", "18999"]]
        # Inherited, not captured: piping it would send the engine's own output
        # somewhere nobody is reading, which is the whole point of --foreground.
        assert "stdout" not in launched[0]["kwargs"] and "stderr" not in launched[0]["kwargs"]
        assert opened == ["http://127.0.0.1:18999/auth#z"]

    def test_the_wait_ends_when_the_foreground_child_dies(self, home: Path, monkeypatch) -> None:
        """The child can fail before it writes serve.json -- a lost lock race, a
        config it cannot load, an import that fails. Watching `web.json` cannot
        see that: nothing supervises a foreground run, so the wait sat out its
        whole 150s ceiling with the error already on screen.
        """
        import time

        monkeypatch.setattr(serve_commands, "_read_serve_state", lambda: None)

        began = time.monotonic()
        url = serve_commands._await_attach(timeout_s=30.0, gone=lambda: True)
        took = time.monotonic() - began

        assert url is None
        assert took < 2.0, f"it waited {took:.1f}s on a child that was already gone"

    def test_the_foreground_run_propagates_a_child_that_failed_at_once(
        self, home: Path, monkeypatch, opened: list[str]
    ) -> None:
        """What the reader gets for it: the exit code, now rather than in two and
        a half minutes, and no claim that the engine did not come up on top of
        the child's own error."""
        import subprocess
        import time

        class _DeadChild:
            returncode = 3

            def poll(self) -> int:
                return 3

            def wait(self) -> int:
                return 3

        said: list[str] = []
        monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: _DeadChild())
        monkeypatch.setattr(serve_commands, "_refuse_incomplete_install", lambda: None)
        monkeypatch.setattr(serve_commands, "_gateway_argv", lambda port: ["engine", str(port)])
        monkeypatch.setattr(serve_commands, "_read_serve_state", lambda: None)
        monkeypatch.setattr(serve_commands.typer, "echo", lambda message="", **_k: said.append(str(message)))

        began = time.monotonic()
        with pytest.raises(typer.Exit) as exit_info:
            serve_commands._run_foreground(18999)
        took = time.monotonic() - began

        assert exit_info.value.exit_code == 3
        assert took < 2.0, f"it waited {took:.1f}s on a child that had already exited"
        assert opened == []
        # The child has already said why on this same terminal. Adding "the
        # engine did not come up" over its traceback is this function guessing
        # out loud about something it can see the answer to.
        assert not [line for line in said if "did not come up" in line], said

    def test_it_falls_back_to_serve_where_a_gateway_already_holds_the_lock(self) -> None:
        """`gateway.page.enabled = false` is a supported way to run: channels in
        the gateway, page separate. A second gateway cannot start there -- it
        exits on the lock -- and a supervisor would spend its whole crash budget
        finding that out, leaving no page at all. Standalone serve is the only
        child that CAN run, and it is not a downgrade: the page reaches the
        incumbent's adapters over `gateway.live_probe`, which finds it through
        that same lock.
        """
        argv = self.argv_with_lock(object(), 18999)

        assert argv[3] == "serve"
        assert argv[argv.index("--port") + 1] == "18999"
        assert "gateway" not in argv

    def test_a_lock_it_cannot_read_is_treated_as_held(self, monkeypatch) -> None:
        """The two mistakes are not equal. Guessing "free" when it is held opens
        no page at all; guessing "held" when it is free opens the page on an
        engine without channels, which is where this surface already was and is
        one `raven gateway` away from right.
        """
        from raven.gateway import lock as _gateway_lock

        def _explode(now: float) -> None:
            raise OSError("lock unreadable")

        monkeypatch.setattr(_gateway_lock, "read_status", _explode)

        assert serve_commands._gateway_holds_the_lock() is True


class TestStopping:
    def test_it_stops_the_supervisor_before_the_gateway(self, home: Path, monkeypatch) -> None:
        """The other order only proves the supervisor works: it would restart the
        gateway between the two signals."""
        import os
        import signal
        import time

        home.mkdir(parents=True, exist_ok=True)
        (home / "web.json").write_text(json.dumps({"pid": 111, "port": 18999}), encoding="utf-8")
        (home / "serve.json").write_text(json.dumps({"port": 18999, "token": "t", "pid": 222}), encoding="utf-8")
        signalled: list[tuple[int, int]] = []
        monkeypatch.setattr(os, "kill", lambda pid, sig: signalled.append((pid, sig)))
        # Separately from os.kill, which the liveness probe also uses: leaving that
        # to the fake would record the probe's own signal-0 as a stop.
        monkeypatch.setattr(serve_commands, "_pid_alive", lambda _pid: True)
        monkeypatch.setattr(time, "sleep", lambda _s: None)

        assert serve_commands._stop_resident() is True
        assert signalled == [(111, signal.SIGTERM), (222, signal.SIGTERM)]

    def test_nothing_running_is_not_an_error(self, home: Path) -> None:
        assert serve_commands._stop_resident() is False


def test_web_is_registered_as_its_own_command() -> None:
    """A verb nobody can type is not a verb."""
    app = typer.Typer()
    serve_commands.register(app)

    assert {c.name for c in app.registered_commands} == {"serve", "web"}


class TestTheSessionSurvivesARestart:
    """A restart must not sign out the tabs that are open.

    `raven web` runs a supervisor whose whole job is restarting the gateway, and
    the gateway used to mint a fresh session cookie on every start -- so the
    supervisor doing its job logged the user out. The tab showed "not
    authenticated" against a gateway that was up and healthy, which is the one
    reading that sends a person to restart a service that is already running.
    """

    @pytest.fixture
    def home(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(serve_commands, "_state_path", lambda: tmp_path / "serve.json")
        monkeypatch.delenv("RAVEN_SERVE_COOKIE", raising=False)
        return tmp_path

    class _Gateway:
        def __init__(self, cookie: str) -> None:
            self.session_cookie = cookie

    def test_the_cookie_is_written_and_read_back(self, home: Path) -> None:
        serve_commands._write_serve_state(18792, "tok-1", "cookie-1")

        gw = self._Gateway("freshly-minted")
        serve_commands.adopt_stored_cookie(gw)

        assert gw.session_cookie == "cookie-1"

    def test_the_env_override_still_wins(self, home: Path, monkeypatch) -> None:
        """`system.upgrade` hands the exact session to its replacement this way,
        so a stored value must not overrule it."""
        serve_commands._write_serve_state(18792, "tok-1", "cookie-1")
        monkeypatch.setenv("RAVEN_SERVE_COOKIE", "from-env")

        gw = self._Gateway("from-env")
        serve_commands.adopt_stored_cookie(gw)

        assert gw.session_cookie == "from-env"

    def test_a_first_launch_keeps_the_freshly_minted_one(self, home: Path) -> None:
        gw = self._Gateway("freshly-minted")
        serve_commands.adopt_stored_cookie(gw)
        assert gw.session_cookie == "freshly-minted"

    @pytest.mark.parametrize("body", ["not json at all", '{"port": 1}', '{"cookie": ""}', '{"cookie": 5}'])
    def test_an_unusable_state_file_is_not_adopted(self, home: Path, body: str) -> None:
        """Anything unreadable means "no previous session", never a crash on the
        startup path -- a gateway that will not boot is worse than a sign-in."""
        (home / "serve.json").write_text(body, encoding="utf-8")

        gw = self._Gateway("freshly-minted")
        serve_commands.adopt_stored_cookie(gw)

        assert gw.session_cookie == "freshly-minted"

    def test_the_state_file_stays_owner_only(self, home: Path) -> None:
        """It now carries two credentials rather than one."""
        path = serve_commands._write_serve_state(18792, "tok-1", "cookie-1")
        assert path is not None
        assert path.stat().st_mode & 0o777 == 0o600
        assert json.loads(path.read_text(encoding="utf-8"))["cookie"] == "cookie-1"


class TestAFirstRunIsGivenTimeToCompile:
    """A fresh install byte-compiles its whole dependency set before the gateway
    can bind. Measured at 28 seconds against a window that used to be 25, which
    told the first-run reader that a gateway seconds from listening had failed
    -- and left them a supervisor they did not know was running."""

    @pytest.fixture
    def poll(self, monkeypatch):
        """A clock that only moves when the poll loop sleeps.

        ``_await_attach`` imports ``time`` inside itself, so patching the real
        module is what reaches it -- and it keeps the test off the wall clock,
        which a 150-second ceiling would otherwise make unrunnable.
        """
        import time as _time

        now = [0.0]
        monkeypatch.setattr(_time, "monotonic", lambda: now[0])
        monkeypatch.setattr(_time, "sleep", lambda s: now.__setitem__(0, now[0] + s))
        return now

    def test_a_gateway_that_binds_after_the_old_window_still_attaches(self, poll, monkeypatch) -> None:
        monkeypatch.setattr(serve_commands, "_read_web_state", lambda: 4242)
        monkeypatch.setattr(
            serve_commands,
            "_read_serve_state",
            lambda: (18792, "tok") if poll[0] >= 28.0 else None,
        )
        # Stubbed as a plain call so the loop never builds a real coroutine
        # for asyncio.run to leave unawaited.
        monkeypatch.setattr(serve_commands, "_attach", lambda *_a: "coro")
        monkeypatch.setattr(serve_commands.asyncio, "run", lambda coro: "http://127.0.0.1:18792/auth#n")

        assert serve_commands._await_attach() == "http://127.0.0.1:18792/auth#n"
        assert poll[0] >= 28.0, "it answered before the gateway could have bound"

    def test_a_dead_supervisor_ends_the_wait_instead_of_burning_the_ceiling(self, poll, monkeypatch) -> None:
        # Nothing is coming: a process that already exited will not start a
        # gateway by being waited on, so the reader hears about it now.
        seen = {"n": 0}

        def _web_state():
            seen["n"] += 1
            return 4242 if seen["n"] < 3 else None

        monkeypatch.setattr(serve_commands, "_read_web_state", _web_state)
        monkeypatch.setattr(serve_commands, "_read_serve_state", lambda: None)

        assert serve_commands._await_attach() is None
        assert poll[0] < serve_commands._ATTACH_PATIENCE_S, "it waited out the full ceiling for a dead supervisor"

    def test_a_supervisor_that_has_not_recorded_itself_yet_is_not_read_as_dead(self, poll, monkeypatch) -> None:
        # The supervisor writes its own pid after it starts, so its absence in
        # the first instants means "not yet", not "never".
        monkeypatch.setattr(serve_commands, "_read_web_state", lambda: 4242 if poll[0] >= 5.0 else None)
        monkeypatch.setattr(
            serve_commands,
            "_read_serve_state",
            lambda: (18792, "tok") if poll[0] >= 9.0 else None,
        )
        monkeypatch.setattr(serve_commands, "_attach", lambda *_a: "coro")
        monkeypatch.setattr(serve_commands.asyncio, "run", lambda coro: "http://127.0.0.1:18792/auth#n")

        assert serve_commands._await_attach() == "http://127.0.0.1:18792/auth#n"


class TestAResidentGatewayAnnouncesUpdatesItself:
    """The page learns about updates from system.version, asked once at boot --
    correct for a process that restarts, invisible for one that stays up for
    days. The announcer pushes the same fact over the socket instead."""

    @pytest.fixture
    def fast_clock(self, monkeypatch):
        """Collapse the poll delays so the loop runs its ticks immediately."""
        monkeypatch.setattr(serve_commands, "_UPDATE_FIRST_CHECK_S", 0.001)
        monkeypatch.setattr(serve_commands, "_UPDATE_POLL_BETA_S", 0.001)
        monkeypatch.setattr(serve_commands, "_UPDATE_POLL_STABLE_S", 0.001)
        monkeypatch.setattr(serve_commands, "_UPDATE_BACKOFF_CEILING_S", 0.001)

    async def test_a_new_version_is_broadcast_once_not_every_poll(self, fast_clock, monkeypatch) -> None:
        frames: list[dict] = []
        checks = {"n": 0}

        def _check(current):
            checks["n"] += 1
            return update_notice.Checked("0.1.12b9", reached=True)

        from raven.cli import update_notice

        monkeypatch.setattr(update_notice, "check_now", _check)

        async def _broadcast(frame):
            frames.append(frame)

        stop = serve_commands.asyncio.Event()

        async def _bounded():
            task = serve_commands.asyncio.get_event_loop().create_task(
                serve_commands._announce_updates(_broadcast, stop)
            )
            while checks["n"] < 4:
                await serve_commands.asyncio.sleep(0.002)
            stop.set()
            await task

        await serve_commands.asyncio.wait_for(_bounded(), timeout=5)

        assert checks["n"] >= 4, "the loop stopped polling"
        assert frames == [{"method": "system.update_available", "params": {"latest_version": "0.1.12b9"}}], (
            "one version must be announced exactly once"
        )

    async def test_nothing_newer_means_silence(self, fast_clock, monkeypatch) -> None:
        frames: list[dict] = []
        checks = {"n": 0}

        def _check(current):
            checks["n"] += 1
            return update_notice.Checked(None, reached=True)

        from raven.cli import update_notice

        monkeypatch.setattr(update_notice, "check_now", _check)

        async def _broadcast(frame):
            frames.append(frame)

        stop = serve_commands.asyncio.Event()
        task = serve_commands.asyncio.get_event_loop().create_task(serve_commands._announce_updates(_broadcast, stop))
        while checks["n"] < 3:
            await serve_commands.asyncio.sleep(0.002)
        stop.set()
        await serve_commands.asyncio.wait_for(task, timeout=5)

        assert frames == []

    async def test_a_check_that_blows_up_does_not_kill_the_loop(self, fast_clock, monkeypatch) -> None:
        checks = {"n": 0}

        def _check(current):
            checks["n"] += 1
            raise RuntimeError("offline")

        from raven.cli import update_notice

        monkeypatch.setattr(update_notice, "check_now", _check)

        async def _broadcast(frame):
            pass

        stop = serve_commands.asyncio.Event()
        task = serve_commands.asyncio.get_event_loop().create_task(serve_commands._announce_updates(_broadcast, stop))
        while checks["n"] < 3:
            await serve_commands.asyncio.sleep(0.002)
        stop.set()
        await serve_commands.asyncio.wait_for(task, timeout=5)

        assert checks["n"] >= 3, "one failed fetch ended the announcer"


class TestThePollFollowsTheChannelItIsWatching:
    """A beta reaches its testers by this poll and no other route, so it runs on
    a minute. Stable does not: those releases land days apart and the check reads
    GitHub's release page rather than our own registry, where a minute from every
    install behind one egress would buy nothing."""

    @pytest.fixture
    def channel(self, monkeypatch):
        """Put the install on, or off, the beta channel."""
        from raven.cli import beta_channel

        def _set(active: bool) -> None:
            monkeypatch.setattr(beta_channel, "is_active", lambda: active)

        return _set

    def test_the_beta_channel_is_watched_every_minute(self, channel) -> None:
        channel(True)
        assert serve_commands._update_poll_seconds() == 60.0

    def test_stable_keeps_its_half_hour(self, channel) -> None:
        channel(False)
        assert serve_commands._update_poll_seconds() == 30 * 60.0

    def test_the_cadence_is_read_per_tick_not_at_startup(self, channel) -> None:
        """Joining the channel is a file appearing; a resident gateway that
        outlives that should follow it without a restart."""
        channel(False)
        assert serve_commands._update_poll_seconds() == 30 * 60.0
        channel(True)
        assert serve_commands._update_poll_seconds() == 60.0

    def test_failures_double_the_wait_up_to_a_half_hour_ceiling(self, channel) -> None:
        channel(True)
        grown = [serve_commands._update_delay(n) for n in range(7)]

        assert grown[:5] == [60.0, 120.0, 240.0, 480.0, 960.0]
        assert grown[5] == 1800.0, "the ceiling is where an offline gateway settles"
        assert grown[6] == 1800.0, "and it stops there"

    def test_stable_is_already_at_the_ceiling_so_it_never_grows(self, channel) -> None:
        channel(False)
        assert serve_commands._update_delay(0) == serve_commands._update_delay(4) == 30 * 60.0

    async def test_consecutive_failures_back_off_and_one_answer_resets(self, monkeypatch) -> None:
        """The loop's own bookkeeping, read off what it asks to wait for next."""
        from raven.cli import update_notice

        monkeypatch.setattr(serve_commands, "_UPDATE_FIRST_CHECK_S", 0.001)
        asked: list[int] = []
        monkeypatch.setattr(serve_commands, "_update_delay", lambda failures: asked.append(failures) or 0.001)

        answers = [
            update_notice.Checked(None, reached=False),
            update_notice.Checked(None, reached=False),
            update_notice.Checked(None, reached=True),
            update_notice.Checked(None, reached=False),
        ]
        checks = {"n": 0}

        def _check(current):
            checks["n"] += 1
            if not answers:
                return update_notice.Checked(None, reached=True)
            return answers.pop(0)

        monkeypatch.setattr(update_notice, "check_now", _check)

        async def _broadcast(frame):
            pass

        stop = serve_commands.asyncio.Event()
        task = serve_commands.asyncio.get_event_loop().create_task(serve_commands._announce_updates(_broadcast, stop))
        while checks["n"] < 4:
            await serve_commands.asyncio.sleep(0.002)
        stop.set()
        await serve_commands.asyncio.wait_for(task, timeout=5)

        assert asked[:4] == [1, 2, 0, 1], f"backoff did not grow then reset: {asked[:4]}"

    async def test_a_check_that_raises_counts_as_a_failure(self, monkeypatch) -> None:
        """An exception out of the check is the offline case arriving by a second
        route, so it has to slow the loop down the same way a reported one does."""
        from raven.cli import update_notice

        monkeypatch.setattr(serve_commands, "_UPDATE_FIRST_CHECK_S", 0.001)
        asked: list[int] = []
        monkeypatch.setattr(serve_commands, "_update_delay", lambda failures: asked.append(failures) or 0.001)

        checks = {"n": 0}

        def _check(current):
            checks["n"] += 1
            raise RuntimeError("offline")

        monkeypatch.setattr(update_notice, "check_now", _check)

        async def _broadcast(frame):
            pass

        stop = serve_commands.asyncio.Event()
        task = serve_commands.asyncio.get_event_loop().create_task(serve_commands._announce_updates(_broadcast, stop))
        while checks["n"] < 3:
            await serve_commands.asyncio.sleep(0.002)
        stop.set()
        await serve_commands.asyncio.wait_for(task, timeout=5)

        assert asked[:3] == [1, 2, 3]


class TestTheSupervisorCleansUpOnTheSignalThatStopsIt:
    """`raven web --stop` sends SIGTERM. Python's default disposition kills the
    process where it stands, so the `finally` that removes `web.json` never ran
    and the file was left behind pointing at a dead pid."""

    def test_sigterm_removes_the_state_file(self, tmp_path, monkeypatch) -> None:
        import os
        import subprocess
        import sys
        import time

        home = tmp_path / "home"
        home.mkdir()
        state = home / "web.json"
        script = (
            "from raven.cli import serve_commands as s\n"
            "s._gateway_argv = lambda p: ['sleep', '600']\n"
            "s._bound_port_of = lambda p: None\n"
            "s._supervise(18999)\n"
        )
        env = {**os.environ, "RAVEN_HOME": str(home)}
        proc = subprocess.Popen([sys.executable, "-c", script], env=env)  # noqa: S603
        try:
            deadline = time.monotonic() + 15
            while not state.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert state.exists(), "the supervisor never recorded itself"
            proc.terminate()
            proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

        deadline = time.monotonic() + 5
        while state.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not state.exists(), "SIGTERM left web.json behind on a dead pid"

    def test_sigterm_during_the_prologue_still_removes_the_state_file(self, tmp_path) -> None:
        """The signal that arrives before the loop does.

        The test above sends SIGTERM once the file exists, which is a real
        moment but a fleeting one: the supervisor is past it within microseconds,
        so a regression here shows up as a rare red pipeline rather than a
        failure. This holds the process in that moment on purpose -- the state
        write is wrapped so it takes three seconds to return -- and terminates
        it there. Then a handler armed outside the guard fails every run instead
        of one in several hundred.
        """
        import os
        import subprocess
        import sys
        import time

        home = tmp_path / "home"
        home.mkdir()
        state = home / "web.json"
        script = (
            "import time\n"
            "from raven.cli import serve_commands as s\n"
            "s._gateway_argv = lambda p: ['sleep', '600']\n"
            "s._bound_port_of = lambda p: None\n"
            "_recorded = s._write_web_state\n"
            "def _linger(port):\n"
            "    _recorded(port)\n"
            "    time.sleep(3)\n"
            "s._write_web_state = _linger\n"
            "s._supervise(18999)\n"
        )
        env = {**os.environ, "RAVEN_HOME": str(home)}
        proc = subprocess.Popen([sys.executable, "-c", script], env=env)  # noqa: S603
        try:
            deadline = time.monotonic() + 15
            while not state.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert state.exists(), "the supervisor never recorded itself"
            # Still inside the lingering write, which is the window.
            proc.terminate()
            proc.wait(timeout=15)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

        deadline = time.monotonic() + 5
        while state.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert not state.exists(), "a SIGTERM before the loop left web.json on a dead pid"


# ---------------------------------------------------------------------------
# `raven serve` -- attach to a gateway that already hosts the page, instead of
# starting a second engine on the same agent home.
# ---------------------------------------------------------------------------


def _record_hosted(home: Path, *, pid: int, port: int = 18792, token: str = "tok") -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "serve.json").write_text(json.dumps({"port": port, "token": token, "pid": pid}), encoding="utf-8")


class TestAGatewayHostedPage:
    """The probe: a page is "gateway-hosted" only when the lock's pid and
    serve.json's pid are the same process."""

    def _lock_says(self, monkeypatch, pid: int | None) -> None:
        from raven.gateway import lock as _gateway_lock

        info = None if pid is None else _gateway_lock.LockInfo(pid=pid, started_at=0.0, config_path="")
        monkeypatch.setattr(_gateway_lock, "read_status", lambda now: info)

    def test_matching_pids_name_the_hosted_page(self, home: Path, monkeypatch) -> None:
        _record_hosted(home, pid=4321)
        self._lock_says(monkeypatch, 4321)
        assert serve_commands._gateway_hosted_page() == (18792, "tok")

    def test_a_stale_serve_json_is_not_the_gateway_s(self, home: Path, monkeypatch) -> None:
        """A serve.json left by a killed standalone serve must not make a
        page-less gateway read as "already hosting" -- starting is correct."""
        _record_hosted(home, pid=4321)
        self._lock_says(monkeypatch, 9999)
        assert serve_commands._gateway_hosted_page() is None

    def test_no_gateway_running_reads_as_nothing_hosted(self, home: Path, monkeypatch) -> None:
        _record_hosted(home, pid=4321)
        self._lock_says(monkeypatch, None)
        assert serve_commands._gateway_hosted_page() is None


class TestServeAgainstAHostedPage:
    """The command: attach and stop, or fall through to a normal start."""

    def _invoke(self, args: list[str] | None = None):
        from typer.testing import CliRunner

        from raven.cli.commands import app

        return CliRunner().invoke(app, ["serve", *(args or [])])

    def test_a_live_hosted_page_is_attached_not_doubled(self, monkeypatch, started, opened) -> None:
        monkeypatch.setattr(serve_commands, "_gateway_hosted_page", lambda: (18792, "tok"))

        async def _minted(port: int, token: str):
            return f"http://127.0.0.1:{port}/auth#nonce"

        monkeypatch.setattr(serve_commands, "_attach", _minted)
        result = self._invoke()
        assert result.exit_code == 0
        assert "already hosts the page" in result.output
        assert started == []

    def test_open_opens_the_hosted_page(self, monkeypatch, started, opened) -> None:
        monkeypatch.setattr(serve_commands, "_gateway_hosted_page", lambda: (18792, "tok"))

        async def _minted(port: int, token: str):
            return f"http://127.0.0.1:{port}/auth#nonce"

        monkeypatch.setattr(serve_commands, "_attach", _minted)
        result = self._invoke(["--open"])
        assert result.exit_code == 0
        assert opened == ["http://127.0.0.1:18792/auth#nonce"]
        assert started == []

    def test_an_unreachable_hosted_record_starts_normally(self, monkeypatch, started) -> None:
        """The gateway is recorded but its page does not answer: nothing to
        attach to, so standalone serve starts exactly as before."""
        monkeypatch.setattr(serve_commands, "_gateway_hosted_page", lambda: (18792, "tok"))

        async def _gone(port: int, token: str):
            return None

        monkeypatch.setattr(serve_commands, "_attach", _gone)
        result = self._invoke()
        assert result.exit_code == 0
        assert started == [18792]

    def test_a_refused_token_is_reported_not_worked_around(self, monkeypatch, started) -> None:
        monkeypatch.setattr(serve_commands, "_gateway_hosted_page", lambda: (18792, "tok"))

        async def _refused(port: int, token: str):
            raise PermissionError(f"the gateway on port {port} refused the recorded token")

        monkeypatch.setattr(serve_commands, "_attach", _refused)
        result = self._invoke()
        assert result.exit_code == 1
        assert started == []

    def test_nothing_hosted_starts_normally(self, monkeypatch, started) -> None:
        monkeypatch.setattr(serve_commands, "_gateway_hosted_page", lambda: None)
        result = self._invoke()
        assert result.exit_code == 0
        assert started == [18792]


class TestRefusingAnIncompleteInstall:
    """`system.upgrade` makes serve exit on purpose, so a supervisor respawns it
    straight into the window where uv has deleted the old environment and not
    yet written the new one. A serve that starts there binds the port and
    answers `/` with the placeholder for the rest of its life, because
    `build_app` picks the page route once."""

    def test_a_sound_install_is_let_through(self, monkeypatch) -> None:
        from raven.cli import _install_guard

        monkeypatch.setattr(_install_guard, "inspect_install", lambda: None)
        serve_commands._refuse_incomplete_install()

    def test_a_half_written_install_refuses_before_the_port_is_bound(self, monkeypatch) -> None:
        from raven.cli import _install_guard

        def unreachable(*_args, **_kwargs):
            raise AssertionError("the gateway must not start on a half-written installation")

        monkeypatch.setattr(
            _install_guard,
            "inspect_install",
            lambda: _install_guard.InstallFault("incomplete", "this installation is missing the packaged page"),
        )
        monkeypatch.setattr(serve_commands, "_serve_main", unreachable)

        with pytest.raises(typer.Exit) as excinfo:
            serve_commands._run(18999, False)

        assert excinfo.value.exit_code == serve_commands.INCOMPLETE_INSTALL_EXIT

    def test_it_names_the_repair_a_reader_can_run(self, monkeypatch, capsys) -> None:
        from raven.cli import _install_guard

        monkeypatch.setattr(
            _install_guard,
            "inspect_install",
            lambda: _install_guard.InstallFault("incomplete", "this installation is missing the packaged page"),
        )
        with pytest.raises(typer.Exit):
            serve_commands._refuse_incomplete_install()

        assert "install.sh" in capsys.readouterr().err

    def test_a_running_upgrade_is_waited_out_rather_than_failed_fast(self, monkeypatch) -> None:
        """`_supervise` gives up after `_CRASH_LOOP_GIVE_UP` failures faster than
        `_HEALTHY_RUN_S`, and an install window is short enough to burn through
        all five. Spending the window asleep in one process is what keeps the
        supervisor trying."""
        from raven.cli import _install_guard

        running = _install_guard.InstallFault("upgrading", "an upgrade to 9.9.9 is replacing this installation")
        verdicts = [running, running, None]
        monkeypatch.setattr(_install_guard, "inspect_install", lambda: verdicts.pop(0))
        monkeypatch.setattr(serve_commands, "_UPGRADE_POLL_S", 0.0)

        with pytest.raises(typer.Exit) as excinfo:
            serve_commands._refuse_incomplete_install()

        assert verdicts == []
        # Even a finished upgrade ends in a refusal: this process already holds
        # half of the build that was replaced, and the rest would load off disk
        # from the new one.
        assert excinfo.value.exit_code == serve_commands.INCOMPLETE_INSTALL_EXIT

    def test_it_gives_up_on_an_upgrade_that_never_ends(self, monkeypatch) -> None:
        from raven.cli import _install_guard

        fault = _install_guard.InstallFault("upgrading", "an upgrade is replacing this installation")
        monkeypatch.setattr(_install_guard, "inspect_install", lambda: fault)
        monkeypatch.setattr(serve_commands, "_UPGRADE_POLL_S", 0.0)
        monkeypatch.setattr(serve_commands, "_UPGRADE_WAIT_S", 0.05)

        with pytest.raises(typer.Exit) as excinfo:
            serve_commands._refuse_incomplete_install()

        assert excinfo.value.exit_code == serve_commands.INCOMPLETE_INSTALL_EXIT

    def test_the_refusal_does_not_read_as_a_clean_stop(self) -> None:
        """`_supervise` stops supervising on a zero exit, because that is how
        `system.upgrade` asks it to stand down. A refusal must not be mistaken
        for that, or the environment never gets started again once it is whole."""
        assert serve_commands.INCOMPLETE_INSTALL_EXIT != 0
