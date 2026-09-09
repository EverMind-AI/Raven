"""Which provider ``agents.defaults.provider`` should name after a model change.

The pin overrides what a model id says, so a stale one silently sends the new
model's request to the old vendor -- with the old vendor's key. The rule lives
here so the TUI picker and the CLI answer the same way, rather than the CLI
telling the user to edit the field by hand.

The four cases, and why each is what it is:

======================  ==========================================================
a provider was named    that provider. The picker always sends one, and a
                        ``--provider`` flag is the user saying it outright.
the id names a vendor    that vendor's spec. The id is evidence and it is
we carry a spec for      unambiguous.
the id is prefixed but   ``auto``. Keeping the old pin would hand its key to a
we have no spec for it   vendor it does not belong to; auto-detection is the
                        honest answer for "somebody, not necessarily who you had".
a bare id                ask the pinned provider whether it serves this model,
                        and keep the pin only if it does -- see
                        ``resolve_bare_against_pin``.
======================  ==========================================================
"""

from __future__ import annotations

AUTO = "auto"


def resolve(model: str, *, provider: str = "", pinned: str = "") -> str | None:
    """The provider to pin for ``model``, or None when nothing can be told.

    ``provider`` is an explicitly chosen one (a picker selection, a ``--provider``
    flag); ``pinned`` is what ``agents.defaults.provider`` currently holds.
    None means the caller must ask rather than guess -- there is no answer that
    does not risk sending one vendor's key to another.
    """
    from raven.providers.registry import find_by_model

    if provider:
        return provider

    spec = find_by_model(model)
    if spec is not None:
        # Only if that vendor is actually configured. A pin is consulted before
        # anything else and answers with that vendor's section whether or not it
        # holds credentials, so pinning an unconfigured one means every request
        # fails on a missing key -- never reaching the fallback that lets a
        # gateway serve a model whose id names the vendor behind it. `auto` is
        # not the weaker answer here; it is the one that reaches the gateway.
        return spec.name if _is_configured(spec.name) else AUTO
    if "/" in model:
        # A prefixed id whose vendor has no spec of ours ("mistral/..."): keeping
        # the previously forced provider would send that provider's key to this
        # other vendor, so hand routing back to auto-detection.
        return AUTO
    return resolve_bare_against_pin(model, pinned=pinned)


def _is_configured(provider: str) -> bool:
    """Does this provider have a section holding usable credentials?

    Takes a registered name -- the only caller passes a spec's. An unregistered
    one raises out of ``get_provider_config`` rather than answering "no", which
    for a misspelling is the difference between a loud failure and quietly
    routing somewhere else.
    """
    from raven.config.update_providers import get_provider_config
    from raven.providers.auth import credential_status

    section = get_provider_config(provider, redact_secrets=False)
    return credential_status(provider, section, include_external=True).ok


def resolve_bare_against_pin(model: str, *, pinned: str) -> str | None:
    """Who serves this bare id, when its own text names nobody?

    A bare id that matches no provider's keywords is what a vendor Raven holds no
    spec for looks like, and the picker never produces one -- it always sends the
    provider alongside. So this is the hand-typed path, where the only other
    evidence is the provider currently pinned.

    Rather than guess, ask whether that provider serves the model: its own curated
    list first, then the catalogue. If it does, the pin was right and stays. A local
    deployment stays too without asking -- its server names whatever models it
    likes, and there is no key to mis-route.

    With no such evidence the pin is not kept: it would send one vendor's key to
    another, the mis-routing the prefix rules exist to prevent. Returning None
    leaves the caller to say so rather than pick a vendor on the user's behalf.
    """
    from raven.config.update_providers import get_provider_config
    from raven.providers.common_models import common_models_for, litellm_models_for
    from raven.providers.registry import find_by_name, split_model_id

    if not pinned or pinned == AUTO:
        return AUTO

    spec = find_by_name(pinned)
    if spec is not None and spec.is_local:
        return pinned

    try:
        configured = get_provider_config(pinned, redact_secrets=True).get("models") or []
    except KeyError:
        configured = []
    for candidate in (*configured, *common_models_for(pinned), *litellm_models_for(pinned)):
        # Stripping the prefix covers every spelling the sources use: the
        # catalogue keys ids the way LiteLLM spells the vendor, a hand-added one
        # sits in the provider's list bare.
        _, bare = split_model_id(candidate)
        if bare == model or candidate == model:
            return pinned
    return None
