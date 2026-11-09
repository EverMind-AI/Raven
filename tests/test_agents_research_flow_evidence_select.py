"""Which tool results the reviewer is shown, when a blind tail shows the wrong ones.

A draft the reviewer wrongly passed averaged 73 tool calls, so an 8-slot tail carried
about 11% of the turn's evidence and dropped exactly the early results that had
located the entity. Offline over 535 drafts, keeping a 6-result tail and spending the
rest of a 16 x 4000 budget on first-coverage of the task's terms stopped 56.2% of
wrong drafts against the tail's 48.6% (paired +27/-11, p 0.014), with false stops
falling from 9.2% to 8.3%.

Two properties are load-bearing beyond that result and are pinned here: the default
still builds the old pack byte for byte, and ``elided_skipped`` -- which feeds the
published ``rejected_on_elided`` observer -- is counted by the same recency walk
under either selection, so readings already on disk stay comparable.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.config import FlowConfig  # noqa: E402
from research_flow.gates.verify import DraftReviewerGate  # noqa: E402
from research_flow.support.evidence_select import choose_by_coverage, task_terms  # noqa: E402
from research_flow.support.harness_text import TOOL_OUTPUT_ELIDED as ELIDED  # noqa: E402


def _gate(**kw) -> DraftReviewerGate:
    return DraftReviewerGate(provider=object(), **kw)


def _tool(content: str, name: str = "web_fetch") -> dict:
    return {"role": "tool", "name": name, "content": content}


class TestTaskTerms:
    def test_content_words_only_and_deduplicated(self):
        terms = task_terms("Which sculpture by Arthur Breen stands in Abbey Square? Give the answer.")
        assert "sculpture" in terms and "breen" in terms and "abbey" in terms
        # Asking words would make a result that discusses the task look informative.
        assert "which" not in terms and "answer" not in terms and "give" not in terms
        assert len(terms) == len(set(terms))

    def test_years_are_terms_and_short_numbers_are_not(self):
        terms = task_terms("released in 1958 with 7 tracks")
        assert "1958" in terms
        assert "7" not in terms

    def test_cjk_is_shingled(self):
        terms = task_terms("父母爱情 的女二号")
        assert "父母" in terms and "母爱" in terms


class TestChooseByCoverage:
    def test_short_turn_returns_everything(self):
        assert choose_by_coverage(["a", "b", "c"], n=8, keep_tail=6, terms=["a"]) == [0, 1, 2]

    def test_tail_is_kept_and_an_early_covering_result_is_added(self):
        bodies = ["gold entity alpha"] + ["filler"] * 8 + ["recent one", "recent two"]
        picks = choose_by_coverage(bodies, n=3, keep_tail=2, terms=["alpha"])
        assert picks == [0, 9, 10]  # chronological, tail intact, index 0 bought on coverage

    def test_result_order_is_chronological(self):
        bodies = ["alpha", "beta", "filler", "filler", "tail"]
        assert choose_by_coverage(bodies, n=3, keep_tail=1, terms=["alpha", "beta"]) == [0, 1, 4]

    def test_budget_is_spent_even_when_nothing_covers_a_new_term(self):
        bodies = ["x"] * 10
        picks = choose_by_coverage(bodies, n=4, keep_tail=1, terms=["nothing-matches"])
        assert len(picks) == 4
        assert 9 in picks  # the tail, plus recency for the rest

    def test_keep_tail_clamped_to_budget_degrades_to_recency(self):
        bodies = [f"b{i}" for i in range(10)]
        assert choose_by_coverage(bodies, n=3, keep_tail=99, terms=["b0"]) == [7, 8, 9]

    def test_zero_budget(self):
        assert choose_by_coverage(["a", "b"], n=0, keep_tail=1, terms=["a"]) == []


class TestEvidencePack:
    def test_default_is_the_recency_tail(self):
        messages = [_tool(f"body {i}") for i in range(12)]
        pack, skipped = _gate(evidence_items=4)._evidence_pack(messages, "anything")
        assert skipped == 0
        assert [f"body {i}" in pack for i in (8, 9, 10, 11)] == [True] * 4
        assert "body 7" not in pack

    def test_coverage_reaches_an_early_result_the_tail_dropped(self):
        messages = [_tool("the Seamus Rafter sculpture stands in Abbey Square")]
        messages += [_tool(f"unrelated filler {i}") for i in range(10)]
        task = "Which sculpture stands in Abbey Square?"
        tail_pack, _ = _gate(evidence_items=3, evidence_keep_tail=2)._evidence_pack(messages, task)
        cov_pack, _ = _gate(evidence_items=3, evidence_select="coverage", evidence_keep_tail=2)._evidence_pack(
            messages, task
        )
        assert "Seamus Rafter" not in tail_pack
        assert "Seamus Rafter" in cov_pack

    def test_truncation_keeps_the_end_of_a_long_body(self):
        messages = [_tool("x" * 50 + "ENDMARK")]
        pack, _ = _gate(evidence_item_chars=7)._evidence_pack(messages, "")
        assert "ENDMARK" in pack
        assert "xxxxxxxx" not in pack

    def test_elided_bodies_are_skipped_by_both_selections_and_counted_once(self):
        messages = [_tool("real one"), _tool(ELIDED), _tool("real two")]
        for select in ("recent", "coverage"):
            pack, skipped = _gate(evidence_items=4, evidence_select=select)._evidence_pack(messages, "real")
            assert ELIDED not in pack
            assert skipped == 1, select

    def test_skipped_is_the_same_recency_walk_under_either_selection(self):
        # The count must not move with the selection: it is published as
        # ``rejected_on_elided`` and has readings on disk.
        messages = [_tool(ELIDED), _tool(ELIDED)] + [_tool(f"body {i}") for i in range(6)]
        counts = {
            select: _gate(evidence_items=3, evidence_select=select)._evidence_pack(messages, "body")[1]
            for select in ("recent", "coverage")
        }
        assert counts["recent"] == counts["coverage"] == 0  # walked none: 3 slots fill first

    def test_non_tool_messages_are_ignored(self):
        messages = [{"role": "user", "content": "the task"}, _tool("body"), {"role": "assistant", "content": "draft"}]
        pack, _ = _gate(evidence_items=4)._evidence_pack(messages, "task")
        assert "body" in pack and "draft" not in pack

    def test_empty_turn(self):
        assert _gate()._evidence_pack([], "task") == ("", 0)


class TestConfigDefaults:
    def test_product_default_is_unchanged(self):
        verify = FlowConfig().verify
        assert verify.evidence_select == "recent"
        assert (verify.evidence_items, verify.evidence_item_chars) == (8, 2000)
        assert verify.evidence_keep_tail == 6

    def test_an_evaluation_profile_can_ask_for_the_measured_configuration(self):
        cfg = FlowConfig.model_validate(
            {"verify": {"evidenceSelect": "coverage", "evidenceItems": 16, "evidenceItemChars": 4000}}
        )
        assert cfg.verify.evidence_select == "coverage"
        assert (cfg.verify.evidence_items, cfg.verify.evidence_item_chars) == (16, 4000)
