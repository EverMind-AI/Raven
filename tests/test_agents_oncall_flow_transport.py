"""Reaching a machine that is this one.

Someone who writes their own solver runs it where they wrote it, and installs
raven on that box. There is then no host and no key, and the whole stack above
this -- a task statement, a declaration, a tool call -- has to read the same as
it does for a machine across the room.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow.backend import JobBackendError  # noqa: E402
from oncall_flow.transport import (
    LOCAL,
    SSH,
    TIMED_OUT_RC,
    make_local_runner,
    runner_from,
    transport_of,
)  # noqa: E402


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
