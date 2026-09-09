"""ask_user over ACP: the broker's frames and the answer round trip."""

import asyncio

from raven.acp.questions import (
    ASK_USER_UPDATE_KIND,
    CLARIFY_RESPOND_METHOD,
    build_question_broker,
)


def test_the_respond_method_is_an_extension_name() -> None:
    """Underscore-prefixed per ACP extensibility: a spec-only client that calls
    nothing custom can never collide with it, and the consuming raven's dialect
    can key on the prefix."""
    assert CLARIFY_RESPOND_METHOD.startswith("_raven/")


def test_the_question_leaves_as_a_session_update() -> None:
    """The broker's ``clarify.request`` is re-wrapped, not re-invented: the
    conversation id IS the wire sessionId, and the requestId rides along so the
    client can answer by either handle."""
    frames: list[dict] = []
    broker = build_question_broker(frames.append)

    async def run():
        task = asyncio.ensure_future(
            broker.await_question("acp:chat1", prompt="which year?", choices=["2023", "2024"])
        )
        await asyncio.sleep(0)  # let the send run
        assert frames, "no frame left the broker"
        update = frames[0]["params"]["update"]
        assert broker.reply(update["requestId"], "2024") is True
        return await task, frames[0]

    answer, frame = asyncio.run(run())
    assert answer == "2024"
    assert frame["method"] == "session/update"
    assert "id" not in frame  # a notification: nothing awaits a response
    assert frame["params"]["sessionId"] == "acp:chat1"
    update = frame["params"]["update"]
    assert update["sessionUpdate"] == ASK_USER_UPDATE_KIND
    assert update["question"] == "which year?"
    assert update["choices"] == ["2023", "2024"]
    assert update["requestId"]


def test_the_session_id_answers_too_and_cancel_all_fail_safes() -> None:
    """``reply`` accepts the conversation handle as well (the client may key by
    session), and ``cancel_all`` resolves a pending question to its default --
    the EOF path in ``server.serve`` depends on it to unblock a held turn."""
    frames: list[dict] = []
    broker = build_question_broker(frames.append)

    async def run():
        task = asyncio.ensure_future(
            broker.await_question("acp:chat2", prompt="scope?", default="")
        )
        await asyncio.sleep(0)
        assert broker.reply("acp:chat2", "the EU market") is True
        first = await task

        task = asyncio.ensure_future(
            broker.await_question("acp:chat2", prompt="scope?", default="proceed")
        )
        await asyncio.sleep(0)
        broker.cancel_all()
        return first, await task

    answered, failed_safe = asyncio.run(run())
    assert answered == "the EU market"
    assert failed_safe == "proceed"
