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
import stat
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

_TUNA, _ALIYUN = install.MIRRORS


@pytest.fixture(autouse=True)
def _untimed(monkeypatch: pytest.MonkeyPatch) -> None:
    """No look times an index from here: under a real shell its curl would reach PyPI and the mirrors.

    The look each test was written against; the tests of the timing clear it (``timed``).
    """
    monkeypatch.setenv(install.NO_MIRROR_ENV, "1")


@pytest.fixture
def timed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(install.NO_MIRROR_ENV, raising=False)


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "connections.json"

    def write(**extra: Any) -> None:
        path.write_text(json.dumps({"connections": [{**ROW, **extra}]}), encoding="utf-8")

    write()
    monkeypatch.setenv("RAVEN_CONNECTIONS", str(path))
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
        # What a probe of that uv reports: one recent enough to read a configured index.
        "uv_version": install.UV_VERSION,
        "free_kb": 10 * 1024 * 1024,
        "fetch": "/usr/bin/curl",
    }
    return Plan(**{**base, **fields})


class Machine:
    """Answers each step by name, records what it was sent, and fails where told."""

    def __init__(self, *, plan: Plan | None = None, **answers: tuple[int, str]) -> None:
        self.answers = answers
        self.plans = [plan] if plan is not None else [_plan(), _plan(python="", uv="/usr/bin/uv")]
        """The plans whose steps are answered by name."""
        self.sent: list[str] = []
        self.pushed: list[bytes] = []
        self.pushed_by: list[tuple[str, bytes]] = []
        """Each push with the command it fed: what reached a step's stdin."""
        self.check = json.dumps({"protocol": protocol.PROTOCOL, "digest": bundle.digest()})

    def _answer(self, command: str) -> tuple[int, str]:
        for name, cmd in [step for p in self.plans for step in install.steps(p)]:
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
        self.pushed_by.append((command, data))
        if command.endswith('tar -xzf - -C "$node/lib"'):
            return self.answers.get("code", (0, ""))
        return self._answer(command)


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
            "bash: no job control\nPRESENT=1\nPYTHON=/usr/bin/python3.12\nUV=/root/.local/bin/uv\nUV_VERSION=0.13.0\n"
            "FREE_KB=2097152\n",
        )

    p = install.plan("box", run=run)

    assert (p.node_dir, p.install_name, p.present, p.python, p.uv, p.uv_version, p.free_kb) == (
        "~/.raven-node",
        bundle.install_name(),
        True,
        "/usr/bin/python3.12",
        "/root/.local/bin/uv",
        "0.13.0",
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


def test_an_install_plan_needs_no_debug_switch(registry, monkeypatch):
    monkeypatch.delenv("RAVEN_DEBUG_MACHINES", raising=False)
    asked = []

    def run(target, command):
        asked.append(target.id)
        return 0, "PYTHON=/usr/bin/python3.12\nUV=/usr/bin/uv\nFREE_KB=1048576\n"

    plan = install.plan("box", run=run)
    assert plan.target.id == "box" and asked == ["box"]


def test_a_machine_that_is_not_registered_is_refused_before_any_ssh(registry):
    with pytest.raises(InstallError, match="machine 'elsewhere' is not registered"):
        install.plan("elsewhere", run=lambda target, command: pytest.fail("no ssh for an unknown machine"))


# --- the index timed from the machine, and the mirrors ------------------------------

_LOOK = "PYTHON=/usr/bin/python3.12\nUV=/usr/bin/uv\nUV_VERSION=0.13.0\nFETCH=/usr/bin/curl\nFREE_KB=2097152\n"


def _says(look: str, mirrors: str = "", mirrors_rc: int = 0) -> tuple[list[str], Any]:
    """A machine that answers the look with ``look`` and the mirrors' timing with ``mirrors``."""
    sent: list[str] = []

    def run(target: remote.Machine, command: str) -> tuple[int, str]:
        sent.append(command)
        return (mirrors_rc, mirrors) if command == install.mirror_script() else (0, look)

    return sent, run


def test_an_index_slow_from_the_machine_gives_way_to_the_fastest_mirror_and_the_plan_says_why(registry, timed):
    """Measured 2026-10-23 on an Alibaba Cloud machine in Shanghai whose pip named PyPI: pillow's page at 31 KB/s
    from pypi.org, cut off at six seconds, and whole within a second from either mirror."""
    sent, run = _says(
        _LOOK + "INDEX=https://pypi.org/simple\nSPEED=28 200 190464 6.000000\n",
        f"MIRROR=0 200 1165208 0.081513 {_ALIYUN}\nMIRROR=0 200 1165206 0.760254 {_TUNA}\n",
    )

    p = install.plan("box", run=run)

    assert (p.index, p.mirror, p.passed_over) == ("https://pypi.org/simple", _ALIYUN, "served 31 KB/s")
    look, mirrors = sent
    assert "curl -K - " in look and mirrors == install.mirror_script()
    assert all(m in mirrors for m in install.MIRRORS)
    assert (
        f"from {_ALIYUN} (a PyPI mirror in mainland China, the fastest from the machine: https://pypi.org/simple, the "
        "index pip is configured with there, served 31 KB/s, and is tried after it), and this Raven's code"
    ) in p.describe()


@pytest.mark.parametrize("rc", sorted(install._UNREACHED_RC))
def test_pypi_the_network_kept_curl_from_gives_way_to_any_mirror_that_answers(registry, timed, rc):
    _, run = _says(
        _LOOK + f"SPEED={rc} 000 0 5.001234\n",
        f"MIRROR=28 200 81920 6.000000 {_TUNA}\nMIRROR=7 000 0 5.001000 {_ALIYUN}\n",
    )

    p = install.plan("box", run=run)

    assert (p.index, p.mirror, p.passed_over) == ("", _TUNA, "could not be reached")
    assert "PyPI, which pip uses there, could not be reached, and is tried after it" in p.describe()


def test_a_mirror_that_answers_with_an_error_is_not_taken_however_quick(registry, timed):
    """A 404 arrives fast; a mirror that does not carry the page carries none of the packages."""
    _, run = _says(
        _LOOK + "SPEED=7 000 0 5.001234\n",
        f"MIRROR=0 404 50000 0.010000 {_TUNA}\nMIRROR=28 200 300000 6.000000 {_ALIYUN}\n",
    )

    assert install.plan("box", run=run).mirror == _ALIYUN


_OLD_UV = "PYTHON=/usr/bin/python3.12\nUV=/usr/bin/uv\nUV_VERSION=0.4.0\nFETCH=/usr/bin/curl\nFREE_KB=2097152\n"


def test_a_mirror_is_never_the_index_a_uv_too_old_to_read_one_is_given(registry, timed):
    """0xKT, 2026-10-23: with pip naming no index and PyPI passed over, the mirror reaches uv only through
    UV_DEFAULT_INDEX, which a uv older than 0.4.23 ignores -- so the plan said the mirror while both tries went to
    PyPI. The uv the steps use is settled after the mirror now, and such a uv is fetched first."""
    _, run = _says(
        _OLD_UV + "SPEED=7 000 0 5.001000\n",
        f"MIRROR=0 200 1165206 0.760254 {_TUNA}\n",
    )

    p = install.plan("box", run=run)

    assert (p.index, p.mirror, p.uv_too_old, p.uv_from) == ("", _TUNA, True, "fetched")
    assert "the machine has uv 0.4.0, and only uv 0.4.23 or later installs from the index an install gives it" in (
        p.describe()
    )
    commands = dict(install.steps(p))
    assert "uv" in commands and 'uv="$dir/.uv/uv"; ' in commands["packages"], "the pinned uv runs the packages"
    # A uv new enough reads the mirror as it is, and is not replaced.
    _, run = _says(_LOOK + "SPEED=7 000 0 5.001000\n", f"MIRROR=0 200 1165206 0.760254 {_TUNA}\n")
    new = install.plan("box", run=run)
    assert (new.mirror, new.uv_too_old, new.uv_from) == (_TUNA, False, "machine")
    assert "uv" not in dict(install.steps(new))


def test_a_mirror_the_steps_give_an_old_uv_takes_the_one_an_earlier_install_fetched(registry, timed):
    look = (
        "PYTHON=/usr/bin/python3.12\nUV=/usr/local/bin/uv\nUV_VERSION=0.4.22\n"
        "NODE_UV=/home/worker/.raven-node/.uv/uv\nNODE_UV_VERSION=0.13.0\nFETCH=/usr/bin/curl\n"
        "FREE_KB=2097152\nSPEED=7 000 0 5.001000\n"
    )
    _, run = _says(look, f"MIRROR=0 200 1165206 0.760254 {_TUNA}\n")

    p = install.plan("box", run=run)

    assert (p.mirror, p.uv, p.uv_from) == (_TUNA, "/home/worker/.raven-node/.uv/uv", "machine")
    assert "uv" not in dict(install.steps(p)), "nothing is fetched twice"
    assert "uv=/home/worker/.raven-node/.uv/uv; " in dict(install.steps(p))["packages"]


def test_a_mirror_for_an_old_uv_with_nothing_to_fetch_one_with_is_refused():
    p = _plan(index="", mirror=_TUNA, uv="/usr/bin/uv", uv_version="0.4.0", fetch="")

    assert install.refusal(p) == (
        "machine 'Lab box' (box) has uv 0.4.0, and only uv 0.4.23 or later installs from the index an install "
        f"gives it there; with no curl or wget to fetch uv {install.UV_VERSION}, upgrade uv there "
        f"({install.UV_INSTALLER}), then run this again"
    )


@pytest.mark.parametrize(
    ("mirrors", "rc"),
    [
        (f"MIRROR=28 200 300000 6.000000 {_TUNA}\nMIRROR=7 000 0 5.000000 {_ALIYUN}\n", 0),
        (f"MIRROR=0 404 120 0.100000 {_TUNA}\nMIRROR=60 000 0 0.300000 {_ALIYUN}\n", 0),
        ("MIRROR=0 200 1165208 0.081513 https://elsewhere.example/simple\nMIRROR=0 200 garbage\n", 0),
        ("", 255),
    ],
    ids=["no faster", "no answer", "not one of the mirrors", "the look failed"],
)
def test_a_slow_index_no_mirror_beats_is_kept_and_the_plan_says_how_it_was_timed(registry, timed, mirrors, rc):
    _, run = _says(_LOOK + "INDEX=https://mirror.example/simple\nSPEED=28 200 600000 6.000000\n", mirrors, rc)

    p = install.plan("box", run=run)

    assert (p.mirror, p.passed_over) == ("", "served 97 KB/s"), "100000 B/s, rounded down"
    assert (
        "from https://mirror.example/simple (the index pip is configured with there; it served 97 KB/s from the "
        "machine, and no PyPI mirror in mainland China did better), and this Raven's code"
    ) in p.describe()
    assert install.steps(p) == install.steps(dataclasses.replace(p, passed_over="")), "the steps are as untimed"


@pytest.mark.parametrize(
    "speed",
    [
        "0 200 1919633 0.699450",
        "28 200 1700000 6.000000",
        "0 200 5400 0.157000",
        "0 404 120 0.200000",
        "28 503 2048 6.000000",
        "0 401 0 0.200000",
        "60 000 0 0.400000",
        "1 000 0 0.010000",
        "127",
        None,
    ],
    ids=[
        "whole in under a second",
        "cut off at 277 KB/s",
        "a small page, quickly",
        "an error status",
        "an error status, slowly",
        "a sign-in curl did not have",
        "a certificate the machine does not trust",
        "a curl that cannot speak https",
        "curl said nothing",
        "not timed: no curl there",
    ],
)
def test_an_index_that_serves_or_answered_is_kept_and_no_mirror_is_asked(registry, timed, speed):
    """Only an index the network kept curl from, or one that answers slowly, is passed over: one that answered
    with an error or asked for a sign-in is the owner's to mend, one curl failed on for a reason of its own was
    not timed, and a mirror would hide why the install failed."""
    sent, run = _says(_LOOK + "INDEX=https://pypi.org/simple\n" + (f"SPEED={speed}\n" if speed else ""))

    p = install.plan("box", run=run)

    assert (p.mirror, p.passed_over) == ("", "") and len(sent) == 1


@pytest.mark.parametrize("value", ["1", "true", " Yes ", "ON"])
def test_raven_no_mirror_times_nothing_and_asks_no_mirror(registry, monkeypatch, value):
    monkeypatch.setenv(install.NO_MIRROR_ENV, value)
    sent, run = _says(_LOOK + "SPEED=7 000 0 5.000000\n")

    p = install.plan("box", run=run)

    assert (p.mirror, p.passed_over) == ("", "") and len(sent) == 1
    assert "curl -K - " not in sent[0] and "SPEED=" not in sent[0]


def test_a_machine_that_has_the_node_or_cannot_take_one_asks_no_mirror(registry, timed):
    for look in (
        "PRESENT=1\nUV=/usr/bin/uv\nSPEED=7 000 0 5.000000\n",
        "PYTHON=/usr/bin/python3.12\nSPEED=7 000 0 5.000000\nFREE_KB=2097152\n",
        _LOOK.replace("FREE_KB=2097152", "FREE_KB=1024") + "SPEED=7 000 0 5.000000\n",
    ):
        sent, run = _says(look)
        p = install.plan("box", run=run)
        assert p.mirror == "" and len(sent) == 1
        assert p.present or install.refusal(p) is not None


@pytest.mark.parametrize(
    ("said", "timing"),
    [
        ("28 200 662835 6.000473", (28, 200, 662835, 6.000473)),
        ("0 200 1165206 0,760254", (0, 200, 1165206, 0.760254)),
        ("7 000 0 5.001", (7, 0, 0, 5.001)),
        ("127", None),
        ("", None),
        ("0 200 x 0.1", None),
    ],
)
def test_a_timing_is_read_as_curl_and_the_shell_said_it(said, timing):
    got = install._timing(said)

    assert (got and (got.rc, got.status, got.size, got.seconds)) == timing


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
        f"uv {install.UV_VERSION} itself first, under ~/.raven-node/.uv (the machine has none), fetched with its own "
        "/usr/bin/curl;" in said
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
# node_dir made owner-only only when the step makes it; one that is there is left as it is.
_MADE = '[ -d "$dir" ] || { mkdir -p "$dir" && chmod 700 "$dir"; }; '
# The half-made Raven node a step given up on left behind, gone before uv venv, which refuses a directory that is there.
_CLEARED = 'rm -rf "$node" && '


def test_the_steps_build_a_virtualenv_with_uv_then_fill_it_with_uv():
    steps = install.steps(_plan(python="/opt/py 3.12/bin/python3", uv="/opt/uv"))

    assert [name for name, _ in steps] == ["virtualenv", "packages", "search", "code", "path", "check"]
    head = "dir=~/.raven-node; id=" + bundle.install_name() + '; node="$dir/$id"; uv=/opt/uv; '
    assert all(command.startswith(head) for _, command in steps)
    commands = dict(steps)
    assert commands["virtualenv"] == (
        head + _MADE + _CLEARED + f'{_UV_ENV} "$uv" venv --quiet --python \'/opt/py 3.12/bin/python3\' "$node"'
    )
    present = dict(install.steps(_plan(python="/opt/py 3.12/bin/python3", uv="/opt/uv", present=True)))
    assert "rm -rf" not in present["virtualenv"], "a Raven node the look found working is never removed"
    pip = f'{_UV_ENV} "$uv" pip install --quiet --python "$node/bin/python"'
    assert commands["packages"] == head + f"{pip} " + " ".join(bundle.pinned(protocol.REQUIREMENTS))
    assert "--default-index" not in " ".join(commands.values()), "no index configured for pip: uv's own default"
    assert "UV_DEFAULT_INDEX" not in " ".join(commands.values()), "and nothing is read off a step's stdin for one"
    assert "python -m pip" not in " ".join(commands.values()) and " -m venv " not in " ".join(commands.values())
    (rg,) = bundle.pinned((protocol.SEARCH_REQUIREMENT,))
    assert commands["search"].count(rg) == 2 and commands["search"].count(pip) == 2
    assert f"--extra-index-url {protocol.PUBLIC_INDEX}" in commands["search"]
    assert commands["search"].endswith(">/dev/null 2>&1 && echo RG=1 || echo RG=0")
    assert commands["code"] == head + 'mkdir -p "$node/lib" && tar -xzf - -C "$node/lib"'
    # ``-I``: the machine user's PYTHONPATH or PYTHONHOME reaches neither the
    # check nor, with the same flag on the launch line, the node.
    assert commands["check"] == head + '"$node/bin/python" -I -m raven.node --version'


def test_the_node_is_told_the_version_its_directory_is_named_by():
    """A node runs this code from ``lib/``, not as an installed distribution, so it has no version of its own to
    read; the install writes the one the host named its directory by beside the code."""
    commands = dict(install.steps(_plan(install_name="0.2.4-abcdef123456")))

    assert commands["path"].endswith(' "$node/lib" 0.2.4')
    assert '"raven-" + version + ".dist-info"' in commands["path"] and "Version: " in commands["path"]
    assert dict(install.steps(_plan(install_name="0.0.0+local-abcdef123456")))["path"].endswith(
        ' "$node/lib" 0.0.0+local'
    )


def test_uv_makes_its_python_inside_the_node_dir():
    commands = dict(install.steps(_plan(python="", uv="/root/.local/bin/uv")))

    assert commands["virtualenv"].endswith(
        f'uv=/root/.local/bin/uv; {_MADE}{_CLEARED}{_UV_ENV} "$uv" venv --quiet --python 3.12 "$node"'
    )


_TAKE_INDEX = "IFS= read -r UV_DEFAULT_INDEX && export UV_DEFAULT_INDEX && "


def test_the_index_pip_is_configured_with_is_read_off_the_step_s_stdin_and_named_in_the_plan():
    p = _plan(index="https://mirror.example/simple")

    commands = dict(install.steps(p))

    head = "dir=~/.raven-node; id=" + bundle.install_name() + '; node="$dir/$id"; uv=/usr/bin/uv; '
    pip = f'{_UV_ENV} "$uv" pip install --quiet --python "$node/bin/python"'
    assert commands["packages"] == head + _TAKE_INDEX + pip + " " + " ".join(bundle.pinned(protocol.REQUIREMENTS))
    # The search package tries the machine's index first, then that index with the public one beside it.
    assert commands["search"].startswith(head + _TAKE_INDEX + "{ " + pip + " ")
    assert commands["search"].count(pip) == 2 and f"--extra-index-url {protocol.PUBLIC_INDEX}" in commands["search"]
    assert not any("mirror.example" in command for command in commands.values()), "no command line names it"
    assert not any("--default-index" in command for command in commands.values())
    others = [name for name in commands if name not in ("packages", "search")]
    assert not any("UV_DEFAULT_INDEX" in commands[name] for name in others), "only the steps that run uv pip read it"
    assert "from https://mirror.example/simple (the index pip is configured with there), and this Raven's code" in (
        p.describe()
    )
    assert "from PyPI (pip is configured with no other index there), and this Raven's code" in _plan().describe()


def test_a_sign_in_inside_the_index_url_reaches_uv_only_on_stdin_never_a_command_line_or_a_sentence(small_tarball):
    """pip takes `https://user:password@host/simple`. A command line on the machine is readable by every account
    there (``/proc/<pid>/cmdline``), and this computer's ssh argv by every account here; the plan is printed before
    the confirmation, and under --yes."""
    index = "https://user:s3cret-pw@mirror.example:8443/simple"
    p = _plan(index=index)
    machine = Machine()

    done = install.install(p, run=machine.run, push=machine.push)

    assert done.rg is True
    assert not any("s3cret-pw" in command or "mirror.example" in command for command in machine.sent)
    names = {command: name for name, command in install.steps(p)}
    assert [(names[command], data) for command, data in machine.pushed_by] == [
        ("packages", index.encode() + b"\n"),
        ("search", index.encode() + b"\n"),
        ("code", small_tarball),
    ], "the two steps that run uv pip read the whole URL, one line, off stdin; the code step its tarball"
    said = p.describe()
    assert (
        "from https://mirror.example:8443/simple (the index pip is configured with there, shown without its sign-in)"
        in said
    )
    assert "s3cret-pw" not in said and "user:" not in said
    failed = install._failed(
        p, "packages", 2, "error: Failed to fetch: `https://user:s3cret-pw@mirror.example:8443/simple/loguru/`"
    )
    assert "s3cret-pw" not in failed and "https://mirror.example:8443/simple/loguru/" in failed
    assert install._shown_index("https://mirror.example/simple") == "https://mirror.example/simple"
    assert install._shown_index("http://u:p@[::1]:8080/simple") == "http://[::1]:8080/simple"


def test_a_mirror_comes_first_on_the_step_s_stdin_and_the_index_it_stands_in_for_second(small_tarball):
    """A mirror that lacks a pinned version fails a step's first try; the machine's own index -- PyPI when pip
    names none -- is its second, as it would have been its only one."""
    for index, then in (("https://user:s3cret-pw@mirror.example:8443/simple", ""), ("", protocol.PUBLIC_INDEX)):
        p = _plan(index=index, mirror=_TUNA, passed_over="could not be reached")
        machine = Machine(plan=p)

        assert install.install(p, run=machine.run, push=machine.push).rg is True

        fed = f"{_TUNA}\n{then or index}\n".encode()
        names = {command: name for name, command in install.steps(p)}
        assert [(names[command], data) for command, data in machine.pushed_by] == [
            ("packages", fed),
            ("search", fed),
            ("code", small_tarball),
        ]
        assert not any(word in command for command in machine.sent for word in ("s3cret-pw", "mirror.example", _TUNA))
    commands = dict(install.steps(_plan(mirror=_TUNA)))
    head = "dir=~/.raven-node; id=" + bundle.install_name() + '; node="$dir/$id"; uv=/usr/bin/uv; '
    pip = f'{_UV_ENV} "$uv" pip install --quiet --python "$node/bin/python"'
    take = "IFS= read -r UV_DEFAULT_INDEX && IFS= read -r own && export UV_DEFAULT_INDEX && "
    pins = " ".join(bundle.pinned(protocol.REQUIREMENTS))
    assert commands["packages"] == head + take + f'{{ {pip} {pins} || UV_DEFAULT_INDEX="$own" {pip} {pins}; }}'
    (rg,) = bundle.pinned((protocol.SEARCH_REQUIREMENT,))
    assert commands["search"] == head + take + (
        f'{{ {pip} {rg} || UV_DEFAULT_INDEX="$own" {pip} --extra-index-url {protocol.PUBLIC_INDEX} {rg}; }}'
        " >/dev/null 2>&1 && echo RG=1 || echo RG=0"
    )


@pytest.mark.parametrize(
    ("fields", "step", "said"),
    [
        ({}, "packages", "the packages step: it did not finish within 15 minutes, installing packages from PyPI"),
        (
            {"index": "https://user:s3cret-pw@mirror.example/simple"},
            "packages",
            "the packages step: it did not finish within 15 minutes, installing packages from "
            "https://mirror.example/simple",
        ),
        (
            {"index": "https://pypi.org/simple", "mirror": _ALIYUN},
            "packages",
            f"the packages step: it did not finish within 15 minutes, installing packages from {_ALIYUN}, then "
            "https://pypi.org/simple",
        ),
        ({}, "code", "the code step: it did not finish within 15 minutes"),
    ],
    ids=["from PyPI", "from the index pip names", "from a mirror, then the index", "no packages"],
)
def test_a_step_out_of_time_says_so_and_where_its_packages_were_coming_from(small_tarball, fields, step, said):
    """Measured 2026-10-22: a packages step still downloading pillow from a slow index when its 15 minutes ran out
    was reported as a machine that did not answer in time."""
    p = _plan(**fields)
    machine = Machine(plan=p, **{step: (124, "")})

    with pytest.raises(InstallError) as caught:
        install.install(p, run=machine.run, push=machine.push)

    assert str(caught.value) == f"installing the Raven node on machine 'Lab box' (box) failed at {said}"
    assert machine.sent[-1].endswith('rm -rf "$node"')


def test_a_search_package_out_of_time_is_left_out_and_the_install_goes_on(small_tarball):
    machine = Machine(search=(124, ""))

    done = install.install(_plan(), run=machine.run, push=machine.push)

    assert done.rg is False
    assert not any(command.endswith('rm -rf "$node"') for command in machine.sent)
    assert machine.sent[-1].endswith('du -sk "$node" 2>/dev/null | cut -f1')


# ---- E2: a port pip's configuration holds that is not a port ---------------------------------


@pytest.mark.parametrize(
    ("url", "shown"),
    [
        ("https://u:pw@mirror.example:84a3/simple", "https://mirror.example:84a3/simple"),
        ("https://u:pw@mirror.example:99999/simple", "https://mirror.example:99999/simple"),
        ("https://mirror.example:84a3/simple", "https://mirror.example:84a3/simple"),
        ("https://u:p@ss@mirror.example/simple", "https://mirror.example/simple"),
        ("https://u@[::1]:x/simple", "https://[::1]:x/simple"),
    ],
    ids=["not a number", "past 65535", "no sign-in", "an @ in the password", "ipv6"],
)
def test_an_index_is_shown_without_its_sign_in_whatever_its_port_says(url, shown):
    assert install._shown_index(url) == shown


@pytest.mark.parametrize("port", ["84a3", "99999"], ids=["not a number", "past 65535"])
def test_an_index_whose_port_is_not_a_port_is_refused_in_one_sentence_by_an_install_before_anything_runs(
    registry, port
):
    """Only an install needs the index, so only an install refuses it: the look takes the machine as it is."""
    looked: list[str] = []

    def run(target: remote.Machine, command: str) -> tuple[int, str]:
        looked.append(command)
        return 0, (
            "PYTHON=/usr/bin/python3.12\nUV=/usr/bin/uv\nUV_VERSION=0.13.0\n"
            f"INDEX=https://user:s3cret-pw@mirror.example:{port}/simple\nFREE_KB=2097152\n"
        )

    p = install.plan("box", run=run)

    said = (
        "the package index pip is configured with on machine 'Lab box' (box) has a port that is not a number "
        f"from 0 to 65535 (https://mirror.example:{port}/simple); correct index-url in pip's configuration there, "
        "or PIP_INDEX_URL"
    )
    assert install.refusal(p) == said
    machine = Machine()
    with pytest.raises(InstallError) as raised:
        install.install(p, run=machine.run, push=machine.push)
    assert str(raised.value) == said
    assert machine.sent == [] and len(looked) == 1, "nothing ran on the machine but the look"


def test_a_machine_that_has_this_node_is_found_so_and_pruned_whatever_port_its_index_names(registry):
    """Neither the look nor a prune needs the index, so a bad port in pip's configuration stops neither:
    install-node says the node is already there, and --prune runs."""
    p = install.plan(
        "box",
        run=lambda target, command: (0, "PRESENT=1\nUV=/usr/bin/uv\nINDEX=https://u:pw@mirror.example:84a3/simple\n"),
    )

    assert p.present
    assert install.prune(p, run=lambda target, command: (0, "PRUNED=0.1-old\n")) == ("0.1-old",)


def test_an_index_whose_port_is_a_port_or_none_goes_ahead(registry):
    """An empty port is the scheme's default (RFC 3986), not a port that is not one."""
    for index in (
        "https://u:pw@mirror.example:8443/simple",
        "https://mirror.example/simple",
        "https://u:pw@mirror.example:/simple",
    ):
        p = install.plan("box", run=lambda target, command, index=index: (0, f"UV=/usr/bin/uv\nINDEX={index}\n"))
        assert p.index == index and "pw" not in p.describe()


_MIRROR = "https://mirror.example/simple"


@pytest.mark.parametrize(
    ("version", "fetch", "uv_from"),
    [
        ("0.4.23", "/usr/bin/curl", "machine"),
        ("0.13.0", "", "machine"),
        ("0.4.22", "/usr/bin/curl", "fetched"),
        ("", "/usr/bin/wget", "fetched"),
        ("0.4.22", "", ""),
        ("", "", ""),
    ],
    ids=[
        "the first that reads it",
        "a later one",
        "older, fetchable",
        "unreadable, fetchable",
        "older, nothing to fetch with",
        "unreadable, nothing to fetch with",
    ],
)
def test_a_machine_uv_that_would_ignore_the_configured_index_is_not_the_one_used(version, fetch, uv_from):
    """UV_DEFAULT_INDEX arrived in uv 0.4.23 (with --default-index). An older uv ignores the variable and installs
    from PyPI while the plan names the mirror, so with an index configured the pinned uv is fetched instead, and
    with nothing to fetch it with the install says why."""
    p = _plan(index=_MIRROR, uv="/usr/bin/uv", uv_version=version, fetch=fetch)

    assert p.uv_from == uv_from
    commands = dict(install.steps(p)) if uv_from else {}
    if uv_from == "machine":
        assert "uv" not in commands and "uv=/usr/bin/uv; " in commands["packages"]
    if uv_from == "fetched":
        assert "uv" in commands and 'uv="$dir/.uv/uv"; ' in commands["packages"]
        said = f"uv {version}" if version else "a uv that does not say which version it is"
        assert (
            f" uv {install.UV_VERSION} itself first, under ~/.raven-node/.uv (the machine has {said}, and only uv "
            f"0.4.23 or later installs from the index pip is configured with), fetched with its own {fetch};"
        ) in p.describe()
        assert p.needed_kb == install._NODE_KB + install._UV_KB
    if not uv_from:
        said = f"uv {version}" if version else "a uv that does not say which version it is"
        assert install.refusal(p) == (
            f"machine 'Lab box' (box) has {said}, and only uv 0.4.23 or later installs from the index pip is "
            f"configured with there; with no curl or wget to fetch uv {install.UV_VERSION}, upgrade uv there "
            f"({install.UV_INSTALLER}), then run this again"
        )


def test_a_uv_an_earlier_install_fetched_is_used_when_the_machine_s_own_would_ignore_the_index(registry):
    """The next install-node after one that passed the machine's uv over finds that uv first again; the one the
    earlier install fetched under node_dir is reported beside it and used, so nothing is fetched twice and a
    machine that has since lost its curl is not refused."""

    def look(*, index: str, own_version: str = "0.13.0") -> Plan:
        said = (
            "PYTHON=/usr/bin/python3.12\nUV=/usr/local/bin/uv\nUV_VERSION=0.4.22\n"
            "NODE_UV=/home/worker/.raven-node/.uv/uv\n"
            + (f"NODE_UV_VERSION={own_version}\n" if own_version else "")
            + "FETCH=/usr/bin/curl\n"
            + (f"INDEX={index}\n" if index else "")
            + "FREE_KB=2097152\n"
        )
        return install.plan("box", run=lambda target, command: (0, said))

    p = look(index=_MIRROR)

    assert (p.uv, p.uv_version, p.uv_from) == ("/home/worker/.raven-node/.uv/uv", "0.13.0", "machine")
    commands = dict(install.steps(p))
    assert "uv" not in commands and "uv=/home/worker/.raven-node/.uv/uv; " in commands["packages"]
    assert p.needed_kb == install._NODE_KB and "itself first" not in p.describe()
    assert install.refusal(dataclasses.replace(p, fetch="")) is None, "nothing to fetch: nothing is needed"
    # Without an index the machine's own uv serves, as before; one under node_dir that is itself too old, or
    # silent, does not help, and the pinned uv is fetched over it.
    assert look(index="").uv == "/usr/local/bin/uv"
    assert (look(index=_MIRROR, own_version="0.4.22").uv, look(index=_MIRROR, own_version="0.4.22").uv_from) == (
        "/usr/local/bin/uv",
        "fetched",
    )
    assert look(index=_MIRROR, own_version="").uv_from == "fetched"


@pytest.mark.parametrize("version", ["0.1.0", ""], ids=["an old uv", "a uv that says no version"])
def test_an_old_uv_still_serves_a_machine_with_no_index_configured(version):
    p = _plan(uv="/usr/bin/uv", uv_version=version, fetch="")

    assert p.uv_from == "machine" and install.refusal(p) is None and "itself first" not in p.describe()


def test_a_plan_whose_index_port_cannot_be_read_still_describes_itself_and_its_failures():
    """An install refuses such an index; a plan that holds one all the same must not trace back either."""
    p = _plan(index="https://u:pw@mirror.example:84a3/simple")

    assert "https://mirror.example:84a3/simple (the index pip is configured with there, shown without its sign-in)" in (
        p.describe()
    )
    assert "pw@" not in install._failed(p, "packages", 2, "error: https://u:pw@mirror.example:84a3/simple/x")


@pytest.mark.parametrize(
    ("fetch", "fetched_with"),
    [
        ("/usr/bin/curl", f"curl -LsSf {install.UV_PINNED_INSTALLER}"),
        ("/usr/bin/wget", f"wget -qO- {install.UV_PINNED_INSTALLER}"),
    ],
)
def test_a_machine_without_uv_fetches_one_version_of_it_under_the_node_dir_and_nowhere_else(fetch, fetched_with):
    steps = install.steps(_plan(uv="", fetch=fetch))

    assert [name for name, _ in steps] == ["uv", "virtualenv", "packages", "search", "code", "path", "check"]
    head = "dir=~/.raven-node; id=" + bundle.install_name() + '; node="$dir/$id"; uv="$dir/.uv/uv"; '
    assert all(command.startswith(head) for _, command in steps)
    # One version, as the packages are pinned; an unmanaged install, which
    # writes no receipt under the home directory and changes no PATH.
    assert install.UV_VERSION in install.UV_PINNED_INSTALLER and install.UV_PINNED_INSTALLER != install.UV_INSTALLER
    assert dict(steps)["uv"] == (
        head + _MADE + f'mkdir -p "$dir/.uv" && {fetched_with} | UV_UNMANAGED_INSTALL="$dir/.uv" sh >/dev/null '
        '&& test -x "$dir/.uv/uv"'
    )
    assert "UV_INSTALL_DIR" not in dict(steps)["uv"] and "MODIFY_PATH" not in dict(steps)["uv"]
    assert dict(steps)["virtualenv"].endswith('"$uv" venv --quiet --python /usr/bin/python3.12 "$node"')


# --- carrying it out --------------------------------------------------------------


def test_an_install_runs_every_step_and_measures_what_it_made(small_tarball):
    machine = Machine()

    done = install.install(_plan(), run=machine.run, push=machine.push)

    assert done == install.Installed("~/.raven-node", bundle.install_name(), 51234, rg=True)
    assert machine.sent[:-1] == [command for _, command in install.steps(_plan())]
    assert machine.sent[-1].endswith('du -sk "$node" 2>/dev/null | cut -f1')
    assert machine.pushed == [small_tarball]
    assert not any(command.endswith('rm -rf "$node"') for command in machine.sent), "no cleanup after a step"


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
        (
            "check",
            (124, ""),
            "installing the Raven node on machine 'Lab box' (box) failed at the check step: it did not finish "
            "within 15 minutes",
        ),
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


def _sh(command: str, home: Path, path: str, **env: str) -> str:
    done = subprocess.run(
        ["/bin/sh", "-c", command],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": path, **env},
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
    assert "SPEED" not in seen, "a machine that has this Raven node installs nothing, so its index is not timed"


@pytest.mark.slow
def test_the_probe_says_which_version_the_uv_it_found_is_and_nothing_for_one_that_does_not_say(shell):
    home, tools = shell
    uv = _script(tools / "uv", 'echo "uv 0.4.22 (5b9c1fa1c 2024-10-16)"\n')

    seen = _lines(_sh(install.probe_script("~/.raven-node", "0.1-x", "d"), home, str(tools)))

    assert (seen["UV"], seen["UV_VERSION"]) == (str(uv), "0.4.22")
    _script(tools / "uv", "exit 0\n")
    assert "UV_VERSION" not in _lines(_sh(install.probe_script("~/.raven-node", "0.1-x", "d"), home, str(tools)))


@pytest.mark.slow
def test_the_probe_reports_the_uv_under_the_node_dir_beside_the_one_it_found_first(shell):
    home, tools = shell
    first = _script(tools / "uv", 'echo "uv 0.4.22 (5b9c1fa1c 2024-10-16)"\n')
    own = _script(home / "nodes" / ".uv" / "uv", 'echo "uv 0.13.0 (x86_64-unknown-linux-gnu)"\n')
    probe = install.probe_script(str(home / "nodes"), "0.1-x", "d")

    seen = _lines(_sh(probe, home, str(tools)))
    assert (seen["UV"], seen["UV_VERSION"], seen["NODE_UV"], seen["NODE_UV_VERSION"]) == (
        str(first),
        "0.4.22",
        str(own),
        "0.13.0",
    )

    first.unlink()
    seen = _lines(_sh(probe, home, str(tools)))
    assert (seen["UV"], seen["NODE_UV"]) == (str(own), str(own)), "found first, it is reported both ways"
    own.unlink()
    seen = _lines(_sh(probe, home, str(tools)))
    assert "UV" not in seen and "NODE_UV" not in seen and "NODE_UV_VERSION" not in seen


@pytest.mark.slow
def test_the_probe_reads_the_index_pip_is_configured_with_as_pip_reads_it(shell):
    home, tools = shell
    _script(tools / "python3", f'exec {sys.executable} "$@"\n')
    conf = home / ".config" / "pip" / "pip.conf"
    conf.parent.mkdir(parents=True)
    conf.write_text("[global]\nindex-url = https://mirror.example/simple\n", encoding="utf-8")
    probe = install.probe_script("~/.raven-node", "0.1-x", "d")

    assert _lines(_sh(probe, home, str(tools)))["INDEX"] == "https://mirror.example/simple"

    # [install] over [global], as for pip's own install command; the environment over every file.
    conf.write_text(
        "[global]\nindex-url = https://a/simple\n[install]\nindex-url = https://b/simple\n", encoding="utf-8"
    )
    assert _lines(_sh(probe, home, str(tools)))["INDEX"] == "https://b/simple"
    assert _lines(_sh(probe, home, str(tools), PIP_INDEX_URL="https://c/simple"))["INDEX"] == "https://c/simple"
    conf.unlink()
    assert "INDEX" not in _lines(_sh(probe, home, str(tools))), "nothing configured: uv's default"


@pytest.mark.slow
def test_a_node_dir_that_was_there_keeps_its_mode_and_one_the_install_makes_is_owner_only(shell, tmp_path):
    home, tools = shell
    for name in ("mkdir", "chmod", "rm"):
        (tools / name).symlink_to(shutil.which(name) or name)
    uv = _script(tools / "uv", "exit 0\n")
    theirs = tmp_path / "theirs"
    theirs.mkdir(mode=0o755)
    (theirs / "notes.txt").write_text("the owner's own\n", encoding="utf-8")
    ours = tmp_path / "ours"

    for node_dir in (theirs, ours):
        step = dict(install.steps(_plan(node_dir=str(node_dir), uv=str(uv), python=sys.executable)))["virtualenv"]
        _sh(step, home, str(tools))

    assert stat.S_IMODE(theirs.stat().st_mode) == 0o755 and (theirs / "notes.txt").is_file()
    assert stat.S_IMODE(ours.stat().st_mode) == 0o700


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


def _sh_in(command: str, home: Path, path: str, data: str) -> subprocess.CompletedProcess[str]:
    """``command`` under ``/bin/sh`` with ``data`` on its stdin, as a pushed step gets it."""
    return subprocess.run(
        ["/bin/sh", "-c", command],
        input=data,
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": path},
        cwd=home,
        timeout=60,
    )


@pytest.mark.slow
def test_the_index_reaches_uv_through_its_environment_and_never_its_argv_under_a_real_shell(shell, tmp_path):
    """On the machine, uv's argv is what another account's ``ps`` shows; its environment is this user's alone."""
    home, tools = shell
    calls = tmp_path / "uv-calls"
    # Fails the search package from the machine's index alone, so the fallback beside the public one runs too.
    uv = _script(
        tools / "uv",
        f'echo "ARGV=$* INDEX=$UV_DEFAULT_INDEX" >> "{calls}"\n'
        'case "$*" in *--extra-index-url*) exit 0 ;; *ripgrep*) exit 1 ;; esac\nexit 0\n',
    )
    index = "https://user:s3cret-pw@mirror.example:8443/simple"
    commands = dict(
        install.steps(_plan(node_dir=str(tmp_path / "nodes"), install_name="0.1-x", uv=str(uv), index=index))
    )

    packages = _sh_in(commands["packages"], home, str(tools), index + "\n")
    search = _sh_in(commands["search"], home, str(tools), index + "\n")

    assert packages.returncode == 0, packages.stderr
    assert search.returncode == 0 and search.stdout.strip() == "RG=1", search.stderr
    asked = calls.read_text(encoding="utf-8").splitlines()
    assert len(asked) == 3, "the packages, the search package from the index, then beside the public one"
    assert all(line.endswith(f" INDEX={index}") for line in asked), "uv reads the index from its environment"
    assert not any("s3cret-pw" in line.split(" INDEX=")[0] for line in asked), "and no argv of uv's carries it"
    assert f"--extra-index-url {protocol.PUBLIC_INDEX}" in asked[2]

    # No line on stdin: the step fails, rather than install from PyPI behind the owner's back.
    nothing = _sh_in(commands["packages"], home, str(tools), "")
    assert nothing.returncode != 0 and len(calls.read_text(encoding="utf-8").splitlines()) == 3


@pytest.mark.slow
def test_a_mirror_that_fails_a_step_is_followed_by_the_index_it_stands_in_for_under_a_real_shell(shell, tmp_path):
    home, tools = shell
    calls = tmp_path / "uv-calls"
    # The mirror fails every try, as one that lacks a pinned version would.
    uv = _script(
        tools / "uv",
        f'echo "ARGV=$* INDEX=$UV_DEFAULT_INDEX" >> "{calls}"\n'
        'case "$UV_DEFAULT_INDEX" in *tuna*) exit 1 ;; esac\nexit 0\n',
    )
    index = "https://user:s3cret-pw@mirror.example:8443/simple"
    commands = dict(
        install.steps(
            _plan(node_dir=str(tmp_path / "nodes"), install_name="0.1-x", uv=str(uv), index=index, mirror=_TUNA)
        )
    )
    fed = f"{_TUNA}\n{index}\n"

    packages = _sh_in(commands["packages"], home, str(tools), fed)
    search = _sh_in(commands["search"], home, str(tools), fed)

    assert packages.returncode == 0, packages.stderr
    assert search.returncode == 0 and search.stdout.strip() == "RG=1", search.stderr
    asked = calls.read_text(encoding="utf-8").splitlines()
    assert [line.split(" INDEX=")[1] for line in asked] == [_TUNA, index, _TUNA, index]
    assert not any("s3cret-pw" in line.split(" INDEX=")[0] for line in asked), "no argv of uv's carries it"
    assert f"--extra-index-url {protocol.PUBLIC_INDEX}" in asked[3]
    # Without the second line the step fails, rather than install from an index it was not given.
    lone = _sh_in(commands["packages"], home, str(tools), f"{_TUNA}\n")
    assert lone.returncode != 0 and len(calls.read_text(encoding="utf-8").splitlines()) == 4


@pytest.mark.slow
def test_the_probe_times_the_index_with_curl_reading_its_url_off_stdin(shell, tmp_path):
    """curl's argv is what another account's ``ps`` shows, and pip takes a sign-in inside the URL."""
    home, tools = shell
    (tools / "cat").symlink_to(shutil.which("cat") or "cat")
    calls = tmp_path / "curl-calls"
    _script(tools / "curl", f'echo "ARGV=$*" >> "{calls}"; cat >> "{calls}"; printf "200 190464 6.000473"; exit 28\n')
    probe = install.probe_script("~/.raven-node", "0.1-x", "d")
    index = "https://user:s3cret-pw@mirror.example:8443/simple/"

    seen = _lines(_sh(probe, home, str(tools), PIP_INDEX_URL=index))

    assert (seen["INDEX"], seen["SPEED"]) == (index, "28 200 190464 6.000473")
    argv, config = calls.read_text(encoding="utf-8").splitlines()
    assert argv.startswith("ARGV=-K - -sL ") and "s3cret-pw" not in argv and "mirror.example" not in argv
    assert config == 'url = "https://user:s3cret-pw@mirror.example:8443/simple/pillow/"'
    calls.unlink()
    # pip names no index: PyPI is what uv would install from, and what is timed.
    seen = _lines(_sh(probe, home, str(tools)))
    assert "INDEX" not in seen and seen["SPEED"] == "28 200 190464 6.000473"
    assert calls.read_text(encoding="utf-8").splitlines()[1] == f'url = "{protocol.PUBLIC_INDEX}/pillow/"'
    calls.unlink()
    # One that a config line cannot carry is not timed, and so is kept; nor is one the look is not asked to time.
    for odd in ('https://mirror.example/sim"ple', "https://mirror.example/sim\\ple"):
        assert "SPEED" not in _lines(_sh(probe, home, str(tools), PIP_INDEX_URL=odd))
    untimed = install.probe_script("~/.raven-node", "0.1-x", "d", timed=False)
    assert "SPEED" not in _lines(_sh(untimed, home, str(tools), PIP_INDEX_URL=index))
    assert not calls.exists()


@pytest.mark.slow
def test_the_mirrors_are_timed_at_once_each_line_naming_its_mirror(shell):
    home, tools = shell
    _script(
        tools / "curl",
        'case "$*" in *tuna*) printf "200 1165206 0.760254"; exit 0 ;; esac\nprintf "000 0 5.001000"; exit 7\n',
    )

    out = _sh(install.mirror_script(), home, str(tools))

    assert sorted(line for line in out.splitlines() if line.startswith("MIRROR=")) == [
        f"MIRROR=0 200 1165206 0.760254 {_TUNA}",
        f"MIRROR=7 000 0 5.001000 {_ALIYUN}",
    ]


@pytest.mark.slow
def test_the_virtualenv_step_clears_a_half_made_node_and_nothing_beside_it(shell, tmp_path):
    """Measured 2026-10-22: the packages step's uv went on after its ssh was given up, and wrote site-packages back
    ten seconds after the cleanup; uv venv refuses a directory that is there and is not a virtualenv."""
    home, tools = shell
    for name in ("mkdir", "chmod", "rm"):
        (tools / name).symlink_to(shutil.which(name) or name)
    nodes = tmp_path / "nodes"
    (nodes / "0.1-x" / "lib" / "python3.12" / "site-packages" / "PIL").mkdir(parents=True)
    (nodes / ".uv-cache" / "wheels-v6").mkdir(parents=True)
    seen = tmp_path / "seen"
    uv = _script(
        tools / "uv", f'for a; do last=$a; done; [ -e "$last" ] && echo there > "{seen}" || echo gone > "{seen}"\n'
    )
    plan = _plan(node_dir=str(nodes), install_name="0.1-x", uv=str(uv), python=sys.executable)

    _sh(dict(install.steps(plan))["virtualenv"], home, str(tools))

    assert seen.read_text(encoding="utf-8").strip() == "gone", "uv venv found the half-made Raven node still there"
    assert sorted(path.name for path in nodes.iterdir()) == [".uv-cache"]


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
@pytest.mark.parametrize(
    "index", [None, "https://user:pw@mirror.invalid/simple"], ids=["no index", "an index pip is configured with"]
)
def test_a_real_install_makes_a_node_that_reports_this_raven_s_code(registry, tmp_path, index):
    if not Path("/bin/sh").exists():
        pytest.skip("the install steps run under /bin/sh")
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin", **({"PIP_INDEX_URL": index} if index else {})}

    fed: list[bytes] = []

    def run(target: remote.Machine, command: str) -> tuple[int, str]:
        if "pip install" in command:
            # The package index is the one thing not reached from a test.
            return 0, "RG=0\n" if "RG=1" in command else ""
        done = subprocess.run(["/bin/sh", "-c", command], capture_output=True, text=True, env=env, cwd=home)
        return done.returncode, done.stdout + (f"\n{done.stderr}" if done.returncode else "")

    def push(target: remote.Machine, command: str, data: bytes) -> tuple[int, str]:
        if "pip install" in command:
            # Nor when the step is sent with the index on its stdin, as it is once pip is configured with one --
            # by this test, or by a system-wide pip.conf of the computer running it.
            fed.append(data)
            return 0, "RG=0\n" if "RG=1" in command else ""
        done = subprocess.run(["/bin/sh", "-c", command], input=data, capture_output=True, env=env, cwd=home)
        return done.returncode, done.stdout.decode() + (f"\n{done.stderr.decode()}" if done.returncode else "")

    nodes = tmp_path / "nodes"
    _script(nodes / "0.0.1-old" / "bin" / "python", "exit 0\n")
    (nodes / "0.0.1-old" / "lib" / "raven").mkdir(parents=True)

    first = install.plan("box", node_dir=str(nodes), run=run)
    assert not first.present
    if index:
        assert first.index == index, "the environment's index, over any pip.conf"
    uv = shutil.which("uv")
    if uv is None:
        pytest.skip("the install goes through uv, which this computer does not have")
    # Said to be recent enough rather than read off this computer's uv: with one older than 0.4.23 here, the
    # case with an index would have the install fetch uv through a stand-in that stubs only pip install, and
    # reach astral.sh. Which uv the plan uses is the other tests' business.
    p = dataclasses.replace(first, python=sys.executable, uv=uv, uv_version=install.UV_VERSION)
    done = install.install(p, run=run, push=push)

    assert done.install_name == bundle.install_name() and done.rg is False and (done.size_kb or 0) > 1000
    if index:
        assert fed == [index.encode() + b"\n"] * 2, "both steps that run uv pip were fed the index, and stood in for"
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
    # The node reads the version its directory is named by, and so names itself as the host named it: its
    # socket and lock under run/, and the version it reports at the handshake.
    named = subprocess.run(
        [
            str(node / "bin" / "python"),
            "-I",
            "-c",
            "import raven; from raven.node import bundle; print(raven.__version__); print(bundle.install_name())",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=home,
        check=True,
    ).stdout.split()
    assert named == [bundle.install_name().rsplit("-", 1)[0], bundle.install_name()]
    assert install.plan("box", node_dir=str(nodes), run=run).present, "the next look finds it"

    assert install.prune(p, run=run) == ("0.0.1-old",)
    # uv's cache stays beside the install, for the next one; nothing else does.
    assert sorted(path.name for path in nodes.iterdir()) == [".uv-cache", bundle.install_name()]
