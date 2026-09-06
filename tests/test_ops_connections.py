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

These pin what stops that: the id is what a campaign stores rather than an
address (names are the owner's and change), a row nothing can be run on is
told apart from one that is merely thin, and what leaves this module carries
what decides a machine without carrying the way onto it.
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


def test_what_travels_never_carries_the_way_in(store):
    """The reader does not authenticate, so a key path is only something to
    misuse -- and it was exactly what the loop went hunting for. Asserted on
    the projection rather than on a rendering of it: the rendering is each
    installation's own, and this is the last point under this module's
    control."""
    store(TWO)

    for row in connections.load():
        projected = connections.shown(row)
        assert {"host", "port", "user", "key", "transport"}.isdisjoint(projected)
        assert "id_rsa" not in str(projected) and "root" not in str(projected)


def test_what_travels_carries_what_decides_the_machine():
    """The positive half of the projection, and it names the fields rather than
    looping over ``_SHOWN``: an assertion derived from that tuple moves with it,
    so dropping a key would delete its own check in the same edit.

    ``software`` is the deciding one and was not obvious -- measured 2026-08-17
    on the real pair, CalculiX runs only on the box with the A800s, because the
    binary needs a glibc the 32-core box does not have. A rule like "a CPU-only
    solver belongs on the CPU box" would pick the one machine that cannot run
    it. What a machine has installed decides; what it is made of only narrows.
    """
    projected = connections.shown(
        {
            "id": "conn_gpu",
            "display_name": "the GPU machine",
            "kind": "gpu",
            "device": "4 x A100 80G",
            "cores": 32,
            "memory": "463 GB",
            "software": "CalculiX 2.21, PyTorch + CUDA",
            "budget_unit": "gpu-minute",
            "concurrency": 2,
            "note": "shared with the lab",
            "host": "14.103.100.27",
            "port": 64101,
            "user": "root",
            "key": "~/.ssh/id_rsa",
        }
    )

    assert projected["software"] == "CalculiX 2.21, PyTorch + CUDA"
    assert projected["kind"] == "gpu"
    assert projected["device"] == "4 x A100 80G"
    assert projected["cores"] == 32
    assert projected["memory"] == "463 GB"
    assert projected["budget_unit"] == "gpu-minute"
    assert projected["concurrency"] == 2
    assert projected["note"] == "shared with the lab"


def test_ids_that_differ_only_in_padding_name_one_machine_twice(store):
    """``problems`` strips ids before duplicate detection, and ``usable`` has to
    collect its duplicate set the same way -- otherwise ``cpu-box`` and
    `` cpu-box `` are two ids there, the padded row is dropped for its own
    blocking fault, and the clean one stays usable while the registry reports a
    blocking duplicate. Reproduced in review; the test that pinned it went with
    the module it was written for.

    The collection side is the whole of it. ``usable`` strips again when it
    filters, and that second call cannot be reached while ``row_problems``
    blocks every padded id outright -- dropping it alone changes nothing here,
    so no case can show a need for it and this test does not claim one.
    """
    row = {"id": "cpu-box", "display_name": "CPU box", "transport": "local"}
    store([row, {**row, "id": " cpu-box ", "display_name": "same id, other box"}])

    assert connections.usable(connections.load()) == []
    assert any(f.blocking for f in connections.problems())


def test_two_machines_at_one_address_are_told_apart_by_id(store):
    """'on 14.103.100.27' names neither of these."""
    store(TWO)

    rows = {row["id"]: row for row in connections.load()}

    assert rows["conn_cpu"]["port"] == 58717
    assert rows["conn_gpu"]["port"] == 64101


def test_a_missing_store_is_no_connections_not_a_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(connections, "store_path", lambda: tmp_path / "nope.json")
    assert connections.load() == []


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

    assert found.state == connections.UNREADABLE
    assert "not valid JSON" in found.detail
    assert connections.load() == [], "and the rows are empty, which is the half that misled"


def test_an_absent_file_still_reads_as_absent(tmp_path, monkeypatch):
    monkeypatch.setattr(connections, "store_path", lambda: tmp_path / "nothing.json")

    assert connections.read().state == connections.MISSING
    assert connections.load() == []


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


def test_a_padded_id_is_blocking_rather_than_healthy_but_unselectable():
    """A padded id read as usable and then could not be selected, because the
    readers disagreed about stripping. Refused at the row instead of taught to
    each of them (reproduced in review)."""
    from raven.ops.connections import row_problems

    faults = row_problems(
        {"id": " cpu ", "display_name": "CPU box", "transport": "local", "budget_unit": "minute", "concurrency": 1}
    )

    assert any(f.blocking and "whitespace" in f.text for f in faults)
