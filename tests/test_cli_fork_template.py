"""The fork template's own process, driven in this one: what it loads, learns, forks and becomes (Linux only)."""

from __future__ import annotations

import json
import os
import signal
import sys
import textwrap
import time
from pathlib import Path

import pytest

from raven.acp_client import fork_template
from raven.acp_client.fork_template import template_available
from raven.cli import _fork_template as template
from raven.cli import entry
from raven.security import tls

pytestmark = pytest.mark.skipif(not template_available(), reason="forking agents is Linux-only")


def _product(tmp_path: Path, body: str) -> Path:
    folder = tmp_path / "product"
    folder.mkdir(exist_ok=True)
    (folder / "subagent.json").write_text('{"name": "P"}', encoding="utf-8")
    script = folder / "run.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    return script


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

    monkeypatch.setattr(template, "_finish", finish)
    monkeypatch.setattr(os, "execv", os.execv)
    # Raven's entry switches the whole process to the system's certificate store,
    # and this process is pytest's.
    monkeypatch.setenv(tls.OPT_OUT_ENV, "1")
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

    template._load(("colorsys",), learned, ())
    assert "colorsys" in sys.modules and "netrc" in sys.modules

    monkeypatch.delitem(sys.modules, "netrc")
    template._load((), learned, ("net",))
    assert "netrc" not in sys.modules

    learned.write_text(json.dumps({"python": [2, 7], "products": {"only": ["netrc"]}}))
    template._load((), learned, ())
    assert "netrc" not in sys.modules


def test_one_products_own_modules_are_loaded_ahead_only_while_it_is_the_only_one_seen() -> None:
    """What only one product imports is shared by no other process, so it is not loaded ahead for the rest."""
    assert template.preload_names(_learned(code=["core", "code.only"])) == ["code.only", "core"]
    two = _learned(
        code=["core", "both", "code.only"], ppt=["core", "both", "deck.engine"], design=["core", "design.only"]
    )
    assert template.preload_names(two) == ["both", "core"]
    assert template.preload_names(_learned()) == []
    assert template.preload_names({"python": [2, 7], "products": {"code": ["core"]}}) == []
    assert template.preload_names({"python": list(sys.version_info[:2]), "modules": ["core"]}) == []
    assert template.preload_names(["not", "a", "record"]) == []
    assert template.preload_names(_learned(code=["core", 7], odd="not a list")) == ["core"]


def test_an_agent_is_learned_under_its_product_folder() -> None:
    assert template.product_of([sys.executable, "/x/agents/raven-code/run.py", "--acp"]) == "raven-code"
    assert template.product_of([sys.executable, "-m", "raven", "acp"]) == "raven"
    assert template.product_of([sys.executable, "run.py"]) == "raven"


def test_an_agent_adds_to_its_products_record_and_starts_over_on_another_python(tmp_path: Path) -> None:
    learned = tmp_path / "t" / "modules.json"
    learned.parent.mkdir()
    learned.write_text(json.dumps(_learned(code=["kept.before"], ppt=["deck.engine"])))

    template._learn(learned, ("json",), "code")
    products = json.loads(learned.read_text())["products"]
    assert "kept.before" in products["code"] and "os" in products["code"]
    assert not any(n.startswith("json") for n in products["code"]) and "__main__" not in products["code"]
    assert products["ppt"] == ["deck.engine"], "another product's record is left as it was"

    learned.write_text(json.dumps({"python": [2, 7], "products": {"code": ["stale.one"]}}))
    template._learn(learned, (), "code")
    assert "stale.one" not in json.loads(learned.read_text())["products"]["code"]


def test_an_agents_end_is_read_the_way_an_interpreter_reads_it(capsys) -> None:
    assert template._exit_code(SystemExit(None)) == 0
    assert template._exit_code(SystemExit(4)) == 4
    assert template._exit_code(SystemExit("bad config")) == 1
    assert "bad config" in capsys.readouterr().err
    assert template._exit_code(ValueError("boom")) == 1
    assert "ValueError: boom" in capsys.readouterr().err


def test_a_script_runs_with_its_own_directory_first_on_the_path(tmp_path: Path, ended) -> None:
    script = _product(
        tmp_path, "import sys\nsys.exit(len(sys.argv) * 10 + (sys.path[0] == __file__.rsplit('/', 1)[0]))\n"
    )
    with pytest.raises(_Ended) as caught:
        template._run_agent([sys.executable, str(script), "a", "b"])
    assert caught.value.code == 31


def test_a_launchers_exec_of_raven_becomes_raven_here_and_any_other_exec_is_real(
    tmp_path: Path, ended, monkeypatch, capsys
) -> None:
    script = _product(
        tmp_path, "import os, sys\nos.execv(sys.executable, [sys.executable, '-m', 'raven', '--version'])\n"
    )
    with pytest.raises(_Ended):
        template._run_agent([sys.executable, str(script)])
    assert ended[0] == 0
    assert "Raven v" in capsys.readouterr().out

    execs: list[list[str]] = []

    def real(path, args):
        execs.append([str(path), *args])
        raise _Ended(99)

    monkeypatch.setattr(os, "execv", real)
    other = _product(tmp_path, "import os\nos.execv('/bin/true', ['/bin/true'])\n")
    with pytest.raises(_Ended):
        template._run_agent([sys.executable, str(other)])
    assert execs == [["/bin/true", "/bin/true"]]


def test_a_module_launch_runs_raven_here(ended, capsys) -> None:
    with pytest.raises(_Ended) as caught:
        template._run_agent([sys.executable, "-m", "raven", "--version"])
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
    monkeypatch.setattr(template.threading, "active_count", lambda: next(counts, 1))
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
    forked = template._serve(path, lifeline=life_r)
    if forked is not None:
        os._exit(9 if forked[0]["argv"] == [sys.executable, "x.py"] and len(forked[1]) == 3 else 1)
    thread.join(timeout=30)
    os.close(life_r)

    assert "error" in seen["refused"]
    assert "thread" in seen["threaded"]["error"]
    assert isinstance(seen["forked"].get("pid"), int)
    assert seen["exit"] == {"exit": 9}
    assert signal.getsignal(signal.SIGCHLD) == signal.SIG_DFL


def test_learning_survives_an_unreadable_record_and_an_unwritable_place(tmp_path: Path) -> None:
    template._load((), tmp_path / "missing.json", ())
    corrupt = tmp_path / "modules.json"
    corrupt.write_text("{not json")
    template._learn(corrupt, (), "code")
    assert "os" in json.loads(corrupt.read_text())["products"]["code"]
    corrupt.write_text(json.dumps(["not", "a", "record"]))
    template._learn(corrupt, (), "code")
    assert "os" in json.loads(corrupt.read_text())["products"]["code"]
    blocked = tmp_path / "file-not-dir"
    blocked.write_text("")
    template._learn(blocked / "modules.json", (), "code")


def test_a_script_that_just_ends_exits_zero(tmp_path: Path, ended) -> None:
    script = _product(tmp_path, "x = 1\n")
    with pytest.raises(_Ended):
        template._run_agent([sys.executable, str(script)])
    assert ended == [0]


def test_the_template_entry_loads_freezes_and_serves(tmp_path: Path, monkeypatch) -> None:
    import gc

    served: list[str] = []
    became: list[tuple] = []
    monkeypatch.setattr(gc, "freeze", lambda: None)
    monkeypatch.setattr(template, "_load", lambda seed, learned, skip: 0)
    monkeypatch.setattr(template.threading, "active_count", lambda: 1)

    def serve(path, *, lifeline):
        served.append(path)
        return None

    monkeypatch.setattr(template, "_serve", serve)
    args = ["--socket", str(tmp_path / "s"), "--learned", str(tmp_path / "m.json"), "--skip", "litellm"]
    assert template.main(args) == 0
    assert served == [str(tmp_path / "s")]

    monkeypatch.setattr(template, "_serve", lambda path, *, lifeline: ({"argv": ["x"]}, [1, 2, 3]))
    monkeypatch.setattr(template, "_become", lambda *a: became.append(a))
    assert template.main(args) == 1
    assert became[0][0] == {"argv": ["x"]} and became[0][4] == ["litellm"]


def test_the_template_entry_refuses_to_serve_with_a_thread_running(tmp_path: Path, monkeypatch, capsys) -> None:
    import gc

    monkeypatch.setattr(gc, "freeze", lambda: None)
    monkeypatch.setattr(template, "_load", lambda seed, learned, skip: 0)
    monkeypatch.setattr(template.threading, "active_count", lambda: 2)
    args = ["--socket", str(tmp_path / "s"), "--learned", str(tmp_path / "m.json")]
    assert template.main(args) == 3
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])["ready"] is False


def test_the_entry_settles_trust_then_hands_the_template_its_arguments(monkeypatch) -> None:
    """The host starts the template through Raven's entry point, so the switch comes before every import."""
    calls: list = []
    monkeypatch.setattr(tls, "use_system_ca", lambda: calls.append("trust"))
    monkeypatch.setattr(template, "main", lambda argv: calls.append(list(argv)) or 0)
    monkeypatch.setattr(sys, "argv", ["raven", fork_template.TEMPLATE_ARG, "--socket", "s"])

    with pytest.raises(SystemExit) as ended:
        entry.run()
    assert ended.value.code == 0
    assert calls == ["trust", ["--socket", "s"]]
