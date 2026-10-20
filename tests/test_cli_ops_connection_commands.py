"""`raven ops connection` -- the front door that replaced hand-editing the file.

What these pin is the one guarantee the command adds over an editor: nothing is
written until the machine itself has answered. The registry is the input every
on-call gate reads and the only one nothing was checking, so a row that cannot
be reached must not reach it -- an absent machine is a question the loop knows to
ask, an unreachable one is a fact it acts on.

`--non-interactive` is covered as its own path rather than as a convenience. A
TUI cannot prompt (`cli.dispatch` runs a command in-process and hands back
stdout), so that flag is the only form the main agent can drive, and the probe
has to still have the last word in it.
"""

from __future__ import annotations

import json
import os
import sys

import pytest
from typer.testing import CliRunner

from raven.cli.ops_connection_commands import connection_app
from raven.ops import connections
from tests._home import point_home

pytestmark = pytest.mark.usefixtures("no_real_ssh_g")

FULL = [
    "--non-interactive",
    "--software",
    "OpenFOAM (/opt/openfoam)",
    "--budget-unit",
    "core-minute",
    "--concurrency",
    "1",
]
SSH = ["--host", "14.103.100.27", "--port", "58717", "--user", "root", "--key", "~/.ssh/id_rsa"]


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "connections.json"
    monkeypatch.setattr(connections, "store_path", lambda: path)
    return path


@pytest.fixture
def reached(monkeypatch):
    """A machine that answers, and says what it is."""
    monkeypatch.setattr(
        "raven.cli.ops_connection_commands.probe", lambda row, **kw: (True, "reached it", {"cores": 128, "kind": "gpu"})
    )


@pytest.fixture
def unreachable(monkeypatch):
    monkeypatch.setattr(
        "raven.cli.ops_connection_commands.probe",
        lambda row, **kw: (False, "could not reach it: Connection refused", {}),
    )


def run(*args):
    return CliRunner().invoke(connection_app, list(args))


def test_a_machine_that_cannot_be_reached_is_not_written(store, unreachable):
    """The whole point of the command. A guessed port that lands in the registry
    stops being a guess -- every later turn reads it as fact."""
    out = run("add", "--id", "gpu", "--name", "GPU", *SSH, *FULL)

    assert out.exit_code == 1
    assert "could not reach" in out.output
    assert not store.exists(), "a machine that never answered must leave no trace"


def test_what_the_machine_said_about_itself_is_written_down(store, reached):
    """Typing the core count twice is how the two copies drift."""
    out = run("add", "--id", "gpu", "--name", "GPU", *SSH, *FULL)

    assert out.exit_code == 0
    row = json.loads(store.read_text())["connections"][0]
    assert row["cores"] == 128 and row["kind"] == "gpu"
    assert row["id"] == "gpu" and row["port"] == 58717


def test_the_flags_are_a_complete_path_because_a_tui_cannot_prompt(store, reached):
    """`cli.dispatch` returns stdout and has no channel for a question, so this
    form is the only one the main agent can drive."""
    out = run("add", "--id", "box", "--name", "the box", "--transport", "local", "--path", "/srv/arena/cases", *FULL)

    assert out.exit_code == 0
    row = json.loads(store.read_text())["connections"][0]
    assert row["paths"] == ["/srv/arena/cases"]


def test_an_ssh_row_with_no_key_is_refused_even_without_a_probe(store, reached):
    out = run(
        "add", "--id", "gpu", "--name", "GPU", "--host", "h", "--port", "22", "--user", "root", "--skip-probe", *FULL
    )

    assert out.exit_code == 1
    assert "'key' is missing" in out.output
    assert not store.exists()


def test_an_id_that_is_taken_is_refused(store, reached):
    run("add", "--id", "gpu", "--name", "GPU", *SSH, *FULL)

    out = run("add", "--id", "gpu", "--name", "another", *SSH, *FULL)

    assert out.exit_code == 1
    assert "already listed" in out.output
    assert len(json.loads(store.read_text())["connections"]) == 1


def test_adding_to_a_file_that_cannot_be_read_would_lose_it(store, reached):
    """A read-modify-write over an unparseable file replaces the owner's machines
    with the one being added."""
    broken = '{"connections": [{"id": "gpu",}]}'
    store.write_text(broken, encoding="utf-8")

    out = run("add", "--id", "cpu", "--name", "CPU", "--transport", "local", *FULL)

    assert out.exit_code == 1
    assert "Nothing was written" in out.output
    assert store.read_text() == broken, "the owner's file is left exactly as it was"


def test_doctor_exits_non_zero_when_there_is_nothing_to_run_on(store):
    store.write_text(json.dumps({"connections": []}), encoding="utf-8")

    assert run("doctor").exit_code == 1


def test_doctor_passes_a_registry_that_predates_a_field(store):
    """The rows on this machine have no `budget_unit`. They work, and a check that
    condemns them is a check the owner turns off."""
    store.write_text(
        json.dumps(
            {
                "connections": [
                    {"id": "gpu", "display_name": "GPU", "host": "h", "port": 22, "user": "root", "key": "k"},
                ]
            }
        ),
        encoding="utf-8",
    )

    out = run("doctor")

    assert out.exit_code == 0
    assert "budget_unit" in out.output, "reported, but not refused"


def test_doctor_json_says_what_a_caller_has_to_branch_on(store):
    """An agent picking a machine reads this: a row it cannot run on has to be
    distinguishable from one it can, without parsing prose."""
    store.write_text(
        json.dumps(
            {
                "connections": [
                    {"id": "ok", "display_name": "OK", "transport": "local"},
                    {"id": "broken", "display_name": "B", "host": "h", "port": 22, "user": "root"},
                ]
            }
        ),
        encoding="utf-8",
    )

    out = run("doctor", "--json")
    payload = json.loads(out.output)

    assert payload["usable_ids"] == ["ok"]
    assert payload["state"] == "ok" and payload["listed"] == 2
    assert any("key" in line for line in payload["blocking"])


def test_doctor_exits_nonzero_when_every_row_shares_one_id(monkeypatch, tmp_path):
    import json

    from typer.testing import CliRunner

    from raven.cli.ops_connection_commands import connection_app

    row = {
        "id": "cpu-box",
        "display_name": "CPU box",
        "transport": "ssh",
        "host": "10.0.0.1",
        "port": 22,
        "user": "root",
        "key": "~/.ssh/id_rsa",
        "software": "ccx",
        "budget_unit": "minute",
        "concurrency": 1,
    }
    path = tmp_path / "connections.json"
    path.write_text(json.dumps({"connections": [row, {**row, "host": "10.0.0.2"}]}), encoding="utf-8")
    monkeypatch.setenv("RAVEN_CONNECTIONS", str(path))

    result = CliRunner().invoke(connection_app, ["doctor"])

    assert result.exit_code == 1, "an ambiguous registry must not read as healthy"


def test_add_refuses_rather_than_erasing_entries_the_reader_filtered(monkeypatch, tmp_path):
    """`read` drops entries without an id and reports them; rewriting from its
    rows would serialize the survivors and silently delete the owner's
    hand-written ones (reproduced in review)."""
    path = tmp_path / "connections.json"
    path.write_text(json.dumps({"connections": [{"display_name": "typo, no id"}]}), encoding="utf-8")
    monkeypatch.setenv("RAVEN_CONNECTIONS", str(path))

    from raven.cli.ops_connection_commands import _write

    with pytest.raises(ValueError, match="had no id"):
        _write({"id": "cpu", "display_name": "CPU box", "transport": "local"})

    assert json.loads(path.read_text())["connections"] == [{"display_name": "typo, no id"}]


def test_the_probe_counts_the_cards_and_writes_a_device_line_admission_can_read():
    """The real two-card box probed to a "+"-joined device list and no count, so
    admission read the row as a 128-core machine (2026-09-07). The count is what
    `gpus` is for, and matching cards are spelled "N x <card>", the other form the
    readers infer a count from."""
    from raven.cli.ops_connection_commands import _parse_probe
    from raven.ops import connections

    out = "CORES=128\nMEM=463\nGPU=NVIDIA A800-SXM4-80GB, 81920 MiB|NVIDIA A800-SXM4-80GB, 81920 MiB\n"
    row = _parse_probe(out)

    assert row["kind"] == "gpu" and row["gpus"] == 2
    assert row["device"] == "2 x NVIDIA A800-SXM4-80GB, 81920 MiB"
    assert connections.resource_unit(row) == "gpus" and connections.capacity(row)["gpus"] == 2

    mixed = _parse_probe("CORES=64\nGPU=NVIDIA A100|NVIDIA H100\n")
    assert mixed["gpus"] == 2 and mixed["device"] == "NVIDIA A100 + NVIDIA H100"
    assert connections.resource_unit(mixed) == "gpus", "an explicit count carries a mixed box too"


# ---- the ssh alias beside the registry ----


@pytest.fixture
def home(tmp_path, monkeypatch):
    return point_home(monkeypatch, tmp_path / "fakehome")


def test_adding_an_ssh_machine_writes_a_host_alias(store, reached, home):
    """Transfers run their client here and only address the machine, so they
    cannot ride the exec machine channel; the alias is how ``rsync ... <id>:``
    resolves without the raw address ever entering the model's context."""
    out = run("add", "--id", "conn_gpu", "--name", "GPU box", *SSH, *FULL)

    assert out.exit_code == 0, out.output
    cfg = (home / ".ssh" / "config").read_text()
    assert "Host conn_gpu" in cfg
    assert "HostName 14.103.100.27" in cfg
    assert "Port 58717" in cfg
    assert "IdentityFile ~/.ssh/id_rsa" in cfg
    assert (home / ".ssh" / "config").stat().st_mode & 0o777 == 0o600
    assert "ssh alias 'conn_gpu'" in out.output


def test_the_alias_block_is_replaced_not_duplicated(store, reached, home):
    from raven.cli.ops_connection_commands import _write_ssh_alias

    row = {"id": "conn_gpu", "host": "14.103.100.27", "port": 58717, "user": "root", "key": "~/.ssh/id_rsa"}
    _write_ssh_alias(row)
    _write_ssh_alias({**row, "port": 7104})

    cfg = (home / ".ssh" / "config").read_text()
    assert cfg.count("Host conn_gpu\n") == 1, "one managed block per id"
    assert "Port 7104" in cfg and "Port 58717" not in cfg


def test_the_owners_own_config_lines_survive(store, reached, home):
    from raven.cli.ops_connection_commands import _write_ssh_alias

    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True)
    (ssh_dir / "config").write_text("Host mybox\n  HostName 10.0.0.5\n")

    _write_ssh_alias({"id": "conn_gpu", "host": "14.103.100.27", "port": 58717})

    cfg = (ssh_dir / "config").read_text()
    assert "Host mybox" in cfg and "HostName 10.0.0.5" in cfg
    assert "Host conn_gpu" in cfg


def test_a_local_machine_gets_no_alias(store, reached, home):
    out = run("add", "--id", "here", "--name", "this box", "--transport", "local", *FULL)

    assert out.exit_code == 0, out.output
    assert not (home / ".ssh" / "config").exists()


# --- install-node -------------------------------------------------------------------
#
# The install itself is raven.node.install's (tests/test_node_install.py); what the
# command adds is the order of things: look, refuse or ask, install, record where,
# and only then say the node answers.


class _Node:
    """raven.node.install and the node's answer, stood in for, with what was asked of them."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, registered: str = "") -> None:
        from raven.acp_client.remote import Machine
        from raven.node import install as node_install
        from raven.node.client import NodeError, NodePool

        self.present = False
        self.refusal: str | None = None
        self.plan_error: str | None = None
        self.install_error: str | None = None
        self.prune_error: str | None = None
        self.hello_error: str | None = None
        self.rg = True
        self.pruned: tuple[str, ...] = ()
        self.asked: list[tuple[str, object]] = []

        def plan(conn_id: str, *, node_dir: str | None = None) -> node_install.Plan:
            self.asked.append(("plan", node_dir))
            if self.plan_error:
                raise node_install.InstallError(self.plan_error)
            row = {"id": conn_id, "node_dir": registered} if registered else {"id": conn_id}
            return node_install.Plan(
                target=Machine(id=conn_id, display_name="Lab box", _row=row),
                node_dir=node_dir or registered or "~/.raven-node",
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
            return node_install.Installed(p.node_dir, p.install_name, 52 * 1024, rg=self.rg)

        def prune(p: node_install.Plan) -> tuple[str, ...]:
            self.asked.append(("prune", p.node_dir))
            if self.prune_error:
                raise node_install.InstallError(self.prune_error)
            return self.pruned

        async def hello(pool: NodePool, machine_id: str) -> dict:
            self.asked.append(("hello", machine_id))
            if self.hello_error:
                raise NodeError(self.hello_error)
            return {"python": "3.12.3", "digest": "abcdef012345"}

        monkeypatch.setattr(node_install, "plan", plan)
        monkeypatch.setattr(node_install, "refusal", lambda p: self.refusal)
        monkeypatch.setattr(node_install, "install", install)
        monkeypatch.setattr(node_install, "prune", prune)
        monkeypatch.setattr(NodePool, "hello", hello)

    def steps(self) -> list[str]:
        return [step for step, _ in self.asked]


@pytest.fixture
def node(monkeypatch):
    return _Node(monkeypatch)


def install_node(*args, answer: str | None = None):
    return CliRunner().invoke(connection_app, ["install-node", *args], input=answer)


def said(result) -> str:
    """The command's output with rich's line wrapping undone."""
    return " ".join(result.output.split())


def test_install_node_asks_before_installing_and_then_checks_the_node_answers(node):
    out = install_node("box", answer="y\n")

    assert out.exit_code == 0, out.output
    assert "Install it?" in out.output
    assert node.steps() == ["plan", "install", "hello"]
    assert "Installed the Raven node 0.2.4-abcdef012345 in ~/.raven-node (52 MB)." in said(out)
    assert "The node answers: Python 3.12.3, code abcdef012345." in said(out)


def test_install_node_does_nothing_the_owner_declined(node):
    out = install_node("box", answer="n\n")

    assert out.exit_code == 1
    assert node.steps() == ["plan"]


def test_install_node_yes_installs_without_asking(node):
    out = install_node("box", "--yes")

    assert out.exit_code == 0, out.output
    assert "Install it?" not in out.output
    assert node.steps() == ["plan", "install", "hello"]


def test_a_machine_that_already_has_the_node_is_left_as_it_is(node):
    node.present = True

    out = install_node("box")

    assert out.exit_code == 0, out.output
    assert node.steps() == ["plan", "hello"]
    assert "Machine 'Lab box' (box) already has the Raven node 0.2.4-abcdef012345 in ~/.raven-node." in said(out)


@pytest.mark.parametrize(
    ("knob", "value"),
    [
        ("plan_error", "machine 'box' is not registered; add it with `raven ops connection add`"),
        ("refusal", "machine 'Lab box' (box) has neither a Python 3.12 (with venv) nor uv"),
        ("install_error", "installing the Raven node on machine 'Lab box' (box) failed at the packages step: no"),
        ("hello_error", "the Raven node on machine 'Lab box' (box) stopped"),
    ],
)
def test_install_node_stops_at_the_first_thing_that_fails_and_says_it(node, knob, value):
    setattr(node, knob, value)

    out = install_node("box", "--yes")

    assert out.exit_code == 1
    assert value in said(out)
    assert "The node answers" not in out.output


def test_a_node_without_rg_says_how_search_runs_there(node):
    node.rg = False

    out = install_node("box", "--yes")

    assert out.exit_code == 0, out.output
    assert "grep there runs Raven's own Python search" in said(out)


def test_install_node_prunes_only_when_asked(node):
    node.pruned = ("0.2.3-aaaaaaaaaaaa",)

    plain = install_node("box", "--yes")
    pruned = install_node("box", "--yes", "--prune")

    assert "prune" not in [step for step, _ in node.asked[:3]]
    assert "Removed 1 other node(s) from ~/.raven-node: 0.2.3-aaaaaaaaaaaa." in said(pruned)
    assert plain.exit_code == pruned.exit_code == 0


def test_a_prune_with_nothing_to_remove_says_none(node):
    node.present = True

    out = install_node("box", "--prune")

    assert "Removed 0 other node(s) from ~/.raven-node: none." in said(out)
    assert node.steps() == ["plan", "prune", "hello"]


def test_a_prune_that_fails_stops_the_command(node):
    node.prune_error = "installing the Raven node on machine 'Lab box' (box) failed at the prune step: busy"

    out = install_node("box", "--yes", "--prune")

    assert out.exit_code == 1
    assert "failed at the prune step: busy" in said(out)


def _box_row(store, **extra):
    store.write_text(
        json.dumps({"connections": [{"id": "box", "host": "203.0.113.7", "port": 22, "key": "/k", **extra}]}),
        encoding="utf-8",
    )


def test_a_directory_chosen_with_dir_is_kept_in_the_registry(node, store):
    _box_row(store, note="kept")

    out = install_node("box", "--yes", "--dir", "/data/raven-node/")

    assert out.exit_code == 0, out.output
    assert node.asked[0] == ("plan", "/data/raven-node")
    (row,) = json.loads(store.read_text(encoding="utf-8"))["connections"]
    assert row["node_dir"] == "/data/raven-node" and row["note"] == "kept"
    assert "Recorded /data/raven-node as where machine 'Lab box' (box) keeps its Raven nodes." in said(out)


def test_the_directory_already_on_record_is_not_written_again(monkeypatch, store):
    node = _Node(monkeypatch, registered="/data/raven-node")
    _box_row(store, node_dir="/data/raven-node")

    out = install_node("box", "--yes", "--dir", "/data/raven-node")

    assert out.exit_code == 0, out.output
    assert node.steps() == ["plan", "install", "hello"]
    assert "Recorded" not in out.output
    row = json.loads(store.read_text(encoding="utf-8"))["connections"][0]
    assert row["node_dir"] == "/data/raven-node"
    # The one thing the install adds to the row: which node is there now, so
    # the Agent Hub lists the machine with its node before any link comes up.
    assert row["node_install"] == "0.2.4-abcdef012345"


def test_the_node_put_there_is_noted_on_the_row_and_a_row_it_cannot_go_on_is_a_warning(node, store):
    """The listing reads the node off the row; a row that cannot take it costs that, not the install."""
    _box_row(store)
    out = install_node("box", "--yes")
    assert out.exit_code == 0, out.output
    assert json.loads(store.read_text(encoding="utf-8"))["connections"][0]["node_install"] == "0.2.4-abcdef012345"

    store.write_text(json.dumps({"connections": [{"id": "box", "host": "h"}, {"id": "box", "host": "h2"}]}))
    out = install_node("box", "--yes")
    assert out.exit_code == 0, out.output
    assert "could not be noted on the machine's row" in said(out) and "listed 2 times" in said(out)
    assert node.steps()[-3:] == ["plan", "install", "hello"]


def test_a_directory_that_cannot_be_recorded_is_said_after_the_install(node, store):
    store.write_text(json.dumps({"connections": [{"id": "box", "host": "h"}, {"id": "box", "host": "h2"}]}))

    out = install_node("box", "--yes", "--dir", "/data/raven-node")

    assert out.exit_code == 1
    assert "The node is installed, but /data/raven-node could not be recorded" in said(out)
    assert "listed 2 times" in said(out)
    assert node.steps() == ["plan", "install"]


@pytest.mark.slow
def test_install_node_closes_the_live_node_it_asked(monkeypatch, tmp_path):
    # The handshake at the end runs through the real pool, launch and teardown,
    # over a stand-in ssh; only the install itself is stood in for. A teardown
    # that raised here would leave the node running and skip the CLI's own
    # report of the failure.
    from pathlib import Path

    from raven.node import bundle
    from raven.node import install as node_install
    from tests.test_node_client import Machine

    if not Path("/bin/sh").exists():
        pytest.skip("the stand-in ssh hands the launch line to a POSIX shell")
    box = Machine(tmp_path, monkeypatch)
    box.node()
    present = node_install.plan.__wrapped__ if hasattr(node_install.plan, "__wrapped__") else None
    del present

    def plan(conn_id, *, node_dir=None):
        from raven.acp_client.remote import machine

        target = machine(conn_id)
        return node_install.Plan(
            target=target,
            node_dir="~/.raven-node",
            install_name=bundle.install_name(),
            present=True,
            python="/usr/bin/python3",
            uv="",
            free_kb=None,
        )

    monkeypatch.setattr(node_install, "plan", plan)

    out = install_node("box")

    assert out.exit_code == 0, out.output
    assert f"The node answers: Python {__import__('platform').python_version()}, code {bundle.digest()}." in said(out)
    assert len(box.launches()) == 1


# --- remove ---------------------------------------------------------------------------
#
# The write itself is raven.ops.connection_add.remove's (tests/test_ops_connection_add.py);
# what the command adds is the refusal over agent entries that still name the
# machine, the question, and saying what stays behind on the machine.


def _named(monkeypatch, *names: str, off: tuple[str, ...] = ()) -> None:
    """The entries naming the machine: ``names`` connected, ``off`` disconnected."""
    monkeypatch.setattr(
        "raven.config.update_subagents.agents_on_machine",
        lambda conn_id, only_enabled=False: list(names) if only_enabled else [*names, *off],
    )


def remove(*args, answer: str | None = None):
    return CliRunner().invoke(connection_app, ["remove", *args], input=answer)


def test_remove_asks_then_takes_the_row_and_its_alias_out_and_says_the_node_stays(store, monkeypatch, tmp_path):
    point_home(monkeypatch, tmp_path)
    _named(monkeypatch)
    _box_row(store, display_name="Lab box")
    from raven.ops.connection_add import write_ssh_alias

    write_ssh_alias(json.loads(store.read_text(encoding="utf-8"))["connections"][0])

    declined = remove("box", answer="n\n")
    assert declined.exit_code == 1 and "Remove machine 'Lab box' (box)" in said(declined)
    assert len(json.loads(store.read_text(encoding="utf-8"))["connections"]) == 1

    out = remove("box", answer="y\n")

    assert out.exit_code == 0, out.output
    assert "removed box from" in said(out) and "ssh alias 'box' removed" in said(out)
    assert "stays there: remove its node directory on the machine by hand" in said(out)
    assert json.loads(store.read_text(encoding="utf-8"))["connections"] == []
    assert "Host box" not in (tmp_path / ".ssh" / "config").read_text(encoding="utf-8")


def test_remove_forgets_the_password_kept_for_a_password_row(store, monkeypatch, tmp_path):
    point_home(monkeypatch, tmp_path)
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    _named(monkeypatch)
    from raven.ops.connection_add import password_path, store_password

    store_password("box", "hunter2")
    _box_row(store, auth="password")

    out = remove("box", answer="y\n")

    assert out.exit_code == 0, out.output
    assert "the password kept here for it is deleted" in said(out) and "hunter2" not in out.output
    assert not password_path("box").exists()
    assert json.loads(store.read_text(encoding="utf-8"))["connections"] == []


def test_remove_of_a_key_row_deletes_a_password_file_left_under_its_id(store, monkeypatch, tmp_path):
    point_home(monkeypatch, tmp_path)
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    from raven.ops.connection_add import password_path, store_password

    _box_row(store)
    store_password("box", "left-behind-1")

    out = CliRunner().invoke(connection_app, ["remove", "box", "--yes"])

    assert out.exit_code == 0, out.output
    assert "the password kept here for it is deleted" in said(out)
    assert not password_path("box").exists()


def test_remove_refuses_while_agent_entries_name_the_machine_unless_forced(store, monkeypatch, tmp_path):
    point_home(monkeypatch, tmp_path)
    _named(monkeypatch, "claude_code@box")
    _box_row(store)

    out = remove("box", "--yes")

    assert out.exit_code == 1
    assert "1 connected agent entry names machine 'box': claude_code@box" in said(out)
    assert "Nothing was changed" in said(out)
    assert len(json.loads(store.read_text(encoding="utf-8"))["connections"]) == 1

    out = remove("box", "--yes", "--force")

    assert out.exit_code == 0, out.output
    assert "claude_code@box still names this machine and fail at dispatch" in said(out)
    assert json.loads(store.read_text(encoding="utf-8"))["connections"] == []


def test_remove_is_not_refused_over_a_disconnected_entry_but_names_it(store, monkeypatch, tmp_path):
    point_home(monkeypatch, tmp_path)
    _named(monkeypatch, off=("codex@box",))
    _box_row(store)

    out = remove("box", "--yes")

    assert out.exit_code == 0, out.output
    assert "The disconnected entry codex@box still names this machine and cannot be connected" in said(out)
    assert json.loads(store.read_text(encoding="utf-8"))["connections"] == []


def test_remove_says_when_the_machine_is_not_registered_or_the_file_cannot_be_read(store, monkeypatch):
    _named(monkeypatch)
    out = remove("ghost", "--yes")
    assert out.exit_code == 1 and "machine 'ghost' is not registered" in said(out)
    store.write_text("{nope", encoding="utf-8")
    out = remove("box", "--yes")
    assert out.exit_code == 1 and "not valid JSON" in said(out)


def test_remove_says_what_it_could_not_do_a_registry_it_cannot_rewrite_an_alias_it_cannot_drop(
    store, monkeypatch, tmp_path
):
    point_home(monkeypatch, tmp_path)
    from pydantic import ValidationError

    def broken(conn_id, **kw):
        raise ValidationError.from_exception_data("SubagentsConfig", [])

    # A malformed agents section names no entry that would fail, so nothing is refused over it.
    monkeypatch.setattr("raven.config.update_subagents.agents_on_machine", broken)
    store.write_text(json.dumps({"connections": [{"id": "box", "host": "h"}, {"host": "no id"}]}), encoding="utf-8")

    out = remove("box", "--yes")

    assert out.exit_code == 1 and "had no id" in said(out) and "Nothing was changed" in said(out)
    assert len(json.loads(store.read_text(encoding="utf-8"))["connections"]) == 2

    _box_row(store)

    def refuse(conn_id):
        raise OSError("read-only file system")

    monkeypatch.setattr("raven.cli.ops_connection_commands._remove_ssh_alias", refuse)
    out = remove("box", "--yes")

    assert out.exit_code == 0, out.output
    assert "ssh alias not removed (read-only file system); take the 'Host box' block out by hand" in said(out)
    assert json.loads(store.read_text(encoding="utf-8"))["connections"] == []

    # A managed block with a mark missing is refused by the alias writer and is a warning here too.
    _box_row(store)

    def incomplete(conn_id):
        raise ValueError("the managed 'Host box' block in ~/.ssh/config is incomplete")

    monkeypatch.setattr("raven.cli.ops_connection_commands._remove_ssh_alias", incomplete)
    out = remove("box", "--yes")
    assert out.exit_code == 0, out.output
    assert "ssh alias not removed (the managed 'Host box' block in ~/.ssh/config is incomplete)" in said(out)


def test_add_reports_an_incomplete_managed_block_as_a_warning_and_still_writes_the_row(store, reached, monkeypatch):
    def refuse(row):
        raise ValueError("the managed 'Host gpu' block in ~/.ssh/config is incomplete")

    monkeypatch.setattr("raven.cli.ops_connection_commands._write_ssh_alias", refuse)

    out = run("add", "--id", "gpu", "--name", "GPU", *SSH, *FULL)

    assert out.exit_code == 0, out.output
    assert "wrote gpu" in said(out) and "ssh alias not written (the managed 'Host gpu' block" in said(out)
    assert json.loads(store.read_text(encoding="utf-8"))["connections"][0]["id"] == "gpu"


# --- add with a password: kept here, used on every connection -----------------------

PW_ADD = ["add", "--id", "box", "--name", "Box", "--host", "14.103.100.27", "--port", "58717", "--user", "root"]


def test_add_with_a_password_from_stdin_keeps_it_here_and_writes_a_row_with_no_key_and_no_alias(
    store, reached, monkeypatch, tmp_path
):
    point_home(monkeypatch, tmp_path)
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    monkeypatch.setattr(sys, "platform", "linux")
    from raven.ops.connection_add import password_path

    out = CliRunner().invoke(
        connection_app, [*PW_ADD, "--auth", "password", "--password-stdin", *FULL], input="hunter2\n"
    )

    assert out.exit_code == 0, out.output
    row = json.loads(store.read_text(encoding="utf-8"))["connections"][0]
    assert row["auth"] == "password" and "key" not in row and "password" not in row
    assert password_path("box").read_text(encoding="utf-8") == "hunter2\n"
    assert "password kept for box" in said(out)
    assert "hunter2" not in out.output and "ssh alias" not in out.output
    assert not (tmp_path / ".ssh" / "config").exists()


def test_a_failed_re_add_under_an_id_in_use_leaves_the_registered_password_alone(
    store, unreachable, monkeypatch, tmp_path
):
    """The id is judged before anything is kept or reached: the registered row's password survives a re-add that fails."""
    point_home(monkeypatch, tmp_path)
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    monkeypatch.setattr(sys, "platform", "linux")
    from raven.ops.connection_add import password_path, store_password

    _box_row(store, auth="password", user="root")
    store_password("box", "working-old")
    before = store.read_text(encoding="utf-8")

    out = CliRunner().invoke(
        connection_app, [*PW_ADD, "--auth", "password", "--password-stdin", *FULL], input="wrong-new\n"
    )

    assert out.exit_code == 1, out.output
    assert "already listed" in said(out) and "remove it first" in said(out)
    assert password_path("box").read_text(encoding="utf-8") == "working-old\n"
    assert store.read_text(encoding="utf-8") == before
    assert "wrong-new" not in out.output


def _nothing_kept(rhome) -> bool:
    """No password file anywhere under the Raven home, keys/ included, and no askpass helper either."""
    return not list(rhome.rglob("*.password")) and not list(rhome.rglob("askpass"))


@pytest.mark.parametrize(
    ("bad_id", "said"),
    [
        ("../escape", "id must be lowercase"),
        ("My Box", "id must be lowercase"),
        ("Box", "id must be lowercase"),
    ],
)
def test_add_by_password_judges_the_id_before_it_keeps_anything(store, reached, monkeypatch, tmp_path, bad_id, said):
    """A path-shaped id used to put the password file outside keys/ (nothing scrubs or removes it there) and an
    id with a space or a capital left an orphan file no `remove` could reach; both are refused with nothing kept."""
    point_home(monkeypatch, tmp_path)
    rhome = tmp_path / "rhome"
    monkeypatch.setenv("RAVEN_HOME", str(rhome))
    monkeypatch.setattr(sys, "platform", "linux")

    out = CliRunner().invoke(
        connection_app,
        [
            "add",
            "--id",
            bad_id,
            "--name",
            "Box",
            "--host",
            "14.103.100.27",
            "--port",
            "58717",
            "--user",
            "root",
            "--auth",
            "password",
            "--password-stdin",
            "--skip-probe",
            *FULL,
        ],
        input="hunter2\n",
    )

    assert out.exit_code == 1, out.output
    assert said in out.output and "Nothing was written" in out.output
    assert _nothing_kept(rhome) and not (rhome / "escape.password").exists()
    assert not store.exists()


def test_add_by_password_on_a_case_insensitive_disk_leaves_the_registered_machines_password_alone(
    store, unreachable, monkeypatch, tmp_path
):
    """`Box` is refused as an id before anything is kept, so on a disk where Box.password and box.password are one
    file the registered machine's password is neither deleted by the refusal nor replaced by the new one."""
    point_home(monkeypatch, tmp_path)
    rhome = tmp_path / "rhome"
    monkeypatch.setenv("RAVEN_HOME", str(rhome))
    monkeypatch.setattr(sys, "platform", "linux")
    from raven.ops.connection_add import password_path, store_password

    _box_row(store, auth="password", user="root")
    store_password("box", "working-old")

    out = CliRunner().invoke(
        connection_app,
        [
            "add",
            "--id",
            "Box",
            "--name",
            "Box",
            "--host",
            "14.103.100.27",
            "--port",
            "58717",
            "--user",
            "root",
            "--auth",
            "password",
            "--password-stdin",
            *FULL,
        ],
        input="wrong-new\n",
    )

    assert out.exit_code == 1, out.output
    assert password_path("box").read_text(encoding="utf-8") == "working-old\n"
    assert "wrong-new" not in out.output


def test_add_by_password_refuses_an_unreadable_registry_before_it_keeps_anything(store, reached, monkeypatch, tmp_path):
    """`load()` reads an unreadable file as empty, so the duplicate check alone would let the password be kept for a
    registration the write then refuses; the registry has to read first, as `machines.add` demands."""
    point_home(monkeypatch, tmp_path)
    rhome = tmp_path / "rhome"
    monkeypatch.setenv("RAVEN_HOME", str(rhome))
    monkeypatch.setattr(sys, "platform", "linux")
    broken = '{"connections": [{"id": "gpu",}]}'
    store.write_text(broken, encoding="utf-8")

    out = CliRunner().invoke(
        connection_app, [*PW_ADD, "--auth", "password", "--password-stdin", *FULL], input="hunter2\n"
    )

    assert out.exit_code == 1, out.output
    said = " ".join(out.output.split())  # the console wraps a long path mid-phrase
    assert "Nothing was written" in said and "not valid JSON" in said
    assert _nothing_kept(rhome) and store.read_text(encoding="utf-8") == broken


def test_a_write_refused_because_the_id_landed_meanwhile_leaves_the_password_file_to_that_row(
    store, reached, monkeypatch, tmp_path
):
    """Between the duplicate check and the write another registration of the same id can land, with its own
    password under the same file name: the file is that row's now, and the refused registration leaves it."""
    point_home(monkeypatch, tmp_path)
    rhome = tmp_path / "rhome"
    monkeypatch.setenv("RAVEN_HOME", str(rhome))
    monkeypatch.setattr(sys, "platform", "linux")
    import raven.cli.ops_connection_commands as cli
    from raven.ops.connection_add import password_path, store_password

    def refuse(row, **kwargs):
        store_password(row["id"], "rpc-pw-2222")  # the competitor's, kept under the same id
        raise ValueError(f"a machine with id {row['id']!r} is already listed in {store}")

    monkeypatch.setattr(cli, "_write", refuse)

    out = CliRunner().invoke(
        connection_app, [*PW_ADD, "--auth", "password", "--password-stdin", *FULL], input="hunter2\n"
    )

    assert out.exit_code == 1, out.output
    assert "already listed" in out.output and "Nothing was written" in out.output
    assert password_path("box").read_text(encoding="utf-8") == "rpc-pw-2222\n"


def test_an_add_aborted_after_another_registration_of_the_id_landed_leaves_that_rows_password(
    store, reached, monkeypatch, tmp_path
):
    """An interactive add parked at a later question, `machines.add` registered the same id with its own
    password meanwhile, the add was aborted -- and its cleanup deleted the landed row's password."""
    point_home(monkeypatch, tmp_path)
    rhome = tmp_path / "rhome"
    monkeypatch.setenv("RAVEN_HOME", str(rhome))
    monkeypatch.setattr(sys, "platform", "linux")
    import typer

    import raven.cli.ops_connection_commands as cli
    from raven.ops.connection_add import password_path, store_password

    answers = {
        "What do you call this machine": "Box",
        "Reached over ssh, or is it this computer? (ssh/local)": "ssh",
        "Short id for it": "box",
        "Address": "14.103.100.27",
        "Port": "58717",
        "Username": "root",
        "Log in with a key or a password? (key/password)": "password",
    }

    def ask(label, default="", *, required=True):
        if label.startswith("What is installed on it"):
            # The other registration lands while this one is parked here: its row, and its password.
            _box_row(store)
            store_password("box", "rpc-pw-2222")
            raise typer.Abort()
        return answers[label]

    monkeypatch.setattr(cli, "_ask", ask)
    monkeypatch.setattr(cli.typer, "prompt", lambda label, **kw: "cli-pw-1111")

    out = CliRunner().invoke(connection_app, ["add"])

    assert out.exit_code == 1 and "Aborted" in out.output
    assert password_path("box").read_text(encoding="utf-8") == "rpc-pw-2222\n", "the landed row's password stays"


def test_an_aborted_add_keeps_the_same_password_of_a_registration_that_has_not_written_its_row(
    store, reached, monkeypatch, tmp_path, supported_host
):
    import typer

    import raven.cli.ops_connection_commands as cli
    from raven.ops import connection_add
    from raven.ops.transport import ssh_env

    point_home(monkeypatch, tmp_path)
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    password = "same-password-1"

    def reach(row, **kwargs):
        assert kwargs["password"] == password
        return True, "reached", {}

    monkeypatch.setattr(cli, "probe", reach)

    def ask(label, default="", *, required=True):
        assert label.startswith("What is installed on it")
        connection_add.store_password("box", password)
        assert not store.exists(), "the other registration is still probing"
        raise typer.Abort()

    monkeypatch.setattr(cli, "_ask", ask)
    out = CliRunner().invoke(
        connection_app,
        ["add", "--id", "box", "--name", "Box", "--transport", "ssh", *SSH, "--password-stdin"],
        input=password + "\n",
    )

    assert out.exit_code == 1 and "Aborted" in out.output
    row = {"id": "box", "host": "192.0.2.10", "port": 22, "user": "alice", "auth": "password"}
    connection_add.write(row)
    assert connection_add.password_path("box").is_file(), "the pending registration still owns the password"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "ssh").write_text('#!/bin/sh\n[ "$1" = -G ] && echo "hostname 192.0.2.10"\n', encoding="utf-8")
    (bin_dir / "ssh").chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    assert ssh_env(row)


def test_an_add_aborted_at_a_later_question_leaves_no_password_behind(store, reached, monkeypatch, tmp_path):
    """Interactive: the password is kept before the probe, and the owner stops at a question after it (Ctrl-C is an
    Abort); the file does not outlive the registration that never happened."""
    point_home(monkeypatch, tmp_path)
    rhome = tmp_path / "rhome"
    monkeypatch.setenv("RAVEN_HOME", str(rhome))
    monkeypatch.setattr(sys, "platform", "linux")
    import typer

    import raven.cli.ops_connection_commands as cli

    answers = {
        "What do you call this machine": "Box",
        "Reached over ssh, or is it this computer? (ssh/local)": "ssh",
        "Short id for it": "box",
        "Address": "14.103.100.27",
        "Port": "58717",
        "Username": "root",
        "Log in with a key or a password? (key/password)": "password",
    }

    def ask(label, default="", *, required=True):
        if label.startswith("What is installed on it"):
            raise typer.Abort()
        return answers[label]

    monkeypatch.setattr(cli, "_ask", ask)
    monkeypatch.setattr(cli.typer, "prompt", lambda label, **kw: "hunter2")

    out = CliRunner().invoke(connection_app, ["add"])

    assert out.exit_code == 1, out.output
    assert "Aborted" in out.output
    assert _nothing_kept(rhome) and not store.exists()


def test_add_by_password_refuses_a_password_shorter_than_the_scrubbers_floor(store, reached, monkeypatch, tmp_path):
    point_home(monkeypatch, tmp_path)
    rhome = tmp_path / "rhome"
    monkeypatch.setenv("RAVEN_HOME", str(rhome))
    monkeypatch.setattr(sys, "platform", "linux")

    out = CliRunner().invoke(connection_app, [*PW_ADD, "--auth", "password", "--password-stdin", *FULL], input="root\n")

    assert out.exit_code == 1, out.output
    assert "shorter than 6 characters cannot be kept" in said(out) and "Nothing was written" in out.output
    assert _nothing_kept(rhome) and not store.exists()


def test_password_stdin_is_read_before_the_first_question_without_non_interactive(
    store, reached, monkeypatch, tmp_path
):
    """Piped in, the password must not be left to answer a question of its own: without --non-interactive the first
    question used to eat it and the password became the machine's name (reviewed 2026-10-19)."""
    point_home(monkeypatch, tmp_path)
    rhome = tmp_path / "rhome"
    monkeypatch.setenv("RAVEN_HOME", str(rhome))
    monkeypatch.setattr(sys, "platform", "linux")
    asked: list[str] = []

    def ask(label, default="", *, required=True):
        asked.append(label)
        return {"What do you call this machine": "Box"}.get(label, default or "x")

    import raven.cli.ops_connection_commands as cli

    monkeypatch.setattr(cli, "_ask", ask)
    monkeypatch.setattr(cli.typer, "prompt", lambda *a, **kw: pytest.fail("the password came from the prompt"))

    out = CliRunner().invoke(
        connection_app,
        [
            "add",
            "--id",
            "box",
            "--host",
            "14.103.100.27",
            "--port",
            "58717",
            "--user",
            "root",
            "--auth",
            "password",
            "--password-stdin",
            "--software",
            "OpenFOAM (/opt/openfoam)",
            "--budget-unit",
            "core-minute",
            "--concurrency",
            "1",
        ],
        input="hunter2\n",
    )

    assert out.exit_code == 0, out.output
    assert asked[0] == "What do you call this machine", "the password was not left to answer it"
    row = json.loads(store.read_text(encoding="utf-8"))["connections"][0]
    assert row["display_name"] == "Box", "the password was not left to answer the name"
    from raven.ops.connection_add import password_path

    assert password_path("box").read_text(encoding="utf-8") == "hunter2\n"


def test_add_by_password_needs_the_password_and_a_refused_one_is_not_kept(store, unreachable, monkeypatch, tmp_path):
    point_home(monkeypatch, tmp_path)
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    monkeypatch.setattr(sys, "platform", "linux")
    from raven.ops.connection_add import password_path

    out = CliRunner().invoke(connection_app, [*PW_ADD, "--auth", "password", *FULL])
    assert out.exit_code == 1 and "needs the password" in said(out) and not store.exists()

    out = CliRunner().invoke(
        connection_app, [*PW_ADD, "--auth", "password", "--password-stdin", *FULL], input="hunter2\n"
    )
    assert out.exit_code == 1 and "could not reach" in said(out)
    assert not password_path("box").exists() and not store.exists()


def test_add_by_password_is_refused_on_windows_before_anything_is_kept(store, monkeypatch, tmp_path):
    point_home(monkeypatch, tmp_path)
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("raven.cli.ops_connection_commands.probe", lambda row, **kw: pytest.fail("reached the machine"))

    out = CliRunner().invoke(
        connection_app, [*PW_ADD, "--auth", "password", "--password-stdin", *FULL], input="hunter2\n"
    )

    assert out.exit_code == 1 and "not supported on Windows" in said(out)
    assert not (tmp_path / "rhome" / "keys").exists() and not store.exists()


def test_add_asks_for_the_password_when_the_login_pick_says_so(store, reached, monkeypatch, tmp_path):
    point_home(monkeypatch, tmp_path)
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    monkeypatch.setattr(sys, "platform", "linux")
    from raven.ops.connection_add import password_path

    answers = (
        "\n".join(
            ["Box", "ssh", "box", "14.103.100.27", "58717", "root", "password", "hunter2", "OpenFOAM", "", "", ""]
        )
        + "\n"
    )
    out = CliRunner().invoke(connection_app, ["add"], input=answers)

    assert out.exit_code == 0, out.output
    row = json.loads(store.read_text(encoding="utf-8"))["connections"][0]
    assert row["auth"] == "password" and "key" not in row
    assert password_path("box").read_text(encoding="utf-8") == "hunter2\n"
    assert "hunter2" not in out.output


def test_list_says_which_machines_log_in_with_a_password(store):
    store.write_text(
        json.dumps(
            {
                "connections": [
                    {"id": "pw", "display_name": "PW", "host": "h", "port": 22, "user": "u", "auth": "password"}
                ]
            }
        ),
        encoding="utf-8",
    )

    out = run("list")

    assert out.exit_code == 0, out.output
    assert "(password)" in said(out)


# --- --password-stdin says how the machine is logged in to -------------------------------


def _password_home(monkeypatch, tmp_path):
    point_home(monkeypatch, tmp_path)
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "rhome"))
    monkeypatch.setattr(sys, "platform", "linux")
    return tmp_path / "rhome"


def test_password_stdin_without_auth_registers_by_password(store, reached, monkeypatch, tmp_path):
    """Piping a password in says how the machine is logged in to; without --auth it used to be read as a key
    login, and the piped line was left unread."""
    rhome = _password_home(monkeypatch, tmp_path)

    out = CliRunner().invoke(connection_app, [*PW_ADD, "--password-stdin", *FULL], input="hunter2\n")

    assert out.exit_code == 0, out.output
    (row,) = json.loads(store.read_text(encoding="utf-8"))["connections"]
    assert row["auth"] == "password" and "key" not in row
    assert (rhome / "keys" / "box.password").read_text(encoding="utf-8") == "hunter2\n"


def test_password_stdin_with_a_key_login_is_refused_and_nothing_is_written(store, reached, monkeypatch, tmp_path):
    """With a key login the piped line would be left to answer a question of its own -- the machine's name, when
    asked -- and the password would land in the registry in the clear."""
    rhome = _password_home(monkeypatch, tmp_path)

    out = CliRunner().invoke(
        connection_app,
        [*PW_ADD, "--auth", "key", "--key", "~/.ssh/id_rsa", "--password-stdin", *FULL],
        input="hunter2\n",
    )

    assert out.exit_code == 1, out.output
    assert "--password-stdin goes with --auth password" in said(out) and "Nothing was written" in out.output
    assert _nothing_kept(rhome) and not store.exists()
    assert "hunter2" not in out.output


@pytest.mark.skipif(sys.platform == "win32", reason="password machines are refused on Windows")
@pytest.mark.parametrize("order", ["key-after-password", "password-after-key"])
def test_add_refuses_a_pair_one_machine_s_alias_would_redirect_even_without_a_probe(
    store, monkeypatch, tmp_path, order
):
    """``--skip-probe`` writes with no connection at all, so the registry write is where the pair is judged:
    a key machine whose ``Host`` alias would take a registered password machine's logins, or a password machine
    at a registered key machine's id. Nothing is kept or written (reviewed 2026-10-20)."""
    point_home(monkeypatch, tmp_path)
    rhome = tmp_path / "rhome"
    monkeypatch.setenv("RAVEN_HOME", str(rhome))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("raven.cli.ops_connection_commands.probe", lambda row, **kw: pytest.fail("reached it"))
    if order == "key-after-password":
        theirs = {"id": "lab", "transport": "ssh", "host": "LabBox", "user": "alice", "auth": "password"}
        argv = ["add", "--id", "labbox", "--name", "Lab box", *SSH, "--skip-probe", *FULL]
        given = None
        named = "password machine 'lab'"
    else:
        theirs = {"id": "labbox", "transport": "ssh", "host": "198.51.100.66", "user": "root", "key": "~/k"}
        argv = [*PW_ADD[:6], "labbox", *PW_ADD[7:], "--auth", "password", "--password-stdin", "--skip-probe", *FULL]
        given = "hunter2\n"
        named = "key machine 'labbox'"
    store.write_text(json.dumps({"connections": [theirs]}), encoding="utf-8")

    out = CliRunner().invoke(connection_app, argv, input=given)

    assert out.exit_code == 1, out.output
    assert named in " ".join(out.output.split()) and "Nothing was written" in out.output
    assert [r["id"] for r in json.loads(store.read_text())["connections"]] == [theirs["id"]]
    assert not (rhome / "keys" / "box.password").exists()
    assert not (tmp_path / ".ssh" / "config").exists()
