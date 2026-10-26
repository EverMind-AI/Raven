"""npx_fetch: an npx-run adapter is fetched before its handshake, by a process nothing here ends."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import tempfile
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.acp_client import client as acp_client
from raven.acp_client import leftovers, npx_fetch
from raven.acp_client.pool import AcpConnectionPool
from raven.acp_client.protocol import AcpConnectionError
from raven.agent.subagent.backends import env as backends_env
from raven.utils import mirrors

_STUB = Path(__file__).with_name("acp_stub_server.py")

# What npm names the npx cache entry of this spec, read off a real `npx -y` of
# it (npm 11.18.0, 2026-10-23): the entry is `<cache>/_npx/<key>`.
_SPEC = "@agentclientprotocol/claude-agent-acp@0.81.1"
_KEY = "5794dd75a801d955"

# npm 11.18.0's own words for an exact-version spec whose entry holds the
# package but no package.json, as printed by a second `npx -y` of it.
_INCOMPLETE = """npm error code ENOENT
npm error syscall open
npm error path /home/someone/.npm/_npx/5794dd75a801d955/package.json
npm error errno -2
npm error enoent Could not read package.json: Error: ENOENT: no such file or directory, open '/home/someone/.npm/_npx/5794dd75a801d955/package.json'
npm error enoent This is related to npm not being able to find a file.
npm error enoent
npm error A complete log of this run can be found in: /home/someone/.npm/_logs/2026-10-23T06_48_20_359Z-debug-0.log
"""

# Plays npm's part: a fetch (`--package=`) installs into the entry the test
# names, or fails the way npm does; anything else starts the ACP stub.
_STAND_IN = """
import json, os, sys, time

argv = sys.argv[1:]
fetching = any(word.startswith("--package=") for word in argv)
with open(os.environ["FAKE_NPX_LOG"], "a") as log:
    log.write(json.dumps({
        "argv": argv,
        "fetch": fetching,
        "sid": os.getsid(0),
        "marked": sorted(key for key in os.environ if key.startswith("RAVEN_ACP_HOST")),
    }) + "\\n")
if fetching:
    if os.environ.get("FAKE_NPX_FETCH") == "fail":
        sys.stderr.write(os.environ["FAKE_NPX_STDERR"])
        sys.exit(254)
    time.sleep(float(os.environ.get("FAKE_NPX_DELAY", "0")))
    entry = os.environ["FAKE_NPX_ENTRY"]
    os.makedirs(entry, exist_ok=True)
    with open(os.path.join(entry, "package.json"), "w") as package_json:
        package_json.write("{}")
    with open(os.environ["FAKE_NPX_LOG"], "a") as log:
        log.write(json.dumps({"finished": True}) + "\\n")
    sys.exit(0)
os.environ["ACP_STUB_MODE"] = "ok"
os.execv(sys.executable, [sys.executable, os.environ["FAKE_NPX_STUB"]])
"""

needs_posix = pytest.mark.skipif(sys.platform == "win32", reason="the stand-in npx is a POSIX shell script")


@pytest.fixture
def stand_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = tmp_path / "stand_in_npx.py"
    script.write_text(_STAND_IN)
    npx = bin_dir / "npx"
    npx.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} {shlex.quote(str(script))} "$@"\n')
    npx.chmod(0o755)
    cache = tmp_path / "npm-cache"
    entry = cache / "_npx" / _KEY
    log = tmp_path / "npx.log"
    shell = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path / "home"),
        "FAKE_NPX_LOG": str(log),
        "FAKE_NPX_ENTRY": str(entry),
        "FAKE_NPX_STUB": str(_STUB),
    }
    monkeypatch.setattr(backends_env, "login_shell_env", lambda: dict(shell))
    return SimpleNamespace(
        log=log,
        entry=entry,
        env={"npm_config_cache": str(cache)},
        cwd=str(tmp_path),
        bin_dir=bin_dir,
        home=Path(shell["HOME"]),
    )


def _calls(log: Path) -> list[dict]:
    if not log.exists():
        return []
    # The stand-in may be part-way through its next line; only a line it ended is whole.
    return [json.loads(line) for line in log.read_text().split("\n")[:-1] if line.strip()]


def _fetches(log: Path) -> list[dict]:
    return [call for call in _calls(log) if call.get("fetch")]


def _finished(log: Path) -> bool:
    return {"finished": True} in _calls(log)


def _wait_for(holds: Callable[[], object], budget: float = 15.0) -> bool:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if holds():
            return True
        time.sleep(0.05)
    return bool(holds())


def _wait_until_finished(log: Path, budget: float = 15.0) -> bool:
    return _wait_for(lambda: _finished(log), budget)


def _reaped(pid: int, budget: float = 5.0) -> bool:
    """Whether ``pid`` has left the process table, where an exited child nobody waits for stays as a zombie."""
    stat = Path(f"/proc/{pid}/stat")
    deadline = time.monotonic() + budget
    while stat.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return not stat.exists()


# ---- which launches are fetched ----------------------------------------------


@pytest.mark.parametrize(
    ("argv", "spec"),
    [
        (["npx", "-y", "@agentclientprotocol/claude-agent-acp@0.81.1"], "@agentclientprotocol/claude-agent-acp@0.81.1"),
        (["npx", "--yes", "pi-acp@0.0.33", "--verbose"], "pi-acp@0.0.33"),
        (
            ["/usr/local/bin/npx", "-y", "@agentclientprotocol/codex-acp@1.13.1"],
            "@agentclientprotocol/codex-acp@1.13.1",
        ),
        (["npx.cmd", "-y", "pi-acp@0.0.34"], "pi-acp@0.0.34"),
        (["npx", "-y", "some-adapter"], "some-adapter"),
    ],
)
def test_a_plain_npx_launch_names_the_package_it_fetches(argv: list[str], spec: str) -> None:
    assert npx_fetch.fetchable_package(argv) == spec


@pytest.mark.parametrize(
    "argv",
    [
        # An option the fetch would have to repeat to resolve the same package.
        ["npx", "-y", "--registry=https://registry.example.com", "pi-acp@0.0.33"],
        # Packages named by flag: npx names the entry after every one of them.
        ["npx", "-p", "pi-acp@0.0.33", "pi-acp"],
        # A directory or a tarball: npx names its entry after the resolved path, not the word.
        ["npx", "-y", "./adapter"],
        ["npx", "-y", "adapter.tgz"],
        ["npx", "-y", "github:someone/adapter"],
        # GitHub's shorthand, fetched from the repository rather than the registry.
        ["npx", "-y", "someone/adapter"],
        # Neither an npm name nor an exact or tagged version: a range, and words a shell would read.
        ["npx", "-y", "pi-acp@^0.0.33"],
        ["npx", "-y", "pi-acp@>=0.0.33"],
        ["npx", "-y", "pi-acp&calc"],
        ["npx", "-y"],
        ["node", "adapter.js"],
    ],
)
def test_a_launch_npx_would_resolve_differently_is_left_to_npx(argv: list[str]) -> None:
    assert npx_fetch.fetchable_package(argv) is None


def test_the_cache_key_is_the_one_npx_names_its_entry_with() -> None:
    assert npx_fetch.cache_key(_SPEC) == _KEY


# ---- the fetch ----------------------------------------------------------------


@needs_posix
async def test_a_package_npx_already_holds_is_not_fetched_again(stand_in: SimpleNamespace) -> None:
    stand_in.entry.mkdir(parents=True)
    (stand_in.entry / "package.json").write_text("{}")

    await npx_fetch.ensure_fetched("Claude Code", ["npx", "-y", _SPEC], env=stand_in.env, cwd=stand_in.cwd)

    assert _calls(stand_in.log) == []


@needs_posix
async def test_without_the_variable_npm_is_asked_where_its_cache_is(stand_in: SimpleNamespace, tmp_path: Path) -> None:
    """npm also reads its cache from its config files, which only npm itself resolves."""
    said = tmp_path / "configured-cache"
    npm = stand_in.bin_dir / "npm"
    npm.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> {shlex.quote(str(tmp_path / "npm.log"))}\necho {said}\n')
    npm.chmod(0o755)
    (said / "_npx" / _KEY).mkdir(parents=True)
    (said / "_npx" / _KEY / "package.json").write_text("{}")

    await npx_fetch.ensure_fetched("Claude Code", ["npx", "-y", _SPEC], env={}, cwd=stand_in.cwd)

    assert (tmp_path / "npm.log").read_text().splitlines() == ["config get cache"]
    assert _calls(stand_in.log) == []


@needs_posix
async def test_without_npm_the_cache_is_the_platform_default(stand_in: SimpleNamespace) -> None:
    entry = stand_in.home / ".npm" / "_npx" / _KEY
    entry.mkdir(parents=True)
    (entry / "package.json").write_text("{}")

    await npx_fetch.ensure_fetched(
        "Claude Code", ["npx", "-y", _SPEC], env={"PATH": str(stand_in.bin_dir)}, cwd=stand_in.cwd
    )

    assert _calls(stand_in.log) == []


@needs_posix
async def test_an_npm_that_cannot_say_leaves_the_platform_default(stand_in: SimpleNamespace) -> None:
    npm = stand_in.bin_dir / "npm"
    npm.write_text("#!/bin/sh\necho 'npm error config broken' >&2\nexit 1\n")
    npm.chmod(0o755)
    entry = stand_in.home / ".npm" / "_npx" / _KEY
    entry.mkdir(parents=True)
    (entry / "package.json").write_text("{}")

    await npx_fetch.ensure_fetched("Claude Code", ["npx", "-y", _SPEC], env={}, cwd=stand_in.cwd)

    assert _calls(stand_in.log) == []


@needs_posix
async def test_a_command_that_cannot_be_parsed_is_left_to_the_launch_that_reports_it(
    stand_in: SimpleNamespace,
) -> None:
    await npx_fetch.ensure_fetched("Claude Code", f"npx -y '{_SPEC}", env=stand_in.env, cwd=stand_in.cwd)

    assert _calls(stand_in.log) == []


@needs_posix
async def test_a_cache_that_cannot_be_looked_into_is_fetched_rather_than_failing_the_launch(
    stand_in: SimpleNamespace, tmp_path: Path
) -> None:
    """What stops the look -- here a name past the file system's limit, for a user a root-owned
    cache -- is npm's to report, in its own words, when the fetch runs into it."""
    env = {"npm_config_cache": str(tmp_path / ("x" * 300))}

    await npx_fetch.ensure_fetched("Claude Code", ["npx", "-y", _SPEC], env=env, cwd=stand_in.cwd)

    assert len(_fetches(stand_in.log)) == 1


@needs_posix
async def test_without_a_temporary_file_the_launch_fetches_as_it_always_has(
    stand_in: SimpleNamespace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fetch's output goes to a temporary file, and a full or missing temporary directory must
    not fail a launch that could still fetch inside its own handshake."""
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "gone"))

    await npx_fetch.ensure_fetched("Claude Code", ["npx", "-y", _SPEC], env=stand_in.env, cwd=stand_in.cwd)

    assert _calls(stand_in.log) == []


@needs_posix
async def test_an_npx_that_is_not_there_is_named_as_the_start_that_failed(
    stand_in: SimpleNamespace, tmp_path: Path
) -> None:
    nowhere = tmp_path / "empty"
    nowhere.mkdir()

    with pytest.raises(AcpConnectionError, match=r"acp agent 'Claude Code': cannot start 'npx': "):
        await npx_fetch.ensure_fetched(
            "Claude Code", ["npx", "-y", _SPEC], env={**stand_in.env, "PATH": str(nowhere)}, cwd=stand_in.cwd
        )


@needs_posix
async def test_a_missing_package_is_fetched_by_a_command_that_runs_nothing(stand_in: SimpleNamespace) -> None:
    await npx_fetch.ensure_fetched(
        "Claude Code", ["npx", "-y", _SPEC, "--adapter-flag"], env=stand_in.env, cwd=stand_in.cwd
    )

    assert [call["argv"] for call in _fetches(stand_in.log)] == [["-y", f"--package={_SPEC}", "--", "node", "-e", "0"]]
    assert (stand_in.entry / "package.json").is_file()


@needs_posix
async def test_a_caller_that_stops_waiting_leaves_the_fetch_to_finish(stand_in: SimpleNamespace) -> None:
    env = {**stand_in.env, "FAKE_NPX_DELAY": "1.0"}
    waiting = asyncio.create_task(
        npx_fetch.ensure_fetched("Claude Code", ["npx", "-y", _SPEC], env=env, cwd=stand_in.cwd)
    )
    await asyncio.sleep(0.3)
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting

    assert await asyncio.to_thread(_wait_until_finished, stand_in.log)
    assert (stand_in.entry / "package.json").is_file()


@needs_posix
async def test_a_caller_that_stops_waiting_leaves_the_others_their_fetch(stand_in: SimpleNamespace) -> None:
    """Launches that overlap share one fetch, so one of them giving up must not end it for the rest."""
    env = {**stand_in.env, "FAKE_NPX_DELAY": "1.0"}
    leaving, staying = (
        asyncio.create_task(npx_fetch.ensure_fetched("Claude Code", ["npx", "-y", _SPEC], env=env, cwd=stand_in.cwd))
        for _ in range(2)
    )
    await asyncio.sleep(0.3)
    leaving.cancel()
    with pytest.raises(asyncio.CancelledError):
        await leaving

    await staying

    assert (stand_in.entry / "package.json").is_file()
    assert len(_fetches(stand_in.log)) == 1


@needs_posix
async def test_a_launch_on_another_event_loop_runs_a_fetch_of_its_own(stand_in: SimpleNamespace) -> None:
    """A CLI command run in-process starts its own loop in a worker thread, and a fetch that loop
    started cannot be awaited from this one."""
    env = {**stand_in.env, "FAKE_NPX_DELAY": "1.0"}

    def elsewhere() -> None:
        asyncio.run(npx_fetch.ensure_fetched("Claude Code", ["npx", "-y", _SPEC], env=env, cwd=stand_in.cwd))

    other = threading.Thread(target=elsewhere)
    other.start()
    try:
        assert await asyncio.to_thread(_wait_for, lambda: _fetches(stand_in.log))

        await npx_fetch.ensure_fetched("Claude Code", ["npx", "-y", _SPEC], env=env, cwd=stand_in.cwd)
    finally:
        await asyncio.to_thread(other.join, 15)

    assert (stand_in.entry / "package.json").is_file()


async def test_a_fetch_that_ended_forgets_itself_and_not_the_one_after_it() -> None:
    """Its done callback can run after a newer fetch of the same entry took its place -- one started
    on another event loop, or right after this one failed -- and that one must stay shared."""
    loop = asyncio.get_running_loop()
    ended = loop.create_task(asyncio.sleep(0))
    await ended
    running = loop.create_task(asyncio.sleep(30))
    npx_fetch._fetches["entry"] = running
    try:
        npx_fetch._forget("entry", ended)

        assert npx_fetch._fetches.get("entry") is running
    finally:
        running.cancel()
        npx_fetch._fetches.pop("entry", None)


@needs_posix
def test_a_fetch_outlives_the_event_loop_that_started_it(stand_in: SimpleNamespace) -> None:
    """A process on its way out cancels what is pending and closes its loop; the fetch must not go with it."""
    env = {**stand_in.env, "FAKE_NPX_DELAY": "1.0"}

    async def start_and_leave() -> None:
        asyncio.get_running_loop().create_task(
            npx_fetch.ensure_fetched("Claude Code", ["npx", "-y", _SPEC], env=env, cwd=stand_in.cwd)
        )
        await asyncio.sleep(0.3)

    asyncio.run(start_and_leave())

    assert _wait_until_finished(stand_in.log)


@pytest.mark.skipif(not Path("/proc/self/stat").exists(), reason="reads the process table from /proc")
def test_a_fetch_its_event_loop_left_is_reaped_when_it_ends(stand_in: SimpleNamespace) -> None:
    """Nothing polls it once its loop is closed: left alone, it would stay a zombie for the life of the host."""
    env = {**stand_in.env, "FAKE_NPX_DELAY": "1.0"}

    async def start_and_leave() -> None:
        asyncio.get_running_loop().create_task(
            npx_fetch.ensure_fetched("Claude Code", ["npx", "-y", _SPEC], env=env, cwd=stand_in.cwd)
        )
        await asyncio.sleep(0.3)

    asyncio.run(start_and_leave())
    assert _wait_until_finished(stand_in.log)

    # A session leader's id is its pid, and the fetch leads a session of its own.
    assert _reaped(_fetches(stand_in.log)[0]["sid"])


@needs_posix
async def test_launches_that_overlap_share_one_fetch(stand_in: SimpleNamespace) -> None:
    env = {**stand_in.env, "FAKE_NPX_DELAY": "0.5"}

    await asyncio.gather(
        *(npx_fetch.ensure_fetched("Claude Code", ["npx", "-y", _SPEC], env=env, cwd=stand_in.cwd) for _ in range(3))
    )

    assert len(_fetches(stand_in.log)) == 1


@needs_posix
async def test_a_fetch_npm_refuses_is_reported_in_npm_s_own_words(stand_in: SimpleNamespace) -> None:
    env = {**stand_in.env, "FAKE_NPX_FETCH": "fail", "FAKE_NPX_STDERR": _INCOMPLETE}

    with pytest.raises(AcpConnectionError) as raised:
        await npx_fetch.ensure_fetched("Claude Code", ["npx", "-y", _SPEC], env=env, cwd=stand_in.cwd)

    assert raised.value.returncode == 254
    assert "Could not read package.json" in (raised.value.stderr or "")
    assert f"npx could not fetch {_SPEC} (exit 254)" in str(raised.value)


# ---- the launch ----------------------------------------------------------------


@needs_posix
async def test_a_launch_fetches_the_package_before_it_starts_the_agent(stand_in: SimpleNamespace) -> None:
    client = await acp_client.AcpClient.launch(
        name="Claude Code", command=f"npx -y {_SPEC}", cwd=stand_in.cwd, env=stand_in.env
    )
    try:
        init = await client.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, timeout=15)
    finally:
        await client.close()

    assert init["protocolVersion"] == 1
    assert [call["fetch"] for call in _calls(stand_in.log) if "argv" in call] == [True, False]


@needs_posix
async def test_the_fetch_runs_apart_from_the_host_and_from_the_agent(stand_in: SimpleNamespace) -> None:
    """Its own session, so no signal to the host's group or the agent's reaches it, and no host mark,
    so a sweep after the host dies leaves it to finish."""
    client = await acp_client.AcpClient.launch(
        name="Claude Code", command=f"npx -y {_SPEC}", cwd=stand_in.cwd, env=stand_in.env
    )
    await client.close()

    fetch, agent = [call for call in _calls(stand_in.log) if "argv" in call]
    assert fetch["sid"] != os.getsid(0)
    assert fetch["sid"] != agent["sid"]
    assert fetch["marked"] == []
    assert bool(agent["marked"]) == bool(leftovers.launch_env())


@needs_posix
async def test_a_pool_fetches_before_it_holds_the_agent_s_lock(stand_in: SimpleNamespace) -> None:
    """Dropping an agent's connections -- an edit, a removal, a wedged worker -- takes the lock a
    launch holds, and a first fetch can run for minutes."""
    pool = AcpConnectionPool()
    env = {**stand_in.env, "FAKE_NPX_DELAY": "2.0"}
    acquiring = asyncio.create_task(
        pool.acquire(name="Claude Code", command=f"npx -y {_SPEC}", cwd=stand_in.cwd, env=env)
    )
    try:
        assert await asyncio.to_thread(_wait_for, lambda: _fetches(stand_in.log))

        await asyncio.wait_for(pool.drop("Claude Code"), timeout=1.0)

        assert not _finished(stand_in.log), "the drop came back while the fetch was still running"
        await acquiring
    finally:
        if not acquiring.done():
            acquiring.cancel()
        await asyncio.gather(acquiring, return_exceptions=True)
        await pool.close_all()


# ---- the mirror, asked before the fetch ---------------------------------------


def _deciding(argv: list[str]) -> Callable[..., Awaitable[list[str]]]:
    """The mirror's answer, as the module gives it: the argv a launch starts with."""

    async def decide(command: object, env: object, cwd: object = None, **_kwargs: object) -> list[str]:
        return list(argv)

    return decide


@needs_posix
async def test_a_launch_the_mirror_took_over_is_not_fetched(
    stand_in: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fetch goes to the default registry, the very one the mirror's answer
    is about: the mirror is asked first, as the direct launch asks it, and a
    launch it took over is started from what it installed."""
    monkeypatch.setattr(mirrors, "package_launch_async", _deciding(["/opt/mirror/bin/agent"]))

    await npx_fetch.ensure_fetched("Claude Code", ["npx", "-y", _SPEC], env=stand_in.env, cwd=stand_in.cwd)

    assert _calls(stand_in.log) == [], "the fetch went to a registry the mirror took over from"


@needs_posix
async def test_a_pooled_launch_the_mirror_took_over_is_not_fetched(
    stand_in: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The user-facing route: the pool's prefetch would reach the registry the
    mirror's answer is about, and a registry that cannot be reached fails the
    launch before the fallback is consulted."""
    monkeypatch.setattr(mirrors, "package_launch_async", _deciding([sys.executable, str(_STUB)]))
    pool = AcpConnectionPool()
    env = {**stand_in.env, "ACP_STUB_MODE": "ok"}
    try:
        await pool.acquire(name="Claude Code", command=f"npx -y {_SPEC}", cwd=stand_in.cwd, env=env)
    finally:
        await pool.close_all()

    assert _fetches(stand_in.log) == [], "the fetch went to a registry the mirror took over from"


# ---- reading npm's report of an incomplete entry -------------------------------


def test_npm_s_report_of_an_incomplete_entry_names_the_entry() -> None:
    assert npx_fetch.incomplete_entry(_INCOMPLETE) == npx_fetch.IncompleteEntry(
        directory="/home/someone/.npm/_npx/5794dd75a801d955", key="5794dd75a801d955"
    )


def test_the_entry_is_read_from_the_tail_a_connect_keeps() -> None:
    """A connect keeps the last 400 characters of stderr; the line naming the entry is in them."""
    tail = _INCOMPLETE[-400:]
    assert not tail.startswith("npm error code")

    entry = npx_fetch.incomplete_entry(tail)

    assert entry is not None and entry.key == "5794dd75a801d955"


def test_a_windows_entry_is_read_with_its_own_separators() -> None:
    line = (
        "npm error enoent Could not read package.json: Error: ENOENT: no such file or directory, open "
        "'C:\\Users\\someone\\AppData\\Local\\npm-cache\\_npx\\5794dd75a801d955\\package.json'"
    )

    assert npx_fetch.incomplete_entry(line) == npx_fetch.IncompleteEntry(
        directory="C:\\Users\\someone\\AppData\\Local\\npm-cache\\_npx\\5794dd75a801d955", key="5794dd75a801d955"
    )


@pytest.mark.parametrize(
    "text",
    [
        # A project's own package.json is not an npx entry.
        "npm error enoent Could not read package.json: Error: ENOENT: no such file or directory, "
        "open '/home/someone/project/package.json'",
        "npm error code ECONNREFUSED",
        "",
    ],
)
def test_text_without_an_npx_entry_path_names_no_entry(text: str) -> None:
    assert npx_fetch.incomplete_entry(text) is None
