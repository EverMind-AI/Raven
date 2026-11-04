"""The user's own description of one model, read off a provider section."""

from __future__ import annotations

from typing import Any


def overlay_for(section: Any, provider: str, model: str) -> Any | None:
    """The overlay entry for (provider, model), keyed by identity, or None.

    ``section`` is either a parsed provider config (``model_overlay``) or the
    live config's raw section (``modelOverlay``); the two readers must not
    carry separate spellings of the same lookup. A file can hold two spellings
    of one model; the last one wins, the precedence the model-label reader and
    the overlay writer already use.
    """
    from raven.providers.wire import merge_key

    if section is None:
        return None
    if isinstance(section, dict):
        overlay = section.get("modelOverlay") or section.get("model_overlay") or {}
    else:
        overlay = getattr(section, "model_overlay", None) or {}
    target = merge_key(provider, model)
    found = None
    for stored, value in overlay.items():
        if merge_key(provider, stored) == target:
            found = value
    return found
