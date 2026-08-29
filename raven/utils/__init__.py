"""Utility leaves for raven: small, dependency-light helpers, each module named for what it holds.

Nothing is imported eagerly here on purpose. ``raven.utils`` is reached from
every layer, and a package ``__init__`` that pulled tiktoken in made every
first import pay for an encoding table it did not ask for.
"""
