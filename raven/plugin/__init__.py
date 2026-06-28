"""Plugin foundation.

PG-1 introduces the manifest schema + plugin context. Registry and
discovery follow in PG-2/PG-3. The first (and currently only) public
contribution point is ``memory_backends``; the schema is forward-
compatible so future contribution types land without breaking existing
manifests.

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

from raven.plugin.bootstrap import assemble_plugin_registry
from raven.plugin.context import PluginContext, ServiceLocator
from raven.plugin.discover import DiscoveredPlugin, PluginDiscovery, Source
from raven.plugin.manifest import (
    Contributes,
    MemoryBackendContribution,
    PluginManifest,
    ToolContribution,
)
from raven.plugin.registry import (
    MemoryBackendFactory,
    PluginConflict,
    PluginError,
    PluginFactoryImportError,
    PluginNotFound,
    PluginRegistry,
    ToolFactory,
)

__all__ = [
    "Contributes",
    "DiscoveredPlugin",
    "assemble_plugin_registry",
    "MemoryBackendContribution",
    "MemoryBackendFactory",
    "PluginConflict",
    "PluginContext",
    "PluginDiscovery",
    "PluginError",
    "PluginFactoryImportError",
    "PluginManifest",
    "PluginNotFound",
    "PluginRegistry",
    "ServiceLocator",
    "Source",
    "ToolContribution",
    "ToolFactory",
]
