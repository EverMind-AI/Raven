"""EverOS memory backend — bundled default plugin.

Implements the host's :class:`raven.memory_engine.MemoryBackend`
Protocol over the ``everos`` substrate (embedded in-process, with an
HTTP mode reserved). Discovered via ``raven-plugin.toml`` (bundled
source); ``backend.make_backend`` is the factory the registry calls.

This module is kept import-cheap on purpose: PluginDiscovery touches it
during resource resolution, so it must NOT import ``backend`` (which
lazily pulls the heavy ``everos`` substrate). Import the backend
explicitly from :mod:`raven.plugin.memory.everos.backend`.
"""

__version__ = "1.0.0"
