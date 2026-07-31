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
#
# Absences here mean unprobed, not known-bad. ``gemini`` in particular: LiteLLM
# routes it and the listed ``vertex_ai`` through the same transform
# (vertex_ai/gemini/transformation.py -> convert_to_gemini_tool_call_result,
# which accepts "gemini" as a target and extracts image parts into inline_data),
# so it very likely works -- but nobody here has a direct Gemini key to prove it,
# and the rule this list holds to is "only what was measured". The result is that
# gemini/* takes the placeholder path while openrouter/google/gemini-* does not,
# which is a gap in coverage rather than a claim about the transport.
IMAGE_TOOL_RESULT_TARGETS = frozenset({"anthropic", "vertex_ai", "bedrock"})

# A gateway hands the request to one of several serving hosts, so what matters
# is the model family, not the creator segment of the model string. Measured
# against a live OpenRouter key on 2026-07-31 by sending a blue test image inside
# a role="tool" message and requiring the model to name its colour -- a 200 alone
# proves nothing. Each host pinned with provider.only + allow_fallbacks=false:
#
#   anthropic/claude-sonnet-4.5   Anthropic  blue | Bedrock  blue
#                                 Azure      blue | Google   blue   -> 4/4 ok
#   google/gemini-2.5-flash       Google     blue | AI Studio blue   -> 2/2 ok
#   google/gemma-3-27b-it         Parasail   blue | Nebius    blue
#                                 DeepInfra  422  | Novita    422    -> 2 of 4 fail
#   openai/gpt-4o                 (400, refuses: "Image URLs are only allowed
#                                  for messages with role 'user'")
#   openai/gpt-4.1-mini           (200, answered "red" -- silently dropped)
#
# Two rows drive the shape of this list. gemma is why the key is a family prefix
# and not the creator segment: it is served only by third-party
# OpenAI-compatible hosts, half of which reject the list content, and OpenRouter
# picks the host per request -- so a creator-level "google" entry buys
# intermittent failure, which is harder to diagnose than consistent failure.
# gpt-4.1-mini is why the list stays a whitelist of what was measured rather than
# a blacklist of what is known to fail: it drops the image and confabulates an
# answer, and the same image in a user message gets a correct one from it, so the
# model is not blind -- the picture is discarded in transit. A refusal is
# recoverable (ErrorClassification's should_drop_tool_images retries on the
# placeholder path); a silent drop is undetectable by any mechanism.
GATEWAY_TARGETS = frozenset({"openrouter"})
GATEWAY_IMAGE_TOOL_RESULT_PREFIXES = ("anthropic/claude-", "google/gemini-")


def supports_image_tool_result(provider: Any, model: str, spec: "ProviderSpec | None" = None) -> bool:
    """Whether a tool result sent to ``provider``/``model`` may carry an image.

    Fail-safe: anything not positively known to work returns False, which costs
    an extra message on the fallback path but never loses the image. A custom
    endpoint is indistinguishable from OpenAI here (it is routed with LiteLLM's
    ``openai/`` prefix), so it needs
    :attr:`ProviderSpec.image_tool_result_override` to opt in.

    Note this answers a *transport* question only. Whether the model can see an
    image at all is separate, and a gateway backend that cannot will reject the
    request rather than pretend.
    """
    if spec is not None and spec.image_tool_result_override is not None:
        return spec.image_tool_result_override

    from raven.providers.openai_codex_provider import OpenAICodexProvider

    if isinstance(provider, OpenAICodexProvider):
        # Responses API: function_call_output.output accepts a content array.
        # Confirmed against the generated spec types -- FunctionCallOutput.output
        # is Union[str, ResponseFunctionCallOutputItemListParam], and that list
        # admits ResponseInputImageContentParam.
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

    if target in GATEWAY_TARGETS:
        return _gateway_route(model).startswith(GATEWAY_IMAGE_TOOL_RESULT_PREFIXES)
    return target in IMAGE_TOOL_RESULT_TARGETS


def _gateway_route(model: str) -> str:
    """The gateway's own model id, with the gateway prefix stripped.

    ``openrouter/google/gemini-2.5-flash`` -> ``google/gemini-2.5-flash``. A bare
    ``openrouter/some-model`` yields the model name, which matches no prefix and
    takes the placeholder path.
    """
    _, _, route = model.partition("/")
    return route.lower()


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
