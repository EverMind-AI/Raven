"""Tests for raven.channels.manager.ChannelManager — spec-based init
(incl. the missing-dependency / ImportError path), allow_from validation, and
status accessors. Outbound delivery moved to the spine outlets (no longer the
manager's job)."""

from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

import pytest

from raven.channels.contract import Capabilities, ChannelSpec
from raven.channels.manager import ChannelManager, _missing_dep_hint


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


# ── _missing_dep_hint (install-mode / OS split) ───────────────────────

_EDITABLE_JSON = '{"url": "file:///src", "dir_info": {"editable": true}}'
_WHEEL_JSON = '{"url": "https://x/raven-0.1.2.whl", "archive_info": {}}'


def _patch_direct_url(monkeypatch, read_text_result):
    class _Dist:
        def read_text(self, name):
            return read_text_result

    monkeypatch.setattr("raven.channels.manager.distribution", lambda pkg: _Dist())


@pytest.mark.parametrize("modname", ["feishu", "weixin", "qq", "telegram", "dingtalk"])
def test_hint_editable_names_the_channel_extra(monkeypatch, modname):
    """Editable checkout -> `uv sync --extra channel-<name>`, name interpolated."""
    _patch_direct_url(monkeypatch, _EDITABLE_JSON)
    assert _missing_dep_hint(modname) == f"Run: uv sync --extra channel-{modname}"


@pytest.mark.parametrize(
    "raw",
    [
        _WHEEL_JSON,  # archive_info: no 'dir_info' key -> .get chain must not KeyError
        None,  # direct_url.json absent -> read_text returns None
        '{"url": "file:///x", "dir_info": {}}',  # dir_info present, 'editable' missing
        "{}",  # empty object
        "{not valid json",  # corrupt file -> JSONDecodeError must be swallowed
    ],
    ids=["wheel", "absent", "dir_info_no_editable", "empty", "malformed"],
)
def test_hint_non_editable_points_to_installer(monkeypatch, raw):
    """Any non-editable / malformed direct_url.json -> installer hint, never raises."""
    _patch_direct_url(monkeypatch, raw)
    monkeypatch.setattr("raven.channels.manager.sys.platform", "linux")
    hint = _missing_dep_hint("weixin")
    assert "uv sync" not in hint
    assert "install.sh" in hint


def test_hint_package_not_found_points_to_installer(monkeypatch):
    """raven distribution not found -> installer hint, no exception."""

    def _raise(pkg):
        raise PackageNotFoundError(pkg)

    monkeypatch.setattr("raven.channels.manager.distribution", _raise)
    monkeypatch.setattr("raven.channels.manager.sys.platform", "darwin")
    assert "install.sh" in _missing_dep_hint("qq")


@pytest.mark.parametrize(
    "platform, marker",
    [("win32", "raw.githubusercontent.com"), ("darwin", "install.sh"), ("linux", "install.sh")],
)
def test_hint_installer_matches_os(monkeypatch, platform, marker):
    """Wheel install picks the installer for the running OS (irm vs curl)."""
    _patch_direct_url(monkeypatch, _WHEEL_JSON)
    monkeypatch.setattr("raven.channels.manager.sys.platform", platform)
    assert marker in _missing_dep_hint("slack")


@pytest.mark.parametrize(
    "direct_url, platform, expected",
    [
        (_EDITABLE_JSON, "linux", "uv sync --extra channel-feishu"),
        (_WHEEL_JSON, "linux", "install.sh"),
        (_WHEEL_JSON, "win32", "raw.githubusercontent.com"),
    ],
    ids=["editable", "wheel-unix", "wheel-win"],
)
def test_init_warning_carries_install_hint(monkeypatch, direct_url, platform, expected):
    """A channel disabled by ImportError logs the mode-correct install hint."""
    from loguru import logger

    _patch_direct_url(monkeypatch, direct_url)
    monkeypatch.setattr("raven.channels.manager.sys.platform", platform)

    def boom(config):
        raise ImportError("No module named 'lark_oapi'")

    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(str(m)), level="WARNING")
    try:
        _manager(
            monkeypatch,
            {"feishu": _spec(boom)},
            _config({"feishu": SimpleNamespace(enabled=True, allow_from=["*"])}),
        )
    finally:
        logger.remove(sink_id)

    warning = "".join(lines)
    assert "feishu channel disabled" in warning
    assert expected in warning


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
