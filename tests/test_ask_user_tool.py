"""Tests for the ask_user tool's display contract (raven/agent/tools/ask_user.py).

``ask_user`` is the first tool to return :class:`ToolResult`, so this pins the
split it introduces: ``model_text`` keeps the natural-language phrasing the
model reads, ``display_text`` carries the question/answer pairing the transcript
renders, and ``display_call`` labels the row with the question rather than the
raw arguments blob.
"""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from raven.agent.tools.ask_user import (
    _MAX_JSON_LAYERS,
    AskUserTool,
    _normalize_options,
    _normalize_questions,
)
from raven.agent.tools.registry import ToolRegistry
from raven.contracts.tool import ToolResult


class _StubBroker:
    """Stands in for QuestionBroker: replies from a scripted answer map."""

    default_timeout_s = 600.0

    def __init__(self, answers: dict[str, str], *, delay_s: float = 0.0) -> None:
        self.answers = answers
        self.asked: list[tuple[str, list[str]]] = []
        self.calls: list[dict] = []
        self._delay_s = delay_s

    async def await_question(self, cid: str, *, prompt: str, choices: list[str], **kwargs) -> str:
        self.asked.append((prompt, choices))
        self.calls.append({"prompt": prompt, "choices": choices, **kwargs})
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        return self.answers.get(prompt, "")


def _tool(answers: dict[str, str], **kw) -> tuple[AskUserTool, _StubBroker]:
    broker = _StubBroker(answers, **kw)
    tool = AskUserTool(broker=broker, conversation_id="tui:test")  # type: ignore[arg-type]
    return tool, broker


@pytest.mark.asyncio
async def test_single_question_splits_model_and_display_text():
    tool, broker = _tool({"Which package manager?": "uv"})

    result = await tool.execute(questions=[{"question": "Which package manager?", "options": ["uv", "pip"]}])

    assert isinstance(result, ToolResult)
    # The model reads the full sentence, including the question it asked.
    assert 'User answered: "Which package manager?" -> "uv".' in result.model_text
    assert result.model_text.endswith("Continue.")
    # A single question needs no pairing in the transcript; the row already
    # shows the question via display_call.
    assert result.display_text == "answered: uv"
    assert broker.asked == [("Which package manager?", ["uv", "pip"])]


@pytest.mark.asyncio
async def test_batch_pairs_each_question_with_its_answer():
    tool, _ = _tool({"Base branch?": "main", "Squash?": "yes"})

    result = await tool.execute(questions=[{"question": "Base branch?"}, {"question": "Squash?"}])

    assert isinstance(result, ToolResult)
    # With several questions the display text must say which answer belongs to
    # which question -- one line per pair, order preserved.
    assert result.display_text == "Base branch? -> main\nSquash? -> yes"
    assert 'User answered: "Base branch?" -> "main".' in result.model_text
    assert 'User answered: "Squash?" -> "yes".' in result.model_text


@pytest.mark.asyncio
async def test_unanswered_question_is_explicit_in_both_texts():
    tool, _ = _tool({})

    result = await tool.execute(questions=[{"question": "Ship it?"}])

    assert isinstance(result, ToolResult)
    assert "did not answer" in result.model_text
    assert result.display_text == "answered: (no answer)"


@pytest.mark.asyncio
async def test_error_paths_return_plain_strings():
    # No broker / no conversation id / no questions predate ToolResult and stay
    # bare strings, so the loop's str branch still has to work.
    tool = AskUserTool(broker=None, conversation_id="tui:test")
    assert await tool.execute(questions=[{"question": "hi"}]) == "Error: ask_user not configured (no question broker)"

    tool, _ = _tool({})
    assert await tool.execute(questions=[]) == "Error: ask_user requires at least one question"
    assert await tool.execute(questions=[{"question": "   "}]) == (
        "Error: ask_user requires at least one non-empty question"
    )


def test_display_call_labels_the_row_with_the_question():
    tool, _ = _tool({})

    assert tool.display_call({"questions": [{"question": "Which base branch?"}]}) == "Which base branch?"
    assert tool.display_call({"questions": [{"question": "Base?"}, {"question": "Squash?"}]}) == "Base? | Squash?"
    # Nothing worth showing falls back to the UI's generic preview.
    assert tool.display_call({"questions": []}) is None
    assert tool.display_call({"questions": [{"question": "  "}]}) is None
    assert tool.display_call({}) is None


def test_display_call_survives_a_json_encoded_questions_argument():
    # Models emit the array as a JSON string often enough that display_call --
    # which only labels a transcript row -- used to take the whole turn down
    # with an AttributeError raised on a character of that string.
    tool, _ = _tool({})

    assert tool.display_call({"questions": '[{"question": "Base?"}]'}) == "Base?"
    assert tool.display_call({"questions": '[{"question": "Base?"}, {"question": "Squash?"}]'}) == "Base? | Squash?"


def test_display_call_returns_none_for_unreadable_arguments():
    tool, _ = _tool({})

    assert tool.display_call({"questions": "not json at all"}) is None
    assert tool.display_call({"questions": 42}) is None
    assert tool.display_call({"questions": ["plain string", {"question": "Base?"}]}) == "Base?"


@pytest.mark.asyncio
async def test_execute_accepts_a_json_encoded_questions_argument():
    # The same coercion has to reach execute: a non-empty string is truthy, so
    # the empty-questions guard passed it straight through to entry.get().
    tool, broker = _tool({"Which base branch?": "main"})

    result = await tool.execute(questions='[{"question": "Which base branch?", "options": ["main", "develop"]}]')

    assert isinstance(result, ToolResult)
    assert broker.asked == [("Which base branch?", ["main", "develop"])]
    assert result.display_text == "answered: main"


@pytest.mark.asyncio
async def test_execute_rejects_unreadable_questions_instead_of_raising():
    tool, _ = _tool({})

    assert await tool.execute(questions="not json at all") == "Error: ask_user requires at least one question"
    assert await tool.execute(questions=42) == "Error: ask_user requires at least one question"


@pytest.mark.asyncio
async def test_deeply_nested_json_does_not_kill_the_turn():
    # json.loads answers deep nesting with RecursionError, which is not a
    # ValueError -- so a normalizer that catches only TypeError/ValueError lets
    # it escape by the exact route the normalizer exists to close.
    tool, _ = _tool({})
    payload = "[" * 20_000 + "]" * 20_000

    assert tool.display_call({"questions": payload}) is None
    assert await tool.execute(questions=payload) == "Error: ask_user requires at least one question"


@pytest.mark.asyncio
async def test_json_encoded_options_reach_the_user_as_options():
    # options carries the same declared array type as questions and arrives as
    # a JSON string just as often. Iterating that string offered the user one
    # suggested answer per character.
    tool, broker = _tool({"Which base?": "main"})

    await tool.execute(questions=[{"question": "Which base?", "options": '["main", "develop"]'}])

    assert broker.asked == [("Which base?", ["main", "develop"])]


def test_a_lone_option_string_is_one_option_not_its_letters():
    """Asserted on the normalizer, not through execute: upstream's contract
    refuses a one-option question outright, so a lone string never reaches a
    human either way. What must not happen is it arriving as three options.
    """
    assert _normalize_options("yes") == ["yes"]
    assert _normalize_options('["yes", "no"]') == ["yes", "no"]


@pytest.mark.asyncio
async def test_absent_or_unusable_options_are_simply_empty():
    tool, broker = _tool({"Q1?": "a", "Q2?": "b"})

    await tool.execute(questions=[{"question": "Q1?"}, {"question": "Q2?", "options": None}])

    assert broker.asked == [("Q1?", []), ("Q2?", [])]


@pytest.mark.asyncio
async def test_questions_written_as_plain_strings_still_reach_the_user():
    # A list with nothing object-shaped in it is a model that wrote the
    # questions as strings. Dropping them answered a perfectly clear question
    # with "requires at least one question" and asked the user nothing.
    tool, broker = _tool({"Which base branch?": "main"})

    result = await tool.execute(questions=["Which base branch?"])

    assert isinstance(result, ToolResult)
    assert broker.asked == [("Which base branch?", [])]
    assert tool.display_call({"questions": ["Which base branch?"]}) == "Which base branch?"


@pytest.mark.asyncio
async def test_an_object_entry_still_wins_over_a_loose_string():
    # The mixed list keeps its old meaning: the objects are the questions and
    # the loose string is noise, not a third question.
    tool, broker = _tool({"Base?": "main"})

    await tool.execute(questions=["noise", {"question": "Base?"}])

    assert broker.asked == [("Base?", [])]


@pytest.mark.asyncio
async def test_a_twice_encoded_argument_is_unwrapped():
    tool, broker = _tool({"Base?": "main"})

    await tool.execute(questions=json.dumps(json.dumps([{"question": "Base?"}])))

    assert broker.asked == [("Base?", [])]


def test_unwrapping_is_bounded():
    # Past a couple of layers this is no longer a quirk to absorb, and the
    # bound is what stops a crafted argument from spending the turn unwrapping.
    payload = json.dumps([{"question": "Base?"}])
    for _ in range(_MAX_JSON_LAYERS + 1):
        payload = json.dumps(payload)

    assert _normalize_questions(payload) == []


# --- through ToolRegistry, the path production actually takes -------------
#
# Everything above drives ``tool.execute`` directly, which is the function's
# contract but not the call chain. ``ToolRegistry.execute`` casts and validates
# against the declared schema first and returns the error without dispatching,
# so a coercion that lives only in ``execute`` never runs on the shapes that
# need it. These go through the registry.


def _registered(answers: dict[str, str]) -> tuple[ToolRegistry, _StubBroker]:
    tool, broker = _tool(answers)
    registry = ToolRegistry()
    registry.register(tool)
    return registry, broker


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "questions",
    [
        pytest.param('[{"question": "Which base branch?"}]', id="array-as-json-string"),
        pytest.param(["Which base branch?"], id="entry-as-plain-string"),
        pytest.param({"question": "Which base branch?"}, id="single-object-unwrapped"),
        pytest.param(json.dumps(json.dumps([{"question": "Which base branch?"}])), id="twice-encoded"),
    ],
)
async def test_the_registry_path_asks_rather_than_rejecting(questions):
    registry, broker = _registered({"Which base branch?": "main"})

    out = await registry.execute("ask_user", {"questions": questions})

    assert broker.asked == [("Which base branch?", [])], out
    assert "Invalid parameters" not in out


@pytest.mark.asyncio
async def test_the_registry_path_normalizes_options_too():
    registry, broker = _registered({"Which base?": "main"})

    out = await registry.execute(
        "ask_user", {"questions": [{"question": "Which base?", "options": '["main", "develop"]'}]}
    )

    assert broker.asked == [("Which base?", ["main", "develop"])], out


@pytest.mark.asyncio
async def test_the_registry_path_reports_unreadable_input_in_the_tools_own_words():
    # Not a schema complaint about a shape the model cannot act on: by the time
    # validation runs the argument has been normalized, so what is left is a
    # genuinely empty question list and the tool says so.
    registry, _ = _registered({})

    out = await registry.execute("ask_user", {"questions": "not json at all"})

    assert "requires at least one question" in out
    assert "should be array" not in out


@pytest.mark.asyncio
async def test_the_registry_path_does_not_rewrite_the_callers_arguments():
    """Normalizing must leave the caller's dict alone.

    The same `arguments` object the registry is handed also goes to the START
    tool event and, on the assistant message, through `to_openai_tool_call`. Both
    happen before `tools.execute` today, so an in-place edit could not reach
    them -- but the object is shared, `cast_params` is the only thing standing
    between the model's text and a rewrite of it, and nothing was watching:
    dropping the copy leaves the whole suite green.
    """
    registry, broker = _registered({"Which base?": "main"})
    args = {"questions": [{"question": "Which base?", "options": '["main", "develop"]'}]}
    frozen = copy.deepcopy(args)

    await registry.execute("ask_user", args)

    assert broker.asked == [("Which base?", ["main", "develop"])]
    assert args == frozen


def test_schema_advertises_only_fields_the_tool_reads():
    """Every declared field must reach the broker; a decorative one misleads.

    ``multiple`` and ``custom`` were declared and never read, so a model that
    asked for a multi-select got a single-select and never learned why.
    """
    tool, _ = _tool({})
    entry = tool.parameters["properties"]["questions"]["items"]["properties"]

    assert "multiple" not in entry
    assert "custom" not in entry
    assert set(entry) == {"question", "header", "options", "recommended"}
    assert tool.parameters["properties"]["questions"]["maxItems"] == 4


@pytest.mark.asyncio
async def test_header_and_batch_position_reach_the_broker():
    tool, broker = _tool({"Base?": "main", "Squash?": "yes"})

    await tool.execute(
        questions=[
            {"question": "Base?", "header": "Base"},
            {"question": "Squash?", "header": "Squash"},
        ]
    )

    assert [c["header"] for c in broker.calls] == ["Base", "Squash"]
    assert [c["index"] for c in broker.calls] == [0, 1]
    assert [c["total"] for c in broker.calls] == [2, 2]
    # Every round-trip carries the whole batch so a surface can render the set
    # and its progress while still collecting one answer at a time.
    assert broker.calls[0]["batch"] == [
        {"question": "Base?", "header": "Base"},
        {"question": "Squash?", "header": "Squash"},
    ]


@pytest.mark.asyncio
async def test_batch_shares_one_deadline_instead_of_one_each():
    """N questions must not cost N full timeouts."""
    tool, broker = _tool({"a?": "1", "b?": "2"}, delay_s=0.05)
    broker.default_timeout_s = 1.0

    await tool.execute(questions=[{"question": "a?"}, {"question": "b?"}])

    first, second = broker.calls[0]["timeout_s"], broker.calls[1]["timeout_s"]
    assert first <= 1.0
    assert second < first, "the second question must inherit what the first left"
    assert second > 0


@pytest.mark.asyncio
async def test_single_option_question_is_rejected_without_asking_anyone():
    tool, broker = _tool({})

    result = await tool.execute(questions=[{"question": "Ship it?", "options": ["yes"]}])

    assert isinstance(result, str)
    assert "exactly one option" in result
    assert "filler" in result
    assert broker.asked == [], "a rejected call must not reach the user"


@pytest.mark.asyncio
async def test_free_form_question_with_no_options_is_still_allowed():
    tool, broker = _tool({"Why?": "because"})

    result = await tool.execute(questions=[{"question": "Why?"}])

    assert isinstance(result, ToolResult)
    assert broker.asked == [("Why?", [])]


@pytest.mark.asyncio
async def test_duplicate_question_text_is_rejected_before_prompting():
    tool, broker = _tool({})

    result = await tool.execute(questions=[{"question": "Ship it?"}, {"question": "Ship it?"}])

    assert isinstance(result, str)
    assert "duplicate question" in result
    assert broker.asked == []


@pytest.mark.asyncio
async def test_duplicate_option_labels_are_deduped_rather_than_rejected():
    """A repeated label is a typo with one obvious reading; dropping it costs
    the model nothing, whereas rejecting the call costs a whole turn."""
    tool, broker = _tool({"Which?": "uv"})

    result = await tool.execute(questions=[{"question": "Which?", "options": ["uv", "pip", "uv"]}])

    assert isinstance(result, ToolResult)
    assert broker.asked == [("Which?", ["uv", "pip"])]


@pytest.mark.asyncio
async def test_batch_over_the_cap_is_rejected_with_the_cap_named():
    tool, broker = _tool({})

    result = await tool.execute(questions=[{"question": f"q{i}?"} for i in range(5)])

    assert isinstance(result, str)
    assert "at most 4" in result
    assert broker.asked == []


@pytest.mark.asyncio
async def test_unanswered_question_names_the_recommended_option():
    """Without a default the model only learns that nobody answered; naming the
    option it recommended is what lets it proceed the way it intended to."""
    tool, _ = _tool({})

    result = await tool.execute(questions=[{"question": "Ship it?", "options": ["hold", "ship"], "recommended": 1}])

    assert isinstance(result, ToolResult)
    assert 'recommended option was "ship"' in result.model_text


@pytest.mark.asyncio
async def test_recommendation_survives_a_duplicate_earlier_in_the_options():
    """The index counts the options as submitted, so a duplicate ahead of it must
    not shift which label the recommendation resolves to."""
    tool, broker = _tool({"Ship?": "ship"})

    await tool.execute(questions=[{"question": "Ship?", "options": ["a", "a", "b", "c"], "recommended": 2}])

    assert broker.calls[0]["choices"] == ["a", "b", "c"]
    assert broker.calls[0]["recommended"] == "b"


@pytest.mark.asyncio
async def test_recommendation_past_the_deduped_length_still_resolves():
    """Dedup shortens the list, so an index valid against the submitted options
    can fall outside it -- that must not silently drop the recommendation."""
    tool, broker = _tool({"Ship?": "ship"})

    await tool.execute(questions=[{"question": "Ship?", "options": ["a", "a", "b", "c"], "recommended": 3}])

    assert broker.calls[0]["recommended"] == "c"


@pytest.mark.asyncio
async def test_out_of_range_recommended_index_is_ignored():
    tool, _ = _tool({})

    result = await tool.execute(questions=[{"question": "Ship it?", "options": ["a", "b"], "recommended": 9}])

    assert isinstance(result, ToolResult)
    assert "recommended option" not in result.model_text
    assert "did not answer" in result.model_text


@pytest.mark.asyncio
async def test_exhausted_budget_stops_asking_instead_of_starting_a_fresh_wait():
    """Once the shared deadline is spent, the remaining questions must not each
    open a new wait -- that is the per-question timeout this replaces."""
    tool, broker = _tool({"a?": "1", "b?": "2"}, delay_s=0.05)
    broker.default_timeout_s = 0.01

    result = await tool.execute(questions=[{"question": "a?"}, {"question": "b?"}])

    assert isinstance(result, ToolResult)
    assert [c["prompt"] for c in broker.calls] == ["a?"], "the second question must not be asked"
    assert "did not answer" in result.model_text


@pytest.mark.asyncio
async def test_overlong_header_is_truncated_rather_than_breaking_the_chip():
    tool, broker = _tool({"Which?": "uv"})

    await tool.execute(questions=[{"question": "Which?", "header": "a-very-long-header-label"}])

    assert broker.calls[0]["header"] == "a-very-long-"


@pytest.mark.asyncio
async def test_recommended_option_reaches_the_broker_for_the_surface_to_mark():
    tool, broker = _tool({"Ship?": "ship"})

    await tool.execute(questions=[{"question": "Ship?", "options": ["hold", "ship"], "recommended": 1}])

    assert broker.calls[0]["recommended"] == "ship"


@pytest.mark.asyncio
async def test_tool_budget_overrides_whatever_the_surface_defaults_to():
    """The batch budget is configuration, so it has to reach the tool without
    the tool re-reading config from under a transport that never had one."""
    broker = _StubBroker({"a?": "1"})
    broker.default_timeout_s = 600.0
    tool = AskUserTool(broker=broker, conversation_id="tui:test", timeout_s=1.5)  # type: ignore[arg-type]

    await tool.execute(questions=[{"question": "a?"}])

    assert broker.calls[0]["timeout_s"] <= 1.5


@pytest.mark.asyncio
async def test_agent_loop_hands_the_configured_budget_to_the_tool(tmp_path):
    """The loop is where the config already is, so it is where the budget has
    to be handed over -- the transports that wire the broker do not all have a
    config to read."""
    from raven.agent.loop.main import AgentLoop
    from raven.config.schema import AskUserToolConfig

    class _Provider:
        """AgentLoop construction reads the default model; no turn is run here."""

        def get_default_model(self) -> str:
            return "stub-model"

        async def chat_with_retry(self, **kwargs):  # pragma: no cover - never invoked
            raise NotImplementedError

    loop = AgentLoop(provider=_Provider(), workspace=tmp_path, ask_user_config=AskUserToolConfig(timeout=42))
    tool = loop.tools.get("ask_user")
    assert tool is not None

    broker = _StubBroker({"a?": "1"})
    broker.default_timeout_s = 600.0
    tool.set_broker(broker)
    tool.set_context("tui:test")

    await tool.execute(questions=[{"question": "a?"}])

    assert broker.calls[0]["timeout_s"] <= 42


def test_description_states_the_cap_the_code_enforces():
    """The description is prompt text the model reads, so a cap named there and
    a cap enforced here are two statements of one rule and will drift apart."""
    from raven.agent.tools.ask_user import MAX_QUESTIONS

    tool, _ = _tool({})

    assert f"up to {MAX_QUESTIONS}" in tool.description
    assert tool.parameters["properties"]["questions"]["maxItems"] == MAX_QUESTIONS


@pytest.mark.asyncio
async def test_round_trip_through_the_real_broker():
    """Every other test here drives a stand-in, which cannot catch the tool and
    the broker disagreeing about the keyword names they pass between them."""
    from raven.rpc.question_broker import QuestionBroker

    frames: list[dict] = []

    async def send_frame(frame: dict) -> None:
        frames.append(frame)
        broker.reply(frame["params"]["conversation_id"], "uv")

    broker = QuestionBroker(send_frame, timeout_s=5.0)
    tool = AskUserTool(broker=broker, conversation_id="tui:test")

    result = await tool.execute(
        questions=[{"question": "Which?", "header": "Pkg", "options": ["uv", "pip"], "recommended": 0}]
    )

    assert isinstance(result, ToolResult)
    assert 'User answered: "Which?" -> "uv".' in result.model_text
    params = frames[0]["params"]
    assert params["header"] == "Pkg"
    assert params["recommended"] == "uv"
    assert params["total"] == 1
    assert params["timeout_s"] <= 5.0


@pytest.mark.asyncio
async def test_ask_direct_forwards_the_batch_to_the_broker():
    tool, broker = _tool({"Which reviewer?": "chandler"})
    batch = [{"question": "Which branch?"}, {"question": "Which reviewer?"}]
    await tool.ask_direct("Which reviewer?", None, "tui:c1", index=1, total=2, batch=batch)
    call = broker.calls[0]
    assert (call["index"], call["total"], call["batch"]) == (1, 2, batch)


@pytest.mark.asyncio
async def test_ask_direct_still_defaults_to_a_lone_question():
    tool, broker = _tool({"Which branch?": "feat/x"})
    await tool.ask_direct("Which branch?", None, "tui:c1")
    call = broker.calls[0]
    assert (call["index"], call["total"], call["batch"]) == (0, 1, None)
    assert not call.get("default")


@pytest.mark.asyncio
async def test_registry_dispatch_and_the_real_clarify_respond_route():
    """The production entry point is the registry, not ``execute`` directly, and
    the answer arrives over the real ``clarify.respond`` handler. Neither layer
    is exercised by the tests above, and both can reject a call the tool would
    have accepted -- the schema validator runs in between.
    """
    from raven.agent.tools.registry import ToolRegistry
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods.question import register_question_methods
    from raven.rpc.question_broker import QuestionBroker

    dispatcher = Dispatcher()

    async def send_frame(frame: dict) -> None:
        params = frame["params"]
        reply = await dispatcher.dispatch(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "clarify.respond",
                "params": {"request_id": params["request_id"], "answer": "uv"},
            }
        )
        assert reply["result"]["ok"] is True

    broker = QuestionBroker(send_frame, timeout_s=5.0)
    register_question_methods(dispatcher, question_broker=broker)
    registry = ToolRegistry()
    registry.register(AskUserTool(broker=broker, conversation_id="tui:test"))

    answered = await registry.execute(
        "ask_user",
        {"questions": [{"question": "Which?", "header": "Pkg", "options": ["uv", "pip"], "recommended": 0}]},
    )
    assert 'User answered: "Which?" -> "uv".' in answered

    # A rejection has to survive the registry intact -- it is the steer the model
    # reads, and the registry appends to it rather than replacing it.
    rejected = await registry.execute("ask_user", {"questions": [{"question": "Ship?", "options": ["yes"]}]})
    assert "exactly one option" in rejected
    assert "filler" in rejected
