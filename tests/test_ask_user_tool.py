"""Tests for the ask_user tool's display contract (raven/agent/tools/ask_user.py).

``ask_user`` is the first tool to return :class:`ToolResult`, so this pins the
split it introduces: ``model_text`` keeps the natural-language phrasing the
model reads, ``display_text`` carries the question/answer pairing the transcript
renders, and ``display_call`` labels the row with the question rather than the
raw arguments blob.
"""

from __future__ import annotations

import asyncio

import pytest

from raven.agent.tools.ask_user import AskUserTool
from raven.agent.tools.base import ToolResult


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
    from raven.tui_rpc.question_broker import QuestionBroker

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
async def test_registry_dispatch_and_the_real_clarify_respond_route():
    """The production entry point is the registry, not ``execute`` directly, and
    the answer arrives over the real ``clarify.respond`` handler. Neither layer
    is exercised by the tests above, and both can reject a call the tool would
    have accepted -- the schema validator runs in between.
    """
    from raven.agent.tools.registry import ToolRegistry
    from raven.tui_rpc.dispatcher import Dispatcher
    from raven.tui_rpc.methods.question import register_question_methods
    from raven.tui_rpc.question_broker import QuestionBroker

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
