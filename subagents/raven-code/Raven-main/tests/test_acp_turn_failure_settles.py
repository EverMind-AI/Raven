"""A failed turn settles its session/prompt instead of hanging it.

The regression: TurnFailed events reached the ACP translator as `error` frames
carrying no turn_id, and a prompt that had already learned its turn id dropped
them as some other turn's - so the prompt future never resolved and the client
(the host's acp backend, an editor, this repo's E2E driver) waited forever.
The sink now stamps the failing turn's id onto the error event; these tests pin
the whole contract from both sides.
"""

import asyncio

import pytest

from raven.acp.updates import AcpSession, UpdateTranslator


@pytest.fixture(scope="module", autouse=True)
def restore_default_event_loop_after_module():
    """Do not leave later synchronous tests without their legacy default loop."""
    yield
    asyncio.set_event_loop(asyncio.new_event_loop())


def make_translator():
    frames = []
    translator = UpdateTranslator(emit=frames.append)
    session = AcpSession(session_id="acp:s", session_key="acp:s", cwd="/tmp", subscription_id="sub1")
    translator.add(session)
    return translator, frames


def error_event(turn_id: str | None):
    payload = {"code": -32099, "message": "turn_failed", "reason": "internal", "detail": "boom"}
    if turn_id is not None:
        payload["turn_id"] = turn_id
    return {"subscription_id": "sub1", "event": {"type": "error", "payload": payload}}


async def _drive(event_turn_id: str | None, accepted: str | None):
    translator, _ = make_translator()
    future = translator.begin_turn("acp:s")
    if accepted is not None:
        translator.accept_turn("acp:s", accepted)
    await translator.send_frame({"jsonrpc": "2.0", "method": "event", "params": error_event(event_turn_id)})
    stop = await asyncio.wait_for(future, timeout=2)
    return stop


@pytest.mark.asyncio
async def test_error_naming_the_turn_settles_the_prompt():
    assert await _drive("t1", accepted="t1") == "end_turn"


@pytest.mark.asyncio
async def test_error_naming_another_turn_does_not_settle():
    translator, _ = make_translator()
    future = translator.begin_turn("acp:s")
    translator.accept_turn("acp:s", "t1")
    await translator.send_frame({"jsonrpc": "2.0", "method": "event", "params": error_event("someone-else")})
    assert not future.done(), "another turn's failure must not answer this prompt"
    translator.end_turn("acp:s")


@pytest.mark.asyncio
async def test_error_with_no_id_on_an_uncorrelated_prompt_still_settles():
    # accept_turn("") = the runtime said there is no id to compare: the legacy
    # behaviour, which must keep settling rather than hold forever.
    assert await _drive(None, accepted="") == "end_turn"
