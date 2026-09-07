"""The campaign-facing half of the connections suite.

The registry suite itself lives on trunk (tests/test_ops_connections.py,
upstreamed from the fork); these four stayed behind with the campaign
machinery they exercise -- the backend seam reading a campaign's named
connection, the submit path refusing to bury it, and the listing saying
which machine holds the work.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow import connections  # noqa: E402
from oncall_flow.tools import base as tools_base  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


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


def test_the_backend_reads_a_campaigns_connection(store, monkeypatch):
    """The one seam: every backend keeps reading the meta keys it always did.

    The account is checked too: make_ssh_runner takes a user and the process
    factory never passed one, so a connection saying "log in as ubuntu" would
    have been written, accepted, and quietly ignored.
    """
    from oncall_flow.backends import backend_from_meta

    seen = {}

    def _fake_runner(host, port, key, *, user="root", **kw):
        seen.update(host=host, port=port, user=user)
        return lambda cmd: (0, "")

    from oncall_flow import docker_backend

    monkeypatch.setattr(docker_backend, "make_ssh_runner", _fake_runner)
    store([{**TWO[0], "user": "ubuntu"}, TWO[1]])
    backend_from_meta({"connection": "conn_cpu", "backend": "process", "remote_dir": "/tmp/x", "command": "true"})
    assert seen == {"host": "14.103.100.27", "port": 58717, "user": "ubuntu"}


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

    import oncall_flow.tools.ops as ops
    from oncall_flow import docker_backend

    store(TWO)
    home = tmp_path / "ops"
    (home / "beam").mkdir(parents=True)
    (home / "beam" / "meta.json").write_text(
        _j.dumps({"connection": "conn_gpu", "backend": "process", "remote_dir": "/tmp/x", "command": "true"}),
        encoding="utf-8",
    )
    tools_base.set_home(home)

    seen = {}

    def _fake_runner(host, port, key, *, user="root", **kw):
        seen.update(host=host, port=port)
        return lambda cmd: (1, "refused")

    from oncall_flow import docker_backend

    monkeypatch.setattr(docker_backend, "make_ssh_runner", _fake_runner)
    asyncio.run(
        ops.OpsSubmitTool().execute(
            campaign="beam", configs=[{"n": 1}], host="14.103.100.27", round=0, eta_seconds=60, objective="x"
        )
    )
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

    import oncall_flow.tools.ops as ops
    from oncall_flow import docker_backend

    store(TWO)
    home = tmp_path / "ops"
    (home / "beam").mkdir(parents=True)
    (home / "beam" / "meta.json").write_text(
        _j.dumps({"connection": "conn_gpu", "backend": "process", "remote_dir": "/tmp/x", "command": "true"}),
        encoding="utf-8",
    )
    tools_base.set_home(home)
    monkeypatch.setattr(docker_backend, "make_ssh_runner", lambda h, p, k, **kw: lambda cmd: (1, "refused"))
    out = asyncio.run(
        ops.OpsSubmitTool().execute(campaign="beam", configs=[{"n": 1}], round=0, eta_seconds=60, objective="x")
    )
    assert "KeyError" not in out and "'host'" not in out
    assert "the GPU machine" in out or "refused" in out


def test_the_listing_of_campaigns_says_which_machine(store, tmp_path, monkeypatch):
    import asyncio
    import json as _j

    import oncall_flow.tools.ops as ops

    store(TWO)
    home = tmp_path / "ops"
    (home / "beam").mkdir(parents=True)
    (home / "beam" / "meta.json").write_text(
        _j.dumps(
            {
                "connection": "conn_cpu",
                "backend": "process",
                "remote_dir": "/tmp/x",
                "budget": {"total": 175, "unit": "core-minute"},
            }
        ),
        encoding="utf-8",
    )
    tools_base.set_home(home)
    out = asyncio.run(ops.OpsCampaignsTool().execute())
    assert "on my CPU box" in out
