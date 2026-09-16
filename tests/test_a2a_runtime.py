"""The one-shot turn `raven/a2a/runtime.py` runs for an inbound A2A task.

Driven against a real `AgentLoop` with only the LLM and sandbox edges faked.
That is the point of the file rather than an implementation detail: every other
test on this path substitutes `run_turn` itself, so the callables the one-shot
path builds are only ever handed to a double, and a double uses them however
the test author assumed they were used.
"""

from __future__ import annotations

from pathlib import Path

from raven.a2a.runtime import make_run_turn
from raven.agent.loop import AgentLoop
from raven.contracts.llm_provider import LLMResponse


class _ScriptedChatProvider:
    """`_run_one_shot_turn` runs with ``stream=False``, so the turn takes the
    ``chat_with_retry`` path rather than ``chat_stream``."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self._i = 0

    async def chat_with_retry(self, **_kwargs: object) -> LLMResponse:
        response = self._responses[min(self._i, len(self._responses) - 1)]
        self._i += 1
        return response

    def get_default_model(self) -> str:
        return "fake/model"


def _loop_without_edges(workspace: Path, *, reply: str) -> AgentLoop:
    """A real loop with the sandbox and MCP bring-up no-oped, so a text-only
    turn needs no VM and no server."""
    loop = AgentLoop(provider=_ScriptedChatProvider([LLMResponse(content=reply)]), workspace=workspace)

    async def _noop(**_kw: object) -> None:
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop
    return loop


async def test_a_turn_against_a_real_loop_comes_back_with_its_text(tmp_path: Path) -> None:
    """The whole one-shot path, end to end, with nothing standing in for the turn.

    This is the only test that would have caught the runner callables being
    built to the wrong contract: `raven/spine/runner.py` types them
    asymmetrically -- `Emit` returns an Awaitable, `Drain` returns a list --
    and `turn_path.py` iterates the drain's return value directly, so an
    ``async def drain`` fails the turn with "'coroutine' object is not
    iterable" while every stand-in test stays green.
    """
    text = await make_run_turn(_loop_without_edges(tmp_path, reply="the real answer"))(
        "do the thing", conversation_id="a2a:real-loop", broker=None
    )

    assert text == "the real answer"


async def test_the_one_shot_turn_builds_runner_callables_to_the_spine_contract(tmp_path: Path) -> None:
    """The same defect stated as the contract it breaks, so a failure names the cause.

    The test above reports a TypeError from deep inside the turn path; this one
    says which callable is wrong and how. Both are kept: one proves the real
    turn runs, the other explains it when it stops.
    """
    captured: dict[str, object] = {}

    loop = _loop_without_edges(tmp_path, reply="unused")

    async def capturing_run_turn(_req, emit, drain, **_kwargs: object) -> None:
        captured["emit"], captured["drain"] = emit, drain

    loop.run_turn = capturing_run_turn
    await make_run_turn(loop)("do the thing", conversation_id="a2a:contract", broker=None)

    # Drain = Callable[[], list[TurnRequest]]: called with no arguments, and its
    # return value iterated without awaiting it.
    assert list(captured["drain"]()) == []
    # Emit = Callable[[RunnerEvent], Awaitable[None]]: the asymmetric one.
    assert await captured["emit"](object()) is None
