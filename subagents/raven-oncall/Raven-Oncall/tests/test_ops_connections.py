"""A machine is something the owner set up and named, not something to find.

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
    {"id": "conn_cpu", "display_name": "my CPU box", "kind": "cpu", "cores": 64,
     "software": "OpenFOAM", "budget_unit": "core-minute", "concurrency": 1,
     "host": "14.103.100.27", "port": 58717, "user": "root", "key": "~/.ssh/id_rsa"},
    {"id": "conn_gpu", "display_name": "the GPU machine", "kind": "gpu",
     "device": "4 x A100 80G", "software": "CalculiX 2.21, PyTorch + CUDA",
     "budget_unit": "gpu-minute", "concurrency": 1,
     "host": "14.103.100.27", "port": 64101, "user": "root", "key": "~/.ssh/id_rsa"},
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


def test_the_backend_reads_a_campaigns_connection(store, monkeypatch):
    """The one seam: every backend keeps reading the meta keys it always did.

    The account is checked too: make_ssh_runner takes a user and the process
    factory never passed one, so a connection saying "log in as ubuntu" would
    have been written, accepted, and quietly ignored.
    """
    import raven.ops as ops_pkg
    from raven.ops.backends import backend_from_meta

    seen = {}

    def _fake_runner(host, port, key, *, user="root", **kw):
        seen.update(host=host, port=port, user=user)
        return lambda cmd: (0, "")

    monkeypatch.setattr(ops_pkg, "make_ssh_runner", _fake_runner)
    store([{**TWO[0], "user": "ubuntu"}, TWO[1]])
    backend_from_meta({"connection": "conn_cpu", "backend": "process",
                       "remote_dir": "/tmp/x", "command": "true"})
    assert seen == {"host": "14.103.100.27", "port": 58717, "user": "ubuntu"}


def test_the_listing_of_campaigns_says_which_machine(store, tmp_path, monkeypatch):
    import asyncio
    import json as _j

    import raven.agent.tools.ops as ops

    store(TWO)
    home = tmp_path / "ops"
    (home / "beam").mkdir(parents=True)
    (home / "beam" / "meta.json").write_text(_j.dumps(
        {"connection": "conn_cpu", "backend": "process", "remote_dir": "/tmp/x",
         "budget": {"total": 175, "unit": "core-minute"}}), encoding="utf-8")
    monkeypatch.setattr(ops, "_ops_home", lambda: home)
    out = asyncio.run(ops.OpsCampaignsTool().execute())
    assert "on my CPU box" in out


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


def test_a_submit_cannot_put_port_22_over_a_campaigns_connection(store, tmp_path, monkeypatch):
    """The address comes from the connection and from nowhere else.

    ops_submit builds a meta from its own arguments -- host from the caller,
    port defaulting to 22 -- and lets the stored meta override it. A campaign
    that names a connection stores no address, so nothing overrode the default.
    Measured 2026-08-17: both FEA campaigns declared a connection on port 64106
    and every submit was refused with "connect to host ... port 22".
    """
    import asyncio
    import json as _j

    import raven.agent.tools.ops as ops
    import raven.ops as ops_pkg

    store(TWO)
    home = tmp_path / "ops"
    (home / "beam").mkdir(parents=True)
    (home / "beam" / "meta.json").write_text(_j.dumps(
        {"connection": "conn_gpu", "backend": "process",
         "remote_dir": "/tmp/x", "command": "true"}), encoding="utf-8")
    monkeypatch.setattr(ops, "_ops_home", lambda: home)

    seen = {}

    def _fake_runner(host, port, key, *, user="root", **kw):
        seen.update(host=host, port=port)
        return lambda cmd: (1, "refused")

    monkeypatch.setattr(ops_pkg, "make_ssh_runner", _fake_runner)
    asyncio.run(ops.OpsSubmitTool().execute(
        campaign="beam", configs=[{"n": 1}], host="14.103.100.27", round=0,
        eta_seconds=60, objective="x"))
    assert seen.get("port") == 64101, f"the connection's port must win, got {seen}"


def test_a_submit_says_where_the_work_went_without_an_address(store, tmp_path, monkeypatch):
    """The line that reports where the work went must not be what fails it.

    Measured 2026-08-17: clearing the caller's address left a meta with no host,
    and the message built from meta["host"] raised KeyError('host'). The agent
    could only read that as "the tool wants a host", so it passed one by hand
    and then edited the campaign's own declaration to add the field.
    """
    import asyncio
    import json as _j

    import raven.agent.tools.ops as ops
    import raven.ops as ops_pkg

    store(TWO)
    home = tmp_path / "ops"
    (home / "beam").mkdir(parents=True)
    (home / "beam" / "meta.json").write_text(_j.dumps(
        {"connection": "conn_gpu", "backend": "process",
         "remote_dir": "/tmp/x", "command": "true"}), encoding="utf-8")
    monkeypatch.setattr(ops, "_ops_home", lambda: home)
    monkeypatch.setattr(ops_pkg, "make_ssh_runner",
                        lambda h, p, k, **kw: (lambda cmd: (1, "refused")))
    out = asyncio.run(ops.OpsSubmitTool().execute(
        campaign="beam", configs=[{"n": 1}], round=0, eta_seconds=60, objective="x"))
    assert "KeyError" not in out and "'host'" not in out
    assert "the GPU machine" in out or "refused" in out
