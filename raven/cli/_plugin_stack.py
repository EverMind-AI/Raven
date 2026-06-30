"""CLI assembly helper for the plugin / memory-backend stack.

Two functions that bridge the gap between RavenConfig (user-facing
settings under ``plugins`` / ``memory``) and the runtime objects
AgentLoop expects (a ready-to-use :class:`MemoryBackend` instance):

- :func:`build_plugin_registry` — discover all installed plugins
  (bundled + user-level + project-level + pip entry points), filter
  by ``config.plugins.disabled``, return an activated registry.
- :func:`maybe_build_memory_backend` — resolve ``config.memory.backend``
  to a concrete :class:`MemoryBackend` instance via the registry, or
  return ``None`` when no backend is selected / the requested
  contribution isn't available.

Both functions are intentionally lenient: a missing
plugin / activation error logs a warning and falls through to ``None``
rather than crashing the host. The legacy ``self.memory`` pipeline in
AgentLoop is unaffected — it always works regardless of whether a
plugin backend is wired.

Lifecycle (``backend.start()`` / ``backend.stop()``) is the **caller's**
responsibility. These helpers only construct; CLI bootstrap code does
the await around them.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from raven.plugin import (
    PluginConflict,
    PluginFactoryImportError,
    PluginNotFound,
    PluginRegistry,
    ServiceLocator,
    assemble_plugin_registry,
)

if TYPE_CHECKING:
    from raven.config.raven import RavenConfig
    from raven.memory_engine import MemoryBackend

logger = logging.getLogger(__name__)


def plugin_discovery_sources() -> dict:
    """Resolve the four discovery-source locations the host scans.

    Shared by :func:`build_plugin_registry` (live boot) and the
    ``raven plugins`` CLI command so both see the same set:

    - bundled — ``raven/plugin/memory/`` inside the package.
    - user    — ``~/.raven/plugin/``.
    - project — ``./.raven/plugin/``.
    - entry_points — the ``raven.plugins`` group.
    """
    import raven

    return {
        "bundled_dir": Path(raven.__path__[0]) / "plugin" / "memory",
        "user_dir": Path.home() / ".raven" / "plugins",
        "project_dir": Path.cwd() / ".raven" / "plugins",
        "entry_points_group": "raven.plugins",
    }


def build_plugin_registry(
    config: "RavenConfig",
) -> PluginRegistry:
    """Discover + activate every installed plugin admitted by ``config``.

    Reads ``config.plugins.disabled`` and forwards it to
    :func:`assemble_plugin_registry`. Activation errors
    (:class:`PluginConflict`, :class:`PluginFactoryImportError`) are
    caught and logged — the caller receives an **empty** registry so
    AgentLoop can still boot and fall back to the legacy path.

    Discovery spans four sources (priority bundled > user > project >
    entry_points):

    - **bundled** — ``raven/plugin/memory/<id>/`` shipped inside the
      raven package (the EverOS backend lives here).
    - **user** — ``~/.raven/plugin/<id>/`` drop-in directories.
    - **project** — ``./.raven/plugin/<id>/`` drop-in directories.
    - **entry_points** — the ``raven.plugins`` group, where
      third-party pip-installed plugins register their factories.
    """
    disabled = frozenset(config.plugins.disabled)
    try:
        return assemble_plugin_registry(
            **plugin_discovery_sources(),
            disabled=disabled,
        )
    except (PluginConflict, PluginFactoryImportError) as e:
        logger.warning(
            "plugin activation failed (%s); continuing without plugins. AgentLoop will use its legacy memory path.",
            e,
        )
        return PluginRegistry()


def maybe_build_memory_backend(
    workspace: Path,
    config: "RavenConfig",
    *,
    registry: PluginRegistry | None = None,
) -> "MemoryBackend | None":
    """Construct the configured memory backend, if any.

    Resolution order:

    1. If ``config.memory.backend`` is ``None``, return ``None``
       immediately — user explicitly disabled the plugin path.
    2. Look up the backend factory in the (possibly host-supplied)
       :class:`PluginRegistry`. If absent (e.g. the everos substrate
       wasn't installed), log a warning and return ``None``.
    3. Resolve the per-plugin config slice from
       ``config.plugins.config`` — first by plugin id (the canonical
       key, e.g. ``"everos-memory"``), then by backend contribution
       name (the friendlier key, e.g. ``"everos"``) as a fallback.

    The returned backend has **not** been ``await``-started — the
    caller (CLI bootstrap) is responsible for the
    ``await backend.start()`` / ``await backend.stop()`` lifecycle so
    those awaits sit in the right async context.
    """
    name = config.memory.backend
    if name is None:
        return None
    if registry is None:
        registry = build_plugin_registry(config)
    plugin_slice = _resolve_plugin_config_slice(registry, config, name)
    services = ServiceLocator(workspace=workspace)
    try:
        backend = registry.build_memory_backend(
            name,
            config=plugin_slice,
            services=services,
        )
    except PluginNotFound:
        logger.warning(
            "memory.backend=%r requested but no plugin contributes it. "
            "The everos backend ships bundled with raven — run `uv sync` "
            "to install its substrate. Continuing without a plugin backend.",
            name,
        )
        return None
    except Exception as e:
        # Factory raised during construction — log + degrade rather
        # than fail the host boot. CLEANUP will tighten this once the
        # plugin path is the canonical one and a failure is fatal.
        logger.warning(
            "memory backend %r factory raised at construction (%s); continuing without backend.",
            name,
            e,
        )
        return None
    return backend


def build_plugin_tools(
    workspace: Path,
    config: "RavenConfig",
    *,
    registry: PluginRegistry | None = None,
) -> list:
    """Construct every plugin-contributed tool admitted by ``config``.

    Mirrors :func:`maybe_build_memory_backend` but for the ``tools``
    contribution point: walks the activated registry's tool names,
    resolves each owning plugin's config slice, and builds the tool via
    :meth:`PluginRegistry.build_tool`. Lenient by design — a single
    tool's construction failure is logged and skipped so one bad plugin
    can't keep the agent from booting. A factory may also return ``None``
    to deliberately decline contribution (e.g. an optional dependency is
    absent); that's skipped quietly, not treated as a failure. The host
    registers the returned tools into the agent's :class:`ToolRegistry`.

    Returns an empty list when no plugin contributes a tool.
    """
    if registry is None:
        registry = build_plugin_registry(config)
    names = registry.tool_names()
    if not names:
        return []
    services = ServiceLocator(workspace=workspace)
    slices = config.plugins.config
    tools = []
    for name in names:
        plugin_id = registry.tool_plugin_id(name)
        plugin_slice = (plugin_id and slices.get(plugin_id)) or slices.get(name) or {}
        try:
            tool = registry.build_tool(
                name,
                config=plugin_slice,
                services=services,
            )
        except Exception as e:
            logger.warning(
                "plugin tool %r factory raised at construction (%s); skipping it.",
                name,
                e,
            )
            continue
        # A factory may return None to decline contribution at runtime
        # (e.g. an optional dependency isn't installed). That's a clean
        # opt-out, not a failure — skip it without the warning.
        if tool is None:
            logger.debug(
                "plugin tool %r factory opted out (returned None); skipping it.",
                name,
            )
            continue
        tools.append(tool)
    return tools


def _resolve_plugin_config_slice(
    registry: PluginRegistry,
    config: "RavenConfig",
    backend_name: str,
) -> dict:
    """Pick the right ``config.plugins.config[...]`` entry for a backend.

    Tries two keys, in order:

    1. The **plugin id** that contributes ``backend_name`` (canonical,
       e.g. ``"everos-memory"`` — comes from the manifest's
       ``[plugin] id`` field).
    2. The **backend contribution name** itself
       (e.g. ``"everos"`` — friendlier for handwritten config files).

    Returns an empty dict when neither key is present, so the plugin
    factory receives a deterministic shape and applies its own
    defaults.
    """
    slices = config.plugins.config
    plugin_id = _plugin_id_for_backend(registry, backend_name)
    if plugin_id is not None and plugin_id in slices:
        return slices[plugin_id]
    if backend_name in slices:
        return slices[backend_name]
    return {}


def _plugin_id_for_backend(
    registry: PluginRegistry,
    backend_name: str,
) -> str | None:
    """Reverse-lookup the plugin id that contributes ``backend_name``.

    Returns ``None`` when no activated plugin contributes the named
    backend — the caller (config resolver) treats that as "fall
    through to the contribution-name key".
    """
    for plugin_id in registry.activated_ids():
        mf = registry.manifest_for(plugin_id)
        if mf is None:
            continue
        for contribution in mf.contributes.memory_backends:
            if contribution.name == backend_name:
                return plugin_id
    return None


__all__ = [
    "build_plugin_registry",
    "build_plugin_tools",
    "maybe_build_memory_backend",
]
