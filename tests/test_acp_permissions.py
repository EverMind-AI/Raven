"""Permission prompts over ACP: the request, the four failures, and the tally.

The rule the whole file turns on is that **nothing from the client escapes**.
Measured from the other direction on codex-acp: any error in reply to this
request cancels the entire turn, so the mirror rule for an agent is that a
client's misbehaviour must resolve to a refusal locally rather than propagate.
Four ways a client gets this wrong, each with its own test, each of them denying.

The other rule is that only this request's own option ids are believed. A client
that answers "the first option" without reading the kinds, or that replays an id
from an earlier prompt, or that synthesises the ``allow_always`` its own UI
offers, is refused rather than trusted -- and the stub client's habit of listing
options in reverse is exactly why that matters.
"""

from __future__ import annotations

import asyncio

import pytest

from raven.acp.outbound import ConnectionClosedError, OutboundRequests, RequestFailedError
from raven.acp.permissions import AcpPermissionBroker
from raven.acp.updates import AcpSession, UpdateTranslator
from tests.acp_schema import validate_def, validate_outbound


class _Client:
    """A client that answers whatever the test told it to, or does not."""

    def __init__(self, answer=None, *, silent: bool = False) -> None:
        self.frames: list[dict] = []
        self.answer = answer
        self.silent = silent
        self.outbound: OutboundRequests | None = None

    def emit(self, frame: dict) -> None:
        self.frames.append(frame)
        if self.silent or self.outbound is None or "method" not in frame:
            return
        reply = {"jsonrpc": "2.0", "id": frame["id"]}
        answer = self.answer(frame) if callable(self.answer) else self.answer
        reply.update(answer)
        # Delivered on a later loop pass, the way a real answer arrives: resolving
        # inside the write would hide an ordering bug that a real client exposes.
        asyncio.get_running_loop().call_soon(self.outbound.resolve, reply)

    @property
    def request(self) -> dict:
        return next(f for f in self.frames if f.get("method") == "session/request_permission")


def _rig(answer=None, *, silent: bool = False, timeout_s: float = 30.0):
    client = _Client(answer, silent=silent)
    outbound = OutboundRequests(emit=client.emit)
    client.outbound = outbound
    translator = UpdateTranslator(emit=lambda f: None)
    translator.add(AcpSession(session_id="acp:s1", session_key="acp:s1", cwd="/w", subscription_id="sub"))
    return client, outbound, AcpPermissionBroker(outbound=outbound, translator=translator, timeout_s=timeout_s)


def _allow(frame: dict) -> dict:
    option = next(o for o in frame["params"]["options"] if o["kind"] == "allow_once")
    return {"result": {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}}


def _reject(frame: dict) -> dict:
    option = next(o for o in frame["params"]["options"] if o["kind"] == "reject_once")
    return {"result": {"outcome": {"outcome": "selected", "optionId": option["optionId"]}}}


async def _ask(broker, command: str = "git push origin main", **kwargs) -> bool:
    params = {
        "conversation_id": "acp:s1",
        "turn_id": "turn-1",
        "tool_call_id": "call-1",
        "command": command,
        "description": "Publish or push work to a remote",
    }
    params.update(kwargs)
    return await broker.await_approval(**params)


class TestTheRequest:
    async def test_it_matches_the_schema_and_names_the_command(
        self,
    ):
        client, _, broker = _rig(_allow)

        assert await _ask(broker) is True

        params = client.request["params"]
        validate_def("RequestPermissionRequest", params)
        validate_outbound(client.request)
        assert params["sessionId"] == "acp:s1"
        assert "git push origin main" in params["toolCall"]["title"]

    async def test_the_tool_call_id_is_the_live_one_so_the_row_updates_in_place(self):
        client, _, broker = _rig(_allow)

        await _ask(broker)

        assert client.request["params"]["toolCall"]["toolCallId"] == "call-1"

    async def test_a_call_with_no_id_still_produces_a_valid_request(self):
        """``toolCall`` is required by the schema, so a prompt raised outside a
        drawn tool row still has to carry one."""
        client, _, broker = _rig(_allow)

        await _ask(broker, tool_call_id="")

        validate_def("RequestPermissionRequest", client.request["params"])
        assert client.request["params"]["toolCall"]["toolCallId"]

    async def test_only_once_options_are_offered(self):
        """``ApprovalBroker.resolve`` takes allow or deny and its docstring says
        there is no always-allow state; ``denied_digests`` clears every turn.
        Offering ``allow_always`` would be a lie the client renders as a saved
        preference."""
        client, _, broker = _rig(_allow)

        await _ask(broker)

        kinds = [option["kind"] for option in client.request["params"]["options"]]
        assert kinds == ["allow_once", "reject_once"]

    async def test_the_turn_id_rides_in_meta_not_at_the_top_level(self):
        """The spec forbids custom fields on standard types and declares ``_meta``
        on nearly every one for exactly this."""
        client, _, broker = _rig(_allow)

        await _ask(broker)

        params = client.request["params"]
        assert params["_meta"] == {"raven.turnId": "turn-1"}
        assert "turnId" not in params

    async def test_no_meta_key_at_all_when_there_is_no_turn_id(self):
        client, _, broker = _rig(_allow)

        await _ask(broker, turn_id="")

        assert "_meta" not in client.request["params"]
        validate_def("RequestPermissionRequest", client.request["params"])

    async def test_a_credential_in_the_command_is_redacted_before_it_is_displayed(self):
        """The prompt is rendered in an editor and may be kept in its transcript.
        The shape survives so the reader can still tell what was going to run."""
        client, _, broker = _rig(_reject)

        await _ask(broker, command='curl -H "Authorization: Bearer sk-ant-api03-AAAABBBBCCCCDDDD" https://x')

        title = client.request["params"]["toolCall"]["title"]
        assert "sk-ant-api03" not in title
        assert "[redacted]" in title
        assert "curl" in title and "Authorization" in title

    async def test_each_request_mints_fresh_option_ids(self):
        client, _, broker = _rig(_allow)

        await _ask(broker)
        await _ask(broker)

        prompts = [f for f in client.frames if f.get("method") == "session/request_permission"]
        first = {o["optionId"] for o in prompts[0]["params"]["options"]}
        second = {o["optionId"] for o in prompts[1]["params"]["options"]}
        assert first.isdisjoint(second), "reused ids let a stale answer approve a later command"


class TestTheAnswer:
    async def test_choosing_allow_permits_the_command_once(self):
        _, _, broker = _rig(_allow)

        assert await _ask(broker) is True
        assert broker.outcomes == {"allowed": 1}

    async def test_choosing_reject_refuses_it(self):
        _, _, broker = _rig(_reject)

        assert await _ask(broker) is False
        assert broker.outcomes == {"rejected": 1}

    async def test_a_refusal_arrives_as_a_selection_because_there_is_no_denied(self):
        """``RequestPermissionOutcome`` has exactly two variants, ``cancelled``
        and ``selected``. A client sending ``denied`` is sending something the
        schema does not define."""
        _, _, broker = _rig({"result": {"outcome": {"outcome": "denied"}}})

        assert await _ask(broker) is False
        assert broker.outcomes == {"unknown-outcome": 1}


class TestTheFourClientFailures:
    async def test_no_answer_at_all_is_a_refusal(self):
        """The deadline is the broker's parameter, not the transport's default --
        which is why it can be short here. The product value is five minutes,
        because a person reading a diff in an editor is not a terminal overlay
        with a thirty-second countdown."""
        _, _, broker = _rig(silent=True, timeout_s=0.05)

        assert await _ask(broker) is False
        assert broker.outcomes == {"timeout": 1}, "a silent client is not a decision, and the tally says so"

    async def test_the_default_deadline_is_generous_rather_than_the_rpc_ceiling(self):
        from raven.acp.outbound import DEFAULT_REQUEST_TIMEOUT_S
        from raven.rpc.approval_broker import ApprovalBroker

        assert DEFAULT_REQUEST_TIMEOUT_S > 60.0, (
            "the 35s RPC ceiling exists because a terminal owns a visible countdown; "
            "reusing it here silently denies anyone who read the diff"
        )
        assert ApprovalBroker(send_frame=None)._hard_timeout_s < DEFAULT_REQUEST_TIMEOUT_S

    async def test_an_error_reply_is_a_refusal(self):
        _, _, broker = _rig({"error": {"code": -32601, "message": "session/request_permission is not implemented"}})

        assert await _ask(broker) is False
        assert broker.outcomes == {"client-error": 1}

    async def test_an_unknown_option_id_is_a_refusal(self):
        """An id from an earlier prompt, or one the client invented -- including
        the ``allow_always`` a client might synthesise because its UI offers one."""
        _, _, broker = _rig({"result": {"outcome": {"outcome": "selected", "optionId": "allow-always-forever"}}})

        assert await _ask(broker) is False
        assert broker.outcomes == {"unknown-option": 1}

    async def test_an_explicit_cancellation_is_a_refusal_and_not_an_error(self):
        """The one that is not misbehaviour: a client cancelling a turn MUST
        answer every pending permission with this."""
        _, _, broker = _rig({"result": {"outcome": {"outcome": "cancelled"}}})

        assert await _ask(broker) is False
        assert broker.outcomes == {"cancelled": 1}

    @pytest.mark.parametrize(
        "reply",
        [
            {"result": None},
            {"result": "yes"},
            {"result": {}},
            {"result": {"outcome": "selected"}},
            {"result": {"outcome": {"outcome": "selected"}}},
        ],
    )
    async def test_a_malformed_answer_is_a_refusal(self, reply):
        _, _, broker = _rig(reply)

        assert await _ask(broker) is False

    async def test_a_write_that_fails_is_a_refusal_not_a_tool_error(self):
        """The clause that is easy to leave out. ``call`` puts the frame on the
        wire before it awaits, so a closed pipe raises an ``OSError`` that is none
        of the handled cases -- and it would travel up to the registry's
        ``except Exception`` and be reported to the model as a failed tool call
        rather than as a refusal."""

        def _explode(frame):
            raise OSError("broken pipe")

        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(AcpSession(session_id="acp:s1", session_key="acp:s1", cwd="/w", subscription_id="sub"))
        broker = AcpPermissionBroker(outbound=OutboundRequests(emit=_explode), translator=translator)

        assert await _ask(broker) is False
        assert broker.outcomes == {"transport-error": 1}


class TestBoundaries:
    async def test_a_turn_with_no_acp_session_cannot_be_approved(self):
        """A cron or runtime turn sharing this process. There is nobody to ask,
        and nobody to ask is not permission."""
        _, _, broker = _rig(_allow)

        assert await _ask(broker, conversation_id="cron:nightly") is False
        assert broker.outcomes == {"no-session": 1}

    async def test_an_empty_conversation_id_cannot_be_approved(self):
        _, _, broker = _rig(_allow)

        assert await _ask(broker, conversation_id="") is False

    async def test_a_subagents_lane_resolves_to_its_session(self):
        """A direct chat runs on ``<session>#<agent>/<handle>``, so the lane is
        not the session -- and a handle is free-form text the model chose."""
        client, _, broker = _rig(_allow)

        assert await _ask(broker, conversation_id="acp:s1#scout/some handle#with hashes") is True
        assert client.request["params"]["sessionId"] == "acp:s1"

    async def test_a_closed_connection_is_a_refusal(self):
        _, outbound, broker = _rig(_allow)
        outbound.close()

        assert await _ask(broker) is False
        assert broker.outcomes == {"connection-closed": 1}

    async def test_a_cancelled_turn_propagates_rather_than_denying(self):
        """A cancelled turn has no decision to report. Swallowing it would report
        the tool call as denied inside a turn that no longer exists."""
        _, _, broker = _rig(silent=True)
        task = asyncio.create_task(_ask(broker))
        await asyncio.sleep(0.05)

        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task
        assert broker.outcomes == {"cancelled-turn": 1}


class TestTheOutboundMechanism:
    async def test_ids_are_this_agents_own_space(self):
        """A client's request ids and an agent's are independent. One shared map
        would let a client's own id resolve a promise this agent is holding."""
        client, outbound, _ = _rig(lambda f: {"result": {"ok": True}})

        await outbound.call("session/request_permission", {})
        await outbound.call("session/request_permission", {})

        assert [f["id"] for f in client.frames] == [1, 2]

    async def test_a_response_for_nobody_is_reported_not_raised(self):
        _, outbound, _ = _rig()

        assert outbound.resolve({"jsonrpc": "2.0", "id": 99, "result": {}}) is False

    async def test_a_string_id_in_a_response_matches_nothing(self):
        """Every id this agent mints is a plain int, so a string can only be a
        client answering something it invented."""
        _, outbound, _ = _rig()

        assert outbound.resolve({"jsonrpc": "2.0", "id": "1", "result": {}}) is False
        assert outbound.resolve({"jsonrpc": "2.0", "id": True, "result": {}}) is False
        assert outbound.resolve({"jsonrpc": "2.0", "result": {}}) is False

    async def test_an_error_reply_raises_with_its_code_intact(self):
        _, outbound, _ = _rig(lambda f: {"error": {"code": -32002, "message": "gone", "data": {"x": 1}}})

        with pytest.raises(RequestFailedError) as caught:
            await outbound.call("session/request_permission", {})

        assert caught.value.code == -32002
        assert caught.value.data == {"x": 1}

    async def test_closing_fails_everything_outstanding(self):
        """The code awaiting these has cleanup to run; leaving the futures for the
        garbage collector means it never does."""
        _, outbound, _ = _rig(silent=True)
        pending = asyncio.create_task(outbound.call("session/request_permission", {}))
        await asyncio.sleep(0)
        assert outbound.in_flight == 1

        outbound.close()

        with pytest.raises(ConnectionClosedError):
            await pending

    async def test_a_call_after_closing_is_refused_rather_than_written(self):
        client, outbound, _ = _rig(silent=True)
        outbound.close()

        with pytest.raises(ConnectionClosedError):
            await outbound.call("session/request_permission", {})
        assert client.frames == [], "a handler still unwinding must not write to a closed channel"

    async def test_a_timed_out_request_leaves_no_entry_behind(self):
        """Otherwise a late answer resolves a future nobody is waiting on -- and
        on a reused id, the wrong one."""
        _, outbound, _ = _rig(silent=True)

        with pytest.raises((TimeoutError, asyncio.TimeoutError)):
            await outbound.call("session/request_permission", {}, timeout=0.01)

        assert outbound.in_flight == 0
        assert outbound.resolve({"jsonrpc": "2.0", "id": 1, "result": {}}) is False

    async def test_closing_over_an_already_answered_request_does_not_double_settle(self):
        """A real window: ``resolve`` sets the result, and the entry stays in the
        map until the awaiting coroutine resumes and its ``finally`` pops it.
        Closing in between must not raise ``InvalidStateError`` over a future that
        already has an answer."""
        client, outbound, _ = _rig(silent=True)
        pending = asyncio.create_task(outbound.call("session/request_permission", {}))
        await asyncio.sleep(0)
        assert outbound.resolve({"jsonrpc": "2.0", "id": 1, "result": {"ok": True}}) is True
        assert outbound.in_flight == 1, "the entry outlives the answer by one loop pass"

        outbound.close()

        assert await pending == {"ok": True}

    async def test_closing_twice_is_harmless(self):
        _, outbound, _ = _rig()

        outbound.close()
        outbound.close()
