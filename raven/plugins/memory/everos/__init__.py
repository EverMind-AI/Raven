"""EverOS memory backend — bundled default plugin.

Implements the host's :class:`raven.memory_engine.MemoryBackend`
Protocol over the ``everos`` substrate (embedded in-process, with an
HTTP mode reserved). Discovered via ``raven-plugin.toml`` (bundled
source); ``backend.make_backend`` is the factory the registry calls.

This module is kept import-cheap on purpose: PluginDiscovery touches it
during resource resolution, so it must NOT import ``backend`` (which
lazily pulls the heavy ``everos`` substrate). Import the backend
explicitly from :mod:`raven.plugins.memory.everos.backend`.

What the host may reach, declared rather than assumed:

- :mod:`.server` -- the local everos service's lifecycle as the host runs it
  (probe, start, stop, lock holder, log path, the default base URL);
- :mod:`.health` -- the capability probe and the sections a healthy service
  reports, which ``raven doctor`` and the onboarding wizard read;
- :mod:`.backend` -- the memory backend the registry builds, plus
  ``convert_messages`` / ``as_ms_epoch`` and ``ServiceState``, public because
  the sub-agent trace writer and ``raven import`` need the same shapes.

Anything else in the package is the plugin's own.
"""

__version__ = "1.1.0"
