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
    from raven.tui_rpc.dispatcher import Dispatcher


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

    provider = defaults.get("provider")
    if isinstance(provider, str) and provider and provider != _AUTO_SENTINEL:
        return True

    providers = payload.get("providers")
    if isinstance(providers, dict) and any(isinstance(v, dict) and v.get("apiKey") for v in providers.values()):
        return True

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
