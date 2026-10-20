"""``machines.*`` -- the Agent Hub's machines tab over the machine registry.

What these pin is the one rule the contract adds to the command's: nothing a
row or a refusal carries is the way onto the machine. The registry is a
scratch file, the node pool and the installer are stood in for, and every
answer is validated against its declared result model.
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading

import pytest
from pydantic import ValidationError

from raven.acp_client import remote
from raven.node import install as node_install
from raven.node.client import MachineStatus, NodeError
from raven.ops import connection_add, connections
from raven.ops.transport import ASKPASS_REFUSED
from raven.rpc.errors import ConfigValidationError
from raven.rpc.methods import machines as rpc
from raven.rpc.models import METHOD_MODELS
from tests._home import point_home


def _check(method: str, payload: dict) -> None:
    """A handler's real answer against its declared result model."""
    _, model = METHOD_MODELS[method]
    model.model_validate(payload)


BOX = {
    "id": "box",
    "display_name": "Lab box",
    "transport": "ssh",
    "host": "203.0.113.7",
    "port": 2222,
    "user": "alice",
    "key": "/home/me/.ssh/id_box",
    "paths": ["/data/work"],
}
HERE = {"id": "here", "display_name": "This computer", "transport": "local"}
PASSWORD_BOX = {k: v for k, v in BOX.items() if k != "key"} | {"auth": "password"}
SECRETS = ("203.0.113.7", "alice", "id_box")


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    point_home(monkeypatch, tmp_path)
    monkeypatch.setattr(connections, "store_path", lambda: tmp_path / "connections.json")
    rpc._INSTALLS.clear()
    yield tmp_path
    rpc._INSTALLS.clear()


def _store(*rows: dict) -> None:
    connections.store_path().write_text(json.dumps({"connections": list(rows)}), encoding="utf-8")


def _rows() -> list[dict]:
    return json.loads(connections.store_path().read_text(encoding="utf-8"))["connections"]


class _Pool:
    """The node pool, stood in for: a status per machine, one hello answer, and what was asked of it."""

    def __init__(self) -> None:
        self.statuses: dict[str, MachineStatus] = {}
        self.hello_answer: dict | Exception = {"version": "0.2.4", "digest": "abcdef012345", "python": "3.12.3"}
        self.asked: list[tuple[str, str]] = []

    def status(self, machine_id: str) -> MachineStatus:
        return self.statuses.get(machine_id, MachineStatus("off"))

    async def hello(self, machine_id: str) -> dict:
        self.asked.append(("hello", machine_id))
        if isinstance(self.hello_answer, Exception):
            raise self.hello_answer
        return dict(self.hello_answer)

    async def close(self, machine_id: str) -> None:
        self.asked.append(("close", machine_id))


@pytest.fixture
def pool(monkeypatch) -> _Pool:
    stand_in = _Pool()
    monkeypatch.setattr(rpc, "get_node_pool", lambda: stand_in)
    return stand_in


@pytest.fixture
def agents(monkeypatch) -> list[str]:
    """The connected agent entries that name a machine, as the config would list them."""
    named: list[str] = []
    monkeypatch.setattr(rpc, "agents_on_machine", lambda conn_id, only_enabled=False: list(named))
    return named


@pytest.fixture
def this_node(monkeypatch) -> str:
    monkeypatch.setattr(rpc.bundle, "install_name", lambda: "0.2.4-abcdef012345")
    return "0.2.4-abcdef012345"


# --- machines.list ---------------------------------------------------------------


async def test_list_says_what_a_machine_is_and_how_its_link_is_doing_and_never_the_way_onto_it(pool):
    _store(BOX, HERE)
    pool.statuses["box"] = MachineStatus("connected", agents=2, version="0.2.4")

    out = await rpc.machines_list({})

    _check("machines.list", out)
    box, here = out["rows"]
    assert box == {
        "id": "box",
        "name": "Lab box",
        "transport": "ssh",
        "state": "connected",
        "detail": "",
        "agents": 2,
        "node_version": "0.2.4",
        "node_current": True,
        "auth": "key",
        "install": {"running": False, "detail": ""},
        "paths": ["/data/work"],
        "problems": [],
        "agents_here": [],
        "agents_off_here": [],
    }
    assert here["transport"] == "local" and here["state"] == "off" and "node_version" not in here
    for secret in SECRETS:
        assert secret not in json.dumps(out)


async def test_list_names_the_connected_agent_entries_that_run_on_each_machine(pool, monkeypatch):
    """The line a reader whose machine is up and idle was missing: what is connected here, by name."""
    _store(BOX, HERE)
    asked: list[tuple[str, bool]] = []

    def named(conn_id: str, only_enabled: bool = False) -> list[str]:
        asked.append((conn_id, only_enabled))
        if conn_id != "box":
            return []
        return ["Claude Code@box", "codex@box"] if only_enabled else ["Claude Code@box", "codex@box", "hermes@box"]

    monkeypatch.setattr(rpc, "agents_on_machine", named)

    out = await rpc.machines_list({})

    _check("machines.list", out)
    box, here = out["rows"]
    assert box["agents_here"] == ["Claude Code@box", "codex@box"]
    # The switched-off one is listed apart, not dropped: it still holds the machine for the add
    # (seen 2026-10-20: the card said "no agent here" while the form greyed the preset as taken).
    assert box["agents_off_here"] == ["hermes@box"]
    assert here["agents_here"] == [] and here["agents_off_here"] == []
    assert ("box", True) in asked and ("box", False) in asked


async def test_list_lists_no_agent_over_a_config_file_that_does_not_parse_rather_than_failing(
    pool, monkeypatch, tmp_path
):
    """A real file, not a stand-in: `// a note` is not JSON, the loader raises its own ConfigReadError (not a
    ValidationError), and the whole listing failed with internal_error while the gateway itself runs on."""
    _store(BOX)
    config = tmp_path / "config.json"
    config.write_text('{ // my note\n  "subagents": {"agents": []}\n}\n', encoding="utf-8")
    monkeypatch.setattr("raven.config.update_subagents.get_config_path", lambda: config)

    out = await rpc.machines_list({})

    _check("machines.list", out)
    assert [r["id"] for r in out["rows"]] == ["box"] and out["rows"][0]["agents_here"] == []


async def test_list_lists_no_agent_over_a_malformed_agents_section_rather_than_failing(pool, monkeypatch):
    _store(BOX)

    def broken(conn_id: str, only_enabled: bool = False) -> list[str]:
        raise ValidationError.from_exception_data("SubagentsConfig", [])

    monkeypatch.setattr(rpc, "agents_on_machine", broken)

    out = await rpc.machines_list({})

    _check("machines.list", out)
    assert out["rows"][0]["agents_here"] == [] and out["rows"][0]["agents_off_here"] == []


# --- machines.local_keys ---------------------------------------------------------


async def test_local_keys_proposes_ssh_s_own_identities_first_then_any_key_with_its_public_half(_home):
    ssh = _home / ".ssh"
    ssh.mkdir()
    for name in (
        "id_rsa",
        "id_rsa.pub",
        "id_ed25519",
        "work_key",
        "work_key.pub",
        "lonely.pub",
        "config",
        "known_hosts",
        "authorized_keys",
    ):
        (ssh / name).write_text("x", encoding="utf-8")
    (ssh / "a_dir.pub").mkdir()

    out = await rpc.machines_local_keys({})

    _check("machines.local_keys", out)
    # rsa before ed25519, as ssh offers them (`ssh -G` lists id_rsa first on OpenSSH 8.9, 9.6 and 9.9),
    # whether or not a .pub is beside it; then the owner's own pair by name; never a .pub, a public
    # half with no private one, or ssh's files.
    assert out["keys"] == ["~/.ssh/id_rsa", "~/.ssh/id_ed25519", "~/.ssh/work_key"]


async def test_local_keys_proposes_nothing_on_a_computer_without_an_ssh_directory(_home):
    out = await rpc.machines_local_keys({})

    _check("machines.local_keys", out)
    assert out == {"keys": []}


async def test_list_reads_the_node_off_the_row_when_no_link_is_up_and_says_whether_it_is_current(pool, this_node):
    _store(
        {**BOX, "node_install": this_node},
        {**BOX, "id": "old", "node_install": "0.2.3-0123456789ab"},
        {**BOX, "id": "bare"},
    )

    rows = {r["id"]: r for r in (await rpc.machines_list({}))["rows"]}

    assert (rows["box"]["node_version"], rows["box"]["node_current"]) == ("0.2.4", True)
    assert (rows["old"]["node_version"], rows["old"]["node_current"]) == ("0.2.3", False)
    assert "node_version" not in rows["bare"] and rows["bare"]["node_current"] is False


async def test_list_takes_the_address_out_of_the_pool_s_sentence_and_names_the_problems_that_block_a_row(pool):
    _store(BOX, {"id": "half", "display_name": "Half", "transport": "ssh", "host": "h"})
    pool.statuses["box"] = MachineStatus("error", "ssh: connect to host 203.0.113.7 port 2222: Connection refused")

    rows = {r["id"]: r for r in (await rpc.machines_list({}))["rows"]}

    assert rows["box"]["state"] == "error"
    assert rows["box"]["detail"] == "ssh: connect to host the machine port 2222: Connection refused"
    assert rows["half"]["problems"] == [
        "half: 'port' is missing, so there is no way onto it",
        "half: 'user' is missing, so there is no way onto it",
        "half: 'key' is missing, so there is no way onto it",
    ]


async def test_refresh_reaches_the_usable_ssh_rows_or_the_one_named_and_notes_the_node_it_finds(pool):
    _store(BOX, {**BOX, "id": "two"}, HERE, {"id": "half", "transport": "ssh", "host": "h"})

    await rpc.machines_list({"refresh": True})

    assert sorted(pool.asked) == [("hello", "box"), ("hello", "two")]
    assert {r["id"]: r.get("node_install") for r in _rows()} == {
        "box": "0.2.4-abcdef012345",
        "two": "0.2.4-abcdef012345",
        "here": None,
        "half": None,
    }
    pool.asked.clear()
    await rpc.machines_list({"refresh": True, "id": "two"})
    assert pool.asked == [("hello", "two")]


async def test_refresh_leaves_the_pool_s_refusal_for_the_listing_and_writes_nothing(pool):
    _store(BOX)
    said = "no Raven node on machine 'Lab box' (box); install it with `raven ops connection install-node box`"
    pool.hello_answer = NodeError(said)
    pool.statuses["box"] = MachineStatus("error", said)

    out = await rpc.machines_list({"refresh": True})

    assert out["rows"][0]["state"] == "error" and out["rows"][0]["detail"] == said
    assert "node_install" not in _rows()[0]


async def test_an_unreadable_registry_is_a_config_error_not_an_empty_list(pool):
    connections.store_path().write_text("{not json", encoding="utf-8")

    with pytest.raises(ConfigValidationError) as caught:
        await rpc.machines_list({})

    assert "not valid JSON" in caught.value.detail


# --- machines.add ----------------------------------------------------------------

FORM = {
    "id": "box",
    "name": "Lab box",
    "host": "203.0.113.7",
    "port": 2222,
    "user": "alice",
    "key": "~/.ssh/id_box",
    "paths": ["/data/work", " "],
}


@pytest.fixture
def reached(monkeypatch):
    monkeypatch.setattr(
        connection_add,
        "probe",
        lambda row, **kw: (
            True,
            f"reached {row['user']}@{row['host']}:{row['port']}",
            {"cores": 32, "memory": "128 GB", "gpus": 2, "device": "2 x NVIDIA A800", "kind": "gpu"},
        ),
    )


async def test_add_reaches_the_machine_then_writes_the_row_and_its_alias_and_answers_without_the_way_in(
    pool, reached, _home
):
    out = await rpc.machines_add(FORM)

    _check("machines.add", out)
    assert out["ok"] and out["reason"] == ""
    assert out["detail"] == "reached machine 'Lab box' (box): 32 cores, 128 GB, 2 x NVIDIA A800"
    assert out["row"]["id"] == "box" and out["row"]["paths"] == ["/data/work"] and out["row"]["state"] == "off"
    for secret in SECRETS:
        assert secret not in json.dumps(out)
    (row,) = _rows()
    assert (row["host"], row["port"], row["user"], row["key"]) == ("203.0.113.7", 2222, "alice", "~/.ssh/id_box")
    assert row["kind"] == "gpu" and row["gpus"] == 2 and row["budget_unit"] == "gpu-minute"
    assert "concurrency" not in row, "a row that hands out devices is admitted by them, not by a job count"
    assert "Host box" in (_home / ".ssh" / "config").read_text(encoding="utf-8")


async def test_add_counts_jobs_on_a_machine_that_hands_out_nothing_countable(pool, monkeypatch):
    monkeypatch.setattr(connection_add, "probe", lambda row, **kw: (True, "reached", {}))

    out = await rpc.machines_add({**FORM, "port": None, "paths": None})

    assert out["ok"] and out["detail"] == "reached machine 'Lab box' (box): nothing it could say about itself"
    (row,) = _rows()
    assert row["port"] == 22 and row["concurrency"] == 1 and row["budget_unit"] == "core-minute"
    assert "paths" not in row


async def test_add_refuses_before_reaching_anything_over_a_missing_field_a_bad_id_or_a_taken_one(pool, monkeypatch):
    probes: list[dict] = []
    monkeypatch.setattr(connection_add, "probe", lambda row, **kw: probes.append(row) or (True, "", {}))

    out = await rpc.machines_add({**FORM, "host": ""})
    assert (out["ok"], out["reason"]) == (False, "invalid") and "'host' is missing" in out["detail"]
    out = await rpc.machines_add({**FORM, "id": "Lab Box"})
    assert out["reason"] == "invalid" and "lowercase" in out["detail"]
    _store(BOX)
    out = await rpc.machines_add(FORM)
    assert (out["ok"], out["reason"]) == (False, "duplicate")

    assert probes == [] and len(_rows()) == 1


@pytest.mark.parametrize(
    "message, reason",
    [
        (
            "could not reach alice@203.0.113.7:2222: connect to host 203.0.113.7 port 2222: Connection refused",
            "unreachable",
        ),
        ("could not reach alice@203.0.113.7:2222: alice@203.0.113.7: Permission denied (publickey).", "login"),
        ("could not reach alice@203.0.113.7:2222: Host key verification failed.", "hostkey"),
        ("could not reach alice@203.0.113.7:2222: no answer within 30s", "timeout"),
        ("could not reach alice@203.0.113.7:2222: ssh not found on this computer", "nossh"),
        (
            f"could not reach alice@203.0.113.7:2222: {ASKPASS_REFUSED}\n"
            "alice@203.0.113.7: Permission denied (keyboard-interactive).",
            "prompt",
        ),
    ],
)
async def test_a_machine_that_cannot_be_reached_is_not_written_and_the_refusal_names_its_kind_not_the_address(
    pool, monkeypatch, message, reason
):
    monkeypatch.setattr(connection_add, "probe", lambda row, **kw: (False, message, {}))

    out = await rpc.machines_add(FORM)

    _check("machines.add", out)
    assert (out["ok"], out["reason"]) == (False, reason)
    assert "203.0.113.7" not in out["detail"] and "alice@" not in out["detail"]
    assert out["detail"].startswith("could not reach the machine")
    assert not connections.store_path().exists()


async def test_add_says_when_the_alias_could_not_be_written_and_still_registers(pool, reached, monkeypatch):
    def refuse(row):
        raise OSError("read-only file system")

    monkeypatch.setattr(connection_add, "write_ssh_alias", refuse)

    out = await rpc.machines_add(FORM)

    assert out["ok"] and "ssh alias was not written to ~/.ssh/config (read-only file system)" in out["detail"]
    assert len(_rows()) == 1


async def test_add_refuses_over_a_registry_it_cannot_rewrite_without_reaching_the_machine(pool, monkeypatch):
    probes: list[dict] = []
    monkeypatch.setattr(connection_add, "probe", lambda row, **kw: probes.append(row) or (True, "", {}))
    connections.store_path().write_text(json.dumps({"connections": [{"host": "no-id"}]}), encoding="utf-8")

    out = await rpc.machines_add(FORM)

    assert (out["ok"], out["reason"]) == (False, "registry") and "had no id" in out["detail"]
    connections.store_path().write_text("{nope", encoding="utf-8")
    out = await rpc.machines_add(FORM)
    assert (out["ok"], out["reason"]) == (False, "registry") and "not valid JSON" in out["detail"]
    assert probes == []


# --- machines.install_node -------------------------------------------------------


class _Installer:
    """raven.node.install stood in for: a plan, a refusal, an install, and what was asked of them."""

    def __init__(self, monkeypatch, *, registered_dir: str = "", present: bool = False) -> None:
        self.present = present
        self.refusal: str | None = None
        self.plan_error: str | None = None
        self.install_error: str | None = None
        self.asked: list[tuple[str, object]] = []

        def plan(conn_id: str, *, node_dir: str | None = None) -> node_install.Plan:
            self.asked.append(("plan", node_dir))
            if self.plan_error:
                raise node_install.InstallError(self.plan_error)
            row = {"id": conn_id, **({"node_dir": registered_dir} if registered_dir else {})}
            return node_install.Plan(
                target=remote.Machine(id=conn_id, display_name="Lab box", _row=row),
                node_dir=node_dir or registered_dir or "~/.raven-node",
                install_name="0.2.4-abcdef012345",
                present=self.present,
                python="/usr/bin/python3.12",
                uv="",
                free_kb=None,
            )

        def install(p: node_install.Plan) -> node_install.Installed:
            self.asked.append(("install", p.node_dir))
            if self.install_error:
                raise node_install.InstallError(self.install_error)
            return node_install.Installed(p.node_dir, p.install_name, 52 * 1024, rg=True)

        monkeypatch.setattr(node_install, "plan", plan)
        monkeypatch.setattr(node_install, "refusal", lambda p: self.refusal)
        monkeypatch.setattr(node_install, "install", install)


async def _settled(conn_id: str) -> rpc._Install:
    rec = rpc._INSTALLS[conn_id]
    assert rec.task is not None
    await rec.task
    return rec


async def test_install_node_runs_in_the_background_and_the_list_reports_it_until_the_node_answers(
    pool, monkeypatch, this_node
):
    installer = _Installer(monkeypatch)
    _store(BOX)

    out = await rpc.machines_install_node({"id": "box", "node_dir": "/data/raven-node/"})

    _check("machines.install_node", out)
    assert out == {"started": True, "detail": "installing the Raven node on machine 'Lab box' (box)"}
    row = (await rpc.machines_list({}))["rows"][0]
    assert row["install"] == {"running": True, "detail": "looking at machine 'Lab box' (box)"}
    again = await rpc.machines_install_node({"id": "box"})
    assert again == {"started": False, "detail": "an install is already running on machine 'box'"}

    rec = await _settled("box")

    assert rec.ok is True
    assert rec.detail == (
        "Installed the Raven node 0.2.4-abcdef012345 in /data/raven-node (52 MB). The node answers: Python 3.12.3."
    )
    assert installer.asked == [("plan", "/data/raven-node"), ("install", "/data/raven-node")]
    assert pool.asked == [("hello", "box")]
    (stored,) = _rows()
    assert stored["node_dir"] == "/data/raven-node" and stored["node_install"] == this_node
    row = (await rpc.machines_list({}))["rows"][0]
    assert row["install"] == {"running": False, "ok": True, "detail": rec.detail}
    assert (row["node_version"], row["node_current"]) == ("0.2.4", True)
    _check("machines.list", {"rows": [row]})


async def test_install_node_leaves_a_machine_that_already_has_the_node_as_it_is_and_still_notes_it(pool, monkeypatch):
    installer = _Installer(monkeypatch, present=True)
    _store(BOX)

    await rpc.machines_install_node({"id": "box"})
    rec = await _settled("box")

    assert rec.ok is True
    assert rec.detail == (
        "Machine 'Lab box' (box) already has the Raven node 0.2.4-abcdef012345 in ~/.raven-node. "
        "The node answers: Python 3.12.3."
    )
    assert installer.asked == [("plan", None)]
    (stored,) = _rows()
    assert stored["node_install"] == "0.2.4-abcdef012345" and "node_dir" not in stored


@pytest.mark.parametrize(
    "field, said",
    [
        ("refusal", "machine 'Lab box' (box) has no uv, and no curl or wget to fetch one with"),
        ("plan_error", "could not look at machine 'Lab box' (box)"),
        ("install_error", "installing the Raven node on machine 'Lab box' (box) failed at the packages step: exit 1"),
    ],
)
async def test_install_node_stops_at_the_first_thing_that_fails_and_the_list_says_what(pool, monkeypatch, field, said):
    installer = _Installer(monkeypatch)
    setattr(installer, field, said)
    _store(BOX)

    assert (await rpc.machines_install_node({"id": "box"}))["started"]
    rec = await _settled("box")

    assert (rec.ok, rec.detail) == (False, said)
    assert "node_install" not in _rows()[0] and pool.asked == []
    row = (await rpc.machines_list({}))["rows"][0]
    assert row["install"] == {"running": False, "ok": False, "detail": said}
    # The next install on the machine starts: the end of the last one is not a lock.
    installer.refusal = installer.plan_error = installer.install_error = None
    assert (await rpc.machines_install_node({"id": "box"}))["started"]
    assert (await _settled("box")).ok is True


async def test_install_node_refuses_a_machine_that_is_not_registered_or_is_this_computer(pool):
    out = await rpc.machines_install_node({"id": "ghost"})
    assert out["started"] is False and "not registered" in out["detail"]
    _store(HERE)
    out = await rpc.machines_install_node({"id": "here"})
    assert out["started"] is False and "this computer" in out["detail"]
    assert rpc._INSTALLS == {}


async def test_a_node_that_does_not_answer_after_the_install_is_a_failed_install(pool, monkeypatch):
    _Installer(monkeypatch)
    _store(BOX)
    pool.hello_answer = NodeError("the Raven node on machine 'Lab box' (box) stopped")

    await rpc.machines_install_node({"id": "box"})
    rec = await _settled("box")

    assert (rec.ok, rec.detail) == (False, "the Raven node on machine 'Lab box' (box) stopped")


async def test_whatever_else_ends_an_install_reaches_the_page_as_its_end(pool, monkeypatch):
    _Installer(monkeypatch)
    _store(BOX)

    def boom(p):
        raise RuntimeError("the disk went away")

    monkeypatch.setattr(node_install, "install", boom)

    await rpc.machines_install_node({"id": "box"})
    rec = await _settled("box")

    assert rec.running is False and rec.ok is False
    assert rec.detail == "installing the Raven node on machine 'box' failed: the disk went away"


# --- machines.doctor -------------------------------------------------------------


async def test_doctor_names_the_row_each_problem_is_about_and_the_file_s_own_with_no_row(pool):
    connections.store_path().write_text(
        json.dumps(
            {
                "connections": [
                    {**BOX, "budget_unit": "hours"},
                    {"id": "half", "transport": "ssh", "host": "h"},
                    {"host": "no id"},
                ]
            }
        ),
        encoding="utf-8",
    )

    out = await rpc.machines_doctor({})

    _check("machines.doctor", out)
    by_id: dict[str, list[tuple[bool, str]]] = {}
    for p in out["problems"]:
        by_id.setdefault(p["id"], []).append((p["blocking"], p["detail"]))
    (file_problem,) = by_id[""]
    assert file_problem[0] is False and "had no id and were skipped" in file_problem[1]
    assert any(not blocking and "'budget_unit' should be one of" in said for blocking, said in by_id["box"])
    # No name, no port, no user, no key: four things without which there is no way onto it.
    assert sum(blocking for blocking, _ in by_id["half"]) == 4


# --- machines.remove -------------------------------------------------------------


async def test_remove_takes_the_row_and_its_alias_out_closes_the_link_and_leaves_the_node_there(pool, _home, agents):
    _store(BOX, {**BOX, "id": "two"})
    connection_add.write_ssh_alias(BOX)
    connection_add.write_ssh_alias({**BOX, "id": "two"})
    rpc._INSTALLS["box"] = rpc._Install(running=False, ok=True, detail="done")

    out = await rpc.machines_remove({"id": "box"})

    _check("machines.remove", out)
    assert out == {
        "removed": True,
        "detail": "removed machine 'Lab box' (box) from the registry; its ssh alias is gone from ~/.ssh/config; "
        "the node installed on it stays there",
        "agents": [],
    }
    assert [r["id"] for r in _rows()] == ["two"]
    text = (_home / ".ssh" / "config").read_text(encoding="utf-8")
    assert "Host box" not in text and "Host two" in text
    assert pool.asked == [("close", "box")] and "box" not in rpc._INSTALLS


async def test_remove_forgets_the_password_kept_for_a_password_row_and_asks_the_machine_for_nothing(
    pool, _home, agents, monkeypatch
):
    monkeypatch.setenv("RAVEN_HOME", str(_home / "rhome"))
    kept = connection_add.store_password("box", "hunter2")
    _store(PASSWORD_BOX)

    out = await rpc.machines_remove({"id": "box"})

    _check("machines.remove", out)
    assert out["removed"]
    assert out["detail"].endswith("; the node installed on it stays there; the password kept here for it is deleted")
    assert not kept.exists() and _rows() == []
    assert pool.asked == [("close", "box")], "nothing is asked of the machine itself"


async def test_remove_of_a_key_row_forgets_nothing_and_says_nothing_about_a_password(pool, _home, agents):
    _store(BOX)

    out = await rpc.machines_remove({"id": "box"})

    assert out["removed"] and "password" not in out["detail"]


async def test_remove_of_a_key_row_deletes_a_password_file_left_under_its_id_and_says_so(
    pool, _home, agents, monkeypatch
):
    """A file kept under the id by a registration refused later, or by a row since re-registered with a key, is
    nobody's once the row is gone; removal is the one place that reaches it."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("RAVEN_HOME", str(_home / "rhome"))
    _store(BOX)
    connection_add.store_password("box", "left-behind-1")

    out = await rpc.machines_remove({"id": "box"})

    assert out["removed"] and out["detail"].endswith("; the password kept here for it is deleted")
    assert not connection_add.password_path("box").exists()


async def test_remove_refuses_while_agent_entries_name_the_machine_and_force_says_what_is_left_pointing_at_nothing(
    pool, agents
):
    _store(BOX)
    agents += ["claude_code@box", "codex@box"]

    out = await rpc.machines_remove({"id": "box"})

    assert out == {
        "removed": False,
        "detail": "2 connected agent entries name machine 'Lab box' (box) (claude_code@box, codex@box); "
        "disconnect them first",
        "agents": ["claude_code@box", "codex@box"],
    }
    assert len(_rows()) == 1 and pool.asked == []

    out = await rpc.machines_remove({"id": "box", "force": True})

    assert out["removed"] and out["agents"] == ["claude_code@box", "codex@box"]
    assert out["detail"].endswith("; claude_code@box, codex@box still name it and fail at dispatch until disconnected")
    assert _rows() == []


async def test_a_disconnected_entry_does_not_refuse_the_removal_but_is_named_as_left_pointing_at_nothing(
    pool, monkeypatch
):
    def entries(conn_id, only_enabled=False):
        return ["claude_code@box"] if only_enabled else ["claude_code@box", "codex@box"]

    monkeypatch.setattr(rpc, "agents_on_machine", entries)
    _store(BOX)

    out = await rpc.machines_remove({"id": "box"})
    assert out["removed"] is False and out["agents"] == ["claude_code@box"]

    monkeypatch.setattr(
        rpc, "agents_on_machine", lambda conn_id, only_enabled=False: [] if only_enabled else ["codex@box"]
    )
    out = await rpc.machines_remove({"id": "box"})

    assert out["removed"] is True and out["agents"] == ["codex@box"]
    assert out["detail"].endswith(
        "; the disconnected entry codex@box still names it and cannot be connected until it is registered again"
    )
    assert _rows() == []


async def test_remove_refuses_while_agents_run_on_the_machine_unless_forced(pool, agents):
    _store(BOX)
    pool.statuses["box"] = MachineStatus("lost", "the link went", agents=1)

    out = await rpc.machines_remove({"id": "box"})

    assert out == {
        "removed": False,
        "detail": "1 agent is running on machine 'Lab box' (box) right now; wait for them to finish or stop them first",
        "agents": [],
    }
    out = await rpc.machines_remove({"id": "box", "force": True})
    assert out["removed"] and pool.asked == [("close", "box")] and _rows() == []


async def test_remove_says_when_there_is_nothing_to_remove_or_the_registry_cannot_be_changed(pool, agents):
    out = await rpc.machines_remove({"id": "ghost"})
    assert out == {"removed": False, "detail": "machine 'ghost' is not registered", "agents": []}

    connections.store_path().write_text(json.dumps({"connections": [BOX, {"host": "no id"}]}), encoding="utf-8")
    out = await rpc.machines_remove({"id": "box"})
    assert out["removed"] is False and "had no id" in out["detail"] and len(_rows()) == 2

    connections.store_path().write_text("{nope", encoding="utf-8")
    out = await rpc.machines_remove({"id": "box"})
    assert out["removed"] is False and "not valid JSON" in out["detail"]
    assert pool.asked == []


async def test_remove_of_a_local_row_closes_no_link_and_says_nothing_about_a_node(pool, agents):
    _store(HERE)

    out = await rpc.machines_remove({"id": "here"})

    assert out == {"removed": True, "detail": "removed machine 'This computer' (here) from the registry", "agents": []}
    assert pool.asked == [] and _rows() == []


@pytest.mark.parametrize(
    "exc", [OSError("read-only file system"), ValueError("the managed 'Host box' block in ~/.ssh/config is incomplete")]
)
async def test_remove_says_when_the_alias_could_not_be_removed_and_still_removes_the_row(
    pool, agents, monkeypatch, exc
):
    _store(BOX)

    def refuse(conn_id):
        raise exc

    monkeypatch.setattr(connection_add, "remove_ssh_alias", refuse)

    out = await rpc.machines_remove({"id": "box"})

    assert out["removed"] and f"could not be removed from ~/.ssh/config ({exc})" in out["detail"]
    assert _rows() == []


async def test_add_says_when_the_managed_block_is_incomplete_and_still_registers(pool, reached, monkeypatch):
    def refuse(row):
        raise ValueError("the managed 'Host box' block in ~/.ssh/config is incomplete")

    monkeypatch.setattr(connection_add, "write_ssh_alias", refuse)

    out = await rpc.machines_add(FORM)

    assert out["ok"] and "ssh alias was not written to ~/.ssh/config (the managed 'Host box' block" in out["detail"]
    assert len(_rows()) == 1


async def test_remove_reads_a_malformed_agents_section_as_naming_nothing(pool, monkeypatch):
    def broken(conn_id, **kw):
        raise ValidationError.from_exception_data("SubagentsConfig", [])

    monkeypatch.setattr(rpc, "agents_on_machine", broken)
    _store(BOX)

    assert (await rpc.machines_remove({"id": "box"}))["removed"] is True


# --- machines.add by password: the password stays here, used on every connection ------

BY_PASSWORD = {k: v for k, v in FORM.items() if k != "key"} | {"auth": "password", "password": "hunter2"}


@pytest.fixture
def rhome(_home, monkeypatch):
    monkeypatch.setenv("RAVEN_HOME", str(_home / "rhome"))
    return _home / "rhome"


async def test_add_by_password_keeps_the_password_here_proves_it_through_the_probe_and_writes_a_row_with_no_key(
    supported_host, pool, rhome, monkeypatch
):
    probed: list[tuple[dict, str]] = []

    def probe(row, **kw):
        assert kw["password"] == BY_PASSWORD["password"]
        # The probe runs as every later connection will: with the password already kept here.
        probed.append((dict(row), connection_add.password_path("box").read_text(encoding="utf-8")))
        return True, "reached alice@203.0.113.7:2222", {"cores": 8}

    monkeypatch.setattr(connection_add, "probe", probe)

    out = await rpc.machines_add(BY_PASSWORD)

    _check("machines.add", out)
    assert out["ok"] and out["reason"] == "" and out["row"]["auth"] == "password"
    ((row, kept),) = probed
    assert row["auth"] == "password" and "key" not in row and "password" not in row and kept == "hunter2\n"
    (stored,) = _rows()
    assert stored["auth"] == "password" and "key" not in stored and "password" not in stored
    assert connection_add.password_path("box").read_text(encoding="utf-8") == "hunter2\n"
    assert "hunter2" not in json.dumps(out) and "hunter2" not in json.dumps(_rows())
    assert not (rhome.parent / ".ssh" / "config").exists(), "no alias: an ssh config has nowhere to carry a password"


async def test_add_by_password_refuses_a_password_shorter_than_the_scrubbers_floor_before_the_probe(
    supported_host, pool, rhome, monkeypatch
):
    monkeypatch.setattr(connection_add, "probe", lambda row, **kw: pytest.fail("the probe is not reached"))

    out = await rpc.machines_add({**BY_PASSWORD, "password": "root1"})

    _check("machines.add", out)
    assert (out["ok"], out["reason"]) == (False, "invalid")
    assert "shorter than 6 characters cannot be kept" in out["detail"] and "root1" not in out["detail"]
    assert not (rhome / "keys").exists(), "nothing was kept, not even the directory"
    assert not connections.store_path().exists(), "and no row was written"


async def test_a_write_refused_because_the_id_landed_meanwhile_leaves_the_password_file_to_that_row(
    supported_host, pool, rhome, monkeypatch
):
    """Between the duplicate check and the write another registration of the same id can land, with its own
    password under the same file name: the file is that row's now, and the refused registration leaves it."""
    monkeypatch.setattr(connection_add, "probe", lambda row, **kw: (True, "reached", {}))

    def refuse(row, **kwargs):
        connection_add.store_password(row["id"], "rpc-pw-2222")  # the competitor's, kept under the same id
        raise ValueError(f"a machine with id {row['id']!r} is already listed in the registry")

    monkeypatch.setattr(connection_add, "write", refuse)

    out = await rpc.machines_add(BY_PASSWORD)

    _check("machines.add", out)
    assert (out["ok"], out["reason"]) == (False, "duplicate")
    assert connection_add.password_path("box").read_text(encoding="utf-8") == "rpc-pw-2222\n"


async def test_a_refusal_forgets_only_a_file_that_still_holds_this_registration_s_password(
    supported_host, pool, rhome, monkeypatch
):
    """The machine refuses after a registration of the same id replaced the file: the file is left as it is."""

    def probe(row, **kw):
        connection_add.store_password(row["id"], "rpc-pw-2222")
        return False, "could not reach alice@203.0.113.7:2222: alice@203.0.113.7: Permission denied (password).", {}

    monkeypatch.setattr(connection_add, "probe", probe)

    out = await rpc.machines_add(BY_PASSWORD)

    assert (out["ok"], out["reason"]) == (False, "login")
    assert connection_add.password_path("box").read_text(encoding="utf-8") == "rpc-pw-2222\n"


async def test_a_refusal_after_the_same_machine_registered_meanwhile_leaves_the_password_it_shares(
    supported_host, pool, rhome, monkeypatch
):
    """The realistic interleaving: the same machine submitted twice with the same password. The second
    submission's probe is refused (the machine saw one login too many, say) after the first wrote its row; the
    file now holds the first row's password, byte for byte the same, and the refusal must leave it."""
    import raven.ops.connections as registry

    listed: list[dict] = []
    monkeypatch.setattr(registry, "read", lambda: registry.Read(list(listed), registry.OK))

    def probe(row, **kw):
        listed.append({**row, "auth": "password"})  # the first submission's row lands during this probe
        return False, "could not reach alice@203.0.113.7:2222: alice@203.0.113.7: Permission denied (password).", {}

    monkeypatch.setattr(connection_add, "probe", probe)

    out = await rpc.machines_add(BY_PASSWORD)

    assert (out["ok"], out["reason"]) == (False, "login")
    assert connection_add.password_path("box").read_text(encoding="utf-8") == BY_PASSWORD["password"] + "\n"


@pytest.mark.skipif(sys.platform == "win32", reason="password machines are refused on Windows before this point")
async def test_a_password_machine_at_another_machine_s_alias_is_refused_as_a_field_that_cannot_be_used(
    supported_host, pool, rhome, monkeypatch
):
    """``Host labbox`` in ~/.ssh/config is the alias Raven keeps for the key machine ``labbox``; a password machine
    registered at ``labbox`` would log in at that machine's address. The sheet names the host field, not the
    network, and the password is forgotten again."""
    connection_add.write_ssh_alias({"id": "labbox", "transport": "ssh", "host": "198.51.100.66", "key": "~/k"})

    out = await rpc.machines_add(BY_PASSWORD | {"host": "labbox"})

    _check("machines.add", out)
    assert (out["ok"], out["reason"]) == (False, "invalid")
    assert "ssh alias Raven keeps" in out["detail"] and "hunter2" not in out["detail"]
    assert not connection_add.password_path("box").exists()


@pytest.mark.parametrize("cancelled", [False, True])
@pytest.mark.parametrize("failed_index", [0, 1])
async def test_a_failed_registration_keeps_the_same_password_of_another_registration_still_probing(
    supported_host, pool, rhome, monkeypatch, cancelled, failed_index
):
    from raven.ops.transport import ssh_env

    entered = [threading.Event(), threading.Event()]
    release = [threading.Event(), threading.Event()]

    def probe(row, **kw):
        index = int(row["display_name"])
        assert connection_add.password_path("box").read_text(encoding="utf-8") == BY_PASSWORD["password"] + "\n"
        entered[index].set()
        assert release[index].wait(5), "the registration's probe was not released"
        return (False, "no answer within 30s", {}) if index == failed_index else (True, "reached", {})

    monkeypatch.setattr(connection_add, "probe", probe)
    first = asyncio.create_task(rpc.machines_add({**BY_PASSWORD, "name": "0"}))
    tasks = [first]
    try:
        assert await asyncio.to_thread(entered[0].wait, 5)
        second = asyncio.create_task(rpc.machines_add({**BY_PASSWORD, "name": "1"}))
        tasks.append(second)
        assert await asyncio.to_thread(entered[1].wait, 5)
        if cancelled:
            tasks[failed_index].cancel()
            with pytest.raises(asyncio.CancelledError):
                await tasks[failed_index]
        else:
            release[failed_index].set()
            assert (await tasks[failed_index])["reason"] == "timeout"
        release[1 - failed_index].set()
        out = await tasks[1 - failed_index]
    finally:
        for event in release:
            event.set()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert out["ok"]
    (row,) = _rows()
    assert connection_add.password_path("box").is_file(), "the successful registration must keep its password"
    assert ssh_env(row), "the registered machine must still be able to start its next connection"


async def test_add_by_password_refuses_a_password_with_a_line_break_before_the_probe(
    supported_host, pool, rhome, monkeypatch
):
    monkeypatch.setattr(connection_add, "probe", lambda row, **kw: pytest.fail("the probe is not reached"))

    out = await rpc.machines_add({**BY_PASSWORD, "password": "abcde\n"})

    _check("machines.add", out)
    assert (out["ok"], out["reason"]) == (False, "invalid")
    assert "cannot contain a line break" in out["detail"] and "abcde" not in out["detail"]
    assert not (rhome / "keys").exists()


async def test_a_password_the_machine_refuses_is_forgotten_here_and_named_as_a_login_refusal(
    supported_host, pool, rhome, monkeypatch
):
    monkeypatch.setattr(
        connection_add,
        "probe",
        lambda row, **kw: (
            False,
            # ssh's own wording names the login twice; the answer names the machine once.
            "could not reach alice@203.0.113.7:2222: alice@203.0.113.7: Permission denied (password).\nThe password "
            "was refused, or this machine does not accept password logins (PasswordAuthentication is off in its sshd).",
            {},
        ),
    )

    out = await rpc.machines_add({**BY_PASSWORD, "password": "wrong-one"})

    _check("machines.add", out)
    assert (out["ok"], out["reason"]) == (False, "login")
    assert out["detail"].startswith("could not reach the machine: Permission denied")
    assert "does not accept password logins" in out["detail"]
    assert "wrong-one" not in out["detail"] and "203.0.113.7" not in out["detail"] and "alice@" not in out["detail"]
    assert not connection_add.password_path("box").exists(), "a password the machine refused is not kept"
    assert not connections.store_path().exists()


async def test_add_by_password_without_one_is_refused_before_anything_is_kept_or_reached(
    supported_host, pool, rhome, monkeypatch
):
    monkeypatch.setattr(connection_add, "probe", lambda row, **kw: pytest.fail("reached the machine"))

    out = await rpc.machines_add({**BY_PASSWORD, "password": ""})

    assert (out["ok"], out["reason"]) == (False, "invalid") and "needs the password" in out["detail"]
    assert not (rhome / "keys").exists() and not connections.store_path().exists()


async def test_add_by_key_still_needs_the_key_path(pool, monkeypatch):
    monkeypatch.setattr(connection_add, "probe", lambda row, **kw: pytest.fail("reached the machine"))

    out = await rpc.machines_add({k: v for k, v in FORM.items() if k != "key"})

    assert (out["ok"], out["reason"]) == (False, "invalid") and "'key' is missing" in out["detail"]


async def test_add_by_password_on_windows_is_refused_before_anything_is_kept(pool, _home, monkeypatch):
    """The Windows review of #1005, kept: the refusal comes first of all, and nothing is left behind."""
    monkeypatch.setenv("RAVEN_HOME", str(_home / "rhome"))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(connection_add, "probe", lambda row, **kw: pytest.fail("reached the machine"))

    out = await rpc.machines_add(BY_PASSWORD)

    _check("machines.add", out)
    assert (out["ok"], out["reason"]) == (False, "platform")
    assert out["detail"] == "registering with a password is not supported on Windows; use a key instead"
    assert not (_home / "rhome" / "keys").exists() and not connections.store_path().exists()


async def test_list_rows_say_how_they_log_in(pool, _home):
    _store(BOX, PASSWORD_BOX | {"id": "pw"}, HERE)

    rows = {r["id"]: r for r in (await rpc.machines_list({}))["rows"]}

    assert rows["box"]["auth"] == "key" and rows["pw"]["auth"] == "password" and "auth" not in rows["here"]
    _check("machines.list", {"rows": list(rows.values())})
