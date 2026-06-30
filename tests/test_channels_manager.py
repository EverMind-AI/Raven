"""Tests for raven.channels.manager.ChannelManager — spec-based init
(incl. the missing-dependency / ImportError path), allow_from validation, and
status accessors. Outbound delivery moved to the spine outlets (no longer the
manager's job)."""

from types import SimpleNamespace

import pytest

from raven.channels.contract import Capabilities, ChannelSpec
from raven.channels.manager import ChannelManager


class _FakeChannel:
    def __init__(self, config):
        self.config = config
        self._running = False
        self.transcription_api_key = ""

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:  # pragma: no cover - not exercised
        self._running = True

    async def stop(self) -> None:  # pragma: no cover - not exercised
        self._running = False

    async def send(self, chat_id, content, media=None) -> None:  # pragma: no cover
        pass


def _spec(factory, display_name="Fake", interactive_login=False) -> ChannelSpec:
    return ChannelSpec(
        display_name=display_name,
        factory=factory,
        capabilities=Capabilities(interactive_login=interactive_login),
    )


def _config(channels=None):
    chan = SimpleNamespace()
    for name, section in (channels or {}).items():
        setattr(chan, name, section)
    return SimpleNamespace(
        providers=SimpleNamespace(groq=SimpleNamespace(api_key="gk")),
        channels=chan,
    )


def _manager(monkeypatch, specs, config) -> ChannelManager:
    monkeypatch.setattr("raven.channels.registry.discover_specs", lambda: specs)
    return ChannelManager(config)


# ── _init_channels ────────────────────────────────────────────────────


def test_init_builds_enabled_channel_and_sets_groq_key(monkeypatch):
    mgr = _manager(
        monkeypatch,
        {"fake": _spec(_FakeChannel)},
        _config({"fake": SimpleNamespace(enabled=True, allow_from=["*"])}),
    )
    assert mgr.enabled_channels == ["fake"]
    assert mgr.channels["fake"].transcription_api_key == "gk"  # set by manager


def test_init_skips_disabled_channel(monkeypatch):
    mgr = _manager(
        monkeypatch,
        {"fake": _spec(_FakeChannel)},
        _config({"fake": SimpleNamespace(enabled=False, allow_from=["*"])}),
    )
    assert mgr.channels == {}


def test_init_disables_channel_on_missing_dependency(monkeypatch):
    """A channel whose factory can't import its SDK is disabled, not fatal."""

    def boom(config):
        raise ImportError("No module named 'botpy'")

    mgr = _manager(
        monkeypatch,
        {"fake": _spec(boom)},
        _config({"fake": SimpleNamespace(enabled=True, allow_from=["*"])}),
    )
    assert "fake" not in mgr.channels  # disabled, construction did not raise


def test_validate_allow_from_rejects_empty(monkeypatch):
    with pytest.raises(SystemExit):
        _manager(
            monkeypatch,
            {"fake": _spec(_FakeChannel)},
            _config({"fake": SimpleNamespace(enabled=True, allow_from=[])}),
        )


# ── status / accessors ────────────────────────────────────────────────


def test_get_status_and_get_channel(monkeypatch):
    mgr = _manager(
        monkeypatch,
        {"fake": _spec(_FakeChannel)},
        _config({"fake": SimpleNamespace(enabled=True, allow_from=["*"])}),
    )
    mgr.channels["fake"]._running = True
    assert mgr.get_status() == {"fake": {"enabled": True, "running": True}}
    assert mgr.get_channel("fake") is mgr.channels["fake"]
    assert mgr.get_channel("nope") is None
