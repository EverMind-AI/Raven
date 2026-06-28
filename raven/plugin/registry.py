"""Plugin registry — turns discovered manifests into callable factories.

Two responsibilities, split deliberately:

1. **Activation** (:meth:`activate`) — for each discovered plugin
   admitted by the user's config (``plugins.disabled`` opt-out list +
   ``enabled_by_default`` flag), resolve each contributed factory
   reference (``module.path:callable``) into an actual callable and
   record it in the factory table. This is where plugin Python code is
   first imported — manifests up to this point have been pure data.

2. **Lookup** (:meth:`get_memory_backend_factory` etc.) — synchronous
   lookups for the eventual ``build_memory_backend`` entry point
   landing in PG-3.

Across-manifest name conflicts (two activated plugins both contributing
a memory_backend named ``"everos"``) raise :class:`PluginConflict` —
which the host treats as a startup failure. The discovery layer already
deduplicated *plugins* by id; the registry adds the second layer of
deduplication on *contribution names*.
"""

from __future__ import annotations

import importlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from raven.plugin.discover import DiscoveredPlugin
from raven.plugin.manifest import PluginManifest

logger = logging.getLogger(__name__)


# A memory-backend factory is a callable that consumes a PluginContext
# and returns a MemoryBackend implementation. MemoryBackend lands in
# MB-1; until then the return is typed as Any so PG can compile alone.
MemoryBackendFactory = Callable[[Any], Any]

# A tool factory consumes a PluginContext and returns a single
# ``raven.agent.tools.base.Tool``. Typed as Any here so the plugin
# layer stays import-light (no dependency on the agent package).
ToolFactory = Callable[[Any], Any]


class PluginError(Exception):
    """Base for plugin-system errors. Catchable as a single class so
    CLI / host startup can render a unified diagnostic banner."""


class PluginConflict(PluginError):
    """Two activated plugins contributed the same name into one slot."""


class PluginFactoryImportError(PluginError):
    """A manifest pointed at ``module.path:callable`` we couldn't import
    or resolve."""


class PluginNotFound(PluginError):
    """The user asked for a backend name no activated plugin contributes."""


@dataclass(frozen=True)
class _ActivatedFactory:
    """Resolved factory + provenance for diagnostics."""

    plugin_id: str
    name: str
    factory: MemoryBackendFactory


class PluginRegistry:
    """Single registration center for activated contribution factories."""

    def __init__(self) -> None:
        self._manifests: dict[str, PluginManifest] = {}
        self._memory_backends: dict[str, _ActivatedFactory] = {}
        self._tools: dict[str, _ActivatedFactory] = {}

    # ── Activation ───────────────────────────────────────────────

    def activate(
        self,
        discovered: list[DiscoveredPlugin],
        *,
        disabled: frozenset[str] = frozenset(),
    ) -> None:
        """Resolve and register every contribution from every admitted plugin.

        A plugin is admitted iff:

        - its id is not in ``disabled`` (user opt-out), AND
        - ``enabled_by_default`` is True OR the host has another reason
          to include it. PG-2 enforces only the first rule; PG-3 layers
          on the second when wired to the user config.
        """
        for d in discovered:
            mf = d.manifest
            if mf.id in disabled:
                logger.info("plugin %s disabled by user config", mf.id)
                continue
            if not mf.enabled_by_default:
                logger.info(
                    "plugin %s not enabled by default; skipping (use "
                    "explicit opt-in once supported)",
                    mf.id,
                )
                continue
            self._activate_one(mf)

    def _activate_one(self, mf: PluginManifest) -> None:
        if mf.id in self._manifests:
            # Discovery should have deduped this already; defensive.
            raise PluginConflict(
                f"plugin id {mf.id!r} activated twice",
            )
        self._manifests[mf.id] = mf

        for contribution in mf.contributes.memory_backends:
            if contribution.name in self._memory_backends:
                prev = self._memory_backends[contribution.name]
                raise PluginConflict(
                    f"memory_backend {contribution.name!r} contributed by "
                    f"both {prev.plugin_id!r} and {mf.id!r}",
                )
            factory = self._resolve_factory(mf.id, contribution.factory)
            self._memory_backends[contribution.name] = _ActivatedFactory(
                plugin_id=mf.id,
                name=contribution.name,
                factory=factory,
            )
            logger.debug(
                "registered memory_backend %s from %s",
                contribution.name, mf.id,
            )

        for tool in mf.contributes.tools:
            if tool.name in self._tools:
                prev = self._tools[tool.name]
                raise PluginConflict(
                    f"tool {tool.name!r} contributed by both "
                    f"{prev.plugin_id!r} and {mf.id!r}",
                )
            factory = self._resolve_factory(mf.id, tool.factory)
            self._tools[tool.name] = _ActivatedFactory(
                plugin_id=mf.id,
                name=tool.name,
                factory=factory,
            )
            logger.debug("registered tool %s from %s", tool.name, mf.id)

    @staticmethod
    def _resolve_factory(plugin_id: str, ref: str) -> MemoryBackendFactory:
        """Import ``module`` and grab ``callable`` from it.

        Manifest validation already enforced the ``module.path:callable``
        shape, so this just splits and imports.
        """
        module_path, _, attr = ref.partition(":")
        try:
            mod = importlib.import_module(module_path)
        except Exception as e:
            raise PluginFactoryImportError(
                f"plugin {plugin_id!r}: importing {module_path!r} failed: {e}",
            ) from e
        try:
            obj = getattr(mod, attr)
        except AttributeError as e:
            raise PluginFactoryImportError(
                f"plugin {plugin_id!r}: {module_path!r} has no attribute "
                f"{attr!r}",
            ) from e
        if not callable(obj):
            raise PluginFactoryImportError(
                f"plugin {plugin_id!r}: {ref} resolved to a non-callable "
                f"{type(obj).__name__}",
            )
        return obj  # type: ignore[return-value]

    # ── Introspection ────────────────────────────────────────────

    def activated_ids(self) -> list[str]:
        """Stable-ordered list of activated plugin ids."""
        return sorted(self._manifests)

    def memory_backend_names(self) -> list[str]:
        """Stable-ordered list of registered memory-backend names."""
        return sorted(self._memory_backends)

    def get_memory_backend_factory(self, name: str) -> MemoryBackendFactory:
        """Look up the factory for ``name``. Raises ``PluginNotFound``."""
        try:
            return self._memory_backends[name].factory
        except KeyError as e:
            raise PluginNotFound(
                f"no memory_backend named {name!r} (registered: "
                f"{self.memory_backend_names()})",
            ) from e

    def tool_names(self) -> list[str]:
        """Stable-ordered list of registered plugin-tool names."""
        return sorted(self._tools)

    def tool_plugin_id(self, name: str) -> str | None:
        """Plugin id that contributed tool ``name``, or ``None``."""
        entry = self._tools.get(name)
        return entry.plugin_id if entry is not None else None

    def get_tool_factory(self, name: str) -> ToolFactory:
        """Look up the factory for tool ``name``. Raises ``PluginNotFound``."""
        try:
            return self._tools[name].factory
        except KeyError as e:
            raise PluginNotFound(
                f"no tool named {name!r} (registered: {self.tool_names()})",
            ) from e

    def manifest_for(self, plugin_id: str) -> PluginManifest | None:
        """Return the manifest of an activated plugin, or None."""
        return self._manifests.get(plugin_id)

    # ── Build (PG-3 entry point) ──────────────────────────────────

    def build_memory_backend(
        self,
        name: str,
        *,
        config: dict[str, Any],
        services: "ServiceLocator",
        logger: logging.Logger | None = None,
    ) -> Any:
        """Resolve the named factory and call it with a fresh ``PluginContext``.

        Construction is synchronous — factories that need async setup
        return a backend whose ``start()`` will be awaited later by the
        host. Any exception from the factory propagates so the host
        sees the real cause rather than a wrapped one.
        """
        from raven.plugin.context import PluginContext  # local: cycle-safe

        factory = self.get_memory_backend_factory(name)
        ctx = PluginContext(
            config=config,
            services=services,
            logger=logger
            or logging.getLogger(f"raven.plugin.{name}"),
        )
        return factory(ctx)

    def build_tool(
        self,
        name: str,
        *,
        config: dict[str, Any],
        services: "ServiceLocator",
        logger: logging.Logger | None = None,
    ) -> Any:
        """Resolve the named tool factory and call it with a fresh
        ``PluginContext``, returning the constructed ``Tool``.

        Symmetric with :meth:`build_memory_backend`: synchronous
        construction, exceptions propagate so the host sees the real
        cause. The host registers the returned tool into the agent's
        :class:`ToolRegistry`.
        """
        from raven.plugin.context import PluginContext  # local: cycle-safe

        factory = self.get_tool_factory(name)
        ctx = PluginContext(
            config=config,
            services=services,
            logger=logger or logging.getLogger(f"raven.plugin.{name}"),
        )
        return factory(ctx)


# Forward import for the type hint above. Kept at module-bottom so the
# import cost is paid only when someone reads the class — and to avoid
# the circular hit at module-load time (registry is imported from
# __init__ before context is).
from raven.plugin.context import ServiceLocator  # noqa: E402


__all__ = [
    "MemoryBackendFactory",
    "PluginConflict",
    "PluginError",
    "PluginFactoryImportError",
    "PluginNotFound",
    "PluginRegistry",
    "ToolFactory",
]
