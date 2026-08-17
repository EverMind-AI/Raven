"""LiteLLM provider implementation for multi-provider support."""

import asyncio
import hashlib
import json
import os
import secrets
import string
import uuid
import warnings
from collections.abc import AsyncIterator
from typing import Any

import json_repair
from loguru import logger

from raven.providers import prompt_cache
from raven.providers.base import (
    GenerationSettings,
    LLMProvider,
    LLMResponse,
    RunMeta,
    StreamDelta,
    ToolCallRequest,
    format_llm_error,
)
from raven.providers.litellm_setup import import_litellm
from raven.providers.prompt_cache import CACHE_CONTROL
from raven.providers.reasoning import split_orphan_think
from raven.providers.registry import (
    canonical_provider_name,
    find_by_keywords,
    find_by_model,
    find_by_name,
    find_gateway,
)
from raven.providers.wire import wire_model

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
_ANTHROPIC_EXTRA_KEYS = frozenset({"thinking_blocks"})
_ALNUM = string.ascii_letters + string.digits

# LiteLLM defaults to X-Title="liteLLM" / HTTP-Referer="https://litellm.ai" for OpenRouter
# requests, which would credit traffic to liteLLM instead of Raven on openrouter.ai/apps.
# Explicit headers here override those defaults; user-supplied extra_headers win over these.
_OPENROUTER_ATTRIBUTION: dict[str, str] = {
    "HTTP-Referer": "https://raven.evermind.ai",
    "X-Title": "Raven Agent",
    "X-OpenRouter-Title": "Raven Agent",
    "X-OpenRouter-Categories": "cli-agent,personal-agent",
}


def _short_tool_id() -> str:
    """Generate a 9-char alphanumeric ID compatible with all providers (incl. Mistral)."""
    return "".join(secrets.choice(_ALNUM) for _ in range(9))


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


def session_affinity_headers() -> dict[str, str]:
    """Headers pinning one caller to one backend replica.

    Self-hosted OpenAI-compatible backends (vLLM and friends) route by this
    header, so a stable value per provider instance keeps prefix-cache hits warm.
    """
    return {"x-session-affinity": uuid.uuid4().hex}


class LiteLLMProvider(LLMProvider):
    """
    LLM provider using LiteLLM for multi-provider support.

    Supports OpenRouter, Anthropic, OpenAI, Gemini, MiniMax, and many other providers through
    a unified interface.  Provider-specific logic is driven by the registry
    (see providers/registry.py) — no if-elif chains needed here.
    """

    def __init__(
        self,
        api_key: str | None = None,
        api_base: str | None = None,
        default_model: str = "anthropic/claude-opus-4-5",
        extra_headers: dict[str, str] | None = None,
        provider_name: str | None = None,
        disable_auto_cache_control: bool = False,
        extra_body: dict[str, Any] | None = None,
        model_overrides: dict[str, dict[str, Any]] | None = None,
        *,
        unparsed_reasoning: bool | None = None,
    ):
        super().__init__(api_key, api_base)
        # None: derive from the resolved spec, as emits_unparsed_reasoning always
        # did. An explicit bool overrides that derivation outright -- for a
        # caller that already knows the answer and for which the spec would
        # guess wrong, e.g. a per-model routing endpoint built with
        # provider_name="custom" for its api_base/api_key shape alone, not
        # because the backend behind it is a self-hosted inference server.
        self._unparsed_reasoning = unparsed_reasoning
        self.default_model = default_model
        self.extra_headers = extra_headers or {}
        # When a TokenStrategy (e.g. CacheOptimizer) handles cache_control
        # placement upstream, turn this on so the provider doesn't also
        # stamp its own breakpoints on top.
        self.disable_auto_cache_control = disable_auto_cache_control
        # Provider-specific request body extras forwarded verbatim to LiteLLM.
        # Common use: OpenRouter routing affinity to keep prompt-cache hits warm,
        #   extra_body={"provider": {"order": ["Anthropic"], "allow_fallbacks": False}}
        self.extra_body = extra_body or {}
        # User-configured per-model parameter overrides; win over the registry's.
        self.model_overrides = model_overrides or {}

        # Detect gateway / local deployment.
        # provider_name (from config key) is the primary signal;
        # api_key / api_base are fallback for auto-detection.
        # Kept because the id alone cannot say where a request goes: a bare
        # `anthropic/claude-...` sent through this client reads as Anthropic's
        # wire, which is not the wire it will travel on.
        self._provider_name = provider_name or ""
        self._gateway = find_gateway(provider_name, api_key, api_base)
        if self._gateway and self._gateway.name == "openrouter":
            self.extra_headers = {**_OPENROUTER_ATTRIBUTION, **self.extra_headers}

        # Configure environment variables
        if api_key:
            self._setup_env(api_key, api_base, default_model)

        # Drop unsupported parameters for providers (e.g., gpt-5 rejects some params)
        litellm.drop_params = True

    def _setup_env(self, api_key: str, api_base: str | None, model: str) -> None:
        """Set environment variables based on detected provider."""
        spec = self._gateway or find_by_model(model)
        if not spec:
            # No spec: the key still reaches LiteLLM as an explicit api_key
            # kwarg on every call, so nothing needs to go into the environment.
            return
        if not spec.env_key:
            # OAuth/provider-only specs (for example: openai_codex)
            return

        # Gateway/local overrides existing env; standard provider doesn't
        if self._gateway:
            os.environ[spec.env_key] = api_key
        else:
            os.environ.setdefault(spec.env_key, api_key)

        # Resolve env_extras placeholders:
        #   {api_key}  → user's API key
        #   {api_base} → user's api_base, falling back to spec.default_api_base
        effective_base = api_base or spec.default_api_base
        for env_name, env_val in spec.env_extras:
            resolved = env_val.replace("{api_key}", api_key)
            resolved = resolved.replace("{api_base}", effective_base)
            os.environ.setdefault(env_name, resolved)

    def _strip_gateway_prefix(self, model: str) -> str:
        """Drop this gateway's own prefix, leaving the upstream vendor's id."""
        if not self._gateway:
            return model
        prefix = f"{self._gateway.model_prefix}/"
        return model[len(prefix) :] if model.startswith(prefix) else model

    def _resolve_model(self, model: str) -> str:
        """The id this request is sent under. See ``providers.wire``."""
        return wire_model(model, gateway=self._gateway)

    def wire_model_id(self, model: str) -> str:
        """See ``LLMProvider.wire_model_id``."""
        return self._resolve_model(model)

    def can_serve(self, model: str) -> bool:
        """See ``LLMProvider.can_serve``.

        A gateway instance answers for any model -- it is the one deciding
        which upstream vendor actually serves it, and its credentials are the
        gateway's own, not tied to one vendor.

        For a direct instance, this only vetoes the one case both sides are
        certain about: this instance's own provider_name resolves to a known,
        non-OAuth spec, the model resolves to a *different* known spec, and
        the two disagree -- that is one vendor's key answering for another
        vendor's model, rejected outright. Every other case is let through
        rather than guessed away here:
          - this instance's own identity does not resolve to a spec (empty
            provider_name, "auto", or a custom passthrough name LiteLLM
            recognizes natively but Raven has no ProviderSpec for, e.g.
            nebius/fireworks/together) -- there is nothing to compare against;
          - the resolved spec is OAuth-based (e.g. github_copilot): one OAuth
            grant can serve several upstream vendors, so a spec mismatch there
            says nothing about whether this instance can serve the model;
          - the model resolves to no spec at all (custom endpoints, bare ids
            only LiteLLM itself recognizes).
        In all of those, the model is not known to be wrong for this
        instance, so it fails loudly at the wire instead of being guessed
        away here.
        """
        if self._gateway is not None:
            return True
        mine = find_by_name(canonical_provider_name(self._provider_name))
        if mine is None or mine.is_oauth:
            return True
        theirs = find_by_model(model)
        if theirs is None:
            return True
        return theirs.name == mine.name

    def emits_unparsed_reasoning(self) -> bool:
        """See ``LLMProvider.emits_unparsed_reasoning``.

        ``self._unparsed_reasoning``, when set explicitly at construction, wins
        outright: it exists for a caller that already knows the answer and for
        which the spec-based guess below is wrong -- a per-model routing
        endpoint is built with ``provider_name="custom"`` for its api_base /
        api_key shape alone, not because the backend behind it is known to be a
        self-hosted inference server, so ``custom`` there would falsely claim
        every one of its responses.

        Otherwise, ``self._gateway`` already answers this for both shapes it
        can hold: a real network gateway (OpenRouter, AiHubMix) fronts one of
        the large hosted vendors below it, so a bare ``</think>`` in content
        is just content; the generic ``custom`` endpoint and a local spec
        (hosted_vllm, ollama_chat) *are* the self-hosted inference server this
        normalization exists for. When nothing was auto-detected, fall back to
        whatever spec ``provider_name`` resolves to.

        An identity that resolves to nothing answers False, the same reading
        ``can_serve`` settled on: an unresolved name says nothing about the
        backend, and several production constructors (the proactive planner,
        the evolver) build direct big-vendor connections with no
        ``provider_name`` at all -- guessing "self-hosted" there re-opens the
        false-positive cut on ordinary content this gate exists to close. A
        genuinely self-hosted backend is reached through ``custom`` or a
        local spec, which is where the parser-less sglang/vLLM shape comes
        from; a resolved direct big vendor (anthropic, openai, ...) never
        produces it behind its own API.
        """
        if self._unparsed_reasoning is not None:
            return self._unparsed_reasoning
        spec = self._gateway or find_by_name(canonical_provider_name(self._provider_name))
        return spec is not None and (spec.is_local or spec.name == "custom")

    def _supports_cache_control(self, model: str) -> bool:
        """Return True when this request may carry cache_control blocks.

        Decided by ``providers.prompt_cache``, which the token strategies ask too
        -- three copies of this question disagreed, and the one here could not
        have answered for the marks they place.

        The address falls back to the auto-detected gateway when no
        ``provider_name`` was given: several production constructors (the
        evolver's launch models, the sentinel planner) pass only an
        ``api_base``, and answering from the model id alone reads
        ``anthropic/claude-...`` as Anthropic's wire while the request actually
        travels through whatever gateway that base names -- a wire that may
        have nowhere honest to put the field.
        """
        from raven.providers.prompt_cache import accepts_cache_control

        addressed = self._provider_name or (self._gateway.name if self._gateway else "")
        return accepts_cache_control(model, addressed_to=addressed)

    def _apply_cache_control(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
        """Return copies of messages and tools with cache_control injected."""
        new_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                content = msg["content"]
                if isinstance(content, str):
                    new_content = [{"type": "text", "text": content, "cache_control": CACHE_CONTROL}]
                else:
                    new_content = list(content)
                    new_content[-1] = {**new_content[-1], "cache_control": CACHE_CONTROL}
                new_messages.append({**msg, "content": new_content})
            else:
                new_messages.append(msg)

        new_tools = tools
        if tools:
            new_tools = list(tools)
            new_tools[-1] = {**new_tools[-1], "cache_control": CACHE_CONTROL}

        return new_messages, new_tools

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
    def _extra_msg_keys(original_model: str, resolved_model: str) -> frozenset[str]:
        """Return provider-specific extra keys to preserve in request messages."""
        spec = find_by_model(original_model) or find_by_model(resolved_model)
        if (
            (spec and spec.name == "anthropic")
            or "claude" in original_model.lower()
            or resolved_model.startswith("anthropic/")
        ):
            return _ANTHROPIC_EXTRA_KEYS
        return frozenset()

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
        messages: list[dict[str, Any]], extra_keys: frozenset[str] = frozenset()
    ) -> list[dict[str, Any]]:
        """Strip non-standard keys and ensure assistant messages have a content key."""
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

            if "tool_call_id" in clean and clean["tool_call_id"]:
                clean["tool_call_id"] = map_id(clean["tool_call_id"])
        return sanitized

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

        if self._supports_cache_control(original_model):
            if not self.disable_auto_cache_control:
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
            "messages": self._sanitize_messages(self._sanitize_empty_content(messages), extra_keys=extra_msg_keys),
            "temperature": temperature,
            # Per-read httpx cap forwarded to the underlying client. This alone
            # cannot bound a backend that trickles bytes forever (the read timer
            # resets on every chunk), so the awaited call is also wrapped in an
            # asyncio.wait_for wall-clock cap below.
            "timeout": self.generation.timeout,
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

        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

        try:
            response = await asyncio.wait_for(acompletion(**kwargs), self.generation.timeout)
            return self._parse_response(response)
        except Exception as e:
            # Return error as content for graceful handling, but classify the
            # live exception here (status_code + type) before it's lost to a
            # string — the retry/fallback layer reads this verdict.
            classification = self.classify_error(e)
            head = self._provider_name or (self._gateway.name if self._gateway else None)
            return LLMResponse(
                content=format_llm_error(e, classification, provider=head),
                finish_reason="error",
                error_classification=classification,
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
    ) -> AsyncIterator[StreamDelta]:
        """Streaming counterpart to chat().

        Yields one StreamDelta per non-empty chunk. Signature matches chat()
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

        if self._supports_cache_control(original_model):
            if not self.disable_auto_cache_control:
                messages, tools = self._apply_cache_control(messages, tools)
        else:
            messages, tools = prompt_cache.strip(messages, tools)

        kwargs: dict[str, Any] = {
            "model": model,
            "messages": self._sanitize_messages(self._sanitize_empty_content(messages), extra_keys=extra_msg_keys),
            "temperature": temperature,
            "stream": True,
            # OpenAI-compatible providers only emit the trailing usage chunk
            # when usage is explicitly requested; without it the stream carries
            # no token counts and downstream cost / context tracking sees zero.
            "stream_options": {"include_usage": True},
            "timeout": self.generation.timeout,
        }
        if max_tokens is not None:
            kwargs["max_tokens"] = max(1, max_tokens)

        self._apply_model_overrides(model, kwargs)

        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.api_base:
            kwargs["api_base"] = self.api_base
        if self.extra_headers:
            kwargs["extra_headers"] = self.extra_headers
        if self.extra_body:
            _merge_extra_body(kwargs, self.extra_body)
        if reasoning_effort:
            kwargs["reasoning_effort"] = reasoning_effort
            kwargs["drop_params"] = True
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice or "auto"

        def _retry_without_breakpoints(exc: Exception) -> bool:
            """Learn the refusal and take the marks off, or say this is not one.

            The one retry this path takes. Restarting a *partially streamed* call
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

        async def _open():
            return (await asyncio.wait_for(acompletion(**kwargs), self.generation.timeout)).__aiter__()

        async def _close(target: Any) -> None:
            aclose = getattr(target, "aclose", None)
            if aclose is not None:
                await aclose()

        # Per-chunk idle cap: the timer resets on every chunk, so a long but
        # steadily-progressing generation is fine while a mid-stream stall (no
        # bytes for `timeout` seconds) raises TimeoutError instead of hanging.
        # Everything from the open onward sits inside the one try/finally, so the
        # underlying HTTP stream is closed deterministically on any exit -- a
        # first-chunk timeout included, which is the most likely one there is
        # (gateway queueing, cold start).
        # A chunk of None is a chunk, not the end of the stream. Pulling the
        # first one before the loop needs a value meaning "there was none", and
        # reusing None for it would let a provider that yields one truncate the
        # response silently -- which is not what the loop did before.
        done = object()

        stream: Any = None
        try:
            # The open and the first pull are one unit, and the `except` has to
            # cover both: an OpenAI-shaped route raises at the open, and a gateway
            # that defers the request until the first pull raises there instead.
            try:
                stream = await _open()
                first = await asyncio.wait_for(stream.__anext__(), self.generation.timeout)
            except StopAsyncIteration:
                first = done
            except Exception as exc:
                if not _retry_without_breakpoints(exc):
                    raise
                # The refused stream is finished with; closing it before opening
                # the replacement keeps at most one live at a time. It is None
                # when the open itself was what failed.
                await _close(stream)
                stream = await _open()
                try:
                    first = await asyncio.wait_for(stream.__anext__(), self.generation.timeout)
                except StopAsyncIteration:
                    first = done

            chunk = first
            while chunk is not done:
                delta = self._normalize_stream_chunk(chunk)
                if delta is not None:
                    yield delta
                try:
                    chunk = await asyncio.wait_for(stream.__anext__(), self.generation.timeout)
                except StopAsyncIteration:
                    break
        finally:
            await _close(stream)

    def _normalize_stream_chunk(self, chunk: Any) -> StreamDelta | None:
        """Normalize a raw provider chunk into a StreamDelta.

        Default: OpenAI shape — `chunk.choices[0].delta.content` (str | None),
        `delta.tool_calls` (list | None), and a final `chunk.usage` snapshot
        on the trailing chunk for some providers. Returns None when the chunk
        carries no content / tool_call / usage payload so callers can skip.

        Provider-specific shapes (e.g. Qwen dashscope) are decided at
        implementation time after a real-provider smoke test (per design.md
        §D4 + tasks.md T3.4). Add a hardcoded branch here keyed on
        `self._gateway` / `find_by_model(...).name` if/when needed.
        """
        try:
            choices = getattr(chunk, "choices", None)
            if not choices:
                return None
            delta_obj = getattr(choices[0], "delta", None)
            if delta_obj is None:
                return None
            content = getattr(delta_obj, "content", None)
            tool_calls = getattr(delta_obj, "tool_calls", None)
            usage = getattr(chunk, "usage", None)
            reasoning_content = getattr(delta_obj, "reasoning_content", None) or None
            # Upstream states why it stopped only on the terminal chunk, which
            # otherwise carries no payload at all. Dropping that chunk (as the
            # emptiness check below used to) discards the one signal that says
            # the response was cut off at the output ceiling rather than
            # finished -- the difference between "the model is done" and "the
            # model was interrupted mid-token".
            finish_reason = getattr(choices[0], "finish_reason", None) or None

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

            usage_dict: dict[str, Any] | None = None
            if usage is not None:
                try:
                    usage_dict = usage.model_dump()
                except AttributeError:
                    usage_dict = {
                        "prompt_tokens": getattr(usage, "prompt_tokens", None),
                        "completion_tokens": getattr(usage, "completion_tokens", None),
                        "total_tokens": getattr(usage, "total_tokens", None),
                    }

            if (
                content is None
                and tool_call_delta is None
                and usage_dict is None
                and reasoning_content is None
                and finish_reason is None
            ):
                return None

            return StreamDelta(
                content=content,
                tool_call_delta=tool_call_delta,
                usage=usage_dict,
                reasoning_content=reasoning_content,
                finish_reason=finish_reason,
            )
        except (AttributeError, IndexError):
            return None

    def _parse_response(self, response: Any) -> LLMResponse:
        """Parse LiteLLM response into our standard format."""
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
                    name=tc.function.name,
                    arguments=args,
                    provider_specific_fields=provider_specific_fields,
                    function_provider_specific_fields=function_provider_specific_fields,
                    run_meta=RunMeta(arguments_repaired=True) if repaired else None,
                )
            )

        usage = {}
        if hasattr(response, "usage") and response.usage:
            usage = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }
            # Cache token extraction. LiteLLM normalizes these across providers
            # in different shapes depending on where the response came from:
            #   - Anthropic native:  usage.cache_read_input_tokens / cache_creation_input_tokens
            #   - LiteLLM internal:  usage._cache_read_input_tokens / _cache_creation_input_tokens
            #   - OpenAI-style:      usage.prompt_tokens_details.cached_tokens (read only)
            #   - OpenRouter:        usage.prompt_tokens_details.cached_tokens
            #                        usage.prompt_tokens_details.cache_write_tokens
            details = getattr(response.usage, "prompt_tokens_details", None)
            cache_read = (
                getattr(response.usage, "cache_read_input_tokens", None)
                or getattr(response.usage, "_cache_read_input_tokens", None)
                or (getattr(details, "cached_tokens", None) if details else None)
                or 0
            )
            cache_write = (
                getattr(response.usage, "cache_creation_input_tokens", None)
                or getattr(response.usage, "_cache_creation_input_tokens", None)
                or (getattr(details, "cache_write_tokens", None) if details else None)
                or 0
            )
            if cache_read:
                usage["cache_read_input_tokens"] = int(cache_read)
            if cache_write:
                usage["cache_creation_input_tokens"] = int(cache_write)

        reasoning_content = getattr(message, "reasoning_content", None) or None
        thinking_blocks = getattr(message, "thinking_blocks", None) or None

        if not reasoning_content and isinstance(content, str) and self.emits_unparsed_reasoning():
            split_reasoning, content = split_orphan_think(content)
            reasoning_content = split_reasoning or reasoning_content

        return LLMResponse(
            content=content,
            tool_calls=tool_calls,
            finish_reason=finish_reason or "stop",
            usage=usage,
            reasoning_content=reasoning_content,
            thinking_blocks=thinking_blocks,
        )

    @property
    def provider_name(self) -> str:
        """The config section this provider was built for, or ``""``.

        Read by callers deciding whether the model string is the operator's
        own naming (a ``custom`` gateway serves whatever its endpoint calls
        the model) -- see ``capabilities._model_id_is_caller_chosen``.
        """
        return self._provider_name

    def get_default_model(self) -> str:
        """Get the default model."""
        return self.default_model
