"""The evidence floor: what it counts, when it bounces, and where it sits in the chain.

The gate exists because every knob that separated ``high`` from ``max`` needed a
trigger that a 5-question batch never supplied, so the properties pinned here are the
ones that make it different in kind: it is consulted on EVERY draft, its counts agree
with the trail's ``pages_ok`` / ``thin_pages`` predicates, its bounce keeps the draft so
the model extends rather than restarts, and it releases past the cap instead of
spinning.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.config import FlowConfig  # noqa: E402
from research_flow.flow import ToolHandles, build_chain  # noqa: E402
from research_flow.gates.evidence_floor import EvidenceFloorGate  # noqa: E402
from research_flow.gates.finalize import ForcedFinalizeGate  # noqa: E402
from research_flow.gates.verify import DraftReviewerGate  # noqa: E402
from research_flow.state import SessionStore  # noqa: E402

from raven.contracts.loop_hooks import AgentHookContext, HookDecision  # noqa: E402

DRAFT = "The answer is 42, per the pages opened above."


def _page(url: str, chars: int = 2000) -> dict:
    return {
        "role": "tool",
        "name": "web_fetch",
        "content": json.dumps({"url": url, "finalUrl": url, "length": chars, "text": "x" * chars}),
    }


def _failed(url: str) -> dict:
    return {"role": "tool", "name": "web_fetch", "content": json.dumps({"error": "403", "url": url})}


def _pages(n: int, *, sites: int | None = None, chars: int = 2000) -> list[dict]:
    sites = n if sites is None else sites
    return [_page(f"https://site{i % sites}.example/p{i}", chars) for i in range(n)]


class _Response:
    def __init__(self, content: str, has_tool_calls: bool = False) -> None:
        self.content = content
        self.has_tool_calls = has_tool_calls
        self.finish_reason = "stop"


def _run(
    gate: EvidenceFloorGate,
    messages: list[dict],
    *,
    metadata: dict | None = None,
    turn_base: int = 0,
    response: _Response | None = None,
) -> tuple[dict, HookDecision]:
    meta = {} if metadata is None else metadata
    ctx = AgentHookContext(
        session_key="t",
        iteration=5,
        messages=[{"role": "user", "content": "how many?"}, *messages],
        response=response or _Response(DRAFT),
        metadata=meta,
        turn_base=turn_base,
    )
    return meta, asyncio.run(gate.after_iteration(ctx))


def test_a_draft_on_enough_pages_and_sites_passes_untouched():
    meta, decision = _run(EvidenceFloorGate(min_pages=3, min_domains=2), _pages(3))
    assert not decision.rollback and decision.rollback_inject is None
    assert meta["evidence_floor"] == {"checks": 1, "rollbacks": 0, "pages": 3, "domains": 3, "met": True}


def test_too_few_pages_bounces_the_draft_and_keeps_it_in_history():
    """The bounce is the reviewer's shape: the draft stays as the assistant turn and a
    user note names the shortfall, so the re-sample extends the research rather than
    restarting it -- and the note carries the numbers, not just a verdict."""
    meta, decision = _run(EvidenceFloorGate(min_pages=3, min_domains=2), _pages(2))
    assert decision.rollback is True
    roles = [m["role"] for m in decision.rollback_inject]
    assert roles == ["assistant", "user"]
    assert decision.rollback_inject[0]["content"] == DRAFT
    note = decision.rollback_inject[1]["content"]
    assert "2 readable page(s) from 2 distinct site(s)" in note and "at least 3 readable pages from 2" in note
    assert meta["evidence_floor"]["rollbacks"] == 1 and "met" not in meta["evidence_floor"]


def test_enough_pages_from_too_few_sites_bounces_too():
    meta, decision = _run(EvidenceFloorGate(min_pages=3, min_domains=3), _pages(4, sites=2))
    assert decision.rollback is True
    assert (meta["evidence_floor"]["pages"], meta["evidence_floor"]["domains"]) == (4, 2)


def test_thin_and_failed_fetches_do_not_count():
    """Same bar as the trail's ``thin_pages``: a 399-character stub is not a page, and a
    fetch that returned an error is not one either, whatever its URL."""
    messages = [*_pages(2), _page("https://thin.example/a", chars=399), _failed("https://dead.example/b")]
    meta, decision = _run(EvidenceFloorGate(min_pages=3, min_domains=3), messages)
    assert decision.rollback is True
    assert (meta["evidence_floor"]["pages"], meta["evidence_floor"]["domains"]) == (2, 2)


def test_sites_are_hosts_with_www_folded_and_case_ignored():
    messages = [
        _page("https://www.Example.com/a"),
        _page("https://example.com/b"),
        _page("https://EXAMPLE.com/c"),
        _page("https://other.org/d"),
    ]
    meta, decision = _run(EvidenceFloorGate(min_pages=4, min_domains=3), messages)
    assert decision.rollback is True
    assert meta["evidence_floor"]["domains"] == 2


def test_the_cap_releases_the_draft_and_records_the_shortfall():
    """Past ``max_rollbacks`` the draft ships to the reviewer with ``unmet`` on the
    record: a topic the corpus cannot supply degrades to the mode below, it does not
    spin, and the record says which happened."""
    gate = EvidenceFloorGate(min_pages=5, min_domains=2, max_rollbacks=2)
    meta: dict = {}
    for expected in (1, 2):
        _, decision = _run(gate, _pages(1), metadata=meta)
        assert decision.rollback is True and meta["evidence_floor"]["rollbacks"] == expected
    _, decision = _run(gate, _pages(1), metadata=meta)
    assert decision.rollback is False and decision.rollback_inject is None
    assert meta["evidence_floor"]["unmet"] is True and meta["evidence_floor"]["checks"] == 3
    assert any("released" in n for n in decision.notes)


def test_a_tool_calling_or_answerless_response_is_not_a_draft():
    """Research iterations are the salvage gate's and the loop's business; the floor
    writes nothing so a turn that never drafted shows no floor state at all."""
    gate = EvidenceFloorGate(min_pages=3, min_domains=2)
    for response in (_Response("", has_tool_calls=True), _Response(""), _Response("   ")):
        meta, decision = _run(gate, _pages(0), response=response)
        assert not decision.rollback and "evidence_floor" not in meta


def test_only_this_turns_pages_count():
    """``turn_base`` scopes the count: a follow-up turn cannot ship on the previous
    turn's reading, the same scope rule the sufficiency and fetch gates keep."""
    earlier = _pages(5)
    gate = EvidenceFloorGate(min_pages=3, min_domains=2)
    meta, decision = _run(gate, [*earlier, *_pages(1)], turn_base=1 + len(earlier))
    assert decision.rollback is True and meta["evidence_floor"]["pages"] == 1


# --------------------------------------------------------------------------
# Wiring
# --------------------------------------------------------------------------


class _StubProvider:
    async def chat_with_retry(self, **kwargs):
        raise AssertionError("no gate should call the provider during assembly")


def _inner(hook):
    while hasattr(hook, "inner"):
        hook = hook.inner
    return hook


def _kinds(observers) -> list[type]:
    return [type(_inner(o)) for o in observers]


def test_off_by_default_the_chain_has_no_floor(tmp_path):
    observers = build_chain(
        FlowConfig(enabled=True),
        _StubProvider(),
        max_iterations=40,
        context_window_tokens=65536,
        tools=ToolHandles(),
        store=SessionStore(tmp_path),
    )
    assert EvidenceFloorGate not in _kinds(observers)


def test_on_it_sits_after_salvage_and_before_the_reviewer(tmp_path):
    """An answerless terminal is salvaged, never floored; a draft below the floor is
    bounced, never reviewed -- the order is the contract, and the floor's knobs reach
    the gate as configured."""
    cfg = FlowConfig(enabled=True)
    cfg.evidence_floor.enabled = True
    cfg.evidence_floor.min_pages = 7
    cfg.evidence_floor.min_domains = 3
    cfg.evidence_floor.max_rollbacks = 1
    observers = build_chain(
        cfg,
        _StubProvider(),
        max_iterations=40,
        context_window_tokens=65536,
        tools=ToolHandles(),
        store=SessionStore(tmp_path),
    )
    kinds = _kinds(observers)
    assert kinds.index(ForcedFinalizeGate) < kinds.index(EvidenceFloorGate) < kinds.index(DraftReviewerGate)
    floor = _inner(observers[kinds.index(EvidenceFloorGate)])
    assert (floor._min_pages, floor._min_domains, floor._max_rollbacks) == (7, 3, 1)


def test_the_floor_is_reachable_from_a_mode_overlay():
    """The camelCase spelling a ``modes/*.json`` file uses is the one the overlay door accepts."""
    base = FlowConfig(enabled=True)
    flow = base.with_overlay({"evidenceFloor": {"enabled": True, "minPages": 18, "minDomains": 8, "maxRollbacks": 2}})
    assert (flow.evidence_floor.enabled, flow.evidence_floor.min_pages, flow.evidence_floor.min_domains) == (
        True,
        18,
        8,
    )
    assert base.evidence_floor.enabled is False


def _tools(*names: str) -> list[dict]:
    return [{"type": "function", "function": {"name": n}} for n in names]


def test_the_floor_does_not_bounce_into_a_search_the_model_cannot_make():
    """The bounce note says "search for and open further sources"; on an iteration
    whose tool list carries no web tool that is an instruction with no compliant
    action, and two bounces would only be spent before the release."""
    gate = EvidenceFloorGate(min_pages=3, min_domains=2)
    meta: dict = {}
    asyncio.run(
        gate.before_iteration(AgentHookContext(session_key="t", iteration=1, tools=_tools("read_file"), metadata=meta))
    )
    meta, decision = _run(gate, _pages(1), metadata=meta)
    assert not decision.rollback and meta["evidence_floor"]["skipped"] == "web_tools_absent"
    assert meta["evidence_floor"]["checks"] == 0, "a skipped draft is not a checked draft"

    # The next iteration offers the tools again: the floor is back.
    asyncio.run(
        gate.before_iteration(
            AgentHookContext(session_key="t", iteration=2, tools=_tools("web_search", "web_fetch"), metadata=meta)
        )
    )
    meta, decision = _run(gate, _pages(1), metadata=meta)
    assert decision.rollback is True and meta["evidence_floor"]["checks"] == 1
