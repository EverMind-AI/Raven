"""Question autofill: the decisions, the extraction, and the prompt shape."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from raven.agent.acp_client import autofill, resolver
from raven.agent.acp_client.resolver import Autofill
from raven.config.raven import MemoryConfig, SubagentQuestionsConfig
from raven.providers.openai_codex_provider import _convert_messages
from raven.spine.events import ToolPhase


def _response(*calls):
    return SimpleNamespace(tool_calls=[SimpleNamespace(arguments=a) for a in calls])


def _is_fenced(body: str, needle: str) -> bool:
    """Whether `needle` sits inside one `wrap_untrusted` fence.

    Containment, not "some BEGIN before it and some END after it": with two
    fenced blocks in one body, the weaker test reads the host's own text
    *between* the blocks as fenced, and every fencing test here goes vacuous.
    """
    needle_idx = body.find(needle)
    if needle_idx == -1:
        return False
    begin_idx = body.rfind("BEGIN UNTRUSTED", 0, needle_idx)
    if begin_idx == -1 or body.find("END UNTRUSTED", needle_idx) == -1:
        return False
    return body.find("END UNTRUSTED", begin_idx, needle_idx) == -1


def _spawn_call(call_id: str, task: str) -> dict:
    """One `spawn` call as the loop stores it (`ToolCall.openai_tool_call`)."""
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "spawn", "arguments": json.dumps({"agent": "raven-code", "task": task})},
    }


def _spawn_snapshot() -> list[dict]:
    """The turn as it stands when a spawned sub-agent asks: the call is still open."""
    return [
        {"role": "user", "content": "have raven-code push it to feat/x"},
        {"role": "assistant", "content": "", "tool_calls": [_spawn_call("call_1", "push the branch")]},
    ]


def test_extract_maps_answers_onto_their_questions():
    questions = [
        autofill.Question(key="branch", prompt="Which branch?", options=[], required=True),
        autofill.Question(key="reviewer", prompt="Which reviewer?", options=[], required=False),
    ]
    response = _response(
        {
            "answers": [
                {"key": "branch", "status": "answer", "answer": "feat/x"},
                {"key": "reviewer", "status": "defer"},
            ]
        }
    )
    out = autofill.extract_resolutions(response, questions)
    assert [r.status for r in out] == ["answer", "defer"]
    assert out[0].answer == "feat/x"


def test_extract_defers_a_question_the_model_did_not_mention():
    questions = [autofill.Question(key="branch", prompt="Which branch?", options=[], required=True)]
    out = autofill.extract_resolutions(_response({"answers": []}), questions)
    assert [r.status for r in out] == ["defer"]


def test_extract_defers_when_no_tool_was_called():
    questions = [autofill.Question(key="branch", prompt="Which branch?", options=[], required=True)]
    out = autofill.extract_resolutions(SimpleNamespace(tool_calls=[]), questions)
    assert [r.status for r in out] == ["defer"]


def test_extract_reads_arguments_given_as_a_json_string():
    questions = [autofill.Question(key="branch", prompt="Which branch?", options=[], required=True)]
    response = _response('{"answers": [{"key": "branch", "status": "answer", "answer": "main"}]}')
    assert autofill.extract_resolutions(response, questions)[0].answer == "main"


def test_extract_defers_an_answer_outside_the_offered_options():
    questions = [autofill.Question(key="pick", prompt="Which?", options=["a", "b"], required=True)]
    response = _response({"answers": [{"key": "pick", "status": "answer", "answer": "c"}]})
    assert autofill.extract_resolutions(response, questions)[0].status == "defer"


def test_extract_defers_an_answer_with_no_text():
    questions = [autofill.Question(key="branch", prompt="Which branch?", options=[], required=True)]
    response = _response({"answers": [{"key": "branch", "status": "answer", "answer": "  "}]})
    assert autofill.extract_resolutions(response, questions)[0].status == "defer"


def test_partial_without_a_note_is_a_plain_defer():
    questions = [autofill.Question(key="branch", prompt="Which branch?", options=[], required=True)]
    response = _response({"answers": [{"key": "branch", "status": "partial", "known": ""}]})
    assert autofill.extract_resolutions(response, questions)[0].status == "defer"


def test_annotate_appends_and_leaves_the_question_intact():
    out = autofill.annotate("Which branch, and who reviews?", "branch = feat/x; reviewer unknown")
    assert out.startswith("Which branch, and who reviews?")
    assert "branch = feat/x; reviewer unknown" in out


def test_annotate_returns_the_prompt_unchanged_when_nothing_is_known():
    assert autofill.annotate("Which branch?", "") == "Which branch?"


def test_questions_reach_the_model_fenced():
    messages = autofill.build_messages(
        snapshot=[{"role": "user", "content": "push it to feat/x"}],
        ledger=[],
        memories=[],
        questions=[autofill.Question(key="branch", prompt="Which branch?", options=[], required=True)],
        agent="raven-code",
        instance="a1b2",
    )
    body = messages[-1]["content"]
    assert _is_fenced(body, "Which branch?")


def test_the_turn_is_shown_before_the_questions():
    messages = autofill.build_messages(
        snapshot=[{"role": "user", "content": "push it to feat/x"}],
        ledger=[],
        memories=[],
        questions=[autofill.Question(key="branch", prompt="Which branch?", options=[], required=True)],
        agent="raven-code",
        instance="a1b2",
    )
    assert messages[0]["role"] == "system"
    assert messages[1:-1] == [{"role": "user", "content": "push it to feat/x"}]


def test_the_instruction_forbids_authorising_an_action():
    messages = autofill.build_messages(
        snapshot=[],
        ledger=[],
        memories=[],
        questions=[autofill.Question(key="go", prompt="Push now?", options=["yes", "no"], required=True)],
        agent="raven-code",
        instance="a1b2",
    )
    instruction = messages[0]["content"]
    required_acts = ["pushing", "deleting", "sending", "paying", "overwriting", "merging"]
    for act in required_acts:
        assert act.lower() in instruction.lower(), f"Instruction must forbid {act}"


def test_extract_maps_by_key_when_entries_are_out_of_order():
    questions = [
        autofill.Question(key="branch", prompt="Which branch?", options=[], required=True),
        autofill.Question(key="reviewer", prompt="Which reviewer?", options=[], required=False),
    ]
    response = _response(
        {
            "answers": [
                {"key": "reviewer", "status": "defer"},
                {"key": "branch", "status": "answer", "answer": "feat/x"},
            ]
        }
    )
    out = autofill.extract_resolutions(response, questions)
    assert [r.status for r in out] == ["answer", "defer"]
    assert out[0].answer == "feat/x"
    assert out[1].status == "defer"


def test_single_question_resolves_from_single_key():
    questions = [autofill.Question(key="", prompt="Which branch?", options=[], required=True)]
    response = _response({"answers": [{"key": "(single)", "status": "answer", "answer": "main"}]})
    out = autofill.extract_resolutions(response, questions)
    assert out[0].status == "answer"
    assert out[0].answer == "main"


def test_single_question_resolves_from_empty_key():
    questions = [autofill.Question(key="", prompt="Which branch?", options=[], required=True)]
    response = _response({"answers": [{"key": "", "status": "answer", "answer": "main"}]})
    out = autofill.extract_resolutions(response, questions)
    assert out[0].status == "answer"
    assert out[0].answer == "main"


def test_duplicate_key_uses_first_entry():
    questions = [autofill.Question(key="branch", prompt="Which branch?", options=[], required=True)]
    response = _response(
        {
            "answers": [
                {"key": "branch", "status": "defer"},
                {"key": "branch", "status": "answer", "answer": "main"},
            ]
        }
    )
    out = autofill.extract_resolutions(response, questions)
    assert out[0].status == "defer"


def test_non_string_answer_defers():
    questions = [autofill.Question(key="branch", prompt="Which branch?", options=[], required=True)]
    response = _response({"answers": [{"key": "branch", "status": "answer", "answer": {"x": 1}}]})
    out = autofill.extract_resolutions(response, questions)
    assert out[0].status == "defer"


def test_defer_all_returns_one_resolution_per_item():
    items = ["a", "b", "c"]
    out = autofill.defer_all(items)
    assert len(out) == 3
    assert all(r.status == "defer" for r in out)


def test_defer_all_accepts_non_question_objects():
    class Field:
        pass

    items = [Field(), Field()]
    out = autofill.defer_all(items)
    assert len(out) == 2
    assert all(r.status == "defer" for r in out)


def test_answer_tool_schema_has_correct_status_enum():
    schema = autofill.answer_tool_schema()
    assert len(schema) == 1
    func = schema[0]["function"]
    answers_schema = func["parameters"]["properties"]["answers"]["items"]
    status_enum = answers_schema["properties"]["status"]["enum"]
    assert set(status_enum) == {"answer", "partial", "defer"}


def test_partial_with_known_survives():
    questions = [autofill.Question(key="branch", prompt="Which branch?", options=[], required=True)]
    response = _response({"answers": [{"key": "branch", "status": "partial", "known": "from user input"}]})
    out = autofill.extract_resolutions(response, questions)
    assert out[0].status == "partial"
    assert out[0].known == "from user input"


def test_recalled_memory_reaches_model_fenced():
    messages = autofill.build_messages(
        snapshot=[{"role": "user", "content": "some context"}],
        ledger=[],
        memories=["prefer main branch"],
        questions=[autofill.Question(key="branch", prompt="Which branch?", options=[], required=True)],
        agent="raven-code",
        instance="a1b2",
    )
    body = messages[-1]["content"]
    assert _is_fenced(body, "prefer main branch")


def test_ledger_reaches_model_fenced():
    messages = autofill.build_messages(
        snapshot=[],
        ledger=["answered branch: main"],
        memories=[],
        questions=[autofill.Question(key="reviewer", prompt="Who reviews?", options=[], required=True)],
        agent="raven-code",
        instance="a1b2",
    )
    body = messages[-1]["content"]
    assert _is_fenced(body, "answered branch: main")


def test_the_host_text_between_two_fenced_blocks_is_not_fenced():
    """Both optional blocks populated, which is what makes `_is_fenced` load-bearing.

    With one block populated a payload cannot sit between two fences, so the
    weaker "a BEGIN before, an END after" test and real containment agree; with
    two, the label raven writes between them passes the weaker one.
    """
    messages = autofill.build_messages(
        snapshot=[],
        ledger=["answered branch: main"],
        memories=["prefer main branch"],
        questions=[autofill.Question(key="reviewer", prompt="Who reviews?", options=[], required=True)],
        agent="raven-code",
        instance="a1b2",
    )
    body = messages[-1]["content"]
    assert _is_fenced(body, "answered branch: main")
    assert _is_fenced(body, "prefer main branch")
    assert _is_fenced(body, "Who reviews?")
    label = "Recalled long-term memory for these questions:"
    assert label in body
    assert not _is_fenced(body, label)


def test_an_open_tool_call_is_answered_before_the_questions():
    # The shape every spawn / DAG question actually has: the sub-agent asks from
    # inside the call that created it, so that call's result cannot be in the
    # snapshot yet.
    snapshot = _spawn_snapshot()
    messages = autofill.build_messages(
        snapshot=snapshot,
        ledger=[],
        memories=[],
        questions=[autofill.Question(key="branch", prompt="Which branch?", options=[], required=True)],
        agent="raven-code",
        instance="a1b2",
    )
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "user"]
    assert messages[3]["tool_call_id"] == "call_1"
    # The spawn arguments carry the sub-agent's task text, which is why the turn
    # is sent at all, so the assistant message reaches the model whole.
    assert messages[2]["tool_calls"] == snapshot[1]["tool_calls"]


def test_only_the_open_tool_call_gets_a_placeholder():
    # Parallel calls: `c1` already returned and its result is in the snapshot, so
    # answering it again would put two `tool` messages on one id.
    snapshot = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [_spawn_call("c1", "read the diff"), _spawn_call("c2", "push the branch")],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "the diff is clean"},
    ]
    messages = autofill.build_messages(
        snapshot=snapshot,
        ledger=[],
        memories=[],
        questions=[autofill.Question(key="branch", prompt="Which branch?", options=[], required=True)],
        agent="raven-code",
        instance="a1b2",
    )
    results = [m for m in messages if m.get("role") == "tool"]
    assert [m["tool_call_id"] for m in results] == ["c1", "c2"]
    assert results[0]["content"] == "the diff is clean"
    assert results[1]["content"] != "the diff is clean"


def test_the_placeholder_result_is_not_fenced():
    # Host-authored text; the fence is for what a sub-agent or a tool wrote.
    messages = autofill.build_messages(
        snapshot=_spawn_snapshot(),
        ledger=[],
        memories=[],
        questions=[autofill.Question(key="branch", prompt="Which branch?", options=[], required=True)],
        agent="raven-code",
        instance="a1b2",
    )
    assert "UNTRUSTED" not in messages[3]["content"]


def _system_snapshot() -> list[dict]:
    """The snapshot as the loop publishes it: raven's own system message first."""
    return [{"role": "system", "content": "You are raven, the main agent."}, *_spawn_snapshot()]


def test_the_turns_system_message_is_folded_into_the_leading_instruction():
    messages = autofill.build_messages(
        snapshot=_system_snapshot(),
        ledger=[],
        memories=[],
        questions=[autofill.Question(key="go", prompt="Push now?", options=["yes", "no"], required=True)],
        agent="raven-code",
        instance="a1b2",
    )
    assert [m["role"] for m in messages] == ["system", "user", "assistant", "tool", "user"]
    content = messages[0]["content"]
    assert content.startswith("You are raven, the main agent.")
    assert content.endswith(autofill.INSTRUCTION)
    assert messages[1:3] == _spawn_snapshot()


def test_a_turn_with_no_system_message_gets_the_instruction_alone():
    # Nothing precedes the policy, so the lead-in joining the two would have
    # nothing to point back at.
    messages = autofill.build_messages(
        snapshot=[{"role": "user", "content": "push it to feat/x"}],
        ledger=[],
        memories=[],
        questions=[autofill.Question(key="go", prompt="Push now?", options=["yes", "no"], required=True)],
        agent="raven-code",
        instance="a1b2",
    )
    assert messages[0]["content"] == autofill.INSTRUCTION


def test_the_codex_conversion_keeps_the_resolver_policy():
    """One system message, because Codex only forwards one.

    `_convert_messages` keeps whichever system message it read last and sends
    that alone as the request's `instructions`. A second one would take the
    policy off the request while leaving the questions and the tool in place --
    the model could still answer, with none of the rules, and the authorisation
    veto is the rule that stops raven approving a push on the user's behalf.
    """
    messages = autofill.build_messages(
        snapshot=_system_snapshot(),
        ledger=[],
        memories=[],
        questions=[autofill.Question(key="go", prompt="Push now?", options=["yes", "no"], required=True)],
        agent="raven-code",
        instance="a1b2",
    )
    instructions, _ = _convert_messages(messages)
    assert "report_answers" in instructions
    assert "NEVER answer a question that asks permission to perform an action" in instructions
    assert "authorising an action is the user's to do, not yours" in instructions
    assert "You are raven, the main agent." in instructions


class _Provider:
    def __init__(self, response=None, error=None, delay=0.0):
        self._response, self._error, self._delay = response, error, delay
        self.calls = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error:
            raise self._error
        return self._response


def _unmatched_tool_call_ids(messages) -> list[str]:
    """The `tool_call` ids no `tool` message answers, in the order opened."""
    open_ids: list[str] = []
    for message in messages:
        if message.get("role") == "assistant":
            for call in message.get("tool_calls") or []:
                open_ids.append(call["id"])
        elif message.get("role") == "tool" and message.get("tool_call_id") in open_ids:
            open_ids.remove(message["tool_call_id"])
    return open_ids


class _StrictProvider(_Provider):
    """A provider that validates the sequence the way Chat Completions does.

    `_Provider` takes any shape, which is how a request no strict provider would
    accept passed every test here: `resolve` catches the rejection and defers, so
    a real 400 reads exactly like "nothing was answerable".
    """

    async def chat_with_retry(self, **kwargs):
        if unmatched := _unmatched_tool_call_ids(kwargs["messages"]):
            raise RuntimeError(
                "An assistant message with 'tool_calls' must be followed by tool "
                f"messages responding to each 'tool_call_id': {unmatched}"
            )
        return await super().chat_with_retry(**kwargs)


class _Loop:
    def __init__(self, provider, backend=None, model="fake/model"):
        self.provider = provider
        self.backend = backend
        self.model = model
        self.memory_config = MemoryConfig()
        self.subagent_questions_config = SubagentQuestionsConfig()


def _autofill(loop, snapshot=None, *, emit=None):
    """One turn's autofill, with its snapshot published as the loop publishes it."""
    auto = Autofill(loop, emit=emit, conversation_id="tui:c1", config=loop.subagent_questions_config)
    auto.set_snapshot([] if snapshot is None else snapshot)
    return auto


QUESTIONS = [autofill.Question(key="branch", prompt="Which branch?", options=[], required=True)]


@pytest.mark.asyncio
async def test_resolve_returns_the_models_answer():
    loop = _Loop(_Provider(_response({"answers": [{"key": "branch", "status": "answer", "answer": "feat/x"}]})))
    out = await _autofill(loop).resolve(QUESTIONS, agent="raven-code", instance="a1b2")
    assert (out[0].status, out[0].answer) == ("answer", "feat/x")


@pytest.mark.asyncio
async def test_resolve_defers_everything_when_the_call_raises():
    loop = _Loop(_Provider(error=RuntimeError("provider down")))
    out = await _autofill(loop).resolve(QUESTIONS, agent="raven-code", instance="a1b2")
    assert [r.status for r in out] == ["defer"]


@pytest.mark.asyncio
async def test_resolve_defers_everything_when_the_call_outruns_its_budget():
    loop = _Loop(_Provider(_response({"answers": []}), delay=0.2))
    loop.subagent_questions_config = SubagentQuestionsConfig(autofill_timeout_seconds=0.01)
    out = await _autofill(loop).resolve(QUESTIONS, agent="raven-code", instance="a1b2")
    assert [r.status for r in out] == ["defer"]


@pytest.mark.asyncio
async def test_resolve_makes_no_call_when_the_switch_is_off():
    provider = _Provider(_response({"answers": []}))
    loop = _Loop(provider)
    loop.subagent_questions_config = SubagentQuestionsConfig(autofill_enabled=False)
    out = await _autofill(loop).resolve(QUESTIONS, agent="raven-code", instance="a1b2")
    assert [r.status for r in out] == ["defer"]
    assert provider.calls == []


@pytest.mark.asyncio
async def test_resolve_recalls_on_the_question_not_on_the_turn():
    seen = {}

    class _Backend:
        async def recall(self, query, *, user_id=None, agent_id=None, top_k=5):
            seen["query"] = query
            return [SimpleNamespace(content="reviewer is chandler")]

    loop = _Loop(_Provider(_response({"answers": []})), backend=_Backend())
    auto = _autofill(loop, snapshot=[{"role": "user", "content": "ship the thing"}])
    await auto.resolve(QUESTIONS, agent="raven-code", instance="a1b2")
    # Keyed on the question, not on "ship the thing": the recall already in the
    # assembled context used the user's message and would miss this.
    assert "Which branch?" in seen["query"]


@pytest.mark.asyncio
async def test_resolve_survives_a_recall_that_raises():
    class _Backend:
        async def recall(self, query, *, user_id=None, agent_id=None, top_k=5):
            raise RuntimeError("everos down")

    loop = _Loop(
        _Provider(_response({"answers": [{"key": "branch", "status": "answer", "answer": "feat/x"}]})),
        backend=_Backend(),
    )
    out = await _autofill(loop).resolve(QUESTIONS, agent="raven-code", instance="a1b2")
    assert out[0].status == "answer"


@pytest.mark.asyncio
async def test_resolve_sends_the_turn_snapshot_as_the_prefix():
    provider = _Provider(_response({"answers": []}))
    auto = _autofill(_Loop(provider), snapshot=[{"role": "user", "content": "push it to feat/x"}])
    await auto.resolve(QUESTIONS, agent="raven-code", instance="a1b2")
    assert {"role": "user", "content": "push it to feat/x"} in provider.calls[0]["messages"]


@pytest.mark.asyncio
async def test_a_spawn_question_resolves_against_a_provider_that_validates_the_sequence():
    # Nearly every provider validates this, and the spawn call is open at exactly
    # the moment the sub-agent under it asks -- so without the synthesised
    # results this feature never autofills a spawn question at all.
    provider = _StrictProvider(_response({"answers": [{"key": "branch", "status": "answer", "answer": "feat/x"}]}))
    auto = _autofill(_Loop(provider), snapshot=_spawn_snapshot())
    out = await auto.resolve(QUESTIONS, agent="raven-code", instance="a1b2")
    assert [r.status for r in out] == ["answer"]
    assert out[0].answer == "feat/x"


@pytest.mark.asyncio
async def test_resolve_defers_before_any_snapshot_was_published():
    # An unwired host has no turn to continue, so there is nothing to decide
    # from: recall and priors alone are strictly less than raven itself sees,
    # and the call must not be made at all.
    provider = _Provider(_response({"answers": [{"key": "branch", "status": "answer", "answer": "feat/x"}]}))
    auto = Autofill(_Loop(provider), emit=None, conversation_id="tui:c1", config=SubagentQuestionsConfig())
    out = await auto.resolve(QUESTIONS, agent="raven-code", instance="a1b2")
    assert [r.status for r in out] == ["defer"]
    assert provider.calls == []


@pytest.mark.asyncio
async def test_a_published_but_empty_turn_still_resolves():
    # `None` is "never published"; `[]` is a turn that has nothing in it yet,
    # which is a context and must not be mistaken for the other.
    loop = _Loop(_Provider(_response({"answers": [{"key": "branch", "status": "answer", "answer": "feat/x"}]})))
    out = await _autofill(loop, snapshot=[]).resolve(QUESTIONS, agent="raven-code", instance="a1b2")
    assert out[0].status == "answer"


@pytest.mark.asyncio
async def test_resolve_calls_the_provider_the_snapshot_was_taken_on():
    # `provider` is a property over the turn's binding, and this call runs on
    # the ACP connection's read loop, which carries a copy of the first turn's
    # ContextVars. Read at question time, a session that switched model would
    # send this to the previous binding's provider and credential.
    first, second = _Provider(_response({"answers": []})), _Provider(_response({"answers": []}))
    loop = _Loop(first)
    auto = _autofill(loop)
    loop.provider = second
    await auto.resolve(QUESTIONS, agent="raven-code", instance="a1b2")
    assert second.calls == []
    assert len(first.calls) == 1


@pytest.mark.asyncio
async def test_resolve_sends_the_turns_model_rather_than_the_providers_default():
    provider = _Provider(_response({"answers": []}))
    loop = _Loop(provider, model="anthropic/turn-model")
    auto = _autofill(loop)
    loop.model = "anthropic/switched-since"
    await auto.resolve(QUESTIONS, agent="raven-code", instance="a1b2")
    assert provider.calls[0]["model"] == "anthropic/turn-model"


def test_current_autofill_is_none_before_a_turn_binds_one():
    from raven.agent.acp_client.asker import current_autofill, start_ask_turn

    start_ask_turn(None, conversation_id="tui:c1")
    assert current_autofill() is None


def test_start_ask_turn_binds_the_autofill():
    from raven.agent.acp_client.asker import current_autofill, start_ask_turn

    marker = object()
    start_ask_turn(None, marker, conversation_id="tui:c1")
    assert current_autofill() is marker


def test_start_ask_turn_still_takes_one_positional_asker():
    # Every existing caller passes the asker positionally and unpacks two values
    # from current_ask; neither may change.
    from raven.agent.acp_client.asker import current_ask, current_autofill, start_ask_turn

    start_ask_turn("asker-obj", conversation_id="tui:c1")
    assert current_ask() == ("asker-obj", "tui:c1")
    assert current_autofill() is None


@pytest.mark.asyncio
async def test_an_autofilled_form_emits_a_tool_row():
    events = []
    loop = _Loop(_Provider(_response({"answers": [{"key": "branch", "status": "answer", "answer": "feat/x"}]})))
    auto = _autofill(loop, emit=events.append)
    await auto.resolve(QUESTIONS, agent="raven-code", instance="a1b2")
    assert [e.phase for e in events] == [ToolPhase.START, ToolPhase.COMPLETE]
    assert events[0].name == "answer_for_user"
    assert events[0].conversation_id == "tui:c1"
    assert "feat/x" in events[1].result_preview


@pytest.mark.asyncio
async def test_a_form_that_answered_nothing_emits_no_row():
    events = []
    loop = _Loop(_Provider(_response({"answers": [{"key": "branch", "status": "defer"}]})))
    auto = _autofill(loop, emit=events.append)
    await auto.resolve(QUESTIONS, agent="raven-code", instance="a1b2")
    assert events == []
    # Not only the emit: an all-defer form that still recorded would poison the
    # next form's prompt with a ledger entry and hand `_flush_autofill` a row
    # for a decision nobody made.
    assert auto.pending_rows() == []
    assert auto._ledger == []


@pytest.mark.asyncio
async def test_one_row_per_form_not_per_question():
    events = []
    questions = [
        autofill.Question(key="branch", prompt="Which branch?", options=[], required=True),
        autofill.Question(key="reviewer", prompt="Which reviewer?", options=[], required=False),
    ]
    loop = _Loop(
        _Provider(
            _response(
                {
                    "answers": [
                        {"key": "branch", "status": "answer", "answer": "feat/x"},
                        {"key": "reviewer", "status": "defer"},
                    ]
                }
            )
        )
    )
    auto = _autofill(loop, emit=events.append)
    await auto.resolve(questions, agent="raven-code", instance="a1b2")
    assert len(events) == 2
    assert "Which reviewer?" in events[1].result_preview


@pytest.mark.asyncio
async def test_an_emit_that_raises_does_not_fail_the_question():
    async def _boom(_event):
        raise RuntimeError("outlet gone")

    loop = _Loop(_Provider(_response({"answers": [{"key": "branch", "status": "answer", "answer": "feat/x"}]})))
    auto = _autofill(loop, emit=_boom)
    out = await auto.resolve(QUESTIONS, agent="raven-code", instance="a1b2")
    assert out[0].status == "answer"


@pytest.mark.asyncio
async def test_pending_rows_hands_the_rows_over_once():
    loop = _Loop(_Provider(_response({"answers": [{"key": "branch", "status": "answer", "answer": "feat/x"}]})))
    auto = _autofill(loop)
    await auto.resolve(QUESTIONS, agent="raven-code", instance="a1b2")
    rows = auto.pending_rows()
    assert rows[0]["agent"] == "raven-code"
    assert rows[0]["instance"] == "a1b2"
    # The sub-agent's wording is not carried: a row becomes the arguments of a
    # synthetic assistant `tool_calls` entry, which nothing fences.
    assert "questions" not in rows[0]
    assert "feat/x" in rows[0]["summary"]
    assert auto.pending_rows() == []


def test_summarise_says_where_each_question_went():
    questions = [
        autofill.Question(key="branch", prompt="Which branch?", options=[], required=True),
        autofill.Question(key="reviewer", prompt="Which reviewer?", options=[], required=False),
        autofill.Question(key="when", prompt="When?", options=[], required=False),
    ]
    resolutions = [
        autofill.Resolution(status="answer", answer="feat/x"),
        autofill.Resolution(status="partial", known="one of chandler or dizhan"),
        autofill.Resolution(status="defer"),
    ]
    lines = resolver.summarise(questions, resolutions).splitlines()
    assert lines[0] == "Which branch? -> feat/x (answered for you)"
    assert lines[1] == "Which reviewer? -> asked you, with: one of chandler or dizhan"
    assert lines[2] == "When? -> asked you"
