"""Round-trips for the re-split state modules (fork module names, fork shapes).

Part one folded ledger/attendance/budget/instrument into one ``state.py``;
part 2a re-splits them into the fork's own module names so the clean test
batch (2c) lands with import redirects only. What these tests pin is the
on-disk contract every later part builds on: the durable ledger (atomic,
refuses to open corrupt, typed JobResult), the three-meter budget with the
legacy keys, attendance counted from the trail, the campaign directory
layout, and the probe/decision records.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-oncall" / "plugins" / "oncall-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from oncall_flow import budget  # noqa: E402
from oncall_flow.attendance import attendance, off_machine_spend  # noqa: E402
from oncall_flow.backend import JobHandle, JobResult, JobSpec, JobStatus  # noqa: E402
from oncall_flow.instrument import (  # noqa: E402
    CampaignStore,
    campaign_slug,
    conclude,
    is_concluded,
    log_event,
    read_events,
    read_meta,
    write_meta,
)
from oncall_flow.ledger import Ledger, LedgerCorruptError  # noqa: E402
from oncall_flow.state_claims import (  # noqa: E402
    StateFacts,
    read_decision_seq,
    read_facts,
    write_decision_seq,
    write_facts,
)

# ── The durable ledger ──────────────────────────────────────────────


def test_the_ledger_round_trips_every_field(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    led = Ledger(path)
    led.record("t1", campaign="camp", config={"lr": 2e-05})
    led.set_handle("t1", JobHandle("mock", "job-9"))
    led.bump_attempts("t1")
    led.set_result(
        "t1",
        JobResult(
            JobStatus.SUCCEEDED,
            metrics={"score": 0.91},
            output={"config": {"lr": 2e-05}},
            deliverable={"ref": "s3://ckpt", "label": "best step", "value": 0.91},
        ),
    )
    led.mark_escalated("t1")

    reopened = Ledger(path).get("t1")
    assert reopened.status is JobStatus.SUCCEEDED and reopened.is_terminal
    assert reopened.campaign == "camp" and reopened.config == {"lr": 2e-05}
    assert reopened.handle == JobHandle("mock", "job-9")
    assert reopened.attempts == 1 and reopened.escalated is True
    assert reopened.result.metrics == {"score": 0.91}
    assert reopened.result.deliverable["ref"] == "s3://ckpt"


def test_the_ledger_refuses_to_open_corrupt(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    Ledger(path).record("t1")
    path.write_text("{definitely not json", encoding="utf-8")
    try:
        Ledger(path)
        raise AssertionError("an unreadable ledger must refuse, not restart empty")
    except LedgerCorruptError as exc:
        assert "move it aside deliberately" in str(exc)


def test_recording_twice_is_one_record_and_configs_backfill(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "ledger.json")
    first = led.record("t1", campaign="camp")
    again = led.record("t1", campaign="camp", config={"lr": 0.1})
    assert again is first
    assert first.config == {"lr": 0.1}, "a record written before configs were kept picks its config up on resubmit"
    led.record("t2", campaign="other")
    assert [r.idem_key for r in led.by_campaign("camp")] == ["t1"]
    assert [r.idem_key for r in led.pending()] == ["t1", "t2"]


def test_a_job_spec_refuses_an_empty_idem_key() -> None:
    try:
        JobSpec({"cfg": 1}, idem_key="  ")
        raise AssertionError("an empty key collapses jobs into one directory; must refuse")
    except ValueError:
        pass


# ── Budgets: three meters, legacy keys, overlap math ────────────────


def test_budget_from_meta_reads_the_declaration_and_never_guesses_the_meter() -> None:
    declared = budget.from_meta({"budget": {"total": 12, "unit": "look", "meter": "look", "overlap": "additive"}})
    assert declared == budget.Budget(unit="look", total=12.0, overlap=budget.ADDITIVE, meter=budget.LOOKS)
    assert declared.off_machine is True

    vague = budget.from_meta({"budget": {"total": 60, "unit": "minute"}})
    assert vague.meter == budget.COMPUTE, "a 'minute' is never guessed into a wall-clock minute"
    assert vague.off_machine is False

    assert budget.from_meta({}) is None, "no budget is a first-class answer, not a zero"
    assert budget.from_meta({"budget": {"unit": "x"}}) is None


def test_legacy_budget_keys_still_read_in_their_original_order() -> None:
    both = budget.from_meta({"budget_core_minutes_total": 30, "budget_minutes_total": 60})
    assert both == budget.Budget(unit="core-minute", total=30.0, overlap=budget.ADDITIVE), (
        "a campaign carrying both keys is a CFD one: core-minutes read in preference"
    )
    gpu = budget.from_meta({"budget_minutes_total": 60})
    assert gpu == budget.Budget(unit="gpu-minute", total=60.0, overlap=budget.SHARED)


def test_accumulate_bills_shared_overlap_once_at_the_widest() -> None:
    additive = budget.accumulate([(0, 60, 8), (0, 60, 8)], overlap=budget.ADDITIVE)
    assert additive == 16.0, "eight cores and eight more cores for a minute is sixteen core-minutes"

    shared = budget.accumulate([(0, 60, 1), (30, 90, 2)], overlap=budget.SHARED)
    assert shared == 2.5, "an overlap is counted once, at the greatest width running across it"
    assert budget.accumulate([], overlap=budget.SHARED) == 0.0


# ── Attendance: counted from the trail, not the model's word ────────


def test_attendance_reads_looks_wakes_and_the_declaration_clock(tmp_path: Path) -> None:
    cdir = tmp_path / "camp"
    opened = datetime.now() - timedelta(minutes=90)
    write_meta(cdir, {"declared_at": opened.isoformat(timespec="seconds")})
    write_facts(cdir, StateFacts(probe_seq=3))
    log_event(cdir, "wake_scheduled", reason="round due")
    log_event(cdir, "check_later", eta=600)
    log_event(cdir, "note", note="not a wake kind")

    kept = attendance(cdir, now=opened + timedelta(minutes=90))
    assert kept.looks == 3, "looks come from the probe counter the tool layer writes"
    assert kept.wakes == 2
    assert kept.minutes_open is not None and abs(kept.minutes_open - 90.0) < 1.0

    empty = attendance(tmp_path / "nowhere")
    assert empty.looks == 0 and empty.wakes == 0 and empty.opened_at is None, (
        "an unreadable campaign reports what could be read, never raises"
    )


def test_off_machine_spend_answers_only_what_the_campaign_can_measure(tmp_path: Path) -> None:
    cdir = tmp_path / "camp"
    write_meta(cdir, {"declared_at": (datetime.now() - timedelta(minutes=30)).isoformat()})
    write_facts(cdir, StateFacts(probe_seq=7))

    looks = budget.Budget(unit="look", total=12, meter=budget.LOOKS)
    clock = budget.Budget(unit="minute", total=60, meter=budget.WALL_CLOCK)
    compute = budget.Budget(unit="gpu-minute", total=60, meter=budget.COMPUTE)

    assert off_machine_spend(cdir, looks) == 7.0
    spent = off_machine_spend(cdir, clock)
    assert spent is not None and spent >= 29.0
    assert off_machine_spend(cdir, compute) is None, "None, not zero: this reader does not know machine time"


# ── The campaign directory layout ───────────────────────────────────


def test_the_slug_rule_is_shared_and_fork_verbatim(tmp_path: Path) -> None:
    assert campaign_slug("Camp A") == "camp-a"
    assert campaign_slug("x" * 40) == "x" * 24
    assert campaign_slug("!!!") == "campaign"
    store = CampaignStore(tmp_path)
    assert store.dir_for("Camp A") == store.dir_for("camp-a"), "every writer lands in the same campaign directory"


def test_meta_conclusion_and_events_round_trip(tmp_path: Path) -> None:
    store = CampaignStore(tmp_path / "state")
    assert store.campaign_dirs() == [], "a missing root lists nothing"
    cdir = store.dir_for("camp")
    write_meta(cdir, {"objective": "hold", "backend": "mock"})
    assert read_meta(cdir)["objective"] == "hold"
    assert store.campaign_dirs() == [cdir]

    assert is_concluded(cdir) is False
    conclude(cdir, {"outcome": "done"})
    assert is_concluded(cdir) is True

    log_event(cdir, "kill", trial="t1", reason="diverged")
    (cdir / "events.jsonl").open("a", encoding="utf-8").write("not json\n")
    events = read_events(cdir)
    assert [e["kind"] for e in events] == ["kill"], "a bad line is skipped, not fatal"

    try:
        read_meta(store.dir_for("never-declared"))
        raise AssertionError("a missing declaration raises; nothing invents an empty one")
    except OSError:
        pass


# ── Probe facts and the decision counter ────────────────────────────


def test_state_facts_round_trip_and_absence_is_all_unknown(tmp_path: Path) -> None:
    cdir = tmp_path / "camp"
    facts = StateFacts(
        checkpoints=("step-100", "step-200"),
        job_statuses=("running",),
        metric_readings={"loss": (0.5, 0.42)},
        probe_seq=4,
        shown_values=(64.0, 2e-05),
        sources={"loss": "progress.jsonl"},
    )
    write_facts(cdir, facts)
    back = read_facts(cdir)
    assert back == facts

    assert read_facts(tmp_path / "nowhere") == StateFacts(), "missing means all-unknown"
    (cdir / "state_facts.json").write_text("{broken", encoding="utf-8")
    assert read_facts(cdir) == StateFacts(), "unreadable means all-unknown, never a crash"


def test_the_decision_counter_lives_beside_not_inside_the_facts(tmp_path: Path) -> None:
    cdir = tmp_path / "camp"
    assert read_decision_seq(cdir) == 0
    write_decision_seq(cdir, 3)
    assert read_decision_seq(cdir) == 3
    write_facts(cdir, StateFacts(probe_seq=9))
    assert read_decision_seq(cdir) == 3, "a probe never overwrites the decision record"
