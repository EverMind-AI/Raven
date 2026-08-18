"""Builds a :class:`ModelBinding` for a model id, and caches it.

One place answers "which credential serves this model", so a per-session
model, a subsystem pin and the configured default all resolve the same way
instead of each guessing. Caching matters because a session switching back
and forth must not rebuild a provider per turn -- building one imports
LiteLLM and writes vendor env vars.

Provider resolution has to go through a config copy with the model
substituted: :meth:`Config._match_provider` short-circuits on a forced
``agents.defaults.provider``, so asking the live config about another
vendor's model would answer with the forced one and pair the wrong key.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from loguru import logger

from raven.providers.binding import ModelBinding

if TYPE_CHECKING:
    from raven.config.schema import Config


class ProviderPool:
    """Resolve and cache one provider per (provider name, model) pair."""

    def __init__(self, config: "Config | Callable[[], Config]") -> None:
        # A supplier, not a snapshot: a credential fixed after start (an OAuth
        # re-login, an edited config file) has to be visible without a
        # restart, which the old per-switch config reload gave for free.
        # Cached bindings are dropped when the config that produced them is no
        # longer the current one.
        self._supplier = config if callable(config) else (lambda: config)
        self._cache: dict[tuple[str, str], ModelBinding] = {}
        self._cache_key: str | None = None

    @property
    def config(self) -> "Config":
        return self._supplier()

    def _live_cache(self) -> dict[tuple[str, str], ModelBinding]:
        """Drop cached providers when the credentials behind them changed.

        Not identity on the config object: a supplier that re-reads the file
        returns a new object every call, which would clear the cache every
        time and defeat the pool. A fingerprint of what a provider is actually
        built from is the thing that has to match.
        """
        fingerprint = self._credentials_fingerprint()
        if self._cache_key != fingerprint:
            self._cache_key = fingerprint
            self._cache = {}
        return self._cache

    def _credentials_fingerprint(self) -> str:
        """What a built provider depends on: the keys, bases and headers."""
        import hashlib
        import json

        try:
            providers = self.config.providers.model_dump(exclude_none=True)
        except Exception:
            return ""
        return hashlib.sha256(json.dumps(providers, sort_keys=True, default=str).encode()).hexdigest()

    def bind(self, model: str, provider_name: str | None = None) -> ModelBinding:
        """Build (or reuse) the provider that serves ``model``.

        ``provider_name`` is what the caller already knows -- the picker sends
        one, and ``agents.defaults.provider`` is one. Absent or ``"auto"``, the
        vendor is derived from the model id, the same derivation
        ``config.set model`` uses for a hand-typed id.
        """
        resolved = self._resolve_provider_name(model, provider_name)
        cache = self._live_cache()
        key = (resolved, model)
        cached = cache.get(key)
        if cached is not None:
            return cached

        from raven.cli._helpers import make_provider

        cfg = self.config.model_copy(deep=True)
        cfg.agents.defaults.model = model
        cfg.agents.defaults.provider = resolved
        # The pin travels with the binding rather than being read where the
        # window is used: a session on another model still owes the user the
        # number they pinned.
        binding = ModelBinding(make_provider(cfg), model, cfg.agents.defaults.context_window_tokens)
        cache[key] = binding
        return binding

    def bind_pin(self, model: str | None, provider_name: str | None = None) -> ModelBinding | None:
        """A subsystem's own model, on its own credential -- or None.

        This is what lets a pinned subsystem run off the session's model. None
        means the pin is unusable, and the caller should fall back to the
        session's binding rather than send one vendor's key to another.

        ``provider_name`` is the configured half of the pair, and when present
        nothing is derived: an id alone cannot say whether ``anthropic`` or a
        gateway reselling it is meant, and those are different credentials and
        different bills. Absent, a configured gateway takes the pin and only
        without one is the vendor derived from the id -- which is what a config
        written before the provider field existed gets. Note the asymmetry: an
        explicitly named vendor that turns out unusable is dropped, while the
        gateway branch binds whatever it is handed, because a gateway having no
        route for an id is not something this can see from here.
        """
        if not model:
            return None
        if provider_name and provider_name != "auto":
            configured = provider_name
            if not self._has_credentials(configured, model):
                # Explicitly configured and still unusable: a config error the
                # user can fix, and silence here is what let a pinned
                # subsystem look configured while never running.
                logger.warning(
                    "pinned model {!r} names provider {!r}, which has no usable credentials; "
                    "the subsystem follows the conversation's model instead",
                    model,
                    configured,
                )
                return None
            resolved = configured
        else:
            # A gateway (or a local deployment) serves whatever id it is handed
            # under its own credential, so the pin is already paired -- asking
            # whether the upstream vendor has a key of its own would drop a pin
            # that works. Bind it through the gateway instead.
            gateway = self._configured_gateway()
            resolved = gateway if gateway is not None else self._resolve_provider_name(model, None)
            if gateway is None and not self._has_credentials(resolved, model):
                return None
        try:
            return self.bind(model, resolved)
        except Exception as exc:
            # Called from the context-engine factory at construction, so this
            # must leave the subsystem following the conversation rather than
            # stop the agent from starting. Deliberately broad: building a
            # provider imports a vendor module, so the failure modes are not
            # only the credential ones.
            logger.warning("cannot build a provider for pinned model {!r}: {}", model, exc)
            return None

    def _configured_gateway(self) -> str | None:
        """The agent's provider, when it is a gateway or a local deployment."""
        from raven.providers.registry import find_by_name

        forced = self.config.agents.defaults.provider
        if not forced or forced == "auto":
            return None
        spec = find_by_name(forced)
        if spec is None:
            return None
        return forced if (spec.is_gateway or spec.is_local) else None

    def _resolve_provider_name(self, model: str, provider_name: str | None) -> str:
        if provider_name and provider_name != "auto":
            return provider_name
        from raven.providers.registry import find_by_model

        spec = find_by_model(model)
        return spec.name if spec is not None else "auto"

    def _has_credentials(self, provider_name: str, model: str) -> bool:
        """Is there a usable section for this vendor, or only a placeholder?

        Every declared provider exists as an empty section, so presence proves
        nothing -- reuse the same check the credential preflight uses.
        """
        from raven.config.schema import _has_credentials as section_is_usable
        from raven.providers.registry import find_by_name

        if provider_name == "auto":
            # No vendor could be derived, so there is no section to check and
            # no way to tell a working pin from a mis-paired one. Treat it as
            # unusable: the caller's fallback is the session's own binding,
            # which is at least a pair.
            return False
        # ``providers.get`` is the only spelling-insensitive lookup; reading the
        # attribute sees one spelling of a name that has several.
        section = self.config.providers.get(provider_name)
        if section is None:
            return False
        return bool(section_is_usable(section, find_by_name(provider_name)))
