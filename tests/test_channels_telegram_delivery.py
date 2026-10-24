"""Telegram delivery failures do not replay acknowledged message parts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from loguru import logger

pytest.importorskip("telegram")

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut

from raven.gateway.outlet import ChannelOutletAdapter
from raven.spine import ChatType, MediaOut, Source, Text
from raven.spine.delivery import DeliveryHub
from raven.spine.message import Media
from tests.test_channels_telegram import _running_channel


@pytest.fixture
async def delivery(monkeypatch, request):
    monkeypatch.setattr("raven.spine.delivery._RETRY_BASE_DELAY", 0)
    channel = _running_channel()
    channel._app.bot.send_message_draft.side_effect = RuntimeError("drafts unavailable")
    receipt = SimpleNamespace(message_id=321)
    for method in ("send_message", "send_photo", "send_voice", "send_audio", "send_document"):
        getattr(channel._app.bot, method).return_value = receipt
    hub = DeliveryHub(send_max_retries=getattr(request, "param", 3))
    hub.register(ChannelOutletAdapter(channel))
    return channel, hub


def source(chat="123456789"):
    return Source("telegram", chat, chat, ChatType.DM)


@pytest.mark.parametrize("failure", ["missing", "forbidden", "bad-request"])
async def test_permanent_media_failure_keeps_prior_photo_notice_and_following_chats(tmp_path, delivery, failure):
    channel, hub = delivery
    photo, document = tmp_path / "chart.png", tmp_path / "report.pdf"
    photo.write_bytes(b"synthetic image")
    if failure != "missing":
        document.write_bytes(b"synthetic document")
        error = Forbidden if failure == "forbidden" else BadRequest
        channel._app.bot.send_document.side_effect = error("SECRET-TOKEN")
    logs = []
    sink = logger.add(lambda message: logs.append(str(message)))
    try:
        await hub.post(
            MediaOut(
                (Media(str(photo), "image/png", "image"), Media(str(document), "application/pdf", "file")),
                source=source(),
            )
        )
        await hub.post(Text("here is the chart", source=source()))
        await hub.post(Text("reply for another user", source=source("987654321")))
        await hub.wait_idle("telegram")
        assert channel._app.bot.send_photo.await_count == 1
        texts = [call.kwargs["text"] for call in channel._app.bot.send_message.await_args_list]
        notices = [] if failure == "forbidden" else ["[Failed to send: report.pdf]"]
        assert texts == notices + ["here is the chart", "reply for another user"]
        assert hub.dropped == 0
        assert "SECRET-TOKEN" not in "".join(logs)
        assert str(tmp_path) not in "".join(logs)
    finally:
        logger.remove(sink)
        await hub.aclose()


async def test_forbidden_text_is_not_retried_or_reformatted(delivery):
    channel, hub = delivery

    async def send(**kwargs):
        if kwargs["chat_id"] == 123456789:
            raise Forbidden("SECRET-TOKEN")
        return SimpleNamespace(message_id=322)

    channel._app.bot.send_message.side_effect = send
    logs = []
    sink = logger.add(lambda message: logs.append(str(message)))
    try:
        await hub.post(Text("not started", source=source()))
        await hub.post(Text("other chat", source=source("987654321")))
        await hub.wait_idle("telegram")
        blocked = [
            call for call in channel._app.bot.send_message.await_args_list if call.kwargs["chat_id"] == 123456789
        ]
        assert len(blocked) == 1
        assert channel._app.bot.send_message.await_args_list[-1].kwargs["text"] == "other chat"
        assert "SECRET-TOKEN" not in "".join(logs)
    finally:
        logger.remove(sink)
        await hub.aclose()


async def test_later_transient_media_failure_retries_only_unacknowledged_part(tmp_path, delivery):
    channel, hub = delivery
    photo, document = tmp_path / "chart.png", tmp_path / "report.pdf"
    photo.write_bytes(b"synthetic image")
    document.write_bytes(b"synthetic document")
    channel._app.bot.send_document.side_effect = [TimedOut("SECRET-TOKEN"), SimpleNamespace(message_id=323)]
    try:
        await hub.post(
            MediaOut(
                (Media(str(photo), "image/png", "image"), Media(str(document), "application/pdf", "file")),
                source=source(),
            )
        )
        await hub.post(Text("reply after attachments", source=source()))
        await hub.wait_idle("telegram")
        assert channel._app.bot.send_photo.await_count == 1
        assert channel._app.bot.send_document.await_count == 2
        assert channel._app.bot.send_message.await_args_list[-1].kwargs["text"] == "reply after attachments"
        assert hub.dropped == 0
    finally:
        await hub.aclose()


async def test_later_text_chunk_resumes_without_replaying_acknowledged_chunks(delivery, monkeypatch):
    channel, hub = delivery
    monkeypatch.setattr("raven.channels.adapters.telegram.channel.MAX_MESSAGE_LEN", 8)
    attempts = {}

    async def send(**kwargs):
        text = kwargs["text"]
        attempts[text] = attempts.get(text, 0) + 1
        if text == "ijklmnop" and attempts[text] < 3:
            raise TimedOut("SECRET-TOKEN")
        return SimpleNamespace(message_id=324)

    channel._app.bot.send_message.side_effect = send
    try:
        await hub.post(Text("abcdefghijklmnopqrstuvwx", source=source()))
        await hub.wait_idle("telegram")
        assert attempts == {"abcdefgh": 1, "ijklmnop": 3, "qrstuvwx": 1}
        assert hub.dropped == 0
    finally:
        await hub.aclose()


async def test_failed_notice_resumes_notice_without_replaying_or_reopening_media(tmp_path, delivery):
    channel, hub = delivery
    photo, missing = tmp_path / "chart.png", tmp_path / "missing.pdf"
    photo.write_bytes(b"synthetic image")
    channel._send_media = AsyncMock(wraps=channel._send_media)
    channel._app.bot.send_message.side_effect = [TimedOut("SECRET-TOKEN"), SimpleNamespace(message_id=325)]
    try:
        await hub.post(
            MediaOut(
                (Media(str(photo), "image/png", "image"), Media(str(missing), "application/pdf", "file")),
                source=source(),
            )
        )
        await hub.wait_idle("telegram")
        assert channel._app.bot.send_photo.await_count == 1
        assert channel._send_media.await_count == 2
        assert [call.kwargs["text"] for call in channel._app.bot.send_message.await_args_list] == [
            "[Failed to send: missing.pdf]",
            "[Failed to send: missing.pdf]",
        ]
        assert hub.dropped == 0
    finally:
        await hub.aclose()


@pytest.mark.parametrize(
    "failure",
    [
        NetworkError("SECRET-TOKEN"),
        TimedOut("SECRET-TOKEN"),
        RetryAfter(0),
        ConnectionError("SECRET-TOKEN"),
        TimeoutError("SECRET-TOKEN"),
        httpx.HTTPStatusError(
            "SECRET-TOKEN", request=httpx.Request("POST", "https://example.invalid"), response=httpx.Response(503)
        ),
    ],
)
async def test_transient_text_failures_use_hub_retry_not_plain_text_fallback(delivery, failure):
    channel, hub = delivery
    channel._app.bot.send_message.side_effect = [failure, SimpleNamespace(message_id=326)]
    try:
        await hub.post(Text("retry me", source=source()))
        await hub.wait_idle("telegram")
        assert channel._app.bot.send_message.await_count == 2
        assert all(call.kwargs.get("parse_mode") == "HTML" for call in channel._app.bot.send_message.await_args_list)
        assert hub.dropped == 0
    finally:
        await hub.aclose()


@pytest.mark.parametrize("delivery", [0, 1, 3], indirect=True)
async def test_exhausted_retry_budget_drops_only_remaining_work(tmp_path, delivery):
    channel, hub = delivery
    photo, document = tmp_path / "chart.png", tmp_path / "report.pdf"
    photo.write_bytes(b"synthetic image")
    document.write_bytes(b"synthetic document")
    channel._app.bot.send_document.side_effect = TimedOut("SECRET-TOKEN")
    logs = []
    sink = logger.add(lambda message: logs.append(str(message)))
    try:
        await hub.post(
            MediaOut(
                (Media(str(photo), "image/png", "image"), Media(str(document), "application/pdf", "file")),
                source=source(),
            )
        )
        await hub.post(Text("next chat", source=source("987654321")))
        await hub.wait_idle("telegram")
        assert channel._app.bot.send_photo.await_count == 1
        assert channel._app.bot.send_document.await_count == hub._send_max_retries + 1
        assert channel._app.bot.send_message.await_args.kwargs["text"] == "next chat"
        assert hub.dropped == 1
        assert "SECRET-TOKEN" not in "".join(logs)
    finally:
        logger.remove(sink)
        await hub.aclose()
