"""CLI tests for ``raven agent-plugins``.

The group is driven through the root app, against a fixture package on
disk: it is listed, inspected, imported into a temporary workspace,
checked against the ledger and removed again. Every run happens against a
temporary Raven home, so neither the ledger nor the workspace the
commands write lands in the home of whoever runs the suite.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from raven.agent_plugins.ledger import INSTALL_MARKER_FILENAME, read_install_marker, workspace_key
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


def _run_config(config: Path, *args: str):
    """Invoke one ``agent-plugins`` subcommand against another config file."""
    return runner.invoke(app, ["agent-plugins", *args, "--config", str(config)])


def _workspace_config(env: _Env, workspace: Path) -> Path:
    """A second config pointing the same home at another workspace."""
    config = env.home / f"config-{workspace.name}.json"
    config.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"workspace": str(workspace)}},
                "agentPlugins": {"packages": [str(env.package)]},
            }
        ),
        encoding="utf-8",
    )
    return config


def _ledger_record(env: _Env) -> dict | None:
    """This workspace's record of the package, from the ledger file on disk."""
    ledger = json.loads(env.ledger.read_text(encoding="utf-8"))
    key = workspace_key(env.workspace)
    return ledger["workspaces"].get(key, {}).get(PACKAGE_NAME)


def _write_v1_ledger(env: _Env) -> None:
    """A ledger as version 1 wrote it: one flat record, no install identity."""
    env.ledger.write_text(
        json.dumps(
            {
                "version": 1,
                "packages": {
                    PACKAGE_NAME: {
                        "version": "1.0.0",
                        "source": str(env.package),
                        "imported_at": "2026-10-25T00:00:00Z",
                        "skills": [SKILL_NAME],
                        "fingerprint": "sha256:old",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _write_own_skill(directory: Path, body: str = "The operator's own copy.\n") -> None:
    """A user-authored skill standing where an install once was."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "SKILL.md").write_text(
        f"---\nname: {SKILL_NAME}\ndescription: The operator's own skill.\n---\n\n{body}",
        encoding="utf-8",
    )


def _json_payload(result) -> dict:
    """The JSON document the command printed.

    The CLI announces the config file it reads on stderr, which the runner
    mixes into stdout; the document itself starts at the first brace.
    """
    text = result.stdout
    assert "{" in text, f"no JSON in the output: {text!r}"
    return json.loads(text[text.index("{") :])


def _flowed(result) -> str:
    """The output with Rich's line wraps collapsed to single spaces.

    A long temporary path reflows the console mid-message, so a multi-word
    phrase an assertion reads must not depend on where the line broke.
    """
    return " ".join(result.stdout.split())


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
    assert ledger["version"] == 2
    record = _ledger_record(env)
    assert record is not None
    assert record["skills"] == [SKILL_NAME]
    assert record["install_token"]
    assert record["skill_tokens"] == {SKILL_NAME: record["skill_tokens"][SKILL_NAME]}
    assert (env.workspace / "skills" / PACKAGE_NAME / ".raven-install.json").is_file()
    assert (env.installed / ".raven-install.json").is_file()


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
    assert _ledger_record(env)["skills"] == [SKILL_NAME]
    assert env.installed.is_dir()


def test_import_leaves_a_disabled_package_alone(env: _Env) -> None:
    config = json.loads(env.config.read_text(encoding="utf-8"))
    config["agentPlugins"]["disabled"] = [PACKAGE_NAME]
    env.config.write_text(json.dumps(config), encoding="utf-8")

    result = _run(env, "import", str(env.package))

    assert result.exit_code == 0, result.stdout
    assert not env.installed.exists()
    if env.ledger.exists():
        assert _ledger_record(env) is None


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

    assert Path(_ledger_record(env)["source"]).is_absolute()

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
    assert _ledger_record(env) is None


def test_remove_of_an_unknown_package_exits_non_zero(env: _Env) -> None:
    result = _run(env, "remove", "ghost-pkg")
    assert result.exit_code != 0
    assert "ghost-pkg" in result.stdout


def test_remove_forgets_the_record_when_the_installed_directory_is_already_gone(env: _Env) -> None:
    assert _run(env, "import", str(env.package)).exit_code == 0
    shutil.rmtree(env.workspace / "skills" / PACKAGE_NAME)

    result = _run(env, "remove", PACKAGE_NAME)

    assert result.exit_code == 0, result.stdout
    assert _ledger_record(env) is None


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
    assert _ledger_record(env)["skills"] == [SKILL_NAME]
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


# ---------------------------------------------------------------------------
# Workspace scoping and install identity
# ---------------------------------------------------------------------------


def test_two_workspaces_sharing_one_home_keep_their_imports_apart(env: _Env, tmp_path: Path) -> None:
    """The same home, the same ledger file, the same package name."""
    workspace_b = tmp_path / "ws-b"
    workspace_b.mkdir()
    config_b = _workspace_config(env, workspace_b)
    assert _run(env, "import", str(env.package)).exit_code == 0

    mine = workspace_b / "skills" / PACKAGE_NAME / SKILL_NAME
    _write_own_skill(mine, "USER CONTENT B.\n")

    result = _run_config(config_b, "import", str(env.package))

    assert result.exit_code != 0, result.stdout
    assert "another workspace" in _flowed(result)
    assert "USER CONTENT B." in (mine / "SKILL.md").read_text(encoding="utf-8")

    result = _run_config(config_b, "remove", PACKAGE_NAME)

    assert result.exit_code != 0
    assert "USER CONTENT B." in (mine / "SKILL.md").read_text(encoding="utf-8")
    assert _ledger_record(env) is not None

    assert _run(env, "remove", PACKAGE_NAME).exit_code == 0

    assert not (env.workspace / "skills" / PACKAGE_NAME).exists()
    assert "USER CONTENT B." in (mine / "SKILL.md").read_text(encoding="utf-8")


def test_a_linked_workspace_spelling_is_the_same_workspace(env: _Env, tmp_path: Path) -> None:
    """Import through a link, remove through the real path: one ledger key."""
    link = tmp_path / "linked-workspace"
    try:
        os.symlink(env.workspace, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create a directory link")
    config_link = _workspace_config(env, link)

    assert _run_config(config_link, "import", str(env.package)).exit_code == 0
    assert env.installed.is_dir()
    assert _ledger_record(env) is not None

    result = _run(env, "remove", PACKAGE_NAME)

    assert result.exit_code == 0, result.stdout
    assert not (env.workspace / "skills" / PACKAGE_NAME).exists()
    assert _ledger_record(env) is None


def test_remove_and_import_refuse_a_replacement_at_the_install_path(env: _Env, tmp_path: Path) -> None:
    """The install moved away; the operator's directory stands there now."""
    assert _run(env, "import", str(env.package)).exit_code == 0
    package_root = env.workspace / "skills" / PACKAGE_NAME
    shutil.move(str(package_root), str(tmp_path / "moved-away"))
    mine = package_root / SKILL_NAME
    _write_own_skill(mine, "USER CONTENT A.\n")

    result = _run(env, "import", str(env.package))

    assert result.exit_code != 0, result.stdout
    assert "refused" in result.stdout
    assert "USER CONTENT A." in (mine / "SKILL.md").read_text(encoding="utf-8")

    result = _run(env, "remove", PACKAGE_NAME)

    assert result.exit_code != 0, result.stdout
    assert "left alone" in _flowed(result)
    assert "USER CONTENT A." in (mine / "SKILL.md").read_text(encoding="utf-8")
    assert _ledger_record(env) is not None

    shutil.rmtree(package_root)
    assert _run(env, "import", str(env.package)).exit_code == 0
    assert "The demo body." in (package_root / SKILL_NAME / "SKILL.md").read_text(encoding="utf-8")


def test_remove_keeps_a_replaced_skill_directory(env: _Env) -> None:
    """One skill directory replaced by the operator; the rest is ours."""
    assert _run(env, "import", str(env.package)).exit_code == 0
    shutil.rmtree(env.installed)
    _write_own_skill(env.installed, "USER SKILL.\n")

    result = _run(env, "remove", PACKAGE_NAME, "--json")

    assert result.exit_code == 0, result.stdout
    payload = _json_payload(result)
    assert payload["kept"] == [SKILL_NAME]
    assert [item["skill"] for item in payload["kept_details"]] == [SKILL_NAME]
    assert set(payload["kept_details"][0]) == {"skill", "reason"}
    assert "install marker" in payload["kept_details"][0]["reason"]
    assert "USER SKILL." in (env.installed / "SKILL.md").read_text(encoding="utf-8")
    # The directory that remains is not provably ours, so the package root
    # and its record stay claimed: a later remove can finish once it is moved.
    assert _ledger_record(env) is not None
    assert (env.workspace / "skills" / PACKAGE_NAME / ".raven-install.json").is_file()

    result = _run(env, "remove", PACKAGE_NAME)
    assert result.exit_code == 0, result.stdout
    assert "unverified" in _flowed(result)
    assert "run remove again" in _flowed(result)
    assert f"removed '{PACKAGE_NAME}'" not in _flowed(result)

    shutil.rmtree(env.installed)
    assert _run(env, "remove", PACKAGE_NAME).exit_code == 0
    assert not (env.workspace / "skills" / PACKAGE_NAME).exists()
    assert _ledger_record(env) is None


def test_a_failed_removal_restores_the_marker_so_the_retry_finishes(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A partial rmtree can consume the marker before it fails.

    The marker lives inside the tree being deleted, so the retry has no proof
    unless the token the ledger still records is written back. Without the
    restore the retry keeps the directory and then drops the record.
    """
    from raven.agent_plugins.skills import remove_tree as real_remove_tree

    assert _run(env, "import", str(env.package)).exit_code == 0
    recorded_token = _ledger_record(env)["skill_tokens"][SKILL_NAME]
    state = {"failed": False}

    def _fail_once(path):
        target = Path(path)
        if target == env.installed and not state["failed"]:
            state["failed"] = True
            (target / INSTALL_MARKER_FILENAME).unlink()
            raise OSError("the directory is busy")
        return real_remove_tree(target)

    monkeypatch.setattr("raven.agent_plugins.skills.remove_tree", _fail_once)

    result = _run(env, "remove", PACKAGE_NAME, "--json")

    assert result.exit_code == 1, result.stdout
    payload = _json_payload(result)
    assert payload["failed"] == [{"skill": SKILL_NAME, "reason": "the directory is busy"}]
    assert _ledger_record(env) is not None
    assert read_install_marker(env.installed) == recorded_token

    monkeypatch.setattr("raven.agent_plugins.skills.remove_tree", real_remove_tree)
    result = _run(env, "remove", PACKAGE_NAME)

    assert result.exit_code == 0, result.stdout
    assert not env.installed.exists()
    assert _ledger_record(env) is None


def test_a_failed_removal_reports_a_marker_that_could_not_be_restored(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed restore is reported with the failure, and the record stays."""

    def _fail_once(path):
        target = Path(path)
        (target / INSTALL_MARKER_FILENAME).unlink()
        raise OSError("the directory is busy")

    def _refuse(directory, token):
        raise OSError("the marker cannot be written")

    assert _run(env, "import", str(env.package)).exit_code == 0
    monkeypatch.setattr("raven.agent_plugins.skills.remove_tree", _fail_once)
    monkeypatch.setattr("raven.agent_plugins.ledger.restore_install_marker", _refuse)

    result = _run(env, "remove", PACKAGE_NAME, "--json")

    assert result.exit_code == 1, result.stdout
    payload = _json_payload(result)
    assert "the directory is busy" in payload["failed"][0]["reason"]
    assert "could not be restored" in payload["failed"][0]["reason"]
    assert _ledger_record(env) is not None
    assert not (env.installed / INSTALL_MARKER_FILENAME).exists()


def test_a_failed_removal_does_not_restore_a_marker_into_a_missing_directory(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A directory that vanished while it was being deleted gets no marker back."""
    assert _run(env, "import", str(env.package)).exit_code == 0

    def _vanish(path):
        target = Path(path)
        if target == env.installed:
            shutil.rmtree(target)
            raise OSError("the directory is gone")
        return shutil.rmtree(path)

    monkeypatch.setattr("raven.agent_plugins.skills.remove_tree", _vanish)

    result = _run(env, "remove", PACKAGE_NAME, "--json")

    assert result.exit_code == 1, result.stdout
    payload = _json_payload(result)
    assert payload["failed"] == [{"skill": SKILL_NAME, "reason": "the directory is gone"}]
    assert not env.installed.exists()


def test_a_failed_removal_does_not_write_a_marker_through_a_link(
    env: _Env, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A link left at the skill's name is not written through to its target."""
    victim = tmp_path / "victim"
    victim.mkdir()
    probe = tmp_path / "probe-link"
    try:
        os.symlink(victim, probe, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform cannot create a directory link")
    probe.unlink()

    assert _run(env, "import", str(env.package)).exit_code == 0

    def _swap_for_a_link(path):
        target = Path(path)
        if target == env.installed:
            shutil.rmtree(target)
            os.symlink(victim, target, target_is_directory=True)
            raise OSError("the directory is busy")
        return shutil.rmtree(path)

    monkeypatch.setattr("raven.agent_plugins.skills.remove_tree", _swap_for_a_link)

    result = _run(env, "remove", PACKAGE_NAME, "--json")

    assert result.exit_code == 1, result.stdout
    payload = _json_payload(result)
    assert payload["failed"] == [{"skill": SKILL_NAME, "reason": "the directory is busy"}]
    assert not (victim / INSTALL_MARKER_FILENAME).exists()
    assert env.installed.is_symlink()


def test_a_failed_removal_does_not_write_a_marker_through_a_junction(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On Windows a junction's lstat is a directory; its reparse tag still refuses the write."""
    assert _run(env, "import", str(env.package)).exit_code == 0
    state = {"failed": False}
    real_lstat = Path.lstat

    def _consume_the_marker_then_fail(path):
        target = Path(path)
        if target == env.installed:
            (target / INSTALL_MARKER_FILENAME).unlink()
            state["failed"] = True
            raise OSError("the directory is busy")
        return shutil.rmtree(path)

    def _junction_lstat(self: Path):
        if state["failed"] and Path(self) == env.installed:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_reparse_tag=0xA0000003)
        return real_lstat(self)

    monkeypatch.setattr("raven.agent_plugins.skills.remove_tree", _consume_the_marker_then_fail)
    monkeypatch.setattr(Path, "lstat", _junction_lstat)

    result = _run(env, "remove", PACKAGE_NAME, "--json")

    assert result.exit_code == 1, result.stdout
    payload = _json_payload(result)
    assert payload["failed"] == [{"skill": SKILL_NAME, "reason": "the directory is busy"}]
    assert not (env.installed / INSTALL_MARKER_FILENAME).exists()


def test_a_mixed_removal_reports_the_kept_entry_beside_the_failure(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed skill must not hide the entry the same run kept."""
    other = "other-skill"
    skill = env.package / "skills" / other
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        f"---\nname: {other}\ndescription: A second demo skill.\n---\n\n# Other Skill\n",
        encoding="utf-8",
    )
    assert _run(env, "import", str(env.package)).exit_code == 0
    kept_dir = env.workspace / "skills" / PACKAGE_NAME / other
    shutil.rmtree(kept_dir)
    kept_dir.mkdir()
    (kept_dir / "SKILL.md").write_text("USER CONTENT.\n", encoding="utf-8")

    def _refuse(path):
        if Path(path) == env.installed:
            raise OSError("the directory is busy")
        return shutil.rmtree(path)

    monkeypatch.setattr("raven.agent_plugins.skills.remove_tree", _refuse)

    result = _run(env, "remove", PACKAGE_NAME)

    assert result.exit_code == 1, result.stdout
    flowed = _flowed(result)
    assert f"failed {SKILL_NAME}: the directory is busy" in flowed
    assert f"kept {other}" in flowed
    assert "1 unverified entry remains" in flowed
    assert "ledger record were kept" in flowed


def test_remove_help_says_a_zero_exit_does_not_mean_the_package_is_gone() -> None:
    """The exit-code contract is only safe if the help states it."""
    result = runner.invoke(app, ["agent-plugins", "remove", "--help"])

    assert result.exit_code == 0, result.stdout
    flowed = " ".join(result.stdout.lower().split())
    assert "exit code 0" in flowed
    assert "kept" in flowed


def test_a_remove_that_cannot_unlink_the_root_marker_keeps_the_record(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A root marker that cannot be removed leaves the package record claimed."""
    assert _run(env, "import", str(env.package)).exit_code == 0
    marker = env.workspace / "skills" / PACKAGE_NAME / INSTALL_MARKER_FILENAME
    real_unlink = Path.unlink

    def _refuse_unlink(self, *args, **kwargs):
        if Path(self) == marker:
            raise OSError("the marker is read-only")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", _refuse_unlink)

    result = _run(env, "remove", PACKAGE_NAME, "--json")

    assert result.exit_code == 1, result.stdout
    payload = _json_payload(result)
    assert payload["failed"][0]["skill"] == PACKAGE_NAME
    assert _ledger_record(env) is not None
    assert marker.is_file()


def test_a_remove_that_cannot_save_the_ledger_reports_it_and_keeps_the_record(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed save is reported; the file on disk still carries the record."""
    from raven.agent_plugins.ledger import PackageLedger

    assert _run(env, "import", str(env.package)).exit_code == 0

    def _refuse(self: PackageLedger) -> None:
        raise OSError("the disk is full")

    with monkeypatch.context() as context:
        context.setattr(PackageLedger, "save", _refuse)
        result = _run(env, "remove", PACKAGE_NAME)

    assert result.exit_code == 1, result.stdout
    assert "could not be saved" in _flowed(result)
    assert not (env.workspace / "skills" / PACKAGE_NAME).exists()
    assert _ledger_record(env) is not None


def test_remove_keeps_a_retryable_record_when_the_package_directory_cannot_be_removed(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed final rmdir keeps the record, and the marker that proves it.

    The record outlives the failure, so the marker must outlive it too:
    without the token back in place, the retry the failure asks for would be
    refused by the very ownership check that kept the record.
    """
    assert _run(env, "import", str(env.package)).exit_code == 0
    package_root = env.workspace / "skills" / PACKAGE_NAME
    recorded_token = _ledger_record(env)["install_token"]

    real_rmdir = Path.rmdir

    def _refuse(path):
        if Path(path) == package_root:
            raise OSError("the directory is busy")
        return real_rmdir(path)

    with monkeypatch.context() as context:
        context.setattr(Path, "rmdir", _refuse)
        result = _run(env, "remove", PACKAGE_NAME)

    assert result.exit_code != 0, result.stdout
    assert _ledger_record(env) is not None
    assert read_install_marker(package_root) == recorded_token
    assert (package_root / INSTALL_MARKER_FILENAME).is_file()

    result = _run(env, "remove", PACKAGE_NAME)

    assert result.exit_code == 0, result.stdout
    assert not package_root.exists()
    assert _ledger_record(env) is None


def test_import_reports_a_replaced_skill_as_released(env: _Env) -> None:
    assert _run(env, "import", str(env.package)).exit_code == 0
    shutil.rmtree(env.package / "skills" / SKILL_NAME)
    shutil.rmtree(env.installed)
    _write_own_skill(env.installed, "USER SKILL.\n")

    result = _run(env, "import", str(env.package), "--json")

    assert result.exit_code == 0, result.stdout
    payload = _json_payload(result)
    assert payload["packages"][0]["installed"] == []
    assert payload["packages"][0]["released"] == [SKILL_NAME]
    assert "USER SKILL." in (env.installed / "SKILL.md").read_text(encoding="utf-8")
    assert _ledger_record(env)["skills"] == []


def test_check_and_list_stay_scoped_to_the_current_workspace(env: _Env, tmp_path: Path) -> None:
    workspace_b = tmp_path / "ws-b"
    workspace_b.mkdir()
    config_b = _workspace_config(env, workspace_b)
    assert _run(env, "import", str(env.package)).exit_code == 0

    check = _run_config(config_b, "check", "--json")
    listing = _run_config(config_b, "list", "--json")

    assert check.exit_code == 0, check.stdout
    assert _json_payload(check)["packages"] == []
    assert _json_payload(listing)["packages"][0]["imported"] is False
    assert _json_payload(_run(env, "list", "--json"))["packages"][0]["imported"] is True


def test_list_distinguishes_a_valid_install_from_a_broken_record(env: _Env, tmp_path: Path) -> None:
    """``imported`` answers for the install standing on disk, not the record."""
    assert _run(env, "import", str(env.package)).exit_code == 0
    payload = _json_payload(_run(env, "list", "--json"))
    assert payload["packages"][0]["imported"] is True
    assert payload["packages"][0]["install"] == "verified"

    package_root = env.workspace / "skills" / PACKAGE_NAME
    shutil.move(str(package_root), str(tmp_path / "moved-away"))
    _write_own_skill(package_root / SKILL_NAME)

    payload = _json_payload(_run(env, "list", "--json"))
    assert payload["packages"][0]["imported"] is False
    assert payload["packages"][0]["install"] == "unverified"
    assert "unverified copy" in _flowed(_run(env, "list"))

    shutil.rmtree(package_root)

    payload = _json_payload(_run(env, "list", "--json"))
    assert payload["packages"][0]["imported"] is False
    assert payload["packages"][0]["install"] == "missing"


def test_list_does_not_read_a_legacy_record_as_an_install(env: _Env) -> None:
    _write_v1_ledger(env)

    payload = _json_payload(_run(env, "list", "--json"))

    assert payload["packages"][0]["imported"] is False
    assert payload["packages"][0]["install"] == "legacy"
    assert "legacy record" in _flowed(_run(env, "list"))


def test_check_reports_the_install_state_of_the_recorded_copy(env: _Env, tmp_path: Path) -> None:
    assert _run(env, "import", str(env.package)).exit_code == 0

    assert _json_payload(_run(env, "check", "--json"))["packages"][0]["install"] == "verified"

    package_root = env.workspace / "skills" / PACKAGE_NAME
    shutil.move(str(package_root), str(tmp_path / "moved-away"))
    _write_own_skill(package_root / SKILL_NAME)

    assert _json_payload(_run(env, "check", "--json"))["packages"][0]["install"] == "unverified"

    shutil.rmtree(package_root)

    assert _json_payload(_run(env, "check", "--json"))["packages"][0]["install"] == "missing"


# ---------------------------------------------------------------------------
# The v1 ledger
# ---------------------------------------------------------------------------


def test_a_v1_record_reports_unverified_and_removes_nothing(env: _Env) -> None:
    _write_v1_ledger(env)

    result = _run(env, "check", "--json")

    assert result.exit_code == 0, result.stdout
    row = _json_payload(result)["packages"][0]
    assert row["name"] == PACKAGE_NAME
    assert row["status"] == "unverified"

    result = _run(env, "remove", PACKAGE_NAME)

    assert result.exit_code != 0, result.stdout
    assert "nothing was deleted" in _flowed(result)
    ledger = json.loads(env.ledger.read_text(encoding="utf-8"))
    assert PACKAGE_NAME in ledger.get("packages", {}) or ledger.get("legacy", {}).get(PACKAGE_NAME)
    assert not (env.workspace / "skills" / PACKAGE_NAME).exists()


def test_a_fresh_import_supersedes_the_v1_record(env: _Env) -> None:
    _write_v1_ledger(env)

    result = _run(env, "import", str(env.package))

    assert result.exit_code == 0, result.stdout
    ledger = json.loads(env.ledger.read_text(encoding="utf-8"))
    assert ledger["version"] == 2
    assert ledger["legacy"] == {}
    assert _ledger_record(env) is not None


def test_a_v1_record_never_claims_a_directory_that_exists(env: _Env) -> None:
    _write_v1_ledger(env)
    mine = env.workspace / "skills" / PACKAGE_NAME / SKILL_NAME
    _write_own_skill(mine, "USER CONTENT.\n")

    result = _run(env, "import", str(env.package))

    assert result.exit_code != 0, result.stdout
    assert "legacy" in result.stdout
    assert "USER CONTENT." in (mine / "SKILL.md").read_text(encoding="utf-8")
    ledger = json.loads(env.ledger.read_text(encoding="utf-8"))
    assert ledger["legacy"].get(PACKAGE_NAME) is not None


# ---------------------------------------------------------------------------
# Ledger files that cannot be trusted
# ---------------------------------------------------------------------------


def test_a_future_ledger_version_blocks_import_and_is_left_untouched(env: _Env) -> None:
    env.ledger.parent.mkdir(parents=True, exist_ok=True)
    env.ledger.write_text(json.dumps({"version": 99, "packages": {}}), encoding="utf-8")
    before = env.ledger.read_bytes()

    result = _run(env, "import", str(env.package))

    assert result.exit_code != 0, result.stdout
    assert "version 99" in _flowed(result)
    assert env.ledger.read_bytes() == before
    assert not env.installed.exists()


def test_a_failed_ledger_save_leaves_no_claim_and_the_retry_refuses(env: _Env, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.agent_plugins.ledger import PackageLedger

    def _refuse(self: PackageLedger) -> None:
        raise OSError("the disk is full")

    with monkeypatch.context() as context:
        context.setattr(PackageLedger, "save", _refuse)
        result = _run(env, "import", str(env.package))

    assert result.exit_code != 0, result.stdout
    assert "could not be saved" in _flowed(result)
    assert not env.ledger.exists()
    assert (env.installed / "SKILL.md").is_file()

    result = _run(env, "import", str(env.package))

    assert result.exit_code != 0, result.stdout
    assert "refused" in result.stdout
    assert "The demo body." in (env.installed / "SKILL.md").read_text(encoding="utf-8")
