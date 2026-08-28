"""The provider paper: the chat surface and the response shapes.

The definitions moved to :mod:`raven.contracts.llm_provider` (the papers own the
shapes); this module re-exports them so existing import paths keep resolving.
"""

from raven.contracts.llm_provider import (  # noqa: F401
    ErrorClassification,
    GenerationSettings,
    LLMProvider,
    LLMResponse,
    ProviderHTTPError,
    RunMeta,
    StreamDelta,
    ToolCallRequest,
    TruncationInfo,
    _strip_json_error_body,
    format_llm_error,
    parse_llm_error,
    send_max_tokens,
)

__all__ = ["ErrorClassification", "GenerationSettings", "LLMProvider", "LLMResponse", "ProviderHTTPError", "RunMeta", "StreamDelta", "ToolCallRequest", "TruncationInfo", "format_llm_error", "parse_llm_error", "send_max_tokens"]
