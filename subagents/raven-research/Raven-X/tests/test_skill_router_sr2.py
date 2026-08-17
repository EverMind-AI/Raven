"""SR-2 — SkillForgeRouter weighted RRF fusion + concurrent fan-out + failure isolation."""

from __future__ import annotations

import asyncio

import pytest

from raven.memory_engine.skill_forge import (
    RouterHit,
    SkillForgeRouter,
    rrf_merge_weighted,
)


def _hit(qid: str, name: str, score: float = 0.5) -> RouterHit:
    return RouterHit(qualified_id=qid, name=name, content=name, score=score)


# ---------------------------------------------------------------------------
# rrf_merge_weighted
# ---------------------------------------------------------------------------


class TestRrfMergeBasics:
    def test_single_source_preserves_order(self) -> None:
        hits = [_hit("a/1", "skill1"), _hit("a/2", "skill2"), _hit("a/3", "skill3")]
        out = rrf_merge_weighted([("a", 1.0, hits)], k=3)
        assert [h.name for h in out] == ["skill1", "skill2", "skill3"]

    def test_k_caps_output(self) -> None:
        hits = [_hit(f"a/{i}", f"s{i}") for i in range(10)]
        out = rrf_merge_weighted([("a", 1.0, hits)], k=3)
        assert len(out) == 3

    def test_empty_input_returns_empty(self) -> None:
        out = rrf_merge_weighted([], k=5)
        assert out == []

    def test_each_source_empty_returns_empty(self) -> None:
        out = rrf_merge_weighted(
            [
                ("a", 1.0, []),
                ("b", 0.8, []),
            ],
            k=5,
        )
        assert out == []


class TestRrfMergeAcrossSources:
    def test_disjoint_sources_all_contribute(self) -> None:
        a_hits = [_hit("local/x", "skill-x")]
        b_hits = [_hit("mass/y", "skill-y")]
        out = rrf_merge_weighted(
            [("local", 1.0, a_hits), ("mass", 0.8, b_hits)],
            k=5,
        )
        names = {h.name for h in out}
        assert names == {"skill-x", "skill-y"}

    def test_name_collision_dedups_to_one(self) -> None:
        a_hits = [_hit("local/x", "shared", score=0.3)]
        b_hits = [_hit("everos/x", "shared", score=0.9)]
        out = rrf_merge_weighted(
            [("local", 1.0, a_hits), ("everos", 0.9, b_hits)],
            k=5,
        )
        assert len(out) == 1
        # Representative is the higher-scoring single-source hit.
        assert out[0].score == 0.9

    def test_contributing_sources_recorded(self) -> None:
        a_hits = [_hit("local/x", "shared")]
        b_hits = [_hit("everos/x", "shared")]
        out = rrf_merge_weighted(
            [("local", 1.0, a_hits), ("everos", 0.9, b_hits)],
            k=5,
        )
        assert out[0].meta["contributing_sources"] == ["local", "everos"]

    def test_rrf_score_written_to_meta(self) -> None:
        hits = [_hit("a/1", "x")]
        out = rrf_merge_weighted([("a", 1.0, hits)], k=5)
        # RRF score for rank-1 of single source with w=1: 1/(60+1) ~= 0.01639
        assert out[0].meta["rrf_score"] == pytest.approx(1.0 / 61.0)

    def test_weight_affects_relative_ranking(self) -> None:
        """A hit ranked #3 in a high-weight source can outrank a hit
        ranked #1 in a low-weight source."""
        high_w_hits = [
            _hit("local/a", "alpha"),
            _hit("local/b", "beta"),
            _hit("local/c", "gamma"),  # rank 3 at weight 1.0
        ]
        low_w_hits = [
            _hit("mass/d", "delta"),  # rank 1 at weight 0.01
        ]
        out = rrf_merge_weighted(
            [("local", 1.0, high_w_hits), ("mass", 0.01, low_w_hits)],
            k=5,
        )
        names = [h.name for h in out]
        # local rank-3 (1/63 = ~0.0159) > mass rank-1 (0.01/61 = ~0.00016)
        # so 'gamma' must outrank 'delta'.
        assert names.index("gamma") < names.index("delta")


class TestRrfMergeDedupBy:
    def test_dedup_by_qualified_id_no_collapse_when_names_match(self) -> None:
        """When dedup_by='qualified_id', same-name from different sources
        stay separate. Demonstrates the design knob."""
        a_hits = [_hit("local/shared", "shared", score=0.3)]
        b_hits = [_hit("everos/shared", "shared", score=0.9)]
        out = rrf_merge_weighted(
            [("local", 1.0, a_hits), ("everos", 0.9, b_hits)],
            k=5,
            dedup_by="qualified_id",
        )
        assert len(out) == 2


class TestRrfRepresentativeStability:
    def test_input_hits_not_mutated(self) -> None:
        """RouterHit is frozen — fusion must construct new hits, not
        try to mutate input."""
        a_hits = [_hit("local/x", "x")]
        original_meta = a_hits[0].meta
        rrf_merge_weighted([("local", 1.0, a_hits)], k=5)
        # Input hit's meta is unchanged.
        assert original_meta == {}


# ---------------------------------------------------------------------------
# SkillForgeRouter — concurrent fan-out, safety, k cap
# ---------------------------------------------------------------------------


class _StubSource:
    def __init__(self, name: str, weight: float, hits: list[RouterHit]):
        self.name = name
        self.weight = weight
        self._hits = hits
        self.last_k = None

    async def search(self, query, history, k):
        self.last_k = k
        return list(self._hits)


class _FailingSource:
    def __init__(self, name: str, weight: float = 1.0):
        self.name = name
        self.weight = weight

    async def search(self, query, history, k):
        raise RuntimeError(f"{self.name} backend unreachable")


class _SlowSource:
    def __init__(self, name: str, hits: list[RouterHit], delay_s: float):
        self.name = name
        self.weight = 1.0
        self._hits = hits
        self._delay = delay_s

    async def search(self, query, history, k):
        await asyncio.sleep(self._delay)
        return list(self._hits)


class TestSkillForgeRouterSelect:
    async def test_fans_out_to_all_sources(self) -> None:
        a = _StubSource("local", 1.0, [_hit("local/x", "x")])
        b = _StubSource("mass", 0.8, [_hit("mass/y", "y")])
        router = SkillForgeRouter([a, b])
        out = await router.select("q", history=[], k=5)
        names = {h.name for h in out}
        assert names == {"x", "y"}

    async def test_over_fetches_per_source(self) -> None:
        """Default over_fetch_factor=2 means each source is asked for k*2."""
        a = _StubSource("local", 1.0, [_hit("local/x", "x")])
        router = SkillForgeRouter([a])
        await router.select("q", history=[], k=3)
        assert a.last_k == 6

    async def test_custom_over_fetch_factor(self) -> None:
        a = _StubSource("local", 1.0, [_hit("local/x", "x")])
        router = SkillForgeRouter([a], over_fetch_factor=4)
        await router.select("q", history=[], k=2)
        assert a.last_k == 8

    async def test_failing_source_isolated(self) -> None:
        good = _StubSource("local", 1.0, [_hit("local/x", "x")])
        bad = _FailingSource("everos")
        router = SkillForgeRouter([good, bad])
        out = await router.select("q", history=[], k=5)
        # Failed source contributed nothing; good source still surfaces.
        assert [h.name for h in out] == ["x"]

    async def test_all_failing_returns_empty(self) -> None:
        bad1 = _FailingSource("a")
        bad2 = _FailingSource("b")
        router = SkillForgeRouter([bad1, bad2])
        out = await router.select("q", history=[], k=5)
        assert out == []

    async def test_concurrent_execution(self) -> None:
        """Two slow sources should finish in roughly the slowest's time,
        not the sum, when run via SkillForgeRouter (asyncio.gather)."""
        import time

        a = _SlowSource("a", [_hit("a/x", "x")], delay_s=0.10)
        b = _SlowSource("b", [_hit("b/y", "y")], delay_s=0.10)
        router = SkillForgeRouter([a, b])
        t0 = time.monotonic()
        out = await router.select("q", history=[], k=5)
        elapsed = time.monotonic() - t0
        # If they ran serially this would be ~0.20s. Concurrent ~0.10.
        # Loose bound at 0.15 to accommodate scheduler jitter.
        assert elapsed < 0.15
        assert len(out) == 2

    async def test_k_cap_respected(self) -> None:
        many_hits = [_hit(f"local/{i}", f"s{i}") for i in range(20)]
        a = _StubSource("local", 1.0, many_hits)
        router = SkillForgeRouter([a])
        out = await router.select("q", history=[], k=5)
        assert len(out) == 5
