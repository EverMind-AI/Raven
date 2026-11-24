"""The three-valued channel state, end to end through the probe."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def isolated_config(tmp_path: Path):
    """A config path of our own, so this reads no developer's real channels."""
    import json

    import raven.home as raven_home_module
    from raven.config.loader import set_config_path

    previous = raven_home_module._current_config_path
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"agents": {"defaults": {"workspace": str(tmp_path / "ws")}}}))
    set_config_path(cfg_path)
    yield cfg_path
    raven_home_module._current_config_path = previous


from raven.rpc.methods import console as console_module


class _Telegram:
    """Runs, but has no idea whether it is 'paired' -- like every non-QR adapter."""

    is_running = True


class _Weixin:
    is_running = True
    pending_qr = None

    @property
    def connected(self) -> bool:
        return True


class _Mgr:
    def __init__(self, mapping):
        self._m = mapping

    def get_channel(self, name):
        return self._m.get(name)


async def _live(mapping):
    from raven.rpc.control import register_control_methods
    from raven.rpc.dispatcher import Dispatcher

    d = Dispatcher()
    register_control_methods(d, channel_manager=_Mgr(mapping))
    return await d.dispatch({"jsonrpc": "2.0", "id": 1, "method": "gateway.channels.live", "params": {}})


async def test_a_channel_that_reports_no_pairing_is_null_not_false() -> None:
    """Collapsing null into false says 'not connected' about a Telegram bot that
    is serving messages -- the same lie as reading the config flag."""
    r = await _live({"telegram": _Telegram(), "weixin": _Weixin()})
    ch = r["result"]["channels"]

    assert ch["telegram"] == {"running": True, "connected": None, "qr_login": False}
    assert ch["weixin"] == {"running": True, "connected": True, "qr_login": True}


async def test_a_channel_the_gateway_never_started_is_not_running() -> None:
    r = await _live({})
    assert r["result"]["channels"]["telegram"] == {"running": False, "connected": None, "qr_login": False}


async def test_status_leaves_the_live_fields_off_when_no_gateway_answers(
    isolated_config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Absent, not false. A probe that cannot reach the gateway knows nothing
    about the channels, and 'not connected' would be a claim it cannot make."""
    from raven.gateway import live_probe

    live_probe.reset_cache()
    monkeypatch.setattr(live_probe, "_endpoint", lambda: None)

    status = await console_module.channels_status({})
    row = next(c for c in status["channels"] if c["name"] == "telegram")

    assert "running" not in row
    assert "connected" not in row


async def test_status_carries_the_live_state_when_the_gateway_answers(
    isolated_config: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.gateway import live_probe

    live_probe.reset_cache()

    async def _fake():
        return {"telegram": {"running": True, "connected": None, "qr_login": False}}

    monkeypatch.setattr(live_probe, "channel_liveness", _fake)

    status = await console_module.channels_status({})
    row = next(c for c in status["channels"] if c["name"] == "telegram")

    assert row["running"] is True
    assert "connected" not in row, "a null pairing must not become a rendered false"


async def test_qr_is_empty_rather_than_an_error_when_no_gateway_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    """To this surface "nothing pending" and "nobody could say" are the same:
    there is no code to show either way, and a distinction here would only add a
    state the dialog has to explain."""
    from raven.gateway import live_probe

    monkeypatch.setattr(live_probe, "_endpoint", lambda: None)

    r = await console_module.channels_qr({"name": "weixin"})

    assert r == {"qr": None, "qr_text": None, "connected": False, "running": False}


async def test_qr_passes_the_gateways_answer_through(monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.gateway import live_probe

    async def _fake(name):
        assert name == "weixin"
        return {"qr": "data:image/png;base64,AAA", "qr_text": None, "connected": False, "running": True}

    monkeypatch.setattr(live_probe, "channel_qr", _fake)

    r = await console_module.channels_qr({"name": "weixin"})

    assert r["qr"] == "data:image/png;base64,AAA"
    assert r["connected"] is False
    assert r["running"] is True


async def test_status_reads_camelcase_extras_written_by_the_web_ui(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Web UI stores channel cargo under camelCase keys (`appId`); a lookup by
    the snake_case spec name alone reported configured credentials as missing."""
    import json

    from raven.gateway import live_probe

    live_probe.reset_cache()
    monkeypatch.setattr(live_probe, "_endpoint", lambda: None)
    isolated_config.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"workspace": str(isolated_config.parent / "ws")}},
                "channels": {
                    "feishu": {
                        "enabled": True,
                        "appId": "cli_x",
                        "appSecret": "s3cret",
                        "encryptKey": "enc",
                    }
                },
            }
        )
    )

    status = await console_module.channels_status({})
    row = next(c for c in status["channels"] if c["name"] == "feishu")
    sets = {f["key"]: f["set"] for f in row["fields"]}

    assert row["missing"] == []
    assert row["configured"] is True
    assert sets["app_id"] is True
    assert sets["app_secret"] is True
    assert sets["encrypt_key"] is True, "an optional camelCase field is set just as a required one is"


async def test_status_reads_snake_case_values_from_the_config(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The camelCase spelling is a fallback, not a replacement: a channel already
    configured under the snake_case spec names must keep reading as configured."""
    import json

    from raven.gateway import live_probe

    live_probe.reset_cache()
    monkeypatch.setattr(live_probe, "_endpoint", lambda: None)
    isolated_config.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"workspace": str(isolated_config.parent / "ws")}},
                "channels": {"feishu": {"enabled": True, "app_id": "cli_x", "app_secret": "s3cret"}},
            }
        )
    )

    status = await console_module.channels_status({})
    row = next(c for c in status["channels"] if c["name"] == "feishu")
    sets = {f["key"]: f["set"] for f in row["fields"]}

    assert row["missing"] == []
    assert row["configured"] is True
    assert sets["app_id"] is True
    assert sets["app_secret"] is True


async def test_snake_case_keeps_priority_when_both_spellings_are_present(
    isolated_config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snake_case value wins over the camelCase fallback when both are written.
    Two filled spellings leave `missing` empty either way, so precedence is read
    through `set`: the snake_case group policy is written at its declared
    default, which reads as not-set only if that is the value the walk reached."""
    import json

    from raven.config.update_channels import channel_field_specs
    from raven.gateway import live_probe

    live_probe.reset_cache()
    monkeypatch.setattr(live_probe, "_endpoint", lambda: None)
    default_policy = channel_field_specs("feishu")["group_policy"]["default"]
    other_policy = "open" if default_policy != "open" else "mention"
    isolated_config.write_text(
        json.dumps(
            {
                "agents": {"defaults": {"workspace": str(isolated_config.parent / "ws")}},
                "channels": {
                    "feishu": {
                        "enabled": True,
                        "app_id": "snake_x",
                        "app_secret": "snake_s",
                        "appId": "camel_x",
                        "appSecret": "camel_s",
                        "group_policy": default_policy,
                        "groupPolicy": other_policy,
                    }
                },
            }
        )
    )

    status = await console_module.channels_status({})
    row = next(c for c in status["channels"] if c["name"] == "feishu")
    sets = {f["key"]: f["set"] for f in row["fields"]}

    assert row["missing"] == [] and row["configured"] is True
    assert sets["app_id"] is True and sets["app_secret"] is True
    assert sets["group_policy"] is False, "the snake_case value at its default was read, not the camelCase one"
