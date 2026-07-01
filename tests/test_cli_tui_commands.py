"""CLI tests for ``raven tui`` commands — ``_build_tui_agent_loop`` wiring.

Verifies the memory backend and plugin tools are wired into the AgentLoop
constructed by ``_build_tui_agent_loop``, mirroring the agent-path coverage
in ``test_cli_agent_commands.py``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, sentinel

import pytest


@pytest.fixture
def patched_tui_loop_deps(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Patch all heavy deps of ``_build_tui_agent_loop`` for isolation.

    Mirrors ``patched_tui_build_deps`` in ``test_tui_cron_tool_wired.py``
    but additionally stubs the plugin-stack helpers so we can assert their
    return values flow into the AgentLoop constructor kwargs.

    Returns ``captured`` dict the tests inspect.
    """
    monkeypatch.chdir(tmp_path)
    captured: dict[str, Any] = {}

    config = MagicMock()
    config.workspace_path = tmp_path
    config.agents.defaults.model = "stub-model"
    config.agents.defaults.max_tool_iterations = 5
    config.agents.defaults.context_window_tokens = 65_536
    config.agents.defaults.enable_personalization = False
    config.agents.defaults.max_concurrent_subagents = 2
    config.agents.defaults.max_subagent_spawns_per_hour = 10
    config.tools.web.search.api_key = None
    config.tools.web.proxy = None
    config.tools.exec = MagicMock()
    config.tools.restrict_to_workspace = True
    config.tools.mcp_servers = []
    config.tools.sandbox = MagicMock()
    config.channels = MagicMock()
    monkeypatch.setattr("raven.cli._helpers.load_runtime_config", lambda _a, _b: config)
    monkeypatch.setattr("raven.cli._helpers.make_provider", lambda _c: MagicMock())

    ec_config = MagicMock()
    ec_config.skill_forge = MagicMock()
    ec_config.runtime = MagicMock()
    monkeypatch.setattr("raven.config.raven.load_raven_config", lambda: ec_config)

    monkeypatch.setattr("raven.session.manager.SessionManager", lambda _wp: MagicMock())
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("raven.config.paths.get_cron_dir", lambda: cron_dir)

    # AgentLoop spy captures all ctor kwargs.
    class _AgentLoopSpy:
        def __init__(self, **kwargs):
            captured["agent_loop_kwargs"] = kwargs
            self.tools = MagicMock()
            self.configure_personalization = MagicMock()

    monkeypatch.setattr("raven.agent.loop.AgentLoop", _AgentLoopSpy)

    # Stub plugin-stack helpers at the source module so patching works
    # before and after the import is added to tui_commands.
    fake_registry = sentinel.fake_registry
    fake_backend = sentinel.fake_backend
    fake_tools = [sentinel.fake_tool_1]

    monkeypatch.setattr(
        "raven.cli._plugin_stack.build_plugin_registry",
        lambda cfg: fake_registry,
    )
    monkeypatch.setattr(
        "raven.cli._plugin_stack.maybe_build_memory_backend",
        lambda ws, cfg, *, registry=None: fake_backend,
    )
    monkeypatch.setattr(
        "raven.cli._plugin_stack.build_plugin_tools",
        lambda ws, cfg, *, registry=None: fake_tools,
    )

    captured["fake_registry"] = fake_registry
    captured["fake_backend"] = fake_backend
    captured["fake_tools"] = fake_tools
    return captured


# ---------------------------------------------------------------------------
# memory backend wired into AgentLoop
# ---------------------------------------------------------------------------


def test_tui_agent_loop_receives_non_none_backend(patched_tui_loop_deps) -> None:
    """``_build_tui_agent_loop`` must pass ``backend=<non-None>`` to AgentLoop
    when the plugin stack returns a backend (today it passes nothing, so
    ``AgentLoop.backend`` defaults to ``None`` and store/recall are no-ops)."""
    from raven.cli.tui_commands import _build_tui_agent_loop

    _build_tui_agent_loop()

    kwargs = patched_tui_loop_deps["agent_loop_kwargs"]
    assert kwargs.get("backend") is not None, (
        "AgentLoop must receive backend= from _build_tui_agent_loop; got None"
    )
    assert kwargs["backend"] is patched_tui_loop_deps["fake_backend"]


# ---------------------------------------------------------------------------
# plugin tools wired into AgentLoop
# ---------------------------------------------------------------------------


def test_tui_agent_loop_receives_plugin_tools(patched_tui_loop_deps) -> None:
    """``_build_tui_agent_loop`` must pass ``plugin_tools=`` to AgentLoop
    so plugin-contributed tools are registered in the TUI agent's tool registry."""
    from raven.cli.tui_commands import _build_tui_agent_loop

    _build_tui_agent_loop()

    kwargs = patched_tui_loop_deps["agent_loop_kwargs"]
    assert "plugin_tools" in kwargs, "AgentLoop must receive plugin_tools kwarg"
    assert kwargs["plugin_tools"] is patched_tui_loop_deps["fake_tools"]


# ---------------------------------------------------------------------------
# single shared plugin registry (build_plugin_registry called once)
# ---------------------------------------------------------------------------


def test_tui_build_plugin_registry_called_once(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """The plugin registry must be built once and shared between the backend
    and tools calls — avoids double discovery overhead and ensures coherence."""
    monkeypatch.chdir(tmp_path)

    config = MagicMock()
    config.workspace_path = tmp_path
    config.agents.defaults.model = "stub-model"
    config.agents.defaults.max_tool_iterations = 5
    config.agents.defaults.context_window_tokens = 65_536
    config.agents.defaults.enable_personalization = False
    config.agents.defaults.max_concurrent_subagents = 2
    config.agents.defaults.max_subagent_spawns_per_hour = 10
    config.tools.web.search.api_key = None
    config.tools.web.proxy = None
    config.tools.exec = MagicMock()
    config.tools.restrict_to_workspace = True
    config.tools.mcp_servers = []
    config.tools.sandbox = MagicMock()
    config.channels = MagicMock()
    monkeypatch.setattr("raven.cli._helpers.load_runtime_config", lambda _a, _b: config)
    monkeypatch.setattr("raven.cli._helpers.make_provider", lambda _c: MagicMock())

    ec_config = MagicMock()
    ec_config.skill_forge = MagicMock()
    ec_config.runtime = MagicMock()
    monkeypatch.setattr("raven.config.raven.load_raven_config", lambda: ec_config)

    monkeypatch.setattr("raven.session.manager.SessionManager", lambda _wp: MagicMock())
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("raven.config.paths.get_cron_dir", lambda: cron_dir)

    monkeypatch.setattr(
        "raven.agent.loop.AgentLoop",
        lambda **kw: MagicMock(tools=MagicMock(), configure_personalization=MagicMock()),
    )

    call_count = {"build": 0}
    passed_registries: list[Any] = []

    def _spy_registry(cfg):
        call_count["build"] += 1
        return sentinel.shared_registry

    def _spy_backend(ws, cfg, *, registry=None):
        passed_registries.append(("backend", registry))
        return None

    def _spy_tools(ws, cfg, *, registry=None):
        passed_registries.append(("tools", registry))
        return []

    monkeypatch.setattr("raven.cli._plugin_stack.build_plugin_registry", _spy_registry)
    monkeypatch.setattr("raven.cli._plugin_stack.maybe_build_memory_backend", _spy_backend)
    monkeypatch.setattr("raven.cli._plugin_stack.build_plugin_tools", _spy_tools)

    from raven.cli.tui_commands import _build_tui_agent_loop

    _build_tui_agent_loop()

    assert call_count["build"] == 1, "build_plugin_registry should be called exactly once"
    backend_reg = next(r for name, r in passed_registries if name == "backend")
    tools_reg = next(r for name, r in passed_registries if name == "tools")
    assert backend_reg is sentinel.shared_registry
    assert tools_reg is sentinel.shared_registry
