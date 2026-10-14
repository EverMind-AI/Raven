"""The permission check for a read, and a write, on a registered machine.

A registered machine's login is often root and its disks shared, so a read
there is not waved through the way a read here is: the directories this
Raven's agents work in there and the row's own paths read freely; anywhere
else asks, and a grant for the session covers that machine and directory
however the path is written. A write there asks wherever it falls, as a
write here does, and its grant is for the one file.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.config.schema import PermissionsConfig
from raven.contracts.permissions import Allow, DecisionSource, NeedsApproval, Tier
from raven.ops import remote_path
from raven.permissions.builtin import session_keys
from raven.permissions.rules import default_tier
from raven.permissions.session import remember_allowed
from tests.test_permissions_gate import bind, gate_for

ROWS = [
    {"id": "box", "display_name": "Lab box", "host": "203.0.113.7", "port": 58717, "user": "root"},
    {"id": "nas", "host": "198.51.100.9", "port": 22, "user": "alice", "paths": ["/data/shared"]},
]


@pytest.fixture(autouse=True)
def registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    path = tmp_path / "connections.json"
    path.write_text(json.dumps({"connections": ROWS}), encoding="utf-8")
    monkeypatch.setenv("RAVEN_CONNECTIONS", str(path))
    monkeypatch.setenv("RAVEN_DEBUG_MACHINES", "1")
    monkeypatch.setattr(remote_path, "_HOMES", {})
    monkeypatch.setattr(
        "raven.config.loader.load_config",
        lambda *a, **k: SimpleNamespace(
            subagents=SimpleNamespace(agents=[SimpleNamespace(machine="box", remote_cwd="/srv/agents")])
        ),
    )


@pytest.mark.parametrize(
    ("tool", "path", "tier"),
    [
        ("read_file", "box:raven-work/t1/report.md", Tier.ALLOW),
        ("list_dir", "box:~/raven-work", Tier.ALLOW),
        ("grep", "box:/srv/agents/t1", Tier.ALLOW),
        ("find", "nas:/data/shared/runs", Tier.ALLOW),
        ("read_file", "box:.ssh/id_rsa", Tier.ASK),
        ("read_file", "box:raven-work/../.ssh/id_rsa", Tier.ASK),
        ("list_dir", "nas:/data", Tier.ASK),
        ("grep", "box:", Tier.ASK),
        # Reaches no machine: the tool refuses it, nothing to ask about.
        ("read_file", "nowhere:/etc/passwd", Tier.ALLOW),
        # A read here is what it always was.
        ("read_file", "/etc/hosts", Tier.ALLOW),
    ],
)
def test_a_remote_read_is_free_in_the_roots_and_asks_elsewhere(tool, path, tier):
    assert default_tier(tool, {"path": path}) is tier


def test_with_the_lane_off_a_read_is_tiered_as_it_always_was(monkeypatch):
    monkeypatch.delenv("RAVEN_DEBUG_MACHINES")

    assert default_tier("read_file", {"path": "box:.ssh/id_rsa"}) is Tier.ALLOW
    assert session_keys("read_file", {"path": "box:.ssh/id_rsa"}) != session_keys("read_file", {"path": "box:.ssh/x"})


def test_an_absolute_path_reads_freely_once_the_machine_s_home_is_known():
    assert default_tier("read_file", {"path": "box:/root/raven-work/a"}) is Tier.ASK

    remote_path.remember_home("box", "/root")

    assert default_tier("read_file", {"path": "box:/root/raven-work/a"}) is Tier.ALLOW
    assert default_tier("read_file", {"path": "box:/root/.ssh/id_rsa"}) is Tier.ASK


def test_a_grant_covers_one_machine_and_directory_however_the_path_is_written():
    remote_path.remember_home("box", "/root")

    one = session_keys("read_file", {"path": "box:/root/.ssh/id_rsa"})
    same = session_keys("read_file", {"path": "scp://root@203.0.113.7:58717//root/.ssh/known_hosts"})
    relative = session_keys("read_file", {"path": "box:.ssh/config"})
    other_dir = session_keys("read_file", {"path": "box:/root/.config/x"})
    listing = session_keys("list_dir", {"path": "box:/root/.ssh"})
    other_machine = session_keys("read_file", {"path": "nas:/root/.ssh/id_rsa"})

    assert one == same == relative == listing
    assert len({one, other_dir, other_machine}) == 3


def test_a_local_read_keys_the_exact_call_as_before():
    assert session_keys("read_file", {"path": "/etc/hosts"}) != session_keys("read_file", {"path": "/etc/passwd"})


@pytest.mark.asyncio
async def test_the_gate_asks_once_and_the_grant_holds_for_the_directory():
    gate = gate_for(PermissionsConfig(mode="ask"))
    bind(None)
    outside = {"path": "box:.ssh/id_rsa"}

    first = await gate.check("read_file", outside)
    assert isinstance(first, NeedsApproval)
    remember_allowed("conv-1", first.session_keys)

    again = await gate.check("read_file", {"path": "box:.ssh/known_hosts"})
    assert isinstance(again, Allow) and again.source is DecisionSource.SESSION
    inside = await gate.check("read_file", {"path": "box:raven-work/a"})
    assert isinstance(inside, Allow) and inside.source is DecisionSource.DEFAULT


# --- writes ask wherever they fall, as a write here does ------------------------------------


@pytest.mark.parametrize(
    ("tool", "path"),
    [
        # Inside the roots that free a read: a change is still asked about.
        ("write_file", "box:raven-work/t1/out.md"),
        ("edit_file", "box:/srv/agents/t1/a.py"),
        ("edit_file", "nas:/data/shared/runs/notes.md"),
        ("write_file", "box:.bashrc"),
        ("write_file", "nas:/etc/hosts"),
        ("edit_file", "box:raven-work/../.ssh/authorized_keys"),
        # A write here asks as it always did.
        ("write_file", "/tmp/notes.md"),
    ],
)
def test_a_remote_write_asks_wherever_it_falls(tool, path):
    assert default_tier(tool, {"path": path}) is Tier.ASK


def test_a_write_grant_is_kept_apart_from_a_read_grant_and_covers_the_one_file():
    remote_path.remember_home("box", "/root")

    read = session_keys("read_file", {"path": "box:/root/.ssh/id_rsa"})
    write = session_keys("write_file", {"path": "box:/root/.ssh/id_rsa"})
    same_file = session_keys("edit_file", {"path": "scp://root@203.0.113.7:58717//root/.ssh/id_rsa"})
    relative = session_keys("write_file", {"path": "box:.ssh/id_rsa"})
    neighbour = session_keys("write_file", {"path": "box:/root/.ssh/config"})

    assert write == same_file == relative
    assert len({read, write, neighbour}) == 3
    assert session_keys("write_file", {"path": "/tmp/a"}) != session_keys("write_file", {"path": "/tmp/b"})


@pytest.mark.asyncio
async def test_a_read_allowed_for_the_session_does_not_allow_a_write_there():
    # Its own directory: the session store is one per process, and the read
    # test above has already allowed `.ssh` for this conversation.
    gate = gate_for(PermissionsConfig(mode="ask"))
    bind(None)

    read = await gate.check("read_file", {"path": "box:.config/raven/state.json"})
    assert isinstance(read, NeedsApproval)
    remember_allowed("conv-1", read.session_keys)
    write = await gate.check("write_file", {"path": "box:.config/raven/state.json", "content": "x"})
    assert isinstance(write, NeedsApproval), "a look is not a change"
    remember_allowed("conv-1", write.session_keys)

    again = await gate.check("edit_file", {"path": "box:.config/raven/state.json", "old_text": "a", "new_text": "b"})
    assert isinstance(again, Allow) and again.source is DecisionSource.SESSION
    neighbour = await gate.check(
        "edit_file", {"path": "box:.config/raven/other.json", "old_text": "a", "new_text": "b"}
    )
    assert isinstance(neighbour, NeedsApproval), "the grant is for the one file, as it is here"
    inside = await gate.check("write_file", {"path": "box:raven-work/t1/out.md", "content": "x"})
    assert isinstance(inside, NeedsApproval), "the roots free a look, not a change"
