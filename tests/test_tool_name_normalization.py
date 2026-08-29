"""A tool name an upstream sent is a key only after it is normalised.

An upstream returned tool names carrying a leading space. Raven used them as
registry keys unchanged, so the lookup missed and the call was refused -- and
because the name is also written back into the history, the bad one was
replayed to the model on every turn afterwards, which is why the model could
not correct it by reasoning about it.

Every exit that builds a `ToolCallRequest` out of upstream data is pinned, and
a guard test enumerates them from the source rather than trusting this list --
the first version of this change covered two of the four and said it covered
all of them. Replay is deliberately not one of them: it reconstructs a recorded
run, and normalising there would make a replay diverge from the run it replays.

The boundary is pinned too. Stripping surrounding whitespace is a fact (a
registry key is written in code and has none); folding case or interior spaces
would be a guess, and "that tool does not exist" is only worth something while
it is certain.
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from raven.providers.azure_openai_provider import AzureOpenAIProvider
from raven.providers.litellm_provider import LiteLLMProvider
from raven.providers.streaming import _finalize_tool_calls
from raven.providers.tool_names import normalized_tool_name, sanitary_form


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


class _SseStream:
    """SSE response stand-in. Ends rather than stalling: `_consume_sse` returns
    when the stream does, so a trailing stall would trip the idle cap first."""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


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


def test_the_azure_exit_hands_on_a_usable_key() -> None:
    """The third exit. `AgentLoop` reaches `chat_with_retry` -- and so this
    parser -- whenever the turn caller wired no delta callback, which is every
    headless entry point."""
    provider = AzureOpenAIProvider(api_key="sk-test", api_base="https://x.openai.azure.com")
    response = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "function": {"name": " read_file", "arguments": '{"path": "a.txt"}'}}
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ]
    }

    assert [tc.name for tc in provider._parse_response(response).tool_calls] == ["read_file"]


@pytest.mark.asyncio
async def test_the_codex_exit_hands_on_a_usable_key() -> None:
    """The fourth exit. Its name arrives on one SSE event and the call is built
    on a later one, so the raw string outlives the event that carried it."""
    from raven.providers.openai_codex_provider import _consume_sse

    added = json.dumps(
        {"type": "response.output_item.added", "item": {"type": "function_call", "call_id": "c1", "name": " read_file"}}
    )
    done = json.dumps(
        {
            "type": "response.output_item.done",
            "item": {"type": "function_call", "call_id": "c1", "arguments": '{"path": "a.txt"}'},
        }
    )
    completed = json.dumps({"type": "response.completed"})

    _, tool_calls, _ = await _consume_sse(
        _SseStream([f"data: {added}", "", f"data: {done}", "", f"data: {completed}", ""]), timeout=1.0
    )

    assert [tc.name for tc in tool_calls] == ["read_file"]


def test_every_exit_that_builds_a_call_from_upstream_data_normalises_it() -> None:
    """The guard the first version of this change needed and did not have.

    Two of the four exits were covered while the docstring and this module both
    claimed all of them were, and nothing went red. Reading the tree rather than
    a hand-kept list is what makes a fifth exit arrive as a failure here instead
    of as a refused tool call in production.
    """
    exempt = {
        # Replays a recorded run. Repairing a name here would make the replay
        # disagree with the run it is reproducing, which is the one thing it
        # exists not to do.
        "raven/trajectory/replay.py",
    }

    def _calls(node: ast.AST, name: str) -> list[ast.Call]:
        """Every call to ``name`` under ``node``, spelled either way.

        Both spellings, because the bare name is the only one used today and the
        case this guard exists for is the exit written differently later: an
        `ast.Attribute` covers `base.ToolCallRequest(...)` after
        `from raven.providers import base`, and an alias resolves to one or the
        other.
        """
        found = []
        for n in ast.walk(node):
            if not isinstance(n, ast.Call):
                continue
            called = getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            if called == name:
                found.append(n)
        return found

    # Anchored to this file, not to the working directory. `Path("raven")` reads
    # as the repo only while pytest happens to run from the root: from anywhere
    # else it enumerates nothing, `offenders` stays empty, and the assert passes
    # having checked nothing -- the one failure mode this guard must not have,
    # since it is what stands between a fifth exit and production.
    root = Path(__file__).resolve().parents[1]
    inspected: list[str] = []
    offenders: list[str] = []
    for path in sorted((root / "raven").rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel in exempt:
            inspected.append(rel)
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            builds = _calls(func, "ToolCallRequest")
            if not builds:
                continue
            inspected.append(f"{rel}:{func.name}")
            # Asked of the whole function rather than of the `name=` expression:
            # the streaming exit normalises into a local and passes that, which
            # an inline-only check reads as a miss.
            if not _calls(func, "normalized_tool_name"):
                offenders.extend(f"{rel}:{n.lineno}" for n in builds)

    # What separates "checked and found nothing" from "checked nothing". The
    # exits are known and counted, so a walk that suddenly sees fewer of them is
    # reporting on a tree it did not read.
    assert len(inspected) >= 5, f"the walk found {len(inspected)} construction sites, expected at least 5: {inspected}"

    assert not offenders, (
        f"these build a ToolCallRequest from an upstream name without normalising it: {offenders}. "
        "Wrap the name in normalized_tool_name, or add the file to `exempt` here with the reason."
    )


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


def test_the_outbound_sanitiser_is_not_usable_on_a_received_name() -> None:
    """Why the two directions share a spec and not a function.

    `sanitary_form` is what a name goes through on the way *out*, where
    substituting an illegal character is safe because the result is the name
    that then gets registered. Used on a name coming *in* it invents one: a
    leading space becomes an underscore, and an interior space silently becomes
    the very name this repo refuses to guess at.
    """
    assert sanitary_form(" read_file") == "_read_file"
    assert sanitary_form("read file") == "read_file"

    assert normalized_tool_name(" read_file") == "read_file"
    assert normalized_tool_name("read file") == "read file"


def test_a_name_that_is_not_a_string_is_handed_back_untouched() -> None:
    """A different fault with a different owner. Raising here would take down
    the parse of a response that may be otherwise fine, and the name still
    misses the registry exactly as it did before."""
    assert normalized_tool_name(None) is None
    assert normalized_tool_name(7) == 7
