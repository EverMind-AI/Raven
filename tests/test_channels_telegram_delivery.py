"""Telegram delivery failures do not replay acknowledged message parts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from loguru import logger

pytest.importorskip("telegram")

from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TimedOut
from telegram.request import HTTPXRequest

from raven.channels.adapters.telegram.channel import MAX_MESSAGE_LEN, _markdown_to_html
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


@pytest.mark.parametrize(
    "status,payload",
    [
        pytest.param(413, {}, id="413-status"),
        pytest.param(413, {"description": "Request Entity Too Large"}, id="413-description"),
        pytest.param(413, {"description": "Payload Too Large"}, id="413-payload-description"),
        pytest.param(422, {}, id="422-status"),
    ],
)
async def test_permanent_sdk_http_failure_keeps_notice_later_attachment_and_text(tmp_path, delivery, status, payload):
    channel, hub = delivery
    photo, document, later = (tmp_path / name for name in ("first.png", "oversized.zip", "later.png"))
    for path in (photo, document, later):
        path.write_bytes(b"synthetic attachment")
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=payload))
    async with HTTPXRequest(httpx_kwargs={"transport": transport}) as request:

        async def upload(**kwargs):
            return await request.post("https://example.invalid/upload", request_data=None)

        channel._app.bot.send_document.side_effect = upload
        try:
            await hub.post(
                MediaOut(
                    tuple(Media(str(path), "application/octet-stream", "file") for path in (photo, document, later)),
                    source=source(),
                )
            )
            await hub.post(Text("reply after attachments", source=source()))
            await hub.post(Text("next chat", source=source("987654321")))
            await hub.wait_idle("telegram")
            assert channel._app.bot.send_document.await_count == 1
            assert [call.kwargs["photo"].name for call in channel._app.bot.send_photo.await_args_list] == [
                str(photo),
                str(later),
            ]
            assert [call.kwargs["text"] for call in channel._app.bot.send_message.await_args_list] == [
                "[Failed to send: oversized.zip]",
                "reply after attachments",
                "next chat",
            ]
            assert hub.dropped == 0
        finally:
            await hub.aclose()


@pytest.mark.parametrize("message", ["Can't parse entities", "Message is too long", "Unsupported formatting"])
async def test_rejected_html_table_falls_back_to_original_text(delivery, message):
    channel, hub = delivery
    text = "|" + "a" * 200 + "|b|\n|---|---|\n" + "|a|b|\n" * 40
    assert len(text) < MAX_MESSAGE_LEN
    assert len(_markdown_to_html(text)) > 4096
    channel._app.bot.send_message.side_effect = [
        BadRequest(message),
        SimpleNamespace(message_id=327),
        SimpleNamespace(message_id=328),
    ]
    try:
        await hub.post(Text(text, source=source()))
        await hub.post(Text("next reply", source=source()))
        await hub.wait_idle("telegram")
        calls = channel._app.bot.send_message.await_args_list
        assert len(calls) == 3
        assert calls[0].kwargs == {"chat_id": 123456789, "text": _markdown_to_html(text), "parse_mode": "HTML"}
        assert calls[1].kwargs == {"chat_id": 123456789, "text": text}
        assert calls[2].kwargs["text"] == "next reply"
        assert hub.dropped == 0
    finally:
        await hub.aclose()


async def test_transient_plain_text_fallback_resumes_without_replaying_prior_chunk(delivery, monkeypatch):
    channel, hub = delivery
    monkeypatch.setattr("raven.channels.adapters.telegram.channel.MAX_MESSAGE_LEN", 8)
    plain_attempts = 0

    async def send(**kwargs):
        nonlocal plain_attempts
        if kwargs["text"] == "ijklmnop":
            if kwargs.get("parse_mode") == "HTML":
                raise BadRequest("Message is too long")
            plain_attempts += 1
            if plain_attempts == 1:
                raise TimedOut("SECRET-TOKEN")
        return SimpleNamespace(message_id=329)

    channel._app.bot.send_message.side_effect = send
    try:
        await hub.post(Text("abcdefghijklmnopqrstuvwx", source=source()))
        await hub.wait_idle("telegram")
        assert [
            (call.kwargs["text"], call.kwargs.get("parse_mode"))
            for call in channel._app.bot.send_message.await_args_list
        ] == [
            ("abcdefgh", "HTML"),
            ("ijklmnop", "HTML"),
            ("ijklmnop", None),
            ("ijklmnop", "HTML"),
            ("ijklmnop", None),
            ("qrstuvwx", "HTML"),
        ]
        assert hub.dropped == 0
    finally:
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
        NetworkError("Service Unavailable (503)"),
        NetworkError("Too Many Requests (429)"),
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
