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
  reports, read by the sub-agent manager for the default base URL;
  ``raven doctor`` and ``raven import`` go through
  :meth:`MemoryBackend.health` instead of this module directly;
- :mod:`.backend` -- the memory backend the registry builds, plus
  ``convert_messages`` / ``as_ms_epoch``, public because the sub-agent trace
  writer needs the same shapes;
- :mod:`.config` -- everos.toml and data-root env management, read and
  written by the console settings RPC methods and borrowed by the knowledge
  and memory RPC surfaces for the plugin's own root and address;
- :mod:`.onboard` -- the ``onboard`` contribution: one ``raven onboard``
  screen, built by the host's plugin registry from the manifest factory.

Anything else in the package is the plugin's own.
"""

__version__ = "1.1.0"
