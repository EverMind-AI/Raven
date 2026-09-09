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

import json
import sys
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.config import FlowConfig  # noqa: E402
from research_flow.flow import ToolHandles, build_chain, evidence_round_for  # noqa: E402
from research_flow.gates.spin_breaker import SpinEntryBreaker  # noqa: E402
from research_flow.gates.verify import _EVIDENCE_ROUND_PROMPT, _REVISION_PROMPT, DraftReviewerGate  # noqa: E402
from research_flow.state import SessionStore  # noqa: E402
from research_flow.support.evidence_round import EvidenceRound  # noqa: E402
from research_flow.tools.web import WebSearchTool, set_current_session  # noqa: E402

from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402


class _StubProvider:
    async def chat_with_retry(self, **kwargs):  # pragma: no cover - never called here
        raise AssertionError("no provider call expected")


class _SerperTransport(httpx.AsyncBaseTransport):
    """Live-web service stub that records every request body it was sent."""

    def __init__(self) -> None:
        self.bodies: list[dict] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"organic": [{"title": f"T{i}", "link": f"https://x/{i}", "snippet": "s"} for i in range(1, 6)]},
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


def test_flow_on_alone_does_not_create_a_round(tmp_path):
    cfg = FlowConfig(enabled=True)
    assert evidence_round_for(cfg) is None
    observers = build_chain(
        cfg,
        _StubProvider(),
        max_iterations=40,
        context_window_tokens=65536,
        tools=ToolHandles(),
        store=SessionStore(tmp_path),
    )
    gate = next(o for o in observers if isinstance(o, DraftReviewerGate))
    assert gate._evidence_round is None


def test_enabling_it_shares_one_object_across_gate_tool_and_breaker(tmp_path):
    config = FlowConfig(enabled=True)
    config.verify.evidence_round = True
    config.spin_breaker.enabled = True

    shared = evidence_round_for(config)
    observers = build_chain(
        config,
        _StubProvider(),
        max_iterations=40,
        context_window_tokens=65536,
        tools=ToolHandles(),
        store=SessionStore(tmp_path),
        evidence_round=shared,
    )

    gate = next(o for o in observers if isinstance(o, DraftReviewerGate))
    breaker = next(o for o in observers if isinstance(o, SpinEntryBreaker))

    assert shared is not None
    # Identity, not equality: the gate opens the round and the tool spends it, so
    # two equal-but-separate objects would be a feature that never transmits.
    assert gate._evidence_round is shared
    assert breaker._evidence_round is shared
    set_current_session("t")
    tool = WebSearchTool(evidence_round_factory=lambda: shared)
    assert tool._state().evidence_round is shared


def test_the_round_cannot_exist_when_verify_is_off(tmp_path):
    config = FlowConfig(enabled=True)
    config.verify.enabled = False
    config.verify.evidence_round = True
    # Two futurex profiles ship exactly this shape. Nothing opens a round when the
    # gate that opens it was never built.
    assert evidence_round_for(config) is None
    observers = build_chain(
        config,
        _StubProvider(),
        max_iterations=40,
        context_window_tokens=65536,
        tools=ToolHandles(),
        store=SessionStore(tmp_path),
    )
    assert not any(isinstance(o, DraftReviewerGate) for o in observers)


# --------------------------------------------------------------------------
# Depth: the round raises how deep we look, never how much we render
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_deep_round_is_not_served_from_the_shallow_cache(monkeypatch):
    """The repeat cache is keyed on the requested depth, resolved before the lookup.

    This is the failure the ordering exists to prevent: the model searches a term,
    is rejected, and re-issues the same term inside a round. If the depth were
    resolved after the lookup, the round would replay the shallow answer and log as
    though it had gone deep.
    """
    set_current_session("t")
    transport = _SerperTransport()
    _patch_client(monkeypatch, transport)
    er = EvidenceRound(depth=50, searches=2)
    tool = WebSearchTool(
        api_key="k",
        cross_query_dedup=True,
        search_depth=20,
        repeat_notice=True,
        evidence_round_factory=lambda: er,
    )

    await tool.execute(query="same terms")
    er.open()
    second = await tool.execute(query="same terms")

    assert "[repeat]" not in second, "a deeper request is not a repeat of a shallow one"
    assert len(transport.bodies) == 2, "the deep request must reach the service"


@pytest.mark.asyncio
async def test_a_repeat_inside_a_round_still_replays_and_does_not_spend_budget(monkeypatch):
    set_current_session("t")
    transport = _SerperTransport()
    _patch_client(monkeypatch, transport)
    er = EvidenceRound(depth=50, searches=3)
    tool = WebSearchTool(
        api_key="k",
        cross_query_dedup=True,
        search_depth=20,
        repeat_notice=True,
        evidence_round_factory=lambda: er,
    )
    er.open()

    await tool.execute(query="same terms")
    replayed = await tool.execute(query="same terms")

    assert "[repeat]" in replayed
    assert len(transport.bodies) == 1
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
    set_current_session("t")
    sent: list[int] = []

    class _NumTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            import json as _json

            sent.append(_json.loads(request.content)["num"])
            return httpx.Response(200, json={"organic": [{"title": "t", "link": "https://x/1"}]})

    transport = _NumTransport()
    real = httpx.AsyncClient
    er = EvidenceRound(depth=50, searches=1)
    tool = WebSearchTool(api_key="k", evidence_round_factory=lambda: er)
    er.open()

    import research_flow.tools.web as web_mod

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
