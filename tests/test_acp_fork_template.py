"""Raven agents forked from a template that has already loaded their code (Linux only)."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import sys
import textwrap
import time
from pathlib import Path

import pytest

from raven.acp_client import fork_template
from raven.acp_client.fork_template import fork_launch, raven_agent_argv

pytestmark = pytest.mark.skipif(not fork_template.template_available(), reason="forking agents is Linux-only")


@pytest.fixture(autouse=True)
async def _no_template():
    yield
    await fork_template.close_template()


def _product(tmp_path: Path, body: str) -> Path:
    folder = tmp_path / "product"
    folder.mkdir(exist_ok=True)
    (folder / "subagent.json").write_text('{"name": "P"}', encoding="utf-8")
    script = folder / "run.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return script


async def _launch(script: Path, *args: str, env: dict[str, str] | None = None, cwd: str | None = None):
    base = dict(os.environ)
    endpoint = await fork_launch(
        [sys.executable, str(script), *args], {**base, **(env or {})}, cwd or str(script.parent), base_env=base
    )
    assert endpoint is not None
    return endpoint


async def _drain(endpoint) -> tuple[bytes, int]:
    out = await asyncio.wait_for(endpoint._stdout.read(), timeout=60)
    return out, await asyncio.wait_for(endpoint.wait(), timeout=60)


def test_only_a_raven_agent_on_this_interpreter_is_forked(tmp_path: Path) -> None:
    script = _product(tmp_path, "pass\n")
    loose = tmp_path / "loose.py"
    loose.write_text("pass\n")

    assert raven_agent_argv([sys.executable, str(script), "--acp"])
    assert raven_agent_argv([sys.executable, "-m", "raven", "acp"])
    assert not raven_agent_argv([sys.executable, "-m", "other"])
    assert not raven_agent_argv([sys.executable, str(loose)])
    assert not raven_agent_argv(["/usr/bin/node", str(script)])
    # Another environment's interpreter runs with its own packages, however it links.
    other = tmp_path / "venv" / "bin" / "python"
    other.parent.mkdir(parents=True)
    other.symlink_to(sys.executable)
    assert not raven_agent_argv([str(other), str(script)])


async def test_a_forked_agent_runs_as_a_fresh_interpreter_would_have(tmp_path: Path) -> None:
    script = _product(
        tmp_path,
        """
        import os, sys
        line = sys.stdin.readline().strip()
        print(line, os.getcwd(), os.environ.get("PROBE"), sys.argv[1:], sys.path[0], os.getpgid(0) == os.getpid(), flush=True)
        print("diagnostic", file=sys.stderr, flush=True)
        sys.exit(7)
        """,
    )
    endpoint = await _launch(script, "--flag", env={"PROBE": "seen"}, cwd=str(tmp_path))
    try:
        assert "forked" in endpoint.describe()
        await endpoint.write(b"hello\n")
        out, code = await _drain(endpoint)
        assert out.decode().split() == ["hello", str(tmp_path), "seen", "['--flag']", str(script.parent), "True"]
        assert await endpoint.readerr() == b"diagnostic\n"
        assert code == 7
        assert endpoint.returncode == 7
    finally:
        endpoint.release()


async def test_a_launchers_exec_of_raven_runs_raven_in_the_same_process(tmp_path: Path) -> None:
    script = _product(
        tmp_path,
        """
        import os, sys
        print(os.getpid(), flush=True)
        os.execv(sys.executable, [sys.executable, "-m", "raven", "--version"])
        raise AssertionError("unreachable")
        """,
    )
    endpoint = await _launch(script)
    try:
        out, code = await _drain(endpoint)
        pid, rest = out.decode().split("\n", 1)
        assert int(pid) == endpoint.pid
        assert "Raven v" in rest
        assert code == 0
    finally:
        endpoint.release()


async def test_ending_an_agent_kills_its_group(tmp_path: Path) -> None:
    script = _product(
        tmp_path,
        """
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(600)"])
        print(child.pid, flush=True)
        time.sleep(600)
        """,
    )
    endpoint = await _launch(script)
    grandchild = int((await asyncio.wait_for(endpoint.readline(), timeout=60)).decode())
    await endpoint.end()
    code = await asyncio.wait_for(endpoint.wait(), timeout=30)
    endpoint.release()

    assert code == -signal.SIGKILL
    for _ in range(100):
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("the agent's own child outlived the group kill")


async def test_an_agent_ends_with_the_template(tmp_path: Path) -> None:
    script = _product(tmp_path, "import time\nprint('up', flush=True)\ntime.sleep(600)\n")
    endpoint = await _launch(script)
    assert await asyncio.wait_for(endpoint.readline(), timeout=60) == b"up\n"
    template = fork_template._TEMPLATE
    assert template is not None and template._proc is not None

    os.kill(template._proc.pid, signal.SIGKILL)
    await asyncio.wait_for(endpoint.wait(), timeout=30)
    endpoint.release()

    for _ in range(100):
        try:
            os.kill(endpoint.pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    pytest.fail("a forked agent outlived its template")


async def test_an_agent_writes_what_it_imported_for_the_next_template(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(fork_template, "LEARN_AFTER_S", 0.2)
    script = _product(tmp_path, "import time, colorsys\ntime.sleep(2)\n")
    endpoint = await _launch(script)
    await asyncio.wait_for(endpoint.wait(), timeout=60)
    endpoint.release()

    from raven.config.paths import get_runtime_subdir

    learned = json.loads((get_runtime_subdir("acp-template") / "modules.json").read_text())
    assert learned["python"] == list(sys.version_info[:2])
    assert list(learned["products"]) == ["product"], "learned under the folder the launcher sits in"
    assert "colorsys" in learned["products"]["product"]
    assert "__main__" not in learned["products"]["product"]


async def test_the_template_closes_once_it_has_had_no_agent_for_the_budget(tmp_path: Path, monkeypatch) -> None:
    from raven.config import live as live_mod

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"subagents": {"runtime": {"idleCloseS": 0.2}}}))
    monkeypatch.setattr(live_mod, "default_live", lambda: live_mod.LiveConfig(config))
    script = _product(tmp_path, "pass\n")
    endpoint = await _launch(script)
    await asyncio.wait_for(endpoint.wait(), timeout=60)
    endpoint.release()
    template = fork_template._TEMPLATE
    assert template is not None

    for _ in range(100):
        if not template.running:
            break
        await asyncio.sleep(0.05)
    assert not template.running


async def test_anything_but_a_raven_agent_is_launched_the_ordinary_way(tmp_path: Path) -> None:
    loose = tmp_path / "loose.py"
    loose.write_text("pass\n")
    base = dict(os.environ)
    assert await fork_launch([sys.executable, str(loose)], base, str(tmp_path), base_env=base) is None
    script = _product(tmp_path, "pass\n")
    assert await fork_launch([sys.executable, str(script)], base, str(tmp_path / "missing"), base_env=base) is None
    assert fork_template._TEMPLATE is None


async def test_a_template_that_cannot_start_is_not_tried_again(tmp_path: Path, monkeypatch) -> None:
    script = _product(tmp_path, "pass\n")
    started: list[int] = []

    async def refuse(self, env):
        started.append(1)
        raise RuntimeError("no room")

    monkeypatch.setattr(fork_template.ForkTemplate, "_start", refuse)
    base = dict(os.environ)
    for _ in range(2):
        assert await fork_launch([sys.executable, str(script)], base, str(tmp_path), base_env=base) is None
    assert started == [1]


async def test_an_acp_launch_of_a_raven_agent_is_forked_and_offered_the_relay(tmp_path: Path, monkeypatch) -> None:
    """Through the real launch: the agent runs from the template and its environment names the host's relay."""
    from raven.acp_client.client import AcpClient
    from raven.acp_client.pool import close_pool

    stub = Path(__file__).with_name("acp_stub_server.py")
    seen = tmp_path / "relay.txt"
    script = _product(
        tmp_path,
        f"""
        import os, runpy
        open({str(seen)!r}, "w").write(os.environ.get("RAVEN_LLM_RELAY", ""))
        runpy.run_path({str(stub)!r}, run_name="__main__")
        """,
    )
    client = await AcpClient.launch(name="forked", command=[sys.executable, str(script)], cwd=str(tmp_path))
    try:
        assert "forked" in client.endpoint.describe()
        result = await client.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, timeout=60)
        assert isinstance(result, dict)
        assert seen.read_text().startswith(("unix:", "tcp:"))
    finally:
        await client.close()
        await close_pool()
    assert client.endpoint.returncode is not None


async def test_switching_both_off_launches_a_raven_agent_as_before(tmp_path: Path, monkeypatch) -> None:
    from raven.acp_client.client import AcpClient
    from raven.config import live as live_mod

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"subagents": {"runtime": {"llmRelay": False, "forkTemplate": False}}}))
    monkeypatch.setattr(live_mod, "default_live", lambda: live_mod.LiveConfig(config))
    stub = Path(__file__).with_name("acp_stub_server.py")
    seen = tmp_path / "relay.txt"
    script = _product(
        tmp_path,
        f"""
        import os, runpy
        open({str(seen)!r}, "w").write(os.environ.get("RAVEN_LLM_RELAY", "none"))
        runpy.run_path({str(stub)!r}, run_name="__main__")
        """,
    )
    client = await AcpClient.launch(name="plain", command=[sys.executable, str(script)], cwd=str(tmp_path))
    try:
        assert "forked" not in client.endpoint.describe()
        await client.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, timeout=60)
        assert seen.read_text() == "none"
    finally:
        await client.close()


# ---- the template's side, driven in this process ---------------------------------


class _Ended(Exception):
    def __init__(self, code: int) -> None:
        super().__init__(code)
        self.code = code


@pytest.fixture
def ended(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Run the agent-side functions here: the exit becomes an exception, and what they change is put back.

    The codes are recorded because the real exit never returns, while this one
    unwinds through the frames above it, which may read it as a second ending.
    """
    codes: list[int] = []

    def finish(code: int) -> None:
        codes.append(code)
        raise _Ended(code)

    monkeypatch.setattr(fork_template, "_finish", finish)
    monkeypatch.setattr(os, "execv", os.execv)
    monkeypatch.setattr(sys, "argv", list(sys.argv))
    monkeypatch.setattr(sys, "path", list(sys.path))
    return codes


def _learned(**products: list[str]) -> dict:
    return {"python": list(sys.version_info[:2]), "products": products}


def test_the_template_loads_the_seed_and_what_agents_imported_but_not_what_it_skips(
    tmp_path: Path, monkeypatch
) -> None:
    for name in ("colorsys", "netrc", "this_module_does_not_exist"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    learned = tmp_path / "modules.json"
    learned.write_text(json.dumps(_learned(only=["netrc", "this_module_does_not_exist"])))

    fork_template._load(("colorsys",), learned, ())
    assert "colorsys" in sys.modules and "netrc" in sys.modules

    monkeypatch.delitem(sys.modules, "netrc")
    fork_template._load((), learned, ("net",))
    assert "netrc" not in sys.modules

    learned.write_text(json.dumps({"python": [2, 7], "products": {"only": ["netrc"]}}))
    fork_template._load((), learned, ())
    assert "netrc" not in sys.modules


def test_one_products_own_modules_are_loaded_ahead_only_while_it_is_the_only_one_seen() -> None:
    """What only one product imports is shared by no other process, so it is not loaded ahead for the rest."""
    assert fork_template.preload_names(_learned(code=["core", "code.only"])) == ["code.only", "core"]
    two = _learned(
        code=["core", "both", "code.only"], ppt=["core", "both", "deck.engine"], design=["core", "design.only"]
    )
    assert fork_template.preload_names(two) == ["both", "core"]
    assert fork_template.preload_names(_learned()) == []
    assert fork_template.preload_names({"python": [2, 7], "products": {"code": ["core"]}}) == []
    assert fork_template.preload_names({"python": list(sys.version_info[:2]), "modules": ["core"]}) == []
    assert fork_template.preload_names(["not", "a", "record"]) == []
    assert fork_template.preload_names(_learned(code=["core", 7], odd="not a list")) == ["core"]


def test_an_agent_is_learned_under_its_product_folder() -> None:
    assert fork_template.product_of([sys.executable, "/x/agents/raven-code/run.py", "--acp"]) == "raven-code"
    assert fork_template.product_of([sys.executable, "-m", "raven", "acp"]) == "raven"
    assert fork_template.product_of([sys.executable, "run.py"]) == "raven"


def test_an_agent_adds_to_its_products_record_and_starts_over_on_another_python(tmp_path: Path) -> None:
    learned = tmp_path / "t" / "modules.json"
    learned.parent.mkdir()
    learned.write_text(json.dumps(_learned(code=["kept.before"], ppt=["deck.engine"])))

    fork_template._learn(learned, ("json",), "code")
    products = json.loads(learned.read_text())["products"]
    assert "kept.before" in products["code"] and "os" in products["code"]
    assert not any(n.startswith("json") for n in products["code"]) and "__main__" not in products["code"]
    assert products["ppt"] == ["deck.engine"], "another product's record is left as it was"

    learned.write_text(json.dumps({"python": [2, 7], "products": {"code": ["stale.one"]}}))
    fork_template._learn(learned, (), "code")
    assert "stale.one" not in json.loads(learned.read_text())["products"]["code"]


def test_an_agents_end_is_read_the_way_an_interpreter_reads_it(capsys) -> None:
    assert fork_template._exit_code(SystemExit(None)) == 0
    assert fork_template._exit_code(SystemExit(4)) == 4
    assert fork_template._exit_code(SystemExit("bad config")) == 1
    assert "bad config" in capsys.readouterr().err
    assert fork_template._exit_code(ValueError("boom")) == 1
    assert "ValueError: boom" in capsys.readouterr().err


def test_a_script_runs_with_its_own_directory_first_on_the_path(tmp_path: Path, ended) -> None:
    script = _product(
        tmp_path, "import sys\nsys.exit(len(sys.argv) * 10 + (sys.path[0] == __file__.rsplit('/', 1)[0]))\n"
    )
    with pytest.raises(_Ended) as caught:
        fork_template._run_agent([sys.executable, str(script), "a", "b"])
    assert caught.value.code == 31


def test_a_launchers_exec_of_raven_becomes_raven_here_and_any_other_exec_is_real(
    tmp_path: Path, ended, monkeypatch, capsys
) -> None:
    script = _product(
        tmp_path, "import os, sys\nos.execv(sys.executable, [sys.executable, '-m', 'raven', '--version'])\n"
    )
    with pytest.raises(_Ended):
        fork_template._run_agent([sys.executable, str(script)])
    assert ended[0] == 0
    assert "Raven v" in capsys.readouterr().out

    execs: list[list[str]] = []

    def real(path, args):
        execs.append([str(path), *args])
        raise _Ended(99)

    monkeypatch.setattr(os, "execv", real)
    other = _product(tmp_path, "import os\nos.execv('/bin/true', ['/bin/true'])\n")
    with pytest.raises(_Ended):
        fork_template._run_agent([sys.executable, str(other)])
    assert execs == [["/bin/true", "/bin/true"]]


def test_a_module_launch_runs_raven_here(ended, capsys) -> None:
    with pytest.raises(_Ended) as caught:
        fork_template._run_agent([sys.executable, "-m", "raven", "--version"])
    assert caught.value.code == 0
    assert "Raven v" in capsys.readouterr().out


# The test process runs threads (the host side below, pytest's own); the forked
# child does nothing but exit, which is what the warning's deadlock cannot reach.
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
def test_the_template_loop_forks_reports_the_exit_and_stops_when_let_go(tmp_path: Path, monkeypatch) -> None:
    import socket
    import threading

    from raven.providers import relay_wire

    counts = iter([2])
    monkeypatch.setattr(fork_template.threading, "active_count", lambda: next(counts, 1))
    path = str(tmp_path / "t.sock")
    life_r, life_w = os.pipe()
    os.write(life_w, b"x")
    seen: dict[str, object] = {}

    def host() -> None:
        while not os.path.exists(path):
            time.sleep(0.01)
        pipes = [os.pipe() for _ in range(3)]
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as bad:
            bad.connect(path)
            socket.send_fds(bad, [b"F"], [pipes[0][0]])
            relay_wire.send_frame(bad, {"argv": ["x"], "cwd": "/"})
            seen["refused"] = relay_wire.recv_frame(bad)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as busy:
            busy.connect(path)
            socket.send_fds(busy, [b"F"], [pipes[0][0], pipes[1][1], pipes[2][1]])
            relay_wire.send_frame(busy, {"argv": [sys.executable, "x.py"], "env": {}, "cwd": "/"})
            seen["threaded"] = relay_wire.recv_frame(busy)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.connect(path)
            socket.send_fds(sock, [b"F"], [pipes[0][0], pipes[1][1], pipes[2][1]])
            relay_wire.send_frame(sock, {"argv": [sys.executable, "x.py"], "env": {}, "cwd": "/"})
            seen["forked"] = relay_wire.recv_frame(sock)
            seen["exit"] = relay_wire.recv_frame(sock)
        os.close(life_w)

    thread = threading.Thread(target=host)
    thread.start()
    forked = fork_template._serve(path, lifeline=life_r)
    if forked is not None:
        os._exit(9 if forked[0]["argv"] == [sys.executable, "x.py"] and len(forked[1]) == 3 else 1)
    thread.join(timeout=30)
    os.close(life_r)

    assert "error" in seen["refused"]
    assert "thread" in seen["threaded"]["error"]
    assert isinstance(seen["forked"].get("pid"), int)
    assert seen["exit"] == {"exit": 9}
    assert signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL


def test_an_argv_the_check_cannot_read_is_not_forked(monkeypatch, tmp_path: Path) -> None:
    script = _product(tmp_path, "pass\n")
    assert not raven_agent_argv([None, str(script)])  # type: ignore[list-item]

    def unreadable(self) -> bool:
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "is_file", unreadable)
    assert not raven_agent_argv([sys.executable, str(script)])


async def test_a_closed_agent_takes_no_more_input_and_its_template_counts_none(tmp_path: Path) -> None:
    script = _product(tmp_path, "import sys\nsys.stdin.read()\n")
    endpoint = await _launch(script)
    await endpoint.end()
    await asyncio.wait_for(endpoint.wait(), timeout=30)
    with pytest.raises(BrokenPipeError):
        await endpoint.write(b"late\n")
    endpoint.release()
    template = fork_template._TEMPLATE
    assert template is not None and template.children == 0


async def test_a_template_that_ended_on_its_own_is_replaced_on_the_next_launch(tmp_path: Path) -> None:
    script = _product(tmp_path, "pass\n")
    first = await _launch(script)
    await asyncio.wait_for(first.wait(), timeout=30)
    first.release()
    template = fork_template._TEMPLATE
    assert template is not None and template._proc is not None
    old = template._proc.pid
    os.kill(old, signal.SIGKILL)
    await asyncio.wait_for(template._proc.wait(), timeout=30)

    second = await _launch(script)
    await asyncio.wait_for(second.wait(), timeout=30)
    second.release()
    assert template._proc is not None and template._proc.pid != old


async def test_a_launch_cancels_the_pending_idle_close(tmp_path: Path) -> None:
    script = _product(tmp_path, "pass\n")
    first = await _launch(script)
    await asyncio.wait_for(first.wait(), timeout=30)
    first.release()
    template = fork_template._TEMPLATE
    assert template is not None
    template.close_when_idle(3600)
    pending = template._idle_task
    assert pending is not None
    template.close_when_idle(3600)
    assert template._idle_task is pending, "a second request does not stack a second timer"

    second = await _launch(script)
    await asyncio.wait_for(second.wait(), timeout=30)
    second.release()
    await asyncio.sleep(0)
    assert pending.cancelled()
    template.close_when_idle(None)
    assert template._idle_task is None or template._idle_task is not pending


async def test_a_template_that_refuses_or_cannot_be_reached_fails_the_fork_alone(tmp_path: Path) -> None:
    script = _product(tmp_path, "pass\n")
    first = await _launch(script)
    await asyncio.wait_for(first.wait(), timeout=30)
    first.release()
    template = fork_template._TEMPLATE
    assert template is not None

    with pytest.raises(RuntimeError, match="refused"):
        await template._fork("not an argv", dict(os.environ), str(tmp_path))  # type: ignore[arg-type]
    real = template._socket
    template._socket = str(tmp_path / "nobody-listens.sock")
    try:
        with pytest.raises(OSError):
            await template._fork([sys.executable, str(script)], dict(os.environ), str(tmp_path))
    finally:
        template._socket = real
    assert not template.broken


async def test_a_template_that_never_reports_ready_is_not_used(tmp_path: Path, monkeypatch) -> None:
    async def silent(proc):
        return {"ready": False, "reason": "an import started a thread (x)"}

    monkeypatch.setattr(fork_template.ForkTemplate, "_ready_report", staticmethod(silent))
    script = _product(tmp_path, "pass\n")
    base = dict(os.environ)
    assert await fork_launch([sys.executable, str(script)], base, str(tmp_path), base_env=base) is None
    template = fork_template._TEMPLATE
    assert template is not None and "thread" in template.broken
    with pytest.raises(RuntimeError):
        await template.spawn([sys.executable, str(script)], base, str(tmp_path), base)


async def test_the_ready_line_is_found_among_what_an_import_printed() -> None:
    from types import SimpleNamespace

    def proc(*lines: bytes):
        stream = asyncio.StreamReader()
        for line in lines:
            stream.feed_data(line)
        stream.feed_eof()
        return SimpleNamespace(stdout=stream, stderr=stream)

    report = await fork_template.ForkTemplate._ready_report(proc(b"noise\n", b'{"other": 1}\n', b'{"ready": true}\n'))
    assert report == {"ready": True}
    assert await fork_template.ForkTemplate._ready_report(proc(b"noise\n")) == {}
    assert await fork_template.ForkTemplate._ready_report(SimpleNamespace(stdout=None)) == {}
    await fork_template.ForkTemplate._log_stderr(proc(b"a warning\n"))
    await fork_template.ForkTemplate._log_stderr(SimpleNamespace(stderr=None))


async def test_a_template_built_for_other_skips_is_replaced_while_it_has_no_agent(tmp_path: Path) -> None:
    script = _product(tmp_path, "pass\n")
    base = dict(os.environ)
    first = await fork_launch([sys.executable, str(script)], base, str(tmp_path), base_env=base, skip=("litellm",))
    assert first is not None
    await asyncio.wait_for(first.wait(), timeout=30)
    first.release()
    old = fork_template._TEMPLATE

    second = await fork_launch([sys.executable, str(script)], base, str(tmp_path), base_env=base)
    assert second is not None
    await asyncio.wait_for(second.wait(), timeout=30)
    second.release()
    assert fork_template._TEMPLATE is not old and old is not None and not old.running


def test_learning_survives_an_unreadable_record_and_an_unwritable_place(tmp_path: Path) -> None:
    fork_template._load((), tmp_path / "missing.json", ())
    corrupt = tmp_path / "modules.json"
    corrupt.write_text("{not json")
    fork_template._learn(corrupt, (), "code")
    assert "os" in json.loads(corrupt.read_text())["products"]["code"]
    corrupt.write_text(json.dumps(["not", "a", "record"]))
    fork_template._learn(corrupt, (), "code")
    assert "os" in json.loads(corrupt.read_text())["products"]["code"]
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("")
    fork_template._learn(blocked / "modules.json", (), "code")


def test_a_script_that_just_ends_exits_zero(tmp_path: Path, ended) -> None:
    script = _product(tmp_path, "x = 1\n")
    with pytest.raises(_Ended):
        fork_template._run_agent([sys.executable, str(script)])
    assert ended == [0]


def test_the_template_entry_loads_freezes_and_serves(tmp_path: Path, monkeypatch) -> None:
    import gc

    served: list[str] = []
    became: list[tuple] = []
    monkeypatch.setattr(gc, "freeze", lambda: None)
    monkeypatch.setattr(fork_template, "_load", lambda seed, learned, skip: 0)
    monkeypatch.setattr(fork_template.threading, "active_count", lambda: 1)

    def serve(path, *, lifeline):
        served.append(path)
        return None

    monkeypatch.setattr(fork_template, "_serve", serve)
    args = ["--socket", str(tmp_path / "s"), "--learned", str(tmp_path / "m.json"), "--skip", "litellm"]
    assert fork_template.main(args) == 0
    assert served == [str(tmp_path / "s")]

    monkeypatch.setattr(fork_template, "_serve", lambda path, *, lifeline: ({"argv": ["x"]}, [1, 2, 3]))
    monkeypatch.setattr(fork_template, "_become", lambda *a: became.append(a))
    assert fork_template.main(args) == 1
    assert became[0][0] == {"argv": ["x"]} and became[0][4] == ["litellm"]


def test_the_template_entry_refuses_to_serve_with_a_thread_running(tmp_path: Path, monkeypatch, capsys) -> None:
    import gc

    monkeypatch.setattr(gc, "freeze", lambda: None)
    monkeypatch.setattr(fork_template, "_load", lambda seed, learned, skip: 0)
    monkeypatch.setattr(fork_template.threading, "active_count", lambda: 2)
    args = ["--socket", str(tmp_path / "s"), "--learned", str(tmp_path / "m.json")]
    assert fork_template.main(args) == 3
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["ready"] is False


async def test_a_relay_that_cannot_start_leaves_the_agent_to_load_litellm(tmp_path: Path, monkeypatch) -> None:
    from raven.acp_client.client import AcpClient
    from raven.providers import relay_server

    async def no_relay():
        raise OSError("no room for a socket")

    monkeypatch.setattr(relay_server, "relay_env", no_relay)
    stub = Path(__file__).with_name("acp_stub_server.py")
    seen = tmp_path / "relay.txt"
    script = _product(
        tmp_path,
        f"""
        import os, runpy
        open({str(seen)!r}, "w").write(os.environ.get("RAVEN_LLM_RELAY", "none"))
        runpy.run_path({str(stub)!r}, run_name="__main__")
        """,
    )
    client = await AcpClient.launch(name="norelay", command=[sys.executable, str(script)], cwd=str(tmp_path))
    try:
        await client.request("initialize", {"protocolVersion": 1, "clientCapabilities": {}}, timeout=60)
        assert seen.read_text() == "none"
    finally:
        await client.close()


def test_a_new_event_loop_gets_a_new_template_and_the_old_one_ends(tmp_path: Path) -> None:
    """A CLI turn is one asyncio.run; a template opened under the last one cannot serve the next."""
    script = _product(tmp_path, "print('ran', flush=True)\n")

    async def once() -> tuple[int, bytes]:
        endpoint = await _launch(script)
        out, _ = await _drain(endpoint)
        endpoint.release()
        template = fork_template._TEMPLATE
        assert template is not None and template._proc is not None
        return template._proc.pid, out

    first_pid, first_out = asyncio.run(once())
    second_pid, second_out = asyncio.run(once())
    try:
        assert first_out == second_out == b"ran\n"
        assert second_pid != first_pid
        for _ in range(100):
            try:
                os.kill(first_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        else:
            pytest.fail("the template opened under the ended loop is still running")
    finally:
        asyncio.run(fork_template.close_template())
