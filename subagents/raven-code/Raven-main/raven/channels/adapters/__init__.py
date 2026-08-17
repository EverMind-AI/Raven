"""Channel adapters.

Each sub-package here ships a ``spec.py`` exporting a
:class:`~raven.channels.contract.ChannelSpec` and is auto-discovered by
:mod:`raven.channels.registry` — no manual registration. The spec's factory
defers the heavy SDK import; internal helper modules are not enumerated by
discovery (the scan is one level deep).
"""
