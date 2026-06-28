"""ProactiveBench 4-cell categories + stratified sampling.

Every pbench adapter samples N records spread across these four cells so
small-N smoke tests still probe each failure mode.
"""

from __future__ import annotations

from typing import Any

CATEGORIES = [
    "Correct-Detection (CD)",
    "Correct-Rejection (CR)",
    "Missed-Need (MN)",
    "False-Alarm (FA)",
]


def sample_stratified(records: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """Pick ``n`` records spread as evenly as possible across 4 categories.

    For n=10: 3+3+2+2 (CD, CR, MN, FA). For n=120: 30+30+30+30.
    Extras land in the first categories deterministically so smoke runs
    are reproducible.
    """
    per_cat_base = n // 4
    extras = n - per_cat_base * 4
    targets = {
        cat: per_cat_base + (1 if i < extras else 0)
        for i, cat in enumerate(CATEGORIES)
    }
    seen = {c: 0 for c in CATEGORIES}
    picks: list[dict[str, Any]] = []
    for r in records:
        c = r.get("category")
        if c in seen and seen[c] < targets[c]:
            picks.append(r)
            seen[c] += 1
        if all(seen[c] >= targets[c] for c in CATEGORIES):
            break
    return picks


__all__ = ["CATEGORIES", "sample_stratified"]
