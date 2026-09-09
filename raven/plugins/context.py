"""Plugin runtime context.

A factory (the ``module.path:callable`` named in a manifest) receives
exactly one :class:`PluginContext`. From it, the factory pulls:

- ``config``  — the plugin's own config slice from RavenConfig, after
  admission: declared keys are type-checked and their declared defaults
  applied, unknown keys pass through with a warning. An empty
  ``config_schema`` keeps verbatim pass-through.
- ``services`` — a :class:`ServiceLocator` exposing only the host
  services a backend is allowed to touch. The locator is intentionally
  narrow so plugins don't grow ambient dependencies on arbitrary host
  internals — every field here is a deliberate capability grant.
- ``logger`` — a logger pre-bound with ``plugin=<id>`` so plugin output
  is grep-able in mixed logs.

The locator is a frozen dataclass: factories cannot mutate the host's
view of available services, only read from it.

The three shapes a plugin implements against (``ServiceLocator``,
``RuntimeHandles``, ``BindDeclinedError``) are papers now -- defined in
``raven.contracts.plugin_surface`` under the contract tier's version and
ledger -- and re-exported here verbatim: this module stays the documented
import address for plugin authors.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from raven.contracts.plugin_surface import BindDeclinedError, RuntimeHandles, ServiceLocator


@dataclass(frozen=True)
class PluginContext:
    """What a plugin factory sees at activation time."""

    config: dict[str, Any]
    services: ServiceLocator
    logger: logging.Logger = field(
        default_factory=lambda: logging.getLogger("raven.plugins"),
    )


__all__ = ["BindDeclinedError", "PluginContext", "RuntimeHandles", "ServiceLocator"]
