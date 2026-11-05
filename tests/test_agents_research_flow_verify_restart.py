"""``verifyRestart``: start the turn over until the reviewer passes the answer it delivers.

The loop owns the rerun (fresh clock, the question alone); the flow owns the test, read
off its own reviewer's record of the attempt that just ended.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow import flow as flow_mod  # noqa: E402
from research_flow.config import FlowConfig, VerifyRestartConfig  # noqa: E402
from research_flow.flow import ResearchFlowParticipant  # noqa: E402

from raven.agent.loop import TURN_BUDGETS_KEY  # noqa: E402
from raven.agent.loop.bundles import HostWiring, ToolWiring, TurnPolicy  # noqa: E402
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest  # noqa: E402


def _budgets(slice_: dict) -> dict:
    return flow_mod._turn_cargo(FlowConfig.from_slice(slice_), "q")[TURN_BUDGETS_KEY]


def test_off_by_default_leaves_the_dead_end_budget_as_it_was():
    assert _budgets({}) == {
        "wall_clock_seconds": None,
        "dead_end_retries": 1,
        "dead_end_reasons": [],
        "rerun_fresh_clock": False,
    }


def test_on_it_budgets_the_attempts_and_a_fresh_clock():
    budgets = _budgets({"verifyRestart": {"enabled": True}, "wallClockSeconds": 3600})
    assert budgets["dead_end_retries"] == 4, "five attempts, the first included"
    assert budgets["rerun_fresh_clock"] is True
    assert budgets["dead_end_reasons"] == [], "every dead end still reruns"


def test_with_dead_ends_switched_off_only_unverified_attempts_rerun():
    budgets = _budgets({"verifyRestart": {"enabled": True}, "deadEndRetry": {"enabled": False}})
    assert budgets["dead_end_reasons"] == ["unverified"]


def test_a_narrowed_dead_end_filter_keeps_the_restart_reason():
    budgets = _budgets({"verifyRestart": {"enabled": True}, "deadEndRetry": {"reasons": ["stranded"]}})
    assert budgets["dead_end_reasons"] == ["stranded", "unverified"]


def test_without_a_reviewer_there_is_nothing_to_restart_on():
    budgets = _budgets({"verifyRestart": {"enabled": True}, "verify": {"enabled": False}})
    assert budgets["dead_end_retries"] == 1
    assert budgets["rerun_fresh_clock"] is False


def test_a_mode_overlay_may_switch_it_on():
    cfg = FlowConfig.from_slice({}).with_overlay({"verifyRestart": {"enabled": True, "maxAttempts": 3}})
    assert cfg.verify_restart == VerifyRestartConfig(enabled=True, max_attempts=3)


@pytest.mark.parametrize("bad", [{"maxAttempts": 0}, {"maxRestarts": 5}])
def test_a_malformed_restart_block_is_refused(bad):
    with pytest.raises(ValueError):
        FlowConfig.from_slice({}).with_overlay({"verifyRestart": bad})


# -- the attempt check, on the participant's own facts --------------------------------


def _participant(**facts) -> ResearchFlowParticipant:
    p = ResearchFlowParticipant(flow=None)  # the check reads nothing off the flow
    p._facts.update(facts)
    return p


_DONE = {"status": "completed", "attempt": 1}
_OFF = VerifyRestartConfig(enabled=True)
_FIRST = VerifyRestartConfig(enabled=True, require_first_pass=True)


@pytest.mark.parametrize(
    ("review", "policy", "expected"),
    [
        ("pass", _OFF, []),
        ("pass_revised", _OFF, []),
        ("budget_spent_pass", _OFF, []),
        ("degraded_no_claim", _OFF, []),
        ("degraded_elided", _OFF, []),
        ("pass_revised", _FIRST, ["unverified:pass_revised"]),
        ("budget_spent_pass", _FIRST, ["unverified:budget_spent_pass"]),
        ("pass", _FIRST, []),
        ("rejected", _OFF, ["unverified:rejected"]),
        ("budget_spent", _OFF, ["unverified:budget_spent"]),
        ("budget_spent_reject", _OFF, ["unverified:budget_spent_reject"]),
        ("unavailable", _OFF, ["unverified:unavailable"]),
    ],
)
def test_what_counts_as_a_pass(review, policy, expected):
    p = _participant(verify_gate={"final_review": review})
    assert p.attempt_reasons(policy, "answer", _DONE) == expected


def test_a_wrap_up_answer_is_unreviewed_even_after_an_earlier_pass():
    p = _participant(verify_gate={"final_review": "pass"})
    assert p.attempt_reasons(_OFF, "answer", {"status": "interrupted"}) == ["unverified:unreviewed"]


def test_an_attempt_that_never_drafted_is_unreviewed():
    assert _participant().attempt_reasons(_OFF, "answer", _DONE) == ["unverified:unreviewed"]


@pytest.mark.parametrize(
    "facts",
    [
        {flow_mod._TURN_MODE_KEY: flow_mod.TurnMode(False, "gate")},
        {"clarify_requested": True},
        {"verify_gate": {"scoped": "plain"}},
    ],
)
def test_a_turn_the_reviewer_does_not_own_is_never_restarted(facts):
    assert _participant(**facts).attempt_reasons(_OFF, "answer", {"status": "interrupted"}) == []


def test_the_record_logs_every_attempt():
    p = _participant(verify_gate={"final_review": "rejected"})
    p.attempt_reasons(_OFF, "a", _DONE)
    p._attempt = 2
    p._facts["verify_gate"] = {"final_review": "pass"}
    p.attempt_reasons(_OFF, "b", _DONE)
    assert p._facts["restart"] == {"attempts": 2, "verified": True, "log": "1:rejected,2:pass"}


# -- end to end: the real plugin on a real loop ---------------------------------------


def _verdict(ok: bool) -> LLMResponse:
    args = {"pass": ok, "unresolved_claims": 0 if ok else 1, "unsupported_claims": [] if ok else ["the winner"]}
    return LLMResponse(
        content=None,
        tool_calls=[ToolCallRequest(id="v", name="review_verdict", arguments=args)],
        finish_reason="tool_calls",
    )


class _StudentAndReviewer(LLMProvider):
    """One provider for both roles, told apart by the verdict tool the reviewer is offered."""

    def __init__(self, drafts: list[str], verdicts: list[bool]) -> None:
        super().__init__(api_key="test")
        self._drafts = list(drafts)
        self._verdicts = list(verdicts)
        self.reviewed: list[str] = []
        self.seeds: list[list[dict]] = []

    async def chat(self, messages, tools=None, model=None, **_kw):
        if any((t.get("function") or {}).get("name") == "review_verdict" for t in tools or []):
            self.reviewed.append(str(messages[-1].get("content")))
            return _verdict(self._verdicts.pop(0))
        if not tools:
            # Another gate's own call (a wrap-up, a salvage): not the student's turn.
            return LLMResponse(content="", finish_reason="stop")
        if [m.get("role") for m in messages if m.get("role") != "system"] == ["user"]:
            self.seeds.append([dict(m) for m in messages])
        return LLMResponse(content=self._drafts.pop(0), finish_reason="stop")

    def get_default_model(self) -> str:
        return "stub"


async def _turn(tmp_path, provider, flow: dict) -> str:
    from raven.agent.loop.main import AgentLoop
    from raven.plugins.bootstrap import assemble_plugin_registry
    from raven.plugins.context import ServiceLocator
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    reg = assemble_plugin_registry(extra_dirs=[REPO / "agents" / "raven-research" / "plugins"], entry_points_group=None)
    services = ServiceLocator(workspace=tmp_path / "ws", user_id="t", agent_id="t", provider=provider)
    slice_ = {
        "enabled": True,
        "conversation": {"enabled": False},
        "fetchFloor": {"enabled": False},
        # As the eval runner runs it: no report-format bounce beside the reviewer's.
        "finalShape": {"reportStructure": False},
        # The stub's drafts carry no reasoning, so none closes a think block.
        "thinkClosingTagRequired": False,
        "search": {"apiKey": "test-key"},
        "stateRoot": str(tmp_path / "state"),
        **flow,
    }
    hook = reg.build_hook("research_flow", config=slice_, services=services)
    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path / "ws",
        model="stub",
        policy=TurnPolicy(interactive=False),
        tools=ToolWiring(restrict_to_workspace=True),
        host=HostWiring(hooks=[hook]),
    )

    async def _noop(**_kw):
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop
    reply, _ = await loop._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="cli", chat_id="c", sender_id="u", chat_type=ChatType.DM),
            text="who won the 1931 race?",
            conversation="cli:restart",
        )
    )
    return reply


@pytest.mark.asyncio
async def test_a_rejected_attempt_restarts_from_the_question_with_its_revision_budget_back(tmp_path):
    """Attempt one: draft A rejected, draft B ships unreviewed once the one revision is
    spent. Attempt two must get its revision budget back, or its first draft would ship
    unreviewed too and the restart would rerun forever on the same verdict."""
    provider = _StudentAndReviewer(drafts=["A: Bob.", "B: Bob.", "C: Alice."], verdicts=[False, True])

    reply = await _turn(tmp_path, provider, {"verifyRestart": {"enabled": True, "maxAttempts": 3}})

    assert reply.startswith("C: Alice.")
    assert len(provider.reviewed) == 2, "draft C was reviewed, not waved through on attempt one's spent budget"
    assert len(provider.seeds) == 2
    assert not any("Bob" in str(m.get("content")) for m in provider.seeds[1]), "attempt two starts on the question"
    (record,) = [json.loads(f.read_text()) for f in (tmp_path / "state" / "sessions").glob("*.json")]
    assert record["observers"]["restart"] == {"attempts": 2, "verified": True, "log": "1:budget_spent,2:pass"}
    assert record["observers"]["verify_gate"]["final_review"] == "pass"


@pytest.mark.asyncio
async def test_off_the_rejected_answer_ships_as_before(tmp_path):
    provider = _StudentAndReviewer(drafts=["A: Bob.", "B: Bob."], verdicts=[False])

    reply = await _turn(tmp_path, provider, {})

    assert reply.startswith("B: Bob.")
    assert len(provider.seeds) == 1
