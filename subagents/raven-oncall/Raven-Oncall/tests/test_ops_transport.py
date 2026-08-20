"""Reaching a machine that is this one.

Someone who writes their own solver runs it where they wrote it, and installs
raven on that box. There is then no host and no key, and the whole stack above
this -- a task statement, a declaration, a tool call -- has to read the same as
it does for a machine across the room.
"""

from __future__ import annotations

import pytest

from raven.ops.backend import JobBackendError
from raven.ops.transport import (
    LOCAL,
    SSH,
    TIMED_OUT_RC,
    make_local_runner,
    runner_from,
    transport_of,
)


def test_a_campaign_that_says_nothing_is_still_reached_over_ssh():
    # Every campaign written before this seam existed says nothing, and has to go
    # on meaning what it meant.
    assert transport_of({}) == SSH
    assert transport_of({"host": "h"}) == SSH
    assert transport_of({"transport": "local"}) == LOCAL
    assert transport_of({"transport": "LOCAL"}) == LOCAL


def test_a_local_runner_answers_in_the_same_shape_as_ssh():
    run = make_local_runner()

    rc, out = run("echo hi")
    assert (rc, out.strip()) == (0, "hi")

    rc, out = run("echo boom >&2; exit 3")
    assert rc == 3 and "boom" in out, "a failure has to carry what was said on stderr"

    rc, out = run("printf 'a\\nb\\n' | wc -l")
    assert rc == 0 and out.strip() == "2", "pipes and redirects are what remote commands use"


def test_the_local_cap_does_not_depend_on_the_timeout_binary():
    # `timeout` is GNU coreutils and macOS does not ship it, which is exactly the
    # machine a local connection is most likely to be. Measured 2026-08-19: the
    # first local look came back `exit 127, /bin/sh: timeout: command not found`.
    rc, _ = make_local_runner(cap_seconds=0.3)("sleep 5")

    assert rc == TIMED_OUT_RC, "the cap has to hold with no coreutils on the box"


def test_a_local_connection_needs_no_address():
    run = runner_from({"connection": "conn_here", "transport": "local"})

    rc, out = run("echo hi")
    assert (rc, out.strip()) == (0, "hi")


def test_a_named_connection_with_no_address_says_which_one():
    # Distinct from "this machine has no address by design": a connection the
    # registry no longer has leaves nothing to fill the address in, and the
    # failure used to reach the owner as their machine refusing the connection.
    with pytest.raises(JobBackendError) as exc:
        runner_from({"connection": "conn_gone"})

    assert "conn_gone" in str(exc.value) and "not in the connection registry" in str(exc.value)


def test_a_meta_with_neither_is_left_alone_rather_than_newly_refused():
    """The hand-written shape that predates connections still builds a runner.

    Refusing it here would be a new rule applied to campaigns that were set up
    before any of this existed. They fail where they always did -- at the staging
    step -- and narrowing this guard to a *named* connection is what keeps the
    change additive.
    """
    assert callable(runner_from({}, what="machine"))



def test_a_path_is_matched_to_the_machine_that_claims_it(monkeypatch):
    from raven.ops.connections import machine_for_path

    monkeypatch.setattr("raven.ops.connections.load", lambda: [
        {"id": "conn_box", "display_name": "the CPU box",
         "software": "CalculiX 2.17 (/srv/arena), OpenFOAM"},
        {"id": "conn_here", "display_name": "the laptop", "paths": ["/home/me/cases"]},
    ])

    assert machine_for_path("/srv/arena")[0] == "conn_box"
    assert machine_for_path("/srv/arena/deck/job.inp")[0] == "conn_box", "under a claim counts"
    assert machine_for_path("/home/me/cases/one")[0] == "conn_here", "an explicit list wins"
    assert machine_for_path("/srv/other") is None, "a sibling is not a claim"
    assert machine_for_path("/etc/passwd") is None


def test_nothing_claimed_means_no_line_and_a_broken_registry_is_silent(monkeypatch):
    from raven.ops.connections import provenance_line

    monkeypatch.setattr("raven.ops.connections.load", lambda: [])
    assert provenance_line("/srv/arena") == ""

    def explode():
        raise RuntimeError("unreadable")

    monkeypatch.setattr("raven.ops.connections.load", explode)
    assert provenance_line("/srv/arena") == "", (
        "a look must not fail because the machine list cannot be read"
    )


@pytest.mark.asyncio
async def test_looking_at_a_claimed_directory_says_whose_it_is(monkeypatch, tmp_path):
    """The fact travels the way the failure would.

    On a machine that is a different box the look fails, and the failure is what
    sends the loop to the machine list -- measured 2026-08-19 14:13: three local
    probes, "the path does not exist", ops_connections eight seconds later. Here
    the look succeeds and used to say nothing, and three runs of the same task
    went straight to a local shell, twice writing into the owner's case.
    """
    from raven.agent.tools.filesystem import ListDirTool

    case = tmp_path / "arena"
    case.mkdir()
    (case / "run.sh").write_text("#!/bin/sh\n")
    monkeypatch.setattr("raven.ops.connections.load", lambda: [
        {"id": "conn_here", "display_name": "the laptop", "paths": [str(case)]},
    ])

    out = await ListDirTool().execute(path=str(case))

    assert "run.sh" in out, "what was asked for still comes back in full"
    assert "the laptop (machine=conn_here)" in out
    assert "ops_declare" in out and "ops_submit" in out


@pytest.mark.asyncio
async def test_a_command_touching_a_claimed_path_says_whose_it_is(monkeypatch, tmp_path):
    from raven.agent.tools.shell import ExecTool

    case = tmp_path / "arena"
    case.mkdir()
    monkeypatch.setattr("raven.ops.connections.load", lambda: [
        {"id": "conn_here", "display_name": "the laptop", "paths": [str(case)]},
    ])
    tool = ExecTool(working_dir=str(tmp_path))

    touched = await tool.execute(command=f"ls {case}")
    assert "the laptop (machine=conn_here)" in touched

    unrelated = await tool.execute(command="echo hi")
    assert "hi" in unrelated and "machine=" not in unrelated, (
        "an ordinary local command must not grow a line about machines"
    )


def test_a_claim_broad_enough_to_be_a_filesystem_is_ignored(monkeypatch):
    """A note that fires on every look is read as noise and then not read at all.

    The claims are parsed out of a free-text field, so a connection written as
    "OpenFOAM 装在 /opt" or "code is in /Users/admin" would otherwise put a line
    about machines on every ordinary directory listing.
    """
    from raven.ops.connections import machine_for_path

    for broad in ("/", "/opt", "/usr/", "/Users", "/Users/admin", "/home/me", "/tmp"):
        monkeypatch.setattr("raven.ops.connections.load", lambda: [
            {"id": "conn_box", "display_name": "the box", "paths": [broad]},
        ])
        assert machine_for_path("/Users/admin/notes/today.md") is None, broad
        assert machine_for_path(broad + "/anything") is None, broad

    monkeypatch.setattr("raven.ops.connections.load", lambda: [
        {"id": "conn_box", "display_name": "the box", "paths": ["/Users/admin/work/arena"]},
    ])
    assert machine_for_path("/Users/admin/work/arena/run.sh") is not None, (
        "a real case directory still counts"
    )
