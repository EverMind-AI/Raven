"""EverOS memory backend -- Raven's default memory plugin.

Implements the host's :class:`raven.memory_engine.MemoryBackend` Protocol over
HTTP against a local everos server this plugin probes and starts (see
``.server``). Shipped as its own distribution (``everos-memory``) and found
through the ``raven.plugins`` entry-point group, which resolves
``raven-plugin.toml`` out of this package; ``backend.make_backend`` is the
factory the registry calls.

This module is kept import-cheap on purpose: PluginDiscovery touches it
during resource resolution, so it must NOT import ``backend`` (which
lazily pulls the heavy ``everos`` substrate). Import the backend
explicitly from :mod:`raven_everos.backend`.

What the host may reach, declared rather than assumed:

- :mod:`.server` -- the local everos service's lifecycle as the host runs it
  (probe, start, stop, lock holder, log path, the default base URL);
- :mod:`.health` -- the capability probe and the sections a healthy service
  reports, which ``raven doctor`` and the onboarding wizard read;
- :mod:`.backend` -- the memory backend the registry builds, plus
  ``convert_messages`` / ``as_ms_epoch`` and ``ServiceState``, public because
  the sub-agent trace writer and ``raven import`` need the same shapes;
- :mod:`.roots` -- where an everos data root may live on this machine and what
  state each is in, which the onboarding wizard discovers and picks from.

Anything else in the package is the plugin's own.
"""

__version__ = "1.1.0"
