"""The user's own description of one model, read off a provider section."""

from __future__ import annotations

from typing import Any


def overlay_for(section: Any, provider: str, model: str) -> Any | None:
    """The overlay entry for (provider, model), keyed by identity, or None."""
    from raven.providers.wire import merge_key

    overlay = getattr(section, "model_overlay", None) or {}
    return overlay.get(merge_key(provider, model))
