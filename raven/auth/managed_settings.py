"""Managed settings — placeholder for company-deployment config locks.

``ManagedSettings`` is a thin dataclass describing
which config fields are locked by operator policy. It's currently
unconsumed — config loading doesn't yet consult it — but defining
the shape now means future work has a stable seam.

Typical use case (post-MVP): a managed Raven deployment locks
``providers.openrouter.api_key`` to the company's shared key and
``sandbox.backend`` to ``"boxlite"``. The loader checks
``ManagedSettings.locked_fields`` and rejects user-side overrides
of any field in that set.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ManagedSettings:
    """Operator policy — which config paths are locked.

    Each entry in ``locked_fields`` is a dot-separated config path
    (e.g. ``providers.openrouter.api_key``). The loader is responsible
    for honoring the lock; this dataclass merely carries the policy.
    """

    locked_fields: frozenset[str] = field(default_factory=frozenset)
    description: str = ""

    def is_locked(self, field_path: str) -> bool:
        return field_path in self.locked_fields


__all__ = ["ManagedSettings"]
