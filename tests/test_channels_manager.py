"""Tests for raven.channels.manager.ChannelManager — spec-based init
(incl. the missing-dependency / ImportError path), allow_from validation, and
status accessors. Outbound delivery moved to the spine outlets (no longer the
manager's job)."""

from importlib.metadata import PackageNotFoundError
from types import SimpleNamespace

import pytest

from raven.channels.contract import Capabilities, ChannelSpec
from raven.channels.manager import ChannelManager, _missing_dep_hint
from raven.config.schema import ProvidersConfig


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
        providers=ProvidersConfig.model_validate({"groq": {"apiKey": "gk"}}),
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


def test_hint_editable_syncs_the_umbrella_extra_inexactly(monkeypatch):
    """Editable checkout -> the umbrella extra, and --inexact so syncing one
    channel's SDK in does not uninstall every other channel's."""
    _patch_direct_url(monkeypatch, _EDITABLE_JSON)
    hint = _missing_dep_hint()
    assert hint == "Run: uv sync --inexact --extra channels"
    assert "--extra channel-" not in hint


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
    hint = _missing_dep_hint()
    assert "uv sync" not in hint
    assert "install.sh" in hint


def test_hint_package_not_found_points_to_installer(monkeypatch):
    """raven distribution not found -> installer hint, no exception."""

    def _raise(pkg):
        raise PackageNotFoundError(pkg)

    monkeypatch.setattr("raven.channels.manager.distribution", _raise)
    monkeypatch.setattr("raven.channels.manager.sys.platform", "darwin")
    assert "install.sh" in _missing_dep_hint()


@pytest.mark.parametrize(
    "platform, marker",
    [("win32", "raw.githubusercontent.com"), ("darwin", "install.sh"), ("linux", "install.sh")],
)
def test_hint_installer_matches_os(monkeypatch, platform, marker):
    """Wheel install picks the installer for the running OS (irm vs curl)."""
    _patch_direct_url(monkeypatch, _WHEEL_JSON)
    monkeypatch.setattr("raven.channels.manager.sys.platform", platform)
    assert marker in _missing_dep_hint()


@pytest.mark.parametrize(
    "direct_url, platform, expected",
    [
        (_EDITABLE_JSON, "linux", "uv sync --inexact --extra channels"),
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


# ── missing_dependency_channels (read-only probe for status / doctor) ──


def test_missing_dependency_channels_reports_only_enabled_import_failures(monkeypatch):
    """An enabled channel whose SDK is absent is reported; a disabled one is not,
    and neither is a channel that fails to build for some other reason -- that is
    a different diagnosis than "install the dependency"."""
    from raven.channels.manager import missing_dependency_channels

    def no_sdk(config):
        raise ImportError("No module named 'telegram'")

    def other_failure(config):
        raise ValueError("bad token")

    monkeypatch.setattr(
        "raven.channels.registry.discover_specs",
        lambda: {
            "telegram": _spec(no_sdk),
            "discord": _spec(no_sdk),
            "slack": _spec(other_failure),
        },
    )
    config = _config(
        {
            "telegram": SimpleNamespace(enabled=True, allow_from=["*"]),
            "discord": SimpleNamespace(enabled=False, allow_from=["*"]),
            "slack": SimpleNamespace(enabled=True, allow_from=["*"]),
        }
    )

    assert missing_dependency_channels(config) == ["telegram"]


# ── start_one / stop_one (a channel enabled while the gateway runs) ────


def _hot(monkeypatch, specs, launch_config, disk_config):
    """A manager launched with ``launch_config`` while config on disk now says
    ``disk_config`` -- the shape a hot start actually happens in: the switch was
    written by another process after this gateway booted."""
    mgr = _manager(monkeypatch, specs, launch_config)
    monkeypatch.setattr("raven.config.loader.load_config", lambda: disk_config)
    return mgr


@pytest.mark.asyncio
async def test_start_one_builds_a_channel_enabled_after_launch(monkeypatch):
    """The whole point: the flag was written by the page's own process, so the
    section has to be re-read from disk. Reading self.config would find the
    channel still off and refuse to start it."""
    started = []
    mgr = _hot(
        monkeypatch,
        {"fake": _spec(_FakeChannel)},
        _config({"fake": SimpleNamespace(enabled=False, allow_from=["*"])}),
        _config({"fake": SimpleNamespace(enabled=True, allow_from=["*"])}),
    )
    assert mgr.enabled_channels == []
    mgr.on_started = started.append

    retired: list[str] = []

    async def note(name: str) -> None:
        retired.append(name)

    mgr.on_stopped = note

    assert await mgr.start_one("fake") == "started"
    assert mgr.enabled_channels == ["fake"]
    # The outlet hook fires with the new channel: without it the channel
    # receives and every reply to it is dropped by the hub.
    assert started == [mgr.channels["fake"]]
    assert mgr.channels["fake"].transcription_api_key == "gk"
    assert await mgr.stop_one("fake") == "stopped"
    assert mgr.enabled_channels == []
    # And the outlet is retired with it. The hub's worker is resident and holds
    # the adapter it started with, so a stop that left the outlet registered
    # meant the next start received on the new adapter and replied through the
    # stopped one.
    assert retired == ["fake"]


@pytest.mark.asyncio
async def test_start_one_answers_rather_than_raising_for_every_refusal(monkeypatch):
    """Each of these is a state the caller draws. An empty allowFrom is fatal at
    start-up (SystemExit); a live gateway must answer instead of dying."""

    def no_sdk(config):
        raise ImportError("No module named 'telegram'")

    mgr = _hot(
        monkeypatch,
        {"fake": _spec(_FakeChannel), "telegram": _spec(no_sdk)},
        _config({"fake": SimpleNamespace(enabled=False, allow_from=["*"])}),
        _config(
            {
                "fake": SimpleNamespace(enabled=False, allow_from=["*"]),
                "telegram": SimpleNamespace(enabled=True, allow_from=["*"]),
                "deny": SimpleNamespace(enabled=True, allow_from=[]),
            }
        ),
    )
    assert await mgr.start_one("fake") == "disabled"
    assert await mgr.start_one("nope") == "unknown"
    assert await mgr.start_one("telegram") == "missing_dep"
    monkeypatch.setattr(
        "raven.channels.registry.discover_specs",
        lambda: {"deny": _spec(_FakeChannel)},
    )
    assert await mgr.start_one("deny") == "deny_all"
    assert mgr.enabled_channels == []
    assert await mgr.stop_one("fake") == "absent"


@pytest.mark.asyncio
async def test_start_one_is_idempotent(monkeypatch):
    """The page writes the flag and asks to start on every apply, including one
    that only corrected a credential."""
    mgr = _hot(
        monkeypatch,
        {"fake": _spec(_FakeChannel)},
        _config({"fake": SimpleNamespace(enabled=True, allow_from=["*"])}),
        _config({"fake": SimpleNamespace(enabled=True, allow_from=["*"])}),
    )
    first = mgr.channels["fake"]
    assert await mgr.start_one("fake") == "already"
    assert mgr.channels["fake"] is first
