"""An agent writes the machine the owner described, after reaching it.

Until 2026-09-06 the host ran `raven ops connection add` on the owner's behalf
before a spawn; that step went with the pre-dispatch registry gate, and the
write moved into the on-call plugin. It is trunk's since 2026-09-23: the
coding agent builds and smoke-tests where the solver is, and runs BEFORE the
on-call agent, so a registry nobody has written yet has to be writable from
where the need first shows up. These pin the guarantee the write carries over
hand-editing -- nothing lands until the machine itself has answered -- and
that what lands is what the readers then show.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from raven.agent.tools.connection_add import ConnectionAddTool
from raven.ops import connection_add as adder
from raven.ops import connections

# Taken before the autouse fixture below replaces it for every test.
_REAL_STORE_PATH = connections.store_path


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(connections, "store_path", lambda: tmp_path / "connections.json")
    return tmp_path


@pytest.fixture
def reached(monkeypatch):
    monkeypatch.setattr(adder, "probe", lambda row, **kw: (True, "reached it", {"cores": 32, "kind": "cpu"}))


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
    return asyncio.run(ConnectionAddTool().execute(**{**OWNER_SAID, **overrides}))


def test_the_tool_keeps_the_name_every_guide_and_face_pins():
    assert ConnectionAddTool().name == "ops_connection_add"


def test_reached_machine_is_written_and_listed(_home, reached):
    out = add()
    assert "wrote my-cpu-box" in out
    rows = json.loads((_home / "connections.json").read_text())["connections"]
    assert rows[0]["id"] == "my-cpu-box"
    assert rows[0]["cores"] == 32, "what the machine said about itself is on the row"
    assert [r["id"] for r in connections.load()] == ["my-cpu-box"], "the readers see it at once"
    assert "Host my-cpu-box" in (_home / ".ssh" / "config").read_text()
    assert "exec's 'machine' parameter" in out, "the id is told where to go, for a product with no ops_declare"


def test_a_registry_that_does_not_exist_yet_is_started(_home, reached):
    """The common case for an agent: an owner who never ran the command."""
    assert not (_home / "connections.json").exists()
    add()
    assert (_home / "connections.json").is_file()


def test_unreachable_machine_writes_nothing(_home, monkeypatch):
    monkeypatch.setattr(adder, "probe", lambda row, **kw: (False, "could not reach it: refused", {}))
    out = add()
    assert out.startswith("REFUSED")
    assert not (_home / "connections.json").exists()


def test_ssh_without_an_address_is_refused_before_probing(_home, monkeypatch):
    monkeypatch.setattr(adder, "probe", lambda row, **kw: pytest.fail("probed with no address"))
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
        adder,
        "ssh_defaults",
        lambda host, **kw: {"port": 58717, "user": "cfd", "keys": ["~/.ssh/id_ed25519", "~/.ssh/id_rsa"]},
    )
    tried = []

    def probe(row, **kw):
        tried.append((row["key"], kw.get("isolate_key")))
        hit = row["key"] == "~/.ssh/id_rsa"
        return hit, "reached it" if hit else "refused", {}

    monkeypatch.setattr(adder, "probe", probe)
    out = add(port=0, user="", key="")
    assert tried == [("~/.ssh/id_ed25519", True), ("~/.ssh/id_rsa", True)]
    row = json.loads((_home / "connections.json").read_text())["connections"][0]
    assert (row["port"], row["user"], row["key"]) == (58717, "cfd", "~/.ssh/id_rsa")
    assert "with key ~/.ssh/id_rsa" in out


def test_a_key_the_owner_named_is_probed_the_way_work_will_reach_the_machine(_home, monkeypatch):
    """No isolation for a path the owner asserted: an agent doing the
    authenticating is their setup working, and refusing it here would refuse a
    machine every later job can reach."""
    seen = []

    def probe(row, **kw):
        seen.append(kw.get("isolate_key"))
        return True, "reached it", {}

    monkeypatch.setattr(adder, "probe", probe)
    monkeypatch.setattr(adder, "ssh_defaults", lambda host, **kw: pytest.fail("resolved a key the owner gave"))
    add(key="~/.ssh/id_rsa")
    assert seen == [False]


def test_the_probe_rides_the_transport_exec_uses(monkeypatch):
    """The machine is reached here the way `exec(machine=...)` will reach it
    afterwards -- one seam, so a row that probes reachable is one that runs."""
    from raven.ops import transport

    asked = []

    def runner_from(row, *, cap_seconds=None):
        asked.append((row["id"], cap_seconds))
        return lambda cmd: (0, "CORES=8\n")

    monkeypatch.setattr(transport, "runner_from", runner_from)
    reached, message, found = adder.probe({"id": "box", "transport": "local"}, timeout=7)
    assert reached and found == {"cores": 8, "kind": "cpu"}
    assert asked == [("box", 7)]


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

    from raven.ops.transport import make_ssh_runner

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


def test_the_ask_is_one_text_the_on_call_listing_and_the_tool_share():
    """Spelled once: the on-call plugin's empty-registry reply carries trunk's
    ASK_OWNER, so the questions cannot drift between the two products."""
    import sys
    from pathlib import Path

    plugin_dir = Path(__file__).resolve().parent.parent / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
    sys.path.insert(0, str(plugin_dir))
    try:
        from oncall_flow import connections as plugin
    finally:
        sys.path.remove(str(plugin_dir))
    assert adder.ASK_OWNER in plugin._REQUEST
    assert "ops_connection_add" in adder.ASK_OWNER


# ---- the probe, through a stand-in ssh on PATH ------------------------------


def _fake_ssh(tmp_path, monkeypatch, body: str):
    """Put an executable named ssh first on PATH; the runners call it by name."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "ssh"
    script.write_text("#!/bin/sh\n" + body + "\n")
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")


SSH_ROW = {"id": "box", "transport": "ssh", "host": "203.0.113.7", "port": 58717, "user": "root", "key": "~/k"}


@pytest.mark.parametrize("isolate_key", [False, True], ids=["as-work-reaches-it", "candidate-key-alone"])
def test_a_machine_that_answers_and_then_goes_silent_is_given_up_on(tmp_path, monkeypatch, isolate_key):
    """The reviewed hole (2026-09-23): ConnectTimeout is over once the session
    is up, and the ssh runner had no deadline of its own, so a probe against a
    machine that accepted and then stopped answering never returned. The old
    CLI capped the call at 30 s; the shared probe keeps that cap on both of
    its ssh paths, and nothing is written on a machine that went quiet."""
    import time

    _fake_ssh(tmp_path, monkeypatch, "sleep 30")
    started = time.monotonic()
    reached, message, found = adder.probe(dict(SSH_ROW), timeout=0.5, isolate_key=isolate_key)
    assert time.monotonic() - started < 10
    assert (reached, found) == (False, {})
    assert "no answer within" in message


def test_a_machine_that_refuses_says_what_ssh_said(tmp_path, monkeypatch):
    _fake_ssh(tmp_path, monkeypatch, "echo 'Permission denied (publickey).' >&2; exit 255")
    reached, message, _ = adder.probe(dict(SSH_ROW), timeout=5)
    assert not reached and "Permission denied" in message


def test_no_ssh_client_on_this_computer_is_a_refusal_not_a_crash(tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    reached, message, _ = adder.probe(dict(SSH_ROW), timeout=5)
    assert not reached and "ssh not found" in message


def test_a_row_with_no_address_is_refused_by_the_transport():
    reached, message, _ = adder.probe({"id": "box", "transport": "ssh", "port": 22}, timeout=5)
    assert not reached and "no address" in message


def test_ssh_defaults_reads_what_ssh_resolves_and_keeps_only_keys_that_exist(tmp_path, monkeypatch):
    real = tmp_path / "id_real"
    real.write_text("")
    _fake_ssh(
        tmp_path,
        monkeypatch,
        f"printf 'user cfd\\nport 58717\\nidentityfile {real}\\nidentityfile {tmp_path}/id_gone\\n'",
    )
    assert adder.ssh_defaults("203.0.113.7") == {"user": "cfd", "port": 58717, "keys": [str(real)]}


def test_ssh_defaults_is_empty_when_ssh_cannot_answer(tmp_path, monkeypatch):
    _fake_ssh(tmp_path, monkeypatch, "exit 255")
    assert adder.ssh_defaults("203.0.113.7", port=22, user="root") == {}


def test_the_probe_reads_the_c_library_line_into_the_note():
    assert adder.parse_probe("CORES=8\nLIBC=ldd (GNU libc) 2.31\n")["note"] == "ldd (GNU libc) 2.31"


# ---- the tool's refusals and remarks --------------------------------------


def test_a_machine_with_no_name_is_refused():
    assert add(name="  ").startswith("REFUSED")


def test_a_local_machine_is_probed_here_and_written_without_an_alias(_home, reached):
    out = add(transport="local", host="", note="the lab mac")
    row = json.loads((_home / "connections.json").read_text())["connections"][0]
    assert row["transport"] == "local" and row["note"] == "the lab mac"
    assert "ssh alias" not in out


def test_no_key_to_try_is_a_question_for_the_owner(_home, monkeypatch):
    monkeypatch.setattr(adder, "ssh_defaults", lambda host, **kw: {"keys": []})
    out = add(key="")
    assert out.startswith("REFUSED") and "key path" in out


def test_candidates_that_all_fail_name_the_keys_tried_and_why_an_alias_is_out(_home, monkeypatch):
    monkeypatch.setattr(adder, "ssh_defaults", lambda host, **kw: {"keys": ["~/.ssh/a", "~/.ssh/b"]})
    monkeypatch.setattr(adder, "probe", lambda row, **kw: (False, "refused", {}))
    out = add(key="")
    assert out.startswith("REFUSED") and "~/.ssh/a, ~/.ssh/b" in out and "jump host" in out


def test_a_row_the_readers_could_not_use_is_not_written(_home, reached):
    out = add(id="Not An Id")
    assert out.startswith("REFUSED") and "lowercase" in out
    assert not (_home / "connections.json").exists()


def test_an_id_already_listed_is_refused_rather_than_overwritten(_home, reached):
    add()
    out = add()
    assert out.startswith("REFUSED") and "already listed" in out


def test_what_is_worth_the_owners_attention_is_said_without_refusing(_home, reached):
    out = add(software="")
    assert "wrote my-cpu-box" in out and "Worth telling the owner" in out and "'software' is not set" in out


def test_concurrency_is_kept_only_where_nothing_else_says_how_many_jobs_fit(_home, monkeypatch):
    monkeypatch.setattr(adder, "probe", lambda row, **kw: (True, "reached it", {}))
    add(concurrency=3)
    assert json.loads((_home / "connections.json").read_text())["connections"][0]["concurrency"] == 3


def test_an_alias_that_cannot_be_written_is_a_warning_not_a_refusal(_home, reached, monkeypatch):
    def refuse(row):
        raise OSError("read-only file system")

    monkeypatch.setattr(adder, "write_ssh_alias", refuse)
    out = add()
    assert "wrote my-cpu-box" in out and "ssh alias not written (read-only file system)" in out


def test_with_no_config_path_to_stand_beside_the_home_is_the_answer(tmp_path, monkeypatch):
    """The autouse fixture stands a scratch store in; this one needs the real
    resolution, so it calls the function the fixture replaced."""
    import raven.config.paths as paths

    def no_config():
        raise RuntimeError("no config")

    monkeypatch.setattr(paths, "get_config_path", no_config)
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(connections.CONNECTIONS_ENV, raising=False)
    assert _REAL_STORE_PATH() == tmp_path / "home" / "connections.json"
