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


def test_bedrock_model_with_no_registry_spec_keeps_its_caching():
    """Blocker 4 repro: Bedrock carries no ProviderSpec at all, so
    ``find_by_name("bedrock")`` resolved to nothing and the strict check
    returned False outright -- without ever asking whether the model's own
    family reads the field. Bedrock translates ``cache_control`` to its own
    ``cachePoint`` natively for exactly the ids that mention "claude", so
    falling back to ``find_by_keywords`` here is what the pre-refactor
    ``find_by_model(model) or find_by_keywords(model)`` expression covered.
    """
    model = "bedrock/anthropic.claude-3-5-sonnet-20241022-v2:0"
    assert prompt_cache.accepts_cache_control(model, addressed_to="bedrock") is True


def test_a_custom_endpoint_still_refuses_despite_an_anthropic_shaped_id():
    """The keyword fallback only fires when ``addressed_to`` resolves to
    nothing. ``custom`` resolves to a real spec (``supports_prompt_caching``
    is False), so the fallback must not override that strict answer just
    because the model id happens to say "claude" -- that strict path is what
    stopped a gateway forwarding the field to a vendor that bills it as an
    unrecognized block instead of refusing it.
    """
    assert prompt_cache.accepts_cache_control("claude-3-opus", addressed_to="custom") is False


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


# --- The probe the construction sites hand to the strategies ---


def test_every_provider_answers_the_question_the_strategies_ask_it():
    """The name the callers ask for has to be the name the providers answer to.

    Nothing defined ``supports_prompt_caching`` on a provider, so the question a
    token strategy needs to put to the object that will send the request had no
    public form at all, and ``CacheOptimizer`` could only fall back to the
    id-only lookup.

    Asserted as agreement with the private answer rather than as ``hasattr``: a
    method that exists and answers on its own is the second copy this file has
    been preventing since the first three disagreed.
    """
    from raven.providers.litellm_provider import LiteLLMProvider

    cases = [
        ("openrouter", "openrouter/anthropic/claude-fable-5", True),
        ("openrouter", "openrouter/google/gemini-3.5-flash", False),
        ("custom", "anthropic/claude-fable-5", False),
        ("anthropic", "anthropic/claude-fable-5", True),
    ]
    for provider_name, model, expected in cases:
        client = LiteLLMProvider(api_key="k", default_model=model, provider_name=provider_name)
        assert client.supports_prompt_caching(model) is expected, model
        assert client.supports_prompt_caching(model) is client._supports_cache_control(model), model


def test_every_wrapper_forwards_the_probe_to_whoever_sends_the_request():
    """Three shapes stand between the loop and a wire, and each defaults to the
    base class's False if it does not forward.

    That default is silent and wrong in the same direction every time: caching
    off for the surface the wrapper serves. ``PerModelProvider`` fronts the
    gateway, ``LazyProvider`` the TUI, ``EndpointRotorProvider`` any section with
    more than one endpoint -- so between them they are most of production.
    """
    from raven.providers.endpoint_rotor import EndpointRotorProvider
    from raven.providers.endpoints import ResolvedEndpoint
    from raven.providers.lazy import LazyProvider
    from raven.providers.litellm_provider import LiteLLMProvider
    from raven.providers.per_model_provider import PerModelProvider

    model = "openrouter/anthropic/claude-fable-5"

    def inner():
        return LiteLLMProvider(api_key="k", default_model=model, provider_name="openrouter")

    assert inner().supports_prompt_caching(model) is True, "premise"

    rotor = EndpointRotorProvider(
        [ResolvedEndpoint(label="a", api_key="k", api_base=None, extra_headers=None)],
        lambda _ep: inner(),
        default_model=model,
    )
    assert rotor.supports_prompt_caching(model) is True

    lazy = LazyProvider(inner, model, inner().generation)
    assert lazy.supports_prompt_caching(model) is True, "asked before the first call, so it must build"

    per_model = PerModelProvider([], fallback=inner())
    assert per_model.supports_prompt_caching(model) is True


def test_the_injected_probe_overrules_an_id_that_names_the_wrong_wire():
    """Why the probe is worth resolving at all.

    ``anthropic/claude-fable-5`` pointed at an OpenAI-shaped endpoint is a shape
    the config matcher produces on purpose. The id-only fallback reads it as
    Anthropic's wire and marks the request; the client then strips every
    breakpoint back off. Both answers are safe -- neither doubles a bill -- but
    only the injected one stops the strategy doing the work at all.
    """
    import asyncio

    from raven.providers.litellm_provider import LiteLLMProvider
    from raven.token_wise.cache_optimizer import CacheOptimizer

    model = "anthropic/claude-fable-5"
    messages = [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    client = LiteLLMProvider(api_key="k", default_model=model, provider_name="custom")

    id_only, _, _ = asyncio.run(CacheOptimizer().before_llm_call(list(messages), None, model))
    assert "cache_control" in str(id_only), "premise: the id alone reads as Anthropic's wire"

    injected, _, _ = asyncio.run(
        CacheOptimizer(supports_caching=client.supports_prompt_caching).before_llm_call(list(messages), None, model)
    )
    assert "cache_control" not in str(injected)


def test_no_wrapper_swallows_the_auto_marking_switch():
    """CacheOptimizer's breakpoint budget is only a budget if nothing else marks.

    The agent loop sets ``disable_auto_cache_control`` on the provider the
    running turn's binding resolved, once it knows the strategy is placing the
    breakpoints. Assigned to a wrapper that only holds it, the inner -- the
    object that actually builds the request -- keeps marking a system block of
    its own on top of the strategy's. Measured before this fix: at
    ``maxCacheBreakpoints: 1`` the request carried 2, and the operator's number
    was silently not the number sent.

    ``hasattr`` at the call site cannot catch it: the attribute exists on
    ``LiteLLMProvider`` instances and on none of the three wrappers, so the
    guard reads False and the assignment is skipped without a word.
    """
    from raven.providers.endpoint_rotor import EndpointRotorProvider
    from raven.providers.endpoints import ResolvedEndpoint
    from raven.providers.lazy import LazyProvider
    from raven.providers.litellm_provider import LiteLLMProvider
    from raven.providers.per_model_provider import PerModelProvider

    def inner():
        return LiteLLMProvider(api_key="k", default_model="m", provider_name="openrouter")

    assert inner().disable_auto_cache_control is False, "premise: the inner starts un-suppressed"

    rotor = EndpointRotorProvider(
        [ResolvedEndpoint(label="a", api_key="k", api_base=None, extra_headers=None)],
        lambda _ep: inner(),
        default_model="m",
    )
    rotor.disable_auto_cache_control = True
    assert all(i.disable_auto_cache_control for i in rotor._inners)

    per_model = PerModelProvider([], fallback=inner())
    per_model.disable_auto_cache_control = True
    assert per_model._fallback.disable_auto_cache_control is True

    lazy = LazyProvider(inner, "m", inner().generation)
    lazy.disable_auto_cache_control = True
    assert lazy._built().disable_auto_cache_control is True, "set before the build, applied at it"


# --- The stable prefix of the system message ---


class TestStablePrefixSplit:
    """A second breakpoint, for the part of the system message that outlives a turn.

    The system message is rebuilt every turn and its tail is derived from what
    the user just said, so a breakpoint at its end keys on text that changes and
    re-bills the identity and bootstrap in front of it. The split gives that head
    a key of its own; what the model reads must not change by one byte.
    """

    def test_the_two_blocks_still_read_as_the_original_message(self):
        text = "IDENTITY\n\n---\n\nBOOTSTRAP\n\n---\n\nRECALL for this turn"
        blocks = prompt_cache.split_stable_prefix(text, len("IDENTITY\n\n---\n\nBOOTSTRAP\n\n---\n\n"))

        assert blocks is not None
        assert "".join(b["text"] for b in blocks) == text

    def test_a_boundary_at_either_end_is_refused(self):
        text = "only one segment"

        assert prompt_cache.split_stable_prefix(text, 0) is None
        assert prompt_cache.split_stable_prefix(text, len(text)) is None
        assert prompt_cache.split_stable_prefix(text, len(text) + 5) is None

    def test_content_that_is_already_blocks_is_refused(self):
        assert prompt_cache.split_stable_prefix([{"type": "text", "text": "x"}], 1) is None


class TestProviderPlacesBothSystemBreakpoints:
    """What ``_apply_cache_control`` does with, and without, a declared boundary."""

    @staticmethod
    def _provider():
        from raven.providers.litellm_provider import LiteLLMProvider

        return LiteLLMProvider(
            api_key="k",
            api_base="https://openrouter.ai/api/v1",
            provider_name="openrouter",
            default_model="openrouter/anthropic/claude-fable-5",
        )

    @staticmethod
    def _marks(content):
        return [i for i, b in enumerate(content) if "cache_control" in b]

    def test_a_declared_boundary_gets_a_breakpoint_of_its_own(self):
        head = "IDENTITY\n\n---\n\nBOOTSTRAP\n\n---\n\n"
        text = head + "RECALL for this turn"
        msgs = [{"role": "system", "content": text, prompt_cache.STABLE_PREFIX_KEY: len(head)}]

        out, _ = self._provider()._apply_cache_control(msgs, None)

        content = out[0]["content"]
        assert self._marks(content) == [0, 1], "the stable head and the end of the message"
        assert content[0]["text"] == head
        assert "".join(b["text"] for b in content) == text, "the model must read the same bytes"

    def test_without_a_boundary_nothing_changes(self):
        text = "one undivided system prompt"
        msgs = [{"role": "system", "content": text}]

        out, _ = self._provider()._apply_cache_control(msgs, None)

        content = out[0]["content"]
        assert self._marks(content) == [0]
        assert content[0]["text"] == text

    def test_a_boundary_the_message_cannot_honour_is_ignored(self):
        """A stale or out-of-range offset falls back rather than reshaping.

        The offset is measured by the assembler over the phase-A parts and the
        message only grows after that, so it cannot run past the end today --
        but a caller that builds its own system message may set anything, and
        the wrong answer to that is a request split at a place nobody chose.
        """
        text = "short"
        msgs = [{"role": "system", "content": text, prompt_cache.STABLE_PREFIX_KEY: 500}]

        out, _ = self._provider()._apply_cache_control(msgs, None)

        content = out[0]["content"]
        assert self._marks(content) == [0]
        assert content[0]["text"] == text

    def test_the_boundary_key_never_reaches_the_wire(self):
        """It is Raven's own bookkeeping; the sanitizer's allow-list drops it."""
        from raven.providers.litellm_provider import LiteLLMProvider

        msgs = [{"role": "system", "content": "a" * 40, prompt_cache.STABLE_PREFIX_KEY: 10}]

        sent = LiteLLMProvider._sanitize_messages(msgs)

        assert prompt_cache.STABLE_PREFIX_KEY not in sent[0]


class TestTheLifetimeEveryMarkAsksFor:
    """One lifetime, set once, carried by every module that writes the field.

    Split lifetimes would be worse than a wrong one: two marks on the same
    prefix asking for different lifetimes make the upstream cache it twice,
    which is the doubling this module exists to prevent, arrived at from the
    other direction.
    """

    @pytest.fixture(autouse=True)
    def _restore(self):
        yield
        prompt_cache.set_ttl(None)

    def test_the_default_asks_for_nothing(self):
        assert prompt_cache.cache_control() == {"type": "ephemeral"}

    def test_an_hour_is_carried_on_the_field(self):
        prompt_cache.set_ttl("1h")

        assert prompt_cache.cache_control() == {"type": "ephemeral", "ttl": "1h"}

    def test_a_lifetime_nobody_defines_is_refused_here(self):
        """Not forwarded and left to the upstream: an unrecognised `ttl` is
        accepted, billed, and silently behaves as though none was asked for."""
        with pytest.raises(ValueError, match="cache ttl"):
            prompt_cache.set_ttl("2h")

        assert prompt_cache.cache_control() == {"type": "ephemeral"}

    def test_each_call_hands_back_its_own_dict(self):
        """These land in request payloads that callers copy and mutate."""
        first = prompt_cache.cache_control()
        first["type"] = "tampered"

        assert prompt_cache.cache_control() == {"type": "ephemeral"}

    def test_every_writer_goes_through_it(self):
        """The strategies and the provider, not just whichever was changed."""
        import re

        for path in _production_files():
            body = path.read_text(encoding="utf-8")
            if path.name == "prompt_cache.py" or '"cache_control"' not in body:
                continue
            assert not re.search(r'"cache_control":\s*CACHE_CONTROL\b', body), (
                f"{path} writes the constant directly, so it cannot carry a configured lifetime"
            )


class TestStreamedCacheTokensReachTheAccounting:
    """The streamed shape used to arrive under names nothing downstream read."""

    @staticmethod
    def _provider():
        from raven.providers.litellm_provider import LiteLLMProvider

        return LiteLLMProvider(
            api_key="k",
            api_base="https://openrouter.ai/api/v1",
            provider_name="openrouter",
            default_model="openrouter/anthropic/claude-fable-5",
        )

    @staticmethod
    def _openrouter_streamed_usage(cached: int, written: int):
        from types import SimpleNamespace

        return SimpleNamespace(
            prompt_tokens=8479,
            completion_tokens=4,
            total_tokens=8483,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached, cache_write_tokens=written),
        )

    def test_the_openrouter_streamed_shape_is_understood(self):
        from raven.providers.litellm_provider import _cache_tokens

        read, write = _cache_tokens(self._openrouter_streamed_usage(7383, 1079))

        assert (read, write) == (7383, 1079)

    def test_the_anthropic_shape_still_is(self):
        from types import SimpleNamespace

        from raven.providers.litellm_provider import _cache_tokens

        usage = SimpleNamespace(cache_read_input_tokens=11, cache_creation_input_tokens=22)

        assert _cache_tokens(usage) == (11, 22)

    def test_a_response_with_no_cache_activity_counts_none(self):
        from types import SimpleNamespace

        from raven.providers.litellm_provider import _cache_tokens

        assert _cache_tokens(SimpleNamespace(prompt_tokens=5)) == (0, 0)

    def test_a_warm_streamed_turn_is_no_longer_priced_as_fresh(self):
        """It was billed at 4x: 8,479 fresh instead of 7,383 read + 1,079 written."""
        from raven.agent.loop.main import AgentLoop
        from raven.providers.base import LLMResponse
        from raven.providers.litellm_provider import _cache_tokens

        read, write = _cache_tokens(self._openrouter_streamed_usage(7383, 1079))
        usage = {"prompt_tokens": 8479, "completion_tokens": 4, "total_tokens": 8483}
        usage["cache_read_input_tokens"] = read
        usage["cache_creation_input_tokens"] = write

        snapshot = AgentLoop._build_usage_snapshot(
            LLMResponse(content="ok", usage=usage), "anthropic/claude-haiku-4.5", "k"
        )

        assert snapshot.cache_read_tokens == 7383
        assert snapshot.cache_write_tokens == 1079
        assert snapshot.input_tokens == 8479 - 7383 - 1079

    def test_the_terminal_stream_chunk_carries_the_normalised_names(self):
        """The seam itself: what `_normalize_stream_chunk` hands the loop.

        Covers the integration the two tests above stop short of -- they drive
        the extractor and the snapshot, and the bug lived between them.
        """
        from types import SimpleNamespace

        chunk = SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=None), finish_reason="stop")],
            usage=self._openrouter_streamed_usage(7383, 1079),
        )

        delta = self._provider()._normalize_stream_chunk(chunk)

        assert delta is not None
        assert delta.usage["cache_read_input_tokens"] == 7383
        assert delta.usage["cache_creation_input_tokens"] == 1079

    def test_a_stream_chunk_with_no_cache_activity_adds_no_names(self):
        """Absent, not zero: the accounting above distinguishes them."""
        from types import SimpleNamespace

        chunk = SimpleNamespace(
            choices=[SimpleNamespace(delta=SimpleNamespace(content=None, tool_calls=None), finish_reason="stop")],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=1, total_tokens=6),
        )

        delta = self._provider()._normalize_stream_chunk(chunk)

        assert delta is not None
        assert "cache_read_input_tokens" not in delta.usage
        assert "cache_creation_input_tokens" not in delta.usage
