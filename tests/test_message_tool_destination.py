"""Explicit message destinations and Telegram transport receipts."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, mock_open, patch

import pytest
from loguru import logger

from raven.agent.tools.message import MessageTool
from raven.channels.intake import Intake
from raven.gateway.spine import build_gateway
from raven.spine.delivery import Capabilities

OWNER = "123456789"


class ToolTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stream = AsyncMock()
        self.tool = MessageTool(self.stream, default_channel="tui", default_chat_id="project")

    async def test_cross_target_without_router_fails_without_stream(self):
        result = await self.tool.execute("milestone", channel="telegram", chat_id=OWNER)
        self.assertTrue(result.startswith("Error:"))
        self.stream.assert_not_awaited()

    async def test_same_target_keeps_stream_and_sent_flag(self):
        self.assertTrue((await self.tool.execute("reply")).startswith("Message sent"))
        self.stream.assert_awaited_once_with("reply", [])
        self.assertTrue(self.tool.sent_in_turn)

    async def test_explicit_destination_reaches_router_without_suppressing_reply(self):
        sender = AsyncMock(return_value="queued receipt")
        self.tool.set_target_sender(sender)
        result = await self.tool.execute("milestone", channel="telegram", chat_id=OWNER, media=["test.pdf"])
        sender.assert_awaited_once_with("telegram", OWNER, "milestone", ["test.pdf"])
        self.assertEqual(result, "queued receipt")
        self.assertFalse(self.tool.sent_in_turn)
        self.stream.assert_not_awaited()

    async def test_partial_or_blank_destination_is_not_guessed(self):
        for args in ({"channel": "telegram"}, {"channel": ""}, {"chat_id": ""}, {"chat_id": 123}):
            with self.subTest(args=args):
                self.assertTrue((await self.tool.execute("test", **args)).startswith("Error:"))
        self.stream.assert_not_awaited()

    async def test_changed_chat_also_requires_explicit_channel(self):
        self.tool.set_context("telegram", "synthetic-current")
        sender = AsyncMock(return_value="queued")
        self.tool.set_target_sender(sender)
        result = await self.tool.execute("test", chat_id=OWNER)
        self.assertTrue(result.startswith("Error:"), result)
        sender.assert_not_awaited()

    async def test_transport_error_is_sanitized(self):
        self.tool.set_target_sender(AsyncMock(side_effect=RuntimeError("SECRET-TOKEN")))
        result = await self.tool.execute("test", channel="telegram", chat_id=OWNER)
        self.assertTrue(result.startswith("Error:"))
        self.assertNotIn("SECRET-TOKEN", result)

    async def test_context_isolation_for_concurrent_turns(self):
        sender = AsyncMock(return_value="queued")
        self.tool.set_target_sender(sender)

        async def one(channel, chat):
            self.tool.set_context(channel, chat)
            self.tool.start_turn()
            await asyncio.sleep(0)
            await self.tool.execute("test", channel="telegram", chat_id=OWNER)
            return self.tool.sent_in_turn

        self.assertEqual(await asyncio.gather(one("tui", "project"), one("telegram", OWNER)), [False, True])
        self.assertEqual(sender.await_count, 1)
        self.assertEqual(self.stream.await_count, 1)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stream = AsyncMock()
        self.tool = MessageTool(self.stream, default_channel="tui", default_chat_id="project")
        config = SimpleNamespace(allow_from=[OWNER])
        self.channel = SimpleNamespace(
            name="telegram",
            config=config,
            is_running=True,
            capabilities=Capabilities(file_attachments=True),
            intake=Intake("telegram", config),
            send=AsyncMock(),
        )
        self.channels = {"telegram": self.channel}
        loop = SimpleNamespace(tools={"message": self.tool})
        self.scheduler, self.hub, _, _, self.close = build_gateway(loop, self.channels, send_max_retries=0)

    async def asyncTearDown(self):
        await self.close()

    async def test_native_hub_outlet_receives_exact_target_and_text(self):
        result = await self.tool.execute("milestone", channel="telegram", chat_id=OWNER)
        self.assertEqual(result, f"Message queued to telegram:{OWNER} (transport delivery pending)")
        await self.hub.wait_idle("telegram")
        self.channel.send.assert_awaited_once_with(OWNER, "milestone")
        self.stream.assert_not_awaited()

    async def test_media_and_text_reach_existing_adapter(self):
        await self.tool.execute("attachment", channel="telegram", chat_id=OWNER, media=["a.pdf"])
        await self.hub.wait_idle("telegram")
        self.assertEqual(self.channel.send.await_args_list[0].args, (OWNER, ""))
        self.assertEqual(self.channel.send.await_args_list[0].kwargs, {"media": ["a.pdf"]})
        self.assertEqual(self.channel.send.await_args_list[1].args, (OWNER, "attachment"))

    async def test_other_destinations_disabled_and_wildcards_fail_closed(self):
        for channel, chat in [("telegram", "123"), ("other", OWNER)]:
            self.assertTrue((await self.tool.execute("test", channel=channel, chat_id=chat)).startswith("Error:"))
        self.channel.is_running = False
        self.assertTrue((await self.tool.execute("test", channel="telegram", chat_id=OWNER)).startswith("Error:"))
        self.channel.is_running = True
        self.channel.config.allow_from = ["*"]
        self.assertTrue((await self.tool.execute("test", channel="telegram", chat_id=OWNER)).startswith("Error:"))
        self.channel.send.assert_not_awaited()

    async def test_retired_outlet_does_not_claim_queue_acceptance(self):
        await self.hub.retire("telegram")
        result = await self.tool.execute("test", channel="telegram", chat_id=OWNER)
        self.assertTrue(result.startswith("Error:"), result)
        self.channel.send.assert_not_awaited()

    async def test_hub_observes_transport_failure_instead_of_false_delivery(self):
        self.channel.send.side_effect = RuntimeError("safe transport failure")
        result = await self.tool.execute("test", channel="telegram", chat_id=OWNER)
        await self.hub.wait_idle("telegram")
        self.assertIn("queued", result)
        self.assertEqual(self.hub.dropped, 1)


class TelegramTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self):
        pytest.importorskip("telegram")
        from raven.channels.adapters.telegram.channel import TelegramChannel

        ch = object.__new__(TelegramChannel)
        ch._typing = {}
        ch._app = SimpleNamespace(
            bot=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=123)))
        )
        return ch

    async def test_retryable_send_failure_raises_without_secret_in_message(self):
        ch = self.adapter()
        from telegram.error import TimedOut

        ch._app.bot.send_message.side_effect = TimedOut("SECRET-TOKEN")
        with self.assertRaisesRegex(RuntimeError, r"^Telegram text delivery failed \(TimedOut\)$"):
            await ch.send(OWNER, "test")
        self.assertEqual(ch._app.bot.send_message.await_count, 1)

    async def test_html_fallback_succeeds(self):
        ch = self.adapter()
        from telegram.error import BadRequest

        ch._app.bot.send_message.side_effect = [BadRequest("Can't parse entities"), SimpleNamespace(message_id=124)]
        await ch._send_text(123456789, "test")
        self.assertEqual(ch._app.bot.send_message.await_count, 2)

    async def test_stopped_or_malformed_target_is_not_retried(self):
        ch = self.adapter()
        await ch.send("bad", "test")
        ch._app = None
        await ch.send(OWNER, "test")

    async def test_successful_text_has_transport_receipt(self):
        ch = self.adapter()
        await ch._send_text(123456789, "test")
        ch._app.bot.send_message.assert_awaited_once()

    async def test_successful_media_logs_exact_receipts(self):
        ch = self.adapter()
        for method in ["send_photo", "send_voice", "send_audio", "send_document"]:
            setattr(ch._app.bot, method, AsyncMock(return_value=SimpleNamespace(message_id=601)))
        for kind in ["photo", "voice", "audio", "document"]:
            with self.subTest(kind=kind):
                logs = []
                sink = logger.add(lambda msg: logs.append(str(msg)))
                try:
                    with (
                        patch("builtins.open", mock_open(read_data=b"test")),
                        patch("raven.channels.adapters.telegram.channel._outbound_kind", return_value=kind),
                    ):
                        await ch._send_media(123456789, "synthetic-file")
                finally:
                    logger.remove(sink)
                self.assertTrue(any("chat_id=123456789 message_id=601" in line for line in logs), logs)


if __name__ == "__main__":
    unittest.main(verbosity=2)
