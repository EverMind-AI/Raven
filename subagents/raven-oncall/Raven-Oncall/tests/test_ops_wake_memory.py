"""What a wake can know about what the last turn worked out.

A wake runs in a fresh session. Measured 2026-08-06 on a real on-call round: one
turn read the case files, derived that the fluid's kinematic viscosity was a
thousand times too large (nu = mu/rho, 1e-3/1000 = 1e-6), and said so; the wake
thirty minutes later opened a different file, reported "numerically normal", and
never mentioned it again. Session files confirm it -- the TUI session mentions
the viscosity four times, the two cron sessions zero.

Two halves are tested here:

  * the conclusion has somewhere to land: ops_check_later requires a basis and
    writes it into the campaign's events;
  * it is handed back at the next wake: ops_tune_status renders the campaign's
    events at call time.

Call time matters. The wake message is written when the wake is *scheduled* and
replayed unchanged when it fires, so anything injected there is a snapshot from
an hour earlier while looking entirely current -- the failure shape this line
keeps meeting, where broken and working produce the same output.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.tools.ops import OpsCheckLaterTool, OpsTuneStatusTool, _campaign_history

BASIS = "alpha bounds fine, but nu=1e-3 is 1000x water; revisit before trusting the result"


def _campaign(tmp_path: Path) -> Path:
    cdir = tmp_path / "camp"
    cdir.mkdir()
    (cdir / "meta.json").write_text(json.dumps({"backend": "process"}), encoding="utf-8")
    (cdir / "ledger.json").write_text(json.dumps({
        "version": 1,
        "records": {"run1": {"idem_key": "run1", "status": "running", "campaign": "c",
                             "handle": {"backend": "process", "job_id": "j"},
                             "result": None, "attempts": 0, "escalated": False}},
    }), encoding="utf-8")
    return cdir



def _seeded_campaign(tmp_path, monkeypatch):
    """A campaign whose facts already carry one probe, so the basis gate has a
    reading to accept against. The gate refuses a decision with no observation
    recorded since the previous one -- which is why a bare check_later, with no
    status call in front of it, is refused here."""
    import dataclasses

    from raven.ops.state_claims import read_facts, write_facts

    cdir = _campaign(tmp_path)
    facts = read_facts(cdir)
    facts = dataclasses.replace(
        facts,
        metric_readings={"ndcg": (0.2937,)},
        probe_seq=facts.probe_seq + 1,
        # What the probe printed: a basis may cite any of it, not only the metric.
        shown_values=(0.2937, 1e-3, 1000.0, 600.0),
    )
    write_facts(cdir, facts)
    return cdir


def test_basis_is_required_so_a_conclusion_cannot_be_left_unsaid():
    tool = OpsCheckLaterTool(cron_service=None)
    assert "basis" in tool.parameters["required"]


@pytest.mark.asyncio
async def test_the_basis_is_written_into_the_campaigns_events(tmp_path, monkeypatch):
    """This tree records the basis on its own ``basis_accepted`` event rather than
    as a field of ``check_later``, because the basis passes a gate first: it must
    cite a reading taken since the previous decision. Either way the text lands in
    ``events.jsonl``, which is what the next wake reads back."""
    cdir = _seeded_campaign(tmp_path, monkeypatch)

    out = await OpsCheckLaterTool(cron_service=None).execute(
        campaign="c", ledger=str(cdir / "ledger.json"), eta_seconds=600, basis=BASIS)
    assert "REFUSED" not in out, out

    events = [json.loads(x) for x in (cdir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert [e for e in events if e["kind"] == "check_later"], events
    accepted = [e for e in events if e["kind"] == "basis_accepted"]
    assert accepted and accepted[-1]["basis"] == BASIS


@pytest.mark.asyncio
async def test_a_missing_basis_is_refused_rather_than_recorded_empty(tmp_path, monkeypatch):
    """Divergence from the branch this came from, kept on purpose. There an empty
    basis is recorded as "(none given)"; here it is refused outright, and no wake
    is scheduled. Refusing is the stronger choice: a decision whose basis is blank
    is exactly the decision the field exists to make impossible."""
    cdir = _seeded_campaign(tmp_path, monkeypatch)

    out = await OpsCheckLaterTool(cron_service=None).execute(
        campaign="c", ledger=str(cdir / "ledger.json"), eta_seconds=600, basis="")

    assert "REFUSED" in out
    events = [json.loads(x) for x in (cdir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert not [e for e in events if e["kind"] == "check_later"]


@pytest.mark.asyncio
async def test_the_next_status_call_shows_it(tmp_path, monkeypatch):
    """The half that closes the loop: what the previous turn concluded is in
    front of whoever handles the next wake."""
    cdir = _seeded_campaign(tmp_path, monkeypatch)
    await OpsCheckLaterTool(cron_service=None).execute(
        campaign="c", ledger=str(cdir / "ledger.json"), eta_seconds=600, basis=BASIS)

    out = await OpsTuneStatusTool().execute(campaign="c", ledger=str(cdir / "ledger.json"))

    assert "record so far" in out
    assert BASIS in out


def test_history_is_printed_verbatim_and_not_summarised(tmp_path):
    """Operands, not conclusions: no ranking, no counting, no 'you have waited
    three times'. Drawing the conclusion is the agent's job."""
    cdir = _campaign(tmp_path)
    (cdir / "events.jsonl").write_text("\n".join(
        json.dumps({"ts": f"t{i}", "kind": "check_later", "eta_seconds": 600, "basis": f"b{i}"})
        for i in range(3)
    ), encoding="utf-8")

    lines = _campaign_history(cdir)

    assert [l.split("] ")[0] for l in lines] == ["[t0", "[t1", "[t2"], "oldest first, unsorted"
    assert all("b%d" % i in lines[i] for i in range(3))
    joined = " ".join(lines).lower()
    for word in ("times", "best", "trend", "总结", "第 3 次"):
        assert word not in joined


def test_history_is_bounded_so_it_cannot_crowd_out_the_status(tmp_path):
    cdir = _campaign(tmp_path)
    (cdir / "events.jsonl").write_text("\n".join(
        json.dumps({"ts": f"t{i}", "kind": "check_later"}) for i in range(50)
    ), encoding="utf-8")

    lines = _campaign_history(cdir)

    assert len(lines) == 12
    assert lines[-1].startswith("[t49"), "the newest must survive the trim"


def test_no_events_file_is_not_an_error(tmp_path):
    assert _campaign_history(tmp_path) == []
