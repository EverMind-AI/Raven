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
import threading
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
def config(tmp_path):
    """Defaults only -- a real ``~/.raven/config.json`` must never leak in here."""
    return load_config(tmp_path / "does_not_exist.json")


# ---------------------------------------------------------------------------
# Extended init-bundle tests (real tools/skills/usage/version)
# ---------------------------------------------------------------------------


async def test_default_session_info_contains_real_tools(fake_agent_loop, config) -> None:
    """T1.1.a (AC-1): ``info.tools`` carries a real builtin bucket from agent_loop.tools."""
    info = await _default_session_info(fake_agent_loop, config)
    assert isinstance(info["tools"], dict), "info.tools must be dict[str, list[str]]"
    assert "builtin" in info["tools"], "info.tools must have a 'builtin' bucket (handoff §3.4 lock)"
    assert len(info["tools"]["builtin"]) >= 1, "builtin tools list must be non-empty"
    # sorted invariant
    assert info["tools"]["builtin"] == sorted(info["tools"]["builtin"]), "tool names within bucket must be sorted"
    # lazy: False on happy path (agent_loop present)
    assert info["lazy"] is False, "lazy=False signals tools/skills are real values (vs placeholder True)"


async def test_default_session_info_contains_real_skills(fake_agent_loop, config) -> None:
    """T1.1.b (AC-2): ``info.skills`` groups skills by SkillMeta.source."""
    info = await _default_session_info(fake_agent_loop, config)
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


async def test_default_session_info_contains_real_usage_baseline(fake_agent_loop, config) -> None:
    """T1.1.c (AC-3): ``info.usage`` carries boot baseline (zeros + context_max)."""
    info = await _default_session_info(fake_agent_loop, config)
    usage = info["usage"]
    assert isinstance(usage, dict)
    # boot-time: no turn run yet
    assert usage["input"] == 0
    assert usage["output"] == 0
    assert usage["cost_usd"] == 0.0
    assert usage["calls"] == 0
    # no configured pin and fake_agent_loop carries no .model -- the UI empty state
    assert usage["context_max"] == 0
    assert usage["context_used"] == 0
    assert usage["context_percent"] == 0


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("openai-codex/gpt-5.6-sol", None),
        ("github_copilot/gpt-4o", None),
        ("anthropic/claude-sonnet-4-5", 0.0),
    ],
)
async def test_the_boot_banner_does_not_open_a_subscription_at_zero(
    model: str,
    expected: float | None,
    fake_agent_loop,
    config,
) -> None:
    """Zero is a price, and on a plan there is no price to report.

    Every other counter here is genuinely zero before the first turn. This one was
    reporting $0.00 for a session whose every turn will answer "no per-token
    price" -- the same "a subscription reads as free" removed from the estimate.
    """
    fake_agent_loop.model = model

    usage = (await _default_session_info(fake_agent_loop, config))["usage"]

    assert usage["cost_usd"] == expected


async def test_default_session_info_contains_real_version(fake_agent_loop, config) -> None:
    """T1.1.d (AC-4): ``info.version`` reads importlib.metadata, not hardcoded '0.1'."""
    info = await _default_session_info(fake_agent_loop, config)
    expected_version = importlib.metadata.version("raven")
    assert info["version"] == expected_version, (
        f"info.version must be importlib.metadata.version('raven') = {expected_version!r}"
    )
    assert info["version"] != "0.1", "the literal '0.1' placeholder must be replaced"


async def test_context_window_reads_config_not_hardcoded_200k(fake_agent_loop, config) -> None:
    """``info.context_window`` mirrors ``info.usage.context_max``, not a stub 200000."""
    info = await _default_session_info(fake_agent_loop, config)
    assert info["context_window"] == info["usage"]["context_max"]
    assert info["context_window"] != 200_000, "the old stub 200000 must be gone"
    # Sanity check the default is what we expect: None means "figure it out".
    assert config.agents.defaults.context_window_tokens is None, (
        "schema default for context_window_tokens should be None (schema.py:context_window_tokens)"
    )


async def test_default_session_info_falls_back_when_agent_loop_none(config) -> None:
    """T1.1.g (AC-7): agent_loop=None graceful fallback per D3 — does not raise."""
    info = await _default_session_info(None, config)
    # tools/skills empty (placeholder semantics)
    assert info["tools"] == {}, "tools must fall back to empty dict when agent_loop is None"
    assert info["skills"] == {}, "skills must fall back to empty dict when agent_loop is None"
    # usage all-zero with context_max from config
    assert info["usage"]["input"] == 0
    assert info["usage"]["output"] == 0
    assert info["usage"]["calls"] == 0
    assert info["usage"]["context_max"] == 0
    # version still real (importlib doesn't need agent_loop)
    assert info["version"] == importlib.metadata.version("raven")
    # lazy=True signals UI that tools/skills are placeholder (not "0 reality")
    assert info["lazy"] is True, "lazy=True on agent_loop=None fallback signals UI that tools/skills are placeholder"


async def test_default_session_info_falls_back_when_no_usage_tracker(fake_agent_loop_no_tracker, config) -> None:
    """agent_loop present but no UsageTracker registered.

    Config may default-off token_wise. Should return baseline zeros +
    context_max from config (not raise).
    """
    info = await _default_session_info(fake_agent_loop_no_tracker, config)
    # tools/skills still real (agent_loop present)
    assert info["tools"] != {}
    assert info["skills"] != {}
    # usage baseline all-zero (tracker absent)
    assert info["usage"]["input"] == 0
    assert info["usage"]["calls"] == 0
    assert info["usage"]["context_max"] == 0
    # lazy=False (tools/skills are real, only usage degraded)
    assert info["lazy"] is False


def test_register_session_methods_accepts_factory() -> None:
    """T1.1.h (AC-8): register_session_methods signature has agent_loop_factory keyword."""
    sig = inspect.signature(session_module.register_session_methods)
    assert "agent_loop_factory" in sig.parameters, (
        "register_session_methods must accept agent_loop_factory keyword parameter"
    )
    factory_param = sig.parameters["agent_loop_factory"]
    assert factory_param.default is None, "agent_loop_factory must default to None (backward compat)"


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


async def test_default_session_info_key_set_matches_expected_v030(fake_agent_loop, config) -> None:
    """wire-shape lock — info dict has exactly the 12 expected keys.

    Anti-drift gate: adding a new field to the init bundle MUST update this
    expected set, forcing an explicit spec amendment, until the dict is
    promoted to an OpenRPC ``SessionInitBundle`` component schema.
    """
    info = await _default_session_info(fake_agent_loop, config)
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
        "endpoint",
    }
    assert set(info) == expected_keys, (
        f"init bundle key set drift: unexpected={set(info) - expected_keys}, missing={expected_keys - set(info)}"
    )


# ---------------------------------------------------------------------------
# Init-bundle field tests adapted to the extended signature
# ---------------------------------------------------------------------------


async def test_default_session_info_contains_real_model(fake_agent_loop, config) -> None:
    """info carries model_id/provider from config (not placeholder)."""
    info = await _default_session_info(fake_agent_loop, config)

    assert info["model_id"] == config.agents.defaults.model
    assert info["provider"] == config.agents.defaults.provider
    # NOTE: context_window assertion moved to test_context_window_reads_config_not_hardcoded_200k
    # NOTE: lazy assertion moved to test_default_session_info_contains_real_tools


async def test_default_session_info_backward_compat_model_field(fake_agent_loop, config) -> None:
    """info.model retained and equals info.model_id."""
    info = await _default_session_info(fake_agent_loop, config)
    assert "model" in info
    assert info["model"] == info["model_id"]
    assert isinstance(info["model"], str) and info["model"]


def test_placeholder_model_constant_removed() -> None:
    """_PLACEHOLDER_MODEL constant removed."""
    session_py = Path(__file__).parent.parent / "raven" / "tui_rpc" / "methods" / "session.py"
    src = session_py.read_text(encoding="utf-8")
    assert "_PLACEHOLDER_MODEL" not in src
    assert '"claude-sonnet-4-6"' not in src


async def test_baseline_usage_resolves_the_window_off_the_event_loop_thread(config, monkeypatch) -> None:
    """SF10: ``resolve_context_window`` defaults to ``allow_fetch=True``, so a
    cold OpenRouter model with both caches expired can reach for a
    synchronous ~10s HTTP call. ``_baseline_usage`` runs on the event loop
    (an RPC handler), so that call must run on a worker thread, not inline."""
    seen: dict[str, threading.Thread] = {}

    def fake_resolve(model: str) -> int:
        seen["thread"] = threading.current_thread()
        return 99_999

    monkeypatch.setattr(session_module, "resolve_context_window", fake_resolve)

    loop = _FakeAgentLoop(with_usage_tracker=True)
    loop.model = "openrouter/deepseek/deepseek-v4-pro"

    usage = await session_module._baseline_usage(loop, config)

    assert usage["context_max"] == 99_999
    assert seen["thread"] is not threading.current_thread()


async def test_boot_context_max_uses_live_window_for_openrouter(config, monkeypatch) -> None:
    """For an OpenRouter model LiteLLM lags on, context_max is the live window."""
    monkeypatch.setattr(
        session_module,
        "resolve_context_window",
        lambda model: 163840 if model.startswith("openrouter/") else None,
    )

    loop = _FakeAgentLoop(with_usage_tracker=True)
    loop.model = "openrouter/deepseek/deepseek-v4-pro"

    info = await _default_session_info(loop, config)

    assert info["usage"]["context_max"] == 163840


async def test_boot_context_max_pinned_config_wins_over_live_window(config, monkeypatch) -> None:
    """A pinned ``context_window_tokens`` answers even when the live window disagrees."""
    config.agents.defaults.context_window_tokens = 8192
    monkeypatch.setattr(session_module, "resolve_context_window", lambda model: 163840)

    loop = _FakeAgentLoop(with_usage_tracker=True)
    loop.model = "openrouter/deepseek/deepseek-v4-pro"

    info = await _default_session_info(loop, config)

    assert info["usage"]["context_max"] == 8192


# ---------------------------------------------------------------------------
# Upgrade-nudge fields (the producer side; the TUI already reads them)
# ---------------------------------------------------------------------------


async def test_default_session_info_carries_the_upgrade_nudge(fake_agent_loop, config, monkeypatch) -> None:
    """A pending release surfaces as ``update_available`` / ``update_command``.

    The TUI status bar reads both fields, so leaving them unpopulated is the
    exact defect the nudge feature fixed -- and nothing else in the suite fails
    if this wiring is removed.
    """
    monkeypatch.setattr(session_module, "update_notice", lambda _v: (True, "raven upgrade"))

    info = await _default_session_info(fake_agent_loop, config)

    assert info["update_available"] is True
    assert info["update_command"] == "raven upgrade"


async def test_default_session_info_omits_the_nudge_when_up_to_date(fake_agent_loop, config, monkeypatch) -> None:
    """No pending release means the keys stay absent, not present-and-false."""
    monkeypatch.setattr(session_module, "update_notice", lambda _v: None)

    info = await _default_session_info(fake_agent_loop, config)

    assert "update_available" not in info
    assert "update_command" not in info


# ---------------------------------------------------------------------------
# Which endpoint the session is on (multi-endpoint providers only)
# ---------------------------------------------------------------------------


def _rotor(labels: list[str], strategy: str = "sticky"):
    from raven.providers.base import LLMProvider
    from raven.providers.endpoint_rotor import EndpointRotorProvider
    from raven.providers.endpoints import ResolvedEndpoint

    class _Inner(LLMProvider):
        async def chat(self, messages, tools=None, model=None, **kwargs):  # pragma: no cover - never called
            raise AssertionError("the banner must not send a request")

        def get_default_model(self) -> str:
            return "m"

    return EndpointRotorProvider(
        endpoints=[ResolvedEndpoint(label=label, api_key="k", api_base=None, extra_headers=None) for label in labels],
        make_inner=lambda _e: _Inner(api_key="test"),
        default_model="m",
        strategy=strategy,
    )


async def test_default_session_info_names_the_endpoint_in_use(config) -> None:
    """A rotor serves several accounts, so which one is answering is a fact the
    banner has to carry -- naming only the provider makes them indistinguishable."""
    loop = _FakeAgentLoop(with_usage_tracker=True)
    loop.provider = _rotor(["eu", "us"])

    assert (await _default_session_info(loop, config))["endpoint"] == "eu"


async def test_default_session_info_endpoint_is_none_for_a_single_endpoint_provider(fake_agent_loop, config) -> None:
    """Every provider but the rotor is reached at one address with no label, so
    the field is present-and-null rather than a borrowed name."""
    assert (await _default_session_info(fake_agent_loop, config))["endpoint"] is None


async def test_reading_the_banner_endpoint_does_not_rotate(config) -> None:
    """Under round_robin the order cursor advances per request. Building the
    banner is not a request, and a getter that moved it would skip an endpoint
    every time the panel was rendered."""
    loop = _FakeAgentLoop(with_usage_tracker=True)
    loop.provider = _rotor(["eu", "us"], strategy="round_robin")

    assert [(await _default_session_info(loop, config))["endpoint"] for _ in range(3)] == ["eu", "eu", "eu"]
