"""Install-time safety policy shared by both Hub install paths.

The two call sites — the ``SkillsSegmentBuilder`` post-gate hydrate
(auto-inject) and the ``use_skill`` tool — apply the same checks before
any ``SkillHubClient.install``:

- ``refuses_low_safety`` — the ``score_safety`` bar. Catalog search
  payloads omit the score, so the authoritative check runs on the
  *detail* metadata (already fetched by both paths); a missing score
  passes to keep score-less deployments working.
- ``is_blocked`` — the operator blocklist (``skillForge.blocklist``),
  matched case-insensitively against any known identifier of the skill
  (name / slug / native id).

Stdlib-only on purpose: this module ships with the client when the
package is extracted for reuse outside Raven.
"""

from __future__ import annotations

from typing import Iterable


def normalize_blocklist(names: Iterable[str] | None) -> frozenset[str]:
    """Casefold + trim a user-supplied blocklist into a match set."""
    if not names:
        return frozenset()
    return frozenset(n.strip().casefold() for n in names if n and n.strip())


def is_blocked(blocklist: frozenset[str], *identifiers: str | None) -> bool:
    """True when any identifier of the skill is on the blocklist."""
    if not blocklist:
        return False
    return any(i is not None and i.casefold() in blocklist for i in identifiers)


def refuses_low_safety(score: object, min_safety: float) -> bool:
    """True when a present, parseable ``score_safety`` is below the bar.

    A missing or malformed score passes: catalog/detail payloads without
    scores are a supported deployment shape, and the guard must not turn
    them into a hard outage.
    """
    if score is None:
        return False
    try:
        return float(score) < min_safety  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


__all__ = ["is_blocked", "normalize_blocklist", "refuses_low_safety"]
