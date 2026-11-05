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
from research_flow.gates.base import GateCtx  # noqa: E402
from research_flow.gates.best_guess import BestGuessGate  # noqa: E402
from research_flow.support.best_guess import abstains, answer_key, same_answer  # noqa: E402

from raven.agent.loop import TURN_BUDGETS_KEY, TURN_SYNTHESIS_KEY  # noqa: E402
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
        "context_reset_tokens": None,
    }


def test_on_it_budgets_the_attempts_and_a_fresh_clock():
    budgets = _budgets({"verifyRestart": {"enabled": True}, "wallClockSeconds": 3600})
    assert budgets["dead_end_retries"] == 4, "five attempts, the first included"
    assert budgets["rerun_fresh_clock"] is True
    assert budgets["dead_end_reasons"] == [], "every dead end still reruns"


def test_the_context_reset_line_rides_only_a_restart():
    on = {"verifyRestart": {"enabled": True, "contextResetTokens": 131072}}
    assert _budgets(on)["context_reset_tokens"] == 131072
    assert _budgets({"verifyRestart": {"enabled": True}})["context_reset_tokens"] == 0, "0 is off"
    assert _budgets({**on, "verify": {"enabled": False}})["context_reset_tokens"] is None


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


def test_an_attempt_the_loop_dropped_at_the_reset_line_says_so():
    end = {"status": "interrupted", "stopped_by": "context_reset"}
    assert _participant().attempt_reasons(_OFF, None, end) == ["unverified:context_reset"]


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


# -- bestGuess: reading an abstention off the answer span -------------------------------

_ABSTAINED = [
    "<answer>No verifiable answer</answer>",
    "Reasoning above.\n\n<answer>Cannot be determined from the available evidence; no verified author fits.</answer>",
    "<answer>Unable to definitively identify \u2014 research incomplete due to tool limitations</answer>",
    "<answer>Unknown \u2014 no title has been verified.</answer>",
    "<answer>Indeterminable \u2014 insufficient evidence to identify the region</answer>",
    "**Answer: Cannot be determined from evidence.**",
    "<answer>\u65e0\u6cd5\u786e\u5b9a\u552f\u4e00\u7b54\u6848</answer>",  # cannot determine a unique answer
    "No verifiable answer.",  # short and unmarked
]
_ANSWERED = [
    "<answer>Irma P. Hall</answer>",
    "I could not verify the 2022 clue against any page I opened.\n\n<answer>Goldfish</answer>",
    "<answer>Uncertain \u2014 no verifiable match; a degree-only identification points to Finnish</answer>",
    "<answer>Not verified, but the best available guess is the Sierra Nevada yellow-legged frog</answer>",
    "<answer>#2013NewBeginnings</answer>",
    "<answer>Leeds United, although the second clue could not be confirmed in a primary source and the "
    "fixture list I reached was incomplete for that season</answer>",
    "A long report with no marker. " * 20 + "It cannot be determined.",
    "<answer><tool_call>\n<function=web_search>\n</function>\n</tool_call></answer>",
    "",
]


@pytest.mark.parametrize("text", _ABSTAINED)
def test_an_abstaining_answer_is_read_as_one(text):
    assert abstains(text)
    assert answer_key(text) is None


@pytest.mark.parametrize("text", _ANSWERED)
def test_a_named_or_unlocatable_answer_is_not_an_abstention(text):
    assert not abstains(text)


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("<answer>Irma P. Hall</answer>", "Answer: **Irma Hall**", True),
        ("<answer>Goldfish (Carassius auratus)</answer>", "<answer>goldfish</answer>", True),
        ("<answer>The Beatles</answer>", "<answer>Beatles</answer>", True),
        ("<answer>Irma P. Hall</answer>", "<answer>Hall</answer>", False),
        ("<answer>Leeds United</answer>", "<answer>Newcastle United</answer>", False),
    ],
)
def test_two_answers_agree_when_one_s_words_sit_inside_the_other_s(a, b, same):
    assert same_answer(answer_key(a), answer_key(b)) is same


def test_under_best_guess_an_abstention_the_reviewer_passed_is_not_a_pass():
    p = _participant(verify_gate={"final_review": "pass"})
    text = "<answer>No verifiable answer</answer>"
    assert p.attempt_reasons(_OFF, text, _DONE) == [], "off: the reviewer's pass stands"
    assert p.attempt_reasons(_OFF, text, _DONE, abstain_fails=True) == ["unverified:abstained"]


def test_best_guess_tells_the_wrap_up_to_name_its_likeliest_answer():
    def guidance(slice_):
        policy = flow_mod._turn_cargo(FlowConfig.from_slice(slice_), "q").get(TURN_SYNTHESIS_KEY)
        return policy.guidance if policy else None

    assert guidance({"finalShape": {"reportStructure": False}}) is None
    assert guidance({"finalShape": {"reportStructure": False}, "bestGuess": {"enabled": True}}) == (
        flow_mod._BEST_GUESS_WRAP_UP
    )
    both = guidance({"bestGuess": {"enabled": True}})
    assert both.endswith(flow_mod._BEST_GUESS_WRAP_UP) and both != flow_mod._BEST_GUESS_WRAP_UP


class _Reply:
    def __init__(self, content: str) -> None:
        self.content = content
        self.has_tool_calls = False
        self.reasoning_content = None


@pytest.mark.asyncio
async def test_the_gate_bounces_an_abstaining_draft_its_budget_s_worth_of_times():
    gate = BestGuessGate(bounces=1)
    facts: dict = {}
    ctx = GateCtx(session_key="s", response=_Reply("<answer>No verifiable answer</answer>"), metadata=facts)

    first = await gate.after_iteration(ctx)
    second = await gate.after_iteration(ctx)

    assert first.rollback and first.rollback_inject[-1]["role"] == "user"
    assert not second.rollback
    assert facts["best_guess"] == {"abstained": 2, "bounces": 1}
    named = GateCtx(session_key="s", response=_Reply("<answer>Alice</answer>"), metadata={})
    assert not (await gate.after_iteration(named)).rollback


# -- the vote: which attempt ships when none passed -------------------------------------


def _voted(finals: list[str], whys: list[str]) -> int | None:
    p = _participant()
    p._unpassed = list(whys)
    return p.pick_attempt(finals)


def test_the_answer_most_attempts_named_ships():
    finals = [
        "<answer>Bob</answer>",
        "<answer>Alice Smith</answer>",
        "<answer>Alice Smith.</answer>",
        "<answer>Carol</answer>",
    ]
    assert _voted(finals, ["rejected"] * 4) == 2, "Alice twice; the later of the two"


def test_an_abstention_never_outvotes_a_named_answer():
    finals = ["<answer>Bob</answer>", "<answer>No verifiable answer</answer>", "<answer>Cannot be determined</answer>"]
    assert _voted(finals, ["rejected", "abstained", "abstained"]) == 0


def test_with_nothing_named_an_abstention_loses_to_a_report_whose_answer_cannot_be_located():
    report = "A long report with no marker. " * 20
    assert _voted([report, "<answer>No verifiable answer</answer>"], ["rejected", "abstained"]) == 0


def test_a_tie_goes_to_the_better_review_then_the_later_attempt():
    finals = ["<answer>Bob</answer>", "<answer>Alice</answer>", "<answer>Carol</answer>"]
    assert _voted(finals, ["budget_spent", "rejected", "rejected"]) == 0
    assert _voted(finals, ["rejected", "rejected", "rejected"]) == 2


def test_the_vote_is_on_the_record():
    p = _participant()
    p._unpassed = ["rejected", "rejected"]
    p.pick_attempt(["<answer>Bob</answer>", "<answer>Bob</answer>"])
    assert p._facts["restart"]["shipped"] == 2
    assert p._facts["restart"]["votes"] == 2


@pytest.mark.asyncio
async def test_with_no_attempt_passed_the_answer_most_attempts_named_ships(tmp_path):
    """Each attempt: one draft rejected, the next shipped once the revision is spent."""
    drafts = [
        "<answer>Bob</answer>",
        "Second look.\n<answer>Bob</answer>",
        "<answer>Bob Jones</answer>",
        "Second look.\n<answer>Bob Jones</answer>",
        "<answer>Alice</answer>",
        "Second look.\n<answer>Alice</answer>",
    ]
    provider = _StudentAndReviewer(drafts=drafts, verdicts=[False, False, False])

    reply = await _turn(tmp_path, provider, {"verifyRestart": {"enabled": True, "maxAttempts": 3}})

    assert reply.startswith("Second look.\n<answer>Bob Jones</answer>"), "Bob twice outvotes Alice once"
    (record,) = [json.loads(f.read_text()) for f in (tmp_path / "state" / "sessions").glob("*.json")]
    assert record["observers"]["restart"]["shipped"] == 2
    assert record["observers"]["restart"]["log"] == "1:budget_spent,2:budget_spent,3:budget_spent"


@pytest.mark.asyncio
async def test_pick_last_ships_the_last_attempt_as_before(tmp_path):
    drafts = ["<answer>Bob</answer>", "<answer>Bob.</answer>", "<answer>Alice</answer>", "<answer>Alice.</answer>"]
    provider = _StudentAndReviewer(drafts=drafts, verdicts=[False, False])

    reply = await _turn(tmp_path, provider, {"verifyRestart": {"enabled": True, "maxAttempts": 2, "pick": "last"}})

    assert reply.startswith("<answer>Alice.</answer>")


@pytest.mark.asyncio
async def test_an_abstaining_draft_is_sent_back_and_the_named_answer_is_reviewed(tmp_path):
    provider = _StudentAndReviewer(
        drafts=["<answer>No verifiable answer</answer>", "<answer>Alice</answer>"], verdicts=[True]
    )

    reply = await _turn(tmp_path, provider, {"bestGuess": {"enabled": True}})

    assert reply.startswith("<answer>Alice</answer>")
    assert len(provider.reviewed) == 1, "the abstention was bounced before the reviewer saw it"
    (record,) = [json.loads(f.read_text()) for f in (tmp_path / "state" / "sessions").glob("*.json")]
    assert record["observers"]["best_guess"] == {"abstained": 1, "bounces": 1}


class _FillsThenDrafts(_StudentAndReviewer):
    """Attempt one searches until its context holds 200k tokens; later attempts draft."""

    def __init__(self, drafts: list[str], verdicts: list[bool]) -> None:
        super().__init__(drafts, verdicts)
        self.searched = 0

    async def chat(self, messages, tools=None, model=None, **kw):
        student = tools and not any((t.get("function") or {}).get("name") == "review_verdict" for t in tools)
        if student and not self.searched:
            self.searched += 1
            self.seeds.append([dict(m) for m in messages])
            return LLMResponse(
                content="",
                tool_calls=[ToolCallRequest(id="s", name="list_dir", arguments={"path": "."})],
                finish_reason="tool_calls",
                usage={"prompt_tokens": 200_000, "completion_tokens": 0},
            )
        return await super().chat(messages, tools=tools, model=model, **kw)


@pytest.mark.asyncio
async def test_an_attempt_whose_context_fills_restarts_before_it_answers(tmp_path):
    provider = _FillsThenDrafts(drafts=["<answer>Alice</answer>"], verdicts=[True])
    flow = {"verifyRestart": {"enabled": True, "maxAttempts": 2, "contextResetTokens": 131072}}

    reply = await _turn(tmp_path, provider, flow)

    assert reply.startswith("<answer>Alice</answer>")
    assert len(provider.seeds) == 2, "the second attempt starts on the question"
    assert len(provider.reviewed) == 1, "the dropped attempt delivered nothing to review"
    (record,) = [json.loads(f.read_text()) for f in (tmp_path / "state" / "sessions").glob("*.json")]
    assert record["observers"]["restart"]["log"] == "1:context_reset,2:pass"
