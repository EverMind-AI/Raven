"""The provider base every adapter subclasses: the paper's interface plus the shared machinery.

:mod:`raven.contracts.llm_provider` declares what a provider answers; this
module is how the answers get produced -- request sanitizing, error
classification, the retry ladder, the tool-image rejection recovery, tracing
-- and the error-string helpers the harness reads a swallowed failure with.
The paper's shapes are re-exported so one import path serves both.
"""

import asyncio
import json
import random
import re
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

from loguru import logger

from raven.contracts.llm_provider import (  # noqa: F401
    ChatDelta,
    ErrorClassification,
    GenerationSettings,
    LLMResponse,
    ProviderHTTPError,
    RunMeta,
    ToolCallRequest,
    TruncationInfo,
)
from raven.contracts.llm_provider import LLMProvider as _LLMProviderPaper
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


_EXC_NAME_PREFIX_RE = re.compile(r"^([A-Za-z_][\w.]*(?:Error|Exception))\s*:\s*")


_JSON_MESSAGE_RE = re.compile(r'"message"\s*:\s*"([^"]*)"')


_LLM_ERROR_CONTENT_RE = re.compile(
    r"^Error calling LLM \((?P<category>[a-z_]+)(?:@(?P<provider>[A-Za-z0-9._-]+))?\):\s*(?P<detail>.*)$",
    re.DOTALL,
)


def _strip_json_error_body(text: str) -> str:
    """Replace a raw JSON error body with its human-readable message.

    Rewrites only when a message is actually extracted, and only within the
    parsed object's own boundary -- trailing text after the JSON survives, and
    a body yielding no message leaves the text unchanged rather than truncated.
    """
    idx = text.find("{")
    if idx == -1:
        return text
    candidate = text[idx:]
    if not candidate.startswith(('{"', "{'")):
        return text
    try:
        obj, end = json.JSONDecoder().raw_decode(candidate)
    except ValueError:
        # Malformed JSON has no knowable boundary; treat the rest of the text
        # as the body, which is the shape the swallowed litellm errors have.
        obj, end = None, len(candidate)
    message = ""
    if isinstance(obj, dict):
        err = obj.get("error")
        found = err.get("message") if isinstance(err, dict) else None
        found = found or obj.get("message")
        if isinstance(found, str):
            message = found.strip()
    if not message:
        m = _JSON_MESSAGE_RE.search(candidate[:end])
        message = m.group(1).strip() if m else ""
    if not message:
        return text
    head = text[:idx].rstrip()
    tail = candidate[end:].strip()
    return " ".join(part for part in (head, message, tail) if part)


def format_llm_error(
    exc: BaseException,
    classification: ErrorClassification,
    provider: str | None = None,
) -> str:
    """Build the canonical content for a failed LLM call.

    Shape: ``Error calling LLM (<category>[@<provider>]): <detail>``. The head
    is machine-parseable (see ``parse_llm_error``) so rendering surfaces can
    show a diagnosis + fix hint instead of the raw exception; the detail drops
    duplicated exception-name prefixes and raw JSON error bodies.
    """
    detail = str(exc).strip()
    names: list[str] = []
    while True:
        m = _EXC_NAME_PREFIX_RE.match(detail)
        if not m:
            break
        name = m.group(1).rsplit(".", 1)[-1]
        if name not in names:
            names.append(name)
        detail = detail[m.end() :]
    detail = _strip_json_error_body(detail).strip()
    prefix = "".join(f"{n}: " for n in names)
    detail = f"{prefix}{detail}".strip().rstrip(":-").strip() or type(exc).__name__
    head = f"{classification.category}@{provider}" if provider else classification.category
    return f"Error calling LLM ({head}): {detail}"


def parse_llm_error(content: str | None) -> tuple[str, str | None, str] | None:
    """Parse content built by ``format_llm_error`` back into
    ``(category, provider, detail)``; ``None`` when the text is not one."""
    m = _LLM_ERROR_CONTENT_RE.match((content or "").strip())
    if not m:
        return None
    return m.group("category"), m.group("provider"), m.group("detail").strip()


def send_max_tokens(generation: Any, model: str | None, *, pinned: int | None = None, allow_fetch: bool = True) -> int:
    """The output ceiling a request will actually carry.

    One function for both the request body and the agent loop's ceiling check.
    Computed separately the two would drift the moment either side grew a
    bound, and the check would stop firing without ever failing.

    A pin is a call site asking for a deliberately short answer, so it wins --
    but never above what the model accepts. Every pin in the tree today is far
    below any real ceiling, which is exactly why the bound has to be written
    down: the first pin that is not would be a rejected request, and nothing
    about the call site would say why.

    ``pinned`` is the per-call argument (``judge`` asks for 64, the curator for
    2048); the settings object carries the per-provider one. The argument has
    to arrive here rather than bypass the function, or the two ways of asking
    for a short answer are bounded by different rules and only one of them is
    the number truncation is judged against.

    ``allow_fetch=False`` is for callers that only need a reservation and must
    not stall on the catalogue's importing tier (~2-7s in a fresh process);
    they get whatever is already loaded, then the fixed fallback. A caller
    about to build a request wants the default.

    The model's own ceiling is the answer, unbounded by anything else. How much
    of the window a turn holds back for its reply is the budget's business, and
    it reserves exactly this number: requests no longer name a ceiling, so the
    one that applies is the model's own, and the prompt has to fit beside it.
    """
    from raven.providers.rates import resolve_max_output_tokens

    ceiling = resolve_max_output_tokens(model, allow_fetch=allow_fetch)
    pin = pinned if pinned is not None else getattr(generation, "max_tokens", None)
    if pin:
        return min(int(pin), ceiling)
    return ceiling


class LLMProvider(_LLMProviderPaper):
    """The base adapters subclass: the paper's interface with the machinery filled in.

    Subclasses implement ``chat`` and ``get_default_model`` (and a real
    ``chat_stream`` when the wire streams); everything the harness calls on top
    of that -- ``chat_with_retry``, ``classify_error`` -- is here once.
    """

    _SENTINEL = _LLMProviderPaper._SENTINEL

    _CHAT_RETRY_DELAYS = (1, 2, 4)

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

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = _SENTINEL,
        temperature: object = _SENTINEL,
        reasoning_effort: object = _SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncIterator[ChatDelta]:
        """Non-streaming fallback: emit the full ``chat()`` response as a single
        terminal delta.

        The TUI agent loop drives turns via ``chat_stream``; providers without a
        real streaming implementation (custom-bespoke / azure / codex) would
        otherwise ``AttributeError`` there. This default makes any provider that
        implements ``chat`` usable in the streaming path — without token-level
        streaming. ``LiteLLMProvider`` overrides this with true streaming.

        Generation defaults resolve from ``self.generation`` the same way
        ``chat_with_retry`` does: literal defaults here would shadow the user's
        configuration, since the agent loop calls this with messages/tools/model
        only.

        ``generation`` is read defensively -- a subclass that never runs this
        ``__init__`` (thin adapters, test doubles) reaches this method with the
        attribute missing, and a crash there would be worse than the settings
        it is meant to restore.
        """
        gen = getattr(self, "generation", None) or GenerationSettings()
        if max_tokens is self._SENTINEL:
            max_tokens = gen.max_tokens
        if temperature is self._SENTINEL:
            temperature = gen.temperature
        if reasoning_effort is self._SENTINEL:
            reasoning_effort = gen.reasoning_effort
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
        yield ChatDelta(
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
        # No bare "404" substring here: it also matched the 404 inside "retry
        # after 1404ms", a request id, and a character offset -- each one
        # burning a fallback model and cooling a healthy endpoint for an error
        # no swap can fix. A provider that renders its non-200 body into a
        # plain string before it reaches this method (azure's path) attaches
        # the classification at the source instead, where the real status
        # code is still available -- see ``AzureOpenAIProvider.chat``.
        if (
            status == 404
            or "notfounderror" in names
            or has(
                "model not found",
                "does not exist",
                "no endpoints",
                "not available",
                "unavailable",
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
                response = LLMResponse(content=None, finish_reason="error")

            if response.finish_reason != "error":
                return response

            # Prefer a provider-attached classification (it had the live
            # exception); else classify the exception we caught, else the string.
            classification = response.error_classification or self.classify_error(exc, response.content or None)
            response.error_classification = classification
            if exc is not None and not response.content:
                response.content = format_llm_error(exc, classification, provider=getattr(self, "provider_name", None))
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
        With ``fallback_models`` empty a single model runs the ladder.

        Parameters default to ``self.generation`` when not explicitly passed, so
        callers need not thread temperature / max_tokens / reasoning_effort
        through every layer.
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
            # One id for both the bound below and the check further down, and
            # the Wire Model this request goes out under rather than the Model Ref --
            # a gateway spelling is its own catalogue row with its own ceiling.
            wire_id = self.wire_model_id(current_model or "")
            # Bounded here rather than inside each provider: a pin is per call
            # but a ceiling is per model, so a fallback hop can change it. Left
            # as ``None`` when nobody pinned, which is the provider's cue to
            # resolve the model's own ceiling.
            sent = None if max_tokens is None else send_max_tokens(self.generation, wire_id, pinned=max_tokens)
            response = await self._chat_attempt_with_retry(
                messages=messages,
                tools=tools,
                model=current_model,
                max_tokens=sent,
                temperature=temperature,
                reasoning_effort=reasoning_effort,
                tool_choice=tool_choice,
            )
            if response.finish_reason != "error":
                # Judged here, not by the caller: this runs inside the
                # ``llm.call`` span, and ``trace.instrument`` extracts its
                # attributes in a ``finally`` that closes the span before the
                # caller sees the result -- a verdict reached afterwards is
                # recorded as ``False`` every time.
                from raven.providers.truncation import flag_truncation

                response.max_tokens, response.truncated = flag_truncation(
                    sent=sent,
                    finish_reason=response.finish_reason,
                    usage=response.usage,
                    tool_calls=response.tool_calls,
                )
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


__all__ = [
    "ErrorClassification",
    "GenerationSettings",
    "LLMProvider",
    "LLMResponse",
    "ProviderHTTPError",
    "RunMeta",
    "ChatDelta",
    "ToolCallRequest",
    "TruncationInfo",
    "format_llm_error",
    "parse_llm_error",
    "send_max_tokens",
]
