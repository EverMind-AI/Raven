"""The assembled-context shapes: re-exported from the papers package.

The definitions moved to :mod:`raven.contracts.assembled`; this module keeps
the historical import path resolving.
"""

from raven.contracts.assembled import (  # noqa: F401
    AssembledContext,
    TokenBudget,
)

__all__ = ["AssembledContext", "TokenBudget"]
