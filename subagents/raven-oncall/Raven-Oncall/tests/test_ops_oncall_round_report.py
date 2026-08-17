"""Unit tests for the on-call round report (benchmarks.ops_oncall.round_report).

The determinism gate is the load-bearing part: round 2 reuses round 1's A arm as
its counterfactual, so a silent pass on a curve that does not match would let a
void comparison through. It is tested in both directions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from benchmarks.ops_oncall.round_report import (  # noqa: E402
    ARM_A_CURVE,
    DETERMINISM_POINTS,
    Curve,  # noqa: E402
    check_determinism,
    contract_readings,
    interventions,
    main,
    read_curve,
    unguarded_paths,
)


def _write_progress(path: Path, points, extra_lines=()) -> Path:
    lines = [json.dumps({"event": "start", "elapsed_s": 0.0})]
    lines += [
        json.dumps({"step": s, "eval_ndcg": v, "elapsed_s": float(i * 240)}) for i, (s, v) in enumerate(points, start=1)
    ]
    lines += list(extra_lines)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _sealed_points():
    return [(s, v) for s, v, _ in ARM_A_CURVE]


# --- the determinism gate ---


def test_gate_passes_on_the_sealed_curve():
    assert check_determinism(Curve([(s, v, 0.0) for s, v, _ in ARM_A_CURVE])) == []


def test_gate_fails_on_a_one_digit_change():
    """A four-decimal difference is a real difference: the claim is bit-identical
    training, so this is checked exactly rather than with a tolerance."""
    perturbed = [(s, 0.3607 if s == 1200 else v, 0.0) for s, v, _ in ARM_A_CURVE]

    problems = check_determinism(Curve(perturbed))

    assert len(problems) == 1
    assert "step 1200" in problems[0] and "0.3607" in problems[0]


def test_gate_fails_when_a_gate_step_is_missing():
    """A run that never reached the step is not a pass. Skipping the check when
    the point is absent is how a void comparison gets through."""
    without_2400 = [(s, v, 0.0) for s, v, _ in ARM_A_CURVE if s != 2400]

    problems = check_determinism(Curve(without_2400))

    assert len(problems) == 1
    assert "no eval point" in problems[0]


def test_gate_covers_three_spread_out_steps():
    assert [step for step, _ in DETERMINISM_POINTS] == [200, 1200, 2400]


def test_main_exits_nonzero_on_a_determinism_mismatch(tmp_path, capsys):
    points = [(s, 0.9 if s == 200 else v) for s, v in _sealed_points()]
    progress = _write_progress(tmp_path / "progress.jsonl", points)
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")

    code = main(["--events", str(tmp_path / "events.jsonl"), "--progress", str(progress)])

    assert code == 2
    assert "DETERMINISM CHECK FAILED" in capsys.readouterr().err


def test_main_exits_zero_on_the_sealed_curve(tmp_path):
    progress = _write_progress(tmp_path / "progress.jsonl", _sealed_points())
    (tmp_path / "events.jsonl").write_text("", encoding="utf-8")

    assert main(["--events", str(tmp_path / "events.jsonl"), "--progress", str(progress)]) == 0


def test_self_test_flag_fails_when_interventions_are_present(tmp_path):
    """The A arm is defined by not being touched, so an intervention on it means
    the file is not what the caller thinks it is."""
    progress = _write_progress(tmp_path / "progress.jsonl", _sealed_points())
    events = tmp_path / "events.jsonl"
    events.write_text(json.dumps({"ts": "t", "kind": "kill", "trial": "x"}) + "\n", encoding="utf-8")

    assert main(["--events", str(events), "--progress", str(progress), "--self-test"]) == 3


# --- reading the curve ---


def test_curve_reads_a_result_json(tmp_path):
    path = tmp_path / "result.json"
    path.write_text(json.dumps({"status": "ok", "eval_points": [[200, 0.3527], [400, 0.3583]]}), encoding="utf-8")

    curve = read_curve(path)

    assert curve.at(200) == 0.3527
    assert curve.at(999) is None


def test_curve_skips_unparsable_and_non_eval_lines(tmp_path):
    path = _write_progress(
        tmp_path / "progress.jsonl",
        [(200, 0.3527)],
        extra_lines=["not json at all", json.dumps({"event": "heartbeat", "elapsed_s": 1.0})],
    )

    assert len(read_curve(path).points) == 1


# --- interventions ---


def test_intervention_with_a_step_is_paired_with_the_arm_a_value():
    curve = Curve([(1200, 0.3700, 0.0)])
    arm_a = Curve([(s, v, 0.0) for s, v, _ in ARM_A_CURVE])

    rows = interventions([{"ts": "t", "kind": "kill", "step": 1200}], curve, arm_a)

    assert rows[0]["in_flight"] == 0.3700
    assert rows[0]["arm_a_at_step"] == 0.3606
    assert rows[0]["delta_vs_arm_a"] == 0.0094


def test_intervention_without_a_step_is_left_unattached():
    """Guessing the step from a wall-clock timestamp would invent a position."""
    rows = interventions([{"ts": "t", "kind": "submit"}], Curve([]), Curve([]))

    assert rows[0]["step"] is None
    assert "not attached" in rows[0]["note"]


def test_non_intervention_events_are_ignored():
    assert interventions([{"ts": "t", "kind": "wake_turn"}], Curve([]), Curve([])) == []


# --- contract readings ---


def test_contract_readings_report_the_denominator_and_the_unestimated_count():
    events = [
        {"kind": "ask_owner", "expected_loss_minutes": 200},
        {"kind": "ask_owner", "expected_loss_minutes": -1},
        {"kind": "ask_owner"},
        {"kind": "ask_owner_delivery", "sent": True},
        {"kind": "ask_owner_delivery", "sent": False},
        {"kind": "ask_owner_refused"},
    ]

    out = contract_readings(events)

    assert out["asks_attempted"] == 3
    assert out["expected_loss_minutes"] == [200]
    assert out["expected_loss_unestimated"] == 2
    assert out["expected_loss_denominator"] == 3
    assert out["asks_delivered"] == 1
    assert out["asks_delivery_failed"] == 1
    assert out["asks_refused_by_contract"] == 1


def test_report_refusals_are_broken_down_by_reason_and_missing_field():
    events = [
        {"kind": "report_accepted"},
        {"kind": "report_refused", "reason": "incomplete", "missing": ["baseline"]},
        {"kind": "report_refused", "reason": "incomplete", "missing": ["baseline", "observed"]},
        {"kind": "report_refused", "reason": "state_claim_contradicted"},
    ]

    out = contract_readings(events)

    assert out["reports_accepted"] == 1
    assert out["reports_refused"] == 3
    assert out["report_refusal_reasons"] == {"incomplete": 2, "state_claim_contradicted": 1}
    assert out["report_missing_fields"] == {"baseline": 2, "observed": 1}


# --- the unguarded paths ---


def _span(session, tool, name="tool.call", start="2026-08-04T08:00:00+00:00"):
    attrs = {"tool.name": tool}
    if session is not None:
        attrs["session.key"] = session
    return json.dumps({"name": name, "startTime": start, "attributes": attrs})


def test_message_spans_are_broken_down_by_session_never_totalled(tmp_path):
    """The real log holds every session on the machine. On the box this was
    written on, 25 such spans were 12 CLI + 9 TUI + 3 unattributed + 1 cron; a
    single total would have been a fabricated finding."""
    path = tmp_path / "spans.log"
    path.write_text(
        "\n".join(
            [
                _span("cli:c", "message"),
                _span("cli:c", "message"),
                _span("tui:default", "message"),
                _span("cron:abc", "message"),
                _span(None, "message"),
                _span("cron:abc", "read_file"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    out = unguarded_paths(path, None, None)

    assert out["per_session"]["cli:c"] == {"message": 2}
    assert out["per_session"]["cron:abc"] == {"message": 1}
    assert out["per_session"]["(no session.key on the span)"] == {"message": 1}
    assert out["spans_with_no_session_key"] == 1
    assert "hits" not in out, "an unqualified total is the failure mode this guards"


def test_session_prefix_restricts_the_scan(tmp_path):
    path = tmp_path / "spans.log"
    path.write_text("\n".join([_span("cli:c", "message"), _span("cron:abc", "ask_user")]) + "\n", encoding="utf-8")

    out = unguarded_paths(path, None, "cron:")

    assert list(out["per_session"]) == ["cron:abc"]
    assert out["per_session"]["cron:abc"] == {"ask_user": 1}


def test_unparsable_span_lines_are_counted_not_dropped(tmp_path):
    path = tmp_path / "spans.log"
    path.write_text(_span("cron:abc", "message") + "\nnot json\n", encoding="utf-8")

    out = unguarded_paths(path, None, None)

    assert out["spans_unparsable"] == 1


def test_since_filters_by_timestamp(tmp_path):
    path = tmp_path / "spans.log"
    path.write_text(
        "\n".join(
            [
                _span("cron:abc", "message", start="2026-08-03T00:00:00+00:00"),
                _span("cron:abc", "message", start="2026-08-04T12:00:00+00:00"),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    out = unguarded_paths(path, "2026-08-04T00:00", None)

    assert out["per_session"]["cron:abc"] == {"message": 1}


def test_a_missing_spans_file_is_reported_as_unchecked(tmp_path):
    out = unguarded_paths(tmp_path / "nope.log", None, None)

    assert out["checked"] is False
    assert "does not exist" in out["reason"]


def test_no_spans_file_is_reported_as_unchecked():
    assert unguarded_paths(None, None, None)["checked"] is False
