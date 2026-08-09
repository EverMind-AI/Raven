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

The first is :func:`supports_vision`, answered per model from the gateway
catalog. Both have to be asked: a model that cannot see is not helped by a
transport that could have carried the picture, and a model that can see still
loses it over a transport that cannot.
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
# Route prefixes whose second segment is a name the user chose rather than a
# model id. LiteLLM's spelling for an Azure deployment; the registry has no
# route by this name, so nothing else recognizes it.
_DEPLOYMENT_NAME_PREFIXES = frozenset({"azure", "azure_ai", "azure_text"})

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


def _model_id_is_caller_chosen(model: str, provider: Any, spec: "ProviderSpec | None") -> bool:
    """Does this route's model string name a deployment rather than a model?

    Azure takes the name of a deployment the user created, and a local runtime
    takes whatever tag the user pulled or served under. Either can be spelled
    exactly like a vendor id it does not serve -- ``gpt-4`` is the deployment name
    Azure's own quickstarts use, and teams keep the name while repointing the
    deployment at a newer model -- so joining it against a vendor catalogue
    answers about somebody else's model. Only a *denial* does damage (a grant is
    what absence already gives), and a denial here is the silent failure this
    module exists to avoid, so the catalogue is not consulted for these at all.

    Asked three ways because Azure arrives three ways. Configured as a Raven
    provider it is served by ``AzureOpenAIProvider`` and the model string is a
    bare deployment name -- no prefix resolves ``find_by_model`` to the Azure
    spec, and ``gpt-4`` alone is indistinguishable from OpenAI's own id, so only
    the live provider instance knows. Routed through LiteLLM instead it carries
    LiteLLM's ``azure/`` prefix, which the registry does not answer to and which
    always introduces a deployment name. And a local runtime is named by a spec
    that says so.
    """
    from raven.providers.azure_openai_provider import AzureOpenAIProvider
    from raven.providers.registry import split_model_id

    if isinstance(provider, AzureOpenAIProvider):
        return True
    if split_model_id(model)[0] in _DEPLOYMENT_NAME_PREFIXES:
        return True
    if spec is None:
        return False
    return bool(spec.is_local) or spec.client == "azure"


def vision_verdict(
    model: str,
    spec: "ProviderSpec | None" = None,
    provider: Any = None,
) -> bool | None:
    """What is *known* about ``model`` seeing images: True, False, or unknown.

    ``None`` is the load-bearing case and the reason this sits under
    :func:`supports_vision` rather than inside it. It means no answer exists yet
    -- an unlisted model, a deployment name, or a catalog not warm -- which a
    caller must not memoize: caching the optimistic default that ``None`` becomes
    would freeze a cold-start guess for the life of the process and the warm
    behind it would fill a table nobody re-reads.
    """
    if spec is not None and spec.vision_override is not None:
        return spec.vision_override
    if _model_id_is_caller_chosen(model, provider, spec):
        return None

    # Imported inside the call: rates reaches back into this package's
    # registry, so a module-level import here would close the loop.
    from raven.providers.rates import openrouter_input_modalities, warm_catalog_in_background

    try:
        mods = openrouter_input_modalities(model)
    except Exception as e:
        # The catalog degrades rather than raising, but a capability probe must
        # never be the thing that fails a turn.
        logger.debug("vision_verdict: catalog lookup failed for {}: {}", model, e)
        return None
    if mods is None:
        warm_catalog_in_background()
        return None
    return "image" in mods


def supports_vision(
    model: str,
    spec: "ProviderSpec | None" = None,
    provider: Any = None,
) -> bool:
    """Whether ``model`` can see an image at all.

    The other half of this module's opening question, and the one that decides
    whether a picture is inlined into the user message or replaced by a note
    telling the model to read it another way.

    Answered from the gateway catalog Raven already fetches and caches for
    pricing (:func:`raven.providers.rates.openrouter_input_modalities`), which
    publishes ``input_modalities`` for every model it lists. That completeness is
    the reason it is the source rather than LiteLLM's price table: the table
    states ``supports_vision`` on under a third of its rows, and reading the
    silence on the other two thirds as a denial would take a picture that reaches
    Grok, Llama 4 and the Qwen-VL family today and replace it with prose.

    No entry at all means yes, which leaves the model exactly where it was before
    this function existed. Being wrong that way is loud -- the endpoint refuses
    the request and the turn fails. Being wrong the other way is silent: the
    picture never arrives and the model answers from the surrounding text as
    though it had seen one. There is no automatic recovery in either direction on
    the attachment path (``should_drop_tool_images`` rescues an image out of a
    *tool result*, not out of a user message), so the choice is between a visible
    failure and an invisible one. :attr:`ProviderSpec.vision_override` settles a
    model the catalog gets wrong or never lists.

    The catalog is read from cache only, never fetched here, so on a cold install
    the first answers are the optimistic default while a background warm fills
    it. The pricing path cannot be left to do that warming -- it reaches this
    catalog only for models LiteLLM's static table misses, which excludes every
    model Raven ships a default for.

    A caller that caches this answer wants :func:`vision_verdict` instead, which
    says whether there was an answer to cache.
    """
    return vision_verdict(model, spec, provider) is not False


def _gateway_route(model: str) -> str:
    """The gateway's own model id, with the gateway prefix stripped.

    ``openrouter/google/gemini-2.5-flash`` -> ``google/gemini-2.5-flash``. A bare
    ``openrouter/some-model`` yields the model name, which matches no prefix and
    takes the placeholder path.
    """
    _, _, route = model.partition("/")
    return route.lower()


def image_placeholder_text(
    blocks: list[dict[str, Any]],
    *,
    blind: bool = False,
    describe_tool: str | None = None,
) -> str:
    """Text standing in for images the model will not receive.

    Keeps the tool's own text (it already names the file and its geometry) and
    appends a line per dropped image so the model knows a picture exists and
    where it came from, rather than silently seeing nothing.

    Two different reasons, and the model has to be told them apart. By default
    the transport cannot put an image in a tool result, so the picture follows
    in a user message and the note says so. With ``blind=True`` the model has
    no vision at all: nothing follows, and saying it did would leave the model
    waiting for a picture that never arrives -- so the note points at the tool
    that can read the image for it instead.

    ``describe_tool`` names that tool, or is ``None`` when the caller has none to
    offer: it is contributed by the EverOS plugin and absent on a default
    install, and naming a tool the model was never given is an instruction it
    cannot follow. The note then says only that a picture exists.
    """
    texts = [b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text"]
    images = sum(1 for b in blocks if isinstance(b, dict) and b.get("type") == "image_url")
    body = "\n".join(t for t in texts if t)
    if images:
        noun = "image" if images == 1 else "images"
        if blind:
            hint = f"; use the {describe_tool} tool to read the file" if describe_tool else ""
            body += f"\n[{images} {noun} not shown — you cannot see images directly{hint}]"
        else:
            body += (
                f"\n[{images} {noun} attached to the following message — "
                "this endpoint cannot carry images in a tool result]"
            )
    return body.strip()


#: Request-body extras a provider needs for particular models, declared rather
#: than branched on at the point a provider is built.
#:
#: Each entry is (provider, substring of the model id, body). A substring rather
#: than an id because a vendor's quirk covers a family; this one is deliberately
#: broad -- every qwen model behind OpenRouter, which is what the branch it
#: replaces matched too.
_WIRE_OVERRIDES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    # OpenRouter routes qwen3.x through hosts that default to reasoning mode
    # (AtlasCloud among them): every completion emits ~800 chain-of-thought
    # tokens and takes ~30s wall, which is fatal interactively and for volume
    # benchmark runs. The flag is OpenRouter's own and rides in extra_body.
    ("openrouter", "qwen", {"reasoning": {"enabled": False}}),
)


def wire_overrides(provider: str | None, model: str | None) -> dict[str, Any]:
    """Extras to send in the request body for this provider and model.

    Lived as an ``if provider_name == ... and ... in model`` inside the factory,
    because a fact about one model family behind one gateway had nowhere else to
    go. Declared here it sits with the other per-model facts, and a second one
    does not mean a second branch in provider construction.
    """
    from raven.providers.registry import normalize_provider_name

    if not provider or not model:
        return {}

    name = normalize_provider_name(provider)
    lowered = model.lower()
    out: dict[str, Any] = {}
    for owner, needle, body in _WIRE_OVERRIDES:
        if normalize_provider_name(owner) == name and needle in lowered:
            out.update(body)
    return out
