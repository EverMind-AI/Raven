"""raven.ops.remote_path: a file on a registered machine, written the way scp writes one.

The rules are scp(1)'s own (OpenSSH 9.9p2): the short form has no port field,
the URI form is the only one with a port, and in it the first slash after the
host only separates. A path names exactly one registered machine -- by its id,
or by address and port -- or it is refused before any ssh. Shown, it uses the
registry's user, address and port, in a form scp reads back.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.ops import remote_path
from raven.ops.remote_path import RemotePathError, absolute, looks_remote, parse, within

ROWS = [
    {"id": "box2", "display_name": "Box 2", "host": "203.0.113.7", "port": 49273, "user": "root"},
    {"id": "box", "display_name": "Box", "host": "203.0.113.7", "port": 41941, "user": "root"},
    {"id": "std", "host": "198.51.100.9", "port": 22, "user": "alice", "paths": ["/data/shared"]},
    {"id": "v6", "host": "2001:db8::5", "port": 22, "user": "root"},
]


@pytest.fixture(autouse=True)
def _fresh_state(monkeypatch):
    monkeypatch.setattr(remote_path, "_HOMES", {})
    monkeypatch.setattr(remote_path, "_NODES", None)


def _where(text: str):
    found = parse(text, ROWS)
    assert found is not None, text
    return found.machine, found.path


# --- the two forms ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "machine", "path"),
    [
        # The machine's id, the id with its user, and the URI with the address.
        ("box2:/root/x", "box2", "/root/x"),
        ("root@box2:/root/x", "box2", "/root/x"),
        ("scp://root@203.0.113.7:49273//root/x", "box2", "/root/x"),
        # The first slash after host:port only separates.
        ("scp://203.0.113.7:49273/root/x", "box2", "root/x"),
        ("scp://box2", "box2", ""),
        # After the colon, a path not starting with / is under the home.
        ("box2:", "box2", ""),
        ("box2:raven-work/a", "box2", "raven-work/a"),
        ("box2:~/raven-work/a", "box2", "~/raven-work/a"),
        # An address with no port is port 22; the registry decides the rest.
        ("alice@198.51.100.9:/data/x", "std", "/data/x"),
        ("198.51.100.9:/data/x", "std", "/data/x"),
        ("sftp://alice@198.51.100.9//data", "std", "/data"),
        ("root@[2001:db8::5]:/etc/hostname", "v6", "/etc/hostname"),
        ("scp://root@[2001:db8::5]:22//etc/hostname", "v6", "/etc/hostname"),
        # An address is matched without regard to case; a user and an id are not.
        ("scp://root@203.0.113.7:49273//x", "box2", "/x"),
    ],
)
def test_a_remote_path_names_one_registered_machine(text, machine, path):
    assert _where(text) == (machine, path)


def test_a_user_and_a_machine_id_are_compared_as_written():
    with pytest.raises(RemotePathError, match="registered for another user"):
        parse("scp://ROOT@203.0.113.7:49273//x", ROWS)
    with pytest.raises(RemotePathError, match="no registered machine matches it"):
        parse("BOX2:/x", ROWS)


@pytest.mark.parametrize(
    ("text", "said"),
    [
        # No port means 22, and no registered machine is on 22 there.
        ("root@203.0.113.7:/etc/hostname", "no registered machine matches it"),
        # The short form has no port field -- `49273:/a` is a file name on 22.
        ("root@203.0.113.7:49273:/a", "no registered machine matches it"),
        ("bob@std:/x", "logs in as 'bob', and machine 'std' is registered for another user"),
        ("scp://box2:41941//x", "names machine 'Box 2' (box2) on a port it is not registered on"),
        ("scp://203.0.113.7:99999//x", "names port '99999', which is not a port"),
        ("scp://203.0.113.7:ssh//x", "names port 'ssh', which is not a port"),
        ("[2001:db8::5/x", "opens an address with '[' and does not close it"),
        ("root@[2001:db8::5:/x", "opens an address with '['"),
        ("scp://[2001:db8::5//x", "opens an address with '['"),
        ("scp://[2001:db8::5]x//x", "something after the address that is not a port"),
        ("nobox:/x", "write it as ./name or as an absolute path"),
        # A local name holding a colon reads as remote unless written as a path
        # (one letter before the colon is a drive on a Windows host: the drive-relative test).
        ("ab:b.txt", "write it as ./name or as an absolute path"),
    ],
)
def test_a_path_that_names_no_registered_machine_is_refused_before_any_ssh(text, said):
    with pytest.raises(RemotePathError, match=re.escape(said)):
        parse(text, ROWS)


def test_two_rows_on_one_address_and_port_are_named_by_id_instead():
    rows = [*ROWS, {**ROWS[0], "id": "box2-again"}]

    with pytest.raises(RemotePathError, match=r"matches more than one registered machine .*write the machine's id"):
        parse("scp://203.0.113.7:49273//x", rows)
    assert parse("box2-again:/x", rows).machine == "box2-again"


@pytest.mark.parametrize(
    "text",
    [
        "./a:b.txt",
        "/abs/a:b",
        "C:\\x",
        "c:/x",
        "C:",
        "\\\\?\\C:\\x\\review_marker.txt",
        "\\\\server\\share\\f",
        "http://x.org/a",
        "https://x.org/a:b",
        "~/x",
        "dir/a:b",
        ":x",
        "",
    ],
)
def test_a_local_path_is_left_to_this_computer(text):
    assert not looks_remote(text)
    assert parse(text, ROWS) is None


def test_a_drive_relative_path_is_this_computer_s_on_a_windows_host_and_scp_s_elsewhere(monkeypatch):
    monkeypatch.setattr(remote_path, "_WINDOWS", True)
    assert not looks_remote("C:review_marker.txt") and parse("C:review_marker.txt", ROWS) is None

    monkeypatch.setattr(remote_path, "_WINDOWS", False)
    assert looks_remote("c:review_marker.txt"), "what scp reads as machine c"


# --- shown the way scp writes it ------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "shown"),
    [
        # Not port 22 -- the URI form, absolute paths with two slashes.
        ("box2:/root/raven-work/t1/report.md", "scp://root@203.0.113.7:49273//root/raven-work/t1/report.md"),
        ("box2:raven-work/a", "scp://root@203.0.113.7:49273/raven-work/a"),
        # Port 22 -- the short form.
        ("std:/data/x", "alice@198.51.100.9:/data/x"),
        ("std:notes", "alice@198.51.100.9:notes"),
        ("v6:/etc/hostname", "root@[2001:db8::5]:/etc/hostname"),
    ],
)
def test_a_path_is_shown_with_the_registry_s_user_address_and_port(text, shown):
    assert parse(text, ROWS).display() == shown


@pytest.mark.parametrize(
    "text", ["box2:/root/x", "box2:raven-work/a", "std:/data/x", "std:notes", "v6:/etc/hostname", "box2:"]
)
def test_what_is_shown_reads_back_as_the_same_file_on_the_same_machine(text):
    first = parse(text, ROWS)

    again = parse(first.display(), ROWS)

    assert (again.machine, again.path) == (first.machine, first.path)


def test_a_path_is_shown_absolute_once_the_home_is_known():
    where = parse("box2:raven-work/a", ROWS)

    assert where.display(where.absolute("/root")) == "scp://root@203.0.113.7:49273//root/raven-work/a"


# --- placing a path on its machine ------------------------------------------------


@pytest.mark.parametrize(
    ("path", "placed"),
    [
        ("", "/root"),
        ("~", "/root"),
        ("~/a", "/root/a"),
        ("a/b", "/root/a/b"),
        ("/x/../y", "/y"),
        ("raven-work/../.ssh/id", "/root/.ssh/id"),
        ("~bob/x", "~bob/x"),
    ],
)
def test_a_path_is_placed_against_the_login_home(path, placed):
    assert absolute(path, "/root") == placed


def test_without_a_home_a_relative_path_stays_as_written():
    assert absolute("a/b", "") == "a/b"
    assert absolute("", "") == ""


@pytest.mark.parametrize(
    ("path", "root", "inside"),
    [
        ("/root/raven-work", "/root/raven-work", True),
        ("/root/raven-work/t1/a", "/root/raven-work", True),
        ("/root/raven-work/t1/a", "/root/raven-work/", True),
        ("/root/raven-workx/a", "/root/raven-work", False),
        ("/root/raven-work/../.ssh/id", "/root/raven-work", False),
        ("raven-work/a", "/root/raven-work", False),
    ],
)
def test_within_compares_whole_directories(path, root, inside):
    assert within(path, root) is inside


def test_only_an_absolute_home_is_remembered():
    remote_path.remember_home("box2", "/root/")
    remote_path.remember_home("box", "relative")

    assert remote_path.known_home("box2") == "/root"
    assert remote_path.known_home("box") == ""


# --- what a read reaches, for its permission check ----------------------------------


@pytest.fixture
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "connections.json"
    path.write_text(json.dumps({"connections": ROWS}), encoding="utf-8")
    monkeypatch.setenv("RAVEN_CONNECTIONS", str(path))


@pytest.fixture
def agents(monkeypatch: pytest.MonkeyPatch):
    """The acp entries config holds; none unless a test adds one."""
    entries: list[SimpleNamespace] = []
    monkeypatch.setattr(
        "raven.config.loader.load_config", lambda *a, **k: SimpleNamespace(subagents=SimpleNamespace(agents=entries))
    )
    return entries


@pytest.mark.parametrize(
    ("tool", "path", "inside", "directory"),
    [
        ("read_file", "box2:raven-work/t1/a.md", True, "~/raven-work/t1"),
        ("read_file", "box2:~/raven-work/t1/a.md", True, "~/raven-work/t1"),
        ("list_dir", "box2:raven-work", True, "~/raven-work"),
        ("grep", "box2:raven-work/t1", True, "~/raven-work/t1"),
        ("find", "box2:", False, "~"),
        ("read_file", "box2:.ssh/id_rsa", False, "~/.ssh"),
        ("read_file", "box2:raven-work/../.ssh/id_rsa", False, "~/.ssh"),
        # Absolute with no home known yet: placed nowhere, so it asks.
        ("read_file", "box2:/root/raven-work/a", False, "/root/raven-work"),
        # The row's own paths.
        ("read_file", "std:/data/shared/a/b.txt", True, "/data/shared/a"),
        ("list_dir", "std:/data", False, "/data"),
    ],
)
def test_a_read_reaches_one_directory_inside_the_roots_or_outside(registry, agents, tool, path, inside, directory):
    reach = remote_path.reading(tool, {"path": path})

    assert (reach.inside, reach.directory) == (inside, directory)


def test_once_the_home_is_known_an_absolute_path_is_placed_in_the_roots(registry, agents):
    remote_path.remember_home("box2", "/root")

    inside = remote_path.reading("read_file", {"path": "scp://root@203.0.113.7:49273//root/raven-work/t1/a"})
    outside = remote_path.reading("read_file", {"path": "box2:/root/.ssh/id_rsa"})

    assert (inside.inside, inside.directory) == (True, "/root/raven-work/t1")
    assert (outside.inside, outside.directory) == (False, "/root/.ssh")


def test_an_agent_entry_s_remote_cwd_on_that_machine_is_a_root(registry, agents):
    agents.append(SimpleNamespace(machine="box2", remote_cwd="/srv/agents"))
    agents.append(SimpleNamespace(machine="box", remote_cwd="/elsewhere"))
    agents.append(SimpleNamespace(machine=None, remote_cwd=None))

    assert remote_path.reading("read_file", {"path": "box2:/srv/agents/t1/a"}).inside
    assert not remote_path.reading("read_file", {"path": "box2:/elsewhere/a"}).inside


def test_a_config_that_cannot_load_adds_no_root(registry, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("not json")

    monkeypatch.setattr("raven.config.loader.load_config", broken)

    assert remote_path.reading("read_file", {"path": "box2:raven-work/a"}).inside
    assert not remote_path.reading("read_file", {"path": "box2:/srv/agents/a"}).inside


@pytest.mark.parametrize(
    ("tool", "params"),
    [
        ("write_file", {"path": "box2:raven-work/a"}),
        ("read_file", {"path": "./notes.md"}),
        ("read_file", {"path": "nobox:/x"}),
        ("read_file", {"path": 7}),
        ("read_file", None),
        ("grep", {"pattern": "x"}),
    ],
)
def test_a_call_that_reaches_no_machine_has_no_reading(registry, agents, tool, params):
    assert remote_path.reading(tool, params) is None


def test_the_fence_is_the_root_a_path_is_under_else_what_was_approved(registry, agents):
    where = parse("box2:raven-work/t1/a", ROWS)

    assert remote_path.fence("read_file", where, "/root/raven-work/t1/a", "/root") == "/root/raven-work"
    assert remote_path.fence("read_file", where, "/root/.ssh/id_rsa", "/root") == "/root/.ssh"
    assert remote_path.fence("list_dir", where, "/root/.ssh", "/root") == "/root/.ssh"
    assert remote_path.fence("read_file", where, "/id", "/root") == "/"


def test_registered_paths_are_remote_without_a_debug_switch(registry, agents, monkeypatch):
    monkeypatch.delenv("RAVEN_DEBUG_MACHINES", raising=False)
    assert remote_path.target("box2:/root/x").machine == "box2"
    assert remote_path.target("scp://root@203.0.113.7:49273//root/x").machine == "box2"
    assert remote_path.reading("read_file", {"path": "box2:.ssh/id_rsa"}) is not None


def test_the_node_pool_is_whatever_assembly_handed_over():
    assert remote_path.nodes() is None
    pool = object()

    remote_path.use_nodes(lambda: pool)

    assert remote_path.nodes() is pool


# --- what a reader read ----------------------------------------------------------------


def test_what_was_read_is_the_reader_s_own_and_the_table_is_bounded(monkeypatch):
    monkeypatch.setattr(remote_path, "_READ", {})
    monkeypatch.setattr(remote_path, "_READ_LIMIT", 3)

    remote_path.remember_read("box2", "/root/a", "d1", reader="A")
    remote_path.remember_read("box2", "/root/a", "d2", reader="B")
    assert remote_path.last_read("box2", "/root/a", reader="A") == "d1"
    assert remote_path.last_read("box2", "/root/a", reader="B") == "d2"
    assert remote_path.last_read("box2", "/root/a") is None, "no name is one more reader, not every reader"

    remote_path.forget_read("box2", "/root/a", reader="A")
    assert remote_path.last_read("box2", "/root/a", reader="A") is None
    assert remote_path.last_read("box2", "/root/a", reader="B") == "d2", "A wrote: B's next overwrite is checked"

    remote_path.remember_read("box2", "/root/b", "d3", reader="B")
    remote_path.remember_read("box2", "/root/a", "d4", reader="B")  # read again: the newest entry now
    remote_path.remember_read("box2", "/root/c", "d5", reader="B")
    remote_path.remember_read("box2", "/root/d", "d6", reader="B")
    assert remote_path.last_read("box2", "/root/b", reader="B") is None, "past the bound the oldest goes"
    assert [remote_path.last_read("box2", p, reader="B") for p in ("/root/a", "/root/c", "/root/d")] == [
        "d4",
        "d5",
        "d6",
    ]
