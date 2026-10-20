"""The campaign-facing half of the connections suite.

The registry suite itself lives on trunk (tests/test_ops_connections.py);
these stayed behind with the campaign machinery they exercise -- the backend
seam reading a campaign's named connection, the submit path refusing to bury
it, and the listing saying which machine holds the work. The plugin reads the
registry through trunk's module since 2026-09-23, so the store is patched
where the reader lives.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow import connections  # noqa: E402
from oncall_flow.tools import base as tools_base  # noqa: E402

from raven.ops import connections as trunk  # noqa: E402
from raven.ops.transport import PASSWORD_AUTH  # noqa: E402


@pytest.fixture(autouse=True)
def _campaign_root(tmp_path):
    tools_base.set_home(tmp_path / "ops")


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "connections.json"
    monkeypatch.setattr(trunk, "store_path", lambda: path)

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


def test_the_plugin_reader_is_trunks_not_a_copy_of_it():
    """Two readers of one file used to be kept byte-aligned by hand, with a
    parity test on each side pinning the same cases; a mutation in the copy
    passed every test until the case was re-pinned (reviewed 2026-09-07). One
    reader now: the names this plugin serves are trunk's own objects."""
    for name in (
        "capacity",
        "resource_unit",
        "row_problems",
        "usable",
        "read",
        "load",
        "problems",
        "shown",
        "store_path",
    ):
        assert getattr(connections, name) is getattr(trunk, name), name
    assert connections.SHOWN is trunk.SHOWN
    assert connections.LOCAL == trunk.LOCAL and connections.SSH == trunk.SSH


def test_what_only_a_campaign_needs_stays_with_the_plugin(store):
    """The lookups a campaign makes -- by id, for the owner's name, into a meta
    -- are the plugin's; trunk keeps its registry readers to what the CLI and
    the exec lane need."""
    store(TWO)
    assert connections.get("conn_cpu")["display_name"] == "my CPU box"
    assert connections.display_name("conn_cpu") == "my CPU box"
    assert connections.display_name("nope") == "nope"
    merged = connections.resolve_into({"connection": "conn_cpu", "port": 1})
    assert merged["host"] == TWO[0]["host"] and merged["port"] == 1, "a meta's own value wins over the row"
    assert not hasattr(trunk, "resolve_into")


# ---- a machine registered with a password: every way a campaign reaches it -----------

PASSWORD_ROW = {
    "id": "conn_lab",
    "display_name": "the lab box",
    "kind": "cpu",
    "cores": 8,
    "software": "OpenFOAM",
    "budget_unit": "core-minute",
    "concurrency": 1,
    "transport": "ssh",
    "host": "203.0.113.7",
    "port": 2222,
    "user": "alice",
    "auth": "password",
}

# Stands in for ssh and rsync alike: one JSON line a call saying what it was
# handed, whether it has a session of its own, and -- as ssh, under
# SSH_ASKPASS_REQUIRE=force -- what the askpass helper answered.
_STAND_IN = """#!{python}
import json, os, subprocess, sys
saw = {{
    "name": os.path.basename(sys.argv[0]),
    "argv": sys.argv[1:],
    "askpass": os.environ.get("SSH_ASKPASS", ""),
    "require": os.environ.get("SSH_ASKPASS_REQUIRE", ""),
    "file": os.environ.get("RAVEN_MACHINE_PASSWORD_FILE", ""),
    "own_session": os.getsid(0) == os.getpid(),
}}
if saw["name"] == "ssh" and saw["require"] == "force" and saw["askpass"]:
    prompt = os.environ.get("RAVEN_MACHINE_PASSWORD_FOR", "") + "'s password: "
    asked = subprocess.run([saw["askpass"], prompt], capture_output=True, text=True)
    saw["given"] = asked.stdout.rstrip("\\n")
with open({log!r}, "a", encoding="utf-8") as fh:
    fh.write(json.dumps(saw) + "\\n")
"""


@pytest.fixture
def password_machine(store, tmp_path, monkeypatch):
    """The two key machines, and one registered by password the way trunk
    registers it: the row says ``auth: password`` and holds no key, and the
    password is kept under the home."""
    from raven.ops import connection_add

    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    store(TWO)
    connection_add.write(dict(PASSWORD_ROW))
    connection_add.store_password("conn_lab", "hunter2")


def _stand_ins(tmp_path, monkeypatch) -> Path:
    """Put the logging ssh and rsync first on PATH; returns where they log."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "saw.jsonl"
    for name in ("ssh", "rsync"):
        script = bin_dir / name
        script.write_text(_STAND_IN.format(python=sys.executable, log=str(log)), encoding="utf-8")
        script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    for name in ("SSH_ASKPASS", "SSH_ASKPASS_REQUIRE", "RAVEN_MACHINE_PASSWORD_FILE"):
        monkeypatch.delenv(name, raising=False)
    return log


def _calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_a_campaign_on_a_password_machine_is_told_how_it_logs_in(password_machine):
    """``auth`` travels into the meta with the address. Reviewed 2026-10-19:
    left behind, the meta read like a key machine's, and every ssh the campaign
    started offered a key the machine was never given."""
    merged = connections.resolve_into({"connection": "conn_lab"})

    assert merged["auth"] == "password" and merged["host"] == "203.0.113.7" and "key" not in merged
    assert "auth" not in connections.resolve_into({"connection": "conn_cpu"}), "a key machine's meta is as it was"


@pytest.mark.skipif(sys.platform == "win32", reason="password machines are refused on Windows; the stand-ins are POSIX")
def test_round_zero_on_a_password_machine_syncs_and_pulls_through_askpass(password_machine, tmp_path, monkeypatch):
    """Round zero's rsync and its image pull both log in with the password kept
    for the machine, as the account the connection names. rsync starts ssh
    itself, so the askpass environment is rsync's, and rsync runs with no
    terminal for an older ssh to ask instead."""
    from oncall_flow.backends import prepare_from_meta

    log = _stand_ins(tmp_path, monkeypatch)
    (tmp_path / "app").mkdir()

    prepare_from_meta({"connection": "conn_lab", "remote_dir": "/srv/ops"}, app_dir=str(tmp_path / "app"))

    calls = _calls(log)
    (sync,) = [c for c in calls if c["name"] == "rsync"]
    shell = sync["argv"][sync["argv"].index("-e") + 1]
    assert " ".join(PASSWORD_AUTH) in shell and "-i" not in shell.split() and "BatchMode=yes" not in shell
    assert sync["argv"][-1] == "alice@203.0.113.7:/srv/ops/app"
    assert sync["require"] == "force" and sync["askpass"] and sync["file"] == str(trunk.password_path("conn_lab"))
    assert sync["own_session"]
    pulls = [c for c in calls if c["name"] == "ssh"]
    assert [c["argv"][-1] for c in pulls] == ["docker images -q python:3.12-slim", "docker pull python:3.12-slim"]
    assert all("-i" not in c["argv"] and c["argv"][-2] == "alice@203.0.113.7" for c in pulls)
    assert all("PubkeyAuthentication=no" in c["argv"] and c["given"] == "hunter2" for c in pulls)


@pytest.mark.skipif(sys.platform == "win32", reason="password machines are refused on Windows; the stand-ins are POSIX")
def test_a_docker_campaign_on_a_password_machine_runs_its_commands_through_askpass(
    password_machine, tmp_path, monkeypatch
):
    from oncall_flow.backend import JobHandle
    from oncall_flow.backends import backend_from_meta

    log = _stand_ins(tmp_path, monkeypatch)
    backend = backend_from_meta({"connection": "conn_lab", "remote_dir": "/srv/ops"})

    asyncio.run(backend.cancel(JobHandle("docker", "ops-t1")))

    (saw,) = _calls(log)
    assert saw["argv"][-2:] == ["alice@203.0.113.7", "docker rm -f ops-t1"]
    assert "-i" not in saw["argv"] and "BatchMode=yes" not in saw["argv"] and "PubkeyAuthentication=no" in saw["argv"]
    assert saw["require"] == "force" and saw["given"] == "hunter2"


@pytest.mark.skipif(sys.platform == "win32", reason="the stand-ins are POSIX scripts")
def test_a_docker_campaign_on_a_key_machine_is_reached_as_it_always_was(store, tmp_path, monkeypatch):
    """Through the plugin's own runner, which the rest of this suite patches,
    and with the rsync line it always had."""
    from oncall_flow import docker_backend
    from oncall_flow.backends import backend_from_meta, prepare_from_meta

    store(TWO)
    log = _stand_ins(tmp_path, monkeypatch)
    (tmp_path / "app").mkdir()
    built = []

    def _fake_runner(host, port, key, **kw):
        built.append((host, port, key))
        return lambda cmd: (0, "")

    monkeypatch.setattr(docker_backend, "make_ssh_runner", _fake_runner)

    prepare_from_meta({"connection": "conn_cpu", "remote_dir": "/srv/ops"}, app_dir=str(tmp_path / "app"))
    backend_from_meta({"connection": "conn_cpu", "remote_dir": "/srv/ops"})

    key = os.path.expanduser("~/.ssh/id_rsa")
    assert built == [("14.103.100.27", 58717, key)] * 2
    (sync,) = _calls(log)
    assert sync["argv"] == [
        "-az",
        "-e",
        f"ssh -i {key} -p 58717 -o BatchMode=yes -o ConnectTimeout=15 -o StrictHostKeyChecking=accept-new",
        str(tmp_path / "app") + "/",
        "root@14.103.100.27:/srv/ops/app",
    ]
    assert sync["require"] == "" and not sync["own_session"]
