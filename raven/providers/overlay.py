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


def declared_for(providers: Any, provider: str, model: str) -> tuple[str, ...]:
    """The effort words a model's overlay states, out of raw or parsed providers.

    The turn holds the live file's raw ``providers`` subtree and the RPC holds
    the parsed ``ProvidersConfig``; resolving the section here keeps both
    readers on one lookup, including a legacy provider spelling.
    """
    from raven.providers.registry import canonical_provider_name

    name = canonical_provider_name(provider) if provider else ""
    if not name or providers is None:
        return ()
    if isinstance(providers, dict):
        try:
            from raven.config.schema import _provider_section

            section = _provider_section(providers, name)
        except Exception:  # noqa: BLE001 - a bad edit must not fail the turn over an overlay
            section = None
    else:
        getter = getattr(providers, "get", None)
        section = getter(name) if callable(getter) else None
    return declared_efforts(overlay_for(section, provider, model))


def declared_efforts(overlay: Any) -> tuple[str, ...]:
    """The reasoning-effort words an overlay states, from a model or a raw dict."""
    if overlay is None:
        return ()
    if isinstance(overlay, dict):
        values = overlay.get("reasoningEfforts") or overlay.get("reasoning_efforts") or ()
    else:
        values = getattr(overlay, "reasoning_efforts", ()) or ()
    return tuple(value for value in values if isinstance(value, str))
