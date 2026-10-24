"""Every verb a generated seat may answer, asked by the in-process worker loop.

The verbs became generatable before this loop asked them, so a `kind: builtin`
seat accepted generated policy and then ignored it -- the shape a reviewer
found by giving a charter a `system_addendum` that ends the turn and watching
the model get called anyway. Driven through `RavenLoopBackend.run` rather than
through the participant directly, because the participant answering is not the
thing that was broken.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

import pytest

from raven.agent.subagent import activity
from raven.agent.subagent.backends.raven_loop import RavenLoopBackend
from raven.agent.subagent.delegate import dispatch_charter
from raven.agent.tools import filesystem
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest


class _Provider(LLMProvider):
    """Records what each call carried, and that it happened at all."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.calls = 0
        self.last_tools: list[dict[str, Any]] | None = None
        self.last_system = ""
        self.tool_call: tuple[str, dict[str, Any]] | None = None
        self.last_messages: list[dict[str, Any]] = []

    def get_default_model(self) -> str:
        return "stub"

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return await self._answer(messages, tools)

    async def chat_with_retry(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        return await self._answer(messages, tools)

    async def _answer(self, messages, tools) -> LLMResponse:
        self.calls += 1
        self.last_tools = list(tools or [])
        self.last_system = next((str(m.get("content", "")) for m in messages if m.get("role") == "system"), "")
        self.last_messages = [dict(m) for m in messages]
        if self.tool_call is not None and self.calls == 1:
            name, arguments = self.tool_call
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name=name, arguments=arguments)],
                finish_reason="tool_calls",
            )
        return LLMResponse(content="provider-called", finish_reason="stop")


async def _run_with(
    functions: dict[str, str], tool_call: tuple[str, dict[str, Any]] | None = None
) -> tuple[str, _Provider]:
    tmp = Path(tempfile.mkdtemp())
    provider = _Provider()
    provider.tool_call = tool_call
    backend = RavenLoopBackend(provider=provider, model="stub", agent_home=tmp / "home")
    with dispatch_charter({"brief": "x", "functions": functions}):
        answer = await backend.run("do it", task_id="t1", workspace=tmp, executor=None)
    return answer, provider


@pytest.mark.asyncio
async def test_a_system_addendum_that_replies_ends_the_turn_before_any_model_call() -> None:
    """The case that was silently ignored: the loop called the model anyway and
    returned its text, so a seat's own gate read as if it had never been set."""
    answer, provider = await _run_with(
        {
            "system_addendum": "def system_addendum(step):\n    return {'text': None, 'reply': 'ended-here', 'note': None}"
        }
    )

    assert answer == "ended-here"
    assert provider.calls == 0, "the reply ends the turn before the call, not after it"


@pytest.mark.asyncio
async def test_a_system_addendum_reaches_the_system_prefix_of_the_call() -> None:
    answer, provider = await _run_with(
        {
            "system_addendum": "def system_addendum(step):\n    return {'text': 'house rule', 'reply': None, 'note': None}"
        }
    )

    assert answer == "provider-called"
    assert provider.last_system.endswith("house rule")


@pytest.mark.asyncio
async def test_select_tools_narrows_what_the_call_carries() -> None:
    _, provider = await _run_with({"select_tools": "def select_tools(offered, step):\n    return []"})

    assert provider.last_tools == [], "an explicit empty array is an answer, not silence"


@pytest.mark.asyncio
async def test_select_tools_cannot_hand_itself_a_tool_the_install_lacks() -> None:
    """Narrowing only, like every other charter field."""
    _, provider = await _run_with(
        {"select_tools": "def select_tools(offered, step):\n    return [{'function': {'name': 'not_installed'}}]"}
    )

    assert provider.last_tools == []


@pytest.mark.asyncio
async def test_review_can_close_the_turn_with_its_own_reply() -> None:
    answer, provider = await _run_with(
        {"review": "def review(step):\n    return {'verdict': 'end', 'reply': 'ended-by-review', 'note': None}"}
    )

    assert answer == "ended-by-review"
    assert provider.calls == 1, "review judges a step, so the step has to have happened"


@pytest.mark.asyncio
async def test_a_review_that_always_resamples_is_bounded_by_the_loop() -> None:
    """A participant pronounces; the loop decides what that costs."""
    from raven.agent.subagent.backends.raven_loop import MAX_PARTICIPANT_RESAMPLES

    answer, provider = await _run_with(
        {
            "review": (
                "def review(step):\n"
                "    return {'verdict': 'resample', 'reason': 'thin', 'inject': None, 'overrides': None, 'note': None}"
            )
        }
    )

    assert answer == "provider-called"
    assert provider.calls == MAX_PARTICIPANT_RESAMPLES + 1


@pytest.mark.asyncio
async def test_what_archive_answers_is_filed_on_the_run_record() -> None:
    """A generated ``archive`` is pure: the mapping it answers with is the whole
    of its effect. Asserted on the record rather than on the reply, because a
    test that only checked the answer passed while the verb was never asked."""
    with activity.collecting() as collected:
        answer, _ = await _run_with({"archive": "def archive(step, reply):\n    return {'audit': {'seen': reply}}"})

    assert answer == "provider-called"
    assert collected.as_meta()["archived"] == {"audit": {"seen": "provider-called"}}


@pytest.mark.asyncio
async def test_a_review_at_execute_tools_keeps_the_proposed_calls_from_running() -> None:
    """The moment this phase exists for. Asserted on the tool, not on the
    reply: a verdict that arrives after the call has run is a verdict on work
    already done, and the reply reads the same either way."""
    ran: list[str] = []

    async def _spy(self, **kwargs) -> str:
        ran.append(str(kwargs.get("path", "")))
        return "contents"

    source = (
        "def review(step):\n"
        "    if step.get('phase') == 'execute_tools':\n"
        "        return {'verdict': 'end', 'reply': 'blocked', 'note': None}\n"
        "    return None"
    )
    original = filesystem.ReadFileTool.execute
    filesystem.ReadFileTool.execute = _spy
    try:
        answer, _ = await _run_with({"review": source}, tool_call=("read_file", {"path": "input.txt"}))
    finally:
        filesystem.ReadFileTool.execute = original

    assert answer == "blocked"
    assert ran == [], "the calls this verdict refused must not have run"


@pytest.mark.asyncio
async def test_a_verb_that_answers_nonsense_leaves_the_turn_alone() -> None:
    """A mapping the host cannot read is silence, not an error."""
    answer, provider = await _run_with(
        {
            "review": "def review(step):\n    return {'verdict': 'maybe'}",
            "system_addendum": "def system_addendum(step):\n    return 'not a mapping'",
        }
    )

    assert answer == "provider-called"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_a_resample_puts_what_it_injected_in_front_of_the_next_call() -> None:
    """``inject`` is transcript, not a note: a verdict that supplies messages
    has them appended as themselves, and only one that supplies none falls back
    to its reason."""
    source = (
        "def review(step):\n"
        "    if step.get('iteration') == 1:\n"
        "        return {'verdict': 'resample', 'reason': 'thin',\n"
        "                'inject': [{'role': 'user', 'content': 'try harder'}],\n"
        "                'overrides': None, 'note': None}\n"
        "    return None"
    )
    _, provider = await _run_with({"review": source})

    assert provider.calls == 2
    assert any("try harder" == str(m.get("content")) for m in provider.last_messages)
    assert not any("thin" == str(m.get("content")) for m in provider.last_messages), (
        "a verdict that injected messages does not also append its reason"
    )


@pytest.mark.asyncio
async def test_select_tools_reads_a_bare_name_as_well_as_a_function_wrapper() -> None:
    """The offered array carries one shape, but a generated verb builds the
    mapping it was shown; both spellings name the same tool."""
    source = (
        "def select_tools(offered, step):\n"
        "    return [{'name': t.get('function', {}).get('name', '')} for t in offered][:1]"
    )
    _, provider = await _run_with({"select_tools": source})

    assert provider.last_tools is not None
    assert len(provider.last_tools) == 1


@pytest.mark.asyncio
async def test_a_review_whose_inject_is_junk_still_carries_its_reason() -> None:
    source = (
        "def review(step):\n"
        "    if step.get('iteration') == 1:\n"
        "        return {'verdict': 'resample', 'reason': 'say why', 'inject': ['not a message'],\n"
        "                'overrides': None, 'note': None}\n"
        "    return None"
    )
    _, provider = await _run_with({"review": source})

    assert provider.calls == 2
    assert any("say why" in str(m.get("content", "")) for m in provider.last_messages)


@pytest.mark.asyncio
async def test_review_is_asked_again_once_the_tool_results_exist() -> None:
    """The second of the two moments one tool-calling step has. The first
    decides whether the calls run; this one judges what they returned, and it
    is the last moment before the next model call consumes them."""
    ran: list[str] = []

    async def _spy(self, **kwargs) -> str:
        ran.append(str(kwargs.get("path", "")))
        return "contents"

    source = (
        "def review(step):\n"
        "    if step.get('phase') == 'after_iteration' and step.get('response', {}).get('tool_calls'):\n"
        "        return {'verdict': 'end', 'reply': 'ended-after-results', 'note': None}\n"
        "    return None"
    )
    original = filesystem.ReadFileTool.execute
    filesystem.ReadFileTool.execute = _spy
    try:
        answer, provider = await _run_with({"review": source}, tool_call=("read_file", {"path": "input.txt"}))
    finally:
        filesystem.ReadFileTool.execute = original

    assert answer == "ended-after-results"
    assert ran == ["input.txt"], "this moment is after the calls, so they have run"
    assert provider.calls == 1, "and before the call that would have consumed their results"


@pytest.mark.asyncio
async def test_one_tool_calling_step_offers_review_both_moments() -> None:
    """Asserted as the pair rather than one at a time: the two were collapsed
    into one once, and a test for either alone stayed green while the other
    was missing."""
    seen: list[tuple[str, bool]] = []

    async def _spy(self, **kwargs) -> str:
        return "contents"

    source = (
        "def review(step):\n"
        "    return {'verdict': 'accept', 'note': step.get('phase', '') + ':' + str(step.get('tools_ran'))}"
    )
    original = filesystem.ReadFileTool.execute
    filesystem.ReadFileTool.execute = _spy
    try:
        from raven.agent.subagent import charter as charter_mod

        real = charter_mod.CharterParticipant.review

        async def _record(self, step):
            seen.append((step.phase, step.tools_ran))
            return await real(self, step)

        charter_mod.CharterParticipant.review = _record
        try:
            await _run_with({"review": source}, tool_call=("read_file", {"path": "input.txt"}))
        finally:
            charter_mod.CharterParticipant.review = real
    finally:
        filesystem.ReadFileTool.execute = original

    assert ("execute_tools", False) in seen, "the moment that can keep the calls from running"
    assert ("after_iteration", True) in seen, "the moment that judges what they returned"


class _RecordingKwargs(_Provider):
    """Records the generation keywords each answered call carried, on any path.

    The second call proposes a tool call, so the step after the re-sampled one
    is an ordinary continuing step rather than another verdict.
    """

    def __init__(self, path: str) -> None:
        super().__init__()
        self.path = path
        self.kwargs_per_call: list[dict[str, Any]] = []

    async def _answer(self, messages, tools) -> LLMResponse:
        answer = await super()._answer(messages, tools)
        if self.calls == 2:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c2", name="read_file", arguments={"path": "missing.txt"})],
                finish_reason="tool_calls",
            )
        return answer

    async def chat_with_retry(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        self.kwargs_per_call.append(dict(kwargs))
        return await self._answer(messages, tools)

    async def chat_stream(self, *, messages, tools=None, model=None, **kwargs):
        from raven.providers.base import ChatDelta

        if self.path == "re-asked":
            yield ChatDelta(content="dropped", finish_reason="error")
            return
        self.kwargs_per_call.append(dict(kwargs))
        answer = await self._answer(messages, tools)
        for call in answer.tool_calls:
            fragment = {"index": 0, "id": call.id, "function": {"name": call.name, "arguments": '{"path": "x"}'}}
            yield ChatDelta(content=None, tool_call_delta={"tool_calls": [fragment]})
        yield ChatDelta(content=answer.content or None, finish_reason=answer.finish_reason)

    @classmethod
    def classify_error(cls, exc=None, content=None):
        from raven.contracts.llm_provider import ErrorClassification

        return ErrorClassification("transport", retryable=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["waited", "streamed", "re-asked"])
async def test_a_resample_carries_its_overrides_on_the_next_call_and_no_other(path: str) -> None:
    """The contract says ``overrides`` ride on the re-sampled call, and the ACP
    lane honours that. A key outside the allowlist is dropped there, so it is
    dropped here too rather than handed to a provider that does not know it.
    The re-sampled call then proposes a tool call, so the third call is an
    ordinary step and shows whether the overrides outlived the call they were
    for. ``re-asked`` is a streamed reply that failed in transport and was
    asked again without the stream."""
    source = (
        "def review(step):\n"
        "    if step.get('iteration') == 1:\n"
        "        return {'verdict': 'resample', 'reason': 'colder', 'inject': None,\n"
        "                'overrides': {'temperature': 0.1, 'max_tokens': 77, 'model': 'other'}, 'note': None}\n"
        "    return None"
    )
    tmp = Path(tempfile.mkdtemp())
    provider = _RecordingKwargs(path)
    backend = RavenLoopBackend(provider=provider, model="stub", agent_home=tmp / "home")

    async def _delta(text: str) -> None:
        return None

    with dispatch_charter({"brief": "x", "functions": {"review": source}}):
        await backend.run(
            "do it", task_id="t1", workspace=tmp, executor=None, on_delta=None if path == "waited" else _delta
        )

    first, second, third = provider.kwargs_per_call
    assert second == {**first, "temperature": 0.1, "max_tokens": 77}
    assert third == first


def test_the_resample_allowlist_is_the_main_loops_rollback_allowlist() -> None:
    """One verdict, two lanes: a key one honours and the other drops would make
    the same generated ``review`` behave differently by where it runs."""
    from raven.agent.loop import AgentLoop
    from raven.agent.subagent.backends.raven_loop import RESAMPLE_OVERRIDE_KEYS

    assert RESAMPLE_OVERRIDE_KEYS == AgentLoop._ROLLBACK_OVERRIDE_KEYS


def test_the_generation_guidance_names_the_keys_a_resample_may_move() -> None:
    """The model writing ``review`` reads the keys here and nowhere else, so a
    key added to the allowlist and not to this text is one no seat will use."""
    import re

    from raven.agent.subagent.backends.raven_loop import RESAMPLE_OVERRIDE_KEYS
    from raven.playbook.agent_generator import _FUNCTION_CONTRACTS

    returns = " ".join(_FUNCTION_CONTRACTS["review"]["returns"].split())
    named = re.search(r"overrides \(([^)]*) for that one call", returns)

    assert named is not None, returns
    assert set(re.findall(r"[a-z_]+", named.group(1))) - {"or"} == RESAMPLE_OVERRIDE_KEYS


@pytest.mark.asyncio
async def test_archive_is_asked_at_the_phase_the_main_hook_names_for_the_same_moment() -> None:
    """The main hook files its record at ``sent``. A generated ``archive``
    that reads ``step['phase']`` has to match on both lanes, so this lane
    names that moment the same way rather than coining a word of its own."""
    with activity.collecting() as collected:
        await _run_with({"archive": "def archive(step, reply):\n    return {'phase': step.get('phase')}"})

    assert collected.as_meta()["archived"] == {"phase": "sent"}


_ENDED_EARLY = {
    "intake": (
        {"intake": "def intake(text, step):\n    return {'text': None, 'reply': 'ended-at-intake', 'note': None}"},
        None,
        "ended-at-intake",
    ),
    "system_addendum": (
        {"system_addendum": "def system_addendum(step):\n    return {'text': None, 'reply': 'ended', 'note': None}"},
        None,
        "ended",
    ),
    "review before the calls": (
        {
            "review": (
                "def review(step):\n"
                "    if step.get('phase') == 'execute_tools':\n"
                "        return {'verdict': 'end', 'reply': 'blocked', 'note': None}\n"
                "    return None"
            )
        },
        ("read_file", {"path": "missing.txt"}),
        "blocked",
    ),
    "review after the calls": (
        {
            "review": (
                "def review(step):\n"
                "    if step.get('phase') == 'after_iteration' and step.get('tools_ran'):\n"
                "        return {'verdict': 'end', 'reply': 'ended-after-results', 'note': None}\n"
                "    return None"
            )
        },
        ("read_file", {"path": "missing.txt"}),
        "ended-after-results",
    ),
    "review of a plain answer": (
        {"review": "def review(step):\n    return {'verdict': 'end', 'reply': 'ended-by-review', 'note': None}"},
        None,
        "ended-by-review",
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", list(_ENDED_EARLY))
async def test_a_turn_another_verb_ends_is_still_archived(path: str) -> None:
    """``archive`` runs once after the turn is over, however it ended. A verb
    that ends the turn with its own reply is a turn that is over, and the
    record has to say so the same as one the model answered."""
    functions, tool_call, reply = _ENDED_EARLY[path]
    archive = "def archive(step, reply):\n    return {'audit': {'seen': reply, 'phase': step.get('phase')}}"
    with activity.collecting() as collected:
        answer, _ = await _run_with({**functions, "archive": archive}, tool_call=tool_call)

    assert answer == reply
    assert collected.as_meta().get("archived") == {"audit": {"seen": reply, "phase": "sent"}}


def test_every_phase_this_lane_asks_at_is_in_the_contract_and_said_by_the_main_hook() -> None:
    """The vocabulary lives in the contract's prose, and the two lanes are
    read by the same generated verbs. Read from the source so a new call site
    with a coined phase fails here rather than in a verb that never matches."""
    import re

    import raven.agent.hook.participant as main_hook
    import raven.agent.subagent.backends.raven_loop as builtin_lane
    import raven.contracts.participant as contract

    contract_source = Path(contract.__file__).read_text(encoding="utf-8")
    described = contract_source.split("phase: str", 1)[1].split('"""', 2)[1].split(". ", 1)[0]
    vocabulary = set(re.findall(r"``([a-z_]+)``", described))
    asked_here = set(re.findall(r'participant_step\("([a-z_]+)"', Path(builtin_lane.__file__).read_text("utf-8")))
    said_there = set(re.findall(r'phase="([a-z_]+)"', Path(main_hook.__file__).read_text("utf-8")))

    assert vocabulary == {"user_inbound", "iteration", "execute_tools", "after_iteration", "answerless", "sent"}
    assert asked_here, "the pattern still finds this lane's call sites"
    assert asked_here <= vocabulary, asked_here - vocabulary
    assert asked_here <= said_there, asked_here - said_there
