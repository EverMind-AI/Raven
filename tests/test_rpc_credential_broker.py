"""The credential card's host side: what the page is shown, what is written, and how every card ends."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from raven.contracts.asking import CredentialOutcome, CredentialRequest
from raven.rpc.credential_broker import CredentialBroker, CredentialRefusedError
from raven.rpc.methods.credential import credential_pending, credential_skip, credential_submit

pytestmark = pytest.mark.asyncio

REQUEST = CredentialRequest(target="config:tools.web.providers.tavily.apiKey", label="Tavily API key")


class Page:
    """The frames the broker sent, and the one it is waiting on."""

    def __init__(self) -> None:
        self.frames: list[dict[str, Any]] = []
        self.opened = asyncio.Event()

    async def send(self, frame: dict[str, Any]) -> None:
        self.frames.append(frame)
        if frame["method"] == "credential.request":
            self.opened.set()

    def request(self) -> dict[str, Any]:
        return next(f["params"] for f in self.frames if f["method"] == "credential.request")

    def closed(self) -> list[str]:
        return [f["params"]["reason"] for f in self.frames if f["method"] == "credential.closed"]


def _broker(page: Page, written: list[tuple[str, str]], **kwargs: Any) -> CredentialBroker:
    async def sink(reference: str, value: str) -> None:
        if value == "bad-shape":
            raise CredentialRefusedError("That does not look like a Tavily key.")
        written.append((reference, value))

    return CredentialBroker(page.send, sinks={"config": sink}, **kwargs)


async def test_the_page_sees_what_to_ask_for_never_where_it_is_written() -> None:
    page, written = Page(), []
    broker = _broker(page, written)
    task = asyncio.create_task(broker.request_credential(conversation_id="c-1", turn_id="t-1", request=REQUEST))
    await page.opened.wait()

    shown = page.request()
    assert shown["label"] == "Tavily API key" and shown["conversation_id"] == "c-1"
    assert "target" not in shown and "tavily.apiKey" not in str(shown)

    assert await broker.submit(shown["request_id"], "c-1", "  tvly-real  ") == {"ok": True}
    assert await task is CredentialOutcome.SAVED
    assert written == [("tools.web.providers.tavily.apiKey", "tvly-real")]
    assert page.closed() == ["saved"]


async def test_a_value_the_sink_refuses_keeps_the_card_open_for_another_try() -> None:
    page, written = Page(), []
    broker = _broker(page, written)
    task = asyncio.create_task(broker.request_credential(conversation_id="c-1", turn_id="t-1", request=REQUEST))
    await page.opened.wait()
    rid = page.request()["request_id"]

    refused = await broker.submit(rid, "c-1", "bad-shape")
    assert refused == {"ok": False, "error": "That does not look like a Tavily key."}
    assert "bad-shape" not in str(page.frames) and not task.done()
    assert await broker.submit(rid, "c-1", "   ") == {"ok": False, "error": "Nothing was entered."}

    assert await broker.submit(rid, "c-1", "tvly-real") == {"ok": True}
    assert await task is CredentialOutcome.SAVED


async def test_a_skip_a_timeout_and_a_teardown_all_end_as_skipped_and_close_the_card() -> None:
    page, written = Page(), []
    broker = _broker(page, written, timeout_s=0.05)

    skipped = asyncio.create_task(broker.request_credential(conversation_id="c-1", turn_id="t-1", request=REQUEST))
    await page.opened.wait()
    assert broker.skip(page.request()["request_id"], "c-1")
    assert await skipped is CredentialOutcome.SKIPPED

    assert await broker.request_credential(conversation_id="c-2", turn_id="t-2", request=REQUEST) is (
        CredentialOutcome.SKIPPED
    )

    slow = _broker(Page(), written)
    pending = asyncio.create_task(slow.request_credential(conversation_id="c-3", turn_id="t-3", request=REQUEST))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    slow.cancel_all()
    assert await pending is CredentialOutcome.SKIPPED
    assert page.closed() == ["skipped", "timeout"] and written == []


async def test_a_target_no_sink_serves_is_never_shown() -> None:
    page = Page()
    broker = _broker(page, [])
    outcome = await broker.request_credential(
        conversation_id="c-1", turn_id="t-1", request=CredentialRequest(target="vault:x", label="x")
    )
    assert outcome is CredentialOutcome.SKIPPED and page.frames == []


async def test_one_card_at_a_time_per_conversation() -> None:
    page, written = Page(), []
    broker = _broker(page, written)
    first = asyncio.create_task(broker.request_credential(conversation_id="c-1", turn_id="t-1", request=REQUEST))
    second = asyncio.create_task(broker.request_credential(conversation_id="c-1", turn_id="t-1", request=REQUEST))
    await page.opened.wait()
    await asyncio.sleep(0.01)
    assert len(broker.pending("c-1")) == 1

    broker.skip(broker.pending("c-1")[0]["request_id"], "c-1")
    await first
    await asyncio.sleep(0.01)
    assert len(broker.pending("c-1")) == 1
    broker.skip(broker.pending("c-1")[0]["request_id"], "c-1")
    assert await second is CredentialOutcome.SKIPPED


async def test_the_methods_answer_only_for_the_conversation_the_card_belongs_to() -> None:
    page, written = Page(), []
    broker = _broker(page, written)
    task = asyncio.create_task(broker.request_credential(conversation_id="c-1", turn_id="t-1", request=REQUEST))
    await page.opened.wait()
    rid = page.request()["request_id"]

    replay = await credential_pending({"conversation_id": "c-1"}, credential_broker=broker)
    assert [r["request_id"] for r in replay["requests"]] == [rid]
    wrong = await credential_submit(
        {"request_id": rid, "conversation_id": "c-2", "value": "tvly-real"}, credential_broker=broker
    )
    assert wrong == {"ok": False, "error": "This request is no longer open."} and written == []

    done = await credential_submit(
        {"request_id": rid, "session_id": "c-1", "value": "tvly-real"}, credential_broker=broker
    )
    assert done == {"ok": True} and await task is CredentialOutcome.SAVED
    assert await credential_skip({"request_id": rid, "conversation_id": "c-1"}, credential_broker=broker) == {
        "ok": False
    }
