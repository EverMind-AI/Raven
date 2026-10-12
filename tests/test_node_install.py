"""raven.node.install: putting this Raven's node on a registered machine.

The steps are shell text run on the machine, so the parts that decide
something -- the probe, the prune -- run here under a real ``sh`` against a
stand-in tree, and one install runs for real on this computer: a virtualenv of
this interpreter, this Raven's code unpacked into it, and the node that makes
reporting this Raven's digest. Only the package index is stood in for; no test
reaches the network.

Every failure is one sentence that names the machine, never its address, and
a step that fails leaves no half-made node behind.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from raven.acp_client import remote
from raven.node import bundle, install, protocol
from raven.node.install import InstallError, Plan

HOST = "203.0.113.7"
PORT = 58717

ROW = {"id": "box", "display_name": "Lab box", "host": HOST, "port": PORT, "user": "worker", "key": "/keys/id_test"}


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "connections.json"

    def write(**extra: Any) -> None:
        path.write_text(json.dumps({"connections": [{**ROW, **extra}]}), encoding="utf-8")

    write()
    monkeypatch.setenv("RAVEN_CONNECTIONS", str(path))
    monkeypatch.setenv("RAVEN_DEBUG_MACHINES", "1")
    return write


def _target(**extra: Any) -> remote.Machine:
    return remote.Machine(id="box", display_name="Lab box", _row={**ROW, **extra})


def _plan(**fields: Any) -> Plan:
    base: dict[str, Any] = {
        "target": _target(),
        "node_dir": "~/.raven-node",
        "install_name": bundle.install_name(),
        "present": False,
        "python": "/usr/bin/python3.12",
        "uv": "/usr/bin/uv",
        "free_kb": 10 * 1024 * 1024,
        "fetch": "/usr/bin/curl",
    }
    return Plan(**{**base, **fields})


class Machine:
    """Answers each step by name, records what it was sent, and fails where told."""

    def __init__(self, **answers: tuple[int, str]) -> None:
        self.answers = answers
        self.sent: list[str] = []
        self.pushed: list[bytes] = []
        self.check = json.dumps({"protocol": protocol.PROTOCOL, "digest": bundle.digest()})

    def _answer(self, command: str) -> tuple[int, str]:
        for name, cmd in install.steps(_plan()) + install.steps(_plan(python="", uv="/usr/bin/uv")):
            if command == cmd and name in self.answers:
                return self.answers[name]
        if command.endswith("-I -m raven.node --version"):
            return 0, self.check
        if " du -sk " in f" {command}":
            return self.answers.get("du", (0, "51234\n"))
        if "echo RG=1 || echo RG=0" in command:
            return 0, "RG=1\n"
        return 0, ""

    def run(self, target: remote.Machine, command: str) -> tuple[int, str]:
        assert target.id == "box"
        self.sent.append(command)
        return self._answer(command)

    def push(self, target: remote.Machine, command: str, data: bytes) -> tuple[int, str]:
        self.sent.append(command)
        self.pushed.append(data)
        return self.answers.get("code", (0, ""))


@pytest.fixture
def small_tarball(monkeypatch: pytest.MonkeyPatch) -> bytes:
    monkeypatch.setattr(bundle, "tarball", lambda root=None: b"TAR")
    return b"TAR"


def test_each_step_runs_through_the_registry_s_ssh_with_the_step_budget(monkeypatch):
    seen: list[tuple] = []
    monkeypatch.setattr(
        remote, "run", lambda target, command, *, timeout: seen.append(("run", command, timeout)) or (0, "")
    )
    monkeypatch.setattr(
        remote,
        "push",
        lambda target, command, data, *, timeout: seen.append(("push", command, data, timeout)) or (0, ""),
    )

    assert install._run(_target(), "true") == (0, "")
    assert install._push(_target(), "cat", b"x") == (0, "")

    assert seen == [("run", "true", 900.0), ("push", "cat", b"x", 900.0)]


# --- one look at the machine ------------------------------------------------------


def test_the_probe_s_answers_become_the_plan(registry):
    sent: list[str] = []

    def run(target: remote.Machine, command: str) -> tuple[int, str]:
        sent.append(command)
        return (
            0,
            "bash: no job control\nPRESENT=1\nPYTHON=/usr/bin/python3.12\nUV=/root/.local/bin/uv\nFREE_KB=2097152\n",
        )

    p = install.plan("box", run=run)

    assert (p.node_dir, p.install_name, p.present, p.python, p.uv, p.free_kb) == (
        "~/.raven-node",
        bundle.install_name(),
        True,
        "/usr/bin/python3.12",
        "/root/.local/bin/uv",
        2097152,
    )
    assert p.target.label == "'Lab box' (box)"
    (script,) = sent
    assert bundle.digest() in script and HOST not in script


def test_a_machine_with_nothing_found_plans_nothing_present(registry):
    p = install.plan("box", run=lambda target, command: (0, "FREE_KB=\n"))

    assert (p.present, p.python, p.uv, p.free_kb, p.how) == (False, "", "", None, "")


@pytest.mark.parametrize(
    ("registered", "asked", "want"),
    [
        (None, None, "~/.raven-node"),
        ("/data/raven-node/", None, "/data/raven-node"),
        ("/data/raven-node", " /tmp/nodes/ ", "/tmp/nodes"),
    ],
)
def test_the_node_dir_is_the_one_asked_for_else_the_registry_s(registry, registered, asked, want):
    if registered is not None:
        registry(node_dir=registered)

    p = install.plan("box", node_dir=asked, run=lambda target, command: (0, ""))

    assert p.node_dir == want


def test_a_machine_that_cannot_be_looked_at_is_named_without_its_address(registry):
    def run(target: remote.Machine, command: str) -> tuple[int, str]:
        return 255, f"ssh: connect to host {HOST} port {PORT}: Connection refused"

    with pytest.raises(InstallError) as caught:
        install.plan("box", run=run)

    assert str(caught.value) == "machine 'Lab box' (box) could not be reached"


def test_a_look_that_failed_past_ssh_says_so(registry):
    with pytest.raises(InstallError, match=r"^could not look at machine 'Lab box' \(box\)$"):
        install.plan("box", run=lambda target, command: (2, "df: weird"))


def test_with_the_lane_off_a_plan_is_refused_before_any_ssh(registry, monkeypatch):
    monkeypatch.delenv("RAVEN_DEBUG_MACHINES")

    with pytest.raises(
        InstallError, match="the machine lane is debug-only and off; start raven with RAVEN_DEBUG_MACHINES=1"
    ):
        install.plan("box", run=lambda target, command: pytest.fail("no ssh with the lane off"))


def test_a_machine_that_is_not_registered_is_refused_before_any_ssh(registry):
    with pytest.raises(InstallError, match="machine 'elsewhere' is not registered"):
        install.plan("elsewhere", run=lambda target, command: pytest.fail("no ssh for an unknown machine"))


# --- what an install would do -----------------------------------------------------


def test_an_install_with_the_machine_s_own_python_is_described_before_it_runs():
    p = _plan(free_kb=2 * 1024 * 1024)

    said = p.describe()

    assert p.how == "python" and p.needed_kb == 60 * 1024
    assert said.startswith(
        f"Install the Raven node {bundle.install_name()} on machine 'Lab box' (box) into ~/.raven-node/{bundle.install_name()}: "
        "a virtualenv of the machine's own /usr/bin/python3.12, "
    )
    for pin in bundle.pinned((*protocol.REQUIREMENTS, protocol.SEARCH_REQUIREMENT)):
        assert pin in said
    assert "About 60 MB; 2 GB free there. Nothing else on the machine changes." in said
    assert HOST not in said


def test_an_install_through_uv_needs_room_for_a_python_too():
    p = _plan(python="", uv="/usr/bin/uv", free_kb=None)

    assert p.how == "uv" and p.needed_kb == 140 * 1024
    assert "a Python 3.12 made by uv, kept beside it" in p.describe()
    assert p.describe().endswith("About 140 MB. Nothing else on the machine changes.")


def test_a_machine_without_uv_gets_one_first_and_the_room_for_it_is_counted():
    p = _plan(uv="", free_kb=None)

    assert p.uv_from == "fetched" and p.how == "python" and p.needed_kb == 85 * 1024
    said = p.describe()
    assert (
        "uv itself first, under ~/.raven-node/.uv (the machine has none), fetched with its own /usr/bin/curl;" in said
    )
    assert "a virtualenv of the machine's own /usr/bin/python3.12" in said and "all through uv" in said
    assert _plan(uv="", fetch="").how == "" and _plan(uv="", fetch="").uv_from == ""


def test_the_machine_s_own_python_wins_over_uv():
    assert _plan(python="/usr/bin/python3.12", uv="/usr/bin/uv").how == "python"


@pytest.mark.parametrize(
    ("fields", "said"),
    [
        (
            {"python": "", "uv": "", "fetch": ""},
            "machine 'Lab box' (box) has no uv, and no curl or wget to fetch one with; "
            f"install uv there ({install.UV_INSTALLER}), then run this again",
        ),
        (
            {"uv": "", "fetch": ""},
            "machine 'Lab box' (box) has no uv, and no curl or wget to fetch one with; "
            f"install uv there ({install.UV_INSTALLER}), then run this again",
        ),
        (
            {"free_kb": 10 * 1024},
            "~/.raven-node on machine 'Lab box' (box) has 10 MB free and a node needs about 60 MB; "
            "free some space or choose another directory with --dir",
        ),
        (
            {"python": "", "free_kb": 100 * 1024},
            "~/.raven-node on machine 'Lab box' (box) has 100 MB free and a node needs about 140 MB; "
            "free some space or choose another directory with --dir",
        ),
        (
            {"uv": "", "free_kb": 70 * 1024},
            "~/.raven-node on machine 'Lab box' (box) has 70 MB free and a node needs about 85 MB; "
            "free some space or choose another directory with --dir",
        ),
        ({"free_kb": 60 * 1024}, None),
        ({"free_kb": None}, None),
    ],
)
def test_an_install_that_cannot_go_ahead_says_why(fields, said):
    assert install.refusal(_plan(**fields)) == said


_UV_ENV = 'UV_CACHE_DIR="$dir/.uv-cache" UV_PYTHON_INSTALL_DIR="$dir/.uv-python"'


def test_the_steps_build_a_virtualenv_with_uv_then_fill_it_with_uv():
    steps = install.steps(_plan(python="/opt/py 3.12/bin/python3", uv="/opt/uv"))

    assert [name for name, _ in steps] == ["virtualenv", "packages", "search", "code", "path", "check"]
    head = "dir=~/.raven-node; id=" + bundle.install_name() + '; node="$dir/$id"; uv=/opt/uv; '
    assert all(command.startswith(head) for _, command in steps)
    commands = dict(steps)
    assert commands["virtualenv"] == (
        head + f'mkdir -p "$dir" && {_UV_ENV} "$uv" venv --quiet --python \'/opt/py 3.12/bin/python3\' "$node"'
    )
    pip = f'{_UV_ENV} "$uv" pip install --quiet --python "$node/bin/python"'
    assert commands["packages"] == head + f"{pip} " + " ".join(bundle.pinned(protocol.REQUIREMENTS))
    assert "python -m pip" not in " ".join(commands.values()) and " -m venv " not in " ".join(commands.values())
    (rg,) = bundle.pinned((protocol.SEARCH_REQUIREMENT,))
    assert commands["search"].count(rg) == 2 and commands["search"].count(pip) == 2
    assert f"--extra-index-url {protocol.PUBLIC_INDEX}" in commands["search"]
    assert commands["search"].endswith(">/dev/null 2>&1 && echo RG=1 || echo RG=0")
    assert commands["code"] == head + 'mkdir -p "$node/lib" && tar -xzf - -C "$node/lib"'
    # ``-I``: the machine user's PYTHONPATH or PYTHONHOME reaches neither the
    # check nor, with the same flag on the launch line, the node.
    assert commands["check"] == head + '"$node/bin/python" -I -m raven.node --version'


def test_uv_makes_its_python_inside_the_node_dir():
    commands = dict(install.steps(_plan(python="", uv="/root/.local/bin/uv")))

    assert commands["virtualenv"].endswith(
        f'uv=/root/.local/bin/uv; mkdir -p "$dir" && {_UV_ENV} "$uv" venv --quiet --python 3.12 "$node"'
    )


@pytest.mark.parametrize(
    ("fetch", "fetched_with"),
    [("/usr/bin/curl", f"curl -LsSf {install.UV_INSTALLER}"), ("/usr/bin/wget", f"wget -qO- {install.UV_INSTALLER}")],
)
def test_a_machine_without_uv_fetches_it_under_the_node_dir_first(fetch, fetched_with):
    steps = install.steps(_plan(uv="", fetch=fetch))

    assert [name for name, _ in steps] == ["uv", "virtualenv", "packages", "search", "code", "path", "check"]
    head = "dir=~/.raven-node; id=" + bundle.install_name() + '; node="$dir/$id"; uv="$dir/.uv/uv"; '
    assert all(command.startswith(head) for _, command in steps)
    assert dict(steps)["uv"] == (
        head + f'mkdir -p "$dir/.uv" && {fetched_with} | UV_INSTALL_DIR="$dir/.uv" UV_NO_MODIFY_PATH=1 '
        'INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null && test -x "$dir/.uv/uv"'
    )
    assert dict(steps)["virtualenv"].endswith('"$uv" venv --quiet --python /usr/bin/python3.12 "$node"')


# --- carrying it out --------------------------------------------------------------


def test_an_install_runs_every_step_and_measures_what_it_made(small_tarball):
    machine = Machine()

    done = install.install(_plan(), run=machine.run, push=machine.push)

    assert done == install.Installed("~/.raven-node", bundle.install_name(), 51234, rg=True)
    assert machine.sent[:-1] == [command for _, command in install.steps(_plan())]
    assert machine.sent[-1].endswith('du -sk "$node" 2>/dev/null | cut -f1')
    assert machine.pushed == [small_tarball]
    assert not any('rm -rf "$node"' in command for command in machine.sent)


def test_an_install_without_rg_still_finishes(small_tarball):
    machine = Machine(search=(0, "RG=0\n"))

    assert install.install(_plan(), run=machine.run, push=machine.push).rg is False


@pytest.mark.parametrize("du", [(1, ""), (0, ""), (0, "du: cannot access\n")])
def test_a_size_that_cannot_be_read_is_left_unsaid(small_tarball, du):
    machine = Machine(du=du)

    assert install.install(_plan(), run=machine.run, push=machine.push).size_kb is None


@pytest.mark.parametrize(
    ("step", "answer", "said"),
    [
        (
            "packages",
            (1, "Collecting pillow\nERROR: No matching distribution found for pillow==99.0\n"),
            "installing the Raven node on machine 'Lab box' (box) failed at the packages step: "
            "ERROR: No matching distribution found for pillow==99.0",
        ),
        (
            "virtualenv",
            (255, f"worker@{HOST}: Permission denied (publickey)."),
            "installing the Raven node: machine 'Lab box' (box) refused the login; check the key it is registered with",
        ),
        (
            "virtualenv",
            (127, "sh: 1: /usr/bin/python3.12: not found"),
            "installing the Raven node on machine 'Lab box' (box) failed at the virtualenv step: "
            "sh: 1: /usr/bin/python3.12: not found",
        ),
        (
            "code",
            (2, "tar: Unexpected EOF in archive\n"),
            "installing the Raven node on machine 'Lab box' (box) failed at the code step: "
            "tar: Unexpected EOF in archive",
        ),
        (
            "path",
            (1, ""),
            "installing the Raven node on machine 'Lab box' (box) failed at the path step: exit 1",
        ),
        ("check", (124, ""), "installing the Raven node: machine 'Lab box' (box) did not answer in time"),
    ],
)
def test_a_step_that_fails_stops_the_install_and_removes_the_half_made_node(small_tarball, step, answer, said):
    machine = Machine(**{step: answer})

    with pytest.raises(InstallError) as caught:
        install.install(_plan(), run=machine.run, push=machine.push)

    assert str(caught.value) == said
    assert HOST not in str(caught.value)
    names = [name for name, _ in install.steps(_plan())]
    ran = [name for name, command in install.steps(_plan()) if command in machine.sent]
    assert ran == names[: names.index(step) + 1], "nothing runs after the step that failed"
    assert machine.sent[-1].endswith('rm -rf "$node"')


@pytest.mark.parametrize(
    ("check", "reported"),
    [
        (json.dumps({"protocol": protocol.PROTOCOL, "digest": "000000000000"}), "000000000000"),
        (json.dumps({"protocol": protocol.PROTOCOL + 1, "digest": "x"}), "x"),
        ("Traceback (most recent call last):\n{not json", "nothing"),
        ("", "nothing"),
    ],
)
def test_a_node_that_does_not_report_this_raven_s_code_is_removed(small_tarball, check, reported):
    machine = Machine()
    machine.check = check

    with pytest.raises(InstallError) as caught:
        install.install(_plan(), run=machine.run, push=machine.push)

    assert str(caught.value) == (
        "the Raven node installed on machine 'Lab box' (box) does not report this Raven's code "
        f"({reported} instead of {bundle.digest()})"
    )
    assert machine.sent[-1].endswith('rm -rf "$node"')


def test_the_last_json_line_is_the_one_checked(small_tarball):
    machine = Machine()
    good = json.dumps({"protocol": protocol.PROTOCOL, "digest": bundle.digest()})
    machine.check = f'{{"stale": true}}\nsome warning\n{good}\n'

    assert install.install(_plan(), run=machine.run, push=machine.push).install_name == bundle.install_name()


def test_an_install_that_cannot_go_ahead_sends_nothing():
    machine = Machine()

    with pytest.raises(InstallError, match="has no uv, and no curl or wget"):
        install.install(_plan(python="", uv="", fetch=""), run=machine.run, push=machine.push)

    assert machine.sent == []


def test_an_install_interrupted_by_an_error_still_cleans_up(small_tarball):
    machine = Machine()

    def run(target: remote.Machine, command: str) -> tuple[int, str]:
        machine.sent.append(command)
        if " venv " in command:
            raise remote.RemoteMachineError("machine 'Lab box' (box): no key")
        return 0, ""

    with pytest.raises(remote.RemoteMachineError):
        install.install(_plan(), run=run, push=machine.push)

    assert machine.sent[-1].endswith('rm -rf "$node"')


def test_prune_reports_the_nodes_it_removed():
    sent: list[str] = []

    def run(target: remote.Machine, command: str) -> tuple[int, str]:
        sent.append(command)
        return 0, "PRUNED=0.2.3-aaaaaaaaaaaa\n  PRUNED=0.2.4-bbbbbbbbbbbb\nnoise\n"

    assert install.prune(_plan(), run=run) == ("0.2.3-aaaaaaaaaaaa", "0.2.4-bbbbbbbbbbbb")
    assert sent == [install.prune_script("~/.raven-node", bundle.install_name())]


def test_a_prune_that_failed_says_where():
    with pytest.raises(InstallError, match=r"failed at the prune step: rm: cannot remove 'x': Busy$"):
        install.prune(_plan(), run=lambda target, command: (1, "rm: cannot remove 'x': Busy"))


# --- the scripts, under a real shell ----------------------------------------------


def _sh(command: str, home: Path, path: str) -> str:
    done = subprocess.run(
        ["/bin/sh", "-c", command],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": path},
        cwd=home,
        timeout=60,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


def _script(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


@pytest.fixture
def shell(tmp_path: Path):
    """A home and a PATH holding only what the scripts call, plus stand-in Pythons."""
    if not Path("/bin/sh").exists():
        pytest.skip("the scripts run under /bin/sh")
    home = tmp_path / "home"
    home.mkdir()
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("dirname", "df", "awk", "grep"):
        found = shutil.which(name)
        assert found, name
        (tools / name).symlink_to(found)
    return home, tools


def _lines(out: str) -> dict[str, str]:
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


@pytest.mark.slow
def test_the_probe_finds_the_node_a_new_enough_python_uv_and_the_room(shell):
    home, tools = shell
    _script(tools / "python3.13", "exit 1\n")  # too old
    _script(tools / "python3.12", "exit 0\n")
    _script(tools / "uv", "exit 0\n")
    _script(tools / "curl", "exit 0\n")
    digest = bundle.digest()
    _script(
        home / ".raven-node" / "0.1-x" / "bin" / "python",
        f"echo '{json.dumps({'protocol': protocol.PROTOCOL, 'digest': digest})}'\n",
    )

    seen = _lines(_sh(install.probe_script("~/.raven-node", "0.1-x", digest), home, str(tools)))

    assert seen["PRESENT"] == "1"
    assert seen["PYTHON"] == str(tools / "python3.12")
    assert seen["UV"] == str(tools / "uv")
    assert seen["FETCH"] == str(tools / "curl")
    assert seen["FREE_KB"].isdigit()


@pytest.mark.slow
def test_the_probe_finds_the_uv_an_earlier_install_put_under_the_node_dir(shell):
    home, tools = shell
    _script(home / "nodes" / ".uv" / "uv", "exit 0\n")

    seen = _lines(_sh(install.probe_script(str(home / "nodes"), "0.1-x", "d"), home, str(tools)))

    assert seen["UV"] == str(home / "nodes" / ".uv" / "uv") and "FETCH" not in seen


@pytest.mark.slow
def test_the_probe_does_not_take_a_node_of_other_code_for_this_one(shell):
    home, tools = shell
    _script(
        home / ".raven-node" / "0.1-x" / "bin" / "python",
        f"echo '{json.dumps({'protocol': protocol.PROTOCOL, 'digest': '000000000000'})}'\n",
    )

    seen = _lines(_sh(install.probe_script("~/.raven-node", "0.1-x", bundle.digest()), home, str(tools)))

    assert "PRESENT" not in seen and "PYTHON" not in seen and "UV" not in seen
    assert seen["FREE_KB"].isdigit(), "room is measured on the nearest directory that exists"


@pytest.mark.slow
def test_the_probe_finds_a_uv_the_non_login_path_misses(shell):
    home, tools = shell
    _script(home / ".local" / "bin" / "uv", "exit 0\n")

    seen = _lines(_sh(install.probe_script("/no/such/dir/nodes", "0.1-x", "d"), home, str(tools)))

    assert seen["UV"] == str(home / ".local" / "bin" / "uv")
    assert seen["FREE_KB"].isdigit()


@pytest.mark.slow
@pytest.mark.parametrize(("uv_rc", "said", "tries"), [(0, "RG=1", 1), (1, "RG=0", 2)])
def test_the_search_package_never_fails_the_install(shell, tmp_path, uv_rc, said, tries):
    home, tools = shell
    calls = tmp_path / "uv-calls"
    nodes = tmp_path / "nodes"
    uv = _script(tools / "uv", f'echo "$@" >> "{calls}"; exit {uv_rc}\n')

    search = dict(install.steps(_plan(node_dir=str(nodes), install_name="0.1-x", uv=str(uv))))["search"]

    assert _sh(search, home, str(tools)).strip() == said
    asked = calls.read_text(encoding="utf-8").splitlines()
    assert len(asked) == tries
    assert asked[0].startswith("pip install --quiet --python ") and "--extra-index-url" not in asked[0]
    if tries == 2:
        assert f"--extra-index-url {protocol.PUBLIC_INDEX}" in asked[1]


@pytest.mark.slow
def test_prune_removes_only_other_nodes(shell, tmp_path):
    home, tools = shell
    nodes = tmp_path / "nodes dir"
    for name in ("0.2.4-current", "0.2.3-old", "0.2.4-older"):
        _script(nodes / name / "bin" / "python", "exit 0\n")
        (nodes / name / "lib" / "raven").mkdir(parents=True)
    _script(nodes / "notes" / "bin" / "python", "exit 0\n")  # no lib/raven: not a node
    (nodes / ".uv-python" / "cpython-3.12").mkdir(parents=True)
    (nodes / "README").write_text("the owner's own file\n", encoding="utf-8")

    out = _sh(install.prune_script(str(nodes), "0.2.4-current"), home, str(tools) + ":/bin:/usr/bin")

    assert sorted(out.splitlines()) == ["PRUNED=0.2.3-old", "PRUNED=0.2.4-older"]
    assert sorted(p.name for p in nodes.iterdir()) == [".uv-python", "0.2.4-current", "README", "notes"]


@pytest.mark.slow
def test_prune_of_a_node_dir_that_does_not_exist_removes_nothing(shell, tmp_path):
    home, tools = shell

    assert _sh(install.prune_script(str(tmp_path / "absent"), "0.1-x"), home, str(tools) + ":/bin:/usr/bin") == ""


# --- one real install, on this computer -------------------------------------------


@pytest.mark.slow
def test_a_real_install_makes_a_node_that_reports_this_raven_s_code(registry, tmp_path):
    if not Path("/bin/sh").exists():
        pytest.skip("the install steps run under /bin/sh")
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}

    def run(target: remote.Machine, command: str) -> tuple[int, str]:
        if "pip install" in command:
            # The package index is the one thing not reached from a test.
            return 0, "RG=0\n" if "RG=1" in command else ""
        done = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, env=env, cwd=home)
        return done.returncode, done.stdout + (f"\n{done.stderr}" if done.returncode else "")

    def push(target: remote.Machine, command: str, data: bytes) -> tuple[int, str]:
        done = subprocess.run(["/bin/sh", "-c", command], input=data, capture_output=True, env=env, cwd=home)
        return done.returncode, done.stdout.decode() + (f"\n{done.stderr.decode()}" if done.returncode else "")

    nodes = tmp_path / "nodes"
    _script(nodes / "0.0.1-old" / "bin" / "python", "exit 0\n")
    (nodes / "0.0.1-old" / "lib" / "raven").mkdir(parents=True)

    first = install.plan("box", node_dir=str(nodes), run=run)
    assert not first.present
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("the install goes through uv, which this computer does not have")
    p = dataclasses.replace(first, python=sys.executable, uv=uv)
    done = install.install(p, run=run, push=push)

    assert done.install_name == bundle.install_name() and done.rg is False and (done.size_kb or 0) > 1000
    node = nodes / bundle.install_name()
    reported = subprocess.run(
        [str(node / "bin" / "python"), "-m", "raven.node", "--version"],
        capture_output=True,
        text=True,
        env=env,
        cwd=home,
        check=True,
    ).stdout
    assert json.loads(reported) == {"protocol": protocol.PROTOCOL, "digest": bundle.digest()}
    assert bundle.tree_digest(node / "lib" / "raven") == bundle.digest()
    assert install.plan("box", node_dir=str(nodes), run=run).present, "the next look finds it"

    assert install.prune(p, run=run) == ("0.0.1-old",)
    # uv's cache stays beside the install, for the next one; nothing else does.
    assert sorted(path.name for path in nodes.iterdir()) == [".uv-cache", bundle.install_name()]
