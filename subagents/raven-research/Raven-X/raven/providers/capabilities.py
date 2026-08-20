"""Declared request-body extras for particular provider/model pairs.

One table and its reader. Upstream's module of this name also answers vision
and image-in-tool-result probes; those are deliberately not ported (see
docs/UPSTREAM_SYNC.md) -- the file keeps the upstream name so the next sync
diffs cleanly.
"""

from __future__ import annotations

from typing import Any

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
