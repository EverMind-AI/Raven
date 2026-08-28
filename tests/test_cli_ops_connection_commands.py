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

import pytest
from typer.testing import CliRunner

from raven.cli.ops_connection_commands import connection_app
from raven.ops import connections

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
    """The host's graph check reads this: an on-call node with no machine to run
    on should be refused before a single sub-agent is dispatched."""
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
