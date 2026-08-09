"""Base LLM provider interface."""

import asyncio
import json
import random
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from typing import Any

from loguru import logger

from raven.tracing import semconv, trace

# Wordings providers use to reject list-type content in a tool message. Each is
# a real 400 body, not a guess: the first group was measured against
# OpenRouter -> OpenAI, the rest are the set Hermes accumulated across vendors
# (agent/error_classifier.py, MIT, see LICENSES/MIT-hermes-agent.txt).
#
# Some are ambiguous alone -- "text is not set" says nothing about images -- and
# that is safe here because the recovery is a no-op when no tool result actually
# carries one, so a false match costs nothing and never retries blind.
_TOOL_IMAGE_REJECTION_PATTERNS = (
    # OpenAI, measured: "Invalid 'messages[2]'. Image URLs are only allowed for
    # messages with role 'user', but this message with role 'tool' contains an
    # image URL."
    "only allowed for messages with role",
    # Xiaomi MiMo: {"code":"400","message":"Param Incorrect","param":"text is not set"}
    "text is not set",
    # Generic "tool message must be a string" shapes
    "tool message content must be a string",
    "tool content must be a string",
    "tool message must be a string",
    # OpenAI-compatible servers rejecting list content at schema validation.
    # The DeepInfra wording was measured on 2026-07-31 (422, not 400):
    # {"message":"Input should be a valid string","param":"messages.2.function..."}
    "expected string, got list",
    "expected string, got array",
    "input should be a valid string",
    # Alibaba / DashScope
    "tool_call.content must be string",
)


@dataclass(frozen=True)
class ErrorClassification:
    """Structured verdict on a failed LLM call — replaces substring guessing.

    Drives the recovery strategy:
      - ``retryable``       → retry the same model after backoff
      - ``should_fallback`` → a different model/provider might succeed
      - ``should_compress`` → context-window overflow; shrink then retry
      - ``should_drop_tool_images`` → the endpoint refuses an image inside a
        tool result; move it to a user message then retry
    ``category`` is for logging/telemetry only.
    """

    category: str
    retryable: bool = False
    should_fallback: bool = False
    should_compress: bool = False
    should_drop_tool_images: bool = False
    #: The upstream refused the prompt-cache breakpoints specifically. Decided
    #: here for the same reason the rest of this verdict is: a provider that
    #: swallows the exception into a string loses the response body with it, and
    #: whether ``str()`` carried that body is a property of the client that
    #: raised it. Deciding while the exception is alive makes it one answer.
    refuses_prompt_cache: bool = False


@dataclass
class ToolCallRequest:
    """A tool call request from the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]
    provider_specific_fields: dict[str, Any] | None = None
    function_provider_specific_fields: dict[str, Any] | None = None

    def to_openai_tool_call(self) -> dict[str, Any]:
        """Serialize to an OpenAI-style tool_call payload."""
        tool_call = {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, ensure_ascii=False),
            },
        }
        if self.provider_specific_fields:
            tool_call["provider_specific_fields"] = self.provider_specific_fields
        if self.function_provider_specific_fields:
            tool_call["function"]["provider_specific_fields"] = self.function_provider_specific_fields
        return tool_call


@dataclass
class LLMResponse:
    """Response from an LLM provider."""

    content: str | None
    tool_calls: list[ToolCallRequest] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, int] = field(default_factory=dict)
    reasoning_content: str | None = None  # Kimi, DeepSeek-R1 etc.
    thinking_blocks: list[dict] | None = None  # Anthropic extended thinking
    # Set when finish_reason == "error". Providers that have the live exception
    # attach a precise classification here; otherwise the retry layer fills it
    # in from the error string.
    error_classification: "ErrorClassification | None" = None

    @property
    def has_tool_calls(self) -> bool:
        """Check if response contains tool calls."""
        return len(self.tool_calls) > 0


@dataclass
class StreamDelta:
    """Single normalized delta from a streaming LLM response.

    Producers (provider.chat_stream) yield one of these per non-empty chunk.
    Consumers (AgentLoop on_token_delta path, TUI SubscriptionEmitter) read
    `.content` for incremental token text; `tool_call_delta` / `usage` are
    optional carriers for in-stream tool deltas and final usage snapshots.

    `finish_reason` / `error_classification` are only ever set on the
    terminal delta of a stream (mirroring `LLMResponse`); mid-stream deltas
    leave both as ``None``.
    """

    content: str | None
    tool_call_delta: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    reasoning_content: str | None = None  # Kimi, DeepSeek-R1, qwen, o-series thinking stream
    finish_reason: str | None = None
    error_classification: ErrorClassification | None = None


@dataclass(frozen=True)
class GenerationSettings:
    """Default generation parameters for LLM calls.

    Stored on the provider so every call site inherits the same defaults
    without having to pass temperature / max_tokens / reasoning_effort
    through every layer.  Individual call sites can still override by
    passing explicit keyword arguments to chat() / chat_with_retry().
    """

    temperature: float = 0.7
    max_tokens: int = 4096
    reasoning_effort: str | None = None
    timeout: float = 600.0


class LLMProvider(ABC):
    """
    Abstract base class for LLM providers.

    Implementations should handle the specifics of each provider's API
    while maintaining a consistent interface.
    """

    _CHAT_RETRY_DELAYS = (1, 2, 4)
    _SENTINEL = object()

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key
        self.api_base = api_base
        self.generation: GenerationSettings = GenerationSettings()

    @staticmethod
    def _sanitize_empty_content(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace empty text content that causes provider 400 errors.

        Empty content can appear when MCP tools return nothing. Most providers
        reject empty-string content or empty text blocks in list content.
        """
        result: list[dict[str, Any]] = []
        for msg in messages:
            content = msg.get("content")

            if isinstance(content, str) and not content:
                clean = dict(msg)
                clean["content"] = None if (msg.get("role") == "assistant" and msg.get("tool_calls")) else "(empty)"
                result.append(clean)
                continue

            if isinstance(content, list):
                filtered = [
                    item
                    for item in content
                    if not (
                        isinstance(item, dict)
                        and item.get("type") in ("text", "input_text", "output_text")
                        and not item.get("text")
                    )
                ]
                if len(filtered) != len(content):
                    clean = dict(msg)
                    if filtered:
                        clean["content"] = filtered
                    elif msg.get("role") == "assistant" and msg.get("tool_calls"):
                        clean["content"] = None
                    else:
                        clean["content"] = "(empty)"
                    result.append(clean)
                    continue

            if isinstance(content, dict):
                clean = dict(msg)
                clean["content"] = [content]
                result.append(clean)
                continue

            result.append(msg)
        return result

    @staticmethod
    def _sanitize_request_messages(
        messages: list[dict[str, Any]],
        allowed_keys: frozenset[str],
    ) -> list[dict[str, Any]]:
        """Keep only provider-safe message keys and normalize assistant content."""
        sanitized = []
        for msg in messages:
            clean = {k: v for k, v in msg.items() if k in allowed_keys}
            if clean.get("role") == "assistant" and "content" not in clean:
                clean["content"] = None
            sanitized.append(clean)
        return sanitized

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        """
        Send a chat completion request.

        Args:
            messages: List of message dicts with 'role' and 'content'.
            tools: Optional list of tool definitions.
            model: Model identifier (provider-specific).
            max_tokens: Maximum tokens in response.
            temperature: Sampling temperature.
            tool_choice: Tool selection strategy ("auto", "required", or specific tool dict).

        Returns:
            LLMResponse with content and/or tool calls.
        """
        pass

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int = 4096,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """Non-streaming fallback: emit the full ``chat()`` response as a single
        terminal delta.

        The TUI agent loop drives turns via ``chat_stream``; providers without a
        real streaming implementation (custom-bespoke / azure / codex) would
        otherwise ``AttributeError`` there. This default makes any provider that
        implements ``chat`` usable in the streaming path — without token-level
        streaming. ``LiteLLMProvider`` overrides this with true streaming.
        """
        response = await self.chat(
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            reasoning_effort=reasoning_effort,
            tool_choice=tool_choice,
        )
        tool_call_delta: dict[str, Any] | None = None
        if response.tool_calls:
            tool_call_delta = {
                "tool_calls": [
                    {
                        "index": i,
                        "id": tc.id,
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                        },
                    }
                    for i, tc in enumerate(response.tool_calls)
                ]
            }
        yield StreamDelta(
            content=response.content,
            tool_call_delta=tool_call_delta,
            usage=response.usage or None,
            reasoning_content=response.reasoning_content,
            finish_reason=response.finish_reason,
            error_classification=response.error_classification,
        )

    @staticmethod
    def _extract_status_code(exc: BaseException | None) -> int | None:
        """Walk the exception's cause/context chain for an HTTP status code."""
        seen: set[int] = set()
        cur: BaseException | None = exc
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            for attr in ("status_code", "http_status", "code"):
                val = getattr(cur, attr, None)
                if isinstance(val, int) and 100 <= val < 600:
                    return val
            cur = cur.__cause__ or cur.__context__
        return None

    @staticmethod
    def _error_type_names(exc: BaseException | None) -> set[str]:
        """Lowercased class names across the exception's MRO + cause chain.

        Lets us recognize provider exception types (RateLimitError,
        ContextWindowExceededError, ...) without importing any provider SDK.
        """
        names: set[str] = set()
        seen: set[int] = set()
        cur: BaseException | None = exc
        while cur is not None and id(cur) not in seen:
            seen.add(id(cur))
            for klass in type(cur).__mro__:
                names.add(klass.__name__.lower())
            cur = cur.__cause__ or cur.__context__
        return names

    @classmethod
    def classify_error(
        cls,
        exc: BaseException | None = None,
        content: str | None = None,
    ) -> ErrorClassification:
        """Classify a failed call by exception type + HTTP status + message.

        Precise when given the live exception (status code + class names);
        degrades to substring matching when the provider already swallowed it
        into ``content`` -- which is why every verdict, including
        ``refuses_prompt_cache``, is decided here rather than downstream.
        """
        from raven.providers import prompt_cache

        verdict = cls._classify(exc, content)
        if prompt_cache.is_rejection(exc if exc is not None else (content or "")):
            return replace(verdict, refuses_prompt_cache=True)
        return verdict

    @classmethod
    def _classify(
        cls,
        exc: BaseException | None = None,
        content: str | None = None,
    ) -> ErrorClassification:
        """The bucket this failure falls in. Order matters: context-overflow and
        rate-limit are checked before the generic 400/server buckets."""
        status = cls._extract_status_code(exc)
        names = cls._error_type_names(exc)
        msg = (content if content is not None else str(exc) if exc is not None else "").lower()

        def has(*needles: str) -> bool:
            return any(n in msg for n in needles)

        # Context-window overflow → compress and retry, NOT fallback (a smaller
        # window won't help; the same model after compaction will). Detected by
        # class name first — a bare 400 otherwise looks like invalid_request.
        if "contextwindowexceedederror" in names or has(
            "context length",
            "context window",
            "maximum context",
            "too many tokens",
            "reduce the length",
        ):
            return ErrorClassification("context_overflow", should_compress=True)

        # Rate limit → wait and retry; a different provider may not be throttled.
        if (
            status == 429
            or "ratelimiterror" in names
            or has(
                "rate limit",
                "429",
                "too many requests",
            )
        ):
            return ErrorClassification("rate_limit", retryable=True, should_fallback=True)

        # Transient server / capacity → retry + fallback.
        if (
            status in (500, 502, 503, 504)
            or {"internalservererror", "serviceunavailableerror", "badgatewayerror"} & names
            or has(
                "overloaded",
                "server error",
                "service unavailable",
                "temporarily unavailable",
                "500",
                "502",
                "503",
                "504",
            )
        ):
            return ErrorClassification("server", retryable=True, should_fallback=True)

        # Timeout / connection → retry + fallback. isinstance covers the builtin
        # TimeoutError raised by asyncio.wait_for (its class name "timeouterror"
        # and empty str() match neither the name set nor the substrings below).
        if (
            isinstance(exc, TimeoutError)
            or {"timeout", "apitimeouterror", "apiconnectionerror"} & names
            or has(
                "timeout",
                "timed out",
                "connection",
            )
        ):
            return ErrorClassification("network", retryable=True, should_fallback=True)

        # Auth / permission → fatal config; retry & fallback won't fix it.
        if (
            status in (401, 403)
            or {"authenticationerror", "permissiondeniederror"} & names
            or has(
                "unauthorized",
                "invalid api key",
                "permission denied",
            )
        ):
            return ErrorClassification("auth")

        # Billing / quota → same model can't recover, a different provider might.
        if status == 402 or has(
            "billing",
            "quota",
            "insufficient",
            "credit",
            "payment",
            "exceeded your current",
        ):
            return ErrorClassification("billing", should_fallback=True)

        # Model unavailable / not found → no point retrying it; try another model.
        # "404" as a substring mirrors the 429/5xx buckets above: a provider
        # that embeds the status into a rendered string (azure's non-200 path)
        # reaches here with no exception to read a status code from, and a
        # route-level body like "Resource not found" names none of the wordier
        # markers.
        if (
            status == 404
            or "notfounderror" in names
            or has(
                "model not found",
                "does not exist",
                "no endpoints",
                "not available",
                "unavailable",
                "404",
            )
        ):
            return ErrorClassification("model_unavailable", should_fallback=True)

        # An image inside a role="tool" message the endpoint won't take → resend
        # with the picture moved to a following user message. Must precede the
        # generic 400 bucket below, which is fatal.
        #
        # The first clause is that bucket's condition plus ``badrequesterror`` in
        # the *message*: once a provider has swallowed the exception into content
        # there is no status code or class name left to read, and LiteLLM's
        # swallowed form reads "litellm.BadRequestError: ...". That is the
        # substring degradation this method's docstring describes, and it is why
        # this branch recognises a 400 the bucket below would call unknown.
        if (
            status == 400 or "badrequesterror" in names or has("badrequesterror", "invalid request", "invalid_request")
        ) and has(*_TOOL_IMAGE_REJECTION_PATTERNS):
            return ErrorClassification("tool_image_unsupported", should_drop_tool_images=True)

        # Generic bad request (non-context 400) → fatal; no model swap helps.
        if status == 400 or "badrequesterror" in names or has("invalid request", "invalid_request"):
            return ErrorClassification("invalid_request")

        return ErrorClassification("unknown")

    @classmethod
    def _is_transient_error(cls, content: str | None) -> bool:
        """Back-compat shim — retryable verdict from the string classifier."""
        return cls.classify_error(content=content).retryable

    @classmethod
    def _should_fallback(cls, content: str | None) -> bool:
        """Back-compat shim — fallback verdict from the string classifier."""
        return cls.classify_error(content=content).should_fallback

    @staticmethod
    def _jittered(delay: float) -> float:
        """Apply +/-10% jitter to a backoff delay to avoid synchronized retries."""
        if delay <= 0:
            return 0.0
        return delay * random.uniform(0.9, 1.1)

    async def _chat_attempt_with_retry(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: object,
        temperature: object,
        reasoning_effort: object,
        tool_choice: str | dict[str, Any] | None,
    ) -> LLMResponse:
        """Run a single model through the retry ladder, classifying each failure.

        ``len(_CHAT_RETRY_DELAYS)`` sleeping attempts + 1 final no-sleep attempt.
        Retries only ``retryable`` errors (with jittered backoff); a
        non-retryable error returns immediately. The returned error response
        always carries an ``error_classification`` so the caller (model-chain
        fallback) can decide without re-classifying.
        """
        from raven.providers import prompt_cache

        total_attempts = len(self._CHAT_RETRY_DELAYS) + 1
        last_response: LLMResponse | None = None
        dropped_cache_control = False
        for attempt in range(1, total_attempts + 1):
            exc: Exception | None = None
            try:
                response = await self.chat(
                    messages=messages,
                    tools=tools,
                    model=model,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    reasoning_effort=reasoning_effort,
                    tool_choice=tool_choice,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                exc = e
                response = LLMResponse(content=f"Error calling LLM: {e}", finish_reason="error")

            if response.finish_reason != "error":
                return response

            # Prefer a provider-attached classification (it had the live
            # exception); else classify the exception we caught, else the string.
            classification = response.error_classification or self.classify_error(exc, response.content)
            response.error_classification = classification
            last_response = response

            # Why an upstream can refuse this at all: see
            # ``providers.prompt_cache.suppress``. Learned from the refusal, once
            # per model. The marks already in the payload were placed upstream by
            # a token strategy, so they are taken off here; suppressing stops the
            # provider adding its own back on the way out.
            # Read off the verdict rather than re-derived here: by this point a
            # provider may have turned the exception into a string.
            if not dropped_cache_control and attempt < total_attempts and classification.refuses_prompt_cache:
                dropped_cache_control = True
                prompt_cache.suppress(model or getattr(self, "default_model", "") or "")
                messages, tools = prompt_cache.strip(messages, tools)
                continue

            if not classification.retryable or attempt == total_attempts:
                return response

            delay = self._jittered(self._CHAT_RETRY_DELAYS[attempt - 1])
            logger.warning(
                "LLM error [{}] (attempt {}/{}) model={}, retrying in {:.1f}s: {}",
                classification.category,
                attempt,
                total_attempts,
                model,
                delay,
                (response.content or "")[:120],
            )
            await asyncio.sleep(delay)

        return last_response  # type: ignore[return-value]  # loop always returns on the last attempt

    def can_serve(self, model: str) -> bool:
        """Whether this provider instance's credentials and wire can serve this model.

        Default True: the base class knows nothing about routing, and a wrong
        guess must fail loudly at the wire rather than silently skip a hop.
        """
        return True

    @trace.instrument("llm.call", extract=semconv.llm_call)
    async def chat_with_retry(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = _SENTINEL,
        temperature: object = _SENTINEL,
        reasoning_effort: object = _SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
        fallback_models: list[str] | None = None,
    ) -> LLMResponse:
        """Call chat() with retry on transient failures, then fall back models.

        Each model in ``[model, *fallback_models]`` is run through the full
        retry ladder. When a model is exhausted with a fallback-worthy error
        (``error_classification.should_fallback``) and another model remains,
        the next model is tried; otherwise the error surfaces to the caller.
        With ``fallback_models`` empty this is exactly the old single-model
        retry behavior.

        Parameters default to ``self.generation`` when not explicitly passed,
        so callers no longer need to thread temperature / max_tokens /
        reasoning_effort through every layer.
        """
        if max_tokens is self._SENTINEL:
            max_tokens = self.generation.max_tokens
        if temperature is self._SENTINEL:
            temperature = self.generation.temperature
        if reasoning_effort is self._SENTINEL:
            reasoning_effort = self.generation.reasoning_effort

        from raven.providers import prompt_cache

        model_chain = [model, *(fallback_models or [])]
        response: LLMResponse | None = None
        for idx, current_model in enumerate(model_chain):
            # A fallback hop that this instance's credentials/wire cannot serve
            # (e.g. a direct provider whose fallback model resolves to another
            # vendor) is skipped rather than sent -- the wrong key on the wrong
            # wire either 400s outright or, worse, silently answers under a
            # same-named model from the wrong vendor. Never skips the primary
            # model: idx 0 is what the caller asked for.
            if idx and not self.can_serve(current_model or ""):
                logger.warning(
                    "Skipping fallback model={} - this provider instance cannot serve it (wrong vendor)",
                    current_model,
                )
                continue

            # The breakpoints in this payload were placed for whoever was asked
            # first. A fallback is a different model, often a different vendor,
            # and the field it does not read is billed rather than refused --
            # sending Anthropic's markers on to Gemini is what doubled a prompt.
            if idx and not prompt_cache.accepts_cache_control(current_model or ""):
                messages, tools = prompt_cache.strip(messages, tools)
            response = await self._chat_attempt_with_retry(
                messages=messages,
                tools=tools,
                model=current_model,
                max_tokens=max_tokens,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                tool_choice=tool_choice,
            )
            if response.finish_reason != "error":
                return response

            classification = response.error_classification or self.classify_error(content=response.content)
            has_next = idx + 1 < len(model_chain)
            if has_next and classification.should_fallback:
                next_model = model_chain[idx + 1]
                logger.warning(
                    "LLM call failed on model={} [{}], falling back to {}: {}",
                    current_model,
                    classification.category,
                    next_model,
                    (response.content or "")[:120],
                )
                continue
            return response

        return response  # type: ignore[return-value]  # chain always non-empty

    @abstractmethod
    def get_default_model(self) -> str:
        """Get the default model for this provider."""
        pass
