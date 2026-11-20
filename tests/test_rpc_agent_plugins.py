"""``agent_plugins.*`` handlers -- the settings page's view of Agent Plugin packages.

Every test runs against a temporary Raven home holding a config that names one
fixture package on disk: a ``plugin.json``, one skill and an ``mcp.json`` with
one stdio server. The list is read the way the page reads it, and the MCP
switch is checked against the config file it writes.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.rpc.errors import ConfigValidationError
from raven.rpc.methods import agent_plugins as rpc
from raven.rpc.models import AgentPluginsListResult, AgentPluginsSetMcpResult

PACKAGE = "demo-pkg"
SKILL = "demo-skill"
SERVER = f"{PACKAGE}__local"
MANIFEST_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"


@dataclass(frozen=True)
class _Env:
    config: Path
    workspace: Path
    package: Path


def _write_package(root: Path, *, mcp: bool = True) -> Path:
    skill = root / "skills" / SKILL
    skill.mkdir(parents=True)
    (root / "plugin.json").write_text(
        json.dumps({"$schema": MANIFEST_SCHEMA, "name": PACKAGE, "version": "1.2.0", "description": "A demo."}),
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text(
        f"---\nname: {SKILL}\ndescription: A demo skill.\n---\n\n# Demo\n",
        encoding="utf-8",
    )
    if mcp:
        (root / "mcp.json").write_text(
            json.dumps({"$schema": MCP_SCHEMA, "mcpServers": {"local": {"type": "stdio", "command": "npx"}}}),
            encoding="utf-8",
        )
    return root


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Env:
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    package = _write_package(tmp_path / PACKAGE)
    monkeypatch.setenv("RAVEN_HOME", str(home))
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
    monkeypatch.setattr("raven.home._current_config_path", config)
    return _Env(config=config, workspace=workspace, package=package)


def _config(env: _Env) -> dict:
    return json.loads(env.config.read_text(encoding="utf-8"))


def _allow(env: _Env, *names: str) -> None:
    data = _config(env)
    data["agentPlugins"]["mcpPackages"] = list(names)
    env.config.write_text(json.dumps(data), encoding="utf-8")


class _FakeManager:
    def __init__(self, states: dict[str, str]):
        self.states = states

    def status(self) -> list[dict]:
        return [
            {"name": name, "state": state, "error": "boom" if state == "error" else None}
            for name, state in sorted(self.states.items())
        ]


class _FakeLoop:
    """The two members of the agent loop the list reads, and nothing else."""

    def __init__(self, servers: set[str], states: dict[str, str] | None = None):
        self.package_mcp_server_names = frozenset(servers)
        self.mcp_manager_if_started = _FakeManager(states) if states is not None else None


def _list(loop: object | None = None) -> dict:
    result = asyncio.run(rpc.agent_plugins_list({}, agent_loop_factory=(lambda: loop) if loop else None))
    AgentPluginsListResult.model_validate(result)
    return result


def _set(name: object, enabled: object) -> dict:
    return asyncio.run(rpc.agent_plugins_set_mcp({"name": name, "enabled": enabled}))


def test_the_fake_loop_does_not_invent_a_contract() -> None:
    from raven.agent.loop.main import AgentLoop

    for member in ("package_mcp_server_names", "mcp_manager_if_started"):
        assert hasattr(AgentLoop, member), member


def test_the_loop_reports_the_package_servers_it_was_built_with() -> None:
    from raven.agent.loop.mcp_glue import McpGlueMixin

    names = McpGlueMixin.package_mcp_server_names.fget
    assert names(SimpleNamespace(_package_mcp_servers={SERVER: object()})) == frozenset({SERVER})
    assert names(SimpleNamespace(_package_mcp_servers=None)) == frozenset()


def test_lists_a_package_with_its_skills_and_servers(env: _Env) -> None:
    result = _list()
    assert result["problems"] == []
    [row] = result["packages"]
    assert row["name"] == PACKAGE
    assert row["version"] == "1.2.0"
    assert row["description"] == "A demo."
    assert row["source"] == str(env.package)
    assert row["skills"] == [SKILL]
    assert row["skills_imported"] is False
    assert row["mcp"] == {
        "servers": [{"name": SERVER, "state": "off", "error": None}],
        "allowed": False,
        "live": False,
        "problems": [],
    }


def test_an_allowed_package_reads_its_servers_from_the_running_manager(env: _Env) -> None:
    _allow(env, PACKAGE)
    mcp = _list(_FakeLoop({SERVER}, {SERVER: "error"}))["packages"][0]["mcp"]
    assert mcp["allowed"] is True
    assert mcp["live"] is True
    assert mcp["servers"] == [{"name": SERVER, "state": "error", "error": "boom"}]


def test_a_running_server_the_manager_has_not_reached_reads_disconnected(env: _Env) -> None:
    _allow(env, PACKAGE)
    mcp = _list(_FakeLoop({SERVER}))["packages"][0]["mcp"]
    assert mcp["servers"] == [{"name": SERVER, "state": "disconnected", "error": None}]


def test_allowed_and_running_disagree_until_the_next_start(env: _Env) -> None:
    """The page says "needs a restart" from exactly this pair."""
    _allow(env, PACKAGE)
    mcp = _list(_FakeLoop(set()))["packages"][0]["mcp"]
    assert (mcp["allowed"], mcp["live"]) == (True, False)
    assert mcp["servers"][0]["state"] == "off"

    _allow(env)
    mcp = _list(_FakeLoop({SERVER}, {SERVER: "connected"}))["packages"][0]["mcp"]
    assert (mcp["allowed"], mcp["live"]) == (False, True)


def test_a_host_server_of_the_same_name_is_reported_not_listed(env: _Env) -> None:
    data = _config(env)
    data["tools"] = {"mcpServers": {SERVER: {"command": "host-own"}}}
    env.config.write_text(json.dumps(data), encoding="utf-8")
    mcp = _list()["packages"][0]["mcp"]
    assert mcp["servers"] == []
    assert len(mcp["problems"]) == 1 and "already configured" in mcp["problems"][0]


def test_an_imported_package_reads_imported(env: _Env) -> None:
    from raven.agent_plugins import PackageLedger, discover_packages, import_package
    from raven.config.paths import get_agent_plugins_ledger_path

    [package], _ = discover_packages([env.package])
    ledger = PackageLedger.load(get_agent_plugins_ledger_path())
    assert not import_package(package, workspace=env.workspace, ledger=ledger).refusal
    ledger.save()
    assert _list()["packages"][0]["skills_imported"] is True

    # A record whose copy is gone is not an import standing in this workspace.
    shutil.rmtree(env.workspace / "skills" / PACKAGE)
    assert _list()["packages"][0]["skills_imported"] is False


def test_a_path_that_holds_no_package_is_a_problem(env: _Env, tmp_path: Path) -> None:
    data = _config(env)
    data["agentPlugins"]["packages"].append(str(tmp_path / "nowhere"))
    env.config.write_text(json.dumps(data), encoding="utf-8")
    result = _list()
    assert [row["name"] for row in result["packages"]] == [PACKAGE]
    assert [p["path"] for p in result["problems"]] == [str(tmp_path / "nowhere")]


def test_no_configured_package_is_an_empty_list(env: _Env) -> None:
    env.config.write_text(json.dumps({"agents": {"defaults": {"workspace": str(env.workspace)}}}), encoding="utf-8")
    assert _list() == {"packages": [], "problems": []}


def test_set_mcp_writes_the_package_into_the_allow_list(env: _Env) -> None:
    result = _set(PACKAGE, True)
    AgentPluginsSetMcpResult.model_validate(result)
    assert result == {"name": PACKAGE, "enabled": True, "changed": True}
    assert _config(env)["agentPlugins"]["mcpPackages"] == [PACKAGE]
    assert _set(PACKAGE, True)["changed"] is False

    assert _set(PACKAGE, False) == {"name": PACKAGE, "enabled": False, "changed": True}
    assert _config(env)["agentPlugins"]["mcpPackages"] == []


def test_set_mcp_refuses_to_allow_a_package_it_cannot_see(env: _Env) -> None:
    with pytest.raises(ConfigValidationError):
        _set("ghost", True)
    assert "mcpPackages" not in _config(env)["agentPlugins"]


def test_set_mcp_withholds_a_name_it_cannot_see(env: _Env) -> None:
    """A package moved away must still be switchable off."""
    _allow(env, "ghost", PACKAGE)
    assert _set("ghost", False)["changed"] is True
    assert _config(env)["agentPlugins"]["mcpPackages"] == [PACKAGE]


@pytest.mark.parametrize(("name", "enabled"), [("", True), (None, True), (7, True), (PACKAGE, "yes"), (PACKAGE, None)])
def test_set_mcp_refuses_malformed_params(env: _Env, name: object, enabled: object) -> None:
    with pytest.raises(ConfigValidationError):
        _set(name, enabled)
