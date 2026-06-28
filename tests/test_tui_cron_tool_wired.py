"""Tests for the ``tui-cron-tool`` wiring.

Validates that ``_build_tui_agent_loop`` wires ``cron_service``,
constructs the cron callback with the MessageTool swap wrapper, and starts
the cron tick loop.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest


@pytest.fixture
def patched_tui_build_deps(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """Patch everything ``_build_tui_agent_loop`` imports lazily so we can
    capture AgentLoop ctor kwargs + cron callback assignment without
    spinning up real provider / SessionManager / asyncio loop.

    Returns a ``captured`` dict the tests inspect.
    """
    monkeypatch.chdir(tmp_path)
    captured: dict[str, Any] = {}

    # Stub config objects
    config = MagicMock()
    config.workspace_path = str(tmp_path)
    config.agents.defaults.model = "stub-model"
    config.agents.defaults.max_tool_iterations = 5
    config.agents.defaults.context_window_tokens = 65_536
    config.agents.defaults.enable_personalization = False
    config.tools.web.search.api_key = None
    config.tools.web.proxy = None
    config.tools.exec = MagicMock()
    config.tools.restrict_to_workspace = True
    config.tools.mcp_servers = []
    config.tools.sandbox = MagicMock()
    config.channels = MagicMock()
    monkeypatch.setattr(
        "raven.cli._helpers.load_runtime_config",
        lambda _a, _b: config,
    )
    monkeypatch.setattr(
        "raven.cli._helpers.make_provider",
        lambda _c: MagicMock(),
    )

    ec_config = MagicMock()
    ec_config.skill_forge = MagicMock()
    monkeypatch.setattr(
        "raven.config.raven.load_raven_config",
        lambda: ec_config,
    )

    # Stub SessionManager + cron store dir
    monkeypatch.setattr(
        "raven.session.manager.SessionManager",
        lambda _wp: MagicMock(),
    )
    cron_dir = tmp_path / "cron"
    cron_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        "raven.config.paths.get_cron_dir",
        lambda: cron_dir,
    )

    # AgentLoop spy
    class _AgentLoopSpy:
        def __init__(self, **kwargs):
            captured["agent_loop_kwargs"] = kwargs
            self._on_job_set = None
            self.tools = MagicMock()
            self.configure_personalization = MagicMock()

    monkeypatch.setattr("raven.agent.loop.AgentLoop", _AgentLoopSpy)

    # CronService spy — capture allowed_channels + start() + on_job assignment
    class _CronServiceSpy:
        instances: list[Any] = []

        def __init__(self, store_path, *, allowed_channels=None, **kwargs):
            self.store_path = store_path
            self.allowed_channels = allowed_channels or set()
            self.on_job = None
            self.started = False
            type(self).instances.append(self)

        async def start(self):
            self.started = True

    _CronServiceSpy.instances = []
    monkeypatch.setattr(
        "raven.proactive_engine.schedulers.cron.service.CronService",
        _CronServiceSpy,
    )

    captured["cron_service_class"] = _CronServiceSpy
    return captured


# ---------------------------------------------------------------------------
# 2.1 — TUI AgentLoop receives a cron_service
# ---------------------------------------------------------------------------


def test_tui_agent_loop_receives_cron_service(patched_tui_build_deps) -> None:
    """``_build_tui_agent_loop`` SHALL pass a ``CronService`` instance to
    ``AgentLoop(cron_service=...)`` so that ``CronTool`` auto-registers
    per ``agent/loop/main.py:314-321``.
    """
    from raven.cli.tui_commands import _build_tui_agent_loop

    _build_tui_agent_loop()

    kwargs = patched_tui_build_deps["agent_loop_kwargs"]
    assert "cron_service" in kwargs, (
        "AgentLoop ctor must receive cron_service kwarg for CronTool auto-register"
    )
    assert kwargs["cron_service"] is not None
    cls = patched_tui_build_deps["cron_service_class"]
    assert isinstance(kwargs["cron_service"], cls)


# ---------------------------------------------------------------------------
# 2.2 — allowed_channels == {"tui"}
# ---------------------------------------------------------------------------


def test_tui_cron_service_allowed_channels_is_tui(patched_tui_build_deps) -> None:
    """TUI process's ``CronService`` SHALL be scoped to ``allowed_channels={"tui"}``
    so it only claims TUI-channel cron jobs and does not race gateway IM crons.
    """
    from raven.cli.tui_commands import _build_tui_agent_loop

    _build_tui_agent_loop()

    cls = patched_tui_build_deps["cron_service_class"]
    assert len(cls.instances) >= 1, (
        "CronService should be constructed once in _build_tui_agent_loop"
    )
    cron = cls.instances[0]
    assert cron.allowed_channels == {"tui"}


# ---------------------------------------------------------------------------
# 2.3 — cron.on_job is wired in run(), not in _build_tui_agent_loop
# ---------------------------------------------------------------------------


def test_tui_cron_on_job_wired_in_run_not_build(patched_tui_build_deps) -> None:
    """``cron.on_job`` is wired in the RPC server run loop once the spine
    scheduler exists (a reminder submits a CRON turn through it and its
    reply is fanned out as cron.delivered). ``_build_tui_agent_loop`` only builds
    the cron service — it must NOT set on_job (the scheduler doesn't exist yet)."""
    from raven.cli.tui_commands import _build_tui_agent_loop

    _build_tui_agent_loop()

    cls = patched_tui_build_deps["cron_service_class"]
    cron = cls.instances[0]
    assert cron.on_job is None, (
        "on_job must be wired in run() (needs the spine scheduler), not in _build_tui_agent_loop"
    )


# The message-tool-swap wrapper and the bus outbound handler are gone
# cron now runs as a spine CRON turn and the reply is fanned out as
# cron.delivered by ``_build_cron_callback_spine`` (covered in
# test_tui_cron_delivered_event.py), so no bus publish / callback swap remains.
