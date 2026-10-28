"""What a LiteLLM-routed provider answers without calling LiteLLM.

A provider that sends through LiteLLM answers a handful of questions before and
after it sends: which id goes on the wire, whether the request may carry cache
breakpoints, whether it may end on an assistant message, whether the backend
leaks bare think tags -- and where the cache breakpoints go when it may. Every
one of those is decided from the provider's own construction arguments and
Raven's registry, never by LiteLLM itself, so it lives here, apart from the
module that imports LiteLLM.

Two classes stand on it: :class:`raven.providers.litellm_provider.LiteLLMProvider`,
which sends, and :class:`raven.providers.relay_provider.RelayProvider`, which asks the host
that launched this process to send. Sharing the base is what keeps a relayed
agent's answers identical to a direct one's: there is one copy of each rule.
"""

import os
import uuid
from typing import Any

from raven.providers import prompt_cache
from raven.providers.base import LLMProvider
from raven.providers.registry import (
    canonical_provider_name,
    find_by_model,
    find_by_name,
    find_gateway,
)
from raven.providers.wire import wire_model

_ANTHROPIC_EXTRA_KEYS = frozenset({"thinking_blocks"})

# LiteLLM defaults to X-Title="liteLLM" / HTTP-Referer="https://litellm.ai" for OpenRouter
# requests, which would credit traffic to liteLLM instead of Raven on openrouter.ai/apps.
# Explicit headers here override those defaults; user-supplied extra_headers win over these.
_OPENROUTER_ATTRIBUTION: dict[str, str] = {
    "HTTP-Referer": "https://raven.evermind.ai",
    "X-Title": "Raven Agent",
    "X-OpenRouter-Title": "Raven Agent",
    "X-OpenRouter-Categories": "cli-agent,personal-agent",
}


def session_affinity_headers() -> dict[str, str]:
    """Headers pinning one caller to one backend replica.

    Self-hosted OpenAI-compatible backends (vLLM and friends) route by this
    header, so a stable value per provider instance keeps prefix-cache hits warm.
    """
    return {"x-session-affinity": uuid.uuid4().hex}


class LiteLLMShape(LLMProvider):
    """The construction and the answers of a LiteLLM-routed provider; the sending is a subclass's.

    Supports OpenRouter, Anthropic, OpenAI, Gemini, MiniMax, and many other providers through
    a unified interface.  Provider-specific logic is driven by the registry
    (see providers/registry.py) -- no if-elif chains needed here.
    """

    api_protocol = "chat"

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
        tls_verify: bool = True,
        *,
        unparsed_reasoning: bool | None = None,
        export_env: bool = True,
    ):
        addressed_spec = find_by_name(provider_name) or find_by_model(default_model)
        if api_base and addressed_spec and addressed_spec.strip_api_base_trailing_slash:
            api_base = api_base.rstrip("/")
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
        # A section that opted out of certificate verification has its calls sent
        # through a client built with verification off (``LiteLLMProvider``);
        # True, the default, lets LiteLLM build its own verifying client.
        self.tls_verify = tls_verify

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

        # A provider built in one process for another (the LLM relay's host)
        # leaves the environment alone: every call passes the key explicitly,
        # and the variables would carry one agent's key into the host's own
        # providers and into every process the host starts after.
        if api_key and export_env:
            self._setup_env(api_key, api_base, default_model)

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

    def supports_prompt_caching(self, model: str) -> bool:
        """See ``LLMProvider.supports_prompt_caching``."""
        return self._supports_cache_control(model)

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
        """Return copies of messages and tools with cache_control injected.

        Two breakpoints land on the system message when it declares a stable
        prefix (``prompt_cache.STABLE_PREFIX_KEY``), one when it does not.

        The second one is the one that survives a turn. A breakpoint keys its
        cache on every block up to and including itself, and the tail of this
        message -- the memory recall, the skill router's hits, the Curator's
        working state -- is rebuilt from whatever the user just said. So the
        end-of-message breakpoint changes key on
        every new turn and re-bills the identity and bootstrap text in front of
        it that never changed at all. Splitting at the boundary gives that head
        a key of its own.

        The end-of-message one is kept beside it rather than moved, because it
        is what holds the whole message across the iterations *within* a turn:
        the assembler builds this message once per turn and the tool loop only
        appends after it.
        """
        new_messages = []
        for msg in messages:
            if msg.get("role") == "system":
                content = msg["content"]
                blocks = prompt_cache.split_stable_prefix(content, int(msg.get(prompt_cache.STABLE_PREFIX_KEY) or 0))
                if blocks is not None:
                    blocks[0] = {**blocks[0], "cache_control": prompt_cache.cache_control()}
                    new_content = blocks
                elif isinstance(content, str):
                    new_content = [{"type": "text", "text": content}]
                else:
                    new_content = list(content)
                new_content[-1] = {**new_content[-1], "cache_control": prompt_cache.cache_control()}
                new_messages.append({**msg, "content": new_content})
            else:
                new_messages.append(msg)

        new_tools = tools
        if tools:
            new_tools = list(tools)
            new_tools[-1] = {**new_tools[-1], "cache_control": prompt_cache.cache_control()}

        return new_messages, new_tools

    @staticmethod
    def _is_anthropic_family(original_model: str, resolved_model: str) -> bool:
        """Whether this request is bound for an Anthropic model, by any spelling.

        One rule for the two decisions that must agree: whether
        ``thinking_blocks`` ride on the wire (``_extra_msg_keys``) and whether
        the request may end on an assistant message
        (``supports_assistant_prefill``). The request Anthropic rejects is
        exactly those blocks plus a trailing assistant message, so deciding the
        two separately would let them drift apart.
        """
        spec = find_by_model(original_model) or find_by_model(resolved_model)
        return bool(
            (spec and spec.name == "anthropic")
            or "claude" in original_model.lower()
            or resolved_model.startswith("anthropic/")
        )

    @staticmethod
    def _extra_msg_keys(original_model: str, resolved_model: str) -> frozenset[str]:
        """Return provider-specific extra keys to preserve in request messages."""
        if LiteLLMShape._is_anthropic_family(original_model, resolved_model):
            return _ANTHROPIC_EXTRA_KEYS
        return frozenset()

    def supports_assistant_prefill(self, model: str | None = None) -> bool:
        """Anthropic rejects a trailing assistant message while thinking is on.

        See ``LLMProvider.supports_assistant_prefill``. The family test is the
        one ``_extra_msg_keys`` applies, on purpose.
        """
        original = model or self.default_model
        return not self._is_anthropic_family(original, self._resolve_model(original))

    @staticmethod
    def _is_deepseek_family(model: str) -> bool:
        """Whether a Wire Model names a DeepSeek model, whichever provider serves it."""
        return model.rsplit("/", 1)[-1].lower().startswith("deepseek")

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
