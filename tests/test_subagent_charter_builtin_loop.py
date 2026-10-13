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

from raven.agent.subagent.backends.raven_loop import RavenLoopBackend
from raven.agent.subagent.delegate import dispatch_charter
from raven.providers.base import LLMProvider, LLMResponse


class _Provider(LLMProvider):
    """Records what each call carried, and that it happened at all."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.calls = 0
        self.last_tools: list[dict[str, Any]] | None = None
        self.last_system = ""

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
        return LLMResponse(content="provider-called", finish_reason="stop")


async def _run_with(functions: dict[str, str]) -> tuple[str, _Provider]:
    tmp = Path(tempfile.mkdtemp())
    provider = _Provider()
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
async def test_archive_is_asked_once_the_turn_has_its_answer() -> None:
    """Asked for its own sake: a dispatched run hands back a string, so what it
    files is a side effect rather than something the caller reads."""
    answer, _ = await _run_with({"archive": "def archive(step, reply):\n    return {'seen': reply}"})

    assert answer == "provider-called"


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
