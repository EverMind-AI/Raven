"""Fetch gate: the state machine and the hook that acts on it.

Every behavioural test here is fed both directions - a run that should close the
gate and a run that should not - because this repo's most repeated failure is a
gate that passes its own tests while measuring something other than its name.
"""

from __future__ import annotations

import json

import pytest

from raven.agent.fetch_gate import FetchGate
from raven.agent.flow.fetch_gate import FetchGateObserver
from raven.agent.harness_text import (
    FETCH_GATE_PREFIX,
    fetch_gate_notice,
    is_harness_authored,
    is_harness_echo,
)
from raven.agent.hook.base import AgentHookContext


def _ok_fetch() -> str:
    return json.dumps({"url": "https://example.com", "content": "x", "length": 1})


def _bad_fetch() -> str:
    return json.dumps({"url": "https://example.com", "error": "404"})


def _search_msg() -> dict:
    return {"role": "tool", "name": "web_search", "content": "1. A\n2. B"}


def _fetch_msg(body: str) -> dict:
    return {"role": "tool", "name": "web_fetch", "content": body}


def _tools() -> list[dict]:
    return [
        {"type": "function", "function": {"name": "web_search"}},
        {"type": "function", "function": {"name": "web_fetch"}},
    ]


# ── state machine ────────────────────────────────────────────────────


def test_gate_closes_only_at_the_threshold():
    g = FetchGate(k=3)
    for _ in range(2):
        g.observe_search()
        assert g.evaluate() is False
    g.observe_search()
    assert g.evaluate() is True
    assert g.fired == 1
    assert g.streak_at_fire == [3]


def test_a_successful_fetch_reopens_and_a_failed_one_does_not():
    """The caliber decision, asserted in both directions.

    ``fetch_floor`` zeroes its streak on any ``web_fetch``; this gate zeroes only
    on a successful one. If that ever regresses, one dead link reopens search for
    the rest of the turn and the gate is satisfiable without reading anything.
    """
    g = FetchGate(k=2, release_after_failed_fetches=99)
    g.observe_search()
    g.observe_search()
    assert g.evaluate() is True

    g.observe_fetch(ok=False)
    assert g.evaluate() is True, "a failed fetch must not reopen search"

    g.observe_fetch(ok=True)
    assert g.evaluate() is False
    assert g.streak == 0
    assert g.opened == 1


def test_release_valve_opens_after_consecutive_failures_and_stays_open():
    g = FetchGate(k=2, release_after_failed_fetches=2)
    g.observe_search()
    g.observe_search()
    assert g.evaluate() is True
    g.observe_fetch(ok=False)
    g.observe_fetch(ok=False)
    assert g.released is True
    assert g.evaluate() is False
    for _ in range(50):
        g.observe_search()
    assert g.evaluate() is False, "release is for the rest of the turn"


def test_a_success_between_failures_resets_the_release_count():
    """Consecutive, not cumulative. A turn that opens pages between dead links is
    working, and must not be released as if it were stuck."""
    g = FetchGate(k=2, release_after_failed_fetches=2)
    g.observe_search()
    g.observe_search()
    assert g.evaluate() is True
    g.observe_fetch(ok=False)
    g.observe_fetch(ok=True)
    g.observe_search()
    g.observe_search()
    assert g.evaluate() is True
    g.observe_fetch(ok=False)
    assert g.released is False


def test_fired_counts_transitions_not_gated_iterations():
    g = FetchGate(k=1)
    g.observe_search()
    for _ in range(5):
        assert g.evaluate() is True
    assert g.fired == 1, "staying closed is one firing, not five"


def test_counters_are_emitted_before_the_gate_ever_fires():
    """"Did not fire" and "was not installed" must not look alike."""
    c = FetchGate(k=15).counters()
    assert c["gate_fired"] == 0
    assert c["gate_released"] is False
    assert c["gate_streak_at_fire"] == []


def test_reset_clears_the_release_flag():
    g = FetchGate(k=1, release_after_failed_fetches=1)
    g.observe_search()
    g.evaluate()
    g.observe_fetch(ok=False)
    assert g.released is True
    g.reset()
    assert g.released is False


# ── hook ─────────────────────────────────────────────────────────────


def _ctx(messages: list[dict]) -> AgentHookContext:
    ctx = AgentHookContext(session_key="test")
    ctx.messages = messages
    ctx.tools = _tools()
    return ctx


@pytest.mark.asyncio
async def test_hook_withholds_web_search_and_leaves_web_fetch():
    hook = FetchGateObserver(FetchGate(k=2))
    msgs = [_search_msg(), _search_msg()]
    decision = await hook.before_iteration(_ctx(msgs))
    names = [t["function"]["name"] for t in decision.modified_tools]
    assert names == ["web_fetch"]


@pytest.mark.asyncio
async def test_hook_does_not_withhold_below_the_threshold():
    hook = FetchGateObserver(FetchGate(k=5))
    decision = await hook.before_iteration(_ctx([_search_msg(), _search_msg()]))
    assert decision.modified_tools is None


@pytest.mark.asyncio
async def test_hook_reads_fetch_success_from_the_tool_envelope():
    """Both directions through the real envelope shape, not a stub boolean."""
    hook = FetchGateObserver(FetchGate(k=2, release_after_failed_fetches=99))
    msgs = [_search_msg(), _search_msg(), _fetch_msg(_bad_fetch())]
    assert (await hook.before_iteration(_ctx(msgs))).modified_tools is not None

    hook2 = FetchGateObserver(FetchGate(k=2, release_after_failed_fetches=99))
    msgs2 = [_search_msg(), _search_msg(), _fetch_msg(_ok_fetch())]
    assert (await hook2.before_iteration(_ctx(msgs2))).modified_tools is None


@pytest.mark.asyncio
async def test_notice_is_appended_once_per_firing_not_once_per_iteration():
    hook = FetchGateObserver(FetchGate(k=2))
    msgs = [_search_msg(), _search_msg()]
    ctx = _ctx(msgs)
    await hook.before_iteration(ctx)
    await hook.before_iteration(ctx)
    await hook.before_iteration(ctx)
    joined = "\n".join(str(m.get("content")) for m in msgs)
    assert joined.count(FETCH_GATE_PREFIX) == 1


@pytest.mark.asyncio
async def test_counters_land_in_metadata_every_iteration():
    hook = FetchGateObserver(FetchGate(k=5))
    ctx = _ctx([_search_msg()])
    await hook.before_iteration(ctx)
    assert ctx.metadata["fetch_gate"]["gate_fired"] == 0
    assert ctx.metadata["fetch_gate"]["gate_k"] == 5


@pytest.mark.asyncio
async def test_a_new_turn_resets_the_gate_that_outlives_it():
    gate = FetchGate(k=2)
    hook = FetchGateObserver(gate)
    ctx = _ctx([_search_msg(), _search_msg()])
    assert (await hook.before_iteration(ctx)).modified_tools is not None
    # Fresh metadata is what a second question looks like to this hook.
    ctx2 = _ctx([])
    assert (await hook.before_iteration(ctx2)).modified_tools is None
    assert gate.streak == 0


@pytest.mark.asyncio
async def test_gate_reports_rather_than_pretends_when_the_tool_is_absent():
    hook = FetchGateObserver(FetchGate(k=2))
    ctx = _ctx([_search_msg(), _search_msg()])
    ctx.tools = [{"type": "function", "function": {"name": "web_fetch"}}]
    decision = await hook.before_iteration(ctx)
    assert decision.modified_tools is None
    assert ctx.metadata["fetch_gate"]["gate_tool_absent"] is True


# ── harness text ─────────────────────────────────────────────────────


def test_the_notice_is_recognised_as_an_echo_but_not_as_an_evidence_body():
    """The inverted membership, asserted so it cannot be "tidied up" later.

    Strict path yes: a salvage model handed this sentence could return it, which
    is exactly how ``hle-256`` shipped the saturation notice as a final answer.
    Permissive path no: the notice rides on a real tool result, so condemning the
    body would drop evidence rather than cost one item of look-back.
    """
    notice = fetch_gate_notice()
    assert is_harness_echo(notice) is True
    assert is_harness_authored(f"real search results\n\n{notice}") is False


def test_an_answer_that_merely_discusses_the_notice_is_not_an_echo():
    assert is_harness_echo(f"{fetch_gate_notice()} - so I opened the first hit.") is False


def test_the_notice_carries_no_varying_number():
    """It takes no argument on purpose; see ``fetch_gate_notice``."""
    assert fetch_gate_notice() == fetch_gate_notice()
    assert not any(ch.isdigit() for ch in fetch_gate_notice())
