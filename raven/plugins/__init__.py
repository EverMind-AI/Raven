"""Plugin foundation: the manifest schema, the plugin context, discovery and
the registry.

A plugin contributes memory backends and tools (``[[plugin.contributes.
memory_backends]]``, ``[[plugin.contributes.tools]]``); the schema ignores
kinds it does not know, so a manifest written for a later host still loads.

Two principles, both load-bearing:

1. **Manifests are pure data.** ``PluginManifest.from_toml_path`` only
   reads the TOML; no plugin code is imported until the registry asks
   the factory to build a backend. This keeps startup deterministic and
   audit-friendly.

2. **Factories are referenced by ``module.path:callable`` strings.**
   The registry imports the module and resolves the callable lazily —
   manifest parsing never triggers import-time side effects in the
   plugin's package.
"""

from __future__ import annotations

from raven.plugins.bootstrap import assemble_plugin_registry
from raven.plugins.context import PluginContext, ServiceLocator
from raven.plugins.discover import DiscoveredPlugin, PluginDiscovery, Source
from raven.plugins.manifest import (
    Contributes,
    MemoryBackendContribution,
    PluginManifest,
    ToolContribution,
)
from raven.plugins.registry import (
    MemoryBackendFactory,
    PluginConflictError,
    PluginError,
    PluginFactoryImportError,
    PluginNotFoundError,
    PluginRegistry,
    ToolFactory,
)

__all__ = [
    "Contributes",
    "DiscoveredPlugin",
    "assemble_plugin_registry",
    "MemoryBackendContribution",
    "MemoryBackendFactory",
    "PluginConflictError",
    "PluginContext",
    "PluginDiscovery",
    "PluginError",
    "PluginFactoryImportError",
    "PluginManifest",
    "PluginNotFoundError",
    "PluginRegistry",
    "ServiceLocator",
    "Source",
    "ToolContribution",
    "ToolFactory",
]
