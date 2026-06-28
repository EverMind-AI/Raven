"""Tests for ``_default_session_info()`` init bundle.

The info dict carries 4 init bundle fields
(``model_id``/``provider``/``context_window``/``lazy``) populated from
``config.agents.defaults`` instead of a hardcoded placeholder.

It further carries real ``tools``/``skills``/``usage``/``version`` populated
from in-repo subsystems via the ``agent_loop`` handle;
``_default_session_info`` accepts ``(agent_loop, config)``;
``register_session_methods`` gains an ``agent_loop_factory`` keyword;
``_resolve_context_window`` stub removed in favour of
``config.agents.defaults.context_window_tokens``.
"""

from __future__ import annotations

import importlib.metadata
import inspect
from pathlib import Path
from typing import Any

import pytest

from raven.config.loader import load_config
from raven.tui_rpc.methods import session as session_module
from raven.tui_rpc.methods.session import _default_session_info

# ---------------------------------------------------------------------------
# Fake AgentLoop fixtures (minimal duck-typed handles)
# ---------------------------------------------------------------------------


class _FakeToolRegistry:
    """Stand-in for ``raven.agent.tools.registry.ToolRegistry``."""

    @property
    def tool_names(self) -> list[str]:
        return ["message", "exec", "web_search"]


class _FakeSkillCatalog:
    """Real LocalSkillCatalog.list_skills returns legacy-shape dicts."""

    def list_skills(self, filter_unavailable: bool = True) -> list[dict[str, str]]:
        return [
            {"name": "curator", "path": "/tmp/curator", "source": "builtin"},
            {"name": "cron-tracker", "path": "/tmp/cron-tracker", "source": "builtin"},
            {"name": "my-workspace-skill", "path": "/tmp/my-skill", "source": "workspace"},
        ]


class _FakeAgentContext:
    @property
    def skills(self) -> _FakeSkillCatalog:
        return _FakeSkillCatalog()


class _FakeUsageSnapshot:
    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0
        self.cache_read_tokens = 0
        self.cache_write_tokens = 0
        self.reasoning_tokens = 0
        self.estimated_cost_usd = 0.0
        self.model = "__empty__"
        self.session_key = "tui:default"


class _FakeUsageTracker:
    name = "usage_tracker"

    def snapshot(self, session_key: str) -> _FakeUsageSnapshot:
        return _FakeUsageSnapshot()


class _FakeStrategyRegistry:
    """Stand-in for ``raven.token_wise.registry.StrategyRegistry``."""

    def __init__(self, with_usage_tracker: bool = True) -> None:
        self._tracker = _FakeUsageTracker() if with_usage_tracker else None

    def get(self, name: str) -> Any:
        if name == "usage_tracker":
            return self._tracker
        return None


class _FakeAgentLoop:
    """Minimal AgentLoop stand-in exposing the 3 attrs the helpers read."""

    def __init__(self, with_usage_tracker: bool = True) -> None:
        self.tools = _FakeToolRegistry()
        self.context = _FakeAgentContext()
        self.strategies = _FakeStrategyRegistry(with_usage_tracker=with_usage_tracker)


@pytest.fixture()
def fake_agent_loop() -> _FakeAgentLoop:
    return _FakeAgentLoop(with_usage_tracker=True)


@pytest.fixture()
def fake_agent_loop_no_tracker() -> _FakeAgentLoop:
    return _FakeAgentLoop(with_usage_tracker=False)


@pytest.fixture()
def config():
    return load_config()


# ---------------------------------------------------------------------------
# Extended init-bundle tests (real tools/skills/usage/version)
# ---------------------------------------------------------------------------


def test_default_session_info_contains_real_tools(fake_agent_loop, config) -> None:
    """T1.1.a (AC-1): ``info.tools`` carries a real builtin bucket from agent_loop.tools."""
    info = _default_session_info(fake_agent_loop, config)
    assert isinstance(info["tools"], dict), "info.tools must be dict[str, list[str]]"
    assert "builtin" in info["tools"], "info.tools must have a 'builtin' bucket (handoff §3.4 lock)"
    assert len(info["tools"]["builtin"]) >= 1, "builtin tools list must be non-empty"
    # sorted invariant
    assert info["tools"]["builtin"] == sorted(info["tools"]["builtin"]), (
        "tool names within bucket must be sorted"
    )
    # lazy: False on happy path (agent_loop present)
    assert info["lazy"] is False, (
        "lazy=False signals tools/skills are real values (vs placeholder True)"
    )


def test_default_session_info_contains_real_skills(fake_agent_loop, config) -> None:
    """T1.1.b (AC-2): ``info.skills`` groups skills by SkillMeta.source."""
    info = _default_session_info(fake_agent_loop, config)
    assert isinstance(info["skills"], dict), "info.skills must be dict[str, list[str]]"
    # fake fixture has 2 builtin + 1 workspace
    assert "builtin" in info["skills"], "fake fixture should produce 'builtin' source group"
    assert "workspace" in info["skills"], "fake fixture should produce 'workspace' source group"
    assert info["skills"]["builtin"] == sorted(info["skills"]["builtin"]), (
        "skill names within source group must be sorted"
    )
    # ensure each group has at least 1 entry
    for source, names in info["skills"].items():
        assert isinstance(names, list)
        assert len(names) >= 1, f"source group {source!r} has empty list"


def test_default_session_info_contains_real_usage_baseline(fake_agent_loop, config) -> None:
    """T1.1.c (AC-3): ``info.usage`` carries boot baseline (zeros + context_max from config)."""
    info = _default_session_info(fake_agent_loop, config)
    usage = info["usage"]
    assert isinstance(usage, dict)
    # boot-time: no turn run yet
    assert usage["input"] == 0
    assert usage["output"] == 0
    assert usage["cost_usd"] == 0.0
    assert usage["calls"] == 0
    # context_max from config (NOT a hardcoded 200000)
    assert usage["context_max"] == config.agents.defaults.context_window_tokens
    assert usage["context_used"] == 0
    assert usage["context_percent"] == 0


def test_default_session_info_contains_real_version(fake_agent_loop, config) -> None:
    """T1.1.d (AC-4): ``info.version`` reads importlib.metadata, not hardcoded '0.1'."""
    info = _default_session_info(fake_agent_loop, config)
    expected_version = importlib.metadata.version("raven")
    assert info["version"] == expected_version, (
        f"info.version must be importlib.metadata.version('raven') = {expected_version!r}"
    )
    assert info["version"] != "0.1", "the literal '0.1' placeholder must be replaced"


def test_context_window_reads_config_not_hardcoded_200k(fake_agent_loop, config) -> None:
    """``info.context_window`` reads config, not a stub 200000."""
    info = _default_session_info(fake_agent_loop, config)
    assert info["context_window"] == config.agents.defaults.context_window_tokens, (
        "context_window must equal config.agents.defaults.context_window_tokens "
        "(default 65536; the old stub 200000 must be gone)"
    )
    # Sanity check the default is what we expect
    assert config.agents.defaults.context_window_tokens == 65_536, (
        "schema default for context_window_tokens should be 65536 (schema.py:258)"
    )


def test_default_session_info_falls_back_when_agent_loop_none(config) -> None:
    """T1.1.g (AC-7): agent_loop=None graceful fallback per D3 — does not raise."""
    info = _default_session_info(None, config)
    # tools/skills empty (placeholder semantics)
    assert info["tools"] == {}, "tools must fall back to empty dict when agent_loop is None"
    assert info["skills"] == {}, "skills must fall back to empty dict when agent_loop is None"
    # usage all-zero with context_max from config
    assert info["usage"]["input"] == 0
    assert info["usage"]["output"] == 0
    assert info["usage"]["calls"] == 0
    assert info["usage"]["context_max"] == config.agents.defaults.context_window_tokens
    # version still real (importlib doesn't need agent_loop)
    assert info["version"] == importlib.metadata.version("raven")
    # lazy=True signals UI that tools/skills are placeholder (not "0 reality")
    assert info["lazy"] is True, (
        "lazy=True on agent_loop=None fallback signals UI that tools/skills are placeholder"
    )


def test_default_session_info_falls_back_when_no_usage_tracker(
    fake_agent_loop_no_tracker, config
) -> None:
    """agent_loop present but no UsageTracker registered.

    Config may default-off token_wise. Should return baseline zeros +
    context_max from config (not raise).
    """
    info = _default_session_info(fake_agent_loop_no_tracker, config)
    # tools/skills still real (agent_loop present)
    assert info["tools"] != {}
    assert info["skills"] != {}
    # usage baseline all-zero (tracker absent)
    assert info["usage"]["input"] == 0
    assert info["usage"]["calls"] == 0
    assert info["usage"]["context_max"] == config.agents.defaults.context_window_tokens
    # lazy=False (tools/skills are real, only usage degraded)
    assert info["lazy"] is False


def test_register_session_methods_accepts_factory() -> None:
    """T1.1.h (AC-8): register_session_methods signature has agent_loop_factory keyword."""
    sig = inspect.signature(session_module.register_session_methods)
    assert "agent_loop_factory" in sig.parameters, (
        "register_session_methods must accept agent_loop_factory keyword parameter"
    )
    factory_param = sig.parameters["agent_loop_factory"]
    assert factory_param.default is None, (
        "agent_loop_factory must default to None (backward compat)"
    )


def test_resolve_context_window_helper_removed() -> None:
    """the stub _resolve_context_window helper has been removed."""
    session_py = Path(__file__).parent.parent / "raven" / "tui_rpc" / "methods" / "session.py"
    src = session_py.read_text(encoding="utf-8")
    assert "_resolve_context_window" not in src, (
        "_resolve_context_window stub helper should be removed; "
        "context_window now reads config.agents.defaults.context_window_tokens"
    )
    assert "TODO(rpc-init-bundle-formalize)" not in src, (
        "the TODO comment for _resolve_context_window should be removed alongside the helper"
    )


def test_default_session_info_key_set_matches_expected_v030(fake_agent_loop, config) -> None:
    """wire-shape lock — info dict has exactly the 11 expected keys.

    Anti-drift gate: adding a new field to the init bundle MUST update this
    expected set, forcing an explicit spec amendment, until the dict is
    promoted to an OpenRPC ``SessionInitBundle`` component schema.
    """
    info = _default_session_info(fake_agent_loop, config)
    expected_keys = {
        # backward-compat / existing
        "model",
        "skills",
        "tools",
        "cwd",
        "version",
        "mcp_servers",
        # init bundle
        "model_id",
        "provider",
        "context_window",
        "lazy",
        # extended bundle
        "usage",
    }
    assert set(info) == expected_keys, (
        f"init bundle key set drift: "
        f"unexpected={set(info) - expected_keys}, missing={expected_keys - set(info)}"
    )


# ---------------------------------------------------------------------------
# Init-bundle field tests adapted to the extended signature
# ---------------------------------------------------------------------------


def test_default_session_info_contains_real_model(fake_agent_loop, config) -> None:
    """info carries model_id/provider from config (not placeholder)."""
    info = _default_session_info(fake_agent_loop, config)

    assert info["model_id"] == config.agents.defaults.model
    assert info["provider"] == config.agents.defaults.provider
    # NOTE: context_window assertion moved to test_context_window_reads_config_not_hardcoded_200k
    # NOTE: lazy assertion moved to test_default_session_info_contains_real_tools


def test_default_session_info_backward_compat_model_field(fake_agent_loop, config) -> None:
    """info.model retained and equals info.model_id."""
    info = _default_session_info(fake_agent_loop, config)
    assert "model" in info
    assert info["model"] == info["model_id"]
    assert isinstance(info["model"], str) and info["model"]


def test_placeholder_model_constant_removed() -> None:
    """_PLACEHOLDER_MODEL constant removed."""
    session_py = Path(__file__).parent.parent / "raven" / "tui_rpc" / "methods" / "session.py"
    src = session_py.read_text(encoding="utf-8")
    assert "_PLACEHOLDER_MODEL" not in src
    assert '"claude-sonnet-4-6"' not in src


def test_boot_context_max_uses_live_window_for_openrouter(config, monkeypatch) -> None:
    """For an OpenRouter model LiteLLM lags on, context_max is the live window."""
    monkeypatch.setattr(
        session_module,
        "resolve_context_window",
        lambda model: 163840 if model.startswith("openrouter/") else None,
    )

    loop = _FakeAgentLoop(with_usage_tracker=True)
    loop.model = "openrouter/deepseek/deepseek-v4-pro"

    info = _default_session_info(loop, config)

    assert info["usage"]["context_max"] == 163840
