"""LiteLLM provider implementation for multi-provider support."""

import asyncio
import hashlib
import json
import secrets
import string
import warnings
from collections.abc import AsyncIterator
from typing import Any

import httpx
import json_repair
from loguru import logger

from raven.providers import call_record, prompt_cache
from raven.providers.base import (
    ChatDelta,
    GenerationSettings,
    LLMProvider,
    LLMResponse,
    RunMeta,
    ToolCallRequest,
    format_llm_error,
    send_max_tokens,
)
from raven.providers.first_byte import (
    BOUND_NAME,
    FirstByteTimeoutError,
    StreamIdleTimeoutError,
    httpx_timeout,
    stream_first_byte_budget,
)
from raven.providers.litellm_setup import import_litellm
from raven.providers.litellm_shape import (  # noqa: F401 - re-exported for callers of this module
    _ANTHROPIC_EXTRA_KEYS,
    _OPENROUTER_ATTRIBUTION,
    LiteLLMShape,
    session_affinity_headers,
)
from raven.providers.reasoning import split_orphan_think
from raven.providers.registry import (
    find_by_keywords,
    find_by_model,
)
from raven.providers.tool_names import normalized_tool_name
from raven.providers.transport_failure import (
    flag_transport_failure,
    native_finish_reason,
    prompt_chars,
    said_nothing,
    transport_failure_response,
    without_sentinels,
)
from raven.providers.usage import merge_usage, reported_cost, token_count
from raven.security.tls import is_untrusted_certificate

litellm = import_litellm()
acompletion = litellm.acompletion

# LiteLLM's async logging worker (LoggingWorker) binds its queue to a single
# event loop. Raven runs each turn under a fresh loop (asyncio.run per call), so
# on the next turn the queue is reset and any pending ``Logging.async_*_handler``
# coroutine is dropped without being awaited. Python then prints a
# ``coroutine ... was never awaited`` RuntimeWarning that bleeds into the Ink TUI
# render. The dropped callback is LiteLLM's own success/failure logging, which
# Raven does not rely on. Scope the filter to LiteLLM's ``Logging`` handlers only
# -- a bare ``coroutine '.*'`` pattern would also hide genuine never-awaited bugs
# in Raven's own coroutines.
warnings.filterwarnings(
    "ignore",
    message=r"coroutine 'Logging\.async_.*' was never awaited",
    category=RuntimeWarning,
)

# Standard chat-completion message keys.
_ALLOWED_MSG_KEYS = frozenset({"role", "content", "tool_calls", "tool_call_id", "name", "reasoning_content"})
_ALNUM = string.ascii_letters + string.digits


def _short_tool_id() -> str:
    """Generate a 9-char alphanumeric ID compatible with all providers (incl. Mistral)."""
    return "".join(secrets.choice(_ALNUM) for _ in range(9))


def _finish_was_made_up(stream: Any) -> bool:
    """Whether the stream wrapper never received a finish reason from upstream.

    Read off LiteLLM's ``CustomStreamWrapper``, which keeps two records: the reason
    of a chunk it judged final (``received_finish_reason``) and the last reason any
    chunk carried (``intermittent_finish_reason``). Both are read, because gemini and
    vertex_ai chunks arrive stamped ``_hidden_params["is_finished"] = False`` on every
    chunk, so their terminal "stop" is never recorded as received, only as
    intermittent -- and reading the first alone stamped every healthy gemini reply as
    a cut. A wrapper without the attributes (a test stub, another library) is trusted,
    so only a reason known to be fabricated is reported as such.
    """
    if not hasattr(stream, "received_finish_reason") or getattr(stream, "received_finish_reason") is not None:
        return False
    return getattr(stream, "intermittent_finish_reason", None) is None


def _merge_extra_body(kwargs: dict[str, Any], wire_extra_body: dict[str, Any]) -> None:
    """Merge the provider's built-in extra_body into kwargs instead of overwriting it.

    A model_overrides entry (see _apply_model_overrides) may have already placed
    a user extra_body dict in kwargs -- for example Qwen3's
    extra_body.chat_template_kwargs.enable_thinking. Assigning wire_extra_body
    over it would silently drop those keys. On a key collision, the user's
    value wins: everything wire_extra_body carries is a shipped default
    workaround (see capabilities._WIRE_OVERRIDES -- disabling OpenRouter's
    qwen reasoning mode is the whole table today), and model_overrides is
    documented as the channel that overrides shipped defaults, so a collision
    is the user deliberately reversing one.
    """
    existing = kwargs.get("extra_body")
    if isinstance(existing, dict):
        kwargs["extra_body"] = {**wire_extra_body, **existing}
    else:
        kwargs["extra_body"] = wire_extra_body


def _usage_field(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, dict) else getattr(value, key, None)


def _cache_tokens(usage: Any) -> tuple[int | None, int | None]:
    """Read cache counts without converting a missing report into zero."""
    # LiteLLM synthesizes private cache counters at zero even when none arrived.
    details = _usage_field(usage, "prompt_tokens_details")

    def first(*values: Any) -> int | None:
        return next((count for value in values if (count := token_count(value)) is not None), None)

    return (
        first(
            _usage_field(usage, "cache_read_input_tokens"),
            _usage_field(details, "cached_tokens"),
            _usage_field(usage, "_cache_read_input_tokens") or None,
        ),
        first(
            _usage_field(usage, "cache_creation_input_tokens"),
            _usage_field(details, "cache_write_tokens"),
            _usage_field(usage, "_cache_creation_input_tokens") or None,
        ),
    )


def _refusal_of(carrier: Any) -> str | None:
    """The refusal text a message or delta carries, when the upstream sent one.

    Read with ``getattr`` because the SDK's own types do not declare the field: it
    is there only when the upstream's body had it. litellm, converting a reply's
    body, moves every field its ``Message`` does not declare into
    ``provider_specific_fields``, so an OpenAI-shaped refusal is read there too.
    """
    text = getattr(carrier, "refusal", None)
    if not (isinstance(text, str) and text):
        extra = getattr(carrier, "provider_specific_fields", None)
        text = extra.get("refusal") if isinstance(extra, dict) else None
    return text if isinstance(text, str) and text else None


def _reasoning_tokens(usage: Any) -> int | None:
    """How much of the output the model spent thinking, when the protocol says so.

    Absent is not zero: a turn whose reasoning text never arrived is only
    distinguishable from a turn that did not think by this count.
    """
    details = _usage_field(usage, "completion_tokens_details")
    for key in ("reasoning_tokens", "thinking_tokens"):
        count = token_count(_usage_field(details, key))
        if count is not None:
            return count
    return None


class LiteLLMProvider(LiteLLMShape):
    """
    LLM provider using LiteLLM for multi-provider support.

    Supports OpenRouter, Anthropic, OpenAI, Gemini, MiniMax, and many other providers through
    a unified interface.  Provider-specific logic is driven by the registry
    (see providers/registry.py) — no if-elif chains needed here.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Built lazily on the event loop the first call runs on, and shared by
        # every call this provider makes afterwards (``tls_verify`` off only).
        self._verify_off_client: Any | None = None
        self._verify_off_http_handler: Any | None = None
        # Whether LiteLLM serves this provider's route through its own HTTP
        # handler rather than the OpenAI SDK; learned, see `_learn_route_from`.
        self._on_litellm_http_route = False
        # Drop unsupported parameters for providers (e.g., gpt-5 rejects some params)
        litellm.drop_params = True

    def _apply_model_overrides(self, model: str, kwargs: dict[str, Any]) -> None:
        """Layer per-model parameter overrides: registry defaults, config on top.

        Config supplies one parameter without discarding the rest of the
        registry's entry -- Kimi keeps its mandated temperature even when the
        user only wanted to set top_p.
        """
        model_lower = model.lower()
        # A gateway-routed id names the gateway and its upstream, not the vendor
        # whose quirks these defaults encode -- so match on keywords there.
        spec = find_by_keywords(self._strip_gateway_prefix(model)) if self._gateway else find_by_model(model)
        if spec:
            for pattern, overrides in spec.model_overrides:
                if pattern in model_lower:
                    kwargs.update(overrides)
                    break
        # Longest match wins, so "kimi-k2.5" beats a broad "kimi" regardless of
        # the order the entries happen to be written in.
        matches = [(p, o) for p, o in self.model_overrides.items() if p.lower() in model_lower]
        if matches:
            kwargs.update(max(matches, key=lambda item: len(item[0]))[1])

    @staticmethod
    def _normalize_tool_call_id(tool_call_id: Any) -> Any:
        """Normalize tool_call_id to a provider-safe 9-char alphanumeric form."""
        if not isinstance(tool_call_id, str):
            return tool_call_id
        if len(tool_call_id) == 9 and tool_call_id.isalnum():
            return tool_call_id
        return hashlib.sha1(tool_call_id.encode()).hexdigest()[:9]

    @staticmethod
    def _sanitize_messages(
        messages: list[dict[str, Any]],
        extra_keys: frozenset[str] = frozenset(),
        *,
        require_reasoning_key: bool = False,
    ) -> list[dict[str, Any]]:
        """Strip non-standard keys and ensure assistant messages have a content key.

        With ``require_reasoning_key`` every tool-call message also carries
        ``reasoning_content``, empty where the model did not think.
        """
        allowed = _ALLOWED_MSG_KEYS | extra_keys
        sanitized = LLMProvider._sanitize_request_messages(messages, allowed)
        id_map: dict[str, str] = {}

        def map_id(value: Any) -> Any:
            if not isinstance(value, str):
                return value
            return id_map.setdefault(value, LiteLLMProvider._normalize_tool_call_id(value))

        for clean in sanitized:
            # Keep assistant tool_calls[].id and tool tool_call_id in sync after
            # shortening, otherwise strict providers reject the broken linkage.
            if isinstance(clean.get("tool_calls"), list):
                normalized_tool_calls = []
                for tc in clean["tool_calls"]:
                    if not isinstance(tc, dict):
                        normalized_tool_calls.append(tc)
                        continue
                    tc_clean = dict(tc)
                    tc_clean["id"] = map_id(tc_clean.get("id"))
                    normalized_tool_calls.append(tc_clean)
                clean["tool_calls"] = normalized_tool_calls

            # DeepSeek's thinking mode refuses (400) a request that continues
            # from a tool result unless each tool-call message carries this key.
            # A step taken without thinking has none, and the ids rewritten above
            # keep DeepSeek from recovering it on its own. Empty is accepted.
            if require_reasoning_key and clean.get("tool_calls") and clean.get("reasoning_content") is None:
                clean["reasoning_content"] = ""

            if "tool_call_id" in clean and clean["tool_call_id"]:
                clean["tool_call_id"] = map_id(clean["tool_call_id"])
        return sanitized

    def _mirror_reasoning_for_gateway(self, kwargs: dict[str, Any], reasoning_effort: str) -> None:
        """Keep ``reasoning_effort`` alive past litellm's ``drop_params``.

        ``drop_params`` silently discards ``reasoning_effort`` for any model
        litellm cannot map -- which is every newly released model behind a
        gateway -- and the agent then runs with reasoning off while the config
        says otherwise. OpenRouter accepts the reasoning object natively, so
        the effort is mirrored into ``extra_body``, which ``drop_params`` never
        touches. A reasoning entry already present (a deployment's own config,
        the qwen default) keeps priority.
        """
        if not (self._gateway and self._gateway.name == "openrouter"):
            return
        body = dict(kwargs.get("extra_body") or {})
        body.setdefault("reasoning", {"effort": reasoning_effort})
        kwargs["extra_body"] = body

    def _verify_off_client_for_route(self) -> Any:
        """The client a `tls_verify=False` section hands LiteLLM, of the kind its route adopts.

        LiteLLM sends a request through one of two client families and verifies
        with whichever it builds: the OpenAI SDK, which adopts a prebuilt
        `AsyncOpenAI` (custom gateways, LM Studio, ...), or its own HTTP handler,
        which adopts a prebuilt `AsyncHTTPHandler` (vLLM, Ollama, DeepSeek,
        Mistral, Gemini, ...). Each ignores the other's kind, and LiteLLM offers
        no way to ask which one a model takes, so the SDK client goes first and
        `_learn_route_from` switches. Either one serves every call this provider
        makes, on the loop the first call ran on; per-call timeouts still apply,
        because LiteLLM forwards them per request rather than baking them in.
        """
        if not self._on_litellm_http_route:
            return self._prebuilt_openai_client()
        if self._verify_off_http_handler is None:
            from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler

            self._verify_off_http_handler = AsyncHTTPHandler(ssl_verify=False)
        return self._verify_off_http_handler

    def _learn_route_from(self, exc: BaseException, sent_with: Any) -> bool:
        """Move to LiteLLM's own HTTP client when `exc` says the SDK one was ignored.

        With verification off, a refused certificate can only mean LiteLLM built
        a verifying client of its own instead of adopting the one handed over.
        The handshake fails before the request is written, so sending it again
        through the other client still sends it once. True when that is what
        happened and the caller should send again. Asked of the client the call
        went out with, not of what this provider has learned since: calls that
        left together through the ignored client each need sending again.
        """
        if self.tls_verify or sent_with is not self._verify_off_client or not is_untrusted_certificate(exc):
            return False
        self._on_litellm_http_route = True
        return True

    async def _acompletion(self, kwargs: dict[str, Any]) -> Any:
        """`acompletion`, sent again through the other client when `_learn_route_from` says so."""
        try:
            return await acompletion(**kwargs)
        except Exception as exc:
            if not self._learn_route_from(exc, kwargs.get("client")):
                raise
        kwargs["client"] = self._verify_off_client_for_route()
        return await acompletion(**kwargs)

    def _prebuilt_openai_client(self) -> Any:
        """The `AsyncOpenAI` a `tls_verify=False` section sends calls through on the OpenAI SDK route."""
        if self._verify_off_client is None:
            from openai import AsyncOpenAI

            self._verify_off_client = AsyncOpenAI(
                # A literal, not None: None sends the process's OPENAI_API_KEY
                # (some other section's) as this one's key. A keyless local
                # gateway ignores the header; a keyed one 401s the key either
                # way, so nothing here can misread as success.
                api_key=self.api_key or "no-key",
                base_url=self.api_base,
                http_client=httpx.AsyncClient(verify=False),
                timeout=self.generation.timeout,
            )
        return self._verify_off_client

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        """
        Send a chat completion request via LiteLLM.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions in OpenAI format.
            model: Model identifier (e.g., 'anthropic/claude-sonnet-4-5').
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.

        Returns:
            LLMResponse with content and/or tool calls.
        """
        original_model = model or self.default_model
        model = self._resolve_model(original_model)
        extra_msg_keys = self._extra_msg_keys(original_model, model)
        require_reasoning_key = self._is_deepseek_family(model)

        if self._supports_cache_control(original_model):
            # Asked of this request, not of the process. A strategy that placed
            # marks upstream stamps them; every other caller in this process --
            # the Curator, a subagent, Sentinel, the session titler -- reaches
            # here without a strategy in front of it and still wants its own.
            if not self.disable_auto_cache_control and not prompt_cache.marks_already_placed(messages):
                messages, tools = self._apply_cache_control(messages, tools)
        else:
            messages, tools = prompt_cache.strip(messages, tools)

        # Never volunteered: a caller that wants a short answer pins one, and
        # a vendor that requires the field has a LiteLLM transformation that
        # supplies it. Clamped to at least 1 when present, since LiteLLM
        # rejects a zero or negative value outright.
        if max_tokens is not None:
            max_tokens = max(1, max_tokens)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._sanitize_messages(
                self._sanitize_empty_content(messages),
                extra_keys=extra_msg_keys,
                require_reasoning_key=require_reasoning_key,
            ),
            "temperature": temperature,
            # Per-phase httpx caps forwarded to the underlying client: connect,
            # write and pool get the first-byte budget, the read keeps the whole
            # call's. Only the read has to stay wide -- a non-streaming
            # completion sends no body until the answer is finished, so its
            # first response byte and its last are the same byte and a tight
            # read cap would kill every long generation rather than every stall.
            # What is separable is everything before the model starts: a route
            # that never connects used to cost the whole call budget. This alone
            # still cannot bound a backend that trickles bytes forever (the read
            # timer resets on every chunk), so the awaited call is also wrapped
            # in an asyncio.wait_for wall-clock cap below.
            "timeout": httpx_timeout(self.generation) or self.generation.timeout,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens

        # Apply model-specific overrides (e.g. kimi-k2.5 temperature)
        self._apply_model_overrides(model, kwargs)

        # Pass api_key directly — more reliable than env vars alone
        if self.api_key:
            kwargs["api_key"] = self.api_key

        # Pass api_base for custom endpoints
        if self.api_base:
            kwargs["api_base"] = self.api_base

        # Pass extra headers (e.g. APP-Code for AiHubMix)
        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers

        # Pass provider-specific body extras (e.g. OpenRouter routing pin)
        if self.extra_body:
            _merge_extra_body(kwargs, self.extra_body)

        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
            kwargs["drop_params"] = True
            self._mirror_reasoning_for_gateway(kwargs, reasoning_effort)

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

        try:
            # A section that opted out of certificate verification sends this
            # call through its own client: LiteLLM builds a verifying one of its
            # own, and only a pre-construction hook turns that off. The
            # `responses` and `anthropic` protocols live in their own providers,
            # which read the same knob. Built here rather than beside the other
            # kwargs so that a client that cannot be constructed (httpx refuses
            # an unparsable proxy setting, say) is classified like any other
            # provider failure instead of escaping to the caller as an exception.
            if not self.tls_verify:
                kwargs["client"] = self._verify_off_client_for_route()
            response = await asyncio.wait_for(self._acompletion(kwargs), self.generation.timeout)
            return self._parse_response(response, sent_chars=prompt_chars(messages))
        except Exception as e:
            # Return error as content for graceful handling, but classify the
            # live exception here (status_code + type) before it's lost to a
            # string — the retry/fallback layer reads this verdict.
            classification = self.classify_error(e)
            # Learned here as well as in the retry layer, as ``chat_stream`` does:
            # the retry layer may sit in another process (an agent behind the LLM
            # relay), and its stripped resend is built by this provider.
            if classification.refuses_prompt_cache:
                prompt_cache.suppress(original_model)
            head = self._provider_name or (self._gateway.name if self._gateway else None)
            return LLMResponse(
                content=format_llm_error(e, classification, provider=head),
                finish_reason="error",
                error_classification=classification,
                # The status and the body, before `format_llm_error` renders the
                # exception into a sentence: the category it keeps is enough to
                # pick a recovery and not enough to diagnose one afterwards.
                call_record=call_record.from_exception(e, status=self._extract_status_code(e)),
            )

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = LLMProvider._SENTINEL,
        temperature: object = LLMProvider._SENTINEL,
        reasoning_effort: object = LLMProvider._SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncIterator[ChatDelta]:
        """Streaming counterpart to chat().

        Yields one ChatDelta per non-empty chunk. Signature matches chat()
        so callers can swap providers transparently. The existing chat() is
        NOT modified — non-TUI paths (channels / cron / sentinel / ...)
        continue to use chat() with no behavioral change.

        Provider-specific chunk shapes (e.g. dashscope) are handled inside
        `_normalize_stream_chunk`. The default OpenAI shape extraction lives
        in that hook; subclasses or implementer additions can override.

        Generation defaults resolve from ``self.generation`` the same way
        ``chat_with_retry`` does: literal defaults here would shadow the user's
        configuration, since the agent loop calls this with messages/tools/model
        only.
        """
        gen = getattr(self, "generation", None) or GenerationSettings()
        if max_tokens is self._SENTINEL:
            max_tokens = gen.max_tokens
        if temperature is self._SENTINEL:
            temperature = gen.temperature
        if reasoning_effort is self._SENTINEL:
            reasoning_effort = gen.reasoning_effort
        original_model = model or self.default_model
        model = self._resolve_model(original_model)
        extra_msg_keys = self._extra_msg_keys(original_model, model)
        require_reasoning_key = self._is_deepseek_family(model)

        if self._supports_cache_control(original_model):
            # Asked of this request, not of the process. A strategy that placed
            # marks upstream stamps them; every other caller in this process --
            # the Curator, a subagent, Sentinel, the session titler -- reaches
            # here without a strategy in front of it and still wants its own.
            if not self.disable_auto_cache_control and not prompt_cache.marks_already_placed(messages):
                messages, tools = self._apply_cache_control(messages, tools)
        else:
            messages, tools = prompt_cache.strip(messages, tools)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._sanitize_messages(
                self._sanitize_empty_content(messages),
                extra_keys=extra_msg_keys,
                require_reasoning_key=require_reasoning_key,
            ),
            "temperature": temperature,
            "stream": True,
            # OpenAI-compatible providers only emit the trailing usage chunk
            # when usage is explicitly requested; without it the stream carries
            # no token counts and downstream cost / context tracking sees zero.
            "stream_options": {"include_usage": True},
            "timeout": httpx_timeout(self.generation) or self.generation.timeout,
        }
        # Named even when nobody pinned: a silent request is bounded by the far
        # side, at a number this repo can neither report nor move. See
        # ``send_max_tokens``.
        ceiling = send_max_tokens(self.generation, self.wire_model_id(original_model), pinned=max_tokens)
        kwargs["max_tokens"] = max(1, ceiling)

        self._apply_model_overrides(model, kwargs)

        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_base:
            kwargs["api_base"] = self.api_base
        # Same override as chat(): the streaming handlers adopt a prebuilt
        # client too, so a `tls_verify=False` section streams through it.
        if not self.tls_verify:
            kwargs["client"] = self._verify_off_client_for_route()
        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers
        if self.extra_body:
            _merge_extra_body(kwargs, self.extra_body)
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
            kwargs["drop_params"] = True
            self._mirror_reasoning_for_gateway(kwargs, reasoning_effort)
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

        def _retry_without_breakpoints(exc: Exception) -> bool:
            """Learn the refusal and take the marks off, or say this is not one.

            One of the retries this path takes. Restarting a *partially streamed* call
            is the problem that kept retry out of here, and this is not that: the
            refusal arrives before any chunk has been handed to the caller, so
            nothing has been said that would have to be unsaid. Without it the
            learned downgrade never reaches the surface that actually streams --
            the TUI, where the affected model answered 400 on every single turn.
            """
            if prompt_cache.is_suppressed(original_model) or not prompt_cache.is_rejection(exc):
                return False
            prompt_cache.suppress(original_model)
            kwargs["messages"], stripped = prompt_cache.strip(kwargs["messages"], kwargs.get("tools"))
            if stripped is not None:
                kwargs["tools"] = stripped
            return True

        async def _close(target: Any) -> None:
            aclose = getattr(target, "aclose", None)
            if aclose is not None:
                await aclose()

        # Per-chunk idle cap: the timer resets on every chunk, so a long but
        # steadily-progressing generation is fine while a mid-stream stall (no
        # bytes for `stream_idle_timeout` seconds) raises TimeoutError instead
        # of hanging for the whole-call budget. Getting started has its own,
        # shorter budget below -- a stream that never began is not a stream that
        # stopped mid-answer, and it is recognisable sooner.
        # Everything from the open onward sits inside the one try/finally, so the
        # underlying HTTP stream is closed deterministically on any exit -- a
        # first-chunk timeout included, which is the most likely one there is
        # (gateway queueing, cold start).
        # A chunk of None is a chunk, not the end of the stream. Pulling the
        # first one before the loop needs a value meaning "there was none", and
        # reusing None for it would let a provider that yields one truncate the
        # response silently -- which is not what the loop did before.
        done = object()

        first_byte = stream_first_byte_budget(self.generation)
        stream: Any = None

        async def _open_and_first() -> Any:
            """Open the stream and take its first chunk under one first-byte deadline.

            One deadline over both awaits rather than one each: an OpenAI-shaped
            route issues the request at the open and a gateway that defers it
            issues the request at the first pull, so budgeting them separately
            would let a single stall spend the bound twice. ``stream`` is bound
            the moment the open returns, so the caller's ``finally`` can still
            close the connection when the first chunk is what never came.
            """
            nonlocal stream
            loop = asyncio.get_running_loop()
            started = loop.time()
            phase = "opening the stream"
            try:
                stream = (await asyncio.wait_for(acompletion(**kwargs), first_byte)).__aiter__()
                phase = "waiting for the first chunk"
                return await asyncio.wait_for(stream.__anext__(), max(0.0, started + first_byte - loop.time()))
            except StopAsyncIteration:
                return done
            except TimeoutError as exc:
                waited = loop.time() - started
                logger.warning(
                    "LLM first byte: nothing from {} after {:.1f}s while {} (bound {}={:g}s); "
                    "giving the call up as stalled so the retry ladder can ask again",
                    self._provider_name or (self._gateway.name if self._gateway else "provider"),
                    waited,
                    phase,
                    BOUND_NAME,
                    first_byte,
                )
                raise FirstByteTimeoutError(phase=phase, budget=first_byte, waited=waited) from exc

        try:
            # The open and the first pull are one unit, and the `except` has to
            # cover both: an OpenAI-shaped route raises at the open, and a gateway
            # that defers the request until the first pull raises there instead.
            # Each retry is taken at most once, so the loop ends.
            while True:
                try:
                    first = await _open_and_first()
                    break
                except Exception as exc:
                    if self._learn_route_from(exc, kwargs.get("client")):
                        kwargs["client"] = self._verify_off_client_for_route()
                    elif not _retry_without_breakpoints(exc):
                        raise
                    # The refused stream is finished with; closing it before opening
                    # the replacement keeps at most one live at a time. It is None
                    # when the open itself was what failed, and cleared so a second
                    # failure's `finally` does not close the same object twice.
                    await _close(stream)
                    stream = None

            # The transport facts, read off the wrapper while it still holds
            # them: the accumulator that turns these deltas into a response never
            # sees this object, and once the stream has ended the headers have
            # gone with it -- which is why a measured empty-200 stream left a
            # record with no status, no serving backend and no request id to ask a
            # gateway about. They ride the deltas below rather than one of their
            # own, because an extra element would change a sequence every consumer
            # of this generator reads positionally. A stream that yields no delta
            # at all therefore carries no record; that case is already a transport
            # failure by the time the accumulator is done with it.
            # The first chunk goes in with the wrapper: the wrapper's own
            # provider/model are the request's, and a gateway that names its
            # serving backend does it in the chunk body.
            record = call_record.from_stream(stream, None if first is done else first)
            record_sent = False

            # LiteLLM rebuilds its final usage and drops API monetary fields.
            # Its retained chunks precede that rebuild and contain reported usage.
            sdk_stream = isinstance(stream, litellm.CustomStreamWrapper)
            usage_cursor = 0

            def reported_usage() -> dict[str, Any] | None:
                nonlocal usage_cursor
                usage = None
                for original in stream.chunks[usage_cursor:]:
                    raw = _usage_field(original, "usage")
                    if raw is not None:
                        usage = merge_usage(usage, self._normalize_usage(raw))
                usage_cursor = len(stream.chunks)
                return usage

            chunk = first
            delivered = False
            while chunk is not done:
                delta = self._normalize_stream_chunk(chunk)
                if sdk_stream:
                    usage = reported_usage()
                    if delta is not None:
                        delta.usage = usage
                    elif usage is not None:
                        delta = ChatDelta(content=None, usage=usage)
                if delta is not None:
                    # LiteLLM's stream wrapper answers an upstream that closed the
                    # connection without a terminal chunk by making up a final
                    # chunk with finish_reason "stop", and keeps the reason it
                    # actually received (None, then) beside it. A consumer cannot
                    # tell that fabricated stop from a real one otherwise, and a
                    # reply cut mid-thought must not read as a finished one.
                    # Except a refusal: the upstream did send it, but at a point where
                    # the wrapper has recorded no finish yet, which is the very state
                    # that marks a stop as made up.
                    if delta.finish_reason and delta.finish_reason != "refusal" and _finish_was_made_up(stream):
                        delta.finish_synthesized = True
                    delivered = delivered or bool(delta.content or delta.tool_call_delta)
                    # The terminal delta of a stream that delivered nothing is the
                    # one failure the deltas cannot describe: every field they
                    # would have carried is absent. What the upstream did put on
                    # the wire is in the chunks the wrapper retained, so that delta
                    # takes them -- and only that one, since a delivered answer
                    # needs no second copy of itself.
                    if delta.finish_reason and not delivered:
                        delta.call_record = call_record.with_body(record, call_record.stream_body(stream))
                        record_sent = True
                    elif not record_sent:
                        delta.call_record = record
                        record_sent = True
                    yield delta
                try:
                    chunk = await asyncio.wait_for(stream.__anext__(), self.generation.stream_idle_timeout)
                except TimeoutError as exc:
                    raise StreamIdleTimeoutError(idle=float(self.generation.stream_idle_timeout)) from exc
                except StopAsyncIteration:
                    if sdk_stream and (usage := reported_usage()) is not None:
                        yield ChatDelta(content=None, usage=usage, call_record=None if record_sent else record)
                    break
        finally:
            await _close(stream)

    def _normalize_usage(self, usage: Any) -> dict[str, Any]:
        """Read protocol usage.cost as reported USD, independently of the endpoint."""
        result = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = token_count(_usage_field(usage, key))
            if value is not None:
                result[key] = value
        read, write = _cache_tokens(usage)
        if read is not None:
            result["cache_read_input_tokens"] = read
        if write is not None:
            result["cache_creation_input_tokens"] = write
        reasoning = _reasoning_tokens(usage)
        if reasoning is not None:
            result["reasoning_tokens"] = reasoning
        result["prompt_tokens_include_cache"] = True
        cost = reported_cost(_usage_field(usage, "cost"))
        if cost is not None:
            result["cost_usd"] = cost
        return result

    def _normalize_stream_chunk(self, chunk: Any) -> ChatDelta | None:
        """Normalize a raw provider chunk into a ChatDelta.

        Default: OpenAI shape — `chunk.choices[0].delta.content` (str | None),
        `delta.tool_calls` (list | None), and a final `chunk.usage` snapshot
        on the trailing chunk for some providers. Returns None when the chunk
        carries no content / tool_call / usage payload so callers can skip.

        A provider-specific shape (Qwen dashscope, say) is decided against a
        real-provider smoke test: add a branch here keyed on ``self._gateway`` or
        ``find_by_model(...).name`` when one is needed.
        """
        try:
            choices = getattr(chunk, "choices", None)
            usage = getattr(chunk, "usage", None)
            if not choices and usage is None:
                return None
            choice = choices[0] if choices else None
            delta_obj = getattr(choice, "delta", None)
            content = getattr(delta_obj, "content", None)
            tool_calls = getattr(delta_obj, "tool_calls", None)
            usage = getattr(chunk, "usage", None)
            reasoning_content = getattr(delta_obj, "reasoning_content", None) or None
            refusal = _refusal_of(delta_obj)
            # Upstream states why it stopped only on the terminal chunk, which
            # otherwise carries no payload at all. Dropping that chunk would
            # discard the one signal that says the response was cut off at the
            # output ceiling rather than finished -- the difference between "the
            # model is done" and "the model was interrupted mid-token".
            finish_reason = getattr(choice, "finish_reason", None) or None
            if refusal:
                # The refusal is the reply, and the finish says so: the terminal
                # chunk after it will only say ``stop``.
                content = content or refusal
                finish_reason = "refusal"

            tool_call_delta: dict[str, Any] | None = None
            if tool_calls:
                # Surface raw tool_call deltas as a list of dict snapshots so
                # downstream layers can re-assemble; intentionally light-touch
                # here (full tool-call accumulation is the consumer's job).
                serialized = []
                for tc in tool_calls:
                    try:
                        serialized.append(tc.model_dump())  # pydantic v2
                    except AttributeError:
                        serialized.append(
                            {
                                "index": getattr(tc, "index", None),
                                "id": getattr(tc, "id", None),
                                "function": {
                                    "name": getattr(getattr(tc, "function", None), "name", None),
                                    "arguments": getattr(getattr(tc, "function", None), "arguments", None),
                                },
                            }
                        )
                tool_call_delta = {"tool_calls": serialized}

            usage_dict = self._normalize_usage(usage) if usage is not None else None

            if (
                content is None
                and tool_call_delta is None
                and usage_dict is None
                and reasoning_content is None
                and finish_reason is None
            ):
                return None

            return ChatDelta(
                content=content,
                tool_call_delta=tool_call_delta,
                usage=usage_dict,
                reasoning_content=reasoning_content,
                finish_reason=finish_reason,
            )
        except (AttributeError, IndexError):
            return None

    def _parse_response(self, response: Any, *, sent_chars: int | None = None) -> LLMResponse:
        """Parse LiteLLM response into our standard format.

        ``sent_chars`` is how much prompt went up, passed so the transport
        verdict below has something to compare the accounting against. Optional
        because the verdict's decisive evidence does not need it.
        """
        choice = response.choices[0]
        message = choice.message
        content = message.content
        finish_reason = choice.finish_reason

        # Some providers (e.g. GitHub Copilot) split content and tool_calls
        # across multiple choices. Merge them so tool_calls are not lost.
        raw_tool_calls = []
        for ch in response.choices:
            msg = ch.message
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                raw_tool_calls.extend(msg.tool_calls)
                if ch.finish_reason in ("tool_calls", "stop"):
                    finish_reason = ch.finish_reason
            if not content and msg.content:
                content = msg.content

        if len(response.choices) > 1:
            logger.debug(
                "LiteLLM response has {} choices, merged {} tool_calls", len(response.choices), len(raw_tool_calls)
            )

        # Settled before the transport verdict below: a refusal is the reply, and
        # a reply with strange accounting is still a reply.
        if refusal := _refusal_of(message):
            content = content or refusal
            finish_reason = "refusal"

        tool_calls = []
        for tc in raw_tool_calls:
            # Parse arguments from JSON string if needed. Strict first, so that
            # "this needed repairing" survives as a signal: an upstream cut mid
            # arguments arrives as an unclosed blob, and json_repair closes it
            # silently. Measured against openrouter, both Anthropic and OpenAI
            # backends send the raw fragment here, so this is the one locally
            # computable clue that the call was cut.
            args = tc.function.arguments
            repaired = False
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = json_repair.loads(args)
                    repaired = True

            provider_specific_fields = getattr(tc, "provider_specific_fields", None) or None
            function_provider_specific_fields = getattr(tc.function, "provider_specific_fields", None) or None

            tool_calls.append(
                ToolCallRequest(
                    id=_short_tool_id(),
                    name=normalized_tool_name(tc.function.name),
                    arguments=args,
                    provider_specific_fields=provider_specific_fields,
                    function_provider_specific_fields=function_provider_specific_fields,
                    run_meta=RunMeta(arguments_repaired=True) if repaired else None,
                )
            )

        raw_usage = getattr(response, "usage", None)
        usage = self._normalize_usage(raw_usage) if raw_usage is not None else {}

        reasoning_content = getattr(message, "reasoning_content", None) or None
        thinking_blocks = getattr(message, "thinking_blocks", None) or None

        if not reasoning_content and isinstance(content, str) and self.emits_unparsed_reasoning():
            split_reasoning, content = split_orphan_think(content)
            reasoning_content = split_reasoning or reasoning_content
        # A reply that is only an upstream sentinel is handed on as empty, so the
        # loop's recovery owns it rather than the user reading `<|im_end|>`.
        content = without_sentinels(content)

        # Asked here rather than by the caller: this is the response exit, and
        # by the time the ladder in `base.py` reads `finish_reason` the only
        # thing standing between an upstream-reported failure and being
        # delivered as an answer is this verdict. Both gates there
        # (`!= "error"`) then work unchanged.
        evidence = flag_transport_failure(
            # The upstream's own word, not the `or "unknown"` default below: a
            # provider that sent no finish reason never said this call ended
            # normally, so there is nothing here to disbelieve.
            finish_reason=finish_reason,
            content=content,
            reasoning=reasoning_content,
            tool_calls=tool_calls,
            # Read off the first choice even though the merge above can adopt a
            # later choice's reason: it only does so for a choice that carried
            # tool calls, and a delivered call already stops this verdict.
            native_finish_reason=native_finish_reason(choice),
            usage=usage,
            sent_chars=sent_chars,
        )
        # Asked once, before either exit: a response that delivered nothing is
        # the one whose body has to be kept, and both exits below are that case.
        record = call_record.from_response(response, usable=not said_nothing(content, tool_calls))

        if evidence:
            logger.warning("upstream reported a failed call as a normal end: {}", evidence)
            return transport_failure_response(evidence, usage=usage, call_record=record)

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason or "unknown",
            usage=usage,
            reasoning_content=reasoning_content,
            thinking_blocks=thinking_blocks,
            call_record=record,
        )
