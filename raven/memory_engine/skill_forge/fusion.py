"""Weighted Reciprocal Rank Fusion across heterogeneous skill sources.

The classic RRF formula sums ``1 / (k + rank_i(d))`` over the sources
that hit document ``d``. Multi-source skill retrieval needs a small
extension: each source carries a **trust weight** so curated content
(Local) outranks imported content (Mass) at equal rank. The weighted
form is::

    rrf_score(d) = Σ_i  w_i / (k + rank_i(d))

with ``k = 60`` (the long-standing RRF constant) and the per-source
``w_i`` coming from the source's :attr:`SkillSource.weight` attribute.

Three additional behaviors are baked in:

- **Cross-source dedup by name.** A skill appearing in multiple
  sources (e.g. Local has the canonical version, EverOS has a
  self-evolved variant under the same name) collapses into one hit.
  RRF scores from both sources accumulate; the representative ``hit``
  is the one with the highest per-source ``score`` so the prompt sees
  the best version available.

- **Telemetry annotation.** Each output hit gets ``rrf_score`` and
  ``contributing_sources`` written into ``meta`` so a debug overlay /
  log line can trace which sources fed which slot.

- **Frozen-output discipline.** :class:`RouterHit` is frozen, so the
  fusion never mutates input hits in place — it constructs new hits
  via :func:`dataclasses.replace`. That keeps the source-side caches
  intact and makes the fusion safely re-runnable on the same inputs.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import replace

from raven.memory_engine.skill_forge.types import RouterHit

# The "60" in classic RRF — dampens rank effects so a #1 at one source
# doesn't always crowd out top-3 from another. Standard value, kept as
# a module constant so the rare experiment that wants to tune it can.
RRF_K: int = 60


def rrf_merge_weighted(
    source_results: list[tuple[str, float, list[RouterHit]]],
    k: int,
    dedup_by: str = "name",
) -> list[RouterHit]:
    """Fuse per-source ranked lists into one top-K.

    Args:
        source_results: One ``(source_name, weight, hits)`` triple per
            source. ``hits`` is the source's own ranked output (best
            first); each list's length is independent.
        k: Maximum number of hits to return after fusion.
        dedup_by: :class:`RouterHit` attribute used as the cross-source
            collapse key. Default ``"name"`` matches the design — two
            sources surfacing a skill with the same display name are
            one logical skill. Tests pass ``"qualified_id"`` when they
            want to verify "no dedup happened" on disjoint hits.

    Returns:
        Up to ``k`` :class:`RouterHit` records, ranked by descending
        RRF score. Each has ``rrf_score`` (float) and
        ``contributing_sources`` (list[str], stable-ordered as
        encountered) added to its ``meta``.
    """
    rrf_scores: dict[str, float] = defaultdict(float)
    best_hit: dict[str, RouterHit] = {}
    contributing: dict[str, list[str]] = defaultdict(list)

    for source_name, weight, hits in source_results:
        for rank, hit in enumerate(hits, start=1):
            key = getattr(hit, dedup_by)
            rrf_scores[key] += weight / (RRF_K + rank)
            contributing[key].append(source_name)
            prev = best_hit.get(key)
            # Keep the hit with the higher per-source ``score`` as the
            # representative — the user-facing prompt should see the
            # best-ranked instance, not arbitrary first-seen.
            if prev is None or hit.score > prev.score:
                best_hit[key] = hit

    # Stable sort by descending RRF; for ties Python's sort is stable
    # so insertion order (= dedup_key encounter order) breaks ties
    # deterministically.
    ranked = sorted(
        rrf_scores.items(),
        key=lambda kv: kv[1],
        reverse=True,
    )

    out: list[RouterHit] = []
    for key, score in ranked[:k]:
        rep = best_hit[key]
        new_meta = {
            **rep.meta,
            "rrf_score": score,
            "contributing_sources": list(contributing[key]),
        }
        out.append(replace(rep, meta=new_meta))
    return out


__all__ = ["RRF_K", "rrf_merge_weighted"]
