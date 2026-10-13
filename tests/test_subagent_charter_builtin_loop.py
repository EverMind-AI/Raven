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
