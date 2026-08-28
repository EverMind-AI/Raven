"""PlugHub — the plugin marketplace engine.

A market "plugin" is a manifest, not a program: a catalog entry declaring
0..n contributions, each landing at a different injection point (an
``mcp`` config stanza, a workspace skill, a python package). Installing is
a ledger-backed transaction over those pieces; uninstalling replays the
ledger in reverse.

Modules:

- :mod:`raven.market.catalog` — entry source (bundled JSON, hub override)
- :mod:`raven.market.ledger`  — one JSON file per installed plugin
- :mod:`raven.market.install` — atomic install / uninstall / toggle
- :mod:`raven.market.trust`   — what a hub endpoint and an entry may be
- :mod:`raven.market.connect` — install + connect + rollback, shared by every surface
"""

from raven.market.catalog import catalog_categories, catalog_detail, catalog_search, catalog_suggest
from raven.market.install import install_plugin, toggle_server, uninstall_plugin
from raven.market.ledger import ledger_path, read_ledger, read_ledgers

__all__ = [
    "catalog_categories",
    "catalog_detail",
    "catalog_search",
    "catalog_suggest",
    "install_plugin",
    "ledger_path",
    "read_ledger",
    "read_ledgers",
    "toggle_server",
    "uninstall_plugin",
]
