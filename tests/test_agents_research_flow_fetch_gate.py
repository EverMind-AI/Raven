"""Fetch gate: the state machine and the hook that acts on it.

Every behavioural test here is fed both directions - a run that should close the
gate and a run that should not - because this repo's most repeated failure is a
gate that passes its own tests while measuring something other than its name.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.gates.fetch_gate import FetchGateObserver  # noqa: E402
from research_flow.support.fetch_gate_core import FetchGate  # noqa: E402
from research_flow.support.harness_text import (  # noqa: E402
    FETCH_GATE_PREFIX,
    fetch_gate_notice,
    is_harness_authored,
    is_harness_echo,
    sufficiency_notice,
)

from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402


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
    """ "Did not fire" and "was not installed" must not look alike."""
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


# ── the release valve, through the bytes production actually hands it ──
#
# Everything above feeds the hook a BARE JSON envelope, and that is how the
# release condition can stay broken while its tests are green. What reaches
# ``before_iteration`` in production is the envelope after two layers the
# tests never applied: ``wrap_untrusted`` fences it on the way into
# ``messages``, and ``BudgetNoteObserver`` - first in the chain, against this
# gate's later slot - appends a budget line to the newest tool result before
# this gate reads it. The tests below feed those bytes.


def _fenced_ok_fetch_with_budget_note() -> str:
    """The literal content of a newest-tool-result web_fetch message on a research turn."""
    from raven.security.trust import wrap_untrusted

    return wrap_untrusted(_ok_fetch(), source="web_fetch") + "\n\n[budget: iteration 12/150 | context ~41%]"


@pytest.mark.asyncio
async def test_a_budget_annotated_fenced_fetch_still_releases_the_gate():
    """Both directions, on the production byte shape.

    A successful fetch must reopen ``web_search`` even though the fence and a
    budget note sit around it; a failed one must not. With ``json.loads`` on the
    whole body both read as "not a page", so the gate closed permanently on the
    fetches that carried a note - and its own ``gate_reopened`` counter is
    identically zero under that bug, so nothing downstream could tell a dead
    valve from a valve nothing needed.
    """
    from raven.security.trust import wrap_untrusted

    hook = FetchGateObserver(FetchGate(k=2, release_after_failed_fetches=99))
    msgs = [_search_msg(), _search_msg(), _fetch_msg(_fenced_ok_fetch_with_budget_note())]
    assert (await hook.before_iteration(_ctx(msgs))).modified_tools is None, (
        "a successful fetch wrapped in the fence and carrying a budget note did not release the gate"
    )

    # The opposite direction on the identical byte shape, so this test cannot
    # pass by the gate having stopped firing altogether.
    hook2 = FetchGateObserver(FetchGate(k=2, release_after_failed_fetches=99))
    bad = wrap_untrusted(_bad_fetch(), source="web_fetch") + "\n\n[budget: iteration 12/150 | context ~41%]"
    msgs2 = [_search_msg(), _search_msg(), _fetch_msg(bad)]
    assert (await hook2.before_iteration(_ctx(msgs2))).modified_tools is not None


def test_the_predicate_survives_every_note_an_observer_may_append():
    """The three appenders, alone and stacked.

    ``FetchFloorObserver`` and this gate append to the same message slot
    ``BudgetNoteObserver`` does, so a repair that only knew about the budget
    line would be the same fix one appender wide.
    """
    from research_flow.tools.web import fetch_result_ok

    from raven.security.trust import unwrap_untrusted, wrap_untrusted

    floor_note = "\n\n[note: 7 searches since the last page was opened - ...]"
    budget_note = "\n\n[budget: iteration 12/150 | context ~41%]"
    gate_note = "\n\n" + fetch_gate_notice()

    fenced = wrap_untrusted(_ok_fetch(), source="web_fetch")
    for label, tail in (
        ("nothing appended", ""),
        ("budget note", budget_note),
        ("fetch-floor note", floor_note),
        ("gate notice", gate_note),
        ("all three stacked", floor_note + gate_note + budget_note),
    ):
        assert fetch_result_ok(unwrap_untrusted(fenced + tail)), label

    # Still False without the unwrap: the fence itself is not a page, and a
    # caller that forgets to unwrap must not be silently rescued. And a failed
    # fetch stays failed under the same note.
    assert not fetch_result_ok(fenced)
    assert not fetch_result_ok(unwrap_untrusted(wrap_untrusted(_bad_fetch(), source="web_fetch") + budget_note))
    # A non-string never parses as a page.
    assert not fetch_result_ok(None)
    assert not fetch_result_ok(b"{}")


@pytest.mark.asyncio
async def test_notice_is_appended_once_per_firing_not_once_per_iteration():
    hook = FetchGateObserver(FetchGate(k=2))
    msgs = [_search_msg(), _search_msg()]
    before = [dict(m) for m in msgs]
    ctx = _ctx(msgs)
    notes = [
        (await hook.before_iteration(ctx)).append_note,
        (await hook.before_iteration(ctx)).append_note,
        (await hook.before_iteration(ctx)).append_note,
    ]
    assert sum(1 for n in notes if n and FETCH_GATE_PREFIX in n) == 1
    assert notes[0] == fetch_gate_notice()
    # The notice travels as ``append_note``; the hook never mutates the transcript.
    assert msgs == before


@pytest.mark.asyncio
async def test_the_pause_notice_defers_off_a_body_carrying_the_release_note():
    """The one stacking the phase order allows: sufficiency writes its release in
    after_iteration onto the newest tool result, and this gate's NEXT
    before_iteration would hang the pause on the same body - "stop opening pages,
    write" followed by "open a page". The notice defers (``notice_at`` does not
    advance) and lands on the next tool result that can carry it; the tool
    withdrawal itself is not deferred."""
    hook = FetchGateObserver(FetchGate(k=2))
    msgs = [_search_msg(), _search_msg()]
    msgs[-1]["content"] = f"{msgs[-1]['content']}\n\n{sufficiency_notice()}"
    ctx = _ctx(msgs)
    decision = await hook.before_iteration(ctx)
    assert decision.modified_tools is not None
    assert decision.append_note is None

    msgs.append(_search_msg())
    decision2 = await hook.before_iteration(ctx)
    assert decision2.append_note == fetch_gate_notice()
    notes = [decision.append_note, decision2.append_note]
    assert sum(1 for n in notes if n and FETCH_GATE_PREFIX in n) == 1
    # The hook itself never writes the notice into the transcript.
    assert FETCH_GATE_PREFIX not in "\n".join(str(m.get("content")) for m in msgs)


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


@pytest.mark.asyncio
async def test_previous_turns_tool_history_does_not_close_the_gate():
    """dr@3.4. The first-iteration scan starts at ``ctx.turn_base``, never 0.

    The session persists every earlier turn's tool messages, and the assembled
    context replays them below the current user message. A previous turn that
    ended on a long unread-search tail (the failure bucket routinely ends with
    100+ searches) must not close ``web_search`` on this turn's very first
    iteration - single-turn tests cannot see this, because there the slice IS
    the whole list.
    """
    history = [_search_msg() for _ in range(20)]
    ctx = AgentHookContext(session_key="test", turn_base=len(history))
    ctx.messages = history + [{"role": "user", "content": "next question"}]
    ctx.tools = _tools()
    hook = FetchGateObserver(FetchGate(k=15))
    decision = await hook.before_iteration(ctx)
    assert decision.modified_tools is None, "prior-turn searches closed this turn's gate"

    # Same shape with turn_base=0 (a single-turn run) must still close it -
    # asserted so this test cannot pass by the gate never firing at all.
    ctx2 = AgentHookContext(session_key="test", turn_base=0)
    ctx2.messages = [_search_msg() for _ in range(20)]
    ctx2.tools = _tools()
    hook2 = FetchGateObserver(FetchGate(k=15))
    decision2 = await hook2.before_iteration(ctx2)
    assert decision2.modified_tools is not None
