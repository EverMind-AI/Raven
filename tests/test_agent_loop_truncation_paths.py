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
from raven.providers.base import GenerationSettings, LLMProvider, LLMResponse, RunMeta, ToolCallRequest
from raven.providers.truncation import flag_truncation
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


def test_no_chat_stream_override_hardcodes_a_generation_setting() -> None:
    """A literal default on `chat_stream` silently replaces configuration.

    This is the defect the whole branch starts from: `chat_stream` declared
    `max_tokens: int = 4096`, the loop called it with messages/tools/model
    only, and every streaming request went out at 4096 whatever was configured.

    `chat_stream` is the exposed one because the loop calls it directly. `chat`
    is shielded by `chat_with_retry`, which resolves all three against
    `self.generation` and always passes them explicitly.

    All three parameters, not just the one that started this: fixing
    `max_tokens` alone on three providers left `temperature` and
    `reasoning_effort` shadowed on MiniMax, which is what a reviewer found
    after this test had already been written to check one field.
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
    for cls in _all_subclasses(LLMProvider) | {LLMProvider}:
        # Test doubles are free to hardcode anything -- they never build a real
        # request. Only shipped providers can shadow a user's configuration.
        if not cls.__module__.startswith("raven."):
            continue
        method = cls.__dict__.get("chat_stream")
        if method is None:
            continue
        params = inspect.signature(method).parameters
        for field in ("max_tokens", "temperature", "reasoning_effort"):
            param = params.get(field)
            if param is None or param.default is inspect.Parameter.empty:
                continue
            if param.default is not LLMProvider._SENTINEL:
                offenders.append(f"{cls.__module__}.{cls.__qualname__}.chat_stream({field}={param.default!r})")

    assert not offenders, "chat_stream defaults shadow configuration: " + "; ".join(offenders)


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


# ---------------------------------------------------------------------------
# The tracing span must carry the verdict, which means judging before it closes
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_llm_call_span_records_a_truncated_non_streaming_turn() -> None:
    """`llm.truncated` is stamped only when true, so it has to be true by then.

    `trace.instrument` extracts span attributes in a `finally` and closes the
    span before handing the result back, so a caller that decides truncation
    after `chat_with_retry` returns records nothing -- the span read `False`
    and is already written. That is why the decision lives inside the method
    rather than at its call site.
    """
    from raven.providers.base import GenerationSettings, LLMProvider, LLMResponse, ToolCallRequest
    from raven.tracing import semconv

    class _CutOff(LLMProvider):
        def __init__(self) -> None:
            super().__init__()
            self.generation = GenerationSettings(max_tokens=60)

        async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="c1", name="write_file", arguments={"path": "a.py"})],
                finish_reason="length",
                usage={"completion_tokens": 60},
            )

        def get_default_model(self) -> str:
            return "stub"

    provider = _CutOff()
    response = await provider.chat_with_retry(messages=[{"role": "user", "content": "hi"}], model="stub")

    # What the extractor would read at the moment the span closes.
    attrs = semconv.llm_attrs(response, "stub", "stub", "_CutOff")

    assert attrs.get("llm.truncated") is True
    assert attrs.get("llm.max_tokens") == 60


@pytest.mark.asyncio
async def test_the_ceiling_comes_from_the_model_that_actually_answered() -> None:
    """A fallback hop changes which ceiling the usage should be measured against.

    `chat_with_retry` walks `[model, *fallback_models]` and `LLMResponse` does
    not record which one answered, so a caller judging afterwards can only use
    the model it asked for. When a fallback serves the turn, that is the wrong
    ceiling: too small and a complete call reads as truncated, too large and a
    real cut goes unnoticed.
    """
    from raven.providers.base import GenerationSettings, LLMProvider, LLMResponse

    class _FirstModelFails(LLMProvider):
        def __init__(self) -> None:
            super().__init__()
            self.generation = GenerationSettings()  # no pin: ceiling comes from the catalogue
            self.served: list[str] = []

        async def chat(self, messages, tools=None, model=None, **kwargs) -> LLMResponse:
            self.served.append(model or "")
            if model == "openai/gpt-4o":
                return LLMResponse(content="", finish_reason="error", error_classification=None)
            # 20000 tokens: over gpt-4o's 16384 ceiling, under claude's 64000
            return LLMResponse(content="ok", finish_reason="stop", usage={"completion_tokens": 20000})

        def classify_error(self, exc=None, content=None):
            from raven.providers.base import ErrorClassification

            return ErrorClassification(category="server", retryable=False, should_fallback=True)

        def get_default_model(self) -> str:
            return "openai/gpt-4o"

    provider = _FirstModelFails()
    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/gpt-4o",
        fallback_models=["anthropic/claude-opus-4-5"],
    )

    assert provider.served == ["openai/gpt-4o", "anthropic/claude-opus-4-5"]
    assert response.max_tokens == 64000, "the ceiling must be the fallback's, not the requested model's"
    assert response.truncated is False, "20000 tokens is a normal reply for the model that answered"


def test_a_truncated_reply_with_no_tool_calls_is_still_logged() -> None:
    """A plain answer cut at the ceiling has nothing to refuse, but is still
    the event an operator goes looking for in the log.

    The warning had been nested under "there is a tool call to mark", which
    made exactly this shape invisible.
    """
    from loguru import logger

    seen: list[str] = []
    sink = logger.add(lambda m: seen.append(str(m)), level="WARNING")
    try:
        _, truncated = flag_truncation(
            _gen(60), model="anthropic/claude-opus-4-5", finish_reason="length", usage=None, tool_calls=[]
        )
    finally:
        logger.remove(sink)

    assert truncated is True
    assert any("truncated" in line for line in seen)


# ---------------------------------------------------------------------------
# The ceiling judged against is the one the request carried
# ---------------------------------------------------------------------------


class _RecordsTheCeilingItWasSent(LLMProvider):
    """Answers with usage sitting exactly on whatever ceiling it received."""

    def __init__(self) -> None:
        super().__init__()
        self.generation = GenerationSettings()
        self.sent: list[int | None] = []

    async def chat(self, messages, tools=None, model=None, max_tokens=None, **kwargs) -> LLMResponse:
        self.sent.append(max_tokens)
        return LLMResponse(
            content="",
            tool_calls=[ToolCallRequest(id="c1", name="write_file", arguments={"path": "a.py"})],
            # A backend claiming a clean stop is the case signal 2 exists for.
            finish_reason="tool_calls",
            usage={"completion_tokens": max_tokens},
        )

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_an_explicit_ceiling_is_what_usage_is_measured_against() -> None:
    """Nine call sites pin `max_tokens` on `chat_with_retry` (judge pins 64,
    the curator 2048). The request carries that pin, so the ceiling check has
    to use it too -- recomputing from `generation` yields the catalogue value,
    which usage never reaches, and signal 2 is dead for every one of them.

    That is the drift `send_max_tokens` was written to prevent: a check that
    stops firing without ever failing.
    """
    provider = _RecordsTheCeilingItWasSent()

    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        model="anthropic/claude-opus-4-5",
        max_tokens=64,
    )

    assert provider.sent == [64], "the pin is what goes out"
    assert response.max_tokens == 64, "and what the model is told it stopped at"
    assert response.truncated is True
    assert response.tool_calls[0].run_meta.truncation.at_tokens == 64


@pytest.mark.asyncio
async def test_a_pin_above_the_model_ceiling_is_clamped_before_it_is_sent() -> None:
    """`send_max_tokens` promises "the output ceiling a request will actually
    carry", and bounds a pin by what the model accepts so an over-large one is
    not a rejected request nobody can explain from the call site.

    Call-site pins used to go around that function entirely, which left the
    promise true only of the settings object.
    """
    provider = _RecordsTheCeilingItWasSent()

    response = await provider.chat_with_retry(
        messages=[{"role": "user", "content": "hi"}],
        model="openai/gpt-4o",
        max_tokens=999_999,
    )

    assert provider.sent == [16384], "gpt-4o accepts no more than this"
    assert response.max_tokens == 16384


# ---------------------------------------------------------------------------
# Every agent loop refuses a truncated call, not just the main one
# ---------------------------------------------------------------------------


class _NoSandbox:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _CutWriteThenDone:
    """A turn cut off inside `write_file`, then a turn that finishes.

    `run_meta` is set here rather than derived, so the case pins what the
    dispatch site does with the verdict, not how the verdict is reached.
    """

    def __init__(self) -> None:
        from raven.providers.base import TruncationInfo

        self.responses = [
            LLMResponse(
                content="",
                tool_calls=[
                    ToolCallRequest(
                        id="c1",
                        name="write_file",
                        arguments={"path": "snake.py", "content": "the last chunk\n"},
                        run_meta=RunMeta(truncation=TruncationInfo(at_tokens=60)),
                    )
                ],
                finish_reason="length",
            ),
            LLMResponse(content="done", finish_reason="stop"),
        ]
        self.seen: list[list[dict[str, Any]]] = []

    async def chat_with_retry(self, **kwargs) -> LLMResponse:
        self.seen.append([dict(m) for m in kwargs["messages"]])
        return self.responses.pop(0)

    def get_default_model(self) -> str:
        return "stub"


@pytest.mark.asyncio
async def test_a_subagent_does_not_dispatch_a_truncated_call(tmp_path, monkeypatch) -> None:
    """The verdict reaches `run_meta` here -- `chat_with_retry` sets it -- and
    was then dropped at dispatch, because only the main loop forwarded it.

    `mode` is optional on `write_file`: a cut landing before it arrives falls
    back to overwrite and silently replaces what an earlier append wrote. This
    is the scenario the branch prevents, at a caller it did not reach.
    """
    from raven.agent.subagent.manager import SubagentManager

    target = tmp_path / "snake.py"
    target.write_text("first chunk\n", encoding="utf-8")

    provider = _CutWriteThenDone()
    manager = SubagentManager(provider=provider, workspace=tmp_path, model="stub")
    monkeypatch.setattr(manager, "_build_subagent_prompt", lambda: "system")

    async def _swallow(*a: object, **k: object) -> None:
        return None

    monkeypatch.setattr(manager, "_announce_result", _swallow)

    await manager._run_subagent_inner(
        "t1",
        "write a snake game",
        "snake",
        {"channel": "tui", "chat_id": "default", "session_key": "tui:s1"},
        _NoSandbox(),
        provider,
        "stub",
    )

    assert target.read_text(encoding="utf-8") == "first chunk\n", "a cut-off write must not land"
    tool_replies = [m for m in provider.seen[1] if m.get("role") == "tool"]
    assert tool_replies, "the second turn should have seen the tool result"
    assert "[truncated]" in str(tool_replies[-1].get("content", ""))
