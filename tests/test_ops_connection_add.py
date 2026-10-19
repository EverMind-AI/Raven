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
import os
import sys

import pytest

from raven.agent.tools.connection_add import ConnectionAddTool
from raven.ops import connection_add as adder
from raven.ops import connections
from tests._home import point_home

# Taken before the autouse fixture below replaces it for every test.
_REAL_STORE_PATH = connections.store_path


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    point_home(monkeypatch, tmp_path)
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


def test_a_key_proved_with_the_config_kept_reaches_the_host_the_config_names_and_offers_no_agent(tmp_path, monkeypatch):
    """A password registration proves the key it just installed, on a machine
    its config may be the only way to (a jump host, an alias): the probe reads
    the owner's config -- the alias resolves, as it did for the install a
    moment before -- and keeps the agent out. The config's own identities for
    that host stay in play, which is what every later connection offers too."""
    import subprocess

    from raven.ops.transport import make_ssh_runner

    cand, other = tmp_path / "cand", tmp_path / "other"
    for f in (cand, other):
        f.write_text("")
    config = tmp_path / "config"
    config.write_text(f"Host target\n  HostName 127.0.0.1\n  Port 2200\n  IdentityFile {other}\n")
    seen: list[list[str]] = []
    monkeypatch.setattr(
        subprocess, "run", lambda argv, **kw: seen.append(argv) or subprocess.CompletedProcess(argv, 0, "", "")
    )
    make_ssh_runner("target", 22, str(cand), user="u", identities_only=True, keep_config=True)("true")
    monkeypatch.undo()
    argv = seen[0]
    assert "-F" not in argv, "the config is read"
    assert "IdentitiesOnly=yes" in argv and "IdentityAgent=none" in argv

    out = subprocess.run(["ssh", "-G", "-F", str(config)] + argv[1:-2] + [argv[-2]], capture_output=True, text=True)
    resolved = dict(ln.split(None, 1) for ln in out.stdout.splitlines() if " " in ln)
    identities = [ln.split(None, 1)[1] for ln in out.stdout.splitlines() if ln.startswith("identityfile ")]
    assert resolved["hostname"] == "127.0.0.1", "the alias in the owner's config is in play"
    assert resolved["identitiesonly"] == "yes" and resolved["identityagent"] == "none"
    assert identities == [str(cand), str(other)], "the key under proof, and what the config names for the host"


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


# --- what is learned about a machine after it was added ------------------------------


def _store(_home, rows) -> object:
    path = _home / "connections.json"
    path.write_text(rows if isinstance(rows, str) else json.dumps({"connections": rows}), encoding="utf-8")
    return path


def test_set_fields_changes_one_machine_and_keeps_everything_else(_home):
    path = _store(_home, [{"id": "a", "host": "h1", "note": "kept"}, {"id": "b", "host": "h2"}])

    assert adder.set_fields("a", {"node_dir": "/data/raven-node"}) == path

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "connections": [
            {"id": "a", "host": "h1", "note": "kept", "node_dir": "/data/raven-node"},
            {"id": "b", "host": "h2"},
        ]
    }


@pytest.mark.parametrize(
    ("rows", "said"),
    [
        ([{"id": "b"}], "machine 'a' is listed 0 times"),
        ([{"id": "a"}, {"id": " a "}], "machine 'a' is listed 2 times"),
        ([{"id": "a"}, {"host": "no id"}], "Give those entries an id"),
        ("{not json", "Fix or move that file before changing it."),
    ],
)
def test_set_fields_refuses_rather_than_guess_or_lose_rows(_home, rows, said):
    path = _store(_home, rows)
    before = path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match=said):
        adder.set_fields("a", {"node_dir": "/x"})

    assert path.read_text(encoding="utf-8") == before


# --- remove: the write half of `raven ops connection remove` and `machines.remove` ---


def test_remove_takes_every_row_under_the_id_out_and_keeps_the_rest_as_written(_home):
    path = connections.store_path()
    rows = [{"id": "a", "host": "1", "note": "mine"}, {"id": "b", "host": "2"}, {"id": "a", "host": "3"}]
    path.write_text(json.dumps({"connections": rows}, indent=2) + "\n", encoding="utf-8")

    assert adder.remove("a") == path

    assert json.loads(path.read_text(encoding="utf-8"))["connections"] == [{"id": "b", "host": "2"}]


def test_remove_refuses_an_id_that_is_not_listed_a_file_it_cannot_read_and_entries_it_would_lose(_home):
    path = connections.store_path()
    with pytest.raises(ValueError, match="is not listed"):
        adder.remove("a")
    path.write_text(json.dumps({"connections": [{"id": "a"}, {"host": "no id"}]}), encoding="utf-8")
    with pytest.raises(ValueError, match="had no id"):
        adder.remove("a")
    assert len(json.loads(path.read_text(encoding="utf-8"))["connections"]) == 2
    path.write_text("{nope", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid JSON"):
        adder.remove("a")


def test_the_fixture_s_home_is_what_the_alias_helpers_open_on_windows_as_on_posix(_home):
    """``write_ssh_alias`` and ``remove_ssh_alias`` find ``~/.ssh/config`` through ``os.path.expanduser``, which on
    Windows reads ``USERPROFILE`` and not ``HOME``: a fixture that moved only ``HOME`` left a native Windows run
    writing the operator's own config. Pinned with both path flavours, on every platform."""
    import ntpath
    import posixpath

    assert ntpath.expanduser("~") == str(_home), "the Windows reading of ~"
    assert posixpath.expanduser("~") == str(_home), "the POSIX reading of ~"


def test_remove_ssh_alias_takes_only_the_managed_block_out(_home):
    cfg = _home / ".ssh" / "config"
    cfg.parent.mkdir(mode=0o700)
    owners = "Host mine\n  HostName 10.0.0.1\n"
    cfg.write_text(owners, encoding="utf-8")
    adder.write_ssh_alias({"id": "a", "host": "h", "port": 22, "user": "u", "key": "/k"})
    adder.write_ssh_alias({"id": "b", "host": "h2", "port": 22, "user": "u", "key": "/k"})
    mark_b = adder.ALIAS_MARK.format(conn_id="b")

    assert adder.remove_ssh_alias("a") is True

    block_b = "\n".join([mark_b, "Host b", "  HostName h2", "  Port 22", "  User u", "  IdentityFile /k", mark_b])
    assert cfg.read_text(encoding="utf-8") == owners + "\n" + block_b + "\n"
    assert adder.remove_ssh_alias("a") is False, "a block that is gone is nothing to do"
    assert adder.remove_ssh_alias("b") is True
    assert cfg.read_text(encoding="utf-8") == owners
    adder.write_ssh_alias({"id": "a", "host": "h", "port": 22, "user": "u", "key": "/k"})
    cfg.write_text(cfg.read_text(encoding="utf-8").removeprefix(owners).lstrip("\n"), encoding="utf-8")
    assert adder.remove_ssh_alias("a") is True
    assert cfg.read_text(encoding="utf-8") == "", "a file that held only the block is left empty, not deleted"
    cfg.unlink()
    assert adder.remove_ssh_alias("a") is False, "no file is nothing to do"


def test_a_managed_block_with_one_mark_missing_is_left_alone_on_remove_and_on_re_add(_home):
    """One mark of two -- a block the owner edited by hand -- is no block: the rest of the file is theirs.

    Measured before the fix: an unrelated ``Host after`` entry following a lone
    mark was gone after ``remove_ssh_alias``, and after a re-add.
    """
    cfg = _home / ".ssh" / "config"
    cfg.parent.mkdir(mode=0o700)
    mark = adder.ALIAS_MARK.format(conn_id="box")
    text = f"Host mine\n  HostName 10.0.0.1\n\n{mark}\nHost box\n  HostName h\n\nHost after\n  HostName 10.0.0.2\n"
    cfg.write_text(text, encoding="utf-8")

    with pytest.raises(ValueError, match="incomplete"):
        adder.remove_ssh_alias("box")
    assert cfg.read_text(encoding="utf-8") == text, "nothing of the owner's may go with a block that is not whole"

    with pytest.raises(ValueError, match="incomplete"):
        adder.write_ssh_alias({"id": "box", "host": "h2", "port": 22, "user": "u", "key": "/k"})
    assert cfg.read_text(encoding="utf-8") == text


def test_the_tool_reports_an_incomplete_managed_block_as_a_warning_not_a_refusal(_home, reached, monkeypatch):
    def refuse(row):
        raise ValueError("the managed 'Host my-cpu-box' block in ~/.ssh/config is incomplete")

    monkeypatch.setattr(adder, "write_ssh_alias", refuse)
    out = add()
    assert "wrote my-cpu-box" in out and "ssh alias not written" in out and "incomplete" in out


# ---- a machine given by password: the password stays here, used on every connection ---


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are not available on Windows")
def test_a_password_is_kept_under_the_home_for_the_owner_alone_and_forgotten_on_request(_home, monkeypatch):
    monkeypatch.setenv("RAVEN_HOME", str(_home / "rhome"))

    path = adder.store_password("gpu1", "hunter2")

    assert path == _home / "rhome" / "keys" / "gpu1.password" == adder.password_path("gpu1")
    assert path.read_text(encoding="utf-8") == "hunter2\n", "one line, which is what askpass hands ssh"
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert oct(path.parent.stat().st_mode & 0o777) == "0o700"
    # Registered again with another password: rewritten in place, still the owner's alone.
    assert adder.store_password("gpu1", "s3cret") == path
    assert path.read_text(encoding="utf-8") == "s3cret\n" and oct(path.stat().st_mode & 0o777) == "0o600"
    assert adder.forget_password("gpu1") is True and not path.exists()
    assert adder.forget_password("gpu1") is False, "nothing kept is nothing to forget"


def test_a_row_registered_with_a_password_says_so_and_gets_no_ssh_alias(_home):
    row = {"id": "gpu1", "transport": "ssh", "host": "h", "port": 22, "user": "u", "auth": "password"}
    assert adder.password_auth(row)
    assert not adder.password_auth({**row, "auth": "key"})
    assert not adder.password_auth({k: v for k, v in row.items() if k != "auth"})

    # scp and rsync run their client here, and an alias has nowhere to carry a password.
    assert adder.write_ssh_alias(row) is None
    assert not (_home / ".ssh" / "config").exists()


def _fake_ssh_that_takes_a_password(tmp_path, monkeypatch, *, accept: str, body: str = "") -> dict:
    """An ssh on PATH that asks the askpass program the way OpenSSH does, and records what it saw."""
    record = tmp_path / "ssh-saw.json"
    _fake_ssh(
        tmp_path,
        monkeypatch,
        f"""
        # OpenSSH reads the password from SSH_ASKPASS when SSH_ASKPASS_REQUIRE=force.
        given=$("$SSH_ASKPASS" "password:")
        argv=$(printf '%s' "$*" | sed 's/\\\\/\\\\\\\\/g; s/"/\\\\"/g')
        printf '{{"argv": "%s", "given": "%s", "require": "%s", "file": "%s"}}\\n' "$argv" "$given" "$SSH_ASKPASS_REQUIRE" "$RAVEN_MACHINE_PASSWORD_FILE" > {record}
        [ "$given" = '{accept}' ] || {{ echo 'Permission denied (password).' >&2; exit 255; }}
        {body}
        """,
    )
    return {"record": record}


PASSWORD_ROW = {k: v for k, v in SSH_ROW.items() if k != "key"} | {"auth": "password"}


@pytest.mark.skipif(
    sys.platform == "win32", reason="password machines are refused on Windows; the stand-in ssh is a POSIX script"
)
def test_the_probe_reaches_a_password_machine_through_askpass_and_the_password_is_never_on_the_command_line(
    tmp_path, monkeypatch
):
    import json

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    adder.store_password("box", "hunter2")
    seen = _fake_ssh_that_takes_a_password(tmp_path, monkeypatch, accept="hunter2", body="echo CORES=4")

    reached, said, found = adder.probe(PASSWORD_ROW, timeout=10)

    assert reached and found == {"cores": 4, "kind": "cpu"}, said
    saw = json.loads(seen["record"].read_text(encoding="utf-8"))
    assert saw["given"] == "hunter2" and saw["require"] == "force"
    assert saw["file"] == str(adder.password_path("box"))
    words = saw["argv"].split()
    assert "hunter2" not in saw["argv"] and "-i" not in words and "BatchMode=yes" not in words
    assert "PubkeyAuthentication=no" in words and "NumberOfPasswordPrompts=1" in words


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-in ssh is a POSIX shell script")
def test_a_refused_password_is_one_sentence_that_names_the_other_cause_too_and_never_the_password(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    adder.store_password("box", "wrong-one")
    _fake_ssh_that_takes_a_password(tmp_path, monkeypatch, accept="hunter2")

    reached, said, _ = adder.probe(PASSWORD_ROW, timeout=10)

    assert not reached
    assert "Permission denied (password)." in said
    assert "The password was refused, or this machine does not accept password logins" in said
    assert "wrong-one" not in said


def test_a_password_row_with_no_password_kept_here_is_refused_before_ssh_is_started(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    _fake_ssh(tmp_path, monkeypatch, "echo 'ssh must not run' >&2; exit 99")

    reached, said, _ = adder.probe(PASSWORD_ROW, timeout=10)

    assert not reached and "no password is kept here for machine 'box'" in said and "register it again" in said
    assert "ssh must not run" not in said


def test_a_password_row_is_refused_on_windows_before_ssh_is_started(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    adder.store_password("box", "hunter2")
    monkeypatch.setattr(sys, "platform", "win32")

    reached, said, _ = adder.probe(PASSWORD_ROW, timeout=10)

    assert not reached and "Windows" in said and "use a key" in said
