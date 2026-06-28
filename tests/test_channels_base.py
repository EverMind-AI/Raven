"""Tests for raven.channels.base.ChannelBase — the thin plumbing base:
init wiring, is_running, and the default deny-by-default allowlist."""

from types import SimpleNamespace

from raven.channels.base import ChannelBase


class _StubChannel(ChannelBase):
    name = "stub"
    display_name = "Stub"

    async def start(self) -> None:  # pragma: no cover - not exercised
        pass

    async def stop(self) -> None:  # pragma: no cover - not exercised
        pass

    async def send(self, chat_id, content, media=None) -> None:  # pragma: no cover
        pass


def _stub(allow_from=None):
    cfg = SimpleNamespace(allow_from=allow_from if allow_from is not None else ["*"])
    return _StubChannel(cfg)


def test_channelbase_init_plumbing():
    ch = _StubChannel(SimpleNamespace(allow_from=["*"]))
    assert ch._running is False and ch.is_running is False
    assert ch.transcription_api_key == ""        # class default, set by manager
    assert ch.intake.channel_name == "stub"       # base wired Intake for this channel


def test_channelbase_is_running_reflects_flag():
    ch = _stub()
    ch._running = True
    assert ch.is_running is True


def test_channelbase_default_is_allowed():
    assert _stub([]).is_allowed("u1") is False        # empty = deny all
    assert _stub(["*"]).is_allowed("u1") is True       # wildcard
    assert _stub(["u1"]).is_allowed("u1") is True
    assert _stub(["u1"]).is_allowed("u2") is False


def test_channelbase_intake_uses_default_is_allowed():
    """The injected Intake must gate on the channel's is_allowed, not its own
    default — pins that base wired allow_check=self.is_allowed."""
    ch = _stub(["u1"])
    assert ch.intake.is_allowed("u1") is True
    assert ch.intake.is_allowed("u2") is False


def test_channelbase_intake_picks_up_overridden_is_allowed():
    """A subclass that overrides is_allowed (e.g. Telegram's id|username form)
    must have the override flow into the injected Intake automatically."""

    class _VipChannel(_StubChannel):
        def is_allowed(self, sender_id: str) -> bool:
            return sender_id == "vip"

    ch = _VipChannel(SimpleNamespace(allow_from=[]))
    assert ch.intake.is_allowed("vip") is True
    assert ch.intake.is_allowed("nobody") is False


def test_channelbase_satisfies_channel_contract():
    from raven.channels import Channel
    from raven.channels.contract import capability_violations
    ch = _stub()
    assert isinstance(ch, Channel)
    assert capability_violations(ch) == []
