"""The on-call instance writes the machine the owner described, after reaching it.

Until 2026-09-06 the host ran `raven ops connection add` on the owner's behalf
before a spawn; that step went with the pre-dispatch registry gate, and an owner who answered the seven questions in conversation had nothing
consuming the answer. These pin the guarantee the write carries over hand-editing
-- nothing lands until the machine itself has answered -- and that what lands is
what the listing then shows.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow import connections  # noqa: E402
from oncall_flow.tools import base as tools_base  # noqa: E402
from oncall_flow.tools.ops_connection_add import OpsConnectionAddTool  # noqa: E402


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    tools_base.set_home(tmp_path / "ops")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(connections, "store_path", lambda: tmp_path / "connections.json")
    return tmp_path


@pytest.fixture
def reached(monkeypatch):
    monkeypatch.setattr(connections, "probe", lambda row, **kw: (True, "reached it", {"cores": 32, "kind": "cpu"}))


OWNER_SAID = {
    "name": "my CPU box",
    "transport": "ssh",
    "host": "14.103.100.27",
    "port": 58717,
    "user": "root",
    "key": "~/.ssh/id_rsa",
    "software": "OpenFOAM v2312 (/opt/openfoam)",
    "budget_unit": "core-minute",
    "concurrency": 1,
    "paths": ["/home/cfd/work"],
}


def add(**overrides):
    return asyncio.run(OpsConnectionAddTool().execute(**{**OWNER_SAID, **overrides}))


def test_reached_machine_is_written_and_listed(_home, reached):
    out = add()
    assert "wrote my-cpu-box" in out
    rows = json.loads((_home / "connections.json").read_text())["connections"]
    assert rows[0]["id"] == "my-cpu-box"
    assert rows[0]["cores"] == 32, "what the machine said about itself is on the row"
    assert "my CPU box" in connections.describe()
    assert "Host my-cpu-box" in (_home / ".ssh" / "config").read_text()


def test_unreachable_machine_writes_nothing(_home, monkeypatch):
    monkeypatch.setattr(connections, "probe", lambda row, **kw: (False, "could not reach it: refused", {}))
    out = add()
    assert out.startswith("REFUSED")
    assert not (_home / "connections.json").exists()


def test_ssh_without_an_address_is_refused_before_probing(_home, monkeypatch):
    monkeypatch.setattr(connections, "probe", lambda row, **kw: pytest.fail("probed with no address"))
    out = add(host="")
    assert "REFUSED" in out and "address" in out
    assert not (_home / "connections.json").exists()


def test_what_the_owner_left_out_comes_from_ssh_config_and_the_key_that_connects(_home, monkeypatch):
    """Ssh's own resolution names the candidates, and each is tried with that key ALONE.

    `-i` is a preference, not a restriction: OpenSSH still offers the other
    configured identities and whatever the agent holds. A candidate credited with
    a session some other key authenticated would put a path in the registry that
    stops working the day that agent or that config goes.
    """
    monkeypatch.setattr(
        connections,
        "ssh_defaults",
        lambda host, **kw: {"port": 58717, "user": "cfd", "keys": ["~/.ssh/id_ed25519", "~/.ssh/id_rsa"]},
    )
    tried = []

    def probe(row, **kw):
        tried.append((row["key"], kw.get("isolate_key")))
        hit = row["key"] == "~/.ssh/id_rsa"
        return hit, "reached it" if hit else "refused", {}

    monkeypatch.setattr(connections, "probe", probe)
    out = add(port=0, user="", key="")
    assert tried == [("~/.ssh/id_ed25519", True), ("~/.ssh/id_rsa", True)]
    row = json.loads((_home / "connections.json").read_text())["connections"][0]
    assert (row["port"], row["user"], row["key"]) == (58717, "cfd", "~/.ssh/id_rsa")
    assert "with key ~/.ssh/id_rsa" in out


def test_a_key_the_owner_named_is_probed_the_way_a_job_will_reach_the_machine(_home, monkeypatch):
    """No isolation for a path the owner asserted: an agent doing the
    authenticating is their setup working, and refusing it here would refuse a
    machine every later job can reach."""
    seen = []

    def probe(row, **kw):
        seen.append(kw.get("isolate_key"))
        return True, "reached it", {}

    monkeypatch.setattr(connections, "probe", probe)
    monkeypatch.setattr(connections, "ssh_defaults", lambda host, **kw: pytest.fail("resolved a key the owner gave"))
    add(key="~/.ssh/id_rsa")
    assert seen == [False]


def test_an_isolated_candidate_leaves_ssh_exactly_one_identity_to_offer(tmp_path, monkeypatch):
    """Measured through ssh's own resolver, because the argv does not settle it.

    `-i` is a preference, not a restriction, and `IdentitiesOnly=yes` is defined
    as excluding what an agent or a provider adds -- not what the config names.
    With the owner's own `IdentityFile` lines still in play, any one of them can
    authenticate the first candidate's session and be credited to it, and the
    registry then holds a path that merely looked verified.

    The owner's real config cannot be pointed somewhere else for a test (ssh
    finds it through the password database, not $HOME), so one is put in front
    of the runner's own arguments instead. The last `-F` wins, which is what
    makes this sensitive: drop the runner's and this config is read.
    """
    import shutil
    import subprocess

    from oncall_flow.docker_backend import make_ssh_runner

    if not shutil.which("ssh"):
        pytest.skip("no ssh on this computer")

    cand, other_a, other_b = (tmp_path / n for n in ("cand", "other_a", "other_b"))
    for f in (cand, other_a, other_b):
        f.write_text("")
    config = tmp_path / "config"
    config.write_text(f"Host target\n  HostName 127.0.0.1\n  IdentityFile {other_a}\n  IdentityFile {other_b}\n")

    def resolved(argv: list[str]) -> list[str]:
        out = subprocess.run(argv, capture_output=True, text=True, check=False)
        return [ln.split(None, 1)[1] for ln in out.stdout.splitlines() if ln.startswith("identityfile ")]

    def as_ssh_reads_it(*, isolated: bool) -> list[str]:
        seen: list[list[str]] = []
        monkeypatch.setattr(
            subprocess, "run", lambda argv, **kw: seen.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
        )
        make_ssh_runner("target", 22, str(cand), user="u", identities_only=isolated)("true")
        monkeypatch.undo()
        # The runner's own argv, handed back to ssh as a question rather than a
        # connection, with this owner's config in front of it.
        return resolved(["ssh", "-G", "-F", str(config)] + seen[0][1:-2] + [seen[0][-2]])

    assert as_ssh_reads_it(isolated=True) == [str(cand)], "only the candidate may authenticate its own probe"

    open_to_all = as_ssh_reads_it(isolated=False)
    assert str(other_a) in open_to_all and str(other_b) in open_to_all, (
        "the guard means nothing unless this config would otherwise put other keys in play"
    )

    # The reviewer's finding, kept as a measurement: shutting the agent out is
    # not enough on its own, which is why the option set also reads no config.
    agent_only = resolved(
        [
            "ssh",
            "-G",
            "-F",
            str(config),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "IdentityAgent=none",
            "-i",
            str(cand),
            "u@target",
        ]
    )
    assert str(other_a) in agent_only and str(other_b) in agent_only
