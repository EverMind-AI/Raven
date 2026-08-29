"""Unit tests for the pure elicitation schema layer."""

import json
from pathlib import Path

from raven.agent.acp_client import autofill

_FIXTURE = Path(__file__).parent / "fixtures" / "acp_elicitation_askuser.json"


def test_a_real_askuser_request_parses() -> None:
    from raven.agent.acp_client.elicitation import parse_request

    ask = parse_request(json.loads(_FIXTURE.read_text()))
    assert ask is not None
    assert ask.mode == "form"
    assert ask.session_id
    assert ask.message


def test_fields_keep_the_schema_write_order() -> None:
    from raven.agent.acp_client.elicitation import fields

    schema = {
        "type": "object",
        "properties": {"b": {"type": "string"}, "a": {"type": "string"}, "c": {"type": "string"}},
    }
    assert [f.name for f in fields(schema)] == ["b", "a", "c"]


def test_an_enum_becomes_options_and_oneof_carries_labels() -> None:
    from raven.agent.acp_client.elicitation import fields

    schema = {
        "type": "object",
        "properties": {
            "pick": {"type": "string", "enum": ["redis", "memcached"]},
            "also": {"type": "string", "oneOf": [{"const": "yes", "description": "do it"}, {"const": "no"}]},
        },
    }
    got = {f.name: f.options for f in fields(schema)}
    assert got["pick"] == ["redis", "memcached"]
    assert got["also"] == ["yes", "no"]


def test_a_multi_select_reads_its_choices_off_items() -> None:
    from raven.agent.acp_client.elicitation import fields

    schema = {
        "type": "object",
        "properties": {"tags": {"type": "array", "items": {"anyOf": [{"const": "a"}, {"const": "b"}]}}},
    }
    assert fields(schema)[0].options == ["a", "b"]


def test_required_is_read_from_the_schema() -> None:
    from raven.agent.acp_client.elicitation import fields

    schema = {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    assert {f.name: f.required for f in fields(schema)} == {"a": True, "b": False}


def test_the_prompt_prefers_title_then_description_then_the_name() -> None:
    from raven.agent.acp_client.elicitation import fields

    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "string", "title": "T", "description": "D"},
            "b": {"type": "string", "description": "D"},
            "c": {"type": "string"},
        },
    }
    assert [f.prompt for f in fields(schema)] == ["T", "D", "c"]


def _field(kind, **kw):
    from raven.agent.acp_client.elicitation import Field

    return Field(name="x", prompt="x", type=kind, options=kw.pop("options", []), required=False, constraints=kw)


def test_coerce_handles_all_five_wire_types() -> None:
    from raven.agent.acp_client.elicitation import coerce

    assert coerce(_field("string"), "hi") == (True, "hi")
    assert coerce(_field("integer"), "7") == (True, 7)
    assert coerce(_field("number"), "1.5") == (True, 1.5)
    assert coerce(_field("boolean"), "yes") == (True, True)
    assert coerce(_field("boolean"), "no") == (True, False)
    assert coerce(_field("array", options=["a", "b"]), "a, b") == (True, ["a", "b"])


def test_coerce_rejects_what_does_not_fit() -> None:
    from raven.agent.acp_client.elicitation import coerce

    assert coerce(_field("integer"), "seven") == (False, None)
    assert coerce(_field("string", pattern=r"^v\d+$"), "nope") == (False, None)
    assert coerce(_field("string", options=["a"]), "b") == (False, None)
    assert coerce(_field("array", options=["a"]), "a, zzz") == (False, None)
    assert coerce(_field("integer", minimum=5), "1") == (False, None)


def test_a_malformed_schema_yields_no_fields() -> None:
    from raven.agent.acp_client.elicitation import fields

    assert fields({}) == []
    assert fields({"type": "object", "properties": "nope"}) == []
    assert fields({"type": "object", "properties": {"a": {"type": "wat"}}}) == []


def test_url_and_unknown_modes_parse_but_are_not_form() -> None:
    from raven.agent.acp_client.elicitation import parse_request

    url = parse_request({"message": "m", "mode": "url", "url": "https://x", "elicitationId": "e"})
    assert url is not None and url.mode == "url"
    other = parse_request({"message": "m", "mode": "_custom"})
    assert other is not None and other.mode == "_custom"
    assert parse_request({"mode": "form"}) is None


def test_the_response_builders_match_the_protocol() -> None:
    from raven.agent.acp_client.elicitation import accept, cancel, decline

    assert accept({"a": 1}) == {"action": "accept", "content": {"a": 1}}
    assert decline() == {"action": "decline"}
    assert cancel() == {"action": "cancel"}


def test_no_asker_bound_reads_as_unavailable() -> None:
    from raven.agent.acp_client.asker import current_ask

    assert current_ask() == (None, "")


async def test_a_bound_asker_is_visible_to_a_child_task() -> None:
    """A sub-agent run is a background task; ContextVars copy into it."""
    import asyncio

    from raven.agent.acp_client.asker import current_ask, start_ask_turn

    class Tool:
        async def ask(self, prompt, choices, conversation_id):
            return f"{conversation_id}:{prompt}"

    start_ask_turn(Tool(), conversation_id="tui:c1")

    async def child():
        asker, cid = current_ask()
        assert asker is not None
        return await asker.ask("q?", None, cid)

    assert await asyncio.create_task(child()) == "tui:c1:q?"


async def test_a_form_is_asked_field_by_field_and_assembled() -> None:
    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor

    asked: list[str] = []

    class Tool:
        async def ask(self, prompt, choices, conversation_id, **_):
            asked.append(prompt)
            return "redis" if choices else "42"

    start_ask_turn(Tool(), conversation_id="tui:c1")
    got = await Elicitor("Coder", "api-refactor").elicit(
        {
            "sessionId": "s",
            "mode": "form",
            "message": "set up a cache",
            "requestedSchema": {
                "type": "object",
                "properties": {
                    "backend": {"type": "string", "enum": ["redis", "memcached"]},
                    "ttl": {"type": "integer"},
                },
            },
        }
    )
    assert got == {"action": "accept", "content": {"backend": "redis", "ttl": 42}}
    assert len(asked) == 2


async def test_the_prompt_names_the_agent_that_asked() -> None:
    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor

    seen: list[str] = []

    class Tool:
        async def ask(self, prompt, choices, conversation_id, **_):
            seen.append(prompt)
            return "x"

    start_ask_turn(Tool(), conversation_id="tui:c1")
    await Elicitor("Coder", "api-refactor").elicit(
        {
            "sessionId": "s",
            "mode": "form",
            "message": "which backend?",
            "requestedSchema": {"type": "object", "properties": {"b": {"type": "string"}}},
        }
    )
    assert seen[0].startswith("Coder(api-refactor): ")
    assert "which backend?" in seen[0]


async def test_no_asker_declines() -> None:
    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor

    start_ask_turn(None, conversation_id="")
    got = await Elicitor("Coder", "h").elicit(
        {
            "sessionId": "s",
            "mode": "form",
            "message": "m",
            "requestedSchema": {"type": "object", "properties": {"b": {"type": "string"}}},
        }
    )
    assert got == {"action": "decline"}


async def test_a_skipped_optional_field_is_omitted_and_a_required_one_declines() -> None:
    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor

    class Silent:
        async def ask(self, prompt, choices, conversation_id, **_):
            return ""

    start_ask_turn(Silent(), conversation_id="tui:c1")
    optional = await Elicitor("A", "h").elicit(
        {
            "sessionId": "s",
            "mode": "form",
            "message": "m",
            "requestedSchema": {"type": "object", "properties": {"b": {"type": "string"}}},
        }
    )
    assert optional == {"action": "accept", "content": {}}

    required = await Elicitor("A", "h").elicit(
        {
            "sessionId": "s",
            "mode": "form",
            "message": "m",
            "requestedSchema": {"type": "object", "required": ["b"], "properties": {"b": {"type": "string"}}},
        }
    )
    assert required == {"action": "decline"}


async def test_a_bad_answer_is_re_asked_then_declines() -> None:
    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitation import MAX_FIELD_RETRIES
    from raven.agent.acp_client.elicitor import Elicitor

    tries = 0

    class Wrong:
        async def ask(self, prompt, choices, conversation_id, **_):
            nonlocal tries
            tries += 1
            return "not-a-number"

    start_ask_turn(Wrong(), conversation_id="tui:c1")
    got = await Elicitor("A", "h").elicit(
        {
            "sessionId": "s",
            "mode": "form",
            "message": "m",
            "requestedSchema": {"type": "object", "required": ["n"], "properties": {"n": {"type": "integer"}}},
        }
    )
    assert got == {"action": "decline"}
    assert tries == MAX_FIELD_RETRIES + 1


async def test_an_unavailable_round_trip_declines_rather_than_accepting_nothing() -> None:
    """`ask_direct` returns None when there is no broker or no conversation.

    Nothing was put to anybody, so an `accept` with empty content would tell the
    agent a human chose to answer nothing. Distinct from a skip, which is a
    decision the user actually made.
    """
    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor

    class NoPath:
        async def ask(self, prompt, choices, conversation_id, **_):
            return None

    start_ask_turn(NoPath(), conversation_id="tui:c1")
    got = await Elicitor("A", "h").elicit(
        {
            "sessionId": "s",
            "mode": "form",
            "message": "m",
            "requestedSchema": {"type": "object", "properties": {"b": {"type": "string"}}},
        }
    )
    assert got == {"action": "decline"}


async def test_a_cancelled_turn_answers_cancel_not_decline() -> None:
    """`cancel` aborts the agent's tool call, which is what a cancelled turn is.

    Answered rather than propagated: an unanswered request leaves the agent's
    turn pending for the life of the session.
    """
    import asyncio

    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor

    class Cancels:
        async def ask(self, prompt, choices, conversation_id, **_):
            raise asyncio.CancelledError

    start_ask_turn(Cancels(), conversation_id="tui:c1")
    got = await Elicitor("A", "h").elicit(
        {
            "sessionId": "s",
            "mode": "form",
            "message": "m",
            "requestedSchema": {"type": "object", "properties": {"b": {"type": "string"}}},
        }
    )
    assert got == {"action": "cancel"}


async def test_a_cancelled_run_stops_the_form_rather_than_skipping_one_field() -> None:
    """`cancel` has to stop the form, not just the question standing on screen.

    `QuestionBroker.await_question` answers a cancellation with the question's
    default and never raises, so cancelling the waiting task alone arrives inside
    `_one` as an empty answer -- which is a user's skip. The form would go on to
    put its next field to a user whose run is already gone.
    """
    import asyncio

    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor

    asked: list[str] = []
    parked = asyncio.Event()

    class Parks:
        async def ask(self, prompt, choices, conversation_id, **_):
            asked.append(prompt)
            parked.set()
            # Answered rather than parked, so a form that went on after the
            # cancel reports below instead of parking the suite.
            if len(asked) > 1:
                return "late"
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                # What the broker does with a cancellation: its own default.
                return ""

    form = {
        "sessionId": "s",
        "mode": "form",
        "message": "m",
        "requestedSchema": {"type": "object", "properties": {"b": {"type": "string"}, "c": {"type": "string"}}},
    }
    start_ask_turn(Parks(), conversation_id="tui:c-cancel")
    elicitor = Elicitor("A", "h")
    task = asyncio.create_task(elicitor.elicit(dict(form)))
    await parked.wait()

    elicitor.cancel()

    assert await task == {"action": "cancel"}
    assert len(asked) == 1, asked

    # And the conversation is free again. The lock is held for a whole form, so
    # a cancelled one that kept it would park every later question there until
    # `LOCK_WAIT_SECONDS`.
    class Answers:
        async def ask(self, prompt, choices, conversation_id, **_):
            return "x"

    start_ask_turn(Answers(), conversation_id="tui:c-cancel")
    got = await asyncio.wait_for(Elicitor("B", "h").elicit(dict(form)), 1.0)
    assert got == {"action": "accept", "content": {"b": "x", "c": "x"}}


async def test_a_retracted_form_stops_rather_than_putting_its_next_field_up() -> None:
    """The agent took its question back, so the rest of the form is not ours to ask.

    Cancelling the answering task is all an inbound `$/cancel_request` can do, and
    `QuestionBroker.await_question` answers a cancellation with the question's
    default rather than propagating one -- so it arrives inside `_one` as an empty
    answer, which is a user's skip. Read that way the form goes on to put its next
    field to a run that has already stopped listening.
    """
    import asyncio

    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor

    asked: list[str] = []
    parked = asyncio.Event()

    class Parks:
        async def ask(self, prompt, choices, conversation_id, **_):
            asked.append(prompt)
            parked.set()
            # Answered rather than parked, so a form that went on after the
            # retraction reports below instead of parking the suite.
            if len(asked) > 1:
                return "late"
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                # What the broker does with a cancellation: its own default.
                return ""

    start_ask_turn(Parks(), conversation_id="tui:c-retract")
    task = asyncio.create_task(
        Elicitor("A", "h").elicit(
            {
                "sessionId": "s",
                "mode": "form",
                "message": "m",
                "requestedSchema": {"type": "object", "properties": {"b": {"type": "string"}, "c": {"type": "string"}}},
            }
        )
    )
    await parked.wait()

    task.cancel()

    assert await task == {"action": "cancel"}
    assert len(asked) == 1, asked


async def test_url_and_unknown_modes_decline() -> None:
    from raven.agent.acp_client.elicitor import Elicitor

    for params in (
        {"sessionId": "s", "mode": "url", "url": "https://x", "elicitationId": "e", "message": "m"},
        {"sessionId": "s", "mode": "_weird", "message": "m"},
    ):
        assert await Elicitor("A", "h").elicit(params) == {"action": "decline"}


async def test_two_forms_on_one_conversation_are_serialised() -> None:
    """The lock covers a whole form, not one field of it.

    Without any lock the broker fail-safes the stale question to its default, so
    one agent's question is silently dropped and never seen by anyone. Per field
    would fix that and still interleave two forms' questions in one conversation,
    which reads to the user as one agent asking half of two different things.
    Peak concurrency cannot tell the two apart -- per-field locking never
    overlaps two asks either -- so the assertion is on the order they were put
    in: every field of one form before any field of the other.
    """
    import asyncio

    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor

    asked: list[str] = []
    inflight = peak = 0

    class Slow:
        async def ask(self, prompt, choices, conversation_id, **_):
            nonlocal inflight, peak
            asked.append(prompt)
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.05)
            inflight -= 1
            return "x"

    start_ask_turn(Slow(), conversation_id="tui:c1")
    form = {
        "sessionId": "s",
        "mode": "form",
        "message": "m",
        "requestedSchema": {"type": "object", "properties": {"b": {"type": "string"}, "c": {"type": "string"}}},
    }
    both = await asyncio.gather(Elicitor("A", "h1").elicit(dict(form)), Elicitor("B", "h2").elicit(dict(form)))
    assert peak == 1
    assert all(r == {"action": "accept", "content": {"b": "x", "c": "x"}} for r in both)
    owners = [prompt.split("(", 1)[0] for prompt in asked]
    assert owners in (["A", "A", "B", "B"], ["B", "B", "A", "A"]), owners


def test_a_conversation_lock_does_not_outlive_the_loop_that_contended_it() -> None:
    """Two sequential runs, each racing two forms on the same conversation.

    Deliberately not an async test: the point is two different event loops, and
    pytest-asyncio hands a test exactly one.

    An `asyncio.Lock` binds itself to the first loop that *contends* it and never
    unbinds, so one lock per conversation for the whole process hands the second
    loop a lock whose acquire raises `RuntimeError` -- which `elicit` can only
    turn into a silent decline. No overlap between the loops is needed, only
    reuse of the conversation key, which every test in this file already does.
    """
    import asyncio

    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor

    class Slow:
        async def ask(self, prompt, choices, conversation_id, **_):
            await asyncio.sleep(0.01)
            return "x"

    form = {
        "sessionId": "s",
        "mode": "form",
        "message": "m",
        "requestedSchema": {"type": "object", "properties": {"b": {"type": "string"}}},
    }

    async def race() -> list[dict]:
        start_ask_turn(Slow(), conversation_id="tui:c1")
        return await asyncio.gather(Elicitor("A", "h1").elicit(dict(form)), Elicitor("B", "h2").elicit(dict(form)))

    accepted = [{"action": "accept", "content": {"b": "x"}}] * 2
    assert asyncio.run(race()) == accepted
    assert asyncio.run(race()) == accepted


async def test_a_form_kept_waiting_for_the_conversation_declines(monkeypatch) -> None:
    """A queued form gives up rather than parking the conversation behind a stuck one.

    The holder takes one human round trip per field and each of those fail-safes
    only at the broker's own timeout, so an unattended form would otherwise block
    every other agent's question in that conversation for a multiple of it. The
    detach at the end of a turn does not cancel a task blocked on the lock
    either, so the wait can outlive the turn that asked.
    """
    import asyncio

    from raven.agent.acp_client import elicitor as elicitor_module
    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor

    class Parked:
        async def ask(self, prompt, choices, conversation_id, **_):
            await asyncio.sleep(0.2)
            return "x"

    monkeypatch.setattr(elicitor_module, "LOCK_WAIT_SECONDS", 0.01)
    start_ask_turn(Parked(), conversation_id="tui:c1")
    form = {
        "sessionId": "s",
        "mode": "form",
        "message": "m",
        "requestedSchema": {"type": "object", "properties": {"b": {"type": "string"}}},
    }
    holder = asyncio.create_task(Elicitor("A", "h1").elicit(dict(form)))
    # One turn of the loop is all the holder needs to take the lock and park on
    # its first question; without it the queued form could win the race.
    await asyncio.sleep(0)
    assert await Elicitor("B", "h2").elicit(dict(form)) == {"action": "decline"}
    assert await holder == {"action": "accept", "content": {"b": "x"}}


def _paired_schema(kind: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "tags": {"type": kind, "items": {"enum": ["a", "b"]}, "title": "Pick tags"},
            "tags_custom": {"type": "string", "title": "Other"},
        },
    }


async def test_a_valid_multi_select_answer_lands_on_the_field_not_the_custom_box() -> None:
    """`coerce` returns a `list[str]` for an array field. Comparing that whole
    list against `options` (a `list[str]`) asks whether the list equals one of
    the option strings, which is never true, so every valid multi-select was
    routed to the custom box regardless of whether it was actually off-enum.
    """
    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor
    from raven.agent.subagent.acp_dialects.claude_code import ClaudeCodeDialect

    class Tool:
        async def ask(self, prompt, choices, conversation_id, **_):
            return "a, b"

    start_ask_turn(Tool(), conversation_id="tui:c1")
    got = await Elicitor("A", "h", dialect=ClaudeCodeDialect()).elicit(
        {"sessionId": "s", "mode": "form", "message": "m", "requestedSchema": _paired_schema("array")}
    )
    assert got == {"action": "accept", "content": {"tags": ["a", "b"]}}


async def test_an_off_enum_multi_select_item_still_routes_to_the_custom_box() -> None:
    """A genuinely off-enum multi-select must still land in `tags_custom`, so
    the fix to the list-membership check must not swing the other way and
    start treating every multi-select as on-enum.
    """
    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor
    from raven.agent.subagent.acp_dialects.claude_code import ClaudeCodeDialect

    class Tool:
        async def ask(self, prompt, choices, conversation_id, **_):
            return "a, made-up"

    start_ask_turn(Tool(), conversation_id="tui:c1")
    got = await Elicitor("A", "h", dialect=ClaudeCodeDialect()).elicit(
        {"sessionId": "s", "mode": "form", "message": "m", "requestedSchema": _paired_schema("array")}
    )
    assert got == {"action": "accept", "content": {"tags_custom": ["a", "made-up"]}}


async def test_a_required_paired_field_declines_rather_than_dropping_its_key() -> None:
    """The enum property's own schema allows only its offered consts, so an
    "Other" answer can never satisfy both that and `required` at once. Writing
    only the custom key would still hand back content the requested schema
    rejects for missing the required one -- exactly what the `invalid` status
    exists to prevent.
    """
    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor
    from raven.agent.subagent.acp_dialects.claude_code import ClaudeCodeDialect

    class Tool:
        async def ask(self, prompt, choices, conversation_id, **_):
            return "sqlite"

    start_ask_turn(Tool(), conversation_id="tui:c1")
    got = await Elicitor("A", "h", dialect=ClaudeCodeDialect()).elicit(
        {
            "sessionId": "s",
            "mode": "form",
            "message": "m",
            "requestedSchema": {
                "type": "object",
                "required": ["backend"],
                "properties": {
                    "backend": {
                        "type": "string",
                        "title": "Backend",
                        "oneOf": [{"const": "redis"}, {"const": "memcached"}],
                    },
                    "backend_custom": {"type": "string", "title": "Other"},
                },
            },
        }
    )
    assert got == {"action": "decline"}


async def test_a_paired_question_is_asked_once_and_answered_either_way() -> None:
    """The point of pairing: one prompt for what the schema still sends as two
    properties, landing in whichever key matches what the user actually typed.
    """
    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor
    from raven.agent.subagent.acp_dialects.claude_code import ClaudeCodeDialect

    schema = {
        "type": "object",
        "properties": {
            "backend": {
                "type": "string",
                "title": "Backend",
                "oneOf": [{"const": "redis"}, {"const": "memcached"}],
            },
            "backend_custom": {"type": "string", "title": "Other"},
        },
    }
    asked: list[str] = []

    class PicksEnum:
        async def ask(self, prompt, choices, conversation_id, **_):
            asked.append(prompt)
            return "redis"

    start_ask_turn(PicksEnum(), conversation_id="tui:c1")
    picked = await Elicitor("A", "h", dialect=ClaudeCodeDialect()).elicit(
        {"sessionId": "s", "mode": "form", "message": "m", "requestedSchema": schema}
    )
    assert picked == {"action": "accept", "content": {"backend": "redis"}}
    assert len(asked) == 1

    class TypesOther:
        async def ask(self, prompt, choices, conversation_id, **_):
            return "sqlite"

    start_ask_turn(TypesOther(), conversation_id="tui:c1")
    typed = await Elicitor("A", "h", dialect=ClaudeCodeDialect()).elicit(
        {"sessionId": "s", "mode": "form", "message": "m", "requestedSchema": schema}
    )
    assert typed == {"action": "accept", "content": {"backend_custom": "sqlite"}}


def test_the_real_askuser_request_still_merges_into_one_question() -> None:
    """The captured frame is the one shape that must never stop merging.

    Its free-text half carries the adapter's own pairing marker and no
    `required` list constrains either property, so every pairing rule has to
    agree on folding this one.
    """
    from raven.agent.acp_client.elicitation import fields
    from raven.agent.subagent.acp_dialects.claude_code import ClaudeCodeDialect

    merged = ClaudeCodeDialect().pair_fields(fields(json.loads(_FIXTURE.read_text())["requestedSchema"]))
    assert [f.name for f in merged] == ["question_0"]
    assert merged[0].custom_name == "question_0_custom"
    assert merged[0].options == ["Redis", "In-memory (process-local)", "Disk / SQLite", "Memcached"]


async def test_a_required_custom_box_is_asked_rather_than_folded_away() -> None:
    """Pairing keeps only the enum half's `required`, so a schema that requires
    the free-text half must not be paired at all: an on-enum answer would
    otherwise be accepted as content missing a key the `requestedSchema`
    demands, which is the one thing no merge is allowed to cost.
    """
    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor
    from raven.agent.subagent.acp_dialects.claude_code import ClaudeCodeDialect

    asked: list[str] = []

    class Tool:
        async def ask(self, prompt, choices, conversation_id, **_):
            asked.append(prompt)
            return "redis" if choices else "an in-house store"

    start_ask_turn(Tool(), conversation_id="tui:c1")
    got = await Elicitor("A", "h", dialect=ClaudeCodeDialect()).elicit(
        {
            "sessionId": "s",
            "mode": "form",
            "message": "m",
            "requestedSchema": {
                "type": "object",
                "required": ["backend_custom"],
                "properties": {
                    "backend": {
                        "type": "string",
                        "title": "Backend",
                        "oneOf": [{"const": "redis"}, {"const": "memcached"}],
                    },
                    "backend_custom": {"type": "string", "title": "Other"},
                },
            },
        }
    )
    assert got == {"action": "accept", "content": {"backend": "redis", "backend_custom": "an in-house store"}}
    assert len(asked) == 2


def test_raven_advertises_form_elicitation_and_never_url() -> None:
    """`url` elicitation exists for out-of-band credential and payment flows, so
    advertising it would let a sub-agent send the user to any URL to enter them.
    The two sub-capabilities are independently advertisable."""
    from raven.agent.acp_client.protocol import CLIENT_CAPABILITIES

    assert CLIENT_CAPABILITIES["elicitation"] == {"form": {}}
    assert "url" not in CLIENT_CAPABILITIES["elicitation"]


# ---- question autofill ------------------------------------------------------


class _RecordingAsker:
    """The `Asker` half of the round trip, without a broker."""

    def __init__(self, answers: dict | None = None) -> None:
        self.asked: list[tuple[str, list[str] | None]] = []
        self.kwargs: list[dict] = []
        self._answers = answers or {}

    async def ask(self, prompt, choices, conversation_id, *, index=0, total=1, batch=None):
        self.asked.append((prompt, choices))
        self.kwargs.append({"index": index, "total": total, "batch": batch})
        for key, value in self._answers.items():
            if key is not None and key in prompt:
                return value
        return self._answers.get(None, "")


class _StubAutofill:
    """Whatever the resolver would have decided, without a model.

    Keyed by field name; `""` is the single-question route's key.
    """

    def __init__(self, decisions: dict[str, tuple[str, str]]) -> None:
        self._decisions = decisions
        self.calls: list[list] = []

    async def resolve(self, questions, *, agent, instance):
        self.calls.append(questions)
        out = []
        for question in questions:
            status, payload = self._decisions.get(question.key, ("defer", ""))
            if status == "answer":
                out.append(autofill.Resolution(status="answer", answer=payload))
            elif status == "partial":
                out.append(autofill.Resolution(status="partial", known=payload))
            else:
                out.append(autofill.Resolution(status="defer"))
        return out


def _form(fields: list[str], types: dict[str, str] | None = None) -> dict:
    types = types or {}
    properties = {name: {"type": types.get(name, "string"), "description": f"Which {name}?"} for name in fields}
    return {
        "sessionId": "s",
        "mode": "form",
        "message": "m",
        "requestedSchema": {"type": "object", "properties": properties},
    }


async def _elicit_with(asker, autofill_obj, *, fields: list[str], types: dict[str, str] | None = None) -> dict:
    """One form, through a real `Elicitor`, with the turn bound as a turn binds it.

    Bound before the `Elicitor` is constructed because that is where both the
    asker and the autofill are read; bound after, the elicitor would have
    neither.
    """
    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor

    start_ask_turn(asker, autofill_obj, conversation_id="tui:c1")
    return await Elicitor("raven-code", "a1b2").elicit(_form(fields, types))


async def test_a_fully_answered_form_never_reaches_the_asker() -> None:
    asker = _RecordingAsker()
    stub_autofill = _StubAutofill({"branch": ("answer", "feat/x")})
    result = await _elicit_with(asker, stub_autofill, fields=["branch"])
    assert result == {"action": "accept", "content": {"branch": "feat/x"}}
    assert asker.asked == []


async def test_a_fully_answered_form_never_takes_the_conversation_lock() -> None:
    import asyncio

    from raven.agent.acp_client.asker import question_lock

    lock = question_lock("tui:c1")
    await lock.acquire()
    try:
        stub_autofill = _StubAutofill({"branch": ("answer", "feat/x")})
        # Held by someone else for the whole call: a form that needs nobody must
        # still complete rather than wait out LOCK_WAIT_SECONDS.
        result = await asyncio.wait_for(_elicit_with(_RecordingAsker(), stub_autofill, fields=["branch"]), timeout=1.0)
    finally:
        lock.release()
    assert result["action"] == "accept"


async def test_only_the_deferred_fields_are_asked() -> None:
    asker = _RecordingAsker(answers={"Which reviewer?": "chandler"})
    stub_autofill = _StubAutofill({"branch": ("answer", "feat/x"), "reviewer": ("defer", "")})
    result = await _elicit_with(asker, stub_autofill, fields=["branch", "reviewer"])
    assert [p for p, _ in asker.asked] == ["raven-code(a1b2): m - Which reviewer?"]
    assert result["content"] == {"branch": "feat/x", "reviewer": "chandler"}


async def test_the_leftovers_are_asked_as_one_batch() -> None:
    asker = _RecordingAsker(answers={"Which reviewer?": "chandler", "Which milestone?": "m1"})
    stub_autofill = _StubAutofill(
        {"branch": ("answer", "feat/x"), "reviewer": ("defer", ""), "milestone": ("defer", "")}
    )
    await _elicit_with(asker, stub_autofill, fields=["branch", "reviewer", "milestone"])
    # Two of three, positioned among themselves: what the user is taken through
    # is the leftovers, not the form the sub-agent sent.
    assert [kw["total"] for kw in asker.kwargs] == [2, 2]
    assert [kw["index"] for kw in asker.kwargs] == [0, 1]
    assert asker.kwargs[0]["batch"] == [
        {"question": "Which reviewer?"},
        {"question": "Which milestone?"},
    ]


async def test_a_partial_carries_its_note_and_leaves_the_question_intact() -> None:
    asker = _RecordingAsker(answers={None: "feat/x"})
    stub_autofill = _StubAutofill({"branch": ("partial", "you said feat/x earlier")})
    await _elicit_with(asker, stub_autofill, fields=["branch"])
    prompt = asker.asked[0][0]
    assert prompt.startswith("raven-code(a1b2): m - Which branch?")
    assert "you said feat/x earlier" in prompt


async def test_an_answer_that_does_not_fit_the_schema_asks_the_user_once() -> None:
    asker = _RecordingAsker(answers={None: "7"})
    stub_autofill = _StubAutofill({"count": ("answer", "not-a-number")})
    result = await _elicit_with(asker, stub_autofill, fields=["count"], types={"count": "integer"})
    # Downgraded to a question, not retried against the resolver: the field's
    # retry budget belongs to the user's answers.
    assert len(asker.asked) == 1
    assert len(stub_autofill.calls) == 1
    assert result["content"] == {"count": 7}


async def test_a_fully_answered_form_does_not_accept_for_a_run_that_ended() -> None:
    """A form answered in full still has to notice its run is gone.

    `_resolve` awaits a model call, and `cancel` can land during it. Before this
    path existed the first `_one` was that await and the check after it caught
    this; skipping the questions must not also skip the check.

    The swallow below is what makes the flag load-bearing rather than the raised
    cancellation: `cancel` marks the run *and* cancels the waiting task, and a
    cancellation absorbed downstream -- which is what `QuestionBroker` does to
    the one on the asking side -- leaves only the flag and the request count.
    """
    import asyncio
    import contextlib

    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor

    held: dict = {}

    class _CancellingAutofill:
        async def resolve(self, questions, *, agent, instance):
            held["elicitor"].cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await asyncio.sleep(0)
            return [autofill.Resolution(status="answer", answer="feat/x") for _ in questions]

    asker = _RecordingAsker()
    start_ask_turn(asker, _CancellingAutofill(), conversation_id="tui:c1")
    held["elicitor"] = Elicitor("raven-code", "a1b2")
    result = await held["elicitor"].elicit(_form(["branch"]))

    assert result == {"action": "cancel"}
    assert asker.asked == []


async def test_with_no_autofill_bound_every_field_is_asked() -> None:
    asker = _RecordingAsker(answers={None: "x"})
    result = await _elicit_with(asker, None, fields=["branch", "reviewer"])
    assert len(asker.asked) == 2
    assert result["action"] == "accept"


async def test_the_autofill_is_read_at_construction_not_when_the_form_lands() -> None:
    """The autofill must be read at construction, in the run's own context.

    A form arrives on the connection's read loop, whose ContextVars are a copy
    of whichever turn first opened the connection, and the pool keeps that
    connection for the life of the process. The rebinding below stands for that
    drift: an elicitor reading at resolve time would see the newest turn's
    autofill -- here, none -- and ask a field raven could have filled.
    """
    from raven.agent.acp_client.asker import start_ask_turn
    from raven.agent.acp_client.elicitor import Elicitor

    asker = _RecordingAsker(answers={None: "asked the user"})
    start_ask_turn(asker, _StubAutofill({"branch": ("answer", "feat/x")}), conversation_id="tui:c1")
    elicitor = Elicitor("raven-code", "a1b2")
    start_ask_turn(asker, None, conversation_id="tui:c1")
    result = await elicitor.elicit(_form(["branch"]))

    assert result == {"action": "accept", "content": {"branch": "feat/x"}}
    assert asker.asked == []
