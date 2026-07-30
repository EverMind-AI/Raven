"""Runtime capability probes for provider/model pairs.

Two questions that look alike but are not:

* *Can the model see images at all?* — a model property.
* *Can an image ride inside a tool result?* — a wire-format property. A
  vision model reached over OpenAI's Chat Completions cannot: that API types
  ``role:"tool"`` content as ``string | ChatCompletionContentPartText[]``, so an
  image block has nowhere to go. Anthropic's ``tool_result.content`` accepts
  ``image``, and the Responses API accepts an array on
  ``function_call_output.output``.

The second question decides whether ``read_file`` hands the model a picture or a
text placeholder plus a follow-up attachment, so it is answered per target, not
per model.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from raven.providers.registry import ProviderSpec

# Targets measured to translate an OpenAI-shaped image block inside a tool
# message into their native tool-result form. Deliberately a whitelist of what
# was actually tested, not a blacklist of what is known to fail: an unknown
# target that silently drops the image is worse than one that takes the
# placeholder path and works.
IMAGE_TOOL_RESULT_TARGETS = frozenset({"anthropic", "vertex_ai", "bedrock"})


def supports_image_tool_result(provider: Any, model: str, spec: "ProviderSpec | None" = None) -> bool:
    """Whether a tool result sent to ``provider``/``model`` may carry an image.

    Fail-safe: anything not positively known to work returns False, which costs
    an extra message on the fallback path but never loses the image. Notably
    ``openrouter`` returns False even when it is fronting Claude — the proxy
    layer's tool-result translation has not been verified — and any custom
    endpoint is indistinguishable from OpenAI here (it is routed with LiteLLM's
    ``openai/`` prefix), so both need
    :attr:`ProviderSpec.image_tool_result_override` to opt in.
    """
    if spec is not None and spec.image_tool_result_override is not None:
        return spec.image_tool_result_override

    from raven.providers.openai_codex_provider import OpenAICodexProvider

    if isinstance(provider, OpenAICodexProvider):
        # Responses API: function_call_output.output accepts a content array.
        return True

    from raven.providers.litellm_provider import LiteLLMProvider

    if not isinstance(provider, LiteLLMProvider):
        # Azure direct and any other bespoke transport speak Chat Completions.
        return False

    try:
        import litellm

        _, target, _, _ = litellm.get_llm_provider(model=model)
    except Exception as e:
        logger.debug("supports_image_tool_result: cannot resolve target for {}: {}", model, e)
        return False
    return target in IMAGE_TOOL_RESULT_TARGETS


def image_placeholder_text(blocks: list[dict[str, Any]]) -> str:
    """Text standing in for images the current transport cannot carry.

    Keeps the tool's own text (it already names the file and its geometry) and
    appends a line per dropped image so the model knows a picture exists and
    where it came from, rather than silently seeing nothing.
    """
    texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    images = sum(1 for b in blocks if isinstance(b, dict) and b.get("type") == "image_url")
    body = "\n".join(t for t in texts if t)
    if images:
        noun = "image" if images == 1 else "images"
        body += f"\n[{images} {noun} attached to the following message — this endpoint cannot carry images in a tool result]"
    return body.strip()
