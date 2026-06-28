"""Base LLM provider interface."""

import asyncio
import json
import random
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from loguru import logger


@dataclass(frozen=True)
class ErrorClassification:
    """Structured verdict on a failed LLM call — replaces substring guessing.

    Drives the recovery strategy:
      - ``retryable``       → retry the same model after backoff
      - ``should_fallback`` → a different model/provider might succeed
      - ``should_compress`` → context-window overflow; shrink then retry
    ``category`` is for logging/telemetry only.
    """

    category: str
    retryable: bool = False
    should_fallback: bool = False
    should_compress: bool = False


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
    """

    content: str | None
    tool_call_delta: dict[str, Any] | None = None
    usage: dict[str, Any] | None = None
    reasoning_content: str | None = None  # Kimi, DeepSeek-R1, qwen, o-series thinking stream


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
                    item for item in content
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
        cls, exc: BaseException | None = None, content: str | None = None,
    ) -> ErrorClassification:
        """Classify a failed call by exception type + HTTP status + message.

        Precise when given the live exception (status code + class names);
        degrades to substring matching when the provider already swallowed it
        into ``content``. Order matters: context-overflow and rate-limit are
        checked before the generic 400/server buckets.
        """
        status = cls._extract_status_code(exc)
        names = cls._error_type_names(exc)
        msg = (content if content is not None else str(exc) if exc is not None else "").lower()

        def has(*needles: str) -> bool:
            return any(n in msg for n in needles)

        # Context-window overflow → compress and retry, NOT fallback (a smaller
        # window won't help; the same model after compaction will). Detected by
        # class name first — a bare 400 otherwise looks like invalid_request.
        if "contextwindowexceedederror" in names or has(
            "context length", "context window", "maximum context",
            "too many tokens", "reduce the length",
        ):
            return ErrorClassification("context_overflow", should_compress=True)

        # Rate limit → wait and retry; a different provider may not be throttled.
        if status == 429 or "ratelimiterror" in names or has(
            "rate limit", "429", "too many requests",
        ):
            return ErrorClassification("rate_limit", retryable=True, should_fallback=True)

        # Transient server / capacity → retry + fallback.
        if (
            status in (500, 502, 503, 504)
            or {"internalservererror", "serviceunavailableerror", "badgatewayerror"} & names
            or has("overloaded", "server error", "service unavailable",
                   "temporarily unavailable", "500", "502", "503", "504")
        ):
            return ErrorClassification("server", retryable=True, should_fallback=True)

        # Timeout / connection → retry + fallback.
        if {"timeout", "apitimeouterror", "apiconnectionerror"} & names or has(
            "timeout", "timed out", "connection",
        ):
            return ErrorClassification("network", retryable=True, should_fallback=True)

        # Auth / permission → fatal config; retry & fallback won't fix it.
        if status in (401, 403) or {"authenticationerror", "permissiondeniederror"} & names or has(
            "unauthorized", "invalid api key", "permission denied",
        ):
            return ErrorClassification("auth")

        # Billing / quota → same model can't recover, a different provider might.
        if status == 402 or has(
            "billing", "quota", "insufficient", "credit", "payment", "exceeded your current",
        ):
            return ErrorClassification("billing", should_fallback=True)

        # Model unavailable / not found → no point retrying it; try another model.
        if status == 404 or "notfounderror" in names or has(
            "model not found", "does not exist", "no endpoints", "not available", "unavailable",
        ):
            return ErrorClassification("model_unavailable", should_fallback=True)

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
        total_attempts = len(self._CHAT_RETRY_DELAYS) + 1
        last_response: LLMResponse | None = None
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

            if not classification.retryable or attempt == total_attempts:
                return response

            delay = self._jittered(self._CHAT_RETRY_DELAYS[attempt - 1])
            logger.warning(
                "LLM error [{}] (attempt {}/{}) model={}, retrying in {:.1f}s: {}",
                classification.category, attempt, total_attempts, model, delay,
                (response.content or "")[:120],
            )
            await asyncio.sleep(delay)

        return last_response  # type: ignore[return-value]  # loop always returns on the last attempt

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

        model_chain = [model, *(fallback_models or [])]
        response: LLMResponse | None = None
        for idx, current_model in enumerate(model_chain):
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
