"""Auto-discovery for channel adapters — no hardcoded registry."""

from __future__ import annotations

import importlib
import pkgutil
from typing import TYPE_CHECKING

from loguru import logger

if TYPE_CHECKING:
    from raven.channels.contract import ChannelSpec

_ADAPTERS_PKG = "raven.channels.adapters"


def discover_specs() -> dict[str, ChannelSpec]:
    """Return ``{name: ChannelSpec}`` for the adapters under ``raven.channels.adapters``,
    keyed by package name.

    Imports only each ``<name>/spec.py`` (cheap — the heavy SDK import is
    deferred into the spec's ``factory``). An adapter without a ``spec.py`` is
    skipped. A spec that cannot be imported is skipped too, with a warning: that
    is a broken adapter rather than an absent one, and a bad folder must neither
    take gateway startup down nor vanish without a trace.
    """
    import raven.channels.adapters as pkg

    specs: dict[str, ChannelSpec] = {}
    for _, name, ispkg in pkgutil.iter_modules(pkg.__path__):
        if not ispkg:
            continue
        spec_module = f"{_ADAPTERS_PKG}.{name}.spec"
        try:
            mod = importlib.import_module(spec_module)
        except ModuleNotFoundError as e:
            # Only the spec module itself may be missing; any other name is an
            # import the spec should have deferred into its factory.
            if e.name != spec_module:
                logger.warning("channel adapter {} skipped: its spec imports {}, which is not installed", name, e.name)
            continue
        if (spec := getattr(mod, "SPEC", None)) is not None:
            specs[name] = spec
    return specs


def discover_channel_names() -> list[str]:
    """Return adapter names by scanning the adapters package (zero imports).

    One sub-package per adapter, one level deep, so helper modules nested
    inside an adapter are not listed and never get mistaken for a channel.
    """
    import raven.channels.adapters as pkg

    return [name for _, name, _ in pkgutil.iter_modules(pkg.__path__)]
