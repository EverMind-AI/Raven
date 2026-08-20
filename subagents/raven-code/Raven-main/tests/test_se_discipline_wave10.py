"""Wave10 discipline revisions (the recall-guard appendix was later removed).

Every assertion below is pinned to a real failed trajectory from the
deepseek-v4-flash offline SWE-Verified round (2026-08-14/15, run
raven-offline-500); the task ids are cited per test. Root causes are recorded
in coding-agent-eval/offline-ds4f/LEDGER.md and the wave10 plan.
"""

from __future__ import annotations

from pathlib import Path

from raven.context_engine.segments import render


def _discipline_lines() -> list[str]:
    return render._SE_DISCIPLINE.strip("\n").split("\n")


def _flat(text: str) -> str:
    """Whitespace-normalized view: assertions must not depend on line wrap."""
    return " ".join(text.split())


class TestDisciplineRevisions:
    def test_line_budget_hard_cap(self):
        # Anti-inflation ratchet: the clauses that earn their keep (stale-test
        # exemption, smallest-change, evidence ranking) get diluted every time
        # the block grows. New rules must displace old lines, not stack.
        assert len(_discipline_lines()) <= 65, (
            f"_SE_DISCIPLINE is {len(_discipline_lines())} lines; budget is 65. Trim before adding."
        )

    def test_p1_evidence_ranking_is_one_directional(self):
        # django-13158: the model empirically confirmed a regression from its
        # own repro script, then dismissed it with "doesn't affect the PR
        # requirement nor any tests" - using the evidence ranking backwards.
        text = render._SE_DISCIPLINE
        assert "never permission to dismiss a failure" in text
        assert "weakest check is real" in text

    def test_p1_reporting_a_stale_test_stays_valid(self):
        # The stale-test exemption is load-bearing (replay data: 10 tasks
        # finished red and were judged correct). P1 must not outlaw it.
        text = render._SE_DISCIPLINE
        assert "is a valid report" in text
        assert "asserts the exact OLD behavior" in text

    def test_p2_self_introduced_regression_is_in_scope(self):
        # django-13158: the model wrote the correct clone() fix, confirmed the
        # regression it prevents, then dropped it as beyond "minimal changes".
        text = render._SE_DISCIPLINE
        assert "always in scope" in text
        assert "not by stacking new code" in text
        assert "Pre-existing problems you did not cause" in text

    def test_p3_acceptance_evidence_exercises_real_capability(self):
        # django-14007: repro mocked sqlite into postgres, so the graded code
        # path never ran. django-15629: compared self-authored SQL strings
        # instead of observing real column state.
        text = _flat(render._SE_DISCIPLINE)
        assert "faking a capability the environment has proves nothing" in text
        assert "never from your own implementation" in text

    def test_p3_unit_test_mocks_stay_unrestricted(self):
        # Side-effect review: without this exemption the rule reads as a ban
        # on ordinary unit-test mocks (django's own suite, TB tasks).
        assert "Mocks inside unit tests" in render._SE_DISCIPLINE

    def test_p4_enumerate_by_property_not_examples(self):
        # astropy-14365: issue said the QDP format is case-insensitive; the
        # model widened only the command regex the issue used as its example.
        text = _flat(render._SE_DISCIPLINE)
        assert "enumerate by the property, not by its examples" in text
        # The scope clamp survives, but rewritten: the old wording paired with
        # P2 would give the model a favorable clause to cite when dropping
        # self-confirmed regressions ("nothing beyond that behavior").
        assert "nothing beyond that behavior" not in text
        assert "nothing beyond that property" in text


class TestRecallGuardRemoved:
    """The recall-guard appendix was removed entirely (litong, 2026-08-18).

    A stale RAVEN_RECALL_GUARD=1 in some environment or eval config must be
    inert: the identity segment stays byte-identical whether or not the
    retired variable is set.
    """

    def test_env_var_is_inert(self, monkeypatch):
        monkeypatch.delenv("RAVEN_RECALL_GUARD", raising=False)
        without = render.identity_text(Path("/tmp/ws"), model="deepseek-v4-flash")
        monkeypatch.setenv("RAVEN_RECALL_GUARD", "1")
        with_env = render.identity_text(Path("/tmp/ws"), model="deepseek-v4-flash")
        assert with_env == without
        assert "Grounding Over Recall" not in with_env
