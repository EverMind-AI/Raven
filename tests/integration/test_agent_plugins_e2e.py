"""End-to-end: an Agent Plugin package imported into a real workspace.

The CLI imports a fixture package into a temporary workspace, the skill
catalog reads that workspace the way the runtime does, and ``remove`` takes
the package back out again -- so the whole loop, from the manifest on disk
to the pool the agent is served from, is exercised against real files.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.memory_engine import LocalSkillCatalog

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
    def skills_root(self) -> Path:
        return self.workspace / "skills"


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


def _import(env: _Env):
    result = _run(env, "import", str(env.package))
    assert result.exit_code == 0, result.stdout
    return result


def _catalog(env: _Env) -> LocalSkillCatalog:
    """A catalog over the workspace, without the background file watcher."""
    return LocalSkillCatalog(env.workspace, start_watcher=False)


# ---------------------------------------------------------------------------
# import -> the pool
# ---------------------------------------------------------------------------


def test_the_imported_skill_is_listed_under_the_package_label(env: _Env) -> None:
    _import(env)

    metas = {meta.id: meta for meta in _catalog(env).gather_all_skills()}

    assert f"{PACKAGE_NAME}/{SKILL_NAME}" in metas
    meta = metas[f"{PACKAGE_NAME}/{SKILL_NAME}"]
    assert meta.source == PACKAGE_NAME
    assert meta.name == SKILL_NAME
    assert meta.always is False


def test_the_imported_skill_body_is_readable(env: _Env) -> None:
    _import(env)

    body = _catalog(env).load_skill(SKILL_NAME)

    assert body is not None
    assert "The demo body." in body
    assert (env.skills_root / PACKAGE_NAME / SKILL_NAME / "SKILL.md").is_file()


# ---------------------------------------------------------------------------
# remove -> the pool
# ---------------------------------------------------------------------------


def test_remove_empties_the_workspace_skills_tree(env: _Env) -> None:
    _import(env)
    assert (env.skills_root / PACKAGE_NAME / SKILL_NAME).is_dir()

    result = _run(env, "remove", PACKAGE_NAME)

    assert result.exit_code == 0, result.stdout
    assert not (env.skills_root / PACKAGE_NAME).exists()
    assert list(env.skills_root.iterdir()) == []

    ledger = json.loads(env.ledger.read_text(encoding="utf-8"))
    assert PACKAGE_NAME not in ledger["packages"]


def test_the_pool_no_longer_sees_a_removed_package(env: _Env) -> None:
    _import(env)
    assert SKILL_NAME in {meta.name for meta in _catalog(env).gather_all_skills()}

    assert _run(env, "remove", PACKAGE_NAME).exit_code == 0

    after = _catalog(env)
    assert SKILL_NAME not in {meta.name for meta in after.gather_all_skills()}
    assert after.load_skill(SKILL_NAME) is None
