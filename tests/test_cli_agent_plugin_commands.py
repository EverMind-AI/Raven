"""CLI tests for ``raven agent-plugins``.

The group is driven through the root app, against a fixture package on
disk: it is listed, inspected, imported into a temporary workspace,
checked against the ledger and removed again. Every run happens against a
temporary Raven home, so neither the ledger nor the workspace the
commands write lands in the home of whoever runs the suite.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app

runner = CliRunner()

PACKAGE_NAME = "demo-pkg"
SKILL_NAME = "demo-skill"
SCHEMA_URL = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"


@dataclass(frozen=True)
class _Env:
    """The paths one test works against."""

    home: Path
    config: Path
    workspace: Path
    package: Path

    @property
    def ledger(self) -> Path:
        return self.home / "agent-plugins.json"

    @property
    def installed(self) -> Path:
        return self.workspace / "skills" / PACKAGE_NAME / SKILL_NAME


def _write_package(root: Path) -> Path:
    """A minimal Agent Plugins 1.0 package holding one skill."""
    skill = root / "skills" / SKILL_NAME
    skill.mkdir(parents=True)
    (root / "plugin.json").write_text(
        json.dumps(
            {
                "$schema": SCHEMA_URL,
                "name": PACKAGE_NAME,
                "version": "1.0.0",
            }
        ),
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text(
        "---\n"
        f"name: {SKILL_NAME}\n"
        "description: A demo skill shipped by the demo package.\n"
        "always: true\n"
        "---\n"
        "\n"
        "# Demo Skill\n"
        "\n"
        "The demo body.\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give the tables room, so a long temp path cannot squeeze a column
    out of the output the assertions read.
    """
    from rich.console import Console

    from raven.cli import agent_plugin_commands

    monkeypatch.setattr(agent_plugin_commands, "console", Console(width=200))


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Env:
    """A home, a workspace and a package, all inside the test's tmp dir."""
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    package = _write_package(tmp_path / PACKAGE_NAME)

    monkeypatch.setenv("RAVEN_HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))

    config = home / "config.json"
    config.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"workspace": str(workspace)}},
                "agentPlugins": {"packages": [str(package)]},
            }
        ),
        encoding="utf-8",
    )
    return _Env(home=home, config=config, workspace=workspace, package=package)


def _run(env: _Env, *args: str):
    """Invoke one ``agent-plugins`` subcommand against the test's config."""
    return runner.invoke(app, ["agent-plugins", *args, "--config", str(env.config)])


def _json_payload(result) -> dict:
    """The JSON document the command printed.

    The CLI announces the config file it reads on stderr, which the runner
    mixes into stdout; the document itself starts at the first brace.
    """
    text = result.stdout
    assert "{" in text, f"no JSON in the output: {text!r}"
    return json.loads(text[text.index("{") :])


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_prints_the_package_name(env: _Env) -> None:
    result = _run(env, "list")

    assert result.exit_code == 0, result.stdout
    assert PACKAGE_NAME in result.stdout


def test_list_json_carries_the_package_name(env: _Env) -> None:
    result = _run(env, "list", "--json")

    assert result.exit_code == 0, result.stdout
    payload = _json_payload(result)
    assert [package["name"] for package in payload["packages"]] == [PACKAGE_NAME]
    assert payload["problems"] == []


def test_list_json_marks_an_imported_package_as_imported(env: _Env) -> None:
    assert _run(env, "import", str(env.package)).exit_code == 0

    result = _run(env, "list", "--json")

    assert result.exit_code == 0, result.stdout
    payload = _json_payload(result)
    assert payload["packages"][0]["imported"] is True


# ---------------------------------------------------------------------------
# inspect
# ---------------------------------------------------------------------------


def test_inspect_prints_the_package_and_its_skill(env: _Env) -> None:
    result = _run(env, "inspect", str(env.package))

    assert result.exit_code == 0, result.stdout
    assert PACKAGE_NAME in result.stdout
    assert SKILL_NAME in result.stdout


def test_inspect_json_carries_the_skill_list(env: _Env) -> None:
    result = _run(env, "inspect", str(env.package), "--json")

    assert result.exit_code == 0, result.stdout
    payload = _json_payload(result)
    assert payload["packages"][0]["name"] == PACKAGE_NAME
    assert payload["packages"][0]["skills"] == [SKILL_NAME]
    assert payload["packages"][0]["problems"] == []


def test_inspect_of_a_directory_without_a_manifest_exits_non_zero(env: _Env) -> None:
    empty = env.home / "not-a-package"
    empty.mkdir()

    result = _run(env, "inspect", str(empty))

    assert result.exit_code != 0
    assert "plugin.json" in result.stdout


def test_inspect_touches_nothing(env: _Env) -> None:
    result = _run(env, "inspect", str(env.package))

    assert result.exit_code == 0, result.stdout
    assert not (env.workspace / "skills" / PACKAGE_NAME).exists()
    assert not env.ledger.exists()


def test_inspect_creates_no_package_data_directory(env: _Env) -> None:
    """Reading a package must not leave a directory for a server it will not run."""
    _write_mcp_config(env.package, {"demo": {"type": "stdio", "command": "npx"}})

    assert _run(env, "inspect", str(env.package)).exit_code == 0

    assert not (env.home / "plugin-data").exists()


def test_inspect_json_reports_a_broken_package_as_a_failure(env: _Env) -> None:
    """A package whose manifest does not validate is a failure in JSON too.

    The document carries the reason and the exit code is non-zero, so a
    script driving the command cannot read a broken package as "nothing
    there".
    """
    (env.package / "plugin.json").write_text("{ not json", encoding="utf-8")

    result = _run(env, "inspect", str(env.package), "--json")

    assert result.exit_code != 0
    payload = _json_payload(result)
    assert payload["packages"] == []
    assert payload["problems"], "the failure must be in the document"
    assert "not valid JSON" in payload["problems"][0]["reason"]


def test_import_json_reports_a_broken_package_as_a_failure(env: _Env) -> None:
    (env.package / "plugin.json").write_text("{ not json", encoding="utf-8")

    result = _run(env, "import", str(env.package), "--json")

    assert result.exit_code != 0
    payload = _json_payload(result)
    assert payload["packages"] == []
    assert payload["problems"], "the failure must be in the document"


# ---------------------------------------------------------------------------
# import
# ---------------------------------------------------------------------------


def test_import_installs_the_skill_and_writes_the_ledger(env: _Env) -> None:
    result = _run(env, "import", str(env.package))

    assert result.exit_code == 0, result.stdout
    assert "1 installed" in result.stdout
    assert (env.installed / "SKILL.md").is_file()
    assert env.ledger.is_file()

    ledger = json.loads(env.ledger.read_text(encoding="utf-8"))
    assert PACKAGE_NAME in ledger["packages"]
    assert ledger["packages"][PACKAGE_NAME]["skills"] == [SKILL_NAME]


def test_import_json_reports_the_installed_skill(env: _Env) -> None:
    result = _run(env, "import", str(env.package), "--json")

    assert result.exit_code == 0, result.stdout
    payload = _json_payload(result)
    assert payload["packages"][0]["package"] == PACKAGE_NAME
    assert payload["packages"][0]["installed"] == [SKILL_NAME]
    assert payload["packages"][0]["failed"] == []


def test_import_keeps_the_ledger_entry_when_a_stale_skill_cannot_be_removed(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed stale deletion fails the command and leaves the skill owned."""
    assert _run(env, "import", str(env.package)).exit_code == 0
    shutil.rmtree(env.package / "skills" / SKILL_NAME)

    real_rmtree = shutil.rmtree

    def _refuse(path, *args, **kwargs):
        if Path(path).name == SKILL_NAME:
            raise OSError("the directory is busy")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr("raven.agent_plugins.skills.shutil.rmtree", _refuse)

    result = _run(env, "import", str(env.package), "--json")

    assert result.exit_code == 1
    payload = _json_payload(result)
    assert payload["packages"][0]["retained"] == [SKILL_NAME]
    ledger = json.loads(env.ledger.read_text(encoding="utf-8"))
    assert ledger["packages"][PACKAGE_NAME]["skills"] == [SKILL_NAME]
    assert env.installed.is_dir()


def test_import_leaves_a_disabled_package_alone(env: _Env) -> None:
    config = json.loads(env.config.read_text(encoding="utf-8"))
    config["agentPlugins"]["disabled"] = [PACKAGE_NAME]
    env.config.write_text(json.dumps(config), encoding="utf-8")

    result = _run(env, "import", str(env.package))

    assert result.exit_code == 0, result.stdout
    assert not env.installed.exists()
    if env.ledger.exists():
        assert PACKAGE_NAME not in json.loads(env.ledger.read_text(encoding="utf-8"))["packages"]


def test_inspect_of_a_disabled_package_reports_nothing(env: _Env) -> None:
    config = json.loads(env.config.read_text(encoding="utf-8"))
    config["agentPlugins"]["disabled"] = [PACKAGE_NAME]
    env.config.write_text(json.dumps(config), encoding="utf-8")

    result = _run(env, "inspect", str(env.package), "--json")

    assert result.exit_code == 0, result.stdout
    assert _json_payload(result)["packages"] == []


def test_check_finds_a_package_imported_by_a_relative_path(
    env: _Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(env.package)
    assert _run(env, "import", ".").exit_code == 0

    ledger = json.loads(env.ledger.read_text(encoding="utf-8"))
    assert Path(ledger["packages"][PACKAGE_NAME]["source"]).is_absolute()

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = _run(env, "check", "--json")

    assert result.exit_code == 0, result.stdout
    assert _json_payload(result)["packages"][0]["status"] == "current"


def test_import_takes_the_always_flag_out_of_the_copy(env: _Env) -> None:
    """A third-party package does not get to be injected into every turn."""
    assert _run(env, "import", str(env.package)).exit_code == 0

    body = (env.installed / "SKILL.md").read_text(encoding="utf-8")

    assert "always: true" not in body
    assert f"name: {SKILL_NAME}" in body


# ---------------------------------------------------------------------------
# remove
# ---------------------------------------------------------------------------


def test_remove_deletes_the_imported_directory_and_the_ledger_entry(env: _Env) -> None:
    assert _run(env, "import", str(env.package)).exit_code == 0
    assert env.installed.is_dir()

    result = _run(env, "remove", PACKAGE_NAME)

    assert result.exit_code == 0, result.stdout
    assert not (env.workspace / "skills" / PACKAGE_NAME).exists()

    ledger = json.loads(env.ledger.read_text(encoding="utf-8"))
    assert PACKAGE_NAME not in ledger["packages"]


def test_remove_of_an_unknown_package_exits_non_zero(env: _Env) -> None:
    result = _run(env, "remove", "ghost-pkg")
    assert result.exit_code != 0
    assert "ghost-pkg" in result.stdout


def test_remove_keeps_a_directory_the_ledger_does_not_claim(env: _Env) -> None:
    """Only what the ledger installed is deleted."""
    assert _run(env, "import", str(env.package)).exit_code == 0
    mine = env.workspace / "skills" / PACKAGE_NAME / "mine"
    mine.mkdir()
    (mine / "NOTES.md").write_text("The operator's own directory.\n", encoding="utf-8")

    result = _run(env, "remove", PACKAGE_NAME)

    assert result.exit_code == 0, result.stdout
    assert not env.installed.exists()
    assert (mine / "NOTES.md").is_file()


def test_remove_reports_a_failed_deletion_and_keeps_the_ledger_entry(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert _run(env, "import", str(env.package)).exit_code == 0

    def _refuse(_path):
        raise PermissionError("the file is read-only")

    monkeypatch.setattr("raven.agent_plugins.skills.remove_tree", _refuse)

    result = _run(env, "remove", PACKAGE_NAME, "--json")

    assert result.exit_code == 1
    payload = _json_payload(result)
    assert payload["failed"] == [{"skill": SKILL_NAME, "reason": "the file is read-only"}]
    ledger = json.loads(env.ledger.read_text(encoding="utf-8"))
    assert ledger["packages"][PACKAGE_NAME]["skills"] == [SKILL_NAME]
    assert env.installed.is_dir()


def test_import_refuses_a_package_directory_the_ledger_does_not_own(env: _Env) -> None:
    """The operator's own directory of the same name is never written into."""
    mine = env.workspace / "skills" / PACKAGE_NAME
    mine.mkdir(parents=True)
    (mine / "NOTES.md").write_text("The operator's own directory.\n", encoding="utf-8")

    result = _run(env, "import", str(env.package))

    assert result.exit_code != 0
    assert "refused" in result.stdout
    assert (mine / "NOTES.md").is_file()
    assert not env.installed.exists()


# ---------------------------------------------------------------------------
# check
# ---------------------------------------------------------------------------


def test_check_reports_an_imported_package_as_current(env: _Env) -> None:
    assert _run(env, "import", str(env.package)).exit_code == 0

    result = _run(env, "check")

    assert result.exit_code == 0, result.stdout
    assert "current" in result.stdout


def test_check_json_reports_the_current_status(env: _Env) -> None:
    assert _run(env, "import", str(env.package)).exit_code == 0

    result = _run(env, "check", "--json")

    assert result.exit_code == 0, result.stdout
    payload = _json_payload(result)
    assert payload["packages"][0]["name"] == PACKAGE_NAME
    assert payload["packages"][0]["status"] == "current"


def test_check_reports_a_changed_package_as_stale(env: _Env) -> None:
    assert _run(env, "import", str(env.package)).exit_code == 0
    (env.package / "skills" / SKILL_NAME / "SKILL.md").write_text(
        "---\n"
        f"name: {SKILL_NAME}\n"
        "description: The demo skill, changed after the import.\n"
        "---\n"
        "\n"
        "# Demo Skill\n"
        "\n"
        "The changed body.\n",
        encoding="utf-8",
    )

    result = _run(env, "check")

    assert result.exit_code == 0, result.stdout
    assert "stale" in result.stdout
    assert _json_payload(_run(env, "check", "--json"))["packages"][0]["status"] == "stale"


# ---------------------------------------------------------------------------
# mcp.json
# ---------------------------------------------------------------------------


def _write_mcp_config(package: Path, servers: dict) -> None:
    (package / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json",
                "mcpServers": servers,
            }
        ),
        encoding="utf-8",
    )


def test_list_marks_a_packages_mcp_servers_as_not_enabled(env: _Env) -> None:
    _write_mcp_config(env.package, {"demo": {"type": "stdio", "command": "npx"}})

    result = _run(env, "list")

    assert result.exit_code == 0, result.stdout
    assert "1 not enabled" in result.stdout


def test_list_marks_an_opted_in_packages_mcp_servers_as_running(env: _Env) -> None:
    _write_mcp_config(env.package, {"demo": {"type": "stdio", "command": "npx"}})
    env.config.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"workspace": str(env.workspace)}},
                "agentPlugins": {"packages": [str(env.package)], "mcpPackages": [PACKAGE_NAME]},
            }
        ),
        encoding="utf-8",
    )

    result = _run(env, "list")

    assert result.exit_code == 0, result.stdout
    assert "1 running" in result.stdout


def test_inspect_reports_the_packages_mcp_servers(env: _Env) -> None:
    _write_mcp_config(
        env.package,
        {
            "demo": {"type": "stdio", "command": "npx"},
            "broken": {"type": "stdio", "command": "npx -y server"},
        },
    )

    result = _run(env, "inspect", str(env.package))

    assert result.exit_code == 0, result.stdout
    assert f"{PACKAGE_NAME}__demo" in result.stdout
    assert "single executable token" in result.stdout


def test_inspect_json_carries_the_mcp_section(env: _Env) -> None:
    _write_mcp_config(env.package, {"demo": {"type": "stdio", "command": "npx"}})

    result = _run(env, "inspect", str(env.package), "--json")

    assert result.exit_code == 0, result.stdout
    mcp = _json_payload(result)["packages"][0]["mcp"]
    assert mcp["servers"] == [f"{PACKAGE_NAME}__demo"]
    assert mcp["disabled"] == ""
