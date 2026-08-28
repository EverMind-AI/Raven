"""A provider that says the turn failed, in the one word litellm rewrites.

`map_finish_reason` has no entry for a provider's `error` (nor zhipu's
`network_error`) and answers "stop" for both. The existing guard catches that
only when the completion came back empty -- so a turn that failed upstream after
saying a few words arrives looking like an agent that finished and chose to stop.
Measured: a run ended at iteration 26 of 600 with its deck unbuilt, and the only
trace anywhere was litellm's own "Unmapped finish_reason 'error'" warning.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from raven.providers.litellm_provider import LiteLLMProvider


def _provider() -> LiteLLMProvider:
    return LiteLLMProvider(api_key="x", api_base="https://openrouter.ai/api/v1", provider_name="openrouter")


def _response(*, content=None, tool_calls=None, native=None, finish="stop"):
    """A litellm completion, as `Choices` hands one over.

    `finish_reason` is already the mapped value and `provider_specific_fields`
    carries what it was rewritten from -- which is exactly what litellm does, and
    only when the mapping changed something.
    """
    message = SimpleNamespace(content=content, tool_calls=tool_calls, reasoning_content=None, thinking_blocks=None)
    choice = SimpleNamespace(
        message=message,
        finish_reason=finish,
        provider_specific_fields={"native_finish_reason": native} if native else None,
    )
    return SimpleNamespace(
        choices=[choice],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20, total_tokens=120),
    )


def test_a_turn_that_failed_upstream_after_saying_something_is_an_error() -> None:
    """The bug. One sentence of preamble, no tool call, and the provider saying the
    turn failed -- returned as `stop` this reads as the agent's final answer, and the
    run ends there."""
    parsed = _provider()._parse_response(_response(content="现在写入 20 页大纲。", native="error"))

    assert parsed.finish_reason == "error"
    assert parsed.error_classification is not None
    assert parsed.error_classification.retryable is True
    assert "upstream_error" in parsed.content


def test_zhipus_own_wording_counts_too() -> None:
    parsed = _provider()._parse_response(_response(content="半句话", native="network_error"))

    assert parsed.finish_reason == "error"


def test_a_turn_with_tool_calls_survives_even_so() -> None:
    """There is work in it. Discarding a turn that came back with something to
    execute would cost more than the failure it guards against."""
    call = SimpleNamespace(
        id="c1", type="function", function=SimpleNamespace(name="ppt_build", arguments="{}")
    )
    parsed = _provider()._parse_response(_response(content="building", tool_calls=[call], native="error"))

    assert parsed.finish_reason != "error"
    assert [c.name for c in parsed.tool_calls] == ["ppt_build"]


def test_an_ordinary_finished_turn_is_untouched() -> None:
    """No rewritten value means litellm changed nothing, which is the common case."""
    parsed = _provider()._parse_response(_response(content="the answer", finish="stop"))

    assert parsed.finish_reason == "stop"
    assert parsed.content == "the answer"
    assert parsed.error_classification is None


@pytest.mark.parametrize("native", ["length", "content_filter", "tool_calls"])
def test_a_rewritten_value_that_is_not_a_failure_is_left_alone(native: str) -> None:
    """Those say why the content is what it is, and each has its own handler."""
    parsed = _provider()._parse_response(_response(content="some words", native=native))

    assert parsed.finish_reason != "error"
