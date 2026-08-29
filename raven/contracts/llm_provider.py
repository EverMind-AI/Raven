"""The provider paper: response shapes and the interface the harness calls.

A provider hands the loop an :class:`LLMResponse` (or :class:`StreamDelta`
stream) and answers a few capability questions; a failed call is described by
an :class:`ErrorClassification`. The machinery that produces those -- retry,
sanitizing, error classification, tracing, the error-string helpers -- lives
in :mod:`raven.providers.base`, which every adapter subclasses. A paper
describes; it does not do.
"""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any


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


class ProviderHTTPError(RuntimeError):
    """Carries a real HTTP status past the point where a provider renders its
    non-200 response into a string.

    ``classify_error`` reads a status code off a live exception; a provider
    that speaks HTTP directly (azure, codex) has one on the response but loses
    it the moment the error becomes ``str`` content -- raising or classifying
    through this keeps the status attached, instead of regex-guessing it back
    out of the rendered text.
    """

    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class TruncationInfo:
    """Where the output stopped, on a call that never finished arriving."""

    at_tokens: int | None = None


@dataclass(frozen=True)
class RunMeta:
    """What happened around a call, as opposed to what the call asks for.

    Kept apart from ``arguments`` because the two travel differently: anything
    inside that dict is serialized into the assistant message by
    ``providers.tool_calls.openai_tool_call``, and the loop does that before the registry sees
    the call -- so a flag stored there is already fixed into the conversation
    history by the time anyone strips it, and the model reads back a field it
    never wrote.

    An empty instance means "nothing worth noting", which is the normal turn.

    The two fields sit at different layers on purpose. ``arguments_repaired``
    is an observation the provider can make -- this call's JSON had to be
    repaired to parse. ``truncation`` is the loop's conclusion drawn from it
    plus the response-level signals. Keeping the observation separate is what
    lets a single decision point serve both response paths: the streaming one
    assembles its tool calls in the loop, where a provider has nothing to
    attach a conclusion to.

    No ``__bool__``: it would have to pick one field to mean "non-empty", and
    every later field would silently fall outside it.
    """

    truncation: TruncationInfo | None = None
    arguments_repaired: bool = False
    #: This call was the last one of its turn. Only ever set alongside
    #: ``arguments_repaired``, because that is the only place it means
    #: anything: generation is sequential, so nothing arrives after a cut, and
    #: a repair with no calls after it is the shape a cut leaves. Recorded as
    #: the position it is rather than as a verdict -- whether the turn ran out
    #: of room is a reading of these two facts, and it belongs in the sentence
    #: the model reads, not in the record.
    last_of_turn: bool = False


@dataclass
class ToolCallRequest:
    """A tool call request from the LLM."""

    id: str
    name: str
    arguments: dict[str, Any]
    provider_specific_fields: dict[str, Any] | None = None
    function_provider_specific_fields: dict[str, Any] | None = None
    run_meta: RunMeta | None = None


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
    # Generation stopped at the output ceiling rather than because the model
    # was done. Distinct from finish_reason: upstream does not always say so
    # (some backends report "stop" on a truncated response), and a tool call
    # whose arguments were cut mid-JSON is truncated no matter what the
    # backend claims.
    truncated: bool = False
    # The ceiling that produced it, for the message shown to the model.
    max_tokens: int | None = None
    # How long this call spent thinking: the first reasoning delta to the first
    # non-reasoning output. Only a streamed call can know it -- a single-shot
    # chat() sees one arrival time for the whole response -- so None means
    # "not measured", never "instant".
    reasoning_ms: int | None = None

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
    #: ``None`` means "no opinion" -- the ceiling is resolved from the model's
    #: own metadata at request time. A number pins it, which is what an
    #: explicit ``chat(max_tokens=...)`` at a call site wants.
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    timeout: float = 600.0


class LLMProvider(ABC):
    """The provider interface: what the harness may call on any provider.

    The shapes above are its vocabulary; this class declares the calls. The
    machinery every adapter shares -- request sanitizing, error classification,
    retry, tracing -- is :class:`raven.providers.base.LLMProvider`, the base
    adapters actually subclass; this paper only says what a provider answers.
    """

    _SENTINEL = object()

    def __init__(self, api_key: str | None = None, api_base: str | None = None):
        self.api_key = api_key
        self.api_base = api_base
        self.generation: GenerationSettings = GenerationSettings()

    @abstractmethod
    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = _SENTINEL,
        temperature: object = _SENTINEL,
        reasoning_effort: object = _SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamDelta]:
        """Stream ``model``'s reply as deltas; the last one carries the usage and finish reason.
        A provider without a streaming wire may emit the whole reply as one terminal delta."""
        ...

    @classmethod
    @abstractmethod
    def classify_error(
        cls,
        exc: BaseException | None = None,
        content: str | None = None,
    ) -> ErrorClassification:
        """Classify a failed call by exception type, HTTP status and message into the
        verdict the harness recovers on: retryable, context overflow, tool-image rejection ..."""
        ...

    @abstractmethod
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
        """``chat`` with the provider's own retry policy applied, and the tool-image
        rejection recovery; the one call the harness makes per turn."""
        ...

    @abstractmethod
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

    def can_serve(self, model: str) -> bool:
        """Whether this provider instance's credentials and wire can serve this model.

        Default True: the base class knows nothing about routing, and a wrong
        guess must fail loudly at the wire rather than silently skip a hop.
        """
        return True

    def wire_model_id(self, model: str) -> str:
        """The id this provider will actually send. See ``providers.wire``.

        Anyone sizing a *request* has to ask under this rather than under the
        stored name. The catalogue files a gateway spelling as its own row with
        its own numbers -- ``openai/gpt-4o`` answers 16384 where
        ``openrouter/openai/gpt-4o`` answers 4096 -- so the two are different
        questions, and only this one is about the request that went out.

        Default identity: a provider that sends the stored id unchanged has
        nothing to translate.
        """
        return model

    def emits_unparsed_reasoning(self) -> bool:
        """Whether this provider's backend may leak bare think tags into content.

        Only an inference server run without its reasoning parser produces the
        orphan-closing-tag shape; everyone else's `</think>` in content is just
        text. Default False: normalization is opt-in per provider shape.
        """
        return False

    def supports_prompt_caching(self, model: str) -> bool:
        """Whether a request this provider sends for ``model`` may carry
        ``cache_control`` breakpoints.

        The public form of the question ``providers.prompt_cache`` answers, so a
        token strategy can put it to the object that will actually send the
        request instead of guessing from the id -- an id names a vendor, not the
        wire it travels on, and only the provider knows the second.

        Default False: the base class knows no dialect, and a provider that can
        carry the field says so. Answering from the model id here would put the
        guess back into the one place that has the wire in hand.
        """
        return False

    @abstractmethod
    def get_default_model(self) -> str:
        """Get the default model for this provider."""
        pass


__tier__ = "contract"
__all__ = [
    "ErrorClassification",
    "GenerationSettings",
    "LLMProvider",
    "LLMResponse",
    "ProviderHTTPError",
    "RunMeta",
    "StreamDelta",
    "ToolCallRequest",
    "TruncationInfo",
]
