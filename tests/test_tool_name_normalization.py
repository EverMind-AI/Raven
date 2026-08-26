"""A tool name an upstream sent is a key only after it is normalised.

An upstream returned tool names carrying a leading space. Raven used them as
registry keys unchanged, so the lookup missed and the call was refused -- and
because the name is also written back into the history, the bad one was
replayed to the model on every turn afterwards, which is why the model could
not correct it by reasoning about it.

Both response paths are pinned, because the fault is not in either of them: it
is in treating a string an upstream wrote as an identifier this repo declared.
The boundary is pinned too. Stripping surrounding whitespace is a fact (a
registry key is written in code and has none); folding case or interior spaces
would be a guess, and "that tool does not exist" is only worth something while
it is certain.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from raven.agent.loop.streaming import _finalize_tool_calls
from raven.providers.base import normalized_tool_name
from raven.providers.litellm_provider import LiteLLMProvider


@dataclass
class _Msg:
    content: str | None = None
    tool_calls: list[Any] | None = None
    reasoning_content: str | None = None


@dataclass
class _Choice:
    message: _Msg
    finish_reason: str = "tool_calls"


@dataclass
class _Usage:
    prompt_tokens: int = 2900
    completion_tokens: int = 12
    total_tokens: int = 2912


@dataclass
class _Response:
    choices: list[_Choice]
    usage: _Usage = field(default_factory=_Usage)


def _raw_call(name: str) -> SimpleNamespace:
    """The shape `_parse_response` reads a tool call out of."""
    return SimpleNamespace(
        function=SimpleNamespace(name=name, arguments='{"path": "a.txt"}', provider_specific_fields=None),
        provider_specific_fields=None,
    )


def _slot(name: Any) -> dict[str, Any]:
    """The shape the streaming accumulator hands to `_finalize_tool_calls`."""
    return {"id": "call_1", "function": {"name": name, "arguments_buf": ['{"path": "a.txt"}']}}


def _provider() -> LiteLLMProvider:
    with (
        patch("raven.providers.litellm_provider.litellm"),
        patch("raven.providers.litellm_provider.LiteLLMProvider._setup_env"),
    ):
        return LiteLLMProvider(api_key="sk-test", provider_name="openrouter")


def _parsed(name: str) -> Any:
    response = _Response(choices=[_Choice(message=_Msg(tool_calls=[_raw_call(name)]))])
    return _provider()._parse_response(response, sent_chars=12000)


# ---------- both parse exits -------------------------------------------------


def test_the_non_streaming_exit_hands_on_a_usable_key() -> None:
    """The observed shape: a leading space, on the path the incident took."""
    assert [tc.name for tc in _parsed(" read_file").tool_calls] == ["read_file"]


def test_the_streaming_exit_hands_on_a_usable_key() -> None:
    """The same question on the other path. A verdict reached on one and not
    the other makes the same upstream read differently inside the TUI than
    outside it."""
    assert [tc.name for tc in _finalize_tool_calls([_slot(" read_file")])] == ["read_file"]


def test_the_name_written_back_into_the_history_is_the_clean_one() -> None:
    """Why this is fixed at the exit and not at the registry lookup.

    A lookup that stripped would resolve the call and still leave the bad name
    on the request -- which `to_openai_tool_call` writes into the assistant
    message, so every later turn replays it to the model. The model then reads
    its own bad name as precedent, which is the loop the incident showed.
    """
    call = _parsed("  read_file\n").tool_calls[0]

    assert call.to_openai_tool_call()["function"]["name"] == "read_file"


# ---------- the boundary, which is the point ---------------------------------


def test_an_interior_space_is_still_a_name_that_does_not_exist() -> None:
    """Not repaired, because repairing it would be a guess. Nothing declares
    that `read file` was meant to be `read_file`."""
    assert normalized_tool_name(" read file ") == "read file"


def test_case_is_not_folded() -> None:
    """`read_file` and `Read_File` can both be registered at once -- an MCP
    server is free to export either -- so folding them would answer a lookup
    with a name the caller never asked for."""
    assert normalized_tool_name(" Read_File") == "Read_File"


def test_a_name_that_is_only_whitespace_is_no_name_at_all() -> None:
    """It comes out empty, and the streaming exit drops an empty name. Passing
    it on would put a call with no name into the history."""
    assert _finalize_tool_calls([_slot("   ")]) == []


def test_a_name_that_is_not_a_string_is_handed_back_untouched() -> None:
    """A different fault with a different owner. Raising here would take down
    the parse of a response that may be otherwise fine, and the name still
    misses the registry exactly as it did before."""
    assert normalized_tool_name(None) is None
    assert normalized_tool_name(7) == 7
