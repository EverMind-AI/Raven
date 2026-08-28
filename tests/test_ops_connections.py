"""The host copy of the registry suite.

Four campaign-facing tests stayed in the on-call fork with the campaign
machinery they exercise (`raven.agent.tools.ops` is not lifted, only the
registry is).

A machine is something the owner set up and named, not something to find.

2026-08-14, two FEA tasks whose statement did not spell the port out: the loop
tried 22, 2222, 8022, 10022, 443, read ~/.ssh/config, pulled a stale port out of
known_hosts and believed it, then read raven's own campaign directory for the
number -- a dozen rounds, no job submitted. It had no way to reach a machine
except to guess at one.

These pin the three things that stop that: the list is the way in, the id is
what a campaign stores (names are the owner's and change), and a campaign
without a connection is untouched so nothing has to be migrated.
"""

from __future__ import annotations

import json

import pytest

from raven.ops import connections


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "connections.json"
    monkeypatch.setattr(connections, "store_path", lambda: path)

    def write(rows):
        path.write_text(json.dumps({"connections": rows}, ensure_ascii=False), encoding="utf-8")

    return write


TWO = [
    {
        "id": "conn_cpu",
        "display_name": "my CPU box",
        "kind": "cpu",
        "cores": 64,
        "software": "OpenFOAM",
        "budget_unit": "core-minute",
        "concurrency": 1,
        "host": "14.103.100.27",
        "port": 58717,
        "user": "root",
        "key": "~/.ssh/id_rsa",
    },
    {
        "id": "conn_gpu",
        "display_name": "the GPU machine",
        "kind": "gpu",
        "device": "4 x A100 80G",
        "software": "CalculiX 2.21, PyTorch + CUDA",
        "budget_unit": "gpu-minute",
        "concurrency": 1,
        "host": "14.103.100.27",
        "port": 64101,
        "user": "root",
        "key": "~/.ssh/id_rsa",
    },
]


def test_the_listing_names_the_machines_and_what_they_are(store):
    store(TWO)
    text = connections.describe()
    assert "my CPU box" in text and "the GPU machine" in text
    assert "4 x A100 80G" in text and "core-minute" in text


def test_the_listing_never_shows_credentials(store):
    """The agent does not authenticate, so a key path is only something to
    misuse -- and it was exactly what the loop went hunting for."""
    store(TWO)
    text = connections.describe()
    assert "id_rsa" not in text and "root" not in text


def test_two_machines_at_one_address_are_told_apart_by_name(store):
    """'on 14.103.100.27' names neither of these."""
    store(TWO)
    assert connections.get("conn_cpu")["port"] == 58717
    assert connections.get("conn_gpu")["port"] == 64101


def test_with_no_connection_it_says_to_ask_rather_than_to_look(store):
    store([])
    text = connections.describe()
    assert "ask" in text.lower()
    assert "do not look" in text.lower() or "rather than looking" in text.lower()


def test_a_campaign_naming_a_connection_gets_its_address(store):
    store(TWO)
    meta = {"connection": "conn_gpu", "backend": "process"}
    got = connections.resolve_into(meta)
    assert (got["host"], got["port"], got["user"]) == ("14.103.100.27", 64101, "root")


def test_a_campaign_without_a_connection_is_untouched(store):
    """Every campaign written before this exists and must keep running."""
    store(TWO)
    meta = {"host": "10.0.0.1", "port": 22, "backend": "process"}
    assert connections.resolve_into(meta) == meta


def test_an_unknown_connection_id_does_not_invent_an_address(store):
    store(TWO)
    meta = {"connection": "conn_gone"}
    assert connections.resolve_into(meta) == meta


def test_the_campaigns_own_value_wins_over_the_connection(store):
    """A campaign may override one field without describing the machine again."""
    store(TWO)
    got = connections.resolve_into({"connection": "conn_cpu", "port": 2222})
    assert got["port"] == 2222 and got["host"] == "14.103.100.27"


def test_a_missing_store_is_no_connections_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(connections, "store_path", lambda: tmp_path / "nope.json")
    assert connections.load() == []


def test_the_listing_says_what_each_machine_has_installed(store):
    """The deciding attribute, and it is not what the machine is made of.

    Measured 2026-08-17 on the real pair: CalculiX runs only on the box with the
    A800s, because its binary needs a glibc the 32-core box does not have. "A
    CPU-only solver belongs on the CPU box" would pick the one machine that
    cannot run it.
    """
    store(TWO)
    text = connections.describe()
    assert "CalculiX" in text and "OpenFOAM" in text


def test_the_listing_names_the_three_shapes_a_target_can_have(store):
    """This listing is the first stop for anything that needs a machine, so the
    fork belongs in it.

    Measured twice on 2026-08-21: a watch task came through here, picked the right
    machine off this very text -- its own reasoning quoted the sentinel line out of
    the software field -- and then built its own monitor out of write_file and
    cron. Nothing on the way had said a campaign could be a watch; every sign said
    case, trial, round. A document saying so would not have been read at the
    moment of choosing, and this text is.
    """
    store(TWO)
    text = connections.describe()

    for shape in ("optimize", "complete", "condition"):
        assert shape in text
    assert "readings" in text, "what a watch declares instead of a case"
    assert "looks" in text, "a budget that is not machine time"
    assert "ops_check_later" in text and "cron" in text, (
        "the alternative it actually reached for has to be named, or the loop is "
        "choosing between one option it was told about and one it thought of itself"
    )


def test_the_listing_says_where_the_id_goes(monkeypatch):
    """An id with no destination gets dropped.

    Measured 2026-08-19: a loop reached this listing, picked the right machine,
    wrote "that one is this laptop, I will just run it here", and hand-ran five
    trials. The run before it skipped the listing, was handed ops_declare by a
    tool result, and used it. The listing named a parameter and no call.
    """
    from raven.ops.connections import describe

    monkeypatch.setattr(
        "raven.ops.connections.load",
        lambda: [
            {"id": "conn_here", "display_name": "the laptop", "kind": "cpu"},
        ],
    )

    out = describe()

    assert "ops_declare" in out, "the next call has to be named, not just the parameter"
    assert "costs nothing and runs nothing" in out, (
        "declaring is free, and a loop weighing whether it is worth it needs that"
    )
    assert "not distance" in out, (
        "the machine being this computer is what made it decide the machinery was "
        "unnecessary; the listing has to answer that where it arises"
    )


def test_a_broken_file_is_not_reported_as_an_empty_one(store, tmp_path, monkeypatch):
    """The worst silent failure this file had, and the reason the states exist.

    A hand-edited registry with one stray comma raised ValueError, became [], and
    the listing then told the loop the owner had set no machine up. Every file
    here was hand-edited until `raven ops connection add` existed, so this was one
    keystroke away at all times -- and the one thing the loop was told about it
    was false. There may well be machines in that file.
    """
    path = tmp_path / "connections.json"
    monkeypatch.setattr(connections, "store_path", lambda: path)
    path.write_text('{"connections": [{"id": "conn_gpu", "host": "1.2.3.4",}]}', encoding="utf-8")

    found = connections.read()
    text = connections.describe()

    assert found.state == connections.UNREADABLE
    assert "not valid JSON" in found.detail
    assert "cannot be read" in text
    assert "NOT the same as having no machines" in text
    assert "no machine is set up" not in text.lower(), "the false half of the old answer"


def test_an_absent_file_still_reads_as_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(connections, "store_path", lambda: tmp_path / "nothing.json")

    assert connections.read().state == connections.MISSING
    assert connections.load() == []


def test_the_request_names_every_field_the_owner_has_to_supply(store):
    """The loop cannot fill any of this in, so the ask has to be complete.

    The old text said to ask the owner and never said what for, which leaves the
    loop to compose the question -- and the fields it cannot see are exactly the
    ones it would leave out.
    """
    store([])

    text = connections.describe()

    for asked in ("port", "private key", "installed", "budget", "at once", "directories"):
        assert asked in text, asked
    assert "raven ops connection add" in text


def test_a_row_with_no_way_in_blocks_and_a_thin_one_does_not(store):
    """Two levels, and which is which.

    An ssh row with no key cannot be used for anything. A row that predates
    `budget_unit` is used every day. Folding them together would condemn a
    working registry, and a check that does that gets turned off.
    """
    no_key = {"id": "conn_a", "display_name": "A", "host": "h", "port": 22, "user": "root"}
    thin = {"id": "conn_b", "display_name": "B", "transport": "local"}

    assert any(p.blocking for p in connections.row_problems(no_key))
    assert connections.row_problems(thin), "a thin row is still worth reporting"
    assert not any(p.blocking for p in connections.row_problems(thin))
    assert [r["id"] for r in connections.usable([no_key, thin])] == ["conn_b"]


def test_a_field_spelled_the_way_a_hand_reaches_for_it_is_named(store):
    """`name` for `display_name` is not hypothetical: a fixture in this repo does it.

    That connection lists as its bare id, because the listing reads `display_name`,
    and nothing said so.
    """
    row = {"id": "conn_a", "name": "the solver box", "transport": "local"}

    text = " ".join(p.text for p in connections.row_problems(row))

    assert "'name'" in text and "display_name" in text


def test_a_port_that_is_a_string_is_caught(store):
    row = {"id": "conn_a", "display_name": "A", "host": "h", "port": "58717", "user": "root", "key": "~/.ssh/id_rsa"}

    problems = connections.row_problems(row)

    assert any(p.blocking and "port" in p.text for p in problems)


def test_two_machines_cannot_share_an_id(store):
    store(
        [
            {"id": "conn_a", "display_name": "A", "transport": "local"},
            {"id": "conn_a", "display_name": "B", "transport": "local"},
        ]
    )

    assert any(p.blocking and "appears 2 times" in p.text for p in connections.problems())


def test_a_row_with_no_id_is_counted_rather_than_dropped_in_silence(store, tmp_path, monkeypatch):
    """Every family in this line reports what it could not use; this one did not."""
    path = tmp_path / "connections.json"
    monkeypatch.setattr(connections, "store_path", lambda: path)
    path.write_text(
        json.dumps({"connections": [{"display_name": "nameless"}, {"id": "conn_a", "transport": "local"}]}),
        encoding="utf-8",
    )

    found = connections.read()

    assert [r["id"] for r in found.rows] == ["conn_a"]
    assert "1 entry" in found.detail and "no id" in found.detail


def test_an_env_var_points_this_instance_at_the_owners_registry(tmp_path, monkeypatch):
    """A sub-agent reads the host's machines rather than a copy of them.

    The launcher used to copy the file in once and never again, so the day the
    owner added a machine the copy stopped being true and nothing said so.
    """
    theirs = tmp_path / "elsewhere" / "connections.json"
    theirs.parent.mkdir()
    theirs.write_text(
        json.dumps({"connections": [{"id": "conn_gpu", "display_name": "GPU", "transport": "local"}]}), encoding="utf-8"
    )
    monkeypatch.setenv(connections.CONNECTIONS_ENV, str(theirs))

    assert connections.store_path() == theirs
    assert [r["id"] for r in connections.load()] == ["conn_gpu"]


def test_clearing_the_env_var_gives_the_instance_its_own_registry_back(tmp_path, monkeypatch):
    """The override is read per call, not remembered.

    An instance that keeps pointing at a registry the launcher pointed it at
    once, after the launcher stopped, is the copy-once bug wearing a different
    hat.
    """
    theirs = tmp_path / "theirs.json"
    theirs.write_text(json.dumps({"connections": []}), encoding="utf-8")
    monkeypatch.setenv(connections.CONNECTIONS_ENV, str(theirs))
    borrowed = connections.store_path()

    monkeypatch.delenv(connections.CONNECTIONS_ENV)

    assert borrowed == theirs
    assert connections.store_path() != theirs
