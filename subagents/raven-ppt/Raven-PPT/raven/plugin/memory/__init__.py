"""Bundled memory-backend plugins.

Subpackages here are discovered by :class:`raven.plugin.PluginDiscovery`
via their ``raven-plugin.toml`` manifests (the bundled source). Keep
these packages' ``__init__`` modules empty/cheap: resource resolution
imports them during discovery, and a heavy import here would defeat the
manifest-only discovery guarantee.
"""
