"""The stream's test for a thought that repeats itself, and the stop it leads to."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from raven.agent.loop import recovery
from raven.providers.base import ChatDelta
from raven.providers.reasoning_loop import LOOP_WINDOW_CHARS, REASONING_LOOP, reasoning_loops
from raven.providers.streaming import stream_llm_call

# The shape of the measured loop: one line whose slot changes, until the ceiling.
_LOOP = "".join(f'Need maybe mention "section {i}" from shattered.io. Good.\n' for i in range(400))
_PROSE = Path(recovery.__file__).read_text(encoding="utf-8")


def test_a_templated_line_repeated_is_a_loop():
    assert reasoning_loops(_LOOP)


def test_prose_and_code_are_no_loop_at_any_length():
    assert not any(reasoning_loops(_PROSE[:end]) for end in range(LOOP_WINDOW_CHARS, len(_PROSE), 500))


def test_a_thought_shorter_than_the_window_is_never_judged():
    assert not reasoning_loops(_LOOP[: LOOP_WINDOW_CHARS - 1])


class _Thinking:
    """A provider whose stream thinks in ``pieces`` and then says ``answer``; it counts what it sent."""

    def __init__(self, pieces: list[str], answer: str = "") -> None:
        self.pieces = pieces
        self.answer = answer
        self.sent = 0
        self.closed = False

    async def chat_stream(self, **_kwargs: Any):
        try:
            for piece in self.pieces:
                self.sent += 1
                yield ChatDelta(content=None, reasoning_content=piece)
            if self.answer:
                yield ChatDelta(content=self.answer)
            yield ChatDelta(content=None, finish_reason="length")
        finally:
            self.closed = True


def _pieces(text: str, size: int = 200) -> list[str]:
    return [text[i : i + size] for i in range(0, len(text), size)]


@pytest.mark.asyncio
async def test_a_looping_thought_is_stopped_before_the_ceiling():
    provider = _Thinking(_pieces(_LOOP * 4))

    response = await stream_llm_call(provider, messages=[{"role": "user", "content": "x"}], tools=None, model="m")

    assert response.finish_reason == REASONING_LOOP
    assert response.content == "" and not response.tool_calls
    assert provider.sent < len(provider.pieces) // 2, "the stream was read past the loop"
    assert provider.closed, "the upstream stream was left open"
    assert recovery.looped(response)


@pytest.mark.asyncio
async def test_an_ordinary_thought_runs_to_its_answer():
    provider = _Thinking(_pieces(_PROSE), answer="the answer")

    response = await stream_llm_call(provider, messages=[{"role": "user", "content": "x"}], tools=None, model="m")

    assert response.finish_reason == "length"
    assert response.content == "the answer"
    assert provider.sent == len(provider.pieces)
