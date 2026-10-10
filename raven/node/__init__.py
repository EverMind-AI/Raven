"""A Raven node: Raven's own file tools, run on a registered machine for the Raven that started it.

The node half is :mod:`raven.node.server`, run there as ``python -m raven.node
--stdio``; the host half is :mod:`raven.node.client` and :mod:`raven.node.install`.
This package's ``__init__`` imports nothing, so starting a node loads only what
the node itself needs.
"""
