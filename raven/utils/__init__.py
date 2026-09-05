"""Utility leaves for raven: small, dependency-light helpers, each module named for what it holds.

Nothing is imported eagerly here on purpose: ``raven.utils`` is reached from
every layer, so an ``__init__`` that pulled tiktoken in would make every first
import pay for an encoding table it did not ask for. ``utils.tokens`` is the
one module that loads it, and only when a caller estimates tokens.
"""
