"""The outbound translator: every wire event, and the turn state machine.

Two things are pinned, and they fail for different reasons.

**Coverage of the wire vocabulary.** ``KNOWN_EVENT_TYPES`` is asserted against
what the code that emits them actually emits, so a new ``TuiOutlet`` event fails
here rather than being dropped by a translator with no branch for it. That is the
failure mode where a client is missing information and nobody notices.

**Shape of every frame.** Each translated update is validated against the
vendored official schema. Comparing against a dict typed out here would only
assert that the translator does what this file expects; the schema is what
decides whether that is the protocol.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from raven.acp.tool_kinds import MAX_LOCATIONS, locations, title_for, tool_kind
from raven.acp.updates import (
    KNOWN_EVENT_TYPES,
    MAX_DEFERRED_ENDINGS,
    MAX_MEDIA_ITEMS,
    MAX_RESULT_PREVIEW,
    SIDE_CHANNEL_METHODS,
    AcpSession,
    TurnAlreadyRunningError,
    UpdateTranslator,
    translate,
)
from tests.acp_schema import validate_def


def _session(session_id: str = "acp:s1", cwd: str = "/work") -> AcpSession:
    return AcpSession(session_id=session_id, session_key=session_id, cwd=cwd, subscription_id="sub-1")


def _event(event_type: str, **payload):
    return {
        "jsonrpc": "2.0",
        "method": "event",
        "params": {"subscription_id": "sub-1", "event": {"type": event_type, "payload": payload}},
    }


def _updates(frames: list[dict]) -> list[dict]:
    return [f["params"]["update"] for f in frames if f.get("method") == "session/update"]


class TestWireVocabulary:
    def test_the_pinned_set_is_what_the_emitters_emit(self):
        """Read out of the source rather than restated, so an event added to the
        spine fails this test instead of silently going untranslated.

        The three that are matched by prefix are the DAG bridge's, which builds
        its wire names from a map rather than as literals at the emit site.
        """
        import inspect
        import re

        from raven.rpc import spine
        from raven.rpc.methods import turn

        emitted = set()
        for module in (spine, turn):
            source = inspect.getsource(module)
            emitted |= set(re.findall(r'"type": "([a-z_.]+)"', source))
        emitted |= set(re.findall(r'"(dag\.[a-z_]+)"', inspect.getsource(spine)))
        emitted |= {"cron.delivered", "cron.missed"}

        missing = emitted - KNOWN_EVENT_TYPES
        assert missing == set(), (
            f"these wire events reach the ACP sink with no branch in the translator: {sorted(missing)}"
        )

    def test_the_pinned_set_does_not_claim_events_that_do_not_exist(self):
        # The other direction: a name left behind after an event was renamed
        # would make the coverage assertion above pass while the branch is dead.
        for kind in KNOWN_EVENT_TYPES:
            translate({"type": kind, "payload": {}})

    def test_the_side_channel_list_covers_every_non_event_notification(self):
        """These are the frames that share ``send_frame`` with the subscription
        stream. Each is dropped today; the list exists so "dropped" is a decision
        with a name rather than an accident."""
        assert {"approval.request", "clarify.request", "confirm.request"} <= SIDE_CHANNEL_METHODS
        assert {"mcp.status", "memory.health", "oauth.pending", "oauth.done"} <= SIDE_CHANNEL_METHODS


class TestTranslatedFrames:
    def test_text_becomes_an_agent_message_chunk(self):
        result = translate({"type": "token.delta", "payload": {"text": "hello"}})

        assert result.updates[0]["sessionUpdate"] == "agent_message_chunk"
        assert result.updates[0]["content"] == {"type": "text", "text": "hello"}
        validate_def("SessionUpdate", result.updates[0])

    def test_reasoning_becomes_a_thought_chunk(self):
        result = translate({"type": "thinking.delta", "payload": {"text": "considering"}})

        assert result.updates[0]["sessionUpdate"] == "agent_thought_chunk"
        validate_def("SessionUpdate", result.updates[0])

    def test_an_empty_delta_produces_no_frame(self):
        """The stream carries empty deltas at boundaries; forwarding them would
        put a frame on the wire for every one."""
        assert translate({"type": "token.delta", "payload": {"text": ""}}).updates == ()
        assert translate({"type": "token.delta", "payload": {}}).updates == ()

    def test_a_tool_start_is_in_progress_not_pending(self):
        """``pending`` means "not started -- streaming input or awaiting
        approval". By the time this event exists the call is running, and a
        pending row that never changes reads as a hang."""
        result = translate(
            {
                "type": "tool.start",
                "payload": {"tool_call_id": "t1", "name": "exec", "arguments": {"command": "npm test"}},
            },
            cwd="/work",
        )
        update = result.updates[0]

        assert update["status"] == "in_progress"
        assert update["kind"] == "execute"
        assert update["title"] == "exec: npm test"
        validate_def("SessionUpdate", update)

    def test_a_tool_start_resolves_its_locations_against_the_session_cwd(self):
        """The spec requires absolute paths, and raven's tools take
        workspace-relative ones. Resolving against the process cwd instead would
        aim the client's follow-along at wherever the editor was launched from."""
        result = translate(
            {
                "type": "tool.start",
                "payload": {"tool_call_id": "t", "name": "read_file", "arguments": {"path": "a.py"}},
            },
            cwd="/work",
        )

        assert result.updates[0]["locations"] == [{"path": "/work/a.py"}]

    def test_a_blocking_tool_is_marked_in_meta(self):
        """There is no standard field for it, and a client that clocks the stream
        needs to stop the clock: a blocking call may emit nothing for minutes."""
        result = translate(
            {"type": "tool.start", "payload": {"tool_call_id": "t", "name": "ask_user", "blocking": True}}
        )

        assert result.updates[0]["_meta"]["raven.blocking"] is True
        validate_def("SessionUpdate", result.updates[0])

    def test_a_tool_completion_carries_its_preview_as_content(self):
        result = translate({"type": "tool.complete", "payload": {"tool_call_id": "t1", "result_preview": "3 passed"}})
        update = result.updates[0]

        assert update["sessionUpdate"] == "tool_call_update"
        assert update["content"] == [{"type": "content", "content": {"type": "text", "text": "3 passed"}}]
        validate_def("SessionUpdate", update)

    def test_a_truncated_preview_says_so(self):
        result = translate(
            {"type": "tool.complete", "payload": {"tool_call_id": "t", "result_preview": "x", "truncated": True}}
        )

        assert result.updates[0]["content"][0]["content"]["text"].endswith("[truncated]")

    def test_an_oversized_preview_is_capped_even_when_unflagged(self):
        """The runtime truncates and sets the flag; this is the backstop for a
        tool that does neither, so one runaway result cannot become a
        multi-megabyte frame."""
        payload = {"tool_call_id": "t", "result_preview": "y" * (MAX_RESULT_PREVIEW + 100)}
        text = translate({"type": "tool.complete", "payload": payload}).updates[0]["content"][0]["content"]["text"]

        assert len(text) <= MAX_RESULT_PREVIEW + len("\n[truncated]")
        assert text.endswith("[truncated]")

    def test_an_empty_preview_sends_status_without_content(self):
        update = translate({"type": "tool.complete", "payload": {"tool_call_id": "t"}}).updates[0]

        assert "content" not in update, "an empty content array renders as a blank block"
        validate_def("SessionUpdate", update)

    def test_a_subagents_reply_is_tagged_rather_than_merged(self):
        """A direct chat runs on its own lane but is emitted onto the session's
        subscription. Untagged, a delegated agent's words are rendered as the
        main agent's; suppressed, a turn looks idle while a sub-agent talks."""
        result = translate(
            {"type": "token.delta", "payload": {"text": "sub", "target": {"agent": "scout", "handle": "h"}}}
        )

        assert result.updates[0]["_meta"] == {"raven.target": {"agent": "scout", "handle": "h"}}
        validate_def("SessionUpdate", result.updates[0])

    def test_a_write_arrives_as_a_structured_diff_beside_the_preview(self):
        """Built from the file's contents, not from the unified diff the same
        event carries: a unified diff cannot be turned back into the file, its
        context is limited, and an oversized rewrite is dropped from it."""
        result = translate(
            {
                "type": "tool.complete",
                "payload": {
                    "tool_call_id": "t",
                    "result_preview": "Successfully wrote 6 bytes",
                    "diff": "--- /w/a.py\n+++ /w/a.py\n@@ -1 +1 @@\n-x = 0\n+x = 1",
                    "file_change": {"path": "/w/a.py", "after": "x = 1\n", "before": "x = 0\n"},
                },
            }
        )
        blocks = result.updates[0]["content"]

        assert [b["type"] for b in blocks] == ["content", "diff"]
        assert blocks[1] == {"type": "diff", "path": "/w/a.py", "newText": "x = 1\n", "oldText": "x = 0\n"}
        validate_def("SessionUpdate", result.updates[0])

    def test_a_new_file_omits_old_text_rather_than_sending_null(self):
        """The schema says ``oldText`` is "the original content (None for new
        files)". Sending null for a file whose previous content is merely
        unavailable claims it was created, and renders every line of a rewrite as
        an addition."""
        result = translate(
            {
                "type": "tool.complete",
                "payload": {"tool_call_id": "t", "file_change": {"path": "/w/new.py", "after": "x = 1\n"}},
            }
        )
        block = result.updates[0]["content"][0]

        assert block == {"type": "diff", "path": "/w/new.py", "newText": "x = 1\n"}
        assert "oldText" not in block

    @pytest.mark.parametrize(
        "change",
        [
            None,
            "diff",
            {},
            {"path": "/w/a.py"},
            {"after": "x"},
            {"path": "", "after": "x"},
            {"path": "/w/a", "after": 5},
        ],
    )
    def test_a_malformed_change_produces_no_diff_block(self, change):
        result = translate({"type": "tool.complete", "payload": {"tool_call_id": "t", "file_change": change}})

        blocks = result.updates[0].get("content", [])
        assert [b for b in blocks if b["type"] == "diff"] == []

    def test_a_call_that_changed_nothing_sends_no_diff(self):
        result = translate({"type": "tool.complete", "payload": {"tool_call_id": "t", "result_preview": "3 passed"}})

        assert [b["type"] for b in result.updates[0]["content"]] == ["content"]

    def test_a_subagents_tool_completion_is_tagged_too(self):
        """The start and the completion both have to carry it, or a client that
        demultiplexes on the tag renders half a delegated tool call as its own."""
        result = translate(
            {
                "type": "tool.complete",
                "payload": {"tool_call_id": "t", "result_preview": "ok", "target": {"agent": "a", "handle": "h"}},
            }
        )

        assert result.updates[0]["_meta"] == {"raven.target": {"agent": "a", "handle": "h"}}
        validate_def("SessionUpdate", result.updates[0])

    def test_a_malformed_target_is_ignored_rather_than_forwarded(self):
        result = translate({"type": "token.delta", "payload": {"text": "x", "target": "scout"}})

        assert "_meta" not in result.updates[0]


class TestUsage:
    """The one place raven's rich accounting maps cleanly onto ACP."""

    def test_a_completion_carries_the_window_and_the_cost(self):
        result = translate(
            {
                "type": "message.complete",
                "payload": {
                    "turn_id": "t",
                    "usage": {
                        "prompt_tokens": 100,
                        "completion_tokens": 20,
                        "cost_usd": 0.0042,
                        "context_used": 13500,
                        "context_max": 200000,
                    },
                },
            }
        )

        assert result.stop == "end_turn"
        assert result.updates[0] == {
            "sessionUpdate": "usage_update",
            "used": 13500,
            "size": 200000,
            "cost": {"amount": 0.0042, "currency": "USD"},
        }
        validate_def("SessionUpdate", result.updates[0])

    def test_the_usage_goes_out_before_the_turn_ends(self):
        """A cost reported after the turn is over arrives when the client has
        already finalised it."""
        result = translate({"type": "message.complete", "payload": {"usage": {"context_used": 1, "context_max": 2}}})

        assert result.updates and result.stop == "end_turn"

    def test_no_cost_is_no_cost_key_rather_than_a_zero(self):
        result = translate({"type": "message.complete", "payload": {"usage": {"context_used": 1, "context_max": 100}}})

        assert "cost" not in result.updates[0]
        validate_def("SessionUpdate", result.updates[0])

    @pytest.mark.parametrize(
        "usage",
        [
            None,
            {},
            "lots",
            {"context_used": 1},
            {"context_max": 100},
            {"context_used": 1, "context_max": 0},
            {"context_used": -1, "context_max": 100},
            {"context_used": "1", "context_max": 100},
        ],
    )
    def test_an_unusable_window_produces_no_update(self, usage):
        """A ``size`` of zero has a client drawing a full bar or dividing by it,
        and an update of zeroes is not the same statement as no update."""
        result = translate({"type": "message.complete", "payload": {"usage": usage}})

        assert result.updates == ()
        assert result.stop == "end_turn", "the turn still ends; only the accounting is withheld"

    def test_a_cost_of_zero_is_still_reported(self):
        """Zero is a real answer -- a cached reply cost nothing -- and different
        from not knowing."""
        result = translate(
            {"type": "message.complete", "payload": {"usage": {"context_used": 1, "context_max": 2, "cost_usd": 0}}}
        )

        assert result.updates[0]["cost"] == {"amount": 0.0, "currency": "USD"}


class TestTerminationMapping:
    def test_a_completion_ends_the_turn(self):
        assert translate({"type": "message.complete", "payload": {"turn_id": "t"}}).stop == "end_turn"

    def test_the_one_cancel_signal_is_recognised_by_its_reason(self):
        """``turn.cancel`` emits exactly one error event, and its docstring says
        it must always fire -- it is the only cancelled-turn signal. Matching on
        the reason and not the code matters: -32099 is also a build failure and a
        draining scheduler, and neither is a cancellation."""
        result = translate(
            {"type": "error", "payload": {"code": -32099, "message": "turn_cancelled", "reason": "cancelled_by_client"}}
        )

        assert result.stop == "cancelled"
        assert result.updates == (), "a cancel needs no explanation; the client asked for it"

    def test_a_failure_is_explained_and_then_ended_not_errored(self):
        """Measured from the other direction on codex-acp: an error in reply to a
        turn-shaped request makes clients tear down the whole turn. So the
        failure is content, and the turn still ends with a stop reason."""
        result = translate(
            {"type": "error", "payload": {"code": -32008, "message": "model_not_available", "reason": "internal"}}
        )

        assert result.stop == "end_turn"
        assert "model_not_available" in result.updates[0]["content"]["text"]
        assert "-32008" in result.updates[0]["content"]["text"]

    def test_a_failure_with_nothing_in_it_still_says_something(self):
        result = translate({"type": "error", "payload": {}})

        assert result.updates[0]["content"]["text"] == "The turn failed."

    def test_a_blocked_action_latches_refusal_without_ending_the_turn(self):
        """The runtime still ends the turn through its normal path. Claiming the
        stop reason here would race that."""
        result = translate({"type": "notice", "payload": {"kind": "action_blocked", "detail": "policy says no"}})

        assert result.latch == "refusal"
        assert result.stop is None
        assert result.updates[0]["content"]["text"] == "policy says no"

    def test_a_blocked_action_with_no_detail_is_still_explained(self):
        """A refusal with no explanation is indistinguishable from an empty
        answer."""
        result = translate({"type": "notice", "payload": {"kind": "action_blocked"}})

        assert result.updates[0]["content"]["text"]

    def test_other_notices_stay_off_the_wire(self):
        for kind in ("progress", "tool_hint", "injected", "delivery_failed"):
            assert translate({"type": "notice", "payload": {"kind": kind}}).updates == ()


class TestUnmappedEvents:
    @pytest.mark.parametrize(
        "kind",
        [
            "message.start",
            "episode.start",
            "dag.run_started",
            "dag.node_updated",
            "dag.run_completed",
            "cron.delivered",
            "cron.missed",
        ],
    )
    def test_they_produce_nothing_and_do_not_raise(self, kind):
        result = translate({"type": kind, "payload": {"anything": 1}})

        assert (result.updates, result.latch, result.stop) == ((), None, None)

    def test_an_unknown_event_is_dropped_rather_than_raised(self):
        """A translator that crashed on an unrecognised event would take the
        connection down over a wire event somebody added for the web client."""
        assert translate({"type": "something.new", "payload": {}}).updates == ()

    def test_a_malformed_event_is_dropped(self):
        assert translate(None).updates == ()
        assert translate("token.delta").updates == ()
        assert translate({"type": "token.delta", "payload": "hello"}).updates == ()


class TestSinkRouting:
    def test_a_non_dict_frame_never_reaches_the_wire(self):
        """Measured, not hypothetical: ``browser.watch`` pushes an RVF1 header
        plus a JPEG through this same sink, and the WebSocket transport branches
        on bytes. On stdio there is no such branch, so one frame of video would
        put binary on the protocol channel."""
        written = []
        translator = UpdateTranslator(emit=written.append)

        asyncio.run(translator.send_frame(b"RVF1\x00\x01"))

        assert written == []
        assert translator.dropped == {"<non-dict frame>": 1}

    def test_a_side_channel_handler_gets_first_refusal(self):
        """``clarify.request`` blocks a tool call, so dropping it stalls a turn
        until the broker's own timeout rather than failing it. The hook is where
        it gets served."""
        seen = []
        translator = UpdateTranslator(emit=lambda f: None, side_channel=lambda m, p: seen.append((m, p)) or True)

        asyncio.run(translator.send_frame({"jsonrpc": "2.0", "method": "clarify.request", "params": {"q": 1}}))

        assert seen == [("clarify.request", {"q": 1})]
        assert translator.dropped == {}, "a handled frame must not also be counted as dropped"

    def test_a_handler_that_declines_leaves_the_frame_on_the_dropped_tally(self):
        """So a surface that grows a new notification still shows up there."""
        translator = UpdateTranslator(emit=lambda f: None, side_channel=lambda m, p: False)

        asyncio.run(translator.send_frame({"jsonrpc": "2.0", "method": "confirm.request", "params": {}}))

        assert translator.dropped == {"confirm.request": 1}

    def test_a_handler_that_raises_does_not_take_the_turn_down(self):
        """The sink is shared with the streaming path."""

        def _boom(method, params):
            raise RuntimeError("handler is wrong")

        translator = UpdateTranslator(emit=lambda f: None, side_channel=_boom)

        asyncio.run(translator.send_frame({"jsonrpc": "2.0", "method": "clarify.request", "params": {}}))

        assert translator.dropped == {"clarify.request": 1}

    def test_a_frame_with_a_non_string_method_never_reaches_the_handler(self):
        called = []
        translator = UpdateTranslator(emit=lambda f: None, side_channel=lambda m, p: called.append(m) or True)

        asyncio.run(translator.send_frame({"jsonrpc": "2.0", "method": 42, "params": {}}))

        assert called == []

    def test_a_side_channel_notification_is_counted_not_forwarded(self):
        written = []
        translator = UpdateTranslator(emit=written.append)

        asyncio.run(translator.send_frame({"jsonrpc": "2.0", "method": "approval.request", "params": {}}))
        asyncio.run(translator.send_frame({"jsonrpc": "2.0", "method": "approval.request", "params": {}}))

        assert written == []
        assert translator.dropped == {"approval.request": 2}

    def test_an_event_for_an_unknown_subscription_is_dropped(self):
        """Not an error: the emitter also serves turns the runtime submitted
        (cron), which have no ACP session."""
        written = []
        translator = UpdateTranslator(emit=written.append)

        asyncio.run(translator.send_frame(_event("token.delta", text="x")))

        assert written == []
        assert "event/<unbound subscription>" in translator.dropped

    def test_a_malformed_event_frame_is_dropped(self):
        translator = UpdateTranslator(emit=lambda f: None)

        asyncio.run(translator.send_frame({"jsonrpc": "2.0", "method": "event", "params": "sub-1"}))

        assert "event/<no params>" in translator.dropped

    def test_a_frame_without_a_method_is_counted_under_its_own_name(self):
        translator = UpdateTranslator(emit=lambda f: None)

        asyncio.run(translator.send_frame({"jsonrpc": "2.0", "id": 1, "result": {}}))

        assert "<no method>" in translator.dropped

    def test_an_event_reaches_the_session_that_owns_its_subscription(self):
        written = []
        translator = UpdateTranslator(emit=written.append)
        translator.add(_session())

        asyncio.run(translator.send_frame(_event("token.delta", text="hi")))

        assert written[0]["method"] == "session/update"
        assert written[0]["params"]["sessionId"] == "acp:s1"
        validate_def("SessionNotification", written[0]["params"])

    def test_rebinding_a_subscription_releases_the_old_one(self):
        """A session that resubscribes must not keep receiving on the dead id --
        two live mappings to one session would double every frame."""
        written = []
        translator = UpdateTranslator(emit=written.append)
        translator.add(_session())
        translator.bind_subscription("acp:s1", "sub-2")

        asyncio.run(translator.send_frame(_event("token.delta", text="stale")))

        assert written == []
        assert "event/<unbound subscription>" in translator.dropped

    def test_a_first_subscription_can_be_bound_after_the_session_exists(self):
        """The ordering ``session/load`` will need: the session is known before
        its stream is, so the first bind has no previous id to release."""
        written = []
        translator = UpdateTranslator(emit=written.append)
        translator.add(AcpSession(session_id="acp:s2", session_key="acp:s2", cwd="/w"))

        translator.bind_subscription("acp:s2", "sub-7")

        asyncio.run(
            translator.send_frame(
                {
                    "jsonrpc": "2.0",
                    "method": "event",
                    "params": {"subscription_id": "sub-7", "event": {"type": "token.delta", "payload": {"text": "x"}}},
                }
            )
        )
        assert _updates(written)[0]["content"]["text"] == "x"

    def test_binding_an_unknown_session_is_a_no_op(self):
        translator = UpdateTranslator(emit=lambda f: None)

        translator.bind_subscription("acp:missing", "sub-9")

        assert translator.get("acp:missing") is None

    def test_a_session_added_without_a_subscription_can_still_be_looked_up(self):
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(AcpSession(session_id="acp:s2", session_key="acp:s2", cwd="/w"))

        assert translator.get("acp:s2") is not None
        assert len(translator.sessions()) == 1


class TestTurnState:
    async def test_the_terminating_event_resolves_the_prompt(self):
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        future = translator.begin_turn("acp:s1")
        translator.accept_turn("acp:s1", "t")

        await translator.send_frame(_event("message.complete", turn_id="t"))

        assert await future == "end_turn"

    async def test_a_latched_refusal_wins_over_the_default(self):
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        future = translator.begin_turn("acp:s1")
        translator.accept_turn("acp:s1", "t")

        await translator.send_frame(_event("notice", kind="action_blocked", detail="no", turn_id="t"))
        await translator.send_frame(_event("message.complete", turn_id="t"))

        assert await future == "refusal"

    async def test_only_the_first_latch_counts(self):
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        future = translator.begin_turn("acp:s1")

        await translator.send_frame(_event("notice", kind="action_blocked", detail="first"))
        await translator.send_frame(_event("notice", kind="action_blocked", detail="second"))
        await translator.send_frame(_event("message.complete"))

        assert await future == "refusal"

    async def test_a_second_terminating_event_is_a_no_op(self):
        """A cancel followed by the sink's own failure event is the normal shape.
        Resolving twice would raise ``InvalidStateError`` inside the emitter's
        coalesce task, where nothing reports it."""
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        future = translator.begin_turn("acp:s1")

        await translator.send_frame(_event("error", code=-32099, message="c", reason="cancelled_by_client"))
        await translator.send_frame(_event("message.complete"))

        assert await future == "cancelled"

    async def test_events_arriving_with_no_turn_open_are_harmless(self):
        written = []
        translator = UpdateTranslator(emit=written.append)
        translator.add(_session())

        await translator.send_frame(_event("message.complete"))
        await translator.send_frame(_event("token.delta", text="late"))

        assert len(written) == 1, "the text still goes out; only the turn bookkeeping is skipped"

    async def test_a_second_concurrent_prompt_is_refused(self):
        """ACP allows several sessions on one connection, but a session's updates
        carry no request correlation -- two prompts in flight produce one
        interleaved stream that cannot be split apart."""
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        translator.begin_turn("acp:s1")

        with pytest.raises(TurnAlreadyRunningError):
            translator.begin_turn("acp:s1")

    async def test_a_new_prompt_is_allowed_once_the_last_one_settled(self):
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        first = translator.begin_turn("acp:s1")
        await translator.send_frame(_event("message.complete"))
        await first

        second = translator.begin_turn("acp:s1")

        assert second is not first

    async def test_settling_from_outside_the_stream_works_once(self):
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        future = translator.begin_turn("acp:s1")

        assert translator.settle_turn("acp:s1", "cancelled") is True
        assert translator.settle_turn("acp:s1", "end_turn") is False
        assert await future == "cancelled"

    def test_settling_an_unknown_or_idle_session_reports_false(self):
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())

        assert translator.settle_turn("acp:nope", "cancelled") is False
        assert translator.settle_turn("acp:s1", "cancelled") is False

    async def test_closing_answers_what_is_waiting(self):
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        future = translator.begin_turn("acp:s1")

        translator.close()

        assert await future == "cancelled"

    async def test_a_turn_opened_after_closing_is_answered_immediately(self):
        """The race a single sweep cannot close: a handler task created before EOF
        may not have begun before EOF, so it opens its turn after everything
        pending was settled -- and would then wait on a stream that is finished."""
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        translator.close()

        future = translator.begin_turn("acp:s1")

        assert future.done()
        assert await future == "cancelled"

    async def test_closing_twice_is_harmless(self):
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        future = translator.begin_turn("acp:s1")

        translator.close()
        translator.close()

        assert await future == "cancelled"

    async def test_closing_with_no_sessions_is_harmless(self):
        UpdateTranslator(emit=lambda f: None).close()

    async def test_ending_a_turn_is_idempotent(self):
        """The caller runs it from a ``finally``, which can be reached twice on a
        cancellation path."""
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        translator.begin_turn("acp:s1")

        translator.end_turn("acp:s1")
        translator.end_turn("acp:s1")
        translator.end_turn("acp:unknown")


class TestToolClassification:
    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("read_file", "read"),
            ("write_file", "edit"),
            ("grep", "search"),
            ("exec", "execute"),
            ("web_fetch", "fetch"),
            ("message", "other"),
        ],
    )
    def test_known_tools_get_their_kind(self, name, kind):
        assert tool_kind(name) == kind

    def test_an_unknown_tool_is_other_rather_than_guessed(self):
        """An MCP server brings names at runtime. A wrong icon is worse than a
        generic one."""
        assert tool_kind("mcp_github_create_issue") == "other"
        assert tool_kind(None) == "other"
        assert tool_kind("") == "other"

    def test_the_runtimes_own_label_wins(self):
        assert title_for("exec", {"command": "ls"}, "Listing the workspace") == "Listing the workspace"

    def test_a_blank_label_falls_back_rather_than_rendering_empty(self):
        """``title`` is required on ``ToolCall`` and an empty string draws a blank
        row."""
        assert title_for("exec", {"command": "ls"}, "   ") == "exec: ls"
        assert title_for(None, None, None) == "tool"

    def test_a_long_subject_is_truncated(self):
        title = title_for("exec", {"command": "x" * 500}, None)

        assert len(title) < 200
        assert title.endswith("…")

    def test_locations_need_a_base_to_resolve_against(self):
        assert locations({"path": "a.py"}, None) == []
        assert locations({"path": "a.py"}, "relative/base") == []
        assert locations({"path": "/abs/a.py"}, None) == [{"path": "/abs/a.py"}]

    def test_locations_are_deduplicated_and_capped(self):
        found = locations({"paths": [f"/p/{i}.py" for i in range(50)] + ["/p/0.py"]}, None)

        assert len(found) == MAX_LOCATIONS
        assert len({item["path"] for item in found}) == MAX_LOCATIONS

    def test_an_unusable_path_costs_only_itself(self):
        found = locations({"paths": ["/good.py", "bad\x00name", 5, "  "]}, None)

        assert found == [{"path": "/good.py"}]

    def test_symlinks_are_left_alone(self):
        """Resolving turns ``/tmp/x`` into ``/private/tmp/x`` on macOS -- a
        different string from the one the editor has open, which is enough to
        break the follow-along this field exists for."""
        assert locations({"path": "/tmp/x.py"}, None) == [{"path": "/tmp/x.py"}]

    def test_a_path_naming_a_user_who_does_not_exist_is_dropped(self):
        """``Path.expanduser`` raises ``RuntimeError`` for an unknown ``~user``.
        One unusable argument must not cost the whole tool row."""
        found = locations({"paths": ["~nosuchuser0xyz/a.py", "/good.py"]}, None)

        assert found == [{"path": "/good.py"}]

    def test_a_home_relative_path_is_expanded(self):
        found = locations({"path": "~/notes.md"}, None)

        assert found and found[0]["path"].startswith("/")
        assert "~" not in found[0]["path"]

    def test_no_paths_means_no_locations_key(self):
        assert locations({"command": "ls"}, "/work") == []
        assert locations(None, "/work") == []


class TestMedia:
    """A reply's files. They reached this translator for the first time when
    ``TuiOutlet`` stopped eating ``MediaOut``; before that a turn that produced a
    chart answered with text naming a file the client was never told about."""

    def test_a_file_becomes_a_resource_link_chunk(self):
        result = translate(
            {"type": "media", "payload": {"items": [{"path": "/work/out.csv", "mime": "text/csv", "kind": "file"}]}},
            cwd="/work",
        )

        assert result.updates[0]["content"] == {
            "type": "resource_link",
            "uri": "file:///work/out.csv",
            "name": "out.csv",
            "mimeType": "text/csv",
        }
        assert result.stop is None
        validate_def("SessionUpdate", result.updates[0])

    def test_a_link_rather_than_an_image_block_even_for_a_picture(self):
        """``image`` carries base64 ``data``, which would mean reading the file --
        and this translator is pure so its frames can be schema-validated in a
        unit test. A local client would rather open the file anyway."""
        result = translate(
            {"type": "media", "payload": {"items": [{"path": "/work/plot.png", "mime": "image/png", "kind": "image"}]}},
            cwd="/work",
        )

        assert result.updates[0]["content"]["type"] == "resource_link"
        validate_def("SessionUpdate", result.updates[0])

    def test_one_chunk_per_file_in_the_order_the_turn_produced_them(self):
        # ``content`` on a chunk is one ContentBlock, not a list, so several
        # files cannot share an update.
        result = translate(
            {
                "type": "media",
                "payload": {
                    "items": [
                        {"path": "/work/a.csv", "mime": "text/csv", "kind": "file"},
                        {"path": "/work/b.csv", "mime": "text/csv", "kind": "file"},
                    ]
                },
            },
            cwd="/work",
        )

        assert [u["content"]["name"] for u in result.updates] == ["a.csv", "b.csv"]

    def test_a_relative_path_resolves_against_the_session_cwd(self):
        result = translate(
            {"type": "media", "payload": {"items": [{"path": "out.csv", "mime": "text/csv", "kind": "file"}]}},
            cwd="/work",
        )

        assert result.updates[0]["content"]["uri"] == "file:///work/out.csv"

    def test_a_relative_path_with_no_cwd_is_named_in_text_not_linked(self):
        """A relative ``file://`` URI resolves against the *client's* current
        directory, so it either fails or opens a different file with the same
        name. Dropping it silently would instead leave a reader looking for a
        file the agent said it produced."""
        result = translate(
            {"type": "media", "payload": {"items": [{"path": "out.csv", "mime": "text/csv", "kind": "file"}]}}
        )

        assert result.updates[0]["content"] == {"type": "text", "text": "[attachment: out.csv]"}
        validate_def("SessionUpdate", result.updates[0])

    def test_a_space_in_the_path_is_percent_encoded(self):
        # An unencoded space terminates a URI, so the client would receive a link
        # to the first word of the directory name.
        result = translate(
            {
                "type": "media",
                "payload": {"items": [{"path": "/work/My Docs/a b.csv", "mime": "text/csv", "kind": "file"}]},
            },
            cwd="/work",
        )

        assert result.updates[0]["content"]["uri"] == "file:///work/My%20Docs/a%20b.csv"
        assert result.updates[0]["content"]["name"] == "a b.csv"
        validate_def("SessionUpdate", result.updates[0])

    def test_a_declared_mime_is_forwarded_and_a_missing_one_is_omitted(self):
        """Forwarded as declared rather than derived from the extension: every
        emit site hardcodes ``application/octet-stream`` today, and inventing a
        type here would be this translator claiming knowledge the event does not
        carry -- a client picks its viewer by it."""
        result = translate(
            {
                "type": "media",
                "payload": {
                    "items": [
                        {"path": "/work/a.bin", "mime": "application/octet-stream", "kind": "file"},
                        {"path": "/work/b.bin", "kind": "file"},
                    ]
                },
            },
            cwd="/work",
        )

        assert result.updates[0]["content"]["mimeType"] == "application/octet-stream"
        assert "mimeType" not in result.updates[1]["content"]

    @pytest.mark.parametrize(
        "payload",
        [
            {},
            {"items": None},
            {"items": "a.csv"},
            {"items": []},
            {"items": [None, 3, "x"]},
            {"items": [{"mime": "text/csv"}]},
            {"items": [{"path": "   "}]},
        ],
    )
    def test_an_unusable_event_produces_no_frame(self, payload):
        assert translate({"type": "media", "payload": payload}, cwd="/work").updates == ()

    def test_too_many_files_are_capped(self):
        items = [{"path": f"/work/f{i}.csv", "mime": "text/csv", "kind": "file"} for i in range(MAX_MEDIA_ITEMS + 5)]

        result = translate({"type": "media", "payload": {"items": items}}, cwd="/work")

        assert len(result.updates) == MAX_MEDIA_ITEMS

    def test_the_uri_builder_degrades_instead_of_raising(self):
        """Unreachable through ``translate`` -- a path only reaches the builder
        after being made absolute -- and pinned anyway, because what the caller
        relies on is that this never raises. A raise here leaves the suspended
        ``session/prompt`` unanswered, and the next caller does not inherit that
        invariant from a comment."""
        from raven.acp.updates import _file_uri

        assert _file_uri("relative/x.csv") == "file://relative/x.csv"

    def test_the_cap_counts_frames_not_entries(self):
        """Slicing the input instead would let a run of unusable entries push the
        real files past the limit, so a client would be told about none of them
        while the event claimed to carry them."""
        items = [{"kind": "file"} for _ in range(MAX_MEDIA_ITEMS)]
        items.append({"path": "/work/real.csv", "mime": "text/csv", "kind": "file"})

        result = translate({"type": "media", "payload": {"items": items}}, cwd="/work")

        assert [u["content"]["name"] for u in result.updates] == ["real.csv"]


class TestTerminationIsExactlyOnce:
    """Every wire event, against the pending prompt: does it end the turn?

    The pinned assertion is the *set*. A prompt is a suspended request, so an
    event that ends the turn when it should not strands the rest of the reply
    with no way to send it, and one that does not end the turn when it should
    leaves the client waiting forever. Neither shows up as an error anywhere,
    which is why the whole vocabulary is enumerated rather than sampled.
    """

    TERMINAL = {"message.complete", "error"}

    # One representative payload per event, each the shape its emitter actually
    # produces -- an empty payload would make several of these vacuous.
    PAYLOADS = {
        "token.delta": {"text": "x"},
        "thinking.delta": {"text": "x"},
        "tool.start": {"tool_call_id": "t", "name": "exec", "arguments": {"command": "ls"}},
        "tool.complete": {"tool_call_id": "t", "result_preview": "ok"},
        "message.start": {"turn_id": "t"},
        "message.complete": {"turn_id": "t", "usage": {}},
        "error": {"code": -32099, "message": "boom", "reason": "internal"},
        "notice": {"kind": "action_blocked", "detail": "no"},
        "episode.start": {"index": 1},
        "dag.run_started": {"nodes": []},
        "dag.node_updated": {"name": "n"},
        "dag.run_completed": {"ok": True},
        "cron.delivered": {"text": "reminder"},
        "cron.missed": {"drops": []},
        "media": {"items": [{"path": "/work/out.csv", "mime": "text/csv", "kind": "file"}]},
    }

    def test_every_known_event_has_a_representative_payload(self):
        # Otherwise the sweep below silently stops covering an event the moment
        # one is added to the vocabulary.
        assert set(self.PAYLOADS) == KNOWN_EVENT_TYPES

    @pytest.mark.parametrize("event_type", sorted(PAYLOADS))
    async def test_only_the_terminating_events_resolve_the_prompt(self, event_type):
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        future = translator.begin_turn("acp:s1")
        # The representative payloads carry ``turn_id: "t"``, so the turn this
        # prompt owns has to be that one. Without it the sweep would measure the
        # hold for an unattributable ending, not whether the event terminates.
        translator.accept_turn("acp:s1", "t")

        await translator.send_frame(_event(event_type, **self.PAYLOADS[event_type]))

        ends_turn = future.done()
        assert ends_turn == (event_type in self.TERMINAL), (
            f"{event_type} {'ended' if ends_turn else 'did not end'} the turn, which is the wrong answer: "
            "ending early strands the rest of the reply, not ending leaves the client waiting forever"
        )

    @pytest.mark.parametrize("event_type", sorted(TERMINAL))
    async def test_a_terminating_event_resolves_exactly_once_however_often_it_arrives(self, event_type):
        """Repeats are the normal shape, not a bug: a cancel is followed by the
        sink's own failure event for the same turn. Resolving twice raises
        ``InvalidStateError`` inside the emitter's coalesce task, where nothing
        would report it."""
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        future = translator.begin_turn("acp:s1")
        translator.accept_turn("acp:s1", "t")

        for _ in range(3):
            await translator.send_frame(_event(event_type, **self.PAYLOADS[event_type]))

        assert future.done()
        assert await future == "end_turn"

    async def test_the_reason_of_the_first_terminating_event_is_the_one_reported(self):
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        future = translator.begin_turn("acp:s1")

        await translator.send_frame(_event("error", code=-32099, message="c", reason="cancelled_by_client"))
        await translator.send_frame(_event("message.complete", turn_id="t", usage={}))
        await translator.send_frame(_event("error", code=-1, message="late", reason="internal"))

        assert await future == "cancelled"


class TestTheRealOutletPath:
    """The translator against the real emitter and the real outlet.

    Everything above tests the translation of a frame that was typed out here.
    This is the seam that decides whether such a frame ever arrives: a spine
    ``Deliverable`` goes into ``TuiOutlet``, through ``SubscriptionEmitter``'s
    coalescing, out of ``send_frame``, and has to come back as ACP. Getting the
    channel or the subscription key wrong produces exactly nothing, with no error
    anywhere -- which is why it is worth assembling the real objects rather than
    asserting on the shape of a dict.
    """

    @staticmethod
    async def _wire(session_id: str = "acp:s1"):
        from raven.rpc.spine import TuiOutlet
        from raven.rpc.subscriptions import SubscriptionEmitter

        written: list[dict] = []
        translator = UpdateTranslator(emit=written.append)
        emitter = SubscriptionEmitter(send_frame=translator.send_frame)
        subscription_id = await emitter.register(session_id)
        translator.add(
            AcpSession(session_id=session_id, session_key=session_id, cwd="/work", subscription_id=subscription_id)
        )
        return written, translator, emitter, TuiOutlet("acp", emitter)

    @staticmethod
    async def _settle():
        from raven.rpc.subscriptions import COALESCE_WINDOW_S

        # The emitter coalesces on a 16ms window before writing, so the frames do
        # not exist yet when deliver() returns.
        await asyncio.sleep(COALESCE_WINDOW_S * 3)

    async def test_a_streamed_reply_arrives_as_agent_message_chunks(self):
        written, translator, emitter, outlet = await self._wire()
        try:
            await outlet.send_stream_chunk("chat", "acp:s1", "Hel")
            await outlet.send_stream_chunk("chat", "acp:s1", "lo")
            await self._settle()
        finally:
            await emitter.close_session("acp:s1")

        updates = _updates(written)
        assert [u["sessionUpdate"] for u in updates] == ["agent_message_chunk"], (
            "consecutive deltas are merged by the emitter before they reach the translator"
        )
        assert updates[0]["content"]["text"] == "Hello"
        for frame in written:
            validate_def("SessionNotification", frame["params"])

    async def test_a_tool_call_arrives_as_a_call_and_an_update(self):
        from raven.spine.events import ToolEvent, ToolPhase

        written, translator, emitter, outlet = await self._wire()
        try:
            await outlet.deliver(
                ToolEvent(
                    phase=ToolPhase.START,
                    tool_call_id="t1",
                    name="read_file",
                    arguments={"path": "a.py"},
                    conversation_id="acp:s1",
                )
            )
            await outlet.deliver(
                ToolEvent(
                    phase=ToolPhase.COMPLETE,
                    tool_call_id="t1",
                    result_preview="contents",
                    conversation_id="acp:s1",
                )
            )
            await self._settle()
        finally:
            await emitter.close_session("acp:s1")

        updates = _updates(written)
        assert [u["sessionUpdate"] for u in updates] == ["tool_call", "tool_call_update"]
        assert updates[0]["locations"] == [{"path": "/work/a.py"}]
        assert updates[0]["kind"] == "read"

    async def test_reasoning_arrives_as_a_thought_chunk(self):
        from raven.spine.events import Reasoning

        written, translator, emitter, outlet = await self._wire()
        try:
            await outlet.deliver(Reasoning(content="thinking", conversation_id="acp:s1"))
            await self._settle()
        finally:
            await emitter.close_session("acp:s1")

        assert _updates(written)[0]["sessionUpdate"] == "agent_thought_chunk"

    async def test_a_turn_completion_resolves_the_prompt_through_the_real_emitter(self):
        written, translator, emitter, outlet = await self._wire()
        future = translator.begin_turn("acp:s1")
        # What ``session/prompt`` does with the id ``turn.send`` gives back. The
        # emitter below completes ``turn-1``, so this is the turn that answers.
        translator.accept_turn("acp:s1", "turn-1")
        try:
            await outlet.send_stream_chunk("chat", "acp:s1", "answer")
            await outlet.emit_complete("acp:s1", "turn-1", {"cost_usd": 0.01})
            await self._settle()
        finally:
            await emitter.close_session("acp:s1")

        assert await future == "end_turn"
        assert _updates(written)[0]["content"]["text"] == "answer"

    async def test_a_cancel_event_resolves_the_prompt_as_cancelled(self):
        written, translator, emitter, outlet = await self._wire()
        future = translator.begin_turn("acp:s1")
        try:
            # The exact call ``turn.cancel`` makes: one error event whose reason is
            # the only cancelled-turn signal the runtime produces.
            await emitter.emit(
                "acp:s1",
                {
                    "type": "error",
                    "payload": {"code": -32099, "message": "turn_cancelled", "reason": "cancelled_by_client"},
                },
            )
            await self._settle()
        finally:
            await emitter.close_session("acp:s1")

        assert await future == "cancelled"

    async def test_a_blocked_action_arrives_as_a_refusal(self):
        from raven.spine.events import Notice, NoticeKind

        written, translator, emitter, outlet = await self._wire()
        future = translator.begin_turn("acp:s1")
        translator.accept_turn("acp:s1", "turn-1")
        try:
            await outlet.deliver(
                Notice(kind=NoticeKind.ACTION_BLOCKED, detail="policy refused", conversation_id="acp:s1")
            )
            await outlet.emit_complete("acp:s1", "turn-1", {})
            await self._settle()
        finally:
            await emitter.close_session("acp:s1")

        assert await future == "refusal"
        assert _updates(written)[0]["content"]["text"] == "policy refused"

    async def test_another_sessions_stream_does_not_leak_into_this_one(self):
        """The emitter serves every session on this connection plus turns the
        runtime submitted. A translator keyed on the wrong thing would render a
        cron turn's output into whichever editor window happened to be open."""
        written, translator, emitter, outlet = await self._wire()
        try:
            await emitter.register("cron:nightly")
            await emitter.emit("cron:nightly", {"type": "token.delta", "payload": {"text": "scheduled"}})
            await self._settle()
        finally:
            await emitter.close_session("acp:s1")
            await emitter.close_session("cron:nightly")

        assert _updates(written) == []
        assert "event/<unbound subscription>" in translator.dropped


class TestTheFileChangeChain:
    """From the real write tool to the ACP frame, with nothing stubbed between.

    Four hops -- ``ToolResult`` to ``ToolOutput`` to the tool event to the wire
    payload -- each of which the unified diff string already makes by hand. A
    field that is added at one end and read at the other, with a hop missed in
    the middle, produces no error anywhere: the client simply never gets a diff,
    which is indistinguishable from a tool that changed nothing.
    """

    async def test_a_write_reaches_the_wire_as_the_file_both_ways(self, tmp_path):
        from raven.agent.tools.filesystem import WriteFileTool
        from raven.agent.tools.registry import ToolRegistry

        target = tmp_path / "a.py"
        target.write_text("x = 0\n")
        registry = ToolRegistry()
        registry.register(WriteFileTool(workspace=tmp_path))

        output = await registry.execute("write_file", {"path": str(target), "content": "x = 1\n"})

        assert output.file_change is not None, "the registry boundary dropped it"
        assert output.file_change.after == "x = 1\n"
        assert output.file_change.before == "x = 0\n"

    async def test_a_created_file_reports_no_previous_content(self, tmp_path):
        from raven.agent.tools.filesystem import WriteFileTool
        from raven.agent.tools.registry import ToolRegistry

        registry = ToolRegistry()
        registry.register(WriteFileTool(workspace=tmp_path))

        output = await registry.execute("write_file", {"path": str(tmp_path / "new.py"), "content": "x = 1\n"})

        assert output.file_change.before is None, "an empty string here would read as 'was empty', not 'did not exist'"

    async def test_an_edit_reports_the_whole_file_not_the_fragment(self, tmp_path):
        """An edit's arguments carry only the replaced text, so a surface handed
        those would render a fragment as though it were the file."""
        from raven.agent.tools.filesystem import EditFileTool
        from raven.agent.tools.registry import ToolRegistry

        target = tmp_path / "a.py"
        target.write_text("one\ntwo\nthree\n")
        registry = ToolRegistry()
        registry.register(EditFileTool(workspace=tmp_path))

        output = await registry.execute("edit_file", {"path": str(target), "old_text": "two", "new_text": "TWO"})

        assert output.file_change.after == "one\nTWO\nthree\n"
        assert output.file_change.before == "one\ntwo\nthree\n"

    async def test_an_unreadable_file_gets_no_structured_change(self, tmp_path):
        """Three states, not two: absent, readable, and present but not text.
        Reporting the third as a creation would tell a client every line is an
        addition to a file that was already there."""
        from raven.agent.tools.filesystem import WriteFileTool
        from raven.agent.tools.registry import ToolRegistry

        target = tmp_path / "blob.bin"
        target.write_bytes(b"\xff\xfe\x00\x01")
        registry = ToolRegistry()
        registry.register(WriteFileTool(workspace=tmp_path))

        output = await registry.execute("write_file", {"path": str(target), "content": "text now\n"})

        assert output.file_change is None

    async def test_an_append_reports_no_change_because_it_reads_nothing_back(self, tmp_path):
        from raven.agent.tools.filesystem import WriteFileTool
        from raven.agent.tools.registry import ToolRegistry

        target = tmp_path / "log.txt"
        target.write_text("first\n")
        registry = ToolRegistry()
        registry.register(WriteFileTool(workspace=tmp_path))

        output = await registry.execute("write_file", {"path": str(target), "content": "second\n", "mode": "append"})

        assert getattr(output, "file_change", None) is None

    async def test_the_outlet_puts_it_on_the_wire_and_the_translator_reads_it(self):
        from raven.spine.events import ToolEvent, ToolPhase

        written, translator, emitter, outlet = await TestTheRealOutletPath._wire()
        try:
            await outlet.deliver(
                ToolEvent(
                    phase=ToolPhase.COMPLETE,
                    tool_call_id="t1",
                    result_preview="Successfully wrote",
                    file_change={"path": "/work/a.py", "after": "x = 1\n", "before": "x = 0\n"},
                    conversation_id="acp:s1",
                )
            )
            await TestTheRealOutletPath._settle()
        finally:
            await emitter.close_session("acp:s1")

        blocks = _updates(written)[0]["content"]
        assert any(b["type"] == "diff" and b["newText"] == "x = 1\n" for b in blocks), (
            f"the change did not survive the outlet: {blocks}"
        )

    async def test_a_call_with_no_change_adds_no_wire_key(self):
        """Absent rather than null, so every payload the wire already carried
        keeps its shape."""
        from raven.spine.events import ToolEvent, ToolPhase

        written, translator, emitter, outlet = await TestTheRealOutletPath._wire()
        try:
            await outlet.deliver(
                ToolEvent(phase=ToolPhase.COMPLETE, tool_call_id="t1", result_preview="ok", conversation_id="acp:s1")
            )
            await TestTheRealOutletPath._settle()
        finally:
            await emitter.close_session("acp:s1")

        payload = written[0]["params"]["event"] if "event" in written[0].get("params", {}) else None
        blocks = _updates(written)[0].get("content", [])
        assert [b for b in blocks if b["type"] == "diff"] == []
        assert payload is None or "file_change" not in payload.get("payload", {})


class TestTheFileChangePayload:
    """The flattening hop, tested where the rest of the chain is tested.

    ``_file_change_payload`` turns the tools' dataclass into the plain mapping the
    event and then the wire carry. It lives in the agent loop because that is
    where the hop happens, and it is exercised here because everything else about
    this field is.
    """

    @staticmethod
    def _payload(change):
        from raven.agent.loop.main import _file_change_payload

        return _file_change_payload(change)

    def test_a_real_change_flattens_to_the_wire_shape(self):
        from raven.agent.tools.base import FileChange

        assert self._payload(FileChange(path="/w/a.py", after="new", before="old")) == {
            "path": "/w/a.py",
            "after": "new",
            "before": "old",
        }

    def test_a_created_file_carries_no_before_key(self):
        """Absent, not empty. An empty string here would read as "the file was
        empty", which is a different fact from "the file was not there"."""
        from raven.agent.tools.base import FileChange

        assert self._payload(FileChange(path="/w/new.py", after="x")) == {"path": "/w/new.py", "after": "x"}

    def test_nothing_in_gives_nothing_out(self):
        assert self._payload(None) is None

    def test_a_malformed_change_is_dropped_rather_than_forwarded(self):
        """The guard exists for a future caller, not for the two write tools:
        a mapping with a non-string path would reach the wire and fail a client's
        own parse, which is a worse place to find out."""
        from types import SimpleNamespace

        assert self._payload(SimpleNamespace(path="/w/a.py", after=None, before=None)) is None
        assert self._payload(SimpleNamespace(path=None, after="x", before=None)) is None
        assert self._payload(SimpleNamespace(path="", after="x", before=None)) is None
        assert self._payload(SimpleNamespace()) is None

    def test_an_oversized_pair_is_dropped_whole(self):
        """The same reasoning ``_unified`` uses for an oversized diff: half a file
        reads as a smaller change than the one that happened. And a whole file
        both ways is the largest thing a tool event carries."""
        from raven.agent.loop.main import _FILE_CHANGE_MAX_CHARS
        from raven.agent.tools.base import FileChange

        big = "x" * (_FILE_CHANGE_MAX_CHARS // 2 + 10)

        assert self._payload(FileChange(path="/w/a.py", after=big, before=big)) is None
        assert self._payload(FileChange(path="/w/a.py", after="small", before=None)) is not None

    def test_the_before_length_counts_toward_the_cap(self):
        """Both halves ride the same event, so measuring only the new content
        would let a rewrite of a large file through at twice the budget."""
        from raven.agent.loop.main import _FILE_CHANGE_MAX_CHARS
        from raven.agent.tools.base import FileChange

        after = "y" * (_FILE_CHANGE_MAX_CHARS - 10)

        assert self._payload(FileChange(path="/w/a.py", after=after, before=None)) is not None
        assert self._payload(FileChange(path="/w/a.py", after=after, before="z" * 20)) is None


class TestTheLiveTranslationPathRedactsWhatItPublishes:
    """A credential in a tool's command line reached the editor verbatim.

    ``redact`` existed and was tested, but nothing on this path called it: the
    title came straight out of ``title_for`` and the preview straight out of
    ``result_preview``. The compatibility matrix said these surfaces were
    redacted, so the document and the code disagreed and the document was the
    one being believed. An editor persists its transcript, so this is not a
    momentary exposure.

    The cases below drive ``translate`` itself rather than ``redact``, because a
    passing test of the redactor is exactly what the gap hid behind.
    """

    SECRET = "sk-ant-api03-AAAABBBBCCCCDDDD"

    def test_a_credential_in_a_command_line_does_not_reach_the_title(self) -> None:
        event = {
            "type": "tool.start",
            "payload": {
                "tool_call_id": "call-1",
                "name": "exec",
                "arguments": {"command": f'curl -H "Authorization: Bearer {self.SECRET}" https://api.example.com'},
            },
        }

        (update,) = translate(event, cwd="/w").updates

        assert self.SECRET not in update["title"]
        assert "curl" in update["title"], "the row still has to be readable, so the shape survives"

    def test_a_credential_in_a_result_preview_does_not_reach_the_client(self) -> None:
        event = {
            "type": "tool.complete",
            "payload": {
                "tool_call_id": "call-1",
                "name": "read_file",
                "result_preview": f"ANTHROPIC_API_KEY={self.SECRET}\n",
            },
        }

        (update,) = translate(event).updates

        published = json.dumps(update)
        assert self.SECRET not in published
        assert "ANTHROPIC_API_KEY" in published, "redacting the label too leaves a row nobody can act on"

    def test_a_credential_in_an_error_message_does_not_reach_the_client(self) -> None:
        """Not in the review, same defect. The matrix claims an error's surviving
        text is redacted, and this path published it as message content."""
        event = {"type": "error", "payload": {"message": f"request rejected for token {self.SECRET}"}}

        (update,) = translate(event).updates

        assert self.SECRET not in json.dumps(update)

    def test_a_credential_in_a_blocked_notice_does_not_reach_the_client(self) -> None:
        """Same again: the runtime's refusal detail quotes what was refused, and
        what was refused is often the command line."""
        event = {
            "type": "notice",
            "payload": {"kind": "action_blocked", "detail": f"blocked: curl -u user:{self.SECRET} https://x"},
        }

        (update,) = translate(event).updates

        assert self.SECRET not in json.dumps(update)

    def test_the_scan_happens_before_the_preview_is_cut(self) -> None:
        """Order matters and the wrong order still passes a naive test. Cutting
        first can slice a credential so that the pattern no longer matches, and
        then the head of it is published as ordinary text."""
        filler = "x" * (MAX_RESULT_PREVIEW - 10)
        event = {
            "type": "tool.complete",
            "payload": {"tool_call_id": "c", "name": "exec", "result_preview": filler + self.SECRET},
        }

        (update,) = translate(event).updates

        # ``sk-ant-api`` is what survives if the cut lands mid-credential:
        # ``redact("token sk-ant-api")`` returns it unchanged, because a sliced
        # credential no longer matches the pattern that would have caught it.
        # Redacting first replaces the whole token, so the head is gone too.
        assert "sk-ant-api" not in json.dumps(update), "a sliced credential is still a leaked credential"


class TestOnlyTheTurnThisPromptStartedCanSettleIt:
    """A prompt was settled by whatever terminal event came past first.

    One session's subscription also carries turns the runtime submitted -- cron
    is the one in production, and ``rpc/spine.py`` handles such a turn ending
    while a client's turn is still queued behind it. Settlement read no
    ``turn_id``, so that foreign ending answered the client's ``session/prompt``:
    the editor is told the turn is over before its own turn starts, and the real
    output then arrives after the request it belonged to has ended.

    ``turn.send`` returns the id of the turn it accepted, which is the only
    reliable answer to "which turn is mine", so that is what settlement is keyed
    on. The ordering wrinkle is real and tested below: ``message.start`` is
    emitted inside ``turn.send`` before it returns, so events can arrive before
    the id is known.
    """

    async def test_a_foreign_turns_ending_does_not_answer_this_prompt(self):
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        future = translator.begin_turn("acp:s1")
        translator.accept_turn("acp:s1", "mine")

        await translator.send_frame(_event("message.complete", turn_id="runtime-turn"))

        assert not future.done(), "a cron turn ending is not this prompt's answer"

        await translator.send_frame(_event("message.complete", turn_id="mine"))

        assert await future == "end_turn"

    async def test_a_foreign_refusal_does_not_latch_onto_this_turn(self):
        """The latch is the same defect one step earlier: a refusal recorded from
        another turn changes the stop reason this prompt eventually reports."""
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        future = translator.begin_turn("acp:s1")
        translator.accept_turn("acp:s1", "mine")

        await translator.send_frame(_event("notice", kind="action_blocked", detail="no", turn_id="runtime-turn"))
        await translator.send_frame(_event("message.complete", turn_id="mine"))

        assert await future == "end_turn", "the refusal belonged to another turn"

    async def test_an_ending_that_arrives_before_the_id_is_known_still_answers(self):
        """``turn.send`` emits ``message.start`` before it returns, so a turn can
        finish before the caller learns its id. Dropping that ending would hang
        the prompt, which is worse than the bug being fixed."""
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        future = translator.begin_turn("acp:s1")

        await translator.send_frame(_event("message.complete", turn_id="mine"))
        assert not future.done(), "nothing can be attributed yet"

        translator.accept_turn("acp:s1", "mine")

        assert await future == "end_turn"

    async def test_a_foreign_ending_held_from_before_the_id_is_never_applied(self):
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        future = translator.begin_turn("acp:s1")

        await translator.send_frame(_event("message.complete", turn_id="runtime-turn"))
        translator.accept_turn("acp:s1", "mine")

        assert not future.done()

        await translator.send_frame(_event("message.complete", turn_id="mine"))

        assert await future == "end_turn"

    async def test_an_ending_with_no_turn_id_still_answers(self):
        """An emitter that does not know the turn id leaves the field empty. That
        cannot be attributed either way, and refusing to settle would hang a
        prompt over a shape that predates this correlation."""
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        future = translator.begin_turn("acp:s1")
        translator.accept_turn("acp:s1", "mine")

        await translator.send_frame(_event("message.complete"))

        assert await future == "end_turn"

    async def test_an_event_that_is_not_a_mapping_carries_no_turn(self):
        """``send_frame`` does not vet the event body, and ``translate`` returns
        nothing for a non-mapping rather than raising. The correlation has to be
        just as incurious, or a malformed frame becomes an exception inside the
        emitter's coalesce task."""
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        future = translator.begin_turn("acp:s1")
        translator.accept_turn("acp:s1", "mine")

        await translator.send_frame(
            {"jsonrpc": "2.0", "method": "event", "params": {"subscription_id": "sub-1", "event": "not a mapping"}}
        )
        await translator.send_frame(
            {"jsonrpc": "2.0", "method": "event", "params": {"subscription_id": "sub-1", "event": {"payload": 7}}}
        )

        assert not future.done(), "neither frame says anything about any turn"

    def test_accepting_a_turn_for_a_session_with_none_open_is_a_no_op(self):
        """``session/prompt`` calls this after ``turn.send`` answers, and the turn
        can already be gone by then -- a cancel, or the connection closing."""
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())

        translator.accept_turn("acp:s1", "mine")
        translator.accept_turn("acp:nope", "mine")

    async def test_the_held_endings_do_not_grow_without_bound(self):
        """The hold exists for one narrow window. A stream of foreign turns must
        not turn it into a leak that lives as long as the connection."""
        translator = UpdateTranslator(emit=lambda f: None)
        translator.add(_session())
        translator.begin_turn("acp:s1")

        for index in range(200):
            await translator.send_frame(_event("message.complete", turn_id=f"other-{index}"))

        session = translator.get("acp:s1")
        assert session is not None and session.turn is not None
        assert len(session.turn.deferred) <= MAX_DEFERRED_ENDINGS
