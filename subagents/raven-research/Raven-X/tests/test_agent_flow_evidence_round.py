"""dr@2.8 sanctioned retrieval round: budget, wiring, and the two asymmetries.

Every assertion here was written against a way the feature can be present and
still do nothing -- shipped, logged, and returning what it was meant to replace.
Three of those are structural rather than hypothetical and get a test each:

* the depth resolved after the repeat-search cache lookup, so a round serves the
  shallow result the same query cached earlier in the turn;
* the depth applied to the live-web path, where the endpoint ignores ``num`` and
  a deeper request returns the same eight rows;
* the round left on for the anchor, which would move the reference frame the
  whole measurement is differenced against.
"""

from __future__ import annotations

import httpx
import pytest

from raven.agent.evidence_round import EvidenceRound
from raven.agent.flow import build_dr_flow
from raven.agent.flow.spin_breaker import SpinEntryBreaker
from raven.agent.flow.verify import _EVIDENCE_ROUND_PROMPT, _REVISION_PROMPT, DraftReviewerGate
from raven.agent.hook.base import AgentHookContext
from raven.agent.tools.web import WebSearchTool
from raven.config.raven import DRFlowConfig


class _StubProvider:
    async def chat_with_retry(self, **kwargs):  # pragma: no cover - never called here
        raise AssertionError("no provider call expected")


class _CountingTransport(httpx.AsyncBaseTransport):
    """Corpus service stub that records the ``k`` it was asked for."""

    def __init__(self) -> None:
        self.ks: list[str | None] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        k = request.url.params.get("k")
        self.ks.append(k)
        # Returns as many rows as it was asked for, so a deeper request produces
        # visibly different text. A stub that ignored ``k`` would let a test assert
        # "the round went deep" while proving only that a call happened.
        n = int(k or 5)
        return httpx.Response(
            200,
            json={
                "results": [
                    {"docid": f"d{i}", "title": f"T{i}", "url": f"https://x/{i}", "snippet": "s"}
                    for i in range(1, n + 1)
                ]
            },
        )


def _patch_client(monkeypatch, transport):
    real = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("proxy", None)
        return real(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


# --------------------------------------------------------------------------
# Budget object
# --------------------------------------------------------------------------


def test_round_is_inactive_until_opened_and_bounded_once_it_is():
    er = EvidenceRound(depth=50, searches=2)
    assert not er.active

    er.open()
    assert er.active and er.opened == 1

    er.consume()
    er.consume()
    assert not er.active, "the search budget is the bound; an exhausted round must close"
    assert er.deep_searches == 2

    er.consume()
    assert er.deep_searches == 2, "spending past the bound must not extend it"


def test_reset_closes_the_round_but_keeps_the_counters():
    er = EvidenceRound(searches=3)
    er.open()
    er.consume()
    er.note_spin_pass()

    er.reset()

    assert not er.active
    # The counters outlive the turn on purpose: they are the only evidence that the
    # feature fired at all, and a per-turn wipe would make "never fired" and "fired
    # and was cleared" identical in the record.
    assert er.snapshot() == {"rounds_opened": 1, "deep_searches": 1, "spin_passes": 1}


# --------------------------------------------------------------------------
# Wiring: one object, three holders, and nothing at all on the anchor
# --------------------------------------------------------------------------


def test_flow_on_alone_does_not_create_a_round():
    assembly = build_dr_flow(
        DRFlowConfig(enabled=True), _StubProvider(), max_iterations=40, context_window_tokens=65536
    )
    assert assembly.web_search_kwargs["evidence_round"] is None
    gate = next(o for o in assembly.observers if isinstance(o, DraftReviewerGate))
    assert gate._evidence_round is None


def test_enabling_it_shares_one_object_across_gate_tool_and_breaker():
    config = DRFlowConfig(enabled=True)
    config.verify.evidence_round = True
    config.spin_breaker.enabled = True
    assembly = build_dr_flow(config, _StubProvider(), max_iterations=40, context_window_tokens=65536)

    shared = assembly.web_search_kwargs["evidence_round"]
    gate = next(o for o in assembly.observers if isinstance(o, DraftReviewerGate))
    breaker = next(o for o in assembly.observers if isinstance(o, SpinEntryBreaker))

    assert shared is not None
    # Identity, not equality: the gate opens the round and the tool spends it, so
    # two equal-but-separate objects would be a feature that never transmits.
    assert gate._evidence_round is shared
    assert breaker._evidence_round is shared


def test_the_round_cannot_exist_when_verify_is_off():
    config = DRFlowConfig(enabled=True)
    config.verify.enabled = False
    config.verify.evidence_round = True
    assembly = build_dr_flow(config, _StubProvider(), max_iterations=40, context_window_tokens=65536)
    # Two futurex profiles ship exactly this shape. Nothing opens a round when the
    # gate that opens it was never built.
    assert assembly.web_search_kwargs["evidence_round"] is None


# --------------------------------------------------------------------------
# Depth: the round raises how deep we look, never how much we render
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_round_raises_the_request_depth_and_nothing_else(monkeypatch):
    transport = _CountingTransport()
    _patch_client(monkeypatch, transport)
    er = EvidenceRound(depth=50, searches=1)
    tool = WebSearchTool(
        corpus_endpoint="http://local:8765",
        cross_query_dedup=True,
        search_depth=20,
        evidence_round=er,
    )

    ordinary = await tool.execute(query="q1")
    er.open()
    inside = await tool.execute(query="q2")
    await tool.execute(query="q3")

    assert transport.ks == ["20", "50", "20"], (
        "the round lifts the depth for its budget and the turn returns to the default"
    )
    assert er.deep_searches == 1
    # The reason this composition is safe: the extra depth is spent on finding
    # unseen documents, not on printing more of them.
    assert ordinary.count("https://x/") == inside.count("https://x/") == 5


@pytest.mark.asyncio
async def test_a_round_is_inert_without_cross_query_dedup(monkeypatch):
    """Depth alone would render the head of a longer list, i.e. more context.

    That is the option the offline pricing rejected: more rows measured lower than
    dedup at unchanged width, and it feeds the failure mode context overflow
    prevention exists to stop. So the round can only ever deepen a search whose
    extra results will be spent on skipping.
    """
    transport = _CountingTransport()
    _patch_client(monkeypatch, transport)
    er = EvidenceRound(depth=50, searches=1)
    tool = WebSearchTool(corpus_endpoint="http://local:8765", evidence_round=er)
    er.open()

    await tool.execute(query="q1")

    assert transport.ks == ["5"]


@pytest.mark.asyncio
async def test_a_deep_round_is_not_served_from_the_shallow_cache(monkeypatch):
    """The repeat cache is keyed on the requested depth, resolved before the lookup.

    This is the failure the ordering exists to prevent: the model searches a term,
    is rejected, and re-issues the same term inside a round. If the depth were
    resolved after the lookup, the round would replay the shallow answer and log as
    though it had gone deep.
    """
    transport = _CountingTransport()
    _patch_client(monkeypatch, transport)
    er = EvidenceRound(depth=50, searches=2)
    tool = WebSearchTool(
        corpus_endpoint="http://local:8765",
        cross_query_dedup=True,
        search_depth=20,
        repeat_notice=True,
        evidence_round=er,
    )

    await tool.execute(query="same terms")
    er.open()
    second = await tool.execute(query="same terms")

    assert "[repeat]" not in second, "a deeper request is not a repeat of a shallow one"
    assert transport.ks == ["20", "50"], "the deep request must reach the service"


@pytest.mark.asyncio
async def test_a_repeat_inside_a_round_still_replays_and_does_not_spend_budget(monkeypatch):
    transport = _CountingTransport()
    _patch_client(monkeypatch, transport)
    er = EvidenceRound(depth=50, searches=3)
    tool = WebSearchTool(
        corpus_endpoint="http://local:8765",
        cross_query_dedup=True,
        search_depth=20,
        repeat_notice=True,
        evidence_round=er,
    )
    er.open()

    await tool.execute(query="same terms")
    replayed = await tool.execute(query="same terms")

    assert "[repeat]" in replayed
    assert transport.ks == ["50"]
    assert er.deep_searches == 1, "a cache replay issues no request and must cost nothing"


@pytest.mark.asyncio
async def test_live_web_depth_is_not_raised_by_a_round():
    """``num`` does nothing on the endpoint this build uses.

    Probed directly: 10, 20, 50 and 100 all returned HTTP 200 with 7-8 organic
    results, while ``page=2`` returned the next ten. Sending a larger ``num`` from
    a round would look deep in the ledger and return the same eight rows. If this
    path is ever deepened it must be by pagination, and this test should be
    replaced rather than deleted.
    """
    sent: list[int] = []

    class _SerperTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            import json as _json

            sent.append(_json.loads(request.content)["num"])
            return httpx.Response(200, json={"organic": [{"title": "t", "link": "https://x/1"}]})

    transport = _SerperTransport()
    real = httpx.AsyncClient
    er = EvidenceRound(depth=50, searches=1)
    tool = WebSearchTool(api_key="k", evidence_round=er)
    er.open()

    import raven.agent.tools.web as web_mod

    orig = web_mod.httpx.AsyncClient
    web_mod.httpx.AsyncClient = lambda **kw: real(transport=transport, **{k: v for k, v in kw.items() if k != "proxy"})
    try:
        await tool.execute(query="q")
    finally:
        web_mod.httpx.AsyncClient = orig

    assert sent == [5], "a round changes the live-web request width by nothing at all"


# --------------------------------------------------------------------------
# The two components that must change behaviour inside a round
# --------------------------------------------------------------------------


def _breaker_ctx(text: str, iteration: int = 40) -> AgentHookContext:
    class _Resp:
        has_tool_calls = True
        content = text
        reasoning_content = None

    return AgentHookContext(session_key="s", iteration=iteration, response=_Resp(), messages=[])


def test_spin_breaker_stands_down_inside_a_round_and_records_it():
    er = EvidenceRound(searches=2)
    breaker = SpinEntryBreaker(max_iterations=50, min_entity_overlap=0, evidence_round=er)
    # One metadata dict across scans: the hit history lives on the turn, and a fresh
    # context per call would keep the breaker permanently below its entry criterion.
    shared_meta: dict = {}

    first = _breaker_ctx("let me start over with Alpha Beta Gamma")
    first.metadata = shared_meta
    breaker._scan(first)  # first hit only records
    er.open()
    second = _breaker_ctx("let me start over with Alpha Beta Gamma")
    second.metadata = shared_meta
    decision = breaker._scan(second)

    assert decision.rollback is False
    assert decision.notes == ["spin_breaker_pass_evidence_round"]
    assert er.spin_passes == 1
    assert shared_meta["spin_breaker"]["triggers"] == 0


def test_spin_breaker_still_fires_outside_a_round():
    er = EvidenceRound(searches=2)
    breaker = SpinEntryBreaker(max_iterations=50, min_entity_overlap=0, evidence_round=er)
    shared_meta: dict = {}

    for _ in range(2):
        ctx = _breaker_ctx("let me start over with Alpha Beta Gamma")
        ctx.metadata = shared_meta
        decision = breaker._scan(ctx)

    assert decision.rollback is True, "a closed round must not disarm the breaker"
    assert er.spin_passes == 0


@pytest.mark.parametrize(
    ("configured", "expected"),
    [(False, _REVISION_PROMPT), (True, _EVIDENCE_ROUND_PROMPT)],
)
def test_the_bounce_prompt_follows_the_configuration(configured, expected):
    # The two differ in exactly one respect - whether retrieval is permitted - so a
    # profile that opts in must not keep receiving the prohibition, and one that
    # does not must not silently gain the permission.
    er = EvidenceRound() if configured else None
    gate = DraftReviewerGate(_StubProvider(), evidence_round=er)
    assert gate._evidence_round is er
    assert ("You may now run additional searches" in expected) is configured
