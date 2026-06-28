"""Tests for the extracted channel services: Intake (inbound gate + spine
submit) and transcribe_audio."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from raven.channels.intake import Intake
from raven.channels.transcribe import transcribe_audio


# ── Intake: permission ────────────────────────────────────────────────


def test_intake_is_allowed():
    assert Intake("tg", SimpleNamespace(allow_from=["*"])).is_allowed("u1") is True
    assert Intake("tg", SimpleNamespace(allow_from=["u1"])).is_allowed("u1") is True
    assert Intake("tg", SimpleNamespace(allow_from=["u9"])).is_allowed("u1") is False
    assert Intake("tg", SimpleNamespace(allow_from=[])).is_allowed("u1") is False  # deny-by-default


def test_intake_custom_allow_check_overrides():
    # bespoke policy (e.g. telegram's <id>|<username>) takes over the default
    intake = Intake("tg", SimpleNamespace(allow_from=[]), allow_check=lambda s: s == "ok")
    assert intake.is_allowed("ok") is True
    assert intake.is_allowed("nope") is False


def test_intake_custom_allow_check_gates_publish():
    submit = AsyncMock()
    intake = Intake("tg", SimpleNamespace(allow_from=["*"]), allow_check=lambda s: False)
    intake.set_submit(submit)
    asyncio.run(intake.publish(sender_id="u", chat_id="c", content="x"))
    submit.assert_not_awaited()   # custom check wins even over allow_from=["*"]


# ── Intake: spine submit path ─────────────────────────────────────────


def test_intake_submit_path_builds_turnrequest():
    from raven.spine import ChatType, Origin

    submit = AsyncMock()
    intake = Intake("tg", SimpleNamespace(allow_from=["*"]))
    intake.set_submit(submit)
    asyncio.run(intake.publish(
        sender_id=123, chat_id=456, content="hi",
        media=["/m.jpg"], metadata={"chat_type": "group", "message_id": "9"}, session_key="s1",
    ))
    submit.assert_awaited_once()
    req = submit.await_args.args[0]
    assert req.origin is Origin.USER
    assert (req.source.channel, req.source.chat_id, req.source.sender_id) == ("tg", "456", "123")
    assert req.source.chat_type is ChatType.GROUP  # mapped from metadata["chat_type"]
    assert req.source.extras == {"chat_type": "group", "message_id": "9"}  # metadata rides extras
    assert req.text == "hi"
    assert [m.path for m in req.media] == ["/m.jpg"]
    assert req.conversation == "s1"  # session_key_override -> conversation


def test_intake_submit_path_dm_default_and_no_conversation():
    from raven.spine import ChatType

    submit = AsyncMock()
    intake = Intake("tg", SimpleNamespace(allow_from=["*"]))
    intake.set_submit(submit)
    asyncio.run(intake.publish(sender_id="u", chat_id="c", content="x"))
    req = submit.await_args.args[0]
    assert req.source.chat_type is ChatType.DM  # no chat_type metadata -> DM
    assert req.conversation is None  # no session_key
    assert req.source.extras == {}


def test_intake_submit_path_denied_does_not_submit():
    submit = AsyncMock()
    intake = Intake("tg", SimpleNamespace(allow_from=[]))  # deny all
    intake.set_submit(submit)
    asyncio.run(intake.publish(sender_id="u", chat_id="c", content="x"))
    submit.assert_not_awaited()  # gate runs before submit (inbound-gate-first)


def test_intake_no_submit_wired_drops(monkeypatch):
    # No spine dispatch wired -> permitted message is dropped (logged), not raised.
    intake = Intake("tg", SimpleNamespace(allow_from=["*"]))
    asyncio.run(intake.publish(sender_id="u", chat_id="c", content="x"))  # must not raise


# ── transcribe_audio ──────────────────────────────────────────────────


def test_transcribe_audio_delegates(monkeypatch):
    class _FakeProvider:
        def __init__(self, api_key=None): pass
        async def transcribe(self, path): return "hello world"
    monkeypatch.setattr("raven.providers.transcription.GroqTranscriptionProvider", _FakeProvider)
    assert asyncio.run(transcribe_audio("/a.ogg", api_key="k")) == "hello world"


def test_transcribe_audio_swallows_errors(monkeypatch):
    class _Boom:
        def __init__(self, api_key=None): raise RuntimeError("nope")
    monkeypatch.setattr("raven.providers.transcription.GroqTranscriptionProvider", _Boom)
    assert asyncio.run(transcribe_audio("/a.ogg")) == ""   # failure -> empty string


def test_transcribe_audio_empty_key_becomes_none(monkeypatch):
    seen = {}
    class _Rec:
        def __init__(self, api_key=None): seen["key"] = api_key
        async def transcribe(self, path): return ""
    monkeypatch.setattr("raven.providers.transcription.GroqTranscriptionProvider", _Rec)
    asyncio.run(transcribe_audio("/a.ogg", api_key=""))    # empty -> None (provider env fallback)
    assert seen["key"] is None
    asyncio.run(transcribe_audio("/a.ogg", api_key="k"))
    assert seen["key"] == "k"
