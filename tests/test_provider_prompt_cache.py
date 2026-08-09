"""Tests for raven.providers.prompt_cache -- who may carry cache_control.

The decision used to exist three times: once in the provider that builds the
request and once in each token strategy that places breakpoints. They disagreed
in the way copies do -- the provider's only ever marked the system message and
the tool list, so it could not have answered for the marks the strategies stamp
onto the last conversation message, which is where a doubled Gemini bill came
from. These assert the one answer, and that all three ask it.
"""

from __future__ import annotations

import pytest

from raven.providers import prompt_cache


@pytest.fixture(autouse=True)
def _forget_suppressions():
    prompt_cache.reset_suppressions()
    yield
    prompt_cache.reset_suppressions()


# --- The predicate: (wire x model family) ---


@pytest.mark.parametrize(
    ("model", "expected", "why"),
    [
        ("anthropic/claude-fable-5", True, "direct to the vendor whose API defines the field"),
        ("openrouter/anthropic/claude-fable-5", True, "a wire that carries it, a vendor that reads it"),
        ("openrouter/google/gemini-3.5-flash", False, "carried, forwarded, and billed twice"),
        ("openrouter/qwen/qwen3.7-max", False, "carried, and it cost the model its own auto-caching"),
        ("openrouter/deepseek/deepseek-chat", False, "DeepSeek caches automatically and takes no breakpoints"),
        ("deepseek/deepseek-chat", False, "an OpenAI-shaped wire has nowhere to put it"),
        ("siliconflow/anthropic/claude-fable-5", False, "the gateway's wire decides, and this one cannot carry it"),
        ("", False, "no id, no answer"),
    ],
)
def test_the_answer_is_the_wire_and_the_family_together(model, expected, why):
    assert prompt_cache.accepts_cache_control(model) is expected, why


def test_the_measured_regression_is_the_one_that_changed():
    """The three answers this rule was measured against on a real machine.

    Fable through the gateway cached only with the field, so it must stay True.
    A marked prompt billed Gemini for nearly double its tokens, and cost Qwen
    its own automatic caching, so both must become False. A change that flips
    any of these three is a change to a real bill.
    """
    assert prompt_cache.accepts_cache_control("openrouter/anthropic/claude-fable-5") is True
    assert prompt_cache.accepts_cache_control("openrouter/google/gemini-3.5-flash") is False
    assert prompt_cache.accepts_cache_control("openrouter/qwen/qwen3.7-max") is False


def test_every_place_that_marks_a_request_asks_the_same_question():
    """The property the three copies did not have.

    Asserted as agreement rather than as "they all import it", because importing
    one answer and then adjusting it locally is exactly how the copies drifted.
    """
    from raven.providers.litellm_provider import LiteLLMProvider
    from raven.token_wise import cache_optimizer, system_and_tail_cache

    # Stored ids, which is what production carries: each names the provider
    # serving it, so the strategies -- which only ever see the id -- have the
    # same information the provider has.
    stored = {
        "openrouter": ("openrouter/anthropic/claude-fable-5", "openrouter/google/gemini-3.5-flash"),
        "anthropic": ("anthropic/claude-fable-5",),
        "deepseek": ("deepseek/deepseek-chat",),
        "siliconflow": ("siliconflow/anthropic/claude-fable-5",),
    }
    for provider_name, models in stored.items():
        provider = LiteLLMProvider(api_key="", default_model="x", provider_name=provider_name)
        for model in models:
            expected = prompt_cache.accepts_cache_control(model)
            assert provider._supports_cache_control(model) is expected, model
            assert cache_optimizer._supports_cache_control(model) is expected, model
            assert system_and_tail_cache._supports_cache_control(model) is expected, model


# --- Learned suppression ---


def test_a_model_an_upstream_refused_is_not_marked_again():
    model = "openrouter/anthropic/claude-3-haiku"
    assert prompt_cache.accepts_cache_control(model) is True

    prompt_cache.suppress(model)

    assert prompt_cache.is_suppressed(model)
    assert prompt_cache.accepts_cache_control(model) is False


def test_suppressing_one_model_does_not_touch_its_neighbours():
    prompt_cache.suppress("openrouter/anthropic/claude-3-haiku")
    assert prompt_cache.accepts_cache_control("openrouter/anthropic/claude-fable-5") is True


#: Two refusal shapes captured verbatim from `openrouter/anthropic/claude-3-haiku`.
#: Both arrive for the same model, because the gateway picks a different
#: upstream per request -- and the second, which never names the field, is the
#: one a matcher built on the field name misses, reading as an intermittent
#: failure.
_REFUSAL_NAMING_THE_FIELD = (
    "Error calling LLM: litellm.BadRequestError: OpenrouterException - "
    "messages.0.content.0.text.cache_control: Extra inputs are not permitted"
)
_REFUSAL_FROM_THE_UPSTREAM = (
    "Error calling LLM: litellm.BadRequestError: OpenrouterException - "
    '{"error":{"message":"Provider returned error","code":400,"metadata":{"raw":'
    '"{\\"message\\":\\"You invoked an unsupported model or your request did not allow '
    'prompt caching. See the documentation for more information.\\"}",'
    '"provider_name":"Amazon Bedrock","is_byok":false}}}'
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (_REFUSAL_NAMING_THE_FIELD, True),
        (_REFUSAL_FROM_THE_UPSTREAM, True),
        ("Error calling LLM: invalid_request_error: cache_control is not supported", True),
        ("BadRequestError: 400 context_length_exceeded", False),
        # The status has to be its own token. As a bare substring it also matched
        # the "400" inside "1400ms", so a rate limit or a timeout whose text
        # happened to name the field read as a refusal -- and that costs the
        # model its caching for the rest of the process, quietly.
        # A gateway paraphrasing its upstream can drop the numeric code entirely,
        # and the spelling that arrives carries a space -- which the run-together
        # forms do not match, leaving the status as the only detector.
        ("Bad Request: your request did not allow prompt caching", True),
        ("BAD REQUEST -- cache_control: Extra inputs are not permitted", True),
        ("429 rate limited, retry after 1400ms; cache_control was fine", False),
        ("Timeout after 24000ms while streaming a prompt caching request", False),
        ("Timeout while sending a cache_control payload", False),
        ("500 Provider returned error: prompt caching is temporarily unavailable", False),
        ("429 rate limited", False),
        ("", False),
    ],
)
def test_only_a_refusal_naming_the_field_counts(message, expected):
    """Both halves required.

    The name alone would read a timeout whose payload was logged as a dialect
    problem and switch caching off for the process; the status alone would
    swallow every other malformed request into a silent retry.
    """
    assert prompt_cache.is_rejection(message) is expected


# --- Stripping marks a strategy already placed ---


def test_stripping_removes_every_breakpoint_a_strategy_placed():
    """Needed as well as suppression, not instead of it: the strategies mark the
    payload upstream of the provider, so the marks are already in the messages a
    retry would resend."""
    messages = [
        {"role": "system", "content": [{"type": "text", "text": "sys", "cache_control": {"type": "ephemeral"}}]},
        {"role": "user", "content": "hi", "cache_control": {"type": "ephemeral"}},
        {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
    ]
    tools = [{"name": "a"}, {"name": "b", "cache_control": {"type": "ephemeral"}}]

    stripped_messages, stripped_tools = prompt_cache.strip(messages, tools)

    assert "cache_control" not in str(stripped_messages) + str(stripped_tools)
    # Content survives, and a one-element text block collapses back to the string
    # it was wrapped from -- the wrap exists only to hold the breakpoint.
    assert stripped_messages[0]["content"] == "sys"
    assert stripped_messages[1]["content"] == "hi"
    assert stripped_messages[2]["content"] == "ok"
    assert [t["name"] for t in stripped_tools] == ["a", "b"]


def test_stripping_leaves_the_caller_s_list_alone():
    """The retry re-sends what it was given; mutating it would change what a
    caller holding the same list believes it sent."""
    messages = [{"role": "user", "content": "hi", "cache_control": {"type": "ephemeral"}}]

    prompt_cache.strip(messages, None)

    assert messages[0]["cache_control"] == {"type": "ephemeral"}


def test_stripping_tolerates_no_tools_and_odd_blocks():
    messages = [{"role": "user", "content": None}, {"role": "tool", "content": ["not-a-dict"]}]

    stripped, tools = prompt_cache.strip(messages, None)

    assert tools is None
    assert stripped[0]["content"] is None
    assert stripped[1]["content"] == ["not-a-dict"]


# --- The retry that learns it (raven.providers.base) ---


class _RecordingProvider:
    """A provider whose first call refuses the field and whose second succeeds.

    Built from ``LLMProvider`` rather than mocked at the transport, because the
    behaviour under test spans two layers: the retry strips what a strategy
    already placed, and the provider must not put its own back on the way out.
    """

    def __init__(self, errors: list[str]):
        from raven.providers.base import LLMProvider, LLMResponse

        self._errors = errors
        self._LLMResponse = LLMResponse
        self.sent: list[tuple[list, list | None]] = []

        outer = self

        class _P(LLMProvider):
            async def chat(self, messages, tools=None, model=None, **kwargs):
                outer.sent.append((messages, tools))
                if outer._errors:
                    return LLMResponse(content=outer._errors.pop(0), finish_reason="error")
                return LLMResponse(content="ok", finish_reason="stop")

            def get_default_model(self) -> str:
                return "openrouter/anthropic/claude-3-haiku"

        self.provider = _P(api_key="test")


def _marked_payload():
    return (
        [{"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}],
        [{"name": "t", "cache_control": {"type": "ephemeral"}}],
    )


@pytest.mark.asyncio
async def test_a_refused_field_is_dropped_and_the_turn_retried_immediately():
    """The failure this exists for: OpenRouter routes an Anthropic model to
    Bedrock, which wants ``cachePoint``, and answers 400 every single turn.

    The retry has to strip as well as suppress -- a token strategy placed these
    marks before the provider was ever called, so suppression alone would resend
    exactly what was refused.
    """
    model = "openrouter/anthropic/claude-3-haiku"
    rig = _RecordingProvider(["litellm.BadRequestError: 400 tools.16.cache_control: Extra inputs are not permitted"])
    messages, tools = _marked_payload()

    response = await rig.provider.chat_with_retry(messages=messages, tools=tools, model=model)

    assert response.finish_reason == "stop"
    assert len(rig.sent) == 2, "expected exactly one extra attempt"
    assert "cache_control" in str(rig.sent[0]), "the first attempt should carry what the strategy placed"
    assert "cache_control" not in str(rig.sent[1]), "the retry resent the field that was just refused"
    assert prompt_cache.is_suppressed(model)


@pytest.mark.asyncio
async def test_an_unrelated_bad_request_is_not_retried_or_learned_from():
    """Nothing is swallowed: an error that does not name the field surfaces as
    itself, and no model is marked as refusing anything."""
    model = "openrouter/anthropic/claude-3-haiku"
    rig = _RecordingProvider(["litellm.BadRequestError: 400 context_length_exceeded"] * 8)
    messages, tools = _marked_payload()

    response = await rig.provider.chat_with_retry(messages=messages, tools=tools, model=model)

    assert response.finish_reason == "error"
    assert "context_length_exceeded" in response.content
    assert not prompt_cache.is_suppressed(model)


@pytest.mark.asyncio
async def test_the_field_is_dropped_once_not_on_every_attempt():
    """A model that refuses the field and then keeps failing must not spend its
    whole retry ladder re-learning the same thing."""
    model = "openrouter/anthropic/claude-3-haiku"
    refusal = "BadRequestError: 400 cache_control not permitted"
    rig = _RecordingProvider([refusal] * 8)
    messages, tools = _marked_payload()

    response = await rig.provider.chat_with_retry(messages=messages, tools=tools, model=model)

    assert response.finish_reason == "error"
    assert prompt_cache.is_suppressed(model)
    # Attempt 1 carried the marks, the rest did not -- and the ladder was not
    # restarted, so the total stays inside the normal budget.
    assert "cache_control" in str(rig.sent[0])
    assert all("cache_control" not in str(sent) for sent in rig.sent[1:])


# --- Nobody marks a request without asking ---


def _production_files():
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "raven"
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def test_the_fields_value_has_one_definition():
    """Three modules each spelled out ``{"type": "ephemeral"}``.

    Anthropic's is the only shape today, so three copies agreed by luck rather
    than by construction -- and a fourth would be written by whoever adds the
    next marker.
    """
    offenders = [
        str(path)
        for path in _production_files()
        if "ephemeral" in path.read_text(encoding="utf-8")
        and path.name != "prompt_cache.py"
        and '"type": "ephemeral"' in path.read_text(encoding="utf-8")
    ]
    assert not offenders, "import CACHE_CONTROL from providers.prompt_cache:\n" + "\n".join(offenders)


def test_every_module_that_writes_the_field_asks_whether_it_may():
    """The shape that would slip past every other test here.

    A fourth marker -- a new strategy, a new provider backend -- can place
    breakpoints correctly, agree with nothing, and be found only by a bill. The
    three that exist are listed because each was read and each asks; a new name
    on this list is a claim that it does too.
    """
    import ast

    writers = {
        "raven/providers/prompt_cache.py",  # the answer itself
        "raven/providers/litellm_provider.py",
        "raven/token_wise/cache_optimizer.py",
        "raven/token_wise/system_and_tail_cache.py",
    }
    root = _production_files()[0].parents[1]
    found = set()
    for path in _production_files():
        source = path.read_text(encoding="utf-8")
        if '"cache_control"' not in source and "'cache_control'" not in source:
            continue
        rel = str(path.relative_to(root))
        found.add(rel)
        if rel == "raven/providers/prompt_cache.py":
            continue
        # The import, not the spelling: a local helper that happens to be called
        # `_supports_cache_control` and answers on its own would satisfy a text
        # scan while being exactly the second copy this is here to prevent.
        imported = any(
            isinstance(node, ast.ImportFrom)
            and node.module == "raven.providers.prompt_cache"
            and any(alias.name == "accepts_cache_control" for alias in node.names)
            for node in ast.walk(ast.parse(source))
        )
        assert imported, f"{rel} marks requests without importing the answer"

    assert found <= writers, f"unreviewed writers of the field: {sorted(found - writers)}"


@pytest.mark.asyncio
async def test_the_client_takes_off_marks_meant_for_a_vendor_it_is_not_calling():
    """A strategy sees an id; only the client knows where the request goes.

    ``anthropic/claude-3`` served through an OpenAI-shaped gateway is a shape the
    config matcher produces on purpose -- an id naming a vendor, routed to
    whoever actually has credentials. The strategy marks it (the id says
    Anthropic), the wire has nowhere to carry the field, and the vendor behind
    the gateway either refuses it or bills the prompt twice without saying so.
    The last word belongs to whoever sends it.
    """
    from raven.providers.litellm_provider import LiteLLMProvider
    from raven.token_wise.cache_optimizer import CacheOptimizer

    model = "anthropic/claude-3"
    messages, tools, _ = await CacheOptimizer().before_llm_call(
        [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}], None, model
    )
    assert "cache_control" in str(messages), "premise: the strategy marks this id"

    client = LiteLLMProvider(api_key="k", default_model=model, provider_name="aihubmix")
    assert client._supports_cache_control(model) is False

    sent, sent_tools = prompt_cache.strip(messages, tools)
    assert "cache_control" not in str(sent) + str(sent_tools)


def test_a_client_that_may_carry_the_field_still_gets_the_strategys_marks():
    """Stripping is for the disagreement, not a blanket removal: where the client
    and the strategy agree, the breakpoints the strategy placed must survive."""
    from raven.providers.litellm_provider import LiteLLMProvider

    client = LiteLLMProvider(api_key="k", default_model="x", provider_name="openrouter")
    assert client._supports_cache_control("openrouter/anthropic/claude-fable-5") is True


@pytest.mark.asyncio
async def test_marking_then_stripping_returns_the_payload_it_started_from():
    """Removing the field is not undoing the marking.

    To have somewhere to put a breakpoint the strategy rewrites string content
    into a one-element text block. Taking the key back off left that rewrite in
    place, so a wire judged unable to carry the field was still sent an
    Anthropic-shaped payload -- and "content must be a string" is among the
    commonest ways an OpenAI-compatible endpoint refuses. That refusal names
    neither the field nor prompt caching, so `is_rejection` would not learn from
    it either: the risk moved from "field rejected" to "shape rejected", with no
    fallback behind it.
    """
    from raven.token_wise.cache_optimizer import CacheOptimizer

    messages = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "TOOL RESULT"},
    ]
    tools = [{"name": "t"}]

    marked, marked_tools, _ = await CacheOptimizer().before_llm_call(
        [dict(m) for m in messages], list(tools), "openrouter/anthropic/claude-fable-5"
    )
    assert "cache_control" in str(marked), "premise: the strategy marks this model"

    assert prompt_cache.strip(marked, marked_tools) == (messages, tools)


def test_stripping_leaves_content_that_was_already_a_list_alone():
    """The collapse undoes one specific rewrite, not every list.

    Multi-block content and blocks carrying anything besides type/text are the
    caller's own shape and must survive untouched.
    """
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]},
        {"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]},
    ]

    stripped, _ = prompt_cache.strip(messages, None)

    assert stripped == messages


def test_a_refusal_whose_str_is_only_a_status_is_still_recognised():
    """The client paraphrases the gateway, which paraphrased the upstream.

    LiteLLM's streaming path raises `MaskedHTTPStatusError`, whose `str()` is a
    URL and a status code and names nothing -- while the body it was built from
    sits on `.text`. Matching only the rendered string made the same refusal
    learnable on the non-streaming path and invisible on the streaming one, which
    is the path the TUI uses.
    """

    class _Masked(Exception):
        def __init__(self):
            super().__init__("Client error '400 Bad Request' for url 'https://openrouter.ai/api/v1/chat/completions'")
            self.text = (
                '{"error":{"message":"messages.0.content.0.text.cache_control: Extra inputs are not permitted"}}'
            )

    masked = _Masked()
    assert "cache_control" not in str(masked), "premise: the rendered string says nothing"
    assert prompt_cache.is_rejection(masked) is True


def test_an_exception_that_says_nothing_anywhere_is_not_a_refusal():
    class _Opaque(Exception):
        text = "Client error '400 Bad Request' for url 'https://example/x'"

    assert prompt_cache.is_rejection(_Opaque("boom")) is False


@pytest.mark.asyncio
async def test_the_non_streaming_retry_also_reads_the_body_off_the_exception():
    """Same asymmetry, other path.

    Whether ``str(exc)`` carries the response body is a property of the handler
    that raised it, not of streaming versus not. The fix landed on the streaming
    path first and the non-streaming one went on matching a rendered string with
    the exception sitting in the same scope.
    """
    from raven.providers import prompt_cache
    from raven.providers.base import LLMProvider

    prompt_cache.reset_suppressions()

    class _Masked(Exception):
        def __init__(self):
            super().__init__("Client error '400 Bad Request' for url 'https://example/x'")
            self.text = (
                '{"error":{"message":"messages.0.content.0.text.cache_control: Extra inputs are not permitted"}}'
            )

    sent: list[bool] = []

    class _P(LLMProvider):
        async def chat(self, messages, tools=None, model=None, **kwargs):
            sent.append("cache_control" in str(messages))
            if len(sent) == 1:
                raise _Masked()
            from raven.providers.base import LLMResponse

            return LLMResponse(content="ok", finish_reason="stop")

        def get_default_model(self) -> str:
            return "openrouter/anthropic/claude-3-haiku"

    provider = _P(api_key="k")
    messages = [{"role": "user", "content": [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]}]

    response = await provider.chat_with_retry(messages=messages, model="openrouter/anthropic/claude-3-haiku")

    assert response.finish_reason == "stop"
    assert sent == [True, False], f"expected one marked attempt then one clean retry, got {sent}"
    assert prompt_cache.is_suppressed("openrouter/anthropic/claude-3-haiku")
    prompt_cache.reset_suppressions()


def test_the_refusal_verdict_survives_a_provider_that_swallows_the_exception():
    """`LiteLLMProvider.chat` turns the exception into a string before the retry
    layer sees it, so asking there asks about a paraphrase.

    Deciding it in `classify_error` -- next to every other verdict, while the
    exception is alive -- is what makes the non-streaming path able to learn from
    a refusal whose `str()` says nothing. Both spellings must reach the same
    answer: the live exception, and the string a provider left behind.
    """
    from raven.providers.base import LLMProvider

    class _Masked(Exception):
        def __init__(self):
            super().__init__("Client error '400 Bad Request' for url 'https://x'")
            self.text = (
                '{"error":{"message":"messages.0.content.0.text.cache_control: Extra inputs are not permitted"}}'
            )

    assert LLMProvider.classify_error(_Masked()).refuses_prompt_cache is True
    swallowed = "Error calling LLM: litellm.BadRequestError: 400 cache_control: Extra inputs are not permitted"
    assert LLMProvider.classify_error(None, swallowed).refuses_prompt_cache is True

    # And nothing else becomes a refusal on the way.
    assert LLMProvider.classify_error(None, "Error calling LLM: 400 context_length_exceeded").refuses_prompt_cache is (
        False
    )
