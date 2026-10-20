"""The three gates of a market scan, and the stop policy's own numbers.

The three gates exist because a report with nothing behind it is a report
whose quality rests on whoever reads it. Each gate answers a different
question, each can be switched off without touching the others, and the
tests below are written around the properties that make that true:

* the mechanical gate never asks a model, and never passes a defect;
* the sufficiency gate refuses criteria written **after** the pages were
  read, because a criterion written afterwards is a description of the
  answer rather than a requirement on the search;
* the judge gate runs last, on a cheap model, and refuses a verdict that
  changes when the companies are presented in the opposite order;
* the stop policy reports precision and calibration error **apart from**
  accuracy, and a regression in either fails the gate even when accuracy
  holds - because the cheap way to raise accuracy is to stop more often,
  and that is a trade, not an improvement.

The last test in the file pins this package's defect checker to the
repository's own ``research_report_audit.py``. The two live in different
places on purpose - the audit has to run against a report on a machine with
no raven checkout on the path - so the pin is what stops their blind spots
drifting apart.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.market import (  # noqa: E402
    CHEAP_JUDGE_MODEL,
    MarketCriteria,
    MarketEntity,
    MarketGateConfig,
    StopDecision,
    StopMetrics,
    build_scan,
    compare_stop_metrics,
    judge_gate,
    mechanical_gate,
    render_markdown,
    report_defects,
    run_market_gates,
    stop_metrics,
    sufficiency_gate,
)

from raven.records.types import Provenance  # noqa: E402
from scripts.research_report_audit import audit  # noqa: E402

AS_OF = "2026-10-20"
ACME = "https://acme.example/pricing"
GLOBEX = "https://globex.example/pricing"
INITECH = "https://initech.example/pricing"


def _provenance(field: str, url: str, *, source: str = "jina") -> Provenance:
    return Provenance(
        field=field,
        source_url=url,
        source=source,
        method="fetch",
        retrieved_at=AS_OF,
    )


def _entities() -> list[MarketEntity]:
    return [
        MarketEntity(
            key="acme",
            name="Acme",
            fields={"what_it_does": "A platform.", "price": "$49 per seat per month"},
            provenance=[_provenance("what_it_does", ACME), _provenance("price", ACME)],
        ),
        MarketEntity(
            key="globex",
            name="Globex",
            fields={"what_it_does": "A platform sold on request."},
            provenance=[_provenance("what_it_does", GLOBEX)],
        ),
        MarketEntity(
            key="initech",
            name="Initech",
            fields={"what_it_does": "A platform.", "price": "$0.50 per GB ingest"},
            provenance=[_provenance("what_it_does", INITECH), _provenance("price", INITECH)],
        ),
    ]


def _scan(entities=None, **kwargs):
    params = {"as_of": AS_OF, "opened_urls": [ACME, GLOBEX, INITECH], "opt_ins": {}}
    params.update(kwargs)
    return build_scan(entities if entities is not None else _entities(), **params)


class _Provider:
    """Records every judge call so a test can assert what was asked."""

    def __init__(self, *replies: object) -> None:
        self._replies = list(replies)
        self.calls: list[dict] = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        reply = self._replies.pop(0) if self._replies else '{"meets_criteria": false, "reason": "no reply"}'
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=str(reply))


# ── the mechanical gate ─────────────────────────────────────────────────────


def test_the_mechanical_gate_passes_a_clean_scan():
    scan = _scan()
    result = mechanical_gate(scan, report_text=render_markdown(scan), opt_ins={})
    assert result.passed, result.findings
    assert result.measurements["checked_report"] is True


def test_the_mechanical_gate_finds_an_unsourced_field():
    """The gate re-checks a scan whose contents changed after it was built.

    ``build_scan`` refuses an unsourced entity, so a scan that carries one
    can only arrive by being changed after the build - a ledger read back
    in, a field edited in place. That is exactly the case a gate exists
    for, so the test builds a sound scan and then spoils it.
    """
    scan = _scan()
    scan.entities.append(
        MarketEntity(
            key="bad",
            name="Bad",
            fields={"what_it_does": "A platform.", "price": "$49"},
            provenance=[_provenance("what_it_does", ACME)],
        )
    )
    result = mechanical_gate(scan, opt_ins={})
    assert not result.passed
    assert any("no source" in f for f in result.findings)


def test_the_mechanical_gate_finds_a_source_that_may_not_be_used():
    scan = _scan()
    scan.entities.append(
        MarketEntity(
            key="bad",
            name="Bad",
            fields={"what_it_does": "A platform."},
            provenance=[_provenance("what_it_does", ACME, source="linkedin")],
        )
    )
    result = mechanical_gate(scan, opt_ins={})
    assert not result.passed
    assert any("may not be used" in f for f in result.findings)


def test_the_mechanical_gate_finds_an_estimate_in_a_quantitative_column():
    report = (
        "| Company | Price |\n|---|---|\n"
        "| Acme | approximately $49 |\n"
        "| Initech | $0.50 per GB |\n"
    )
    result = mechanical_gate(_scan(), report_text=report, opt_ins={})
    assert not result.passed
    assert any("estimate_in_quantitative_column" in f for f in result.findings)


def test_a_row_marked_not_fetched_is_reported_but_does_not_fail_the_gate():
    """The audit's own severity split, kept.

    ``unfetched_table_row`` is soft in the repository's audit - the
    disclosure is honest, so it is reported and the run continues. The
    gate mirrors that, so the two agree about what blocks and what does
    not.
    """
    report = "| Company | Price |\n|---|---|\n| Acme | not fetched |\n| Initech | $0.50 |\n"
    result = mechanical_gate(_scan(), report_text=report, opt_ins={})
    assert result.passed
    assert any("unfetched_table_row" in n for n in result.notes)
    assert result.measurements["soft_defects"] == 1


def test_the_mechanical_gate_asks_no_model():
    """It is a pass/fail gate. There is nothing to ask."""
    scan = _scan()
    result = mechanical_gate(scan, report_text=render_markdown(scan), opt_ins={})
    assert result.measurements["entities"] == 3
    assert result.measurements["report_defects"] == 0


# ── the sufficiency gate ────────────────────────────────────────────────────


def test_criteria_declared_before_the_first_page_are_accepted():
    criteria = MarketCriteria(declared_at=100.0, min_entities=3)
    result = sufficiency_gate(_scan(), criteria, retrieved_at=200.0)
    assert result.passed, result.findings
    assert result.measurements["retrofit"] is False


def test_criteria_declared_after_the_first_page_are_refused():
    """A criterion written with the answer in view is a description, not a requirement."""
    criteria = MarketCriteria(declared_at=300.0, min_entities=3)
    result = sufficiency_gate(_scan(), criteria, retrieved_at=200.0)
    assert not result.passed
    assert result.measurements["retrofit"] is True
    assert any("description of the result" in f for f in result.findings)


def test_a_run_with_no_recorded_retrieval_cannot_be_sufficient():
    result = sufficiency_gate(_scan(), MarketCriteria(declared_at=100.0), retrieved_at=None)
    assert not result.passed
    assert any("no retrieval" in f for f in result.findings)


def test_no_criteria_means_sufficiency_was_never_checked():
    result = sufficiency_gate(_scan(), None, retrieved_at=200.0)
    assert not result.passed
    assert any("no criteria were declared" in f for f in result.findings)


def test_a_shortfall_fails_and_names_what_is_missing():
    criteria = MarketCriteria(declared_at=100.0, min_entities=5)
    result = sufficiency_gate(_scan(), criteria, retrieved_at=200.0)
    assert not result.passed
    assert any("fewer than the 5 declared" in f for f in result.findings)
    assert result.measurements["missed"]


def test_a_company_with_no_published_price_fails_a_price_criterion():
    criteria = MarketCriteria(declared_at=100.0, require_price_for_all=True)
    result = sufficiency_gate(_scan(), criteria, retrieved_at=200.0)
    assert not result.passed
    assert any("Globex" in f for f in result.findings)


def test_too_few_source_hosts_fails_a_coverage_criterion():
    criteria = MarketCriteria(declared_at=100.0, min_source_hosts=5)
    result = sufficiency_gate(_scan(), criteria, retrieved_at=200.0)
    assert not result.passed
    assert any("source host" in f for f in result.findings)


def test_a_required_field_absent_from_one_company_fails():
    criteria = MarketCriteria(declared_at=100.0, required_fields=("price",))
    result = sufficiency_gate(_scan(), criteria, retrieved_at=200.0)
    assert not result.passed
    assert any("do not carry 'price'" in f for f in result.findings)


# ── the judge gate ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_judge_that_accepts_passes():
    provider = _Provider('{"meets_criteria": true, "reason": "every claim is traced"}')
    result = await judge_gate(_scan(), provider, position_swap=False)
    assert result.passed, result.findings
    assert result.measurements["judged"] is True
    assert result.measurements["verdict"] is True


@pytest.mark.asyncio
async def test_a_judge_that_rejects_fails_with_its_reason():
    provider = _Provider('{"meets_criteria": false, "reason": "the price column is unsourced"}')
    result = await judge_gate(_scan(), provider, position_swap=False)
    assert not result.passed
    assert any("the price column is unsourced" in f for f in result.findings)


@pytest.mark.asyncio
async def test_the_judge_runs_on_the_cheap_model_by_default():
    """The judge reads a report and answers one question. The big model is for the work."""
    provider = _Provider('{"meets_criteria": true, "reason": "ok"}')
    await judge_gate(_scan(), provider, position_swap=False)
    assert provider.calls[0]["model"] == CHEAP_JUDGE_MODEL
    assert CHEAP_JUDGE_MODEL == "claude-haiku-4-5"


@pytest.mark.asyncio
async def test_a_judge_that_flips_when_the_order_flips_fails_the_gate():
    """A verdict that depends on the order is not a verdict about the scan."""
    provider = _Provider(
        '{"meets_criteria": true, "reason": "ok"}',
        '{"meets_criteria": false, "reason": "the order matters"}',
    )
    result = await judge_gate(_scan(), provider, position_swap=True)
    assert not result.passed
    assert any("depends on the order" in f for f in result.findings)
    assert result.measurements["order_stable"] is False


@pytest.mark.asyncio
async def test_a_judge_that_is_stable_across_the_swap_passes():
    provider = _Provider(
        '{"meets_criteria": true, "reason": "ok"}',
        '{"meets_criteria": true, "reason": "ok"}',
    )
    result = await judge_gate(_scan(), provider, position_swap=True)
    assert result.passed, result.findings
    assert result.measurements["order_stable"] is True
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_the_position_swap_can_be_switched_off_and_the_judge_is_asked_once():
    provider = _Provider('{"meets_criteria": true, "reason": "ok"}')
    await judge_gate(_scan(), provider, position_swap=False)
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_a_judge_that_cannot_be_reached_fails_open():
    """A judge with no verdict is a scan the reader still gets."""
    provider = _Provider(RuntimeError("provider is down"))
    result = await judge_gate(_scan(), provider, position_swap=False)
    assert result.passed
    assert result.measurements["judged"] is False


@pytest.mark.asyncio
async def test_a_judge_that_answers_nonsense_fails_open():
    provider = _Provider("I think it looks quite good actually")
    result = await judge_gate(_scan(), provider, position_swap=False)
    assert result.passed
    assert result.measurements["judged"] is False


# ── the gates are ablatable, one at a time ──────────────────────────────────


@pytest.mark.asyncio
async def test_each_gate_is_switched_off_on_its_own():
    provider = _Provider('{"meets_criteria": true, "reason": "ok"}')

    only_mechanical = await run_market_gates(
        _scan(),
        MarketCriteria(declared_at=100.0),
        config=MarketGateConfig(sufficiency=False, judge=False),
        provider=provider,
        report_text=render_markdown(_scan()),
        retrieved_at=200.0,
    )
    assert [g.name for g in only_mechanical] == ["mechanical", "sufficiency", "judge"]
    assert [g.enabled for g in only_mechanical] == [True, False, False]
    assert provider.calls == []

    only_sufficiency = await run_market_gates(
        _scan(),
        MarketCriteria(declared_at=100.0),
        config=MarketGateConfig(mechanical=False, judge=False),
        provider=provider,
        retrieved_at=200.0,
    )
    assert [g.enabled for g in only_sufficiency] == [False, True, False]

    only_judge = await run_market_gates(
        _scan(),
        MarketCriteria(declared_at=100.0),
        config=MarketGateConfig(mechanical=False, sufficiency=False),
        provider=provider,
        retrieved_at=200.0,
    )
    assert [g.enabled for g in only_judge] == [False, False, True]
    # The judge runs twice by default: once as it is, once with the
    # companies in the opposite order. That second call is the ablation.
    assert len(provider.calls) == 2


@pytest.mark.asyncio
async def test_a_skipped_gate_says_it_was_skipped():
    """An ablated arm must not look like an arm whose gate was satisfied."""
    results = await run_market_gates(
        _scan(),
        MarketCriteria(declared_at=100.0),
        config=MarketGateConfig(mechanical=False, sufficiency=False, judge=False),
        retrieved_at=200.0,
    )
    for result in results:
        assert result.enabled is False
        assert result.measurements.get("skipped") is True


@pytest.mark.asyncio
async def test_the_judge_is_skipped_rather_than_failing_without_a_provider():
    results = await run_market_gates(
        _scan(),
        MarketCriteria(declared_at=100.0),
        provider=None,
        retrieved_at=200.0,
    )
    judge = [g for g in results if g.name == "judge"][0]
    assert judge.enabled is False
    assert judge.passed


# ── the stop policy's numbers ───────────────────────────────────────────────


def test_the_stop_policy_reports_accuracy_precision_and_ece_apart():
    decisions = [
        StopDecision(stopped=True, confidence=0.9, correct=True),
        StopDecision(stopped=True, confidence=0.8, correct=False),
        StopDecision(stopped=False, confidence=0.4, correct=True),
        StopDecision(stopped=False, confidence=0.6, correct=True),
    ]
    metrics = stop_metrics(decisions)
    assert metrics.accuracy == 0.75
    assert metrics.precision == 0.5
    assert metrics.ece is not None and metrics.ece > 0
    assert metrics.decisions == 4
    assert metrics.stops == 2


def test_a_policy_that_never_stops_has_no_precision_to_report():
    """No stop, no precision - and its calibration is still measured.

    The ECE is not zero: a policy that says "50% sure" about a decision it
    gets right every time is badly calibrated, and a metric that skipped
    the decisions it never makes would score that policy as perfect.
    """
    decisions = [StopDecision(stopped=False, confidence=0.5, correct=True) for _ in range(4)]
    metrics = stop_metrics(decisions)
    assert metrics.precision is None
    assert metrics.ece == 0.5
    assert metrics.accuracy == 1.0


def test_no_labels_means_no_numbers():
    metrics = stop_metrics([])
    assert metrics.measured() is False
    assert metrics.accuracy is None
    assert metrics.precision is None
    assert metrics.ece is None


def test_a_regression_in_precision_fails_even_when_accuracy_improves():
    """The gate watches precision, so a change cannot buy accuracy with it.

    The fixture is the shape of the trade the gate exists to catch: the
    policy stopped on the same number of decisions and got them wrong more
    often, while the decisions it carried on through became right more
    often. Accuracy rose. Precision fell. The comparison has to flag the
    precision, because accuracy alone would have called this an improvement.
    """
    baseline = stop_metrics(
        [
            StopDecision(stopped=True, confidence=0.5, correct=True),
            StopDecision(stopped=True, confidence=0.5, correct=False),
            StopDecision(stopped=False, confidence=0.5, correct=True),
            StopDecision(stopped=False, confidence=0.5, correct=False),
            StopDecision(stopped=False, confidence=0.5, correct=False),
            StopDecision(stopped=False, confidence=0.5, correct=False),
        ]
    )
    current = stop_metrics(
        [
            StopDecision(stopped=True, confidence=0.5, correct=False),
            StopDecision(stopped=True, confidence=0.5, correct=False),
            StopDecision(stopped=False, confidence=0.5, correct=True),
            StopDecision(stopped=False, confidence=0.5, correct=True),
            StopDecision(stopped=False, confidence=0.5, correct=True),
            StopDecision(stopped=False, confidence=0.5, correct=True),
        ]
    )
    assert current.accuracy > baseline.accuracy
    assert current.precision < baseline.precision

    movements = compare_stop_metrics(current, baseline)
    precision = [m for m in movements if m.metric == "precision"][0]
    assert precision.regressed is True


def test_a_rise_in_calibration_error_fails():
    baseline = stop_metrics(
        [StopDecision(stopped=True, confidence=1.0, correct=True) for _ in range(4)]
    )
    current = stop_metrics(
        [
            StopDecision(stopped=True, confidence=1.0, correct=False),
            StopDecision(stopped=True, confidence=1.0, correct=True),
            StopDecision(stopped=True, confidence=1.0, correct=False),
            StopDecision(stopped=True, confidence=1.0, correct=False),
        ]
    )
    movements = compare_stop_metrics(current, baseline)
    ece = [m for m in movements if m.metric == "ece"][0]
    assert ece.regressed is True
    assert current.accuracy < baseline.accuracy


def test_accuracy_alone_never_fails_the_comparison():
    """A gate that watched accuracy would reward a policy that stops more often."""
    baseline = stop_metrics([StopDecision(stopped=True, confidence=0.5, correct=True)])
    current = stop_metrics([StopDecision(stopped=True, confidence=0.5, correct=True)])
    movements = compare_stop_metrics(current, baseline)
    assert all(not m.regressed for m in movements)
    assert {m.metric for m in movements} == {"precision", "ece"}


def test_a_missing_baseline_is_not_a_regression():
    metrics = stop_metrics([StopDecision(stopped=True, confidence=0.5, correct=True)])
    assert compare_stop_metrics(metrics, None) == []
    movements = compare_stop_metrics(metrics, StopMetrics())
    assert all(not m.regressed for m in movements)
    assert all("not measured" in m.describe() for m in movements)


# ── the pin to the repository's audit ───────────────────────────────────────


def _report(body: str) -> str:
    return (
        "> A short answer with a citation: [Acme](https://acme.example/pricing).\n\n"
        "## Findings\n\n"
        f"{body}\n\n"
        "## Limitations\n\n"
        "- A limitation.\n"
    )


ADVERSARIAL = {
    "clean": "| Company | Price |\n|---|---|\n| Acme | $49 |\n| Initech | $0.50 |\n",
    "estimate": "| Company | Price |\n|---|---|\n| Acme | ~$49 |\n| Initech | $0.50 |\n",
    "not_fetched": "| Company | Price |\n|---|---|\n| Acme | not fetched |\n| Initech | $0.50 |\n",
    "wide": "| A | B | C | D | E | F | G | H | I | J |\n"
    + "|".join("---" for _ in range(10))
    + "\n"
    + "|".join(f"v{i}" for i in range(10))
    + "\n",
}


def test_this_package_and_the_repositories_audit_find_the_same_defects(tmp_path):
    """The two checkers live apart on purpose. This test is what holds them together.

    The audit has to run against a report on a machine with no raven checkout on
    the path, and the flow plugin ships without the repo's scripts, so neither
    can import the other. A marker added to one and not the other would
    otherwise widen one checker's blind spot quietly - and the pin is where
    that shows up instead.
    """
    for name, table in ADVERSARIAL.items():
        report = _report(table)
        path = tmp_path / f"{name}.md"
        path.write_text(report, encoding="utf-8")

        mine = {d["kind"] for d in report_defects(report)}
        theirs = {f.kind for f in audit(path).findings}

        table_kinds = {
            "estimate_in_quantitative_column",
            "unfetched_table_row",
            "too_many_compared_columns",
            "rank_contradicts_total",
        }
        assert mine & table_kinds == theirs & table_kinds, (
            f"{name}: this package found {sorted(mine & table_kinds)}, "
            f"the audit found {sorted(theirs & table_kinds)}"
        )
