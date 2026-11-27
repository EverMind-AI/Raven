"""The verify gate sends a revision back once when it talks about the review instead of to the reader.

A bounced draft is revised in a turn that just read a reviewer's objections, and the
revision ships as the report. Replays of real bounces showed the bounce prompt's own
instruction is not enough: a revision still opened sections like "response to the
review" or noted "(the original draft said 1.7x; corrected here)".
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.gates.verify import DraftReviewerGate, review_talk_lines  # noqa: E402
from research_flow.support import ledger as ledger_mod  # noqa: E402
from research_flow.support.harness_text import REVIEW_TALK_PREFIX, harness_ask_kind  # noqa: E402

from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402
from raven.i18n import zh_lexicon  # noqa: E402


class _Passer:
    def __init__(self) -> None:
        self.calls = 0

    async def chat_with_retry(self, **kwargs):
        self.calls += 1
        return SimpleNamespace(content='{"pass": true, "unsupported_claims": [], "issues": []}')


def _ctx(answer: str, revisions: int) -> AgentHookContext:
    ctx = AgentHookContext(session_key="cli:test")
    ctx.iteration = 5
    ctx.response = SimpleNamespace(has_tool_calls=False, content=answer)
    ctx.messages = [{"role": "user", "content": "task"}, {"role": "tool", "name": "web_fetch", "content": "body"}]
    ctx.metadata["verify_gate"] = {
        "revisions": revisions,
        "reviews": revisions,
        "fail_open": 0,
        "passes": 0,
        "rejects": revisions,
    }
    return ctx


LEAK = "> Revenue grew 40%.\n\n## Notes on this revision\n\nThe reviewer's first objection does not hold.\n"


@pytest.fixture(autouse=True)
def _ledger_reset():
    yield
    ledger_mod.close_product_ledger()
    ledger_mod.set_ledger_dir(None)


@pytest.mark.asyncio
async def test_a_revision_that_answers_the_reviewer_is_sent_back_before_review():
    provider = _Passer()
    gate = DraftReviewerGate(provider, max_revisions=2)
    ctx = _ctx(LEAK, revisions=1)

    decision = await gate.after_iteration(ctx)

    assert decision.rollback is True
    assistant, ask = decision.rollback_inject
    assert assistant == {"role": "assistant", "content": LEAK.strip(), "_recovery_synthetic": True}
    assert ask["_recovery_synthetic"] is True, "the ask must not outlive the turn either"
    assert ask["content"].startswith(REVIEW_TALK_PREFIX)
    assert "- The reviewer's first objection does not hold." in ask["content"]
    assert provider.calls == 0, "a draft about to be rewritten is not worth a review"
    assert ctx.metadata["verify_gate"]["revisions"] == 1, "the clean copy does not spend a revision"


@pytest.mark.asyncio
async def test_the_clean_copy_is_asked_for_once_and_then_reviewed():
    provider = _Passer()
    gate = DraftReviewerGate(provider, max_revisions=2)
    ctx = _ctx(LEAK, revisions=1)
    await gate.after_iteration(ctx)

    ctx.response = SimpleNamespace(has_tool_calls=False, content=LEAK)
    decision = await gate.after_iteration(ctx)

    assert decision.rollback is False
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_a_first_draft_is_never_checked():
    # No review has happened yet, so the words can only be about the subject - a report
    # on peer review, say - and must reach the reviewer untouched.
    provider = _Passer()
    gate = DraftReviewerGate(provider, max_revisions=1)
    decision = await gate.after_iteration(_ctx(LEAK, revisions=0))

    assert decision.rollback is False
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_the_report_the_write_step_asked_for_is_checked_for_draft_talk():
    # It follows a draft set aside, and shipped "the first draft had these errors"
    # with no review in between.
    provider = _Passer()
    gate = DraftReviewerGate(provider, max_revisions=1)
    leak = f"{zh_lexicon.DRAFT_TALK_MARKERS[1]}: Task Arithmetic is ICLR 2023, not NeurIPS 2022."
    ctx = _ctx(f"> Merging works.\n\n{leak}\n", revisions=0)
    ctx.metadata["write_phase"] = {"asked": True}

    decision = await gate.after_iteration(ctx)

    assert decision.rollback is True and f"- {leak}" in decision.rollback_inject[1]["content"]
    assert provider.calls == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "line",
    [
        "The reviewer's first objection does not hold.",
        f"SWE-bench doubled over the {zh_lexicon.REVIEW_TALK_MARKERS[4]}.",
    ],
    ids=["review_words", "previous_release"],
)
async def test_the_written_report_reads_only_the_draft_phrases(line):
    # No review has happened yet, and "the previous version" names a product's last release.
    provider = _Passer()
    ctx = _ctx(f"> Revenue grew 40%.\n\n{line}\n", revisions=0)
    ctx.metadata["write_phase"] = {"asked": True}

    decision = await DraftReviewerGate(provider, max_revisions=1).after_iteration(ctx)

    assert decision.rollback is False and provider.calls == 1


@pytest.mark.asyncio
async def test_a_revision_past_the_budget_is_still_cleaned(tmp_path):
    ledger_mod.set_ledger_dir(tmp_path)
    ledger = Path(ledger_mod.open_product_ledger("l"))
    gate = DraftReviewerGate(_Passer(), max_revisions=1)

    decision = await gate.after_iteration(_ctx(LEAK, revisions=1))

    assert decision.rollback is True
    rows = [json.loads(ln) for ln in ledger.read_text(encoding="utf-8").splitlines() if ln.strip()]
    outcome = [r for r in rows if r["op"] == "verify"]
    assert [r["outcome"] for r in outcome] == ["review_talk"]
    assert outcome[0]["reviewed"] is False


@pytest.mark.parametrize("marker", zh_lexicon.REVIEW_TALK_MARKERS)
def test_every_chinese_marker_is_caught(marker):
    assert review_talk_lines(f"Revenue 1.4x ({marker} 1.7x)\nclean line") == [f"Revenue 1.4x ({marker} 1.7x)"]


def test_the_subjects_own_words_and_addresses_are_no_review_talk():
    """A report on peer review or code review uses the words of the review; an address can hold them too."""
    report = "Peer reviewers disagree in 30% of cases.\nSee https://example.com/the-review/reviewer-guide"
    assert review_talk_lines(report, "How reliable is peer review? Who is a good reviewer?") == []
    assert review_talk_lines(report) == ["Peer reviewers disagree in 30% of cases."]
    leak = "The reviewer's first objection does not hold, as the first draft said."
    assert review_talk_lines(f"Peer review.\n{leak}", "How reliable is peer review?") == [leak]


def test_a_clean_answer_has_no_lines_and_the_ask_is_a_harness_ask():
    assert review_talk_lines("> Revenue grew 40%.\n\n## Sources\n\n1. https://example.com") == []
    assert harness_ask_kind(REVIEW_TALK_PREFIX + " anything") == "review_talk"
