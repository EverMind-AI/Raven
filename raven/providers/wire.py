"""Storage form to wire form: the one place a stored model id becomes a sent one.

A model id is written down in one shape and sent in another. `openrouter/x` is
stored with its gateway named so the picker and the router agree on who serves
it, but MiniMax's own client wants the prefix gone, LiteLLM wants the vendor it
routes on in front, and Azure wants the bare deployment name because the id
lands in a URL path.

That conversion used to be spelled at each client. The spellings drifted: the
standard path grew a canonicalizer for prefixes written in a former or
hyphenated spelling and the gateway path never got one, so a local deployment
addressed as "hosted-vllm/..." came out double-prefixed. Collapsing the two
here is what made that one fix rather than two, and it is fixed --
`tests/data/wire_model_baseline.json` records the single-prefix result.

So callers ask here rather than building the prefix themselves -- an invariant
test keeps `model_prefix` / `skip_prefixes` readable only by this module and the
two that decompose an id rather than build one.

The module owns both directions, because they are one contract seen from two
ends. Outbound is `wire_model`. Inbound -- what id to write down when a user
picks a model -- is `stored_model_id`, and identity between two written ids is
`merge_key`.

Inbound had the same history and a worse symptom. Two implementations decided
what to store, and they disagreed for most providers: picking a model in the
TUI wrote `glm-4.6` while picking the same one in the wizard wrote `zai/glm-4.6`.
Both landed in the same list, so the list held one model twice and removing
either spelling left the other -- deletion reported success and changed nothing.
Comparing by `merge_key` rather than by string is what makes that impossible.
"""

from __future__ import annotations

from raven.providers.registry import (
    ProviderSpec,
    find_by_model,
    normalize_provider_name,
    public_model_prefix,
    split_model_id,
)


def wire_model(model: str, *, spec: ProviderSpec | None = None, gateway: ProviderSpec | None = None) -> str:
    """The id this model is sent under, given who is about to send it.

    ``gateway`` is the gateway or local deployment the request goes through,
    already detected from the key and address by ``find_gateway``; when it is
    set it decides alone, because the prefix that matters is the one naming the
    gateway rather than the vendor behind it.

    ``spec`` is the provider whose client is calling. It selects a non-LiteLLM
    client's own convention; the LiteLLM path deliberately ignores it and asks
    the model id instead, since an id may name a provider other than the
    configured one and routing follows the id.
    """
    if gateway is not None:
        return _through_gateway(model, gateway)
    if spec is not None:
        if spec.client == "codex":
            return _without_own_prefix(model, spec)
        if spec.client == "azure":
            # The id lands in a URL path as a deployment name, so its provider
            # prefix has to come off -- left on, "azure_openai/" became a path
            # segment and the request went to a deployment that does not exist.
            # Only reached when no `deployment` is configured; that field is the
            # proper home for this, and this is the fallback for configs written
            # before it existed.
            return _without_own_prefix(model, spec)
    return _direct(model)


def _through_gateway(model: str, gateway: ProviderSpec) -> str:
    """Put the gateway's own prefix in front, replacing the vendor's if asked.

    ``model_prefix`` rather than the raw field: a gateway whose name is already
    LiteLLM's declares no driver, and reading the field would drop the prefix
    entirely -- which is how a gateway's key came to be posted to the vendor
    named in the model id.
    """
    prefix = gateway.model_prefix
    if gateway.strip_model_prefix:
        # One leading vendor segment, not everything but the last: a model id
        # may itself contain a slash ("openai/gpt-oss-120b" is Groq's own name
        # for it), and keeping only the tail truncated the id being served.
        _, model = split_model_id(model)
    # Canonicalize first, exactly as the direct path does. Comparing the raw
    # string instead did not recognize this provider's own name written in
    # another of its spellings, so "hosted-vllm/x" -- which is how a stored id
    # for a local deployment is written -- came out as
    # "hosted_vllm/hosted-vllm/x". The two branches answering one question
    # differently is what this module exists to end.
    model = _canonical_prefix(model, gateway, prefix)
    if prefix and not model.startswith(f"{prefix}/"):
        model = f"{prefix}/{model}"
    return model


def _direct(model: str) -> str:
    """Put the routing vendor's prefix in front, for LiteLLM to route on."""
    spec = find_by_model(model)
    prefix = spec.model_prefix if spec else ""
    if spec and prefix:
        model = _canonical_prefix(model, spec, prefix)
        if not any(model.startswith(s) for s in (*spec.skip_prefixes, f"{prefix}/")):
            model = f"{prefix}/{model}"
    return model


def _canonical_prefix(model: str, spec: ProviderSpec, canonical: str) -> str:
    """Rewrite a prefix written in a former or hyphenated spelling."""
    if "/" not in model:
        return model
    prefix, remainder = split_model_id(model)
    if prefix not in spec.route_names:
        return model
    return f"{canonical}/{remainder}"


def _without_own_prefix(model: str, spec: ProviderSpec) -> str:
    """Drop the provider's own name, which its client does not want on the id.

    Both spellings, because the stored id carries the public one
    ("openai-codex/") while a config or a command line may have written the
    field name ("openai_codex/").
    """
    for prefix in (f"{public_model_prefix(spec)}/", f"{spec.name}/"):
        if model.startswith(prefix):
            return model[len(prefix) :]
    return model


# ---------------------------------------------------------------------------
# Inbound: what to write down, and when two written ids are the same model
# ---------------------------------------------------------------------------


def stored_model_id(provider: str, model: str) -> str:
    """The canonical form to persist for a model chosen under ``provider``.

    Always names its provider. A bare id is claimed by keyword matching instead,
    which sends it wherever those rules land rather than to the section the user
    just configured: "gpt-5.6-sol" entered under a Codex section matches OpenAI's
    keywords and the request leaves for a provider that does not serve it.

    Three shapes arrive here and only one is prefixed blindly:

    * an id already carrying a name this provider answers to is rewritten to the
      canonical spelling, so a former name or a hyphenated one does not become a
      second entry for the same model;
    * an id carrying a prefix this provider declares it accepts is left alone --
      that is what `skip_prefixes` means, and a model reached through a gateway
      says so in its own id;
    * anything else is bare, and gets this provider's name in front.
    """
    from raven.providers.registry import canonical_provider_name, find_by_name, litellm_spelling

    if not model:
        return model

    provider = canonical_provider_name(provider)
    spec = find_by_name(provider)
    # LiteLLM's own spelling for a vendor Raven has no spec for: it is the prefix
    # LiteLLM routes on, and the underscored form is rejected outright.
    public = public_model_prefix(spec) if spec else litellm_spelling(provider)
    if not public:
        return model

    head, rest = split_model_id(model)
    if head and (head == normalize_provider_name(public) or (spec and head in spec.route_names)):
        return f"{public}/{rest}"
    if spec and any(model.startswith(skip) for skip in spec.skip_prefixes):
        return model
    return f"{public}/{model}"


def merge_key(provider: str, model: str) -> str:
    """Identity of a stored model id, for comparison and de-duplication.

    Two ids name the same model when they name the same provider and the same
    vendor id, whatever spelling either was written in. Comparing the strings
    instead is what let one model sit in a list twice under two spellings, with
    `remove` matching neither the one the user meant nor reporting that it had
    not.

    Takes the provider explicitly so an id written before ids carried one still
    matches its qualified form.
    """
    from raven.providers.registry import canonical_provider_name, find_by_name

    provider = canonical_provider_name(provider)
    spec = find_by_name(provider)
    head, rest = split_model_id(model or "")
    bare = rest if head and (spec and head in spec.route_names or head == normalize_provider_name(provider)) else model
    return f"{normalize_provider_name(provider)}::{(bare or '').lower()}"


def metadata_candidates(model: str) -> list[str]:
    """Which ids to ask LiteLLM's table about, best first.

    A provider reached by region or by subscription is filed in that table under
    the vendor's own spelling, and the registry says which
    ("minimax-global/MiniMax-M3" -> "minimax/MiniMax-M3"). Asking with the
    routing id instead missed every time, and each miss cost a second guess and
    the live catalogue fetch behind it.

    Everything else is asked about as it routes, and only then under an
    ``openrouter/`` alias: OpenRouter fronts other vendors and prices them under
    its own prefix, so the alias covers models LiteLLM lists nowhere else -- but
    it answers with OpenRouter's numbers, which are not what a user routing
    directly to the vendor pays. Asked alias-first, ``deepseek/deepseek-chat``
    reported a 65,536-token window at $0.14/M where the vendor's own row says
    131,072 at $0.28/M.
    """
    from raven.providers.registry import metadata_model_id

    filed_as = metadata_model_id(model)
    if filed_as:
        return [filed_as]

    if model.startswith("openrouter/"):
        return [model]

    return [model, f"openrouter/{model}"]
