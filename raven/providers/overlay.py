"""The user's own description of one model, read off a provider section."""

from __future__ import annotations

from typing import Any


def overlay_for(section: Any, provider: str, model: str) -> Any | None:
    """The overlay entry for (provider, model), keyed by identity, or None.

    ``section`` is either a parsed provider config (``model_overlay``) or the
    live config's raw section (``modelOverlay``); the two readers must not
    carry separate spellings of the same lookup.
    """
    from raven.providers.wire import merge_key

    if section is None:
        return None
    if isinstance(section, dict):
        overlay = section.get("modelOverlay") or section.get("model_overlay") or {}
    else:
        overlay = getattr(section, "model_overlay", None) or {}
    return overlay.get(merge_key(provider, model))


def declared_efforts(overlay: Any) -> tuple[str, ...]:
    """The reasoning-effort words an overlay states, from a model or a raw dict."""
    if overlay is None:
        return ()
    if isinstance(overlay, dict):
        values = overlay.get("reasoningEfforts") or overlay.get("reasoning_efforts") or ()
    else:
        values = getattr(overlay, "reasoning_efforts", ()) or ()
    return tuple(value for value in values if isinstance(value, str))
