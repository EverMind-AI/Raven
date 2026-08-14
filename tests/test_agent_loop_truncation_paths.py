"""Truncation is decided the same way on both response paths.

The agent loop reaches the model two ways: streaming when a caller wired token
callbacks (the TUI), non-streaming when nobody did (``raven agent`` on the
CLI, sub-agents, the sentinel). The detection was originally written inside
the streaming helper, which left every non-streaming caller reporting a
cut-off tool call as a missing required field -- reproduced live as a
twelve-iteration retry loop, the same shape as the incident this work started
from.

These tests pin the decision itself, not the plumbing, so a future path that
forgets to call it is the only way to regress.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from raven.agent.loop import AgentLoop
from raven.agent.loop.truncation import flag_truncation
from raven.providers.base import GenerationSettings, LLMProvider, LLMResponse, RunMeta, ToolCallRequest
from raven.spine.message import ChatType, Source
from raven.spine.turn import Origin, TurnRequest


def _call(name: str = "write_file", args: dict | None = None) -> ToolCallRequest:
    return ToolCallRequest(id="c1", name=name, arguments=args if args is not None else {"path": "snake.py"})


def _gen(max_tokens: int | None = 60) -> SimpleNamespace:
    return SimpleNamespace(max_tokens=max_tokens)


def test_upstream_length_alone_flags_truncation() -> None:
    """Signal 1 on its own. This is what the non-streaming path relies on.

    It cannot see whether the arguments JSON needed repairing -- the provider
    repairs it before the loop is handed the result -- so if this signal did
    not stand alone, the CLI would have no detection at all.
    """
    calls = [_call()]

    sent, truncated = flag_truncation(
        _gen(60), model="anthropic/claude-opus-4-5", finish_reason="length", usage=None, tool_calls=calls
    )

    assert truncated is True
    assert sent == 60
    assert calls[0].run_meta is not None
    assert calls[0].run_meta.truncation.at_tokens == 60


def test_usage_reaching_the_ceiling_flags_truncation_without_finish_reason() -> None:
    """Signal 2 on its own -- for backends that report a clean stop anyway."""
    calls = [_call()]

    _, truncated = flag_truncation(
        _gen(60),
        model="anthropic/claude-opus-4-5",
        finish_reason="stop",
        usage={"completion_tokens": 60},
        tool_calls=calls,
    )

    assert truncated is True
    assert calls[0].run_meta is not None


def test_a_repair_alone_is_malformed_json_not_a_ceiling_hit() -> None:
    """No length stop, no ceiling hit -- just JSON the model got wrong.

    The call is still refused (its arguments cannot be trusted), but nothing
    about the turn says it was cut, so it must not be recorded as truncated
    nor told to resend in smaller pieces.
    """
    calls = [_call()]
    calls[0].run_meta = RunMeta(arguments_repaired=True)

    _, truncated = flag_truncation(
        _gen(60),
        model="anthropic/claude-opus-4-5",
        finish_reason="tool_calls",
        usage={"completion_tokens": 12},
        tool_calls=calls,
    )

    assert truncated is False
    assert calls[0].run_meta.truncation is None
    assert calls[0].run_meta.arguments_repaired is True


def test_a_repair_under_a_ceiling_hit_is_a_cut() -> None:
    """Same repair, but usage says the turn hit the ceiling."""
    calls = [_call()]
    calls[0].run_meta = RunMeta(arguments_repaired=True)

    _, truncated = flag_truncation(
        _gen(60),
        model="anthropic/claude-opus-4-5",
        finish_reason="tool_calls",
        usage={"completion_tokens": 60},
        tool_calls=calls,
    )

    assert truncated is True
    assert calls[0].run_meta.truncation is not None


def test_an_earlier_malformed_call_is_bad_json_even_when_the_turn_was_cut() -> None:
    """Three calls, the first malformed, and the turn did hit the ceiling.

    Both get refused, for different reasons, and the position is what tells
    them apart: a cut leaves no later calls to arrive, so a repair on anything
    but the last call was the model writing bad JSON. Reading it as a cut
    would tell the model to resend in pieces something that was never too long.

    This is also the shape that made a per-response boolean wrong -- it would
    have refused the third call and dispatched the first.
    """
    calls = [_call("write_file"), _call("read_file"), _call("list_dir")]
    calls[0].run_meta = RunMeta(arguments_repaired=True)

    flag_truncation(
        _gen(60),
        model="anthropic/claude-opus-4-5",
        finish_reason="length",
        usage=None,
        tool_calls=calls,
    )

    assert calls[0].run_meta.truncation is None, "an earlier repair is bad JSON, not a cut"
    assert calls[0].run_meta.arguments_repaired is True, "but it is still refused"
    assert calls[2].run_meta.truncation is not None, "the last call is the one that was cut"
    assert calls[1].run_meta is None, "the untouched middle call stays dispatchable"


def test_a_complete_turn_is_left_alone() -> None:
    """No signal present: nothing is flagged and no call is marked."""
    calls = [_call()]

    _, truncated = flag_truncation(
        _gen(60),
        model="anthropic/claude-opus-4-5",
        finish_reason="stop",
        usage={"completion_tokens": 12},
        tool_calls=calls,
    )

    assert truncated is False
    assert calls[0].run_meta is None


def test_only_the_last_call_of_a_truncated_turn_is_marked() -> None:
    """Calls arrive in order, so the earlier ones finished before the ceiling."""
    calls = [_call("read_file", {"path": "a.py"}), _call("write_file", {"path": "snake.py"})]

    flag_truncation(_gen(60), model="anthropic/claude-opus-4-5", finish_reason="length", usage=None, tool_calls=calls)

    assert calls[0].run_meta is None
    assert calls[1].run_meta is not None


def test_a_truncated_turn_with_no_tool_calls_marks_nothing() -> None:
    """A cut-off prose answer is still truncated; there is just nothing to mark."""
    _, truncated = flag_truncation(
        _gen(60), model="anthropic/claude-opus-4-5", finish_reason="length", usage=None, tool_calls=[]
    )

    assert truncated is True


# ---------------------------------------------------------------------------
# End to end on the non-streaming path -- the one the CLI takes
# ---------------------------------------------------------------------------


@pytest.fixture
def workspace():
    with tempfile.TemporaryDirectory() as td:
        yield Path(td)


class _TruncatedThenDone(LLMProvider):
    """First turn is cut off mid-arguments; the second one answers.

    Shaped after the live reproduction: `write_file` arrives with `path` but
    no `content`, and the backend says it stopped at the ceiling.
    """

    def __init__(self) -> None:
        super().__init__()
        self.generation = GenerationSettings(max_tokens=60)
        self.calls = 0
        self.seen: list[list[dict[str, Any]]] = []

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="write_file",
                        arguments={"path": "snake.py"},
                        run_meta=RunMeta(arguments_repaired=True),
                    )
                ],
                finish_reason="length",
            )
        return LLMResponse(content="done", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_cli_path_reports_truncation_not_a_missing_field(workspace) -> None:
    """No token callback wired, so the turn goes through chat_with_retry.

    Reproduced live before this was covered: twelve iterations, each telling
    the model it forgot `content` when the field had simply been cut off, and
    each answered by re-sending the same call.
    """
    provider = _TruncatedThenDone()
    agent = AgentLoop(
        provider=provider,
        workspace=workspace,
        model="stub",
        max_iterations=4,
        restrict_to_workspace=True,
    )

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="write a snake game",
        ),
        session_key="s1",
    )

    tool_replies = [m for m in provider.seen[1] if m.get("role") == "tool"]
    assert tool_replies, "the second turn should have seen the tool result"
    text = str(tool_replies[-1].get("content", ""))
    assert "[truncated]" in text
    assert "missing required" not in text
    assert "different approach" not in text


def test_generation_settings_ships_with_no_opinion_on_max_tokens() -> None:
    """The shipped default must stay `None`, meaning "resolve it per model".

    Written after a debugging value (60) was committed by a `git add -A` that
    swept up an unrelated working-tree edit. Nothing else in the suite would
    have failed: every provider stub sets its own generation, so a wrong
    default here silently caps real traffic and nothing turns red.
    """
    from raven.providers.base import GenerationSettings, LLMResponse

    assert GenerationSettings().max_tokens is None
    assert LLMResponse(content="x").max_tokens is None


# ---------------------------------------------------------------------------
# No provider may reintroduce a hardcoded ceiling in its signature
# ---------------------------------------------------------------------------


def test_no_provider_signature_hardcodes_a_max_tokens_default() -> None:
    """A literal default here silently shadows configuration.

    This is the original defect of this whole branch: `chat_stream` declared
    `max_tokens: int = 4096`, the loop called it without that argument, and
    every streaming request went out at 4096 no matter what was configured.
    It was fixed on two providers and missed on three others, which then took
    a `None` from `chat_with_retry` -- one of them into `max(1, None)`.

    Asked of every subclass rather than of a list, so a provider added later
    cannot reintroduce it quietly.
    """
    import inspect
    import pkgutil
    from importlib import import_module

    import raven.providers as providers_pkg
    from raven.providers.base import LLMProvider

    for mod in pkgutil.iter_modules(providers_pkg.__path__):
        try:
            import_module(f"raven.providers.{mod.name}")
        except Exception:
            continue  # optional backends whose deps are absent

    offenders: list[str] = []
    for cls in _all_subclasses(LLMProvider):
        # Test doubles are free to hardcode anything -- they never build a real
        # request. Only shipped providers can shadow a user's configuration.
        if not cls.__module__.startswith("raven."):
            continue
        for method_name in ("chat", "chat_stream", "chat_with_retry"):
            method = cls.__dict__.get(method_name)
            if method is None:
                continue
            param = inspect.signature(method).parameters.get("max_tokens")
            if param is None or param.default is inspect.Parameter.empty:
                continue
            if isinstance(param.default, int) and not isinstance(param.default, bool):
                offenders.append(f"{cls.__module__}.{cls.__qualname__}.{method_name} = {param.default}")

    assert not offenders, "hardcoded max_tokens defaults shadow configuration: " + "; ".join(offenders)


def _all_subclasses(cls: type) -> set[type]:
    direct = set(cls.__subclasses__())
    return direct.union(*(_all_subclasses(c) for c in direct)) if direct else direct


# ---------------------------------------------------------------------------
# Where the cut landed, when the caller can tell
# ---------------------------------------------------------------------------


def test_a_cut_inside_tool_arguments_marks_the_call() -> None:
    calls = [_call()]

    flag_truncation(
        _gen(60),
        model="anthropic/claude-opus-4-5",
        finish_reason="length",
        usage=None,
        tool_calls=calls,
    )

    assert calls[0].run_meta is not None


def test_repaired_arguments_are_reported_as_a_parse_failure() -> None:
    """The provider must not swallow the fact that repair was needed.

    Measured against openrouter: a cut mid-arguments arrives as an unclosed
    blob from both Anthropic- and OpenAI-backed models, and `json_repair`
    closes it silently. Discarding that left the non-streaming path with one
    working signal on gpt-4o, which answers a ceiling hit with
    `finish_reason="tool_calls"` rather than `"length"` (4 of 4 probes).
    """
    from types import SimpleNamespace as N

    from raven.providers.litellm_provider import LiteLLMProvider

    def response(arguments: str):
        fn = N(name="write_file", arguments=arguments, provider_specific_fields=None)
        tc = N(id="c1", function=fn, provider_specific_fields=None)
        msg = N(content=None, tool_calls=[tc], reasoning_content=None, thinking_blocks=None)
        return N(choices=[N(message=msg, finish_reason="tool_calls")], usage=None)

    provider = LiteLLMProvider.__new__(LiteLLMProvider)

    whole = LiteLLMProvider._parse_response(provider, response('{"path": "a.py"}'))
    assert whole.tool_calls[0].run_meta is None

    cut = LiteLLMProvider._parse_response(provider, response('{"path": "a.py", "content": "import ran'))
    assert cut.tool_calls[0].run_meta.arguments_repaired is True
    # Still repaired -- the signal is additional, not a replacement.
    assert cut.tool_calls[0].arguments["content"] == "import ran"


class _LyingFinishReason(LLMProvider):
    """A backend that reports a clean tool_calls stop on a truncated turn.

    Shaped after gpt-4o through openrouter: `finish_reason` says `tool_calls`,
    usage sits below the ceiling, and only the unparseable arguments give it
    away.
    """

    def __init__(self) -> None:
        super().__init__()
        self.generation = GenerationSettings(max_tokens=100_000)
        self.calls = 0
        self.seen: list[list[dict[str, Any]]] = []

    async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
        self.calls += 1
        self.seen.append([dict(m) for m in messages])
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="write_file",
                        arguments={"path": "snake.py"},
                        run_meta=RunMeta(arguments_repaired=True),
                    )
                ],
                finish_reason="tool_calls",
                usage={"completion_tokens": 12},
            )
        return LLMResponse(content="done", finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_a_malformed_call_is_refused_on_the_non_streaming_path(workspace) -> None:
    """No usable finish_reason, no ceiling hit -- only the repaired arguments.

    Without the observation reaching the loop, the turn reads as a plain schema
    error and the model is told it forgot a field it did send.
    """
    provider = _LyingFinishReason()
    agent = AgentLoop(
        provider=provider, workspace=workspace, model="stub", max_iterations=4, restrict_to_workspace=True
    )

    await agent._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="write a snake game",
        ),
        session_key="s1",
    )

    tool_replies = [m for m in provider.seen[1] if m.get("role") == "tool"]
    assert tool_replies
    text = str(tool_replies[-1].get("content", ""))
    assert "invalid arguments" in text
    assert "missing required" not in text
