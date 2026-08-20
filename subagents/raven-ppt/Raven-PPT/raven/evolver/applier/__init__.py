"""Evolver patch-application machinery.

Owns the **commit-side** of evolver patches: given a candidate
``AppliedPatch`` produced by the judge, decide whether it can be applied
to the working tree, and if so, write it out.

Currently exposes:

- :mod:`path_guard` — first-line immutability check; rejects patches that
  target kernel paths (spec §22 + §22.7).
- :mod:`beacon_guard` — code-class patch validation; rejects patches without
  activation beacons before eval (design section 3).

Future C2 will add a Git-backed atomic apply that uses ``path_guard``
internally to short-circuit before any commit is created.
"""

from .beacon_guard import (
    MissingBeaconError,
    assert_beacon_present,
)
from .path_guard import (
    IMMUTABLE_PATTERNS,
    MUTABLE_OVERRIDES,
    ImmutablePathError,
    assert_patch_allowed,
    check_patch_paths,
    is_immutable,
)

__all__ = [
    "IMMUTABLE_PATTERNS",
    "MUTABLE_OVERRIDES",
    "ImmutablePathError",
    "assert_patch_allowed",
    "check_patch_paths",
    "is_immutable",
    "MissingBeaconError",
    "assert_beacon_present",
]
