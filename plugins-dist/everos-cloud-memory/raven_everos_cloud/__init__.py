"""EverOS Cloud memory backend for Raven.

Implements the host's :class:`raven.memory_engine.MemoryBackend` Protocol over
HTTPS against EverOS Cloud (``https://api.evermind.ai``): the same
``/api/v2/memory/{search,add,flush,get}`` routes the local ``everos-memory``
plugin speaks, with a Bearer key instead of a local server to start and no
model roles to configure. ``backend.make_backend`` is the factory the registry
calls; ``onboard.make_onboard_step`` builds the one ``raven onboard`` screen.

Kept import-cheap on purpose: PluginDiscovery resolves ``raven-plugin.toml``
through this module, so it imports neither ``httpx`` nor ``backend``.
"""

__version__ = "0.1.0"
