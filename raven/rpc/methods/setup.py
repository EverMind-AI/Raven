"""``setup.status`` RPC handler — provider configuration probe.

Contract: ``docs/openspec/changes/tui-ipc-bridge/specs/tui-ipc.md §3.9`` +
``design.md §3a.1``.

Why this exists
---------------

hermes's fork-imported ``useSessionLifecycle.ts:127,206`` + ``setupHandoff.ts:43``
hard-call ``setup.status`` on app boot. If the response is
``{provider_configured: false}`` the UI parks the user on a *Setup required*
panel and refuses to start a new session. The contract therefore has to be
honoured even in v0.1.

Q9 (partial answer): we treat ``agents.defaults.provider`` as the canonical
provider field. A concrete provider name (``"anthropic"`` / ``"openai"`` / …)
counts as *configured*; the sentinel value ``"auto"`` counts as
*not-yet-configured* (the user has not picked one and Raven has not run
auto-detection). If the config read fails for any reason — file missing,
unparseable JSON, unexpected shape — the v0.1 fallback returns
``{"provider_configured": true}`` (design §3a.1) so the hermes UI never blocks
on a transient I/O hiccup. The real signal can be tightened in v0.2 once we
support proper provider auto-detection.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from raven.rpc.dispatcher import Dispatcher


_CONFIG_FILENAME = "config.json"
_CONFIG_DIR_NAME = ".raven"
_AUTO_SENTINEL = "auto"


def _config_path() -> Path:
    return Path.home() / _CONFIG_DIR_NAME / _CONFIG_FILENAME


def _detect_provider_configured(payload: dict) -> bool:
    """Return True iff the loaded config payload indicates a usable provider.

    The onboarding gate's criterion ("required config complete"): at least one
    provider has an ``apiKey`` AND ``agents.defaults.model`` is set. Either
    alone can't drive a turn, so the UI must still park on the setup panel.
    An explicit non-``auto`` ``agents.defaults.provider`` also counts as a
    provider signal (legacy configs that pre-date per-provider sections).
    """
    if not isinstance(payload, dict):
        return False

    agents = payload.get("agents")
    defaults = agents.get("defaults") if isinstance(agents, dict) else None
    defaults = defaults if isinstance(defaults, dict) else {}

    model = defaults.get("model")
    if not (isinstance(model, str) and model):
        return False

    # `agents.defaults.provider` used to be waved through on its own, as a
    # provider signal from configs predating per-provider sections. It is now
    # written on every model change, so that branch would let a pinned name
    # stand for credentials nobody has -- the gate would pass with an empty
    # config. The name still says which section to ask about; whether it holds
    # anything is asked below, like every other provider.
    provider = defaults.get("provider")
    if isinstance(provider, str) and provider in {"minimax_global", "minimax_cn"}:
        from raven.providers.minimax_oauth import load_token

        return load_token("global" if provider == "minimax_global" else "cn") is not None

    providers = payload.get("providers")
    if isinstance(providers, dict):
        # `providers.auth`, like every other gate. Reading `apiKey` off the raw
        # payload made this the seventh rule and it disagreed with the other six
        # in both directions -- on the exact two configurations this module's
        # rewrite was filed to fix. A Gemini section holding only `apiKeyList`
        # parked a working install on the setup panel; Azure with a key and no
        # address was waved through into a chat that then could not run.
        from raven.config.schema import ProvidersConfig
        from raven.providers.auth import credential_status

        try:
            sections = ProvidersConfig.model_validate(providers)
        except Exception:
            sections = None
        if sections is not None:
            # Iterate the validated instance's own field names, not the raw
            # payload's keys: `canonical_provider_name` does not decompose
            # camelCase, so a camelCase key like "azureOpenai" -- the shape
            # `ProvidersConfig` serializes to -- fails to resolve back to the
            # `azure_openai` field it validated into, and `sections.get` on it
            # returns None. The declared fields are always snake_case, so
            # asking for those by name always resolves. Extra (unspecced)
            # sections keep their original payload spelling.
            names = set(type(sections).model_fields) | set(sections.model_extra or {})
            for name in names:
                section = sections.get(name)
                if section is not None and credential_status(name, section, include_external=True).ok:
                    return True

    from raven.providers.registry import split_model_id

    model_prefix, _ = split_model_id(model)
    if model_prefix in {"minimax_global", "minimax_cn"}:
        from raven.providers.minimax_oauth import load_token

        region = "global" if model_prefix == "minimax_global" else "cn"
        return load_token(region) is not None

    return False


async def setup_status(params: dict) -> dict:
    """``setup.status`` — return whether a provider has been configured.

    v0.1 fallback: on any read / parse failure, return
    ``{"provider_configured": true}`` so the hermes UI does not park on the
    *Setup required* panel.
    """
    path = _config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        logger.debug("setup.status: {} missing → v0.1 fallback true", path)
        return {"provider_configured": True}
    except OSError as exc:
        logger.warning("setup.status: read failed for {}: {} → fallback true", path, exc)
        return {"provider_configured": True}

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        logger.warning("setup.status: invalid JSON in {}: {} → fallback true", path, exc)
        return {"provider_configured": True}

    return {"provider_configured": _detect_provider_configured(payload)}


def register_setup_methods(dispatcher: "Dispatcher") -> None:
    """Register ``setup.status`` on a dispatcher instance."""
    dispatcher.register("setup.status", setup_status)


__all__ = ["setup_status", "register_setup_methods"]
