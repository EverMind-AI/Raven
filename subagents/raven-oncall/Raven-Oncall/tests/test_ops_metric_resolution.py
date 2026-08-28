"""Which metric a campaign is read by, when nobody named one.

This tool layer is shared with every domain that runs jobs, so it must not know
any metric by name. It used to default to "ndcg" in five places, which is how a
CFD campaign came to read back "trials succeeded but none reported metric 'ndcg'"
for runs that reported their residual perfectly well.

Removing the default cannot mean rewriting the campaigns that predate the
``objective`` field: r13 and r14 are on disk, and editing their meta to suit
today's code would edit the record of what the device was while they ran. What
their records themselves report is a fact rather than a guess, so that is the
last resort -- and only when it settles the question outright.
"""

from __future__ import annotations

import json
from pathlib import Path

from raven.agent.tools.ops import _campaign_metric


class _Result:
    def __init__(self, metrics: dict) -> None:
        self.metrics = metrics


class _Record:
    def __init__(self, metrics: dict | None) -> None:
        self.result = _Result(metrics) if metrics is not None else None


def _meta(tmp_path: Path, body: dict | None) -> Path:
    path = tmp_path / "meta.json"
    if body is not None:
        path.write_text(json.dumps(body), encoding="utf-8")
    return path


def test_what_the_caller_asked_for_wins(tmp_path: Path) -> None:
    meta = _meta(tmp_path, {"objective": {"metric": "f1", "direction": "max"}})

    assert _campaign_metric(meta, "pass@1", [_Record({"ndcg": 0.3})]) == "pass@1"


def test_the_declared_objective_is_used_when_nothing_is_asked(tmp_path: Path) -> None:
    meta = _meta(tmp_path, {"objective": {"metric": "residual", "direction": "min"}})

    assert _campaign_metric(meta, "", [_Record({"ndcg": 0.3})]) == "residual"


def test_a_campaign_predating_the_field_is_read_by_what_its_records_report(tmp_path: Path) -> None:
    """r13 and r14 in one line: no objective on disk, one metric in the ledger."""
    meta = _meta(tmp_path, {"host": "h", "budget_minutes_total": 140})

    assert _campaign_metric(meta, "", [_Record({"ndcg": 0.362, "gpu_minutes_used": 30.0})]) == "ndcg"


def test_infrastructure_readings_are_never_the_answer(tmp_path: Path) -> None:
    """gpu_minutes_used is a reading about the run, not the score it is judged by.
    Counting it as a candidate would rank a campaign by how long it ran."""
    meta = _meta(tmp_path, {"host": "h"})

    assert _campaign_metric(meta, "", [_Record({"gpu_minutes_used": 30.0})]) == ""


def test_several_candidates_are_not_guessed_between(tmp_path: Path) -> None:
    """Picking one of several would rank the campaign by a number nobody chose,
    and that reads exactly like a real ranking."""
    meta = _meta(tmp_path, {"host": "h"})

    assert _campaign_metric(meta, "", [_Record({"ndcg": 0.3, "recall": 0.8})]) == ""


def test_no_records_and_no_declaration_resolves_to_nothing(tmp_path: Path) -> None:
    assert _campaign_metric(_meta(tmp_path, {"host": "h"}), "", []) == ""


def test_a_missing_meta_file_is_not_an_error(tmp_path: Path) -> None:
    assert _campaign_metric(_meta(tmp_path, None), "", [_Record({"f1": 0.7})]) == "f1"


def test_records_without_results_are_skipped(tmp_path: Path) -> None:
    meta = _meta(tmp_path, {"host": "h"})

    assert _campaign_metric(meta, "", [_Record(None), _Record({"f1": 0.7})]) == "f1"
